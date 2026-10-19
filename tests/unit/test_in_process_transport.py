"""``HttpxTransport.for_app`` (DESIGN_DECISIONS.md section 13.3).

Every test drives a project-authored application from ``in_process_apps``,
built from ``graphql-core`` and the standard library, so no socket opens.

The property the ASGI bridge protects: every thread and event loop it starts
is stopped, joined and closed on every exit path, ``close()`` and each failed
construction alike. The tests below read the live threads and the loops the
bridge created after each path.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import threading
import time
from collections.abc import Callable, Iterator, MutableMapping
from typing import Any

import httpx
import pytest
from graphql import GraphQLSchema, build_schema

from pytest_graphql import (
    ArgumentError,
    GraphQLHTTPStatusError,
    GraphQLTimeoutError,
    GraphQLTransportError,
    build_client,
)
from pytest_graphql._core.client import GraphQLClient
from pytest_graphql._core.transport import in_process
from pytest_graphql._core.transport.httpx_transport import HttpxTransport
from tests.unit.in_process_apps import (
    SDL,
    AppCrashError,
    AsgiApp,
    asgi_function,
    wsgi_app,
)

THREAD_NAME = "pytest-graphql-asgi"


def bridge_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == THREAD_NAME]


@pytest.fixture
def loops(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[asyncio.AbstractEventLoop]]:
    """Every event loop the bridge creates during the test, for the close checks."""
    created: list[asyncio.AbstractEventLoop] = []
    original = asyncio.new_event_loop

    def recording() -> asyncio.AbstractEventLoop:
        loop = original()
        created.append(loop)
        return loop

    monkeypatch.setattr(in_process.asyncio, "new_event_loop", recording)
    assert not bridge_threads()
    yield created
    assert not bridge_threads()


def assert_released(loops: list[asyncio.AbstractEventLoop]) -> None:
    """No bridge thread is alive, and every loop the bridge made is closed."""
    assert loops, "the bridge made no loop"
    assert not bridge_threads()
    assert all(loop.is_closed() for loop in loops)


def client_over(
    transport: HttpxTransport,
    url: str = "/graphql",
    schema: GraphQLSchema | None = None,
) -> GraphQLClient:
    return build_client(url=url, transport=transport, schema=schema)


#: The schema of the test apps, for a client that must not introspect.
SCHEMA = build_schema(SDL)


# -- the interface ------------------------------------------------------------


@pytest.mark.parametrize(
    ("app", "asgi"),
    [(asgi_function, True), (AsgiApp(), True), (wsgi_app, False)],
    ids=["async-function", "async-call", "wsgi-function"],
)
@pytest.mark.usefixtures("loops")
def test_auto_picks_the_interface_from_the_app(
    app: Callable[..., Any], asgi: bool
) -> None:
    transport = HttpxTransport.for_app(app)
    try:
        assert isinstance(transport._pool, in_process._AsgiBridge) is asgi
        assert isinstance(transport._pool, httpx.WSGITransport) is not asgi
        assert client_over(transport).query("hello") == "world"
    finally:
        transport.close()
    assert not bridge_threads()


def test_a_forced_interface_overrides_auto(
    loops: list[asyncio.AbstractEventLoop],
) -> None:
    inner = AsgiApp()

    def returns_a_coroutine(
        scope: MutableMapping[str, Any], receive: Any, send: Any
    ) -> Any:
        return inner(scope, receive, send)

    assert not in_process.is_asgi(returns_a_coroutine)
    transport = HttpxTransport.for_app(returns_a_coroutine, interface="asgi")
    try:
        assert isinstance(transport._pool, in_process._AsgiBridge)
        assert client_over(transport).query("hello") == "world"
    finally:
        transport.close()
    assert_released(loops)


def test_an_unknown_interface_or_lifespan_is_an_argument_error() -> None:
    with pytest.raises(ArgumentError) as raised:
        HttpxTransport.for_app(wsgi_app, interface="asgii")  # type: ignore[arg-type]
    assert raised.value.bad_name == "interface"
    assert "Did you mean: asgi" in str(raised.value)
    assert 'Fix: pass interface="asgi"' in str(raised.value)
    with pytest.raises(ArgumentError) as raised:
        HttpxTransport.for_app(AsgiApp(), lifespan="of")  # type: ignore[arg-type]
    assert raised.value.bad_name == "lifespan"


@pytest.mark.parametrize("lifespan", ["on", "off"])
def test_a_lifespan_other_than_auto_for_a_wsgi_app_is_an_argument_error(
    lifespan: in_process.Lifespan,
) -> None:
    with pytest.raises(ArgumentError) as raised:
        HttpxTransport.for_app(wsgi_app, lifespan=lifespan)
    assert raised.value.bad_name == "lifespan"
    assert "WSGI" in str(raised.value)


def test_a_path_url_and_a_full_testserver_url_both_reach_the_app() -> None:
    transport = HttpxTransport.for_app(wsgi_app)
    try:
        assert client_over(transport, "/graphql").query("hello") == "world"
        url = "http://testserver/graphql"
        assert client_over(transport, url).query("hello") == "world"
    finally:
        transport.close()


# -- lifespan -----------------------------------------------------------------


def test_auto_lifespan_starts_now_shares_its_state_and_shuts_down_at_close(
    loops: list[asyncio.AbstractEventLoop],
) -> None:
    app = AsgiApp()
    transport = HttpxTransport.for_app(app)
    assert app.events == ["lifespan.startup"]
    try:
        assert client_over(transport).query("greeting") == "hello from startup"
    finally:
        transport.close()
    assert app.events == ["lifespan.startup", "lifespan.shutdown"]
    assert_released(loops)


@pytest.mark.parametrize("mode", ["unsupported", "returns"])
def test_auto_lifespan_continues_when_the_app_does_not_support_it(
    mode: Any, loops: list[asyncio.AbstractEventLoop]
) -> None:
    app = AsgiApp(lifespan=mode)
    transport = HttpxTransport.for_app(app)
    try:
        assert client_over(transport).query("hello") == "world"
        assert client_over(transport).query("greeting") is None
    finally:
        transport.close()
    assert app.events == []
    assert_released(loops)


def test_off_sends_no_lifespan_event(loops: list[asyncio.AbstractEventLoop]) -> None:
    app = AsgiApp()
    transport = HttpxTransport.for_app(app, lifespan="off")
    transport.close()
    assert app.events == []
    assert_released(loops)


def test_on_lets_the_apps_own_lifespan_exception_leave_unchanged(
    loops: list[asyncio.AbstractEventLoop],
) -> None:
    with pytest.raises(AppCrashError, match="lifespan is not supported"):
        HttpxTransport.for_app(AsgiApp(lifespan="unsupported"), lifespan="on")
    assert_released(loops)


def test_on_refuses_an_app_that_returns_from_the_lifespan_scope(
    loops: list[asyncio.AbstractEventLoop],
) -> None:
    with pytest.raises(RuntimeError, match="does not support the ASGI lifespan"):
        HttpxTransport.for_app(AsgiApp(lifespan="returns"), lifespan="on")
    assert_released(loops)


@pytest.mark.parametrize("lifespan", ["auto", "on"])
def test_an_explicit_startup_failure_raises_under_auto_and_on(
    lifespan: in_process.Lifespan, loops: list[asyncio.AbstractEventLoop]
) -> None:
    with pytest.raises(RuntimeError, match="startup failed: 'no database'"):
        HttpxTransport.for_app(AsgiApp(lifespan="failed"), lifespan=lifespan)
    assert_released(loops)


def test_the_lifespan_startup_waits_at_most_the_connect_timeout(
    loops: list[asyncio.AbstractEventLoop],
) -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=r"0\.2 seconds, the connect timeout"):
        HttpxTransport.for_app(
            AsgiApp(lifespan="hangs"), timeout=httpx.Timeout(30, connect=0.2)
        )
    assert time.monotonic() - started < 5
    assert_released(loops)


def test_a_failed_shutdown_raises_from_close_after_the_loop_is_closed(
    loops: list[asyncio.AbstractEventLoop],
) -> None:
    app = AsgiApp(lifespan="shutdown_failed")
    transport = HttpxTransport.for_app(app)
    with pytest.raises(RuntimeError, match="shutdown failed: 'flush failed'"):
        transport.close()
    assert_released(loops)
    transport.close()  # idempotent: nothing is closed twice


# -- application errors -------------------------------------------------------


@pytest.mark.parametrize("app", [AsgiApp(), wsgi_app], ids=["asgi", "wsgi"])
def test_raise_app_errors_lets_the_apps_exception_reach_the_test(
    app: Callable[..., Any],
) -> None:
    transport = HttpxTransport.for_app(app)
    try:
        with pytest.raises(AppCrashError, match="the application crashed"):
            client_over(transport, "/crash", SCHEMA).query("hello")
    finally:
        transport.close()


@pytest.mark.parametrize("app", [AsgiApp(), wsgi_app], ids=["asgi", "wsgi"])
def test_raise_app_errors_false_turns_the_exception_into_a_500(
    app: Callable[..., Any],
) -> None:
    transport = HttpxTransport.for_app(app, raise_app_errors=False)
    try:
        with pytest.raises(GraphQLHTTPStatusError) as raised:
            client_over(transport, "/crash", SCHEMA).query("hello")
        assert raised.value.status_code == 500
    finally:
        transport.close()


def test_an_httpx_error_the_app_raises_is_neither_converted_nor_retried() -> None:
    calls: list[str] = []
    original = httpx.ConnectError("the app's own upstream call failed")

    def app(environ: dict[str, Any], _start_response: Callable[..., Any]) -> Any:
        calls.append(environ["PATH_INFO"])
        raise original

    transport = HttpxTransport.for_app(app)
    try:
        with pytest.raises(httpx.ConnectError) as raised:
            client_over(transport, schema=SCHEMA).query("hello", raw=True)
        assert raised.value is original
        assert calls == ["/graphql"]
    finally:
        transport.close()


ENVELOPE = json.dumps({"data": {"hello": "world"}}).encode()
JSON_HEADERS = [("Content-Type", "application/json")]


def wsgi_generator(*, fail_after: int, closed: list[bool]) -> Callable[..., Any]:
    """A WSGI app whose body is a generator that fails after ``fail_after`` parts."""

    def app(_environ: dict[str, Any], start_response: Callable[..., Any]) -> Any:
        def body() -> Iterator[bytes]:
            try:
                start_response("200 OK", JSON_HEADERS)
                for _ in range(fail_after):
                    yield ENVELOPE[:5]
                raise AppCrashError("the generator failed")
            finally:
                closed.append(True)

        return body()

    return app


async def asgi_failing_after_start(
    _scope: MutableMapping[str, Any], receive: Any, send: Any
) -> None:
    await receive()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{", "more_body": True})
    raise AppCrashError("the body failed")


@pytest.mark.parametrize(
    "fail_after", [0, 1], ids=["before-the-first-part", "after-a-part"]
)
def test_a_wsgi_generator_error_propagates_under_raise_app_errors(
    fail_after: int,
) -> None:
    closed: list[bool] = []
    app = wsgi_generator(fail_after=fail_after, closed=closed)
    transport = HttpxTransport.for_app(app)
    try:
        with pytest.raises(AppCrashError, match="the generator failed"):
            client_over(transport, schema=SCHEMA).query("hello")
    finally:
        transport.close()
    assert closed == [True]


def test_a_wsgi_generator_error_before_any_part_is_a_500_when_suppressed() -> None:
    closed: list[bool] = []
    app = wsgi_generator(fail_after=0, closed=closed)
    transport = HttpxTransport.for_app(app, raise_app_errors=False)
    try:
        with pytest.raises(GraphQLHTTPStatusError) as raised:
            client_over(transport, schema=SCHEMA).query("hello")
        assert raised.value.status_code == 500
    finally:
        transport.close()
    assert closed == [True]


@pytest.mark.parametrize(
    "app",
    [wsgi_generator(fail_after=1, closed=[]), asgi_failing_after_start],
    ids=["wsgi", "asgi"],
)
def test_an_error_after_the_body_started_ends_it_when_suppressed(
    app: Callable[..., Any],
) -> None:
    transport = HttpxTransport.for_app(app, raise_app_errors=False)
    try:
        with pytest.raises(GraphQLTransportError, match="not a valid GraphQL envelope"):
            client_over(transport, schema=SCHEMA).query("hello")
    finally:
        transport.close()


def test_an_asgi_error_after_the_body_started_propagates_by_default() -> None:
    transport = HttpxTransport.for_app(asgi_failing_after_start)
    try:
        with pytest.raises(AppCrashError, match="the body failed"):
            client_over(transport, schema=SCHEMA).query("hello")
    finally:
        transport.close()


def test_an_httpx_error_the_asgi_app_raises_is_neither_converted_nor_retried() -> None:
    calls: list[str] = []
    original = httpx.ReadError("the app's own upstream call failed")

    async def app(scope: MutableMapping[str, Any], _receive: Any, _send: Any) -> None:
        calls.append(scope["path"])
        raise original

    transport = HttpxTransport.for_app(app, lifespan="off")
    try:
        with pytest.raises(httpx.ReadError) as raised:
            client_over(transport, schema=SCHEMA).query("hello", raw=True)
        assert raised.value is original
        assert calls == ["/graphql"]
    finally:
        transport.close()


def malformed_gzip_wsgi(
    _environ: dict[str, Any], start_response: Callable[..., Any]
) -> Any:
    start_response("200 OK", [*JSON_HEADERS, ("Content-Encoding", "gzip")])
    return [b"this is not gzip"]


async def malformed_gzip_asgi(
    _scope: MutableMapping[str, Any], receive: Any, send: Any
) -> None:
    await receive()
    headers = [(b"content-type", b"application/json"), (b"content-encoding", b"gzip")]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": b"this is not gzip"})


@pytest.mark.parametrize(
    "app", [malformed_gzip_wsgi, malformed_gzip_asgi], ids=["wsgi", "asgi"]
)
def test_an_httpx_error_httpx_raises_on_the_response_is_classified(
    app: Callable[..., Any],
) -> None:
    # Only an error the application raised leaves unchanged. One that httpx
    # raises while it decodes the response is a transport error, as over HTTP.
    transport = HttpxTransport.for_app(
        app, lifespan="off" if app is malformed_gzip_asgi else "auto"
    )
    try:
        with pytest.raises(GraphQLTransportError):
            client_over(transport, schema=SCHEMA).query("hello", raw=True)
    finally:
        transport.close()


# -- the size cap -------------------------------------------------------------


def streaming_asgi(
    sent: list[int], *, parts: int, then_wait: bool, gzipped: bool = False
) -> Callable[..., Any]:
    """An ASGI app that sends ``parts`` parts of 1024 bytes, counting each."""

    async def app(_scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        await receive()
        headers = [(b"content-type", b"application/json")]
        if gzipped:
            headers.append((b"content-encoding", b"gzip"))
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        part = gzip.compress(b" " * 1024 * 1024) if gzipped else b" " * 1024
        for _ in range(parts):
            await send({"type": "http.response.body", "body": part, "more_body": True})
            sent.append(len(part))
        if then_wait:
            await asyncio.Event().wait()
        await send({"type": "http.response.body", "body": b""})

    return app


@pytest.mark.parametrize("then_wait", [False, True], ids=["ends", "then-waits"])
def test_the_size_cap_stops_reading_an_asgi_body_at_the_cap(
    then_wait: bool, loops: list[asyncio.AbstractEventLoop]
) -> None:
    sent: list[int] = []
    app = streaming_asgi(sent, parts=100, then_wait=then_wait)
    transport = HttpxTransport.for_app(app, lifespan="off", max_response_bytes=1024)
    try:
        started = time.monotonic()
        with pytest.raises(GraphQLTransportError, match="exceeded max_response_bytes"):
            client_over(transport, schema=SCHEMA).query("hello", timeout=10)
        # Rejected for its size, not timed out, and the app was stopped after
        # the queue's one part and the part it was sending.
        assert time.monotonic() - started < 5
        assert len(sent) <= 3
    finally:
        transport.close()
    assert_released(loops)


def test_the_size_cap_stops_a_body_whose_last_part_is_waiting(
    loops: list[asyncio.AbstractEventLoop],
) -> None:
    # The last part fills the queue, so the end of the body waits behind it.
    # Closing the exchange still stops the application.
    async def app(_scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {"type": "http.response.body", "body": b"x" * 2048, "more_body": True}
        )
        await send({"type": "http.response.body", "body": b"end"})

    transport = HttpxTransport.for_app(app, lifespan="off", max_response_bytes=1024)
    try:
        for _ in range(3):
            started = time.monotonic()
            with pytest.raises(
                GraphQLTransportError, match="exceeded max_response_bytes"
            ):
                client_over(transport, schema=SCHEMA).query("hello", timeout=10)
            assert time.monotonic() - started < 5
    finally:
        transport.close()
    assert_released(loops)


def test_the_size_cap_counts_decompressed_asgi_bytes() -> None:
    sent: list[int] = []
    app = streaming_asgi(sent, parts=100, then_wait=False, gzipped=True)
    transport = HttpxTransport.for_app(app, lifespan="off", max_response_bytes=4096)
    try:
        with pytest.raises(GraphQLTransportError, match="exceeded max_response_bytes"):
            client_over(transport, schema=SCHEMA).query("hello", timeout=10)
        assert len(sent) <= 3
    finally:
        transport.close()


def test_a_body_part_that_never_comes_times_out() -> None:
    sent: list[int] = []
    app = streaming_asgi(sent, parts=1, then_wait=True)
    transport = HttpxTransport.for_app(app, lifespan="off")
    try:
        with pytest.raises(GraphQLTimeoutError, match=r"did not answer within 0\.2"):
            client_over(transport, schema=SCHEMA).query("hello", timeout=0.2)
    finally:
        transport.close()


# -- timeouts -----------------------------------------------------------------


def test_a_slow_asgi_handler_is_cancelled_at_the_call_timeout() -> None:
    transport = HttpxTransport.for_app(AsgiApp())
    try:
        client = client_over(transport)
        started = time.monotonic()
        with pytest.raises(GraphQLTimeoutError, match=r"did not answer within 0\.2"):
            client.query("slow", seconds=10, timeout=0.2)
        assert time.monotonic() - started < 5
        # The task was cancelled at its await, so the loop is free again.
        assert client.query("hello", timeout=5) == "world"
    finally:
        transport.close()


def test_an_asgi_handler_that_blocks_the_loop_times_out_later_requests_too() -> None:
    transport = HttpxTransport.for_app(AsgiApp())
    try:
        client = client_over(transport)
        with pytest.raises(GraphQLTimeoutError, match="blocks the event loop"):
            client.query("block", seconds=1.0, timeout=0.2)
        with pytest.raises(GraphQLTimeoutError, match="blocks the event loop"):
            client.query("hello", timeout=0.2)
        time.sleep(1.0)
        assert client.query("hello", timeout=5) == "world"
    finally:
        transport.close()


def test_a_slow_wsgi_app_completes_after_the_timeout() -> None:
    # The documented limit: WSGI runs in the test's own thread, so the call
    # timeout bounds nothing. A change that claims a deadline must change the
    # rule in the design first.
    transport = HttpxTransport.for_app(wsgi_app)
    try:
        started = time.monotonic()
        assert client_over(transport).query("slow", seconds=0.5, timeout=0.1) == "slow"
        assert time.monotonic() - started >= 0.5
    finally:
        transport.close()


# -- schema, derivation and cookies -------------------------------------------


@pytest.mark.parametrize("app", [AsgiApp(), wsgi_app], ids=["asgi", "wsgi"])
def test_the_schema_is_introspected_in_process(app: Callable[..., Any]) -> None:
    transport = HttpxTransport.for_app(app)
    try:
        client = client_over(transport)
        assert client.schema.query_type is not None
        assert "hello" in client.schema.query_type.fields
    finally:
        transport.close()


@pytest.mark.parametrize("app", [AsgiApp(), wsgi_app], ids=["asgi", "wsgi"])
def test_client_cookie_scope_keeps_cookies_per_derived_transport(
    app: Callable[..., Any],
) -> None:
    root = HttpxTransport.for_app(app, cookie_scope="client")
    try:
        first, second = root.derive(), root.derive()
        try:
            schema = client_over(root).schema
            ada = build_client(url="/graphql", transport=first, schema=schema)
            other = build_client(url="/graphql", transport=second, schema=schema)
            assert ada.mutation("login", name="ada") == "ada"
            assert ada.query("whoami") == "ada"
            assert other.query("whoami") is None
        finally:
            first.close()
            second.close()
    finally:
        root.close()


def test_the_default_cookie_scope_keeps_no_cookie() -> None:
    transport = HttpxTransport.for_app(wsgi_app)
    try:
        client = client_over(transport)
        assert client.mutation("login", name="ada") == "ada"
        assert client.query("whoami") is None
    finally:
        transport.close()


def test_derived_transports_share_the_bridge_and_only_the_root_closes_it(
    loops: list[asyncio.AbstractEventLoop],
) -> None:
    app = AsgiApp()
    root = HttpxTransport.for_app(app)
    derived = root.derive()
    schema = client_over(root).schema
    derived.close()
    assert bridge_threads(), "closing a derived transport stopped the bridge"
    assert app.events == ["lifespan.startup"]
    survivor = root.derive()
    try:
        client = build_client(url="/graphql", transport=survivor, schema=schema)
        assert client.query("hello") == "world"
    finally:
        survivor.close()
    root.close()
    assert app.events == ["lifespan.startup", "lifespan.shutdown"]
    assert_released(loops)
    root.close()  # idempotent


def test_no_thread_survives_close(loops: list[asyncio.AbstractEventLoop]) -> None:
    transports = [HttpxTransport.for_app(AsgiApp()) for _ in range(3)]
    assert len(bridge_threads()) == 3
    for transport in transports:
        assert client_over(transport).query("hello") == "world"
        transport.close()
    assert_released(loops)


def test_close_lets_a_cancelled_task_finish_its_cleanup(
    loops: list[asyncio.AbstractEventLoop],
) -> None:
    released: list[bool] = []

    async def holder() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.05)
            released.append(True)

    async def app(_scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        asyncio.get_running_loop().create_task(holder())
        await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": ENVELOPE})

    transport = HttpxTransport.for_app(app, lifespan="off", timeout=2)
    assert client_over(transport, schema=SCHEMA).query("hello") == "world"
    transport.close()
    assert released == [True]
    assert_released(loops)


@pytest.mark.parametrize("where", ["head", "body"])
def test_close_does_not_interrupt_the_cleanup_of_a_timed_out_request(
    where: str, loops: list[asyncio.AbstractEventLoop]
) -> None:
    events: list[str] = []
    cleaning = threading.Event()

    async def app(_scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        await receive()
        if where == "body":
            await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            await asyncio.Event().wait()
        finally:
            events.append("cleanup started")
            cleaning.set()
            try:
                await asyncio.sleep(0.15)
            except asyncio.CancelledError:
                events.append("cleanup cancelled again")
                raise
            events.append("cleanup finished")

    transport = HttpxTransport.for_app(app, lifespan="off", timeout=2)
    with pytest.raises(GraphQLTimeoutError):
        client_over(transport, schema=SCHEMA).query("hello", timeout=0.02)
    assert cleaning.wait(1)
    transport.close()
    assert events == ["cleanup started", "cleanup finished"]
    assert_released(loops)


@pytest.mark.parametrize("where", ["head", "body"])
def test_a_timeout_does_not_interrupt_the_cleanup_of_a_self_cancelled_app(
    where: str, loops: list[asyncio.AbstractEventLoop]
) -> None:
    events: list[str] = []
    cleaning = threading.Event()

    async def app(_scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        await receive()
        if where == "body":
            await send({"type": "http.response.start", "status": 200, "headers": []})
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("cleanup started")
            cleaning.set()
            try:
                await asyncio.sleep(0.15)
            except asyncio.CancelledError:
                events.append("cleanup cancelled again")
                raise
            events.append("cleanup finished")

    transport = HttpxTransport.for_app(app, lifespan="off", timeout=2)
    with pytest.raises(GraphQLTimeoutError):
        client_over(transport, schema=SCHEMA).query("hello", timeout=0.02)
    assert cleaning.wait(1)
    transport.close()
    assert events == ["cleanup started", "cleanup finished"]
    assert_released(loops)


def test_a_failure_after_the_bridge_started_closes_it(
    monkeypatch: pytest.MonkeyPatch, loops: list[asyncio.AbstractEventLoop]
) -> None:
    app = AsgiApp()

    def refuse(*_args: object, **_kwargs: object) -> httpx.Client:
        raise RuntimeError("no client")

    monkeypatch.setattr(httpx, "Client", refuse)
    with pytest.raises(RuntimeError, match="no client"):
        HttpxTransport.for_app(app)
    assert app.events == ["lifespan.startup", "lifespan.shutdown"]
    assert_released(loops)


def test_a_thread_that_fails_to_start_closes_its_loop(
    monkeypatch: pytest.MonkeyPatch, loops: list[asyncio.AbstractEventLoop]
) -> None:
    def refuse(_self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(in_process.threading.Thread, "start", refuse)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        HttpxTransport.for_app(AsgiApp())
    assert_released(loops)
