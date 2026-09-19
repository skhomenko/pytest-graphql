"""``HttpxTransport``: request encoding, C3 classification, C13 limits, retry.

One ``httpx.Client`` backs the whole session (SPEC 5.6). ``derive()`` (C19,
C23) returns a transport with its own ``httpx.Client`` -- and so its own
cookie jar and header state -- layered over the same underlying connection
pool through ``_NonClosingPoolWrapper``, so a clone's own identity is
isolated while the expensive part, the pool, is shared. The default
``own_pool=False`` leaves that pool for the wrapper's owner to close later;
``own_pool=True`` makes this one derived transport responsible for closing
it, exactly once, when it closes itself.

Every exception this module raises carries ``request.redacted()``, never the
live ``request``, and every piece of response-controlled or exception-derived
text that reaches one -- a body excerpt, a structured GraphQL error, an
underlying ``httpx`` exception's own message -- is scrubbed and escaped with
the two primitives the Diagnostics foundation milestone exposed for exactly
this (C59): ``RequestInfo.scrub`` and ``escape_control_characters``. The
scrub runs twice around the escape pass, mirroring ``diagnostics.py``'s own
``_scrub_and_escape``, because escaping can itself synthesize a different
qualifying secret's spelling from a raw control character that was not that
secret before escaping expanded it (module docstring of ``diagnostics.py``,
"A secret's source label").
"""

from __future__ import annotations

import codecs
import json
import random
import ssl
import sys
import time
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import certifi
import httpx
from httpx._config import create_ssl_context as _httpx_create_ssl_context

from pytest_graphql._core.diagnostics import (
    RequestInfo,
    escape_control_characters,
)
from pytest_graphql._core.errors import (
    GraphQLConnectionError,
    GraphQLHTTPStatusError,
    GraphQLRequestError,
    GraphQLTimeoutError,
    GraphQLTransportError,
)
from pytest_graphql._core.transport.base import DerivableTransportBase, RawResponse

#: SPEC 5.6, C3: the fixed request Accept header, honored unless the caller's
#: own ``RequestInfo.headers`` already names one.
_ACCEPT_HEADER = "application/graphql-response+json, application/json;q=0.9"

#: C3's first classification branch: parsed as a GraphQL response at any
#: status, per the GraphQL-over-HTTP draft.
_GRAPHQL_RESPONSE_MEDIA_TYPE = "application/graphql-response+json"

#: C3's second, legacy classification branch.
_LEGACY_JSON_MEDIA_TYPE = "application/json"

#: C13 defaults.
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024

#: C13: "Backoff is min(0.1 * 2 ** (attempt - 1), 2.0) seconds with full jitter."
_BACKOFF_BASE_SECONDS = 0.1
_BACKOFF_CAP_SECONDS = 2.0


class _NonClosingPoolWrapper(httpx.BaseTransport):
    """Delegates every request to a shared pool; closes it only when told to.

    C19's derivation contract: the default derived transport leaves the root
    pool open for its owner, and only a transport derived with
    ``own_pool=True`` closes it -- exactly once, however many times its own
    ``close()`` runs, because :meth:`close` is itself idempotent.
    """

    def __init__(self, pool: httpx.BaseTransport, *, close_pool: bool) -> None:
        self._pool = pool
        self._close_pool = close_pool
        self._closed = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._pool.handle_request(request)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._close_pool:
            self._pool.close()


def _media_type(content_type: str) -> str:
    """The media type alone, lowercased, stripped of any ``; charset=...`` params."""
    return content_type.split(";", 1)[0].strip().lower()


def _declared_charset(content_type: str) -> str | None:
    for param in content_type.split(";")[1:]:
        name, _, value = param.partition("=")
        if name.strip().lower() == "charset":
            return value.strip().strip('"').lower()
    return None


def _sanitize_text(request: RequestInfo, text: str) -> str:
    """Scrub, escape, scrub again: the stage order C16 documents (module docstring)."""
    once = request.scrub(text)
    escaped = escape_control_characters(once)
    return request.scrub(escaped)


def _truncate_text(text: str, limit: int) -> tuple[str, int]:
    """Truncate ``text`` to at most ``limit`` UTF-8 bytes, on a code-point boundary.

    Returns the truncated text and the number of bytes cut. A binary search
    over code-point counts, the same technique ``diagnostics.py`` uses for a
    JSON string body, adapted for a plain string with no surrounding quotes.
    """
    limit = max(limit, 0)
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, 0
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if len(text[:mid].encode("utf-8")) <= limit:
            low = mid
        else:
            high = mid - 1
    truncated = text[:low]
    return truncated, len(encoded) - len(truncated.encode("utf-8"))


def _safe_excerpt(request: RequestInfo, text: str) -> str:
    """A scrubbed, escaped, length-capped body excerpt for an exception (C3).

    Truncation runs last (C16 "Stage order"), after the scrub and the
    escape, so a cut can never leave part of a secret behind.
    """
    safe = _sanitize_text(request, text)
    truncated, cut = _truncate_text(safe, request.max_diagnostic_bytes)
    if cut:
        return f"{truncated}... (truncated, {cut} byte(s) cut)"
    return truncated


def _sanitize_json_value(request: RequestInfo, value: Any) -> Any:
    """Recursively scrub and escape every string in a parsed JSON value.

    Used for the structured ``errors`` array ``GraphQLRequestError`` carries:
    a message or an extension value can echo a credential the server was
    given, and this is an exception, so it crosses the rendering boundary
    (DESIGN_DECISIONS.md section 7) like any other.
    """
    if isinstance(value, str):
        return _sanitize_text(request, value)
    if isinstance(value, Mapping):
        return {
            (_sanitize_text(request, key) if isinstance(key, str) else key): (
                _sanitize_json_value(request, sub)
            )
            for key, sub in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_json_value(request, item) for item in value]
    return value


def _valid_optional_mapping(parsed: Mapping[str, Any], key: str) -> bool:
    """``key`` is absent, or present and a JSON object (never coerced).

    The same rule ``_is_envelope`` applies to a top-level field applies here
    to a field inside one error object: a value the public contract types as
    ``Mapping[str, Any]`` must actually be a mapping to be accepted, or the
    envelope carrying it is not valid at all -- it is never silently dropped
    to ``None`` or passed through with the wrong shape
    (CR-20260919T001700Z-6ed5cad-3631bad9-F03).
    """
    return key not in parsed or isinstance(parsed[key], Mapping)


def _valid_optional_list(
    item: Mapping[str, Any], key: str, predicate: Callable[[Any], bool]
) -> bool:
    """``key`` is absent, ``null``, or a list whose every element satisfies
    ``predicate``.

    The same "absent, or present and the declared shape" rule
    :func:`_valid_optional_mapping` applies to a mapping-typed field, applied
    to a list-typed one, and extended to accept ``null`` in place of
    omission: SPEC.md types both ``GraphQLErrorInfo.path`` and ``.locations``
    as ``... | None``, so a JSON ``null`` is the documented spelling of
    "not present," not a shape violation.
    """
    if key not in item or item[key] is None:
        return True
    value = item[key]
    return isinstance(value, list) and all(predicate(element) for element in value)


def _valid_int_field(value: Mapping[str, Any], key: str) -> bool:
    """``value[key]`` is an ``int``, excluding ``bool`` (a ``bool`` is an
    ``int`` subclass in Python but never a valid GraphQL ``Int``)."""
    field = value.get(key)
    return isinstance(field, int) and not isinstance(field, bool)


def _valid_path_segment(value: Any) -> bool:
    """One ``GraphQLErrorInfo.path`` segment: a ``str`` or non-boolean ``int``
    (SPEC.md types the field ``tuple[str | int, ...] | None``)."""
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _valid_location(value: Any) -> bool:
    """One ``GraphQLErrorInfo.locations`` entry: a mapping with non-boolean
    integer ``line`` and ``column`` (SPEC.md types the field
    ``tuple[tuple[int, int], ...] | None``)."""
    return (
        isinstance(value, Mapping)
        and _valid_int_field(value, "line")
        and _valid_int_field(value, "column")
    )


def _valid_error_object(item: Any) -> bool:
    """One GraphQL error object: a mapping with a required string ``message``
    and, when present, every other field SPEC.md's ``GraphQLErrorInfo``
    types downstream -- a mapping ``extensions``, a ``path`` of ``str``/``int``
    segments, and ``locations`` entries with integer ``line``/``column`` --
    in its declared shape (the GraphQL-over-HTTP error shape). A mapping
    missing ``message``, carrying a non-string one, or carrying any of these
    sibling fields in the wrong shape, is not a GraphQL error at all, just
    JSON that happens to be an object.
    """
    return (
        isinstance(item, Mapping)
        and isinstance(item.get("message"), str)
        and _valid_optional_mapping(item, "extensions")
        and _valid_optional_list(item, "path", _valid_path_segment)
        and _valid_optional_list(item, "locations", _valid_location)
    )


def _valid_errors(value: Any) -> bool:
    """The public ``errors`` contract (``RawResponse``/``GraphQLRequestError``):
    a non-empty JSON array of GraphQL error objects. Anything else -- a
    string, a mapping, an empty list, an object missing ``message`` or with a
    non-string ``message`` -- is not a structured GraphQL error list, and a
    body carrying it is not a valid envelope at all (C3), never a coercion of
    whatever JSON happened to sit at that key.
    """
    return (
        isinstance(value, list)
        and len(value) > 0
        and all(_valid_error_object(item) for item in value)
    )


def _is_envelope(parsed: Any) -> bool:
    """A GraphQL-over-HTTP envelope: a JSON object carrying ``data`` or ``errors``.

    When ``errors`` is present, it must already be the documented shape;
    otherwise the whole body is not a valid envelope, so a malformed
    ``errors`` value can never be reinterpreted as valid ``data``-only
    success or silently coerced into typed error objects. The same holds for
    a top-level ``extensions``: present but not a mapping makes the whole
    envelope invalid, rather than being silently dropped to ``None``.
    """
    if not isinstance(parsed, dict):
        return False
    if "errors" in parsed and not _valid_errors(parsed["errors"]):
        return False
    if not _valid_optional_mapping(parsed, "extensions"):
        return False
    return "data" in parsed or "errors" in parsed


def _local_default_ssl_context(
    *, cafile: str | None = None, capath: str | None = None
) -> ssl.SSLContext:
    """The verified-client half of ``ssl.create_default_context()``, without
    its unconditional, unsuppressible ``SSLKEYLOGFILE`` read.

    Python's ``ssl.create_default_context()`` (``Lib/ssl.py``) builds a
    ``PROTOCOL_TLS_CLIENT`` context, hardens it, loads the given certificate
    source, and only then -- as its last, separate step, with no parameter to
    opt out of it -- reads ``os.environ.get('SSLKEYLOGFILE')`` and eagerly
    opens that path if it is set. Every value this module could pass to that
    function still reaches the same unconditional read, so honoring
    "``SSLKEYLOGFILE`` ... ignored" (DESIGN_DECISIONS.md, "Operational
    limits") requires not calling it at all for ``trust_env=False``, rather
    than calling it and then reacting to what it already did (the two prior
    fixes' shared flaw, and a third that only narrowed the exposure window:
    see :func:`_resolve_ssl_context`).

    This reproduces just the certificate-loading and hardening steps. Which
    steps those are is version-dependent, not merely which flag constants
    happen to exist: ``VERIFY_X509_PARTIAL_CHAIN`` and ``VERIFY_X509_STRICT``
    have both existed on every interpreter in the project's declared range
    (``>=3.10,<3.15``) since 3.10 and 3.4 respectively, but CPython's own
    ``create_default_context`` only started ORing them in as of Python 3.13
    (see its "What's New" entry for that release). Gating on
    ``getattr(ssl, name, 0)`` would therefore silently turn on stricter
    chain validation on 3.10 through 3.12 that neither ``httpx`` nor the
    running interpreter's own default policy applies there
    (CR-20260919T023658Z-6ed5cad-93c4fe37-F01). Gating on
    ``sys.version_info`` instead mirrors the exact interpreter-version
    boundary where CPython's own behavior changed.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    if sys.version_info >= (3, 13):
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        context.verify_flags |= ssl.VERIFY_X509_STRICT
    if cafile or capath:
        context.load_verify_locations(cafile=cafile, capath=capath)
    else:
        context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    return context


def _resolve_ssl_context(
    verify: bool | str | ssl.SSLContext, *, trust_env: bool
) -> bool | str | ssl.SSLContext:
    """Build the pool's SSL context ourselves when ``trust_env=False`` (C13).

    ``httpx``'s own ``trust_env`` only gates its CA-bundle environment lookups
    (``SSL_CERT_FILE``, ``SSL_CERT_DIR``); Python's ``ssl.create_default_context``
    independently honors ``SSLKEYLOGFILE`` from the process environment no
    matter what ``trust_env`` says, which would otherwise let an ambient
    variable make the pool log TLS session secrets despite the documented
    "ambient ... SSLKEYLOGFILE ... are ignored" promise (DESIGN_DECISIONS.md,
    "Operational limits"). An ``ssl.SSLContext`` the caller already built and
    supplied explicitly is left untouched: that is an explicit override, not
    ambient configuration.

    Three prior fixes tried to keep ``ssl.create_default_context`` from
    seeing ``SSLKEYLOGFILE``, or from acting on it, by touching state some
    other part of the process shares: popping the variable from
    ``os.environ`` around the call (lost a concurrent writer's update on
    restore), replacing ``os.environ.get`` with a context-local wrapper
    (stayed installed for the process's life, hid the variable from unrelated
    same-context reads, and became self-recursive across a module reload --
    CR-20260919T014345Z-6ed5cad-f040aef1-F01), and calling the function
    unmodified then clearing ``keylog_filename`` on the result (the eager
    open the function performs while reading the ambient variable already
    ran by then, so a bad ambient path still raised ``FileNotFoundError``
    before construction could finish, and a usable one still created a file
    on disk -- CR-20260919T021158Z-6ed5cad-605986bf-F01). Every one of those
    approaches still let ``ssl.create_default_context`` run with
    ``SSLKEYLOGFILE`` visible; the difference between them was only how much
    of its aftermath got cleaned up. Calling
    :func:`_local_default_ssl_context` instead means that function's own
    unconditional environment read is simply never reached: nothing is
    mutated, nothing is popped and restored, and nothing about the ambient
    variable's value or presence can change what this returns.

    The ``verify=False`` branch (``ssl.SSLContext(...)`` with
    ``check_hostname=False`` and ``verify_mode=CERT_NONE``) never calls
    ``ssl.create_default_context`` in ``httpx`` either, so it carries no
    ``SSLKEYLOGFILE`` exposure and is left to ``httpx``'s own construction
    unmodified.
    """
    if trust_env or isinstance(verify, ssl.SSLContext):
        return verify
    if verify is False:
        return _httpx_create_ssl_context(verify=verify, trust_env=trust_env)
    if verify is True:
        return _local_default_ssl_context(cafile=certifi.where())
    if Path(verify).is_dir():
        return _local_default_ssl_context(capath=verify)
    return _local_default_ssl_context(cafile=verify)


def _ceiling_timeout(configured: httpx.Timeout, call_timeout: float) -> httpx.Timeout:
    """Clamp each of ``configured``'s four phases to at most ``call_timeout``
    (DESIGN_DECISIONS.md, "Operational limits").

    ``Transport.send()``'s per-call ``timeout`` is mandatory and scalar
    (SPEC.md 5.6), so every call must supply one regardless of whether the
    transport already has a phase-specific budget from its own constructor.
    Treating the per-call value as a ceiling -- never a floor -- is the one
    combination that needs nothing beyond what this method already has: a
    phase already at or under the ceiling is untouched, so a documented
    ``Timeout(connect, read, write, pool)`` genuinely governs a request
    whenever it is not looser than the caller's budget, and a phase with no
    configured limit (``None``) is bounded by the ceiling instead of staying
    unbounded.
    """

    def _clamped(phase: float | None) -> float:
        return call_timeout if phase is None else min(phase, call_timeout)

    return httpx.Timeout(
        connect=_clamped(configured.connect),
        read=_clamped(configured.read),
        write=_clamped(configured.write),
        pool=_clamped(configured.pool),
    )


def _should_retry_connect_failure(request: RequestInfo) -> bool:
    """SPEC 5.6: a query always retries on connect failure; a mutation only
    when ``idempotent=True``."""
    return request.kind == "query" or request.idempotent


def _backoff_seconds(attempt: int) -> float:
    """C13's formula, with full jitter: a uniform draw between 0 and the cap."""
    cap = min(_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), _BACKOFF_CAP_SECONDS)
    return random.uniform(0, cap)


#: A private name for the retry backoff's own sleep, so a test can replace
#: this module's use of it without patching the shared ``time`` module object
#: -- which would also silence a ``delay`` a test's own local HTTP server
#: relies on, since both modules import the very same ``time`` module.
_sleep = time.sleep


class HttpxTransport(DerivableTransportBase):
    """The ``httpx``-backed ``Transport`` (SPEC 5.6), one client per session."""

    def __init__(
        self,
        *,
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        trust_env: bool = False,
        proxy: httpx.Proxy | str | None = None,
        verify: bool | str | ssl.SSLContext = True,
        http2: bool = False,
        _shared_pool: httpx.BaseTransport | None = None,
    ) -> None:
        if verify is False:
            warnings.warn(
                "verify=False disables TLS certificate verification; do not "
                "use this outside a trusted test environment.",
                stacklevel=2,
            )
        self._max_attempts = max_attempts
        self._max_response_bytes = max_response_bytes
        self._pool: httpx.BaseTransport = _shared_pool or httpx.HTTPTransport(
            verify=_resolve_ssl_context(verify, trust_env=trust_env),
            trust_env=trust_env,
            http2=http2,
            proxy=proxy,
            retries=0,
        )
        self._client = httpx.Client(
            transport=self._pool,
            timeout=timeout,
            trust_env=trust_env,
            follow_redirects=False,
        )
        self._closed = False

    # -- Transport protocol ---------------------------------------------------

    def send(self, request: RequestInfo, *, timeout: float) -> RawResponse:
        """Send one request, retry-gated on connect failure, then classify (C3).

        Every phase -- request construction, connect, header receipt and
        streamed body reads -- is covered by the same redaction boundary. A
        lower-level exception is always fully constructed *inside* the
        ``except`` clause that catches it, then raised only after that clause
        has exited: raising from inside the clause would leave the raw
        exception reachable through the new exception's ``__context__`` even
        with ``from None``, since a bare ``raise`` re-attaches whatever
        exception is currently being handled regardless of an explicit
        ``from`` clause.
        """
        httpx_request: httpx.Request | None = None
        build_error: GraphQLTransportError | None = None
        try:
            httpx_request = self._build_request(request, timeout=timeout)
        except Exception as exc:
            build_error = self._request_construction_error(request, exc)
        if build_error is not None:
            raise build_error
        assert httpx_request is not None

        retry_eligible = _should_retry_connect_failure(request)
        attempts_allowed = self._max_attempts if retry_eligible else 1

        response: httpx.Response | None = None
        send_error: GraphQLTransportError | None = None
        for attempt in range(1, attempts_allowed + 1):
            try:
                response = self._client.send(httpx_request, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt < attempts_allowed:
                    _sleep(_backoff_seconds(attempt))
                    continue
                send_error = self._connection_error(request, exc)
                break
            except httpx.TimeoutException as exc:
                send_error = self._timeout_error(request, exc)
                break
            except httpx.HTTPError as exc:
                send_error = self._connection_error(request, exc)
                break
            else:
                break
        if send_error is not None:
            raise send_error

        assert response is not None
        classify_error: GraphQLTransportError
        try:
            return self._classify(request, response)
        except httpx.TimeoutException as exc:
            classify_error = self._timeout_error(request, exc)
        except httpx.HTTPError as exc:
            classify_error = self._connection_error(request, exc)
        raise classify_error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._client.close()

    def derive(self, *, own_pool: bool = False) -> HttpxTransport:
        """Return a transport with its own client over the shared pool (C19).

        The default leaves the pool for its owner to close later. With
        ``own_pool=True`` this derived transport closes the pool itself,
        exactly once, when it closes -- the caller's responsibility is to
        grant that to at most one client per root pool.
        """
        wrapper = _NonClosingPoolWrapper(self._pool, close_pool=own_pool)
        return HttpxTransport(
            timeout=self._client.timeout,
            max_attempts=self._max_attempts,
            max_response_bytes=self._max_response_bytes,
            _shared_pool=wrapper,
        )

    # -- request encoding -------------------------------------------------

    def _build_request(self, request: RequestInfo, *, timeout: float) -> httpx.Request:
        """Encode the request per the fixed wire contract (DESIGN_DECISIONS.md
        "Transport and protocol"): always a POST, always
        ``Content-Type: application/json``, always the documented ``Accept``.
        These three are protocol invariants, not caller-overridable defaults --
        a caller's own ``Content-Type``/``Accept`` header is dropped, and
        ``request.method`` plays no part in what goes on the wire. Every other
        caller-supplied header (auth, tracing, ...) still passes through.

        ``timeout`` is applied as a ceiling on the client's own configured
        phase-specific budget (DESIGN_DECISIONS.md, "Operational limits"),
        not as a blanket replacement of it -- see :func:`_ceiling_timeout`.
        """
        body: dict[str, Any] = {"query": request.document}
        if request.operation is not None:
            body["operationName"] = request.operation
        body["variables"] = dict(request.variables)
        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in ("content-type", "accept")
        }
        headers["Accept"] = _ACCEPT_HEADER
        effective_timeout = _ceiling_timeout(self._client.timeout, timeout)
        return self._client.build_request(
            "POST", request.url, json=body, headers=headers, timeout=effective_timeout
        )

    # -- response classification (C3) --------------------------------------

    def _classify(self, request: RequestInfo, response: httpx.Response) -> RawResponse:
        content_type = response.headers.get("content-type", "")
        media_type = _media_type(content_type)
        status = response.status_code

        body_bytes = self._read_capped_body(request, response)
        text = self._decode_body(request, body_bytes, content_type)
        parsed = self._parse_json(text)
        is_graphql_media_type = media_type in (
            _GRAPHQL_RESPONSE_MEDIA_TYPE,
            _LEGACY_JSON_MEDIA_TYPE,
        )
        if not is_graphql_media_type or not _is_envelope(parsed):
            excerpt = _safe_excerpt(request, text)
            safe_content_type = _sanitize_text(request, content_type)
            if 200 <= status < 300:
                raise GraphQLTransportError(
                    f"response was not a valid GraphQL envelope "
                    f"(content-type {safe_content_type!r}, status {status}): "
                    f"{excerpt}",
                    request=request.redacted(),
                    body_excerpt=excerpt,
                )
            raise GraphQLHTTPStatusError(
                f"request failed with status {status} "
                f"(content-type {safe_content_type!r}): {excerpt}",
                request=request.redacted(),
                status_code=status,
                body_excerpt=excerpt,
            )

        envelope: dict[str, Any] = parsed
        if "data" not in envelope:
            errors = tuple(
                _sanitize_json_value(request, error)
                for error in envelope.get("errors") or ()
            )
            raise GraphQLRequestError(
                f"the server rejected the request before execution (status {status}).",
                request=request.redacted(),
                status_code=status,
                media_type=media_type,
                errors=errors,
            )

        extensions = envelope.get("extensions")
        return RawResponse(
            status_code=status,
            media_type=media_type,
            data=envelope.get("data"),
            errors=tuple(envelope.get("errors") or ()),
            extensions=extensions if isinstance(extensions, Mapping) else None,
            headers=dict(response.headers),
        )

    def _read_capped_body(
        self, request: RequestInfo, response: httpx.Response
    ) -> bytes:
        """Read up to ``max_response_bytes``, never buffering past it (C13).

        The chunk that would cross the cap is itself sliced to what still
        fits: the retained, excerptable body never exceeds
        ``max_response_bytes``, regardless of how large a single decoded
        chunk the server sent. On overflow, this raises
        ``GraphQLTransportError`` directly, after the read loop has already
        finished (normal control flow, not exception handling), so no raw
        ``httpx`` iteration state is ever attached to it.
        """
        chunks: list[bytes] = []
        total = 0
        overflowed = False
        try:
            for chunk in response.iter_bytes():
                remaining = self._max_response_bytes - total
                if len(chunk) > remaining:
                    if remaining > 0:
                        chunks.append(chunk[:remaining])
                    overflowed = True
                    break
                chunks.append(chunk)
                total += len(chunk)
        finally:
            response.close()

        partial = b"".join(chunks)
        if overflowed:
            excerpt = _safe_excerpt(request, partial.decode("utf-8", errors="replace"))
            raise GraphQLTransportError(
                f"response body exceeded max_response_bytes="
                f"{self._max_response_bytes:,}: {excerpt}",
                request=request.redacted(),
                body_excerpt=excerpt,
            )
        return partial

    def _decode_body(
        self,
        request: RequestInfo,
        body_bytes: bytes,
        content_type: str,
    ) -> str:
        charset = _declared_charset(content_type) or "utf-8"
        unknown_charset = False
        try:
            codecs.lookup(charset)
        except LookupError:
            unknown_charset = True
        if unknown_charset:
            safe_charset = _sanitize_text(request, charset)
            raise GraphQLTransportError(
                f"response declared an unknown charset {safe_charset!r}.",
                request=request.redacted(),
            )
        try:
            return body_bytes.decode(charset)
        except UnicodeDecodeError:
            # An undecodable body is simply an unparsable one; the ordinary
            # C3 classification path (JSON parse failure) reports it, with
            # a best-effort text form for the excerpt.
            return body_bytes.decode(charset, errors="replace")

    @staticmethod
    def _parse_json(text: str) -> Any:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    # -- exception construction ---------------------------------------------

    def _connection_error(
        self, request: RequestInfo, exc: Exception
    ) -> GraphQLConnectionError:
        message = _sanitize_text(request, f"connection failed: {exc}")
        return GraphQLConnectionError(message, request=request.redacted())

    def _timeout_error(
        self, request: RequestInfo, exc: Exception
    ) -> GraphQLTimeoutError:
        message = _sanitize_text(request, f"request timed out: {exc}")
        return GraphQLTimeoutError(message, request=request.redacted())

    def _request_construction_error(
        self, request: RequestInfo, exc: Exception
    ) -> GraphQLTransportError:
        """Any failure while encoding the URL, headers or JSON body (C2, C16).

        This runs before any network I/O, on caller-controlled data alone, so
        a broad ``except Exception`` in :meth:`send` is deliberate: whatever
        ``httpx.Client.build_request`` raises -- ``httpx.InvalidURL`` for a
        malformed URL, a header-encoding error, or anything else -- can carry
        the same caller-supplied text a live request would, and none of those
        exception types are guaranteed to be an ``httpx.HTTPError`` subclass
        the other handlers already catch.
        """
        message = _sanitize_text(request, f"failed to build the request: {exc}")
        return GraphQLTransportError(message, request=request.redacted())
