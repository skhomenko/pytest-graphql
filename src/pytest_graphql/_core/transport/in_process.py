"""The pools behind ``HttpxTransport.for_app``: an application, called in process.

A WSGI application gets ``httpx.WSGITransport``, which calls it in the test's
own thread. An ASGI application gets :class:`_AsgiBridge`, an
``httpx.BaseTransport`` that owns one event loop on one daemon thread and runs
the application there, so the synchronous client can wait for each part of a
response at most a bounded time (DESIGN_DECISIONS.md section 13.3).

The properties this module protects, stated as rules before any code:

    Every thread and event loop the bridge starts is stopped, joined and
    closed on every exit path: ``close()``, a construction that fails, and a
    lifespan startup or shutdown that fails or runs past its bound. Before
    the loop stops, every task on it is cancelled once and given a bounded
    time to finish its cleanup. The one exception is application code that holds the
    loop thread, or ignores its cancellation. Then ``close()`` returns after
    a bounded wait, and the daemon thread closes the loop itself when that
    code returns.

    A response body reaches the client one part at a time, as the
    application produces it, so the client's size cap stops reading before
    the rest is held in memory.

    Every exception that application code raises while it serves a
    request, wherever the pool runs that code, follows ``raise_app_errors``
    and is recorded as the application's (:func:`raised_by_app`). No other
    exception is. The lifespan has its own rules (:class:`_Lifespan`).

The sites of the first rule are: the bridge constructor (a failing thread
start, a failing startup), :meth:`_AsgiBridge.close` (shutdown, then the loop),
the transport constructor that receives the bridge (``for_app`` closes it when
that fails), derived transports (they share the bridge and never close it),
the tasks a timed-out or closed request or lifespan leaves on the loop
(cancelled and drained when the loop stops), and the loop's own default
executor and async generators (shut down before the loop closes). The WSGI
path starts no thread.

Each releasing site goes through the one sweep and report of section 9.3.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import sys
import threading
import time
import weakref
from collections.abc import Callable, Coroutine, Iterable, Iterator, MutableMapping
from typing import Any, Literal, TypeVar

import httpx

from pytest_graphql._core.lifecycle import Closable, _close_all, _Owned, _report

#: How ``for_app`` decides the interface of an application.
AppInterface = Literal["auto", "asgi", "wsgi"]

#: Whether the ASGI lifespan events are sent.
Lifespan = Literal["auto", "on", "off"]

T = TypeVar("T")

_ASGI_VERSION = {"version": "3.0", "spec_version": "2.0"}


def is_asgi(app: object) -> bool:
    """Whether ``app``, or the ``__call__`` of its type, is a coroutine function.

    The method is looked up on the type, as Python does when it calls ``app``.
    """
    return inspect.iscoroutinefunction(app) or (
        callable(app) and inspect.iscoroutinefunction(type(app).__call__)
    )


def _seconds_text(bound: float | None) -> str:
    return f"{bound:g} seconds"


class _BridgeTimeout(httpx.ReadTimeout):
    """The bridge's own timeout: a part of the response did not arrive in time.

    ``HttpxTransport.send`` turns it into the same ``GraphQLTimeoutError`` as
    a read timeout over HTTP.
    """


_APP_ERRORS: weakref.WeakSet[BaseException] = weakref.WeakSet()
_APP_ERRORS_LOCK = threading.Lock()


def _from_app(error: BaseException) -> BaseException:
    """Record ``error`` as raised by application code, and return it.

    Only an ``httpx`` error is recorded: ``HttpxTransport.send`` lets every
    other exception pass anyway. The record is weak, so it holds no
    exception past its last use.
    """
    if isinstance(error, httpx.HTTPError):
        with _APP_ERRORS_LOCK:
            _APP_ERRORS.add(error)
    return error


def raised_by_app(error: BaseException) -> bool:
    """Whether application code that an in-process pool ran raised ``error``.

    ``HttpxTransport.send`` lets such an error leave unchanged, even an
    ``httpx`` one. Every other ``httpx`` error, including one that ``httpx``
    raises while it decodes an in-process response, is classified as over
    HTTP.
    """
    with _APP_ERRORS_LOCK:
        return error in _APP_ERRORS


class _LoopThread:
    """One event loop running on one daemon thread, until :meth:`close`."""

    def __init__(self, *, stop_timeout: float | None) -> None:
        self._stop_timeout = stop_timeout
        self._closed = False
        # Tasks the bridge has cancelled. Only the loop thread touches it.
        self._cancelled: weakref.WeakSet[asyncio.Task[Any]] = weakref.WeakSet()
        self.loop = asyncio.new_event_loop()
        if sys.version_info < (3, 11):
            self.loop.set_task_factory(_recorded_task)
        try:
            self._thread = threading.Thread(
                target=self._run, name="pytest-graphql-asgi", daemon=True
            )
            self._thread.start()
        except BaseException:
            self.loop.close()
            raise

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            try:
                self.loop.run_until_complete(self.loop.shutdown_asyncgens())
                self.loop.run_until_complete(self.loop.shutdown_default_executor())
            finally:
                asyncio.set_event_loop(None)
                self.loop.close()

    def run(
        self, coroutine: Coroutine[Any, Any, T], timeout: float | None
    ) -> concurrent.futures.Future[T] | None:
        """Run ``coroutine`` on the loop and wait at most ``timeout`` seconds.

        Returns the finished future, or ``None`` when the wait expired, after
        asking the loop to cancel the coroutine. The cancellation takes effect
        at the coroutine's next ``await``. ``concurrent.futures.wait`` keeps the
        expiry apart from a ``TimeoutError`` the coroutine raised itself.
        """
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        done, _ = concurrent.futures.wait([future], timeout)
        if not done:
            future.cancel()
            return None
        return future

    @property
    def closed(self) -> bool:
        return self._closed

    def cancel(self, task: asyncio.Task[Any]) -> None:
        """Cancel ``task`` once, from the loop thread.

        A task already cancelled, by this method or by its own code, is left
        alone: a second cancellation would reach the cleanup the first one
        started.
        """
        if _unwinding(task, self._cancelled):
            return
        self._cancelled.add(task)
        task.cancel()

    def _halt(self) -> None:
        """Drain the loop's tasks, then stop it."""
        self._drainer = self.loop.create_task(self._drain())
        self._drainer.add_done_callback(lambda _: self.loop.stop())

    async def _drain(self) -> None:
        """Cancel every other task, and wait at most the stop timeout for them.

        A cancelled task can await its own cleanup, which then finishes here.
        :meth:`cancel` does not cancel a task twice, so that cleanup is not
        interrupted. A task can
        also start another while it cleans up, so the cancellation repeats
        until no task is left or the time is spent.
        """
        current = asyncio.current_task()
        deadline = (
            None
            if self._stop_timeout is None
            else time.monotonic() + self._stop_timeout
        )
        while True:
            tasks = [task for task in asyncio.all_tasks() if task is not current]
            if not tasks:
                return
            for task in tasks:
                self.cancel(task)
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return
            _, pending = await asyncio.wait(tasks, timeout=remaining)
            if pending:
                return

    def close(self) -> None:
        """Drain and stop the loop, join the thread and let it close the loop.

        The join waits at most twice the stop timeout: once for the drain,
        once for the async generators and the default executor. A thread
        still alive after it is held by application code that blocks the
        loop, and as a daemon it does not keep the process alive.
        """
        if self._closed:
            return
        self._closed = True
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._halt)
        bound = self._stop_timeout
        self._thread.join(None if bound is None else 2 * bound)


class _RecordedTask(asyncio.Task[Any]):
    """A task that records a cancellation request, for Python 3.10.

    Python 3.10 has no ``Task.cancelling()``. The bridge's loop creates its
    tasks from this class there, so a cancellation that the application's own
    code requested is seen as well.
    """

    cancel_requested = False

    def cancel(self, msg: Any | None = None) -> bool:
        self.cancel_requested = True
        return super().cancel(msg)


def _recorded_task(loop: asyncio.AbstractEventLoop, coro: Any) -> asyncio.Future[Any]:
    return _RecordedTask(coro, loop=loop)


def _unwinding(task: asyncio.Task[Any], cancelled: weakref.WeakSet[Any]) -> bool:
    """Whether ``task`` was cancelled already and is still handling it.

    This includes a cancellation that the application's own code requested:
    ``Task.cancelling()`` reports it from Python 3.11, and
    :class:`_RecordedTask` on Python 3.10.
    """
    if task in cancelled or getattr(task, "cancel_requested", False):
        return True
    cancelling = getattr(task, "cancelling", None)
    return cancelling is not None and cancelling() > 0


class _AppExitError(Exception):
    """A ``SystemExit`` the application raised, while it crosses the loop.

    A task that ends in ``SystemExit`` stops the event loop, and with it the
    bridge's thread. So the bridge handles the exit as it handles any other
    exception of the application: it carries it as this error, and
    :func:`_result` raises the original in the thread that made the call.
    """

    def __init__(self, exit_: SystemExit) -> None:
        super().__init__(exit_)
        self.exit = exit_


def _carried(error: Exception | SystemExit) -> Exception:
    """``error``, or the carrier of it when it is a ``SystemExit``."""
    return _AppExitError(error) if isinstance(error, SystemExit) else error


def _result(future: concurrent.futures.Future[T]) -> T:
    """The result of a finished future from :meth:`_LoopThread.run`.

    An exit the application raised is raised as the ``SystemExit`` it was.
    """
    try:
        return future.result()
    except _AppExitError as carrier:
        raise carrier.exit from None


def _settle(future: asyncio.Future[T], value: T) -> None:
    if not future.done():
        future.set_result(value)


def _fail(future: asyncio.Future[Any], error: BaseException) -> None:
    if not future.done():
        future.set_exception(error)


class _Lifespan:
    """One ASGI lifespan session: startup when started, shutdown when closed.

    Startup and shutdown each wait at most ``timeout`` seconds, the connect
    timeout. The outcomes of startup:

    - ``lifespan.startup.complete``: started, and shutdown runs at close.
    - ``lifespan.startup.failed``: ``RuntimeError`` with the application's
      message, whether ``required`` or not.
    - The application raises or returns before startup completes: it does not
      support lifespan. Under ``required`` its exception propagates unchanged,
      or a ``RuntimeError`` says it returned. Otherwise the bridge continues
      without lifespan and sends no shutdown.
    - No answer within ``timeout``: ``TimeoutError``.
    """

    def __init__(
        self,
        app: Callable[..., Any],
        state: dict[str, Any],
        runner: _LoopThread,
        *,
        timeout: float | None,
        required: bool,
    ) -> None:
        self._app = app
        self._state = state
        self._runner = runner
        self._timeout = timeout
        self._required = required
        self._started = False
        self._closed = False

    def start(self) -> None:
        future = self._runner.run(self._startup(), self._timeout)
        if future is None:
            raise TimeoutError(
                "the application's ASGI lifespan startup did not finish within "
                f"{_seconds_text(self._timeout)}, the connect timeout."
            )
        self._started = _result(future)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._started:
            return
        future = self._runner.run(self._shutdown(), self._timeout)
        if future is None:
            raise TimeoutError(
                "the application's ASGI lifespan shutdown did not finish within "
                f"{_seconds_text(self._timeout)}, the connect timeout."
            )
        _result(future)

    async def _startup(self) -> bool:
        loop = asyncio.get_running_loop()
        self._messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._startup_result: asyncio.Future[bool] = loop.create_future()
        self._shutdown_result: asyncio.Future[None] = loop.create_future()
        self._messages.put_nowait({"type": "lifespan.startup"})
        self._task = loop.create_task(self._main())
        return await self._startup_result

    async def _shutdown(self) -> None:
        self._messages.put_nowait({"type": "lifespan.shutdown"})
        await self._shutdown_result

    async def _main(self) -> None:
        scope = {"type": "lifespan", "asgi": _ASGI_VERSION, "state": self._state}
        try:
            await self._app(scope, self._messages.get, self._send)
        except (Exception, SystemExit) as error:
            self._ended(_carried(error))
        else:
            self._ended(None)

    def _ended(self, error: Exception | None) -> None:
        """The application returned or raised. Settle what was still waiting."""
        if not self._startup_result.done():
            if not self._required:
                _settle(self._startup_result, False)
            elif error is not None:
                _fail(self._startup_result, error)
            else:
                _fail(
                    self._startup_result,
                    RuntimeError(
                        "the application does not support the ASGI lifespan "
                        "protocol, and lifespan='on' requires it."
                    ),
                )
        elif error is not None:
            _fail(self._shutdown_result, error)
        else:
            _settle(self._shutdown_result, None)

    async def _send(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "lifespan.startup.complete":
            _settle(self._startup_result, True)
        elif kind == "lifespan.shutdown.complete":
            _settle(self._shutdown_result, None)
        elif kind in ("lifespan.startup.failed", "lifespan.shutdown.failed"):
            phase = "startup" if kind == "lifespan.startup.failed" else "shutdown"
            target = (
                self._startup_result if phase == "startup" else self._shutdown_result
            )
            _fail(
                target,
                RuntimeError(
                    f"the application's ASGI lifespan {phase} failed: "
                    f"{message.get('message', '')!r}"
                ),
            )


class _Exchange:
    """One request to an ASGI application, run as one task on the bridge's loop.

    The body reaches the reader one part at a time. The queue between them
    holds one part, so the application waits in its ``send`` until the reader
    takes the part before. A reader that closes the exchange before it took
    the end of the body cancels the application's task, wherever that task
    waits, so no ``send`` waits for a reader that is gone. Every coroutine
    here runs on the loop, so the state needs no lock.

    An exception the application raises is recorded with :func:`_from_app`.
    Under ``raise_app_errors`` it reaches the reader where it happened: from
    :meth:`start` before the response started, from :meth:`next_part` during
    the body, or from :meth:`aclose` after it. Otherwise a response that did
    not start becomes a 500 with an empty body, and a body ends where the
    error happened.
    """

    def __init__(
        self,
        app: Callable[..., Any],
        scope: dict[str, Any],
        body: bytes,
        *,
        raise_app_errors: bool,
        cancel: Callable[[asyncio.Task[Any]], None],
    ) -> None:
        self._app = app
        self._scope = scope
        self._body: bytes | None = body
        self._raise = raise_app_errors
        self._cancel = cancel
        # Whether the application sent the end of the body, and whether the
        # reader took it.
        self._body_ended = False
        self._end_taken = False
        self._late_error: BaseException | None = None

    async def start(self) -> tuple[int, list[tuple[bytes, bytes]]]:
        """Start the application, and wait for its status and headers."""
        loop = asyncio.get_running_loop()
        self._head: asyncio.Future[tuple[int, list[tuple[bytes, bytes]]]] = (
            loop.create_future()
        )
        self._parts: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue(1)
        self._disconnected = asyncio.Event()
        self._task = loop.create_task(self._main())
        try:
            return await self._head
        except asyncio.CancelledError:
            self._cancel(self._task)
            raise

    async def next_part(self) -> bytes | None:
        """The next part of the body, or ``None`` after the last one."""
        part = await self._parts.get()
        if part is None or isinstance(part, BaseException):
            self._end_taken = True
        if isinstance(part, BaseException):
            raise part
        return part

    async def aclose(self) -> None:
        """Tell the application the client left, and wait for its task.

        The task is cancelled first unless the reader took the end of the
        body. Under ``raise_app_errors``, an exception the application raised
        after that is raised here.
        """
        self._disconnected.set()
        if not self._end_taken:
            self._cancel(self._task)
        await asyncio.wait([self._task])
        if self._late_error is not None:
            raise self._late_error

    async def _main(self) -> None:
        try:
            await self._app(self._scope, self._receive, self._send)
        except (Exception, SystemExit) as error:
            await self._failed(_from_app(_carried(error)))
        else:
            if not self._head.done():
                self._head.set_exception(
                    RuntimeError(
                        "the ASGI application returned without sending "
                        "http.response.start."
                    )
                )
            await self._end_body(None)

    async def _failed(self, error: BaseException) -> None:
        if not self._head.done():
            if self._raise:
                self._head.set_exception(error)
                return
            self._head.set_result((500, []))
        elif self._body_ended:
            if self._raise:
                self._late_error = error
            return
        await self._end_body(error if self._raise else None)

    async def _end_body(self, last: BaseException | None) -> None:
        if self._body_ended:
            return
        self._body_ended = True
        await self._parts.put(last)

    async def _receive(self) -> dict[str, Any]:
        if self._body is not None:
            body, self._body = self._body, None
            return {"type": "http.request", "body": body, "more_body": False}
        await self._disconnected.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: MutableMapping[str, Any]) -> None:
        kind = message["type"]
        if kind == "http.response.start":
            if self._head.done():
                raise RuntimeError("http.response.start was sent twice.")
            self._head.set_result((message["status"], list(message.get("headers", []))))
        elif kind == "http.response.body":
            if not self._head.done():
                raise RuntimeError(
                    "http.response.body was sent before http.response.start."
                )
            if self._body_ended:
                return
            part = message.get("body", b"")
            if part:
                await self._parts.put(part)
            if not message.get("more_body", False):
                await self._end_body(None)


class _BodyStream(httpx.SyncByteStream):
    """The body of one bridged response, read part by part from the loop."""

    def __init__(
        self, bridge: _AsgiBridge, exchange: _Exchange, request: httpx.Request
    ) -> None:
        self._bridge = bridge
        self._exchange = exchange
        self._request = request
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        while True:
            part = self._bridge._wait(self._exchange.next_part(), self._request)
            if part is None:
                return
            yield part

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._bridge._runner.closed:
            self._bridge._wait(self._exchange.aclose(), self._request)


class _AsgiBridge(httpx.BaseTransport):
    """An ASGI application on a private loop, behind a synchronous pool.

    The response head and each part of the body wait at most the request's
    read timeout, which the client has already clamped to the call timeout.
    On expiry the waiting step is cancelled, which cancels the application's
    task, and :class:`_BridgeTimeout` is raised. ``close()`` sends the
    lifespan shutdown, then drains and stops the loop. Neither waits past its
    bound for a task that ignores its cancellation.

    Every request scope carries a copy of the lifespan ``state``, as the ASGI
    lifespan specification asks of a server.
    """

    def __init__(
        self,
        app: Callable[..., Any],
        *,
        lifespan: Lifespan,
        raise_app_errors: bool,
        lifespan_timeout: float | None,
    ) -> None:
        self._closed = False
        self._app = app
        self._raise_app_errors = raise_app_errors
        self._state: dict[str, Any] = {}
        # The loop thread first, then the lifespan session. Each is adopted
        # before it starts (9.2), and the sweep closes them in reverse.
        self._items: list[Closable] = []
        try:
            self._runner = _Owned(
                self._items, _LoopThread, stop_timeout=lifespan_timeout
            ).value
            if lifespan != "off":
                session = _Lifespan(
                    app,
                    self._state,
                    self._runner,
                    timeout=lifespan_timeout,
                    required=lifespan == "on",
                )
                self._items.append(session)
                session.start()
        except BaseException as failure:
            errors = _close_all(self._items)
            if errors:
                _report(errors, failure)
            raise

    def _wait(self, coroutine: Coroutine[Any, Any, T], request: httpx.Request) -> T:
        """Run one step of an exchange, at most the request's read timeout."""
        timeout = request.extensions.get("timeout", {}).get("read")
        future = self._runner.run(coroutine, timeout)
        if future is None:
            raise _BridgeTimeout(
                f"the application did not answer within {_seconds_text(timeout)}, "
                "and its request was cancelled. Cancellation takes effect only "
                "at an await, so an application that blocks the event loop "
                "keeps it busy, and each later request on this transport times "
                "out the same way until the loop is free.",
                request=request,
            )
        return _result(future)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": request.method,
            "headers": [(name.lower(), value) for name, value in request.headers.raw],
            "scheme": url.scheme,
            "path": url.path,
            "raw_path": url.raw_path.split(b"?")[0],
            "query_string": url.query,
            "server": (url.host, url.port),
            "client": ("127.0.0.1", 123),
            "root_path": "",
            "state": dict(self._state),
        }
        exchange = _Exchange(
            self._app,
            scope,
            request.read(),
            raise_app_errors=self._raise_app_errors,
            cancel=self._runner.cancel,
        )
        status, headers = self._wait(exchange.start(), request)
        return httpx.Response(
            status,
            headers=headers,
            stream=_BodyStream(self, exchange, request),
        )

    def close(self) -> None:
        """Shut the lifespan down, then the loop, through the one sweep.

        Among ordinary failures the lifespan's wins, because it is the
        application's own.
        """
        if self._closed:
            return
        self._closed = True
        errors = _close_all(self._items)
        if errors:
            _report(errors, errors[0])


class _WsgiResult:
    """The iterable a WSGI application returned, with its errors guarded.

    The application's code runs while the iterable is iterated and closed,
    not only when it is called, so every one of those steps is guarded.
    """

    def __init__(
        self,
        result: Iterable[bytes],
        start_response: Callable[..., Any],
        *,
        raise_app_errors: bool,
    ) -> None:
        self._result = result
        self._parts: Iterator[bytes] | None = None
        self._start_response = start_response
        self._raise = raise_app_errors
        self._output_started = False
        self._ended = False

    def __iter__(self) -> _WsgiResult:
        return self

    def __next__(self) -> bytes:
        if self._ended:
            raise StopIteration
        try:
            if self._parts is None:
                self._parts = iter(self._result)
            part = next(self._parts)
        except StopIteration:
            self._ended = True
            raise
        except Exception as error:
            self._ended = True
            _from_app(error)
            if self._raise:
                raise
            if not self._output_started:
                _start_500(self._start_response)
            raise StopIteration from None
        if part:
            self._output_started = True
        return part

    def close(self) -> None:
        close = getattr(self._result, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception as error:
            _from_app(error)
            if self._raise:
                raise


def _start_500(start_response: Callable[..., Any]) -> None:
    start_response(
        "500 Internal Server Error", [("Content-Type", "text/plain")], sys.exc_info()
    )


def _guarded_wsgi(
    app: Callable[..., Any], *, raise_app_errors: bool
) -> Callable[..., Any]:
    """``app``, with every exception its code raises guarded.

    An exception from the call, from the iteration or from the close of the
    result, and one the application hands to ``start_response``, is recorded
    as the application's. Under ``raise_app_errors`` it propagates.
    Otherwise it becomes a 500 when no body was sent yet, because
    ``httpx.WSGITransport`` builds its 500 response only from the
    ``exc_info`` given to ``start_response``, and the body ends where it
    happened when one was.
    """

    def guarded(environ: Any, start_response: Callable[..., Any]) -> Any:
        def recording_start_response(
            status: str, headers: list[tuple[str, str]], exc_info: Any = None
        ) -> Any:
            if exc_info is not None and exc_info[1] is not None:
                _from_app(exc_info[1])
            return start_response(status, headers, exc_info)

        try:
            result = app(environ, recording_start_response)
        except Exception as error:
            _from_app(error)
            if raise_app_errors:
                raise
            _start_500(start_response)
            return [b""]
        return _WsgiResult(
            result, recording_start_response, raise_app_errors=raise_app_errors
        )

    return guarded


def app_pool(
    app: Callable[..., Any],
    *,
    asgi: bool,
    lifespan: Lifespan,
    raise_app_errors: bool,
    lifespan_timeout: float | None,
) -> httpx.BaseTransport:
    """The pool that calls ``app`` in process, started and ready."""
    if not asgi:
        return httpx.WSGITransport(
            app=_guarded_wsgi(app, raise_app_errors=raise_app_errors),
            raise_app_exceptions=raise_app_errors,
        )
    return _AsgiBridge(
        app,
        lifespan=lifespan,
        raise_app_errors=raise_app_errors,
        lifespan_timeout=lifespan_timeout,
    )
