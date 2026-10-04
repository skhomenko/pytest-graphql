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
    """Everything a client reads that is data rather than an object.

    The split is B4's rule, restated in the "Constructor and configuration
    split" section of ``docs/reference/DESIGN_DECISIONS.md``: this object
    holds data, and the constructor holds objects (``transport``, ``schema``,
    ``scalars``, ``fake_context``, ``middleware``). ``seed`` lives here and never
    on the client constructor.

    ``include_deprecated`` defaults to ``False``, not to SPEC 3.10's ``True``:
    C1 turned it off and the design document states the current rule, so the
    default here matches ``SelectionPolicy``'s. A config that disagreed with
    the policy would silently re-enable deprecated fields for every call that
    did not override it.
    """

    url: str | None = None
    #: The fields that carry a credential (``headers``, ``schema_headers``,
    #: ``cookies`` and ``proxy``) stay out of ``repr()``. A configuration is
    #: plain data a traceback or an assertion report prints whole, and no
    #: redaction stage runs over it (C2, C16).
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    #: C4: schema loading uses this alone, so a function-scoped auth fixture
    #: cannot change the session-scoped schema. ``None`` means "use
    #: ``headers``", which is the documented default.
    schema_headers: Mapping[str, str] | None = field(default=None, repr=False)
    cookies: Mapping[str, str] = field(default_factory=dict, repr=False)
    cookie_scope: CookieScope = "none"
    #: "Operational limits": a float applied to all four phases, or a
    #: ``Timeout(connect, read, write, pool)``. :meth:`call_timeout` turns
    #: either form into the scalar ceiling ``Transport.send()`` requires.
    timeout: float | httpx.Timeout = DEFAULT_TIMEOUT_SECONDS
    retries: int = 2
    #: ``httpx`` semantics: ``True``, a CA bundle path, or an ``SSLContext``.
    verify: bool | str | ssl.SSLContext = True
    #: Ambient proxy, netrc and ``SSLKEYLOGFILE`` handling stays off unless a
    #: project opts in, so a run cannot silently route through an ambient
    #: proxy. An explicit :attr:`proxy` is unaffected by this flag.
    trust_env: bool = False
    proxy: httpx.Proxy | str | None = field(default=None, repr=False)
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    follow_redirects: bool = False
    http2: bool = False

    max_depth: int = 3
    cycle_policy: CyclePolicy = "shallow"
    per_type_depth_cap: Mapping[str, int] = field(default_factory=dict)
    include_deprecated: bool = False
    max_fields: int = 2000
    exclude: Sequence[str] = ()
    relay_aware: bool = True

    validate: bool = True
    raise_on_error: bool = True
    raise_on_partial: bool = True

    seed: int = 0
    schema_cache_dir: str | None = None
    schema_cache_ttl: int = 0

    redact_headers: Sequence[str] = tuple(sorted(DEFAULT_REDACT_HEADERS))
    redact_variables: Sequence[str] = DEFAULT_REDACT_VARIABLES
    redact_values: bool = True
    min_redacted_value_length: int = DEFAULT_MIN_REDACTED_VALUE_LENGTH
    max_diagnostic_bytes: int = DEFAULT_MAX_DIAGNOSTIC_BYTES
    max_recorded_errors: int = DEFAULT_MAX_RECORDED_ERRORS
    max_recorded_calls: int = DEFAULT_MAX_RECORDED_CALLS

    def call_timeout(self, override: float | None = None) -> float:
        """The scalar ceiling ``Transport.send()`` takes for one call.

        ``Transport.send()``'s per-call ``timeout`` is mandatory and scalar
        (SPEC 5.6), and the transport applies it as a ceiling on each of its
        own four phases rather than as a replacement of them. So the scalar
        this returns for a phase-specific :attr:`timeout` is the smallest one
        that clamps no phase the project configured: the largest configured
        phase, or no bound at all when a phase was deliberately left
        unbounded. A stored ``Timeout`` therefore governs the request it
        describes, exactly as "Operational limits" says it does.

        An explicit per-call ``timeout`` option is the caller's own budget
        and is used as given, which is how a call tightens a configured
        phase. It can only tighten: a value looser than a configured phase
        leaves that phase where the project put it.

        Both inputs are held to one domain before any I/O: a number greater
        than zero, with ``math.inf`` (or a ``None`` phase) for no limit.
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
        """The policy this configuration describes, before per-call overrides."""
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
    """One logical client: a transport, a schema, and one identity.

    Ownership follows 9.1 exactly. ``owns_transport`` is a constructor
    argument, never a type check, and it is true exactly when closing this
    client closes the transport it holds. The cleanup list is the only close
    mechanism: when no list is supplied the flag builds it, and when a list is
    supplied it is already complete and this constructor never appends to it.
    A client never calls ``derive()`` for itself; a clone does, which is why a
    clone owns a transport only when it derived one.
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
        if self._closed:
            return
        self._closed = True
        errors = _close_all(self._cleanup)
        if errors:
            _report(errors, errors[-1])

    def __enter__(self) -> GraphQLClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- read-only state ------------------------------------------------------

    @property
    def schema(self) -> GraphQLSchema:
        return self._schema

    @property
    def expect(self) -> ExpectNamespace:
        """``gql.expect.Type(**fields)``: matchers checked against the schema."""
        if self._expect is None:
            self._expect = ExpectNamespace(self._schema)
        return self._expect

    @property
    def fake(self) -> FakeNamespace:
        """``gql.fake.Type(**overrides)``: seeded input payloads (SPEC 3.7)."""
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
        """The custom scalars this client decodes, serializes and fakes."""
        return self._scalars

    @property
    def config(self) -> ClientConfig:
        return self._config

    @property
    def transport(self) -> Transport:
        return self._transport

    @property
    def recorder(self) -> DiagnosticsRecorder:
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
        """A clone carrying these headers on top of this client's (B21, C4)."""
        return self._clone(
            auth=self._auth,
            headers=merge_headers(self._headers, headers, named),
        )

    def with_auth(self, auth: Auth) -> GraphQLClient:
        """A clone using ``auth`` in place of this client's."""
        return self._clone(auth=auth, headers=self._headers)

    def as_(self, auth: Auth | str) -> GraphQLClient:
        """A clone under another identity. A bare string is a bearer token."""
        return self.with_auth(resolve_auth(auth))

    def anonymous(self) -> GraphQLClient:
        """A clone with no auth at all."""
        return self._clone(auth=None, headers=self._headers)

    # -- calling operations (SPEC 5.2) ----------------------------------------

    def query(self, name: str, /, **variables: Any) -> Any:
        return self._call("query", name, variables)

    def mutation(self, name: str, /, **variables: Any) -> Any:
        return self._call("mutation", name, variables)

    def execute(
        self,
        document: str,
        variables: Mapping[str, Any] | None = None,
        *,
        operation_name: str | None = None,
        **options: Any,
    ) -> GraphQLResponse[Any]:
        """Send a raw document, and always return the response itself (C5).

        Convenience unwrapping stays on ``query()`` and ``mutation()``, which
        have exactly one known top-level field. A raw document may select any
        number, so ``GraphQLResponse.unwrap()`` is the explicit opt-in here.
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
        """Assert that the block raises a ``GraphQLExecutionError``.

        Every supplied filter must match at least one of the response's
        errors, and ``count`` is the exact number of errors it returned.
        See :mod:`pytest_graphql._core.expect_error`.
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
        """Run the query ``name`` until ``until`` holds, or raise ``WaitTimeoutError``.

        Queries only. See :mod:`pytest_graphql._core.polling`.
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
    for node in definition.variable_definitions:
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
    """Build a client outside pytest, owning every resource it creates (C28).

    This is SPEC 3.11's standalone entry point, so it is the standalone
    spelling of the constructor and ``ClientConfig`` together. The named
    parameters above are the objects the constructor takes; every other
    keyword is a ``ClientConfig`` field, which is what makes the documented
    ``build_client(url=..., headers=..., max_depth=3)`` shape work. Data
    options land on the configuration rather than on the client, so C4's
    default schema identity applies to them: ``headers`` given here is what
    introspection sends, which an authenticated endpoint requires.

    The client transport is derived with the default flag, so no transfer
    statement is left to get wrong and the cleanup list is the single owner.
    The handler runs only on an exception, and an exception anywhere up to the
    return means the client was never delivered, so closing everything there
    is always correct and no transfer flag is needed to tell the two cases
    apart. ``probe.close()`` stays on the success path; the later sweep closes
    nothing a second time, because every wrapper is idempotent.

    ``build_client(transport=...)`` creates no pool and closes nothing: the
    caller owns what the caller supplied.
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
