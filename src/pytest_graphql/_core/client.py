"""The GraphQL client and its factory (M5c).

Ownership follows ``docs/reference/DESIGN_DECISIONS.md`` section 9. The
cleanup sweep and the failure report live in
:mod:`pytest_graphql._core.lifecycle`, which this module and the transport
share, so a correction to either reaches every releasing call site.
"""

from __future__ import annotations

import contextlib
import dataclasses
import math
import re
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import (
    Any,
    cast,
)

import httpx
from graphql import (
    DocumentNode,
    GraphQLInputType,
    GraphQLSchema,
    OperationDefinitionNode,
    parse,
    print_ast,
    type_from_ast,
)

from pytest_graphql._core.auth import Auth
from pytest_graphql._core.auth import as_ as resolve_auth
from pytest_graphql._core.diagnostics import (
    DEFAULT_MAX_DIAGNOSTIC_BYTES,
    DEFAULT_MAX_RECORDED_CALLS,
    DEFAULT_MAX_RECORDED_ERRORS,
    DEFAULT_MIN_REDACTED_VALUE_LENGTH,
    DEFAULT_REDACT_HEADERS,
    DEFAULT_REDACT_VARIABLES,
    WITHHELD_TEXT,
    DiagnosticsRecorder,
    OmissionRecord,
    RecordedCall,
    RequestInfo,
    header_credentials,
    request_credentials,
    require_safe_rendering,
    safe_excerpt,
)
from pytest_graphql._core.errors import (
    ArgumentError,
    GraphQLExecutionError,
    GraphQLPartialDataError,
    SelectionError,
)
from pytest_graphql._core.excerpt import render_data_excerpt
from pytest_graphql._core.expect_error import (
    CapturedErrors,
    ExpectedError,
    observe_response,
)
from pytest_graphql._core.factory import FakeContext, FakeNamespace, ScalarRegistry
from pytest_graphql._core.graphql_compat import ast_tuple
from pytest_graphql._core.headers import merge_headers
from pytest_graphql._core.lifecycle import Closable, _close_all, _Owned, _report
from pytest_graphql._core.matching.expect import ExpectNamespace
from pytest_graphql._core.middleware import (
    Middleware,
    apply_after_response,
    apply_before_request,
)
from pytest_graphql._core.operation import assemble_operation
from pytest_graphql._core.polling import RequestTrace
from pytest_graphql._core.polling import wait_until as _wait_until
from pytest_graphql._core.response import GraphQLResponse, build_response
from pytest_graphql._core.schema.info import OperationKind
from pytest_graphql._core.schema.source import IntrospectionSource, SchemaSource
from pytest_graphql._core.selection.builder import SelectionBuilder
from pytest_graphql._core.selection.model import AUTO, SelectionInput
from pytest_graphql._core.selection.policy import CyclePolicy, SelectionPolicy
from pytest_graphql._core.transport.base import DerivableTransportBase, Transport
from pytest_graphql._core.transport.httpx_transport import (
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    CookieScope,
    HttpxTransport,
    _parse_proxy,
    _proxy_credentials,
    checked_seconds,
    checked_timeout,
)
from pytest_graphql._core.validation import (
    RESERVED_OPTIONS,
    coerce_variables,
    validate_document,
)

# -- derivation (C19, C23) ----------------------------------------------------


def _derivation_of(transport: Transport) -> Callable[[], Transport] | None:
    """The transport's own ``derive``, or None when it does not support one.

    Opting in is inheritance and nothing else (C23). ``isinstance`` against a
    real class is a type guard, so the bound method comes back fully typed and
    a subclass with the wrong signature is an error where it is written. A
    transport that does not inherit the base is shared whatever methods it
    has, so an unrelated member named ``derive`` is never looked up.
    """
    if isinstance(transport, DerivableTransportBase):
        return transport.derive
    return None


def _client_over(
    transport: Transport, build: Callable[[Transport, bool], GraphQLClient]
) -> GraphQLClient:
    """A client over ``transport``, or over a transport derived from it.

    ``build`` receives the transport the client holds and whether the client
    owns it. Over a derivable transport that is a fresh derived transport the
    client owns; over any other it is ``transport`` itself, owned by nobody
    here. A derived transport is adopted by a cleanup list before it is
    derived (9.2), and the list is swept through the one sweep and report
    (9.3) if anything raises before the client exists, so a failing
    ``build`` cannot strand it. Once the client exists it is the transport's
    only owner, and the list is dropped unswept.
    """
    derive = _derivation_of(transport)
    if derive is None:
        return build(transport, False)
    cleanup: list[Closable] = []
    try:
        derived = _Owned(cleanup, derive)
        return build(derived.value, True)
    except BaseException as failure:
        errors = _close_all(cleanup)
        if errors:
            _report(errors, failure)
        raise


# -- configuration (SPEC 3.10) ------------------------------------------------


@dataclass
class ClientConfig:
    """The settings of a client: everything that is data and not an object.

    A `ClientConfig` holds the endpoint, headers, timeouts, selection limits,
    raising rules, seed and redaction settings. Objects that a client uses, such
    as the transport, the schema, the scalar registry and the middleware, are
    arguments of `GraphQLClient` and `build_client()`. The seed lives here and
    not on the client.

    It is a plain dataclass. Copy it with `dataclasses.replace()` to change a few
    fields. `build_client()` also accepts any field as a keyword, so
    `build_client(url=..., max_depth=2)` needs no `ClientConfig` of its own.

    The fields that can hold a credential (`headers`, `schema_headers`,
    `cookies` and `proxy`) are left out of `repr()`, so printing a config does not
    show them.

    Many of these settings can also be set for one call, with the keyword of the
    same name on `query()` or `mutation()`. See `GraphQLClient.query()`.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig, build_client

        config = ClientConfig(timeout=5.0, max_depth=2, headers={"X-Env": "test"})
        client = build_client(
            url="http://localhost:8000/graphql",
            config=config,
            transport=gql.transport,
            schema=gql.schema,
        )
        assert client.config.max_depth == 2
        assert client.config.url == "http://localhost:8000/graphql"
        ```
    """

    url: str | None = None
    """The URL of the GraphQL endpoint. `build_client(url=...)` sets it.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        config = ClientConfig(url="http://localhost:8000/graphql")
        assert config.url == "http://localhost:8000/graphql"
        assert ClientConfig().url is None
        ```
    """
    #: The fields that carry a credential (``headers``, ``schema_headers``,
    #: ``cookies`` and ``proxy``) stay out of ``repr()``. A configuration is
    #: plain data a traceback or an assertion report prints whole, and no
    #: redaction stage runs over it (C2, C16).
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    """Headers sent with every request.

    This includes the request that loads the schema, unless `schema_headers` is
    set. Names compare without regard to case.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        config = ClientConfig(headers={"X-Env": "test"})
        assert config.headers == {"X-Env": "test"}
        assert "X-Env" not in repr(config)
        ```
    """
    #: C4: schema loading uses this alone, so a function-scoped auth fixture
    #: cannot change the session-scoped schema. ``None`` means "use
    #: ``headers``", which is the documented default.
    schema_headers: Mapping[str, str] | None = field(default=None, repr=False)
    """Headers for the request that loads the schema, and for nothing else.

    Auth objects and per-call headers never reach it. `None`, the default, means
    use `headers`.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().schema_headers is None
        config = ClientConfig(schema_headers={"X-Schema": "1"})
        assert config.schema_headers == {"X-Schema": "1"}
        ```
    """
    cookies: Mapping[str, str] = field(default_factory=dict, repr=False)
    """Cookie values to treat as secrets.

    In this version the client does not send these cookies. The values are only
    added to the set of secrets that are removed from error messages and reports.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        config = ClientConfig(cookies={"session": "abc123"})
        assert config.cookies == {"session": "abc123"}
        assert "abc123" not in repr(config)
        ```
    """
    cookie_scope: CookieScope = "none"
    """Whether a client keeps the cookies that a server sets.

    With `"none"`, the default, every cookie is dropped after each response, so
    no state carries over from one call to the next. With `"client"`, a client
    keeps its cookies and sends them back. The cookies belong to that client
    only. A clone made with `with_headers()` or `as_()` starts with none.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().cookie_scope == "none"
        assert ClientConfig(cookie_scope="client").cookie_scope == "client"
        ```
    """
    #: "Operational limits": a float applied to all four phases, or a
    #: ``Timeout(connect, read, write, pool)``. :meth:`call_timeout` turns
    #: either form into the scalar ceiling ``Transport.send()`` requires.
    timeout: float | httpx.Timeout = DEFAULT_TIMEOUT_SECONDS
    """The time limit of one call, in seconds. The default is 30.

    A number applies to each of the four phases: connect, read, write and wait
    for a connection from the pool. To set the phases apart, give an
    `httpx.Timeout`. A phase set to `None` has no limit. A number must be more
    than zero and at most 1,000,000, or `math.inf` for no limit. Anything else
    raises an error before any request is sent.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        import httpx

        assert ClientConfig().timeout == 30.0
        assert ClientConfig(timeout=5).timeout == 5
        phases = httpx.Timeout(connect=2, read=20, write=5, pool=1)
        assert ClientConfig(timeout=phases).call_timeout() == 20
        ```
    """
    retries: int = 2
    """How many times a call is tried again after it fails to connect.

    The default is 2, so a call is tried up to three times. Only a failure to
    connect is retried, never a request that reached the server, and a mutation
    is not retried unless the call has `idempotent=True`. The wait between tries
    grows by steps and has a random part.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().retries == 2
        assert ClientConfig(retries=0).retries == 0
        ```
    """
    #: ``httpx`` semantics: ``True``, a CA bundle path, or an ``SSLContext``.
    verify: bool | str | ssl.SSLContext = True
    """How to check the server's TLS certificate.

    `True` uses the standard certificate authorities. A string is the path of a
    CA bundle file. An `ssl.SSLContext` is used as it is. `False` turns the check
    off and issues a warning.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().verify is True
        assert ClientConfig(verify="/etc/ssl/ca.pem").verify == "/etc/ssl/ca.pem"
        ```
    """
    #: Ambient proxy, netrc and ``SSLKEYLOGFILE`` handling stays off unless a
    #: project opts in, so a run cannot silently route through an ambient
    #: proxy. An explicit :attr:`proxy` is unaffected by this flag.
    trust_env: bool = False
    """Whether to read proxy settings from the environment.

    Off by default, so a test run does not quietly go through a proxy that the
    machine happens to have. When it is off, the variables `HTTP_PROXY`,
    `HTTPS_PROXY` and `NO_PROXY`, the netrc file and `SSLKEYLOGFILE` are ignored.
    A `proxy` that you set is used either way.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().trust_env is False
        assert ClientConfig(trust_env=True).trust_env is True
        ```
    """
    proxy: httpx.Proxy | str | None = field(default=None, repr=False)
    """A proxy for every request: a URL, an `httpx.Proxy`, or `None`.

    An explicit proxy takes every request and outranks the environment. A URL
    that the transport cannot use raises an error that does not quote the URL,
    because a proxy password can be in it.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        config = ClientConfig(proxy="http://proxy.local:3128")
        assert config.proxy == "http://proxy.local:3128"
        assert "proxy.local" not in repr(config)
        ```
    """
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    """The largest response body that the client reads, in bytes. The default is 32 MiB.

    The count is of bytes after decompression. A larger response raises an
    error, and the client does not read the rest.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().max_response_bytes == 32 * 1024 * 1024
        config = ClientConfig(max_response_bytes=1_000_000)
        assert config.max_response_bytes == 1_000_000
        ```
    """
    follow_redirects: bool = False
    """Not used in this version. The client never follows a redirect.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().follow_redirects is False
        ```
    """
    http2: bool = False
    """Whether to use HTTP/2. It needs the `h2` package that `httpx[http2]` installs.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().http2 is False
        assert ClientConfig(http2=True).http2 is True
        ```
    """

    max_depth: int = 3
    """How many levels of objects auto-selection selects. See `SelectionPolicy`.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().max_depth == 3
        assert ClientConfig(max_depth=1).selection_policy().max_depth == 1
        ```
    """
    cycle_policy: CyclePolicy = "shallow"
    """What auto-selection does when a type repeats on its path. See `CyclePolicy`.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().cycle_policy == "shallow"
        assert ClientConfig(cycle_policy="stop").cycle_policy == "stop"
        ```
    """
    per_type_depth_cap: Mapping[str, int] = field(default_factory=dict)
    """Stricter depth limits for named types. See `SelectionPolicy`.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        config = ClientConfig(per_type_depth_cap={"User": 1})
        assert config.per_type_depth_cap == {"User": 1}
        assert config.selection_policy().depth_cap_for("User") == 1
        ```
    """
    include_deprecated: bool = False
    """Whether auto-selection selects deprecated fields. Off by default.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().include_deprecated is False
        assert ClientConfig(include_deprecated=True).include_deprecated is True
        ```
    """
    max_fields: int = 2000
    """The most fields that one generated query may select. See `SelectionPolicy`.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().max_fields == 2000
        assert ClientConfig(max_fields=500).max_fields == 500
        ```
    """
    exclude: Sequence[str] = ()
    """Patterns for fields that auto-selection never selects.

    Each is `"Type.field"`, `"*.field"` or `"Type.*"`. Use it to leave out fields that
    your test user may not read.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        config = ClientConfig(exclude=["User.balance", "*.preferences"])
        assert config.exclude == ["User.balance", "*.preferences"]
        policy = config.selection_policy()
        assert not policy.should_include("User", "balance", ("balance",), 0)
        ```
    """
    relay_aware: bool = True
    """Whether auto-selection recognizes Relay connections. See `SelectionPolicy`.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().relay_aware is True
        assert ClientConfig(relay_aware=False).relay_aware is False
        ```
    """

    validate: bool = True
    """Whether to check a query against the schema before it is sent.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().validate is True
        assert ClientConfig(validate=False).validate is False
        ```
    """
    raise_on_error: bool = True
    """Whether a response with errors raises `GraphQLExecutionError`.

    With `False`, no error is raised, and that includes a partial-data error.
    Read `response.errors` yourself, usually with `raw=True`.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig, build_client

        client = build_client(
            url="http://localhost:8000/graphql",
            transport=gql.transport,
            schema=gql.schema,
            config=ClientConfig(raise_on_error=False),
        )
        response = client.mutation("updateUser", id="missing", fields=["id"], raw=True)
        assert response.errors[0].path == ("updateUser",)
        ```
    """
    raise_on_partial: bool = True
    """Whether a response with both data and errors raises `GraphQLPartialDataError`.

    It applies only when `raise_on_error` is `True`.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().raise_on_partial is True
        assert ClientConfig(raise_on_partial=False).raise_on_partial is False
        ```
    """

    seed: int = 0
    """The seed for `gql.fake`. The same seed gives the same data on every run.

    Examples:
        ```python {.exec}
        from pytest_graphql import build_client

        def client_with(seed):
            return build_client(
                url="http://localhost:8000/graphql",
                transport=gql.transport,
                schema=gql.schema,
                seed=seed,
            )


        one, two = client_with(1), client_with(2)
        assert one.fake.CreatePostInput() == one.fake.CreatePostInput()
        assert one.fake.CreatePostInput() != two.fake.CreatePostInput()
        ```
    """
    schema_cache_dir: str | None = None
    """Not used in this version. The client does not cache the schema on disk.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().schema_cache_dir is None
        ```
    """
    schema_cache_ttl: int = 0
    """Not used in this version. The client does not cache the schema on disk.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().schema_cache_ttl == 0
        ```
    """

    redact_headers: Sequence[str] = tuple(sorted(DEFAULT_REDACT_HEADERS))
    """The names of headers whose values are hidden in reports and error messages.

    Names compare without regard to case. The default is `authorization`,
    `cookie`, `proxy-authorization` and `x-api-key`. A list that you give here
    replaces the default, so include these names when you add one.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert "authorization" in ClientConfig().redact_headers
        config = ClientConfig(redact_headers=("authorization", "x-session"))
        assert "x-session" in config.redact_headers
        ```
    """
    redact_variables: Sequence[str] = DEFAULT_REDACT_VARIABLES
    """Patterns for variable names whose values are hidden.

    A pattern is a name or a dotted path, and it can have `*` wildcards. It matches
    at the end of a variable's path, at any depth, in any capitalization. The
    default covers `password`, `token`, `secret`, `api_key`, `access_token`,
    `refresh_token`, `authorization`, `otp`, `pin`, `credit_card` and `ssn`. The same
    patterns hide values in the response data shown in a report.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert "password" in ClientConfig().redact_variables
        config = ClientConfig(redact_variables=["password", "*.card_number"])
        assert "*.card_number" in config.redact_variables
        ```
    """
    redact_values: bool = True
    """Whether to also remove known secret values from free text.

    Free text is text like a server's error message or the query text. With
    `True`, a known secret is removed from it if it is at least
    `min_redacted_value_length` characters long. A server can echo such a value
    back. See `DiagnosticSnapshot` for what a known secret is. In short, it is a
    value of a header named in `redact_headers`, of a variable that matches
    `redact_variables`, a cookie, or a part of the URL, so add the name of every
    other header or variable that holds a secret. With `False`, no value is
    removed from free text, and only headers and variables that `redact_headers`
    and `redact_variables` name are replaced.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().redact_values is True
        assert ClientConfig(redact_values=False).redact_values is False
        ```
    """
    min_redacted_value_length: int = DEFAULT_MIN_REDACTED_VALUE_LENGTH
    """The shortest known secret that is removed from free text. The default is 8.

    A very short value, such as `1` or `on`, would match ordinary words and
    damage the text. So a known secret shorter than this stays in free text, for
    example a 3-character token that appears in the query. Give test accounts
    credentials that are at least this long. The limit applies only to known
    secrets. See `redact_values`.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().min_redacted_value_length == 8
        config = ClientConfig(min_redacted_value_length=12)
        assert config.min_redacted_value_length == 12
        ```
    """
    max_diagnostic_bytes: int = DEFAULT_MAX_DIAGNOSTIC_BYTES
    """The size limit of one field of a request shown in a report, in bytes.

    The default is 4096. Each cut says how much it removed.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().max_diagnostic_bytes == 4096
        assert ClientConfig(max_diagnostic_bytes=1024).max_diagnostic_bytes == 1024
        ```
    """
    max_recorded_errors: int = DEFAULT_MAX_RECORDED_ERRORS
    """How many of a response's errors a report or message lists. The default is 20.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().max_recorded_errors == 20
        assert ClientConfig(max_recorded_errors=5).max_recorded_errors == 5
        ```
    """
    max_recorded_calls: int = DEFAULT_MAX_RECORDED_CALLS
    """How many recent calls the client remembers for a report. The default is 50.

    Examples:
        ```python {.exec}
        from pytest_graphql import ClientConfig

        assert ClientConfig().max_recorded_calls == 50
        assert ClientConfig(max_recorded_calls=10).max_recorded_calls == 10
        ```
    """

    def call_timeout(self, override: float | None = None) -> float:
        """Return the one time limit, in seconds, that a call hands to the transport.

        The transport takes a single number for each call and applies it as a
        ceiling on each of its four phases. A `timeout` setting that is one
        number gives that number. A setting that has a limit for each phase gives
        the largest of them, which cuts none of the phases short. If any phase
        has no limit, the result is `math.inf`.

        A per-call `timeout` is used as it is. It can tighten a phase, and it can
        never make a phase longer than the configured limit.

        Args:
            override: A limit for this call alone, in seconds, or `None` to use
                the configured one.

        Returns:
            The limit in seconds.

        Raises:
            ValueError: When a limit is zero, negative, NaN or above 1,000,000.
            TypeError: When a limit is not a number.

        Examples:
            ```python {.exec}
            import httpx

            from pytest_graphql import ClientConfig

            assert ClientConfig(timeout=5).call_timeout() == 5
            assert ClientConfig(timeout=5).call_timeout(2) == 2
            phases = httpx.Timeout(connect=2, read=20, write=5, pool=1)
            assert ClientConfig(timeout=phases).call_timeout() == 20
            ```
        """
        if override is not None:
            return checked_seconds(override, source="the timeout option")
        configured = checked_timeout(self.timeout, source="ClientConfig.timeout")
        if not isinstance(configured, httpx.Timeout):
            return configured
        phases = (
            configured.connect,
            configured.read,
            configured.write,
            configured.pool,
        )
        if any(phase is None for phase in phases):
            return math.inf
        return max(phase for phase in phases if phase is not None)

    def selection_policy(self) -> SelectionPolicy:
        """Return the `SelectionPolicy` that these settings describe.

        It is built from `max_depth`, `cycle_policy`, `per_type_depth_cap`,
        `include_deprecated`, `max_fields`, `exclude` and `relay_aware`. The other
        fields of a policy have their defaults. A keyword on a single call can
        change some of the values for that call.

        Returns:
            A new policy.

        Examples:
            ```python {.exec}
            from pytest_graphql import ClientConfig

            config = ClientConfig(max_depth=2, exclude=["User.balance"])
            policy = config.selection_policy()
            assert policy.max_depth == 2
            assert policy.exclude == ("User.balance",)
            ```
        """
        return SelectionPolicy(
            max_depth=self.max_depth,
            cycle_policy=self.cycle_policy,
            per_type_depth_cap=self.per_type_depth_cap,
            include_deprecated=self.include_deprecated,
            max_fields=self.max_fields,
            exclude=self.exclude,
            relay_aware=self.relay_aware,
        )


# -- the client (part 6 of 2.14, call flow SPEC 5.2) --------------------------


def configuration_credentials(config: ClientConfig) -> tuple[tuple[str, str], ...]:
    """Every credential ``config`` holds, as ``(label, value)`` pairs.

    A configuration has four fields that carry one: ``headers``,
    ``schema_headers``, ``cookies`` and ``proxy``. Each call's request lists all
    of them in its secret set, whichever of them that call sends. Text built
    from the request is scrubbed before it is cut, so a request that did not
    know a credential could not remove it, and a cut through it would leave the
    start of it in the text (DESIGN section 7, "Stage order"). A server can echo
    any of them, because the schema load and the calls go to one endpoint.

    The header fields go through the header rule. A cookie value and what the
    proxy makes the pool send are always credentials.
    """
    found = list(header_credentials(config.headers, config.redact_headers))
    if config.schema_headers is not None:
        found.extend(header_credentials(config.schema_headers, config.redact_headers))
    found.extend(("cookie", str(value)) for value in config.cookies.values())
    if config.proxy is not None:
        # An unusable proxy is refused where it is used, and holds no credential
        # this function could name.
        with contextlib.suppress(ValueError):
            found.extend(_proxy_credentials(_parse_proxy(config.proxy, source="proxy")))
    return tuple(found)


class GraphQLClient:
    """A client that sends GraphQL operations to a server as one identity.

    A client has a transport, a schema and a `ClientConfig`. Call `query()` or
    `mutation()` with the name of a root field and its arguments as keywords. The
    client checks the call against the schema, chooses the fields to ask for,
    sends the request and returns the result as a `Node`, a `NodeList` or a
    plain value.

    Most code gets a client from the `gql` fixture or from `build_client()`.
    That function loads the schema and creates the transport. Construct a
    `GraphQLClient` yourself when you already have both.

    Identity changes make a clone. `with_headers()`, `with_auth()`, `as_()` and
    `anonymous()` return a new client that shares the schema, config, scalar
    registry and middleware, and has its own headers and auth. Over the default
    HTTP transport a clone also gets its own connection and cookie state, shares
    the connection pool, and must be closed.

    The client is a context manager. Leaving the `with` block closes it.

    Args:
        transport: What sends the requests.
        schema: The `graphql.GraphQLSchema` of the server.
        config: The settings. `None` means `ClientConfig()`.
        scalars: The custom scalars this client knows. `None` creates an empty
            `ScalarRegistry` that belongs to this client.
        fake_context: Which test the data of `gql.fake` is for, and the source of
            its `unique()` values. `None` makes a context for use outside pytest.
        middleware: Hooks that run around every call. See `Middleware`.
        auth: The identity applied to every request. See `Auth`.
        headers: Headers that the client adds on top of those of `config`.
        owns_transport: Whether `close()` closes `transport`. The default is
            `False`, so the client never closes a transport that you supplied.
        cleanup: Internal. The list of what the client closes. `build_client()`
            passes it. Leave it out.
        recorder: Internal. The record of recent calls that clones share. Leave it
            out.
        builder: Internal. The cache of generated selections that clones share.
            Leave it out.

    Examples:
        ```python {.exec}
        from pytest_graphql import GraphQLClient, build_client

        base = build_client(
            url="http://localhost:8000/graphql",
            transport=gql.transport,
            schema=gql.schema,
        )
        with GraphQLClient(transport=base.transport, schema=base.schema) as client:
            user = client.query("user", id="u1")
            assert user.name == "Ada Lovelace"
        ```
    """

    def __init__(
        self,
        *,
        transport: Transport,
        schema: GraphQLSchema,
        config: ClientConfig | None = None,
        scalars: ScalarRegistry | None = None,
        fake_context: FakeContext | None = None,
        middleware: Sequence[Middleware] = (),
        auth: Auth | None = None,
        headers: Mapping[str, str] | None = None,
        owns_transport: bool = False,
        cleanup: list[Closable] | None = None,
        recorder: DiagnosticsRecorder | None = None,
        builder: SelectionBuilder | None = None,
    ) -> None:
        self._transport = transport
        if cleanup is None:
            # public form: the flag builds the list
            self._cleanup: list[Closable] = [transport] if owns_transport else []
        else:
            # internal form: the list is already complete, and is never
            # appended to. Exactly one caller supplies it, `build_client`,
            # and the invariant that it closes the transport this client
            # declares is held by tests rather than by this constructor.
            self._cleanup = cleanup
        self.owns_transport = owns_transport
        """Whether closing this client closes its transport.

        Examples:
            ```python {.exec}
            from pytest_graphql import build_client

            assert gql.owns_transport is False

            client = build_client(
                url="http://localhost:8000/graphql", schema=gql.schema
            )
            assert client.owns_transport is True
            client.close()
            ```
        """
        self._closed = False

        self._schema = schema
        self._config = config if config is not None else ClientConfig()
        self._credentials = configuration_credentials(self._config)
        if scalars is not None and not isinstance(scalars, ScalarRegistry):
            raise TypeError(
                "scalars must be a ScalarRegistry or None, got "
                f"{type(scalars).__name__}."
            )
        if fake_context is not None and not isinstance(fake_context, FakeContext):
            raise TypeError(
                "fake_context must be a FakeContext or None, got "
                f"{type(fake_context).__name__}."
            )
        # One registry serves decoding, serialization and the factory, so a
        # scalar registered at any time is seen by all three on the next call.
        # Clones share it, and share the context, whose unique source must be
        # one object or two clones would each count from zero.
        self._scalars = scalars if scalars is not None else ScalarRegistry()
        self._fake_context = (
            fake_context if fake_context is not None else FakeContext.standalone()
        )
        self._fake: FakeNamespace | None = None
        self._middleware = tuple(middleware)
        self._auth = auth
        self._headers = dict(headers or {})
        self._recorder = (
            recorder
            if recorder is not None
            else DiagnosticsRecorder(self._config.max_recorded_calls)
        )
        self._builder = builder if builder is not None else SelectionBuilder(schema)
        self._expect: ExpectNamespace | None = None

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Release what this client owns.

        A client from `build_client()` that created its own transport closes the
        connection pool. A client over a transport that you gave it closes
        nothing, because that transport is yours. A clone closes only the
        transport that it derived for itself.

        It is safe to call it more than once. If closing several things fails,
        the failures are reported together and none is lost.

        Examples:
            ```python {.exec}
            from pytest_graphql import build_client

            client = build_client(
                url="http://localhost:8000/graphql",
                schema=gql.schema,
            )
            assert client.owns_transport
            client.close()
            client.close()
            ```
        """
        if self._closed:
            return
        self._closed = True
        errors = _close_all(self._cleanup)
        if errors:
            _report(errors, errors[-1])

    def __enter__(self) -> GraphQLClient:
        """Use the client in a `with` block. It returns the client itself.

        Examples:
            ```python {.exec}
            from pytest_graphql import build_client

            with build_client(
                url="http://localhost:8000/graphql",
                transport=gql.transport,
                schema=gql.schema,
            ) as client:
                assert client.query("user", id="u1").name == "Ada Lovelace"
            ```
        """
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the client when the `with` block ends. It never hides an exception.

        Examples:
            ```python {.exec}
            from pytest_graphql import build_client

            with build_client(
                url="http://localhost:8000/graphql",
                schema=gql.schema,
            ) as client:
                assert client.owns_transport
            ```
        """
        self.close()

    # -- read-only state ------------------------------------------------------

    @property
    def schema(self) -> GraphQLSchema:
        """The schema this client checks calls against, a `graphql.GraphQLSchema`.

        Examples:
            ```python {.exec}
            assert gql.schema.query_type.name == "Query"
            assert "User" in gql.schema.type_map
            ```
        """
        return self._schema

    @property
    def expect(self) -> ExpectNamespace:
        """Build matchers for the types of the schema: `gql.expect.Type(**fields)`.

        `gql.expect.User(name="Ada")` returns a `Matcher` that checks only the
        fields you name. The type name and the field names are checked against the
        schema when you build the matcher, so a typo fails on that line and not
        later. A field name works in its exact spelling and in snake_case. A type
        with no fields, such as an enum, an input object or a scalar, raises an
        error.

        Examples:
            ```python {.exec}
            user = gql.query("user", id="u1")
            assert user == gql.expect.User(id="u1", name="Ada Lovelace")
            assert user != gql.expect.User(name="Grace Hopper")
            ```
        """
        if self._expect is None:
            self._expect = ExpectNamespace(self._schema)
        return self._expect

    @property
    def fake(self) -> FakeNamespace:
        """Build test input for the input types of the schema: `gql.fake.Type()`.

        `gql.fake.CreatePostInput()` returns a `dict` with a value for each field
        of that input type. The values are seeded by `ClientConfig.seed` and by
        the test, so the same test gets the same payload on every run. Give
        keywords to override fields: `gql.fake.CreatePostInput(title="Hello")`. An
        override key is the exact field name or its snake_case form, and the result
        uses the exact names. An unknown key raises an error.

        Rules for the values:

        - A required field is always filled. An optional field is filled too,
          unless you pass `_required_only=True`.
        - Input objects nested up to `_depth` levels (default `2`) are filled
          in full. Deeper ones get their required fields only.
        - A list gets one to three elements.
        - A custom scalar gets the value from the `fake` function of its
          `ScalarSpec`. A scalar with no spec raises `ScalarNotRegisteredError`
          unless you give the field a value.
        - For a value that must differ on every call, use `unique()`.

        Examples:
            ```python {.exec}
            payload = gql.fake.CreatePostInput(title="Hello")
            assert payload["title"] == "Hello"
            assert set(payload) == {"title", "authorId"}
            assert payload == gql.fake.CreatePostInput(title="Hello")

            post = gql.mutation("createPost", input=payload, fields=["title"])
            assert post.title == "Hello"
            ```
        """
        if self._fake is None:
            self._fake = FakeNamespace(
                self._schema,
                self._scalars,
                global_seed=self._config.seed,
                node_id=self._fake_context.node_id,
                unique_source=self._fake_context.unique_source,
            )
        return self._fake

    @property
    def scalars(self) -> ScalarRegistry:
        """The custom scalars that this client decodes, serializes and fakes.

        The registry is shared with the clones of the client. A scalar that you
        register takes effect on the next call.

        Examples:
            ```python {.exec}
            from pytest_graphql import ScalarSpec

            gql.scalars.register(ScalarSpec(name="Money", serialize=str, fake=str))
            assert "Money" in gql.scalars
            ```
        """
        return self._scalars

    @property
    def config(self) -> ClientConfig:
        """The settings of this client.

        Examples:
            ```python {.exec}
            assert gql.config.max_depth == 3
            assert gql.config.raise_on_error is True
            ```
        """
        return self._config

    @property
    def transport(self) -> Transport:
        """The transport that this client sends its requests through.

        Examples:
            ```python {.exec}
            assert callable(gql.transport.send)
            assert callable(gql.transport.close)
            ```
        """
        return self._transport

    @property
    def recorder(self) -> DiagnosticsRecorder:
        """The record of this client's recent calls.

        The pytest plugin reads it to add the calls of a failed test to the
        report. Clones of a client share one recorder. It keeps at most
        `ClientConfig.max_recorded_calls` calls, as redacted text, and never a
        live request or response. Most code does not use it directly.

        Examples:
            ```python {.exec}
            gql.query("user", id="u1", fields=["id"])
            assert len(gql.recorder.calls) >= 1
            ```
        """
        return self._recorder

    # -- identity and cloning (C17, C19) --------------------------------------

    def _clone(
        self,
        *,
        auth: Auth | None,
        headers: Mapping[str, str],
    ) -> GraphQLClient:
        """A clone with its own identity, and its own transport where it can.

        Cookie state is identity state and is never inherited, so a clone
        over a derivable transport derives one and owns it. A clone over any
        other transport shares the parent's and owns nothing, which is B2's
        original behaviour as C19 corrected it.
        """
        return _client_over(
            self._transport,
            lambda transport, owns: GraphQLClient(
                transport=transport,
                schema=self._schema,
                config=self._config,
                scalars=self._scalars,
                fake_context=self._fake_context,
                middleware=self._middleware,
                auth=auth,
                headers=headers,
                owns_transport=owns,
                recorder=self._recorder,
                builder=self._builder,
            ),
        )

    def with_headers(
        self, headers: Mapping[str, str] | None = None, /, **named: str
    ) -> GraphQLClient:
        """Return a clone that sends extra headers.

        The new headers are added over the ones this client already sends. A
        header with the same name, in any capitalization, replaces the old one.
        The clone's headers rank above those of the config and of `Auth`, and
        below a `headers=` option on a single call.

        Header names that contain a hyphen cannot be keyword arguments, so the
        method also takes a mapping as its first positional argument. You can
        use both in one call.

        A clone over the default HTTP transport has its own connection and
        cookie state, and you must close it. Use it in a `with` block.

        Args:
            headers: An optional mapping of header names to values. It is
                positional only.
            **named: More headers, written as keyword arguments.

        Returns:
            A new client.

        Examples:
            ```python {.exec}
            headers = {"X-Api-Key": "key-123"}
            with gql.with_headers(headers, Authorization="Bearer abc") as client:
                assert client.query("user", id="u1").name == "Ada Lovelace"
            ```
        """
        return self._clone(
            auth=self._auth,
            headers=merge_headers(self._headers, headers, named),
        )

    def with_auth(self, auth: Auth) -> GraphQLClient:
        """Return a clone that uses another `Auth` object.

        The clone's headers and the rest of its settings stay as they are. Only the
        auth changes. Over the default HTTP transport the clone has its own
        connection and cookie state, and you must close it.

        Args:
            auth: The identity for the clone.

        Returns:
            A new client.

        Examples:
            ```python {.exec}
            from pytest_graphql import HeaderAuth

            with gql.with_auth(HeaderAuth({"X-Api-Key": "key-123"})) as client:
                assert client.query("user", id="u1").name == "Ada Lovelace"
            ```
        """
        return self._clone(auth=auth, headers=self._headers)

    def as_(self, auth: Auth | str) -> GraphQLClient:
        """Return a clone that acts as another identity.

        Pass an `Auth` object, or a plain string, which is taken as a bearer
        token. `gql.as_("tok")` is the same as `gql.with_auth(BearerAuth("tok"))`.
        Use it to test the same call as different users.

        Args:
            auth: An `Auth` object, or a bearer token as a string.

        Returns:
            A new client.

        Examples:
            ```python {.exec}
            with gql.as_("admin-token") as admin, gql.as_("guest-token") as guest:
                assert admin.query("user", id="u1").name == "Ada Lovelace"
                assert guest.query("user", id="u1").name == "Ada Lovelace"
            ```
        """
        return self.with_auth(resolve_auth(auth))

    def anonymous(self) -> GraphQLClient:
        """Return a clone that sends no auth.

        Use it to check that a call is refused without a login. The clone drops
        the `Auth` object. Headers that were set with `with_headers()` stay.

        Returns:
            A new client.

        Examples:
            ```python {.exec}
            with gql.as_("admin-token") as admin:
                with admin.anonymous() as visitor:
                    assert visitor.query("users").pluck("id") == ["u1", "u2", "u3"]
            ```
        """
        return self._clone(auth=None, headers=self._headers)

    # -- calling operations (SPEC 5.2) ----------------------------------------

    def query(self, name: str, /, **variables: Any) -> Any:
        """Run a query by the name of a root field and return its result.

        The client looks the field up in the schema, checks the arguments you
        gave, chooses the fields to ask for (unless you list them), builds the
        operation, sends it and returns the value of that one field. The result is
        a `Node` for an object, a `NodeList` for a list of objects, and a plain
        value for a scalar. Arguments are sent as variables, so a value never
        changes the shape of the query text.

        Give each argument of the field as a keyword. A keyword works in its exact
        schema spelling and in snake_case. The keywords below are options, and not
        arguments. An argument of the field that has one of these names can only be
        given through `variables=`.

        - `fields`: what to select. A list or dict of names, a `Field`, a
          `Selection`, a raw GraphQL string, or `AUTO`, the default.
        - `variables`: a dict of variables by their exact names. Use it for an
          argument that has the name of an option. Giving one argument both
          ways is an error.
        - `raw`: return the whole `GraphQLResponse` and not the value of the field.
        - `validate`: check the operation against the schema before it is sent.
        - `raise_on_error`, `raise_on_partial`: see `ClientConfig`.
        - `max_depth`, `cycle_policy`, `per_type_depth_cap`, `include_deprecated`,
          `max_fields`: the selection settings of `ClientConfig`, for this call.
        - `operation_name`: the name written into the operation. The default is
          the field name.
        - `timeout`: the time limit of this call, in seconds. It can shorten a
          configured limit and cannot make it longer.
        - `idempotent`: allow a mutation to be tried again after a failure to
          connect. A query is always allowed.
        - `headers`: extra headers for this call. They rank above every other
          source of headers.
        - `retries`: reserved. It has no effect. Set `ClientConfig.retries`.

        When the operation has one argument of an input object type, you may give
        the fields of that object as keywords, and they are wrapped into it. This
        works only when no keyword is an argument of the operation, and at least
        one is a field of the input. Writing `input=payload` always works.

        Every variable is checked against its input type before it is sent, and a
        value of a custom scalar goes through the `serialize` function of its
        `ScalarSpec`. A wrong value raises an error that names the path and the
        type and never repeats the value.

        Args:
            name: The name of the root query field, such as `"user"`.
            **variables: The arguments of the field, and the options above.

        Returns:
            The value of the field. With `raw=True`, the `GraphQLResponse`.

        Raises:
            OperationNotFoundError: When the schema has no query with that name.
                The message suggests the closest name.
            ArgumentError: When an argument is missing, unknown or has a wrong
                value.
            SelectionError: When `fields` is wrong or the operation is not valid.
            GraphQLExecutionError: When the server returned errors and no data.
            GraphQLPartialDataError: When the server returned data and errors.
            ResponseShapeError: When the response does not fit the schema.

        Examples:
            ```python {.exec}
            user = gql.query("user", id="u1")
            assert user.name == "Ada Lovelace"

            user = gql.query("user", id="u1", fields=["name", {"team": ["name"]}])
            assert user.team.name == "Core"

            response = gql.query("user", id="u1", fields=["name"], raw=True)
            assert response.http.status_code == 200
            assert response.data.user.name == "Ada Lovelace"
            ```
        """
        return self._call("query", name, variables)

    def mutation(self, name: str, /, **variables: Any) -> Any:
        """Run a mutation by the name of a root field and return its result.

        It works as `query()` does, with the same arguments, options and
        results, for a root field of the `Mutation` type. A mutation is not tried
        again after a failure to connect unless you pass `idempotent=True`.

        Args:
            name: The name of the root mutation field, such as `"createPost"`.
            **variables: The arguments of the field, and the options of `query()`.

        Returns:
            The value of the field. With `raw=True`, the `GraphQLResponse`.

        Raises:
            OperationNotFoundError: When the schema has no mutation with that name.
            ArgumentError: When an argument is missing, unknown or has a wrong
                value.
            GraphQLExecutionError: When the server returned errors and no data.
            GraphQLPartialDataError: When the server returned data and errors.

        Examples:
            ```python {.exec}
            payload = gql.fake.CreatePostInput(title="Hello")
            post = gql.mutation("createPost", input=payload, fields=["title"])
            assert post.title == "Hello"

            # The fields of the input object can be given as plain keywords.
            post = gql.mutation("createPost", title="Hi", author_id="u1")
            assert post.author.name == "Ada Lovelace"
            ```
        """
        return self._call("mutation", name, variables)

    def execute(
        self,
        document: str,
        variables: Mapping[str, Any] | None = None,
        *,
        operation_name: str | None = None,
        **options: Any,
    ) -> GraphQLResponse[Any]:
        """Send a GraphQL document that you wrote, and return the response.

        Use it for a query that `query()` and `mutation()` cannot build, such as
        one with directives or with several root fields. The document is checked
        against the schema first, unless you turn that off. Values go in
        `variables`, and each custom scalar value is serialized and checked as in
        `query()`.

        It always returns the `GraphQLResponse`. A document may select any number
        of root fields, so there is no single value to unwrap. Call
        `GraphQLResponse.unwrap()` when it has exactly one. Errors are raised as
        they are for `query()`.

        These options work here: `validate`, `raise_on_error`, `raise_on_partial`,
        `timeout`, `idempotent` and `headers`. See `query()`.

        Args:
            document: The GraphQL text. It may have several operations.
            variables: The variables by name. Only variables that the operation
                declares are sent.
            operation_name: Which operation to run, when the document has more
                than one. It is an error to leave it out then.
            **options: The options listed above.

        Returns:
            The response.

        Raises:
            graphql.GraphQLSyntaxError: When the text is not valid GraphQL.
            SelectionError: When the document does not pass validation against
                the schema, or has several operations and no `operation_name`.
            ArgumentError: When a variable has a wrong value.
            GraphQLExecutionError: When the server returned errors and no data.
            GraphQLPartialDataError: When the server returned data and errors.

        Examples:
            ```python {.exec}
            response = gql.execute(
                "query Whoami($id: ID!) { user(id: $id) { name } }",
                {"id": "u1"},
            )
            assert response.data.user.name == "Ada Lovelace"
            assert response.unwrap().name == "Ada Lovelace"
            ```
        """
        parsed = parse(document)
        if options.get("validate", self._config.validate):
            validate_document(self._schema, parsed)
        definition = _operation_definition(parsed, operation_name)
        values = coerce_variables(
            _declared_variables(self._schema, definition, variables or {}),
            kind=definition.operation.value,
            operation_name=operation_name or "<anonymous>",
            scalars=self._scalars,
        )
        return self._send(
            kind=_operation_kind(definition),
            operation_name=(
                operation_name
                if operation_name is not None
                else (definition.name.value if definition.name else None)
            ),
            document=parsed,
            variables=values,
            options=options,
            omissions=(),
            omissions_total=0,
        )

    # -- assertions and polling (SPEC 3.8, 3.9) -------------------------------

    def expect_error(
        self,
        *,
        code: str | None = None,
        path: Sequence[str | int] | None = None,
        message_matches: str | re.Pattern[str] | None = None,
        count: int | None = None,
    ) -> AbstractContextManager[CapturedErrors]:
        """Check that the code in a `with` block fails with a GraphQL error.

        The block must end with a `GraphQLExecutionError`. A
        `GraphQLPartialDataError` counts, because it is one. Any other exception
        passes through unchanged. If the block ends without one, or a filter
        fails, it raises `ExpectedErrorNotRaised`. A client set to not raise, with
        `raise_on_error=False` or `raise_on_partial=False`, never satisfies the
        block.

        Each filter you give must match at least one error of the response. The
        filters need not match the same error.

        - `code` equals `extensions["code"]` of the error.
        - `path` equals the whole path of the error, segment by segment. It is not
          a prefix.
        - `message_matches` is a regular expression search on the message.
        - `count` is the exact number of errors that the server returned. It does not
          depend on the other filters.

        A block sees the calls of every client in its context, so a call through
        `gql.as_(...)` counts. The values are checked when you call
        `expect_error()`, and a wrong one fails there.

        Args:
            code: The error code to look for.
            path: The error path to look for, as names and list indexes.
            message_matches: A pattern, as text or as a compiled pattern.
            count: How many errors the server returned. An integer of at least 1.

        Returns:
            A context manager. It gives a `CapturedErrors` object that has
            `errors` (every error that the server returned), `first` (the first
            of them) and `response`. Read them after the block has ended.

        Raises:
            ExpectedErrorNotRaised: When the block does not end in a matching
                error. It is raised on leaving the block.
            TypeError: When a filter has the wrong type.
            ValueError: When a filter has a wrong value, such as a `count` below 1.

        Examples:
            ```python {.exec}
            with gql.expect_error(path=["updateUser"], count=1) as caught:
                gql.mutation("updateUser", id="missing", name="Ada", fields=["id"])
            assert caught.first.path == ("updateUser",)
            assert len(caught.errors) == 1
            ```
        """
        return ExpectedError(
            code=code, path=path, message_matches=message_matches, count=count
        )

    def wait_until(
        self,
        name: str,
        /,
        *,
        until: Callable[[Any], Any],
        timeout: float = 30.0,
        interval: float = 1.0,
        backoff: float = 1.0,
        ignore: type[Exception] | tuple[type[Exception], ...] = (),
        **variables: Any,
    ) -> Any:
        """Run a query again and again until a condition holds, or time runs out.

        Use it when a result becomes true later, for example after a background
        job. At least one attempt always runs, even with `timeout=0`. After a failed
        attempt the call sleeps `interval * backoff ** (attempt - 1)` seconds, and
        never longer than the time that is left. The deadline is the only
        limit. It is set once at the start.

        The query takes the same arguments and options as `query()`. In this
        method `timeout` is the deadline of the whole wait. The time limit of one
        HTTP call is `ClientConfig.timeout`.

        Only queries can be polled. A name that is a mutation raises an error
        before any call is made.

        `ignore` covers one whole attempt: the call, the unwrapping of the
        response, and `until`. It names exception classes that are swallowed and
        counted as a failed attempt. Any other exception passes through unchanged.

        Args:
            name: The name of the root query field.
            until: A function that gets what `query()` returns for the same
                arguments, or the whole response when `raw=True`. The wait ends
                when its result is true.
            timeout: The deadline of the whole wait, in seconds. A number of zero
                or more. Infinity is not accepted.
            interval: The first sleep between attempts, in seconds. A number of
                zero or more.
            backoff: The factor by which the sleep grows after each attempt. A
                number of one or more. The default `1.0` keeps it constant.
            ignore: An exception class, or a tuple of classes, to swallow. Each
                must be a subclass of `Exception`.
            **variables: The arguments of the field, and the options of `query()`.

        Returns:
            The value that `until` accepted. It is what `query()` returns.

        Raises:
            WaitTimeoutError: When the deadline passes before `until` holds. It
                tells you the attempts, the time, the last response and the last
                swallowed exception.
            ArgumentError: When `name` is a mutation.
            TypeError: When `timeout`, `interval` or `backoff` is not a number, or
                `ignore` has a class that is not an `Exception` subclass.
            ValueError: When a number is out of range.

        Examples:
            ```python {.exec}
            user = gql.wait_until(
                "user",
                id="u1",
                until=lambda user: user.name == "Ada Lovelace",
                timeout=5,
                interval=0.1,
            )
            assert user.name == "Ada Lovelace"
            ```
        """
        return _wait_until(
            self,
            name,
            until=until,
            timeout=timeout,
            interval=interval,
            backoff=backoff,
            ignore=ignore,
            **variables,
        )

    # -- the call flow --------------------------------------------------------

    def _call(self, kind: OperationKind, name: str, kwargs: Mapping[str, Any]) -> Any:
        """Steps 1 to 14 of SPEC 5.2 for a schema-resolved operation."""
        response = self._run_operation(kind, name, kwargs)
        if bool(kwargs.get("raw", False)):
            return response
        return response.unwrap()

    def _run_operation(
        self,
        kind: OperationKind,
        name: str,
        kwargs: Mapping[str, Any],
        *,
        trace: RequestTrace | None = None,
    ) -> GraphQLResponse[Any]:
        """Steps 1 to 13 of SPEC 5.2: the response, before any unwrapping.

        ``wait_until`` calls this directly, because it needs the response of
        an attempt whether or not ``until`` accepts its value. It passes a
        ``trace`` so that a failure before any response still names its request.
        """
        options = {
            key: value for key, value in kwargs.items() if key in RESERVED_OPTIONS
        }
        assembled = assemble_operation(
            schema=self._schema,
            kind=kind,
            name=name,
            kwargs=kwargs,
            fields=cast("SelectionInput", options.get("fields", AUTO)),
            policy=self._policy_for(options),
            builder=self._builder,
            operation_name=options.get("operation_name"),
            validate=bool(options.get("validate", self._config.validate)),
            scalars=self._scalars,
        )
        return self._send(
            kind=kind,
            operation_name=options.get("operation_name") or name,
            document=assembled.document,
            variables=assembled.variables,
            options=options,
            omissions=assembled.omissions,
            omissions_total=assembled.omissions_total,
            trace=trace,
        )

    def _policy_for(self, options: Mapping[str, Any]) -> SelectionPolicy:
        """The configured policy, with this call's own overrides applied."""
        config = self._config
        return SelectionPolicy(
            max_depth=options.get("max_depth", config.max_depth),
            cycle_policy=options.get("cycle_policy", config.cycle_policy),
            per_type_depth_cap=options.get(
                "per_type_depth_cap", config.per_type_depth_cap
            ),
            include_deprecated=options.get(
                "include_deprecated", config.include_deprecated
            ),
            max_fields=options.get("max_fields", config.max_fields),
            exclude=config.exclude,
            relay_aware=config.relay_aware,
        )

    def _build_request(
        self,
        *,
        kind: OperationKind,
        operation_name: str | None,
        document: DocumentNode,
        variables: Mapping[str, Any],
        options: Mapping[str, Any],
        omissions: Sequence[OmissionRecord],
        omissions_total: int,
    ) -> RequestInfo:
        """Assemble the request, applying the C4 header precedence in order.

        Lowest to highest: ``ClientConfig.headers``, then ``Auth.apply``,
        then the headers this client accumulated through ``with_headers()``
        in clone order, then this call's own ``headers=``. Auth runs in the
        middle rather than last because it receives and returns the whole
        request, so it must not be able to overwrite a header the caller set
        on the clone or on the call.
        """
        config = self._config
        request = RequestInfo(
            operation=operation_name,
            kind=kind,
            document=print_ast(document),
            variables=variables,
            headers=merge_headers(config.headers),
            url=config.url or "",
            idempotent=bool(options.get("idempotent", False)),
            redact_headers=frozenset(config.redact_headers),
            redact_variables=tuple(config.redact_variables),
            redact_values=config.redact_values,
            min_redacted_value_length=config.min_redacted_value_length,
            max_diagnostic_bytes=config.max_diagnostic_bytes,
            max_recorded_errors=config.max_recorded_errors,
            omissions=tuple(omissions),
            omissions_total=omissions_total,
            transport_credentials=self._credentials,
        )
        if self._auth is not None:
            request = self._auth.apply(request)
        return dataclasses.replace(
            request,
            headers=merge_headers(
                request.headers, self._headers, options.get("headers")
            ),
        )

    def _with_held(self, request: RequestInfo) -> RequestInfo:
        """``request`` with what past calls sent added to its secret set.

        Text is cut while a call is recorded, and the request of a later call
        does not carry a header, variable or cookie that an earlier call sent. A
        server that repeats one, with a cap that ends inside it, would leave the
        start of it in the text. The transport builds text from the request it is
        handed too, so it is handed this one.
        """
        return dataclasses.replace(
            request,
            transport_credentials=(
                *request.transport_credentials,
                *self._recorder._held(),
            ),
        )

    def _send(
        self,
        *,
        kind: OperationKind,
        operation_name: str | None,
        document: DocumentNode,
        variables: Mapping[str, Any],
        options: Mapping[str, Any],
        omissions: Sequence[OmissionRecord],
        omissions_total: int,
        trace: RequestTrace | None = None,
    ) -> GraphQLResponse[Any]:
        """SPEC 5.2 steps 7 to 13: middleware, transport, response, record.

        A ``trace`` receives the redacted request when the call fails after
        the request was built, so the caller can check the failure's text
        against it.
        """
        request = self._build_request(
            kind=kind,
            operation_name=operation_name,
            document=document,
            variables=variables,
            options=options,
            omissions=omissions,
            omissions_total=omissions_total,
        )
        try:
            request = apply_before_request(self._middleware, request)
            # Presence, not the value, decides whether the caller set the
            # option: an explicit ``timeout=None`` is outside the domain and
            # is refused, never read as "use the configured timeout".
            timeout = (
                checked_seconds(options["timeout"], source="the timeout option")
                if "timeout" in options
                else self._config.call_timeout()
            )
        except BaseException:
            if trace is not None:
                trace.snapshot = self._with_held(request).redacted()
            raise

        # The request as it will be sent: what it carries is what a server can
        # repeat in a later call's response.
        self._recorder._hold(request_credentials(request))
        request = self._with_held(request)

        started = time.perf_counter()
        status_code: int | None = None
        try:
            raw = self._transport.send(request, timeout=timeout)
            status_code = raw.status_code
            duration_ms = (time.perf_counter() - started) * 1000.0
            if raw.transport_credentials:
                self._recorder._hold(raw.transport_credentials)
                request = dataclasses.replace(
                    request,
                    transport_credentials=(
                        *request.transport_credentials,
                        *raw.transport_credentials,
                    ),
                )
            response = build_response(
                raw,
                request=request,
                schema=self._schema,
                document=document,
                parsers=self._scalars.parsers(),
                operation_name=operation_name,
                duration_ms=duration_ms,
            )
            response = apply_after_response(self._middleware, response)
        except BaseException as failure:
            # Recorded before the exception leaves, because a failed call is
            # the one a report most needs. A call fails here when the
            # transport raises, and also after it returned: a response that
            # contradicts the schema, or a raising `after_response`.
            snapshot = request.redacted()
            if trace is not None:
                trace.snapshot = snapshot
            self._recorder.record(
                RecordedCall(
                    request=snapshot,
                    outcome="failed",
                    status_code=status_code,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    failure=safe_excerpt(request, _failure_text(failure)),
                    curl=_curl_text(request),
                )
            )
            raise

        self._recorder.record(_recorded_response(request, response))
        # Every response is seen by an open `expect_error` block before the
        # client decides whether to raise it, so the block can name the
        # response it received even when nothing raised.
        observe_response(response)
        self._raise_for_state(response, options)
        return response

    def _raise_for_state(
        self, response: GraphQLResponse[Any], options: Mapping[str, Any]
    ) -> None:
        """B16, as C5 refined it: the complete raising table, in order.

        ``raise_on_error=False`` disables all raising, partial included, so
        it is tested first. ``raise_on_partial`` applies only underneath it.
        """
        config = self._config
        raise_on_error = bool(options.get("raise_on_error", config.raise_on_error))
        raise_on_partial = bool(
            options.get("raise_on_partial", config.raise_on_partial)
        )
        if not response.errors:
            if response.data_state != "present":
                raise GraphQLExecutionError(
                    "the server returned no errors and no data "
                    f"({response.data_state}), which is a protocol violation.",
                    response=response,
                )
            return
        if not raise_on_error:
            return
        if response.has_data:
            if raise_on_partial:
                raise GraphQLPartialDataError(
                    f"the server returned {len(response.errors)} error(s) "
                    "alongside data.",
                    response=response,
                )
            return
        raise GraphQLExecutionError(
            f"the server returned {len(response.errors)} error(s).",
            response=response,
        )


def _curl_text(request: RequestInfo) -> str:
    """``as_curl()`` for a record, or the withheld notice when it refuses.

    Recording must not replace the exception the caller is waiting for, and a
    refusal is a ``DiagnosticRenderError``, so it cannot propagate from here.
    """
    try:
        return request.as_curl()
    except Exception:
        return WITHHELD_TEXT


def _recorded_response(
    request: RequestInfo, response: GraphQLResponse[Any]
) -> RecordedCall:
    """The record of a call that returned a response, text built from ``request``.

    The error lines were built when the response was, with the live request in
    hand. The data excerpt is built here for the same reason, and checked once
    against the snapshot before it is kept.
    """
    try:
        excerpt = render_data_excerpt(request, response.raw.get("data"))
        text = require_safe_rendering(response.request, excerpt.text, "data excerpt")
        fields, cut = excerpt.fields, excerpt.cut
    except Exception:
        text, fields, cut = WITHHELD_TEXT, 0, False
    return RecordedCall(
        request=response.request,
        outcome="errors" if response.errors else "ok",
        status_code=response.http.status_code,
        duration_ms=response.duration_ms,
        error_count=len(response.errors),
        errors=tuple(
            info._summary for info in response.errors if info._summary is not None
        ),
        data=text,
        data_fields=fields,
        data_cut=cut,
        curl=_curl_text(request),
    )


def _failure_text(failure: BaseException) -> str:
    """The text a recorded call shows for the exception that failed it.

    Recording must not replace the exception the caller is waiting for, so
    a message that cannot be rendered is left out and the type name stays.
    """
    name = type(failure).__name__
    try:
        message = str(failure)
    except Exception:
        return f"{name}: <message unavailable>"
    return f"{name}: {message}"


def _operation_definition(
    document: DocumentNode, operation_name: str | None
) -> OperationDefinitionNode:
    """The one operation ``execute()`` is sending, named or sole."""
    definitions = [
        node
        for node in document.definitions
        if isinstance(node, OperationDefinitionNode)
    ]
    if operation_name is not None:
        for node in definitions:
            if node.name is not None and node.name.value == operation_name:
                return node
        raise SelectionError(
            f"the document declares no operation named {operation_name!r}.\n"
            f"  It declares: "
            f"{', '.join(n.name.value for n in definitions if n.name) or 'none'}."
        )
    if len(definitions) != 1:
        raise SelectionError(
            f"the document declares {len(definitions)} operations, so one has "
            "to be chosen.\n  Pass operation_name= to name it."
        )
    return definitions[0]


def _declared_variables(
    schema: GraphQLSchema,
    definition: OperationDefinitionNode,
    values: Mapping[str, Any],
) -> list[tuple[str, GraphQLInputType, Any]]:
    """Each declared variable with its resolved type and supplied value (B14)."""
    declared: list[tuple[str, GraphQLInputType, Any]] = []
    for node in ast_tuple(definition.variable_definitions):
        name = node.variable.name.value
        if name not in values:
            continue
        type_ = type_from_ast(schema, node.type)
        if type_ is None:
            raise SelectionError(
                f"variable ${name} is declared with a type the schema does not define."
            )
        declared.append((name, cast("GraphQLInputType", type_), values[name]))
    return declared


# -- the factory (part 5 of 2.14) ---------------------------------------------


class _IntrospectionExecutor:
    """A ``QueryExecutor`` over a ``Transport``, for schema loading only.

    Schema identity is explicit (C4, B20): this uses
    ``ClientConfig.schema_headers``, which defaults to ``ClientConfig.headers``,
    and never an ``Auth`` or a per-call header. The schema is session-scoped,
    so a function-scoped identity must not be able to change it.
    """

    __slots__ = ("_config", "_transport", "_url")

    def __init__(self, transport: Transport, url: str, config: ClientConfig) -> None:
        self._transport = transport
        self._url = url
        self._config = config

    def execute(
        self, query: str, *, variables: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        config = self._config
        schema_headers = (
            config.headers if config.schema_headers is None else config.schema_headers
        )
        request = RequestInfo(
            operation="IntrospectionQuery",
            kind="query",
            document=query,
            variables=variables or {},
            headers=merge_headers(schema_headers),
            url=self._url,
            redact_headers=frozenset(config.redact_headers),
            redact_variables=tuple(config.redact_variables),
            redact_values=config.redact_values,
            min_redacted_value_length=config.min_redacted_value_length,
            max_diagnostic_bytes=config.max_diagnostic_bytes,
            max_recorded_errors=config.max_recorded_errors,
            transport_credentials=configuration_credentials(config),
        )
        raw = self._transport.send(request, timeout=config.call_timeout())
        envelope: dict[str, Any] = {"data": raw.data}
        if raw.errors:
            envelope["errors"] = list(raw.errors)
        return envelope


def _new_root_pool(config: ClientConfig) -> HttpxTransport:
    """The one root connection pool this factory owns (C17)."""
    return HttpxTransport(
        timeout=config.timeout,
        max_attempts=config.retries + 1,
        max_response_bytes=config.max_response_bytes,
        trust_env=config.trust_env,
        proxy=config.proxy,
        verify=config.verify,
        http2=config.http2,
        cookie_scope=config.cookie_scope,
    )


def _derive(pool: HttpxTransport, *, own_pool: bool = False) -> HttpxTransport:
    """One derived transport over ``pool``: its own client, jar and headers."""
    return pool.derive(own_pool=own_pool)


def _load_schema(
    probe: Transport, url: str, config: ClientConfig, source: SchemaSource | None
) -> GraphQLSchema:
    """Load the schema over the throwaway introspection transport."""
    if source is None:
        source = IntrospectionSource(_IntrospectionExecutor(probe, url, config))
    return source.load()


def _configure(
    config: ClientConfig | None, url: str, options: Mapping[str, Any]
) -> ClientConfig:
    """One ``ClientConfig`` from a supplied one and the factory's own options.

    B4's split decides which name goes where, so this is the only rule the
    factory needs: every data option is a ``ClientConfig`` field and lands
    here, and every object stays a constructor argument. An option given to
    the factory overrides the same field on a supplied ``config``, because a
    caller who writes it at the call site meant it for this build.

    An unknown name raises rather than being ignored, so a typo cannot leave
    a client silently running on a default the caller thought they replaced.
    """
    base = ClientConfig() if config is None else config
    known = tuple(f.name for f in dataclasses.fields(base))
    for name in options:
        if name not in known:
            raise ArgumentError.unknown_option(
                callable_name="build_client()", bad_name=name, candidates=known
            )
    return dataclasses.replace(base, **dict(options), url=url)


def build_client(
    *,
    url: str,
    transport: Transport | None = None,
    cleanup: list[Closable] | None = None,
    config: ClientConfig | None = None,
    schema: GraphQLSchema | None = None,
    schema_source: SchemaSource | None = None,
    scalars: ScalarRegistry | None = None,
    fake_context: FakeContext | None = None,
    middleware: Sequence[Middleware] = (),
    auth: Auth | None = None,
    **config_options: Any,
) -> GraphQLClient:
    """Build a client, with its transport and its schema, for use outside pytest.

    This is the standalone way to get a `GraphQLClient`. Under pytest, the `gql`
    fixture does the same work.

    Without a `transport`, it creates an HTTP transport and loads the schema from
    the server by the standard introspection query. You can skip that request
    with `schema=` (a schema you have) or `schema_source=` (a `SchemaSource` that
    knows where to read it). The client owns the transport it created, so close
    the client when you are done. Use it in a `with` block.

    With a `transport`, the function creates no pool and closes nothing. What you
    give is yours to close. If no schema is given, it is loaded through that
    transport.

    If anything fails while the client is built, everything that was created is
    closed before the error leaves, and no connection is left open.

    Each keyword that is not named below is a field of `ClientConfig`, so
    `build_client(url=..., headers=..., max_depth=3)` works without a config
    object. A keyword overrides the same field of a `config` that you also give.
    A name that is not a field raises `ArgumentError` and lists the valid names, so
    a misspelled option cannot leave you on a default. `headers` given here are
    what the schema request sends, which an endpoint with a login needs. To rank a
    header above `Auth`, set it on the returned client with `with_headers()`.

    Args:
        url: The URL of the GraphQL endpoint.
        transport: What sends the requests. `None` creates an HTTP transport.
        cleanup: Internal. A list that receives each resource that this function
            creates. Leave it out.
        config: The settings. Keywords below override its fields.
        schema: A schema to use as it is. The schema is not loaded.
        schema_source: Where to read the schema from. It is ignored when `schema`
            is given.
        scalars: The custom scalars the client knows. `None` creates an empty
            registry.
        fake_context: Which test the data of `gql.fake` is for. `None` makes a
            context for use outside pytest.
        middleware: Hooks that run around every call. See `Middleware`.
        auth: The identity applied to every request. See `Auth`.
        **config_options: Fields of `ClientConfig`, such as `headers`, `timeout`,
            `max_depth` or `raise_on_error`.

    Returns:
        A new client.

    Raises:
        ArgumentError: When a keyword is not a field of `ClientConfig`.
        SchemaError: When the schema cannot be loaded or is not valid.

    Examples:
        ```python {.exec}
        from pytest_graphql import build_client

        with build_client(
            url="http://localhost:8000/graphql",
            transport=gql.transport,
            schema=gql.schema,
            max_depth=2,
        ) as client:
            assert client.config.max_depth == 2
            assert client.query("user", id="u1").name == "Ada Lovelace"
        ```

        Against a real server, the call is the same without `transport` and
        `schema`:

        ```python {.no-exec}
        from pytest_graphql import build_client

        with build_client(
            url="http://localhost:8000/graphql",
            headers={"Authorization": "Bearer my-token"},
        ) as gql:
            user = gql.query("user", id="u1")
        ```
    """
    config = _configure(config, url, config_options)

    if transport is not None:
        loaded = (
            schema
            if schema is not None
            else _load_schema(transport, url, config, schema_source)
        )
        return GraphQLClient(
            transport=transport,
            schema=loaded,
            config=config,
            scalars=scalars,
            fake_context=fake_context,
            middleware=middleware,
            auth=auth,
            owns_transport=False,
        )

    cleanup = [] if cleanup is None else cleanup
    try:
        pool = _Owned(cleanup, _new_root_pool, config)
        probe = _Owned(cleanup, _derive, pool.value, own_pool=False)
        loaded = (
            schema
            if schema is not None
            else _load_schema(probe.value, url, config, schema_source)
        )
        probe.close()
        owner = _Owned(cleanup, _derive, pool.value, own_pool=False)
        return GraphQLClient(
            transport=owner.value,
            owns_transport=True,
            cleanup=cleanup,
            schema=loaded,
            config=config,
            scalars=scalars,
            fake_context=fake_context,
            middleware=middleware,
            auth=auth,
        )
    except BaseException as failure:
        errors = _close_all(cleanup)
        if errors:
            _report(errors, failure)
        raise


def _operation_kind(definition: OperationDefinitionNode) -> OperationKind:
    """The ``OperationKind`` a parsed operation declares."""
    value = definition.operation.value
    if value in ("query", "mutation", "subscription"):
        return cast("OperationKind", value)
    raise SelectionError(f"unsupported operation type {value!r}.")
