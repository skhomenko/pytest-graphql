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
underlying ``httpx`` exception's own message -- goes through the shared
primitives ``diagnostics.py`` owns: ``sanitize_text`` and ``safe_excerpt``.
They live there rather than here because M5c's recorder dump, log records and
report sections need the same three stages, and a second implementation of
them is how one of those paths ends up missing one. The scrub runs twice
around the escape pass, mirroring ``diagnostics.py``'s own
``_scrub_and_escape``, because escaping can itself synthesize a different
qualifying secret's spelling from a raw control character that was not that
secret before escaping expanded it (module docstring of ``diagnostics.py``,
"A secret's source label").
"""

from __future__ import annotations

import codecs
import dataclasses
import json
import math
import numbers
import random
import reprlib
import ssl
import sys
import time
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, cast

import certifi
import httpx
from httpx._config import create_ssl_context as _httpx_create_ssl_context
from httpx._utils import URLPattern, get_environment_proxies

from pytest_graphql._core.diagnostics import (
    RequestInfo,
    basic_credentials,
    safe_excerpt,
    sanitize_text,
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

#: The largest finite timeout, about 11.5 days. A larger finite value cannot
#: become a socket deadline on every platform, so it is refused instead.
#: CPython on Windows has no ``poll()`` and refuses a socket timeout above
#: ``INT_MAX`` milliseconds (about 2147483.6 seconds) with ``OverflowError``,
#: and 64-bit macOS refuses one near 1e12 seconds. ``math.inf`` is the way
#: to ask for no limit.
MAX_TIMEOUT_SECONDS = 1e6
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024

#: C4/C17. Cookie isolation is a property of this transport, not of the
#: client: an ``httpx.Client`` persists cookies by design, so the scope has
#: to be applied where the jar lives. ``"none"``, the default, clears the
#: jar after every response, so no ``Set-Cookie`` survives a call.
#: ``"client"`` keeps it for that one logical client.
CookieScope = Literal["none", "client"]

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


class _ProxyRouter(httpx.BaseTransport):
    """One pool that sends each request through the proxy its URL selects.

    ``httpx.Client`` resolves ambient proxies only when it builds its own
    transport, and this module always supplies one, because the pool has to
    be shared across derived clients (C19). So ``trust_env=True`` resolves
    the environment here instead, with ``httpx``'s own reader and matching
    rule: the most specific pattern wins, a ``NO_PROXY`` pattern maps to the
    direct pool, and an unmatched URL goes direct. Living inside the shared
    pool, the routing reaches every client derived from it.
    """

    def __init__(
        self,
        direct: httpx.BaseTransport,
        mounts: Mapping[URLPattern, httpx.BaseTransport | None],
    ) -> None:
        self._direct = direct
        self._mounts = dict(sorted(mounts.items()))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        for pattern, pool in self._mounts.items():
            if pattern.matches(request.url):
                return (pool or self._direct).handle_request(request)
        return self._direct.handle_request(request)

    def close(self) -> None:
        self._direct.close()
        for pool in self._mounts.values():
            if pool is not None:
                pool.close()


#: The proxy schemes ``httpx.Proxy`` accepts.
_PROXY_SCHEMES = "http, https, socks5 or socks5h"


def _parse_proxy(value: httpx.Proxy | str, *, source: str) -> httpx.Proxy:
    """``value`` as an ``httpx.Proxy``, refusing it without echoing it.

    ``httpx.Proxy`` puts the whole URL, userinfo included, into the message
    it raises for an unknown scheme. A proxy URL is where a proxy password
    lives, so the refusal here names the problem and never the value.
    """
    if isinstance(value, httpx.Proxy):
        return value
    refusal: str | None = None
    try:
        return httpx.Proxy(value)
    except httpx.InvalidURL:
        refusal = f"the {source} URL is not a valid URL."
    except ValueError:
        refusal = f"the {source} URL must use {_PROXY_SCHEMES}."
    raise ValueError(refusal)


def _proxy_credentials(proxy: httpx.Proxy) -> tuple[tuple[str, str], ...]:
    """Every credential ``proxy`` makes the pool send, for the C16 secret set.

    The userinfo is sent as a ``Proxy-Authorization: Basic`` value that
    ``httpcore`` builds below every redaction boundary, so the value is
    rebuilt here the same way, and the username, the password and the pair
    they encode are added beside it. Any header the proxy was given is sent
    to the proxy alone, so each one counts as a credential as well. A
    repeated header goes on the wire as one line per value, so each value is
    added on its own: ``items()`` would join them into a text no proxy ever
    receives or reflects.
    """
    found: list[tuple[str, str]] = []
    if proxy.auth is not None:
        username, password = proxy.auth
        found += basic_credentials(
            username, password, source="proxy", header="proxy-authorization"
        )
    found += [(name.lower(), value) for name, value in proxy.headers.multi_items()]
    return tuple(found)


def _request_auth(request: httpx.Request) -> httpx.Auth:
    """The authentication step for one send, decided here rather than by httpx.

    C4 places URL userinfo below every header source: it supplies Basic
    authentication only when no layer set ``Authorization``. Left to itself,
    ``httpx.Client.send`` turns userinfo into a Basic header after this
    project's header merge and replaces whatever header the caller chose,
    so every send passes an explicit ``auth`` and httpx's own URL step never
    runs. The base ``httpx.Auth`` sends the request unchanged.
    """
    if "authorization" in request.headers:
        return httpx.Auth()
    username, password = request.url.username, request.url.password
    if username or password:
        return httpx.BasicAuth(username, password)
    return httpx.Auth()


def _build_pool(
    *,
    verify: bool | str | ssl.SSLContext,
    trust_env: bool,
    http2: bool,
    proxy: httpx.Proxy | str | None,
) -> tuple[httpx.BaseTransport, tuple[tuple[str, str], ...]]:
    """The root pool and the proxy credentials it sends (C13, C16).

    An explicit ``proxy`` takes every request, as ``httpx`` gives it
    precedence over the environment. With none, ``trust_env=True`` routes
    by the ambient ``HTTP_PROXY``, ``HTTPS_PROXY``, ``ALL_PROXY`` and
    ``NO_PROXY`` through :class:`_ProxyRouter`, and ``trust_env=False``
    reads none of them. Every proxy URL is parsed before any pool exists,
    so a refused one leaves nothing half built.
    """
    resolved_verify = _resolve_ssl_context(verify, trust_env=trust_env)

    def pool_for(selected: httpx.Proxy | None) -> httpx.HTTPTransport:
        return httpx.HTTPTransport(
            verify=resolved_verify,
            trust_env=trust_env,
            http2=http2,
            proxy=selected,
            retries=0,
        )

    if proxy is not None:
        selected = _parse_proxy(proxy, source="proxy")
        return pool_for(selected), _proxy_credentials(selected)
    if not trust_env:
        return pool_for(None), ()
    ambient = {
        URLPattern(pattern): (
            None if url is None else _parse_proxy(url, source="environment proxy")
        )
        for pattern, url in get_environment_proxies().items()
    }
    if not ambient:
        return pool_for(None), ()
    credentials = tuple(
        credential
        for selected in ambient.values()
        if selected is not None
        for credential in _proxy_credentials(selected)
    )
    mounts = {
        pattern: None if selected is None else pool_for(selected)
        for pattern, selected in ambient.items()
    }
    return _ProxyRouter(pool_for(None), mounts), credentials


def _media_type(content_type: str) -> str:
    """The media type alone, lowercased, stripped of any ``; charset=...`` params."""
    return content_type.split(";", 1)[0].strip().lower()


def _declared_charset(content_type: str) -> str | None:
    for param in content_type.split(";")[1:]:
        name, _, value = param.partition("=")
        if name.strip().lower() == "charset":
            return value.strip().strip('"').lower()
    return None


def _sanitize_json_value(request: RequestInfo, value: Any) -> Any:
    """Recursively scrub and escape every string in a parsed JSON value.

    Used for the structured ``errors`` array ``GraphQLRequestError`` carries:
    a message or an extension value can echo a credential the server was
    given, and this is an exception, so it crosses the rendering boundary
    (DESIGN_DECISIONS.md section 7) like any other.
    """
    if isinstance(value, str):
        return sanitize_text(request, value)
    if isinstance(value, Mapping):
        return {
            (sanitize_text(request, key) if isinstance(key, str) else key): (
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


def checked_seconds(value: object, *, source: str) -> float:
    """``value`` as a timeout in seconds, or a refusal naming ``source``.

    A timeout is a real number greater than zero and at most
    :data:`MAX_TIMEOUT_SECONDS`, or ``math.inf``, which means "no limit".
    Everything else is refused here, before any I/O: NaN reaches the socket
    layer as a raw ``ValueError``, zero makes the socket non-blocking, an
    unchecked negative infinity would read as "no limit" and remove every
    configured bound, and a larger finite value overflows the socket
    deadline. The range is compared on ``value`` itself, before ``float()``,
    because an ``int`` beyond float range overflows that conversion too. It
    is compared again on the converted ``float``, which is the value every
    caller uses: a positive ``Fraction`` below float range rounds to zero.
    ``value`` is converted exactly once, and that stored ``float`` is both
    the one compared and the one returned, so a ``Real`` whose conversion
    changes between calls cannot pass with one result and return another.
    The refusal shows ``value`` through ``reprlib``, so a huge ``int``
    cannot fill the message.
    """
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError(
            f"{source} must be a number of seconds, got {type(value).__name__}."
        )
    # ``numbers.Real`` declares no comparison for the type checker. ``int``,
    # ``float`` and ``Fraction`` all compare with a ``float`` exactly.
    real = cast("float", value)
    seconds = float(real) if _in_timeout_domain(real) else math.nan
    if not _in_timeout_domain(seconds):
        raise ValueError(
            f"{source} must be greater than zero and at most "
            f"{MAX_TIMEOUT_SECONDS:g} seconds, or math.inf for no limit; "
            f"got {reprlib.repr(value)}."
        )
    return seconds


def _in_timeout_domain(real: float) -> bool:
    return real > 0 and (real == math.inf or real <= MAX_TIMEOUT_SECONDS)


def checked_timeout(
    value: float | httpx.Timeout, *, source: str
) -> float | httpx.Timeout:
    """``value`` with every phase checked by :func:`checked_seconds`.

    A phase-specific ``Timeout`` keeps ``None`` as its own spelling of "no
    limit"; every other phase is held to the same domain as a scalar. The
    result is rebuilt from the checked ``float`` phases, because ``httpx``
    stores a phase as given and the socket refuses a ``Fraction``.
    """
    if not isinstance(value, httpx.Timeout):
        return checked_seconds(value, source=source)
    phases = {
        name: None
        if (phase := getattr(value, name)) is None
        else checked_seconds(phase, source=f"{source} {name} phase")
        for name in ("connect", "read", "write", "pool")
    }
    return httpx.Timeout(**phases)


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

    An unbounded result leaves as ``None``, which is how ``httpx`` spells
    "no limit". An infinite ceiling, the client's spelling of the same
    thing, would otherwise reach ``socket.settimeout()``, which rejects it
    with ``OverflowError``. So ``None`` under an infinite ceiling stays
    ``None``, and an infinite configured phase becomes ``None`` too. Both
    inputs were already held to :func:`checked_seconds`' domain, so the only
    infinity that can arrive here is the positive one.
    """

    def _clamped(phase: float | None) -> float | None:
        bound = call_timeout if phase is None else min(phase, call_timeout)
        return None if bound == math.inf else bound

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
        cookie_scope: CookieScope = "none",
        _shared_pool: httpx.BaseTransport | None = None,
        _proxy_credentials: tuple[tuple[str, str], ...] = (),
    ) -> None:
        timeout = checked_timeout(timeout, source="timeout")
        if verify is False:
            warnings.warn(
                "verify=False disables TLS certificate verification; do not "
                "use this outside a trusted test environment.",
                stacklevel=2,
            )
        self._max_attempts = max_attempts
        self._max_response_bytes = max_response_bytes
        self._cookie_scope: CookieScope = cookie_scope
        self._trust_env = trust_env
        self._pool: httpx.BaseTransport
        if _shared_pool is None:
            self._pool, self._proxy_credentials = _build_pool(
                verify=verify, trust_env=trust_env, http2=http2, proxy=proxy
            )
        else:
            self._pool, self._proxy_credentials = _shared_pool, _proxy_credentials
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

        ``request`` is first given the credentials this transport sends, so
        every message and excerpt below scrubs them too (C16). ``timeout`` is
        refused before any I/O when it is outside :func:`checked_seconds`'
        domain.
        """
        timeout = checked_seconds(timeout, source="timeout")
        request = self._with_transport_credentials(request)
        httpx_request: httpx.Request | None = None
        build_error: GraphQLTransportError | None = None
        try:
            httpx_request = self._build_request(request, timeout=timeout)
        except Exception as exc:
            build_error = self._request_construction_error(request, exc)
        if build_error is not None:
            raise build_error
        assert httpx_request is not None

        auth = _request_auth(httpx_request)
        retry_eligible = _should_retry_connect_failure(request)
        attempts_allowed = self._max_attempts if retry_eligible else 1

        response: httpx.Response | None = None
        send_error: GraphQLTransportError | None = None
        for attempt in range(1, attempts_allowed + 1):
            try:
                response = self._client.send(httpx_request, stream=True, auth=auth)
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
        finally:
            # C17. Under the default scope no ``Set-Cookie`` survives a call,
            # so the jar is cleared whatever the response did, including on a
            # classification failure: a response that set a cookie and then
            # failed to parse must not leave that cookie behind either.
            self._clear_cookies_if_scoped()
        raise classify_error

    def _with_transport_credentials(self, request: RequestInfo) -> RequestInfo:
        """``request`` with every credential this pool sends on its behalf.

        Those are the proxy's, which are not in ``request.headers``, so no
        header rule can see them (C16). The Basic value httpx builds from
        the target URL's userinfo needs nothing here: ``RequestInfo``
        derives it from its own URL, so every renderer has it.
        """
        if not self._proxy_credentials:
            return request
        return dataclasses.replace(
            request,
            transport_credentials=(
                *request.transport_credentials,
                *self._proxy_credentials,
            ),
        )

    def _clear_cookies_if_scoped(self) -> None:
        """Drop every cookie this client holds, under ``cookie_scope="none"``."""
        if self._cookie_scope == "none":
            self._client.cookies.clear()

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
            trust_env=self._trust_env,
            cookie_scope=self._cookie_scope,
            _shared_pool=wrapper,
            _proxy_credentials=self._proxy_credentials,
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
            excerpt = safe_excerpt(request, text)
            safe_content_type = sanitize_text(request, content_type)
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
            transport_credentials=self._proxy_credentials,
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
            excerpt = safe_excerpt(request, partial.decode("utf-8", errors="replace"))
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
            safe_charset = sanitize_text(request, charset)
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
        message = sanitize_text(request, f"connection failed: {exc}")
        return GraphQLConnectionError(message, request=request.redacted())

    def _timeout_error(
        self, request: RequestInfo, exc: Exception
    ) -> GraphQLTimeoutError:
        message = sanitize_text(request, f"request timed out: {exc}")
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
        message = sanitize_text(request, f"failed to build the request: {exc}")
        return GraphQLTransportError(message, request=request.redacted())
