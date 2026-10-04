"""Reporting (SPEC 7.5 and 7.6, DESIGN section 3 "Reporting").

Everything the plugin prints about GraphQL sits here: the session header, the
failure section, the sections that other plugins add, the matcher diff of a
failed assertion, the call log, the schema summary, and what the xdist
controller learns from its workers.

The text of a call is built from a ``RecordedCall``, which holds a snapshot and
finished text and nothing live (DESIGN section 7, "Boundary"). Each line is
checked against the secret set of its own call before it is kept, and the whole
text is checked again, so one hostile line is withheld alone and a secret that
spans lines is withheld with the section.

State is bounded. A test's calls live in one :class:`CallTrace`, which holds the
recorder's bounded deque, at most as many live requests as the recorder holds
calls, and the last response. There is one slot for it in the session stash. It
is replaced by the next test's trace and cleared when a test's protocol ends. A
second slot holds the credential ledger, which every test's recorder shares and
which has a bound of its own, so nothing here grows with the number of tests or
calls in a session.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import pytest
from graphql import (
    GraphQLEnumType,
    GraphQLInputObjectType,
    GraphQLInterfaceType,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLUnionType,
)

from pytest_graphql._core.client import ClientConfig, configuration_credentials
from pytest_graphql._core.diagnostics import (
    WITHHELD_TEXT,
    CredentialLedger,
    DiagnosticSnapshot,
    DiagnosticsRecorder,
    OmissionRecord,
    RecordedCall,
    RequestInfo,
    distinct_secret_sets,
    escape_control_characters,
    require_safe_for_all,
    safe_excerpt,
    text_tools,
    withhold_if_gapped,
)
from pytest_graphql._core.errors import DiagnosticRenderError
from pytest_graphql._core.matching import Matcher, RenderOptions
from pytest_graphql._core.matching.render import DEFAULT_MAX_VALUE_BYTES, show_value
from pytest_graphql._core.response import GraphQLResponse, Node
from pytest_graphql._core.schema.source import SchemaSource
from pytest_graphql.plugin.options import SEED_KEY, Settings, settings_of
from pytest_graphql.plugin.session import run_id, worker_id

LOGGER_NAME: Final = "pytest_graphql.calls"

#: The key a worker files its report under in ``workeroutput``.
WORKER_KEY: Final = "pytest_graphql"

_INDENT = "    "
_FAILED_HERE = "   <-- FAILED HERE"

#: The words a skipped-field line uses for each omission reason (SPEC 7.5 shows
#: the first).
_REASON_LABELS: Final[Mapping[str, str]] = {
    "required-argument": "require arguments",
    "deprecated": "deprecated",
    "connection-page-size": "connection with no page-size argument",
    "connection-depth": "connection depth cap",
    "depth": "depth cap",
    "cycle": "cycle",
    "should-include": "excluded by the selection policy",
    "union-member-cap": "union member cap",
}


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else plural or singular + 's'}"


# -- the text of a call -------------------------------------------------------

#: The snapshots whose secret sets a line is checked against. A server can echo
#: a credential that one call sent into the response to another, so a line is
#: checked against every call the same output shows, not only its own.
Guards = Sequence[DiagnosticSnapshot]


def _line(guards: Guards, label: str, payload: str) -> str:
    """``label + payload``, or ``label`` and the withheld notice if it shows one."""
    text = label + payload
    try:
        return require_safe_for_all(guards, text, "failure section")
    except DiagnosticRenderError:
        return label + WITHHELD_TEXT


def _one_line(text: str) -> str:
    """Each run of whitespace becomes one space, so a document fits one line."""
    return " ".join(text.split())


def _variables_text(variables: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(variables),
        ensure_ascii=False,
        default=lambda value: f"<{type(value).__name__}>",
    )


def _skipped_lines(guards: Guards, snapshot: DiagnosticSnapshot) -> list[str]:
    groups: dict[str, list[str]] = {}
    for record in snapshot.omissions:
        groups.setdefault(record.reason, []).append(_omitted_field(record))
    lines = [
        _line(
            guards,
            f"{_INDENT}skipped ({_reason_label(reason)}): ",
            ", ".join(names),
        )
        for reason, names in groups.items()
    ]
    if snapshot.omissions_dropped:
        lines.append(f"{_INDENT}skipped: {snapshot.omissions_dropped} more not listed")
    return lines


def _reason_label(reason: str) -> str:
    return _REASON_LABELS.get(reason, reason.replace("-", " "))


def _omitted_field(record: OmissionRecord) -> str:
    leaf = record.path[-1] if record.path else ""
    return f"{record.parent_type}.{leaf}" if leaf else record.parent_type


def _error_lines(guards: Guards, call: RecordedCall) -> list[str]:
    lines = [f"{_INDENT}errors:"]
    for entry in call.errors:
        # A message with a newline continues under its own entry's indent, so
        # it cannot pass for another entry (DESIGN section 7, "Boundary").
        first, *rest = entry.split("\n")
        text = "\n".join([first, *(f"{_INDENT}    {more}" for more in rest)])
        lines.extend(_line(guards, f"{_INDENT}  - ", text).split("\n"))
    hidden = call.error_count - len(call.errors)
    if hidden > 0:
        lines.append(f"{_INDENT}  - ... {hidden} more not shown")
    return lines


def _status_text(call: RecordedCall) -> str:
    return "-" if call.status_code is None else str(call.status_code)


def render_call(
    number: int,
    call: RecordedCall,
    *,
    failed_here: bool = False,
    reproduce: bool = False,
    guards: Guards | None = None,
) -> list[str]:
    """The lines of one call, in the SPEC 7.5 layout.

    ``failed_here`` adds the marker to the heading. ``reproduce`` adds the
    ``curl`` command, which SPEC 7.5 shows for the failing call only. ``guards``
    are the snapshots every line is checked against, which default to this
    call's own.
    """
    snapshot = call.request
    checks = [snapshot] if guards is None else guards
    name = snapshot.operation or "<anonymous>"
    heading = _line(
        checks,
        f"[{number}] ",
        f"{snapshot.kind} {name}  {_status_text(call)}  {call.duration_ms:.0f}ms",
    )
    lines = [heading + (_FAILED_HERE if failed_here else "")]
    if snapshot.document:
        lines.append(_line(checks, _INDENT, _one_line(snapshot.document)))
    if snapshot.variables:
        lines.append(
            _line(checks, f"{_INDENT}variables: ", _variables_text(snapshot.variables))
        )
    if snapshot.truncated:
        lines.append(
            _line(checks, f"{_INDENT}truncated: ", ", ".join(snapshot.truncated))
        )
    lines.extend(_skipped_lines(checks, snapshot))
    if call.data not in (None, "null"):
        suffix = (
            f"   (truncated, {_plural(call.data_fields, 'field')})"
            if call.data_cut
            else ""
        )
        lines.append(_line(checks, f"{_INDENT}data: ", f"{call.data}{suffix}"))
    if call.errors or call.error_count:
        lines.extend(_error_lines(checks, call))
    if call.failure:
        lines.append(_line(checks, f"{_INDENT}failure: ", call.failure))
    if reproduce and call.curl:
        lines.append(f"{_INDENT}reproduce:")
        lines.append(_line(checks, f"{_INDENT}  ", call.curl))
    return lines


def section_title(held: int, total: int) -> str:
    """The heading of the failure section, which counts the calls it shows."""
    if total > held:
        return f"GraphQL calls (last {held} of {total})"
    return f"GraphQL calls ({held})"


def render_calls(
    calls: Sequence[RecordedCall],
    *,
    total: int | None = None,
    configured: Sequence[DiagnosticSnapshot] = (),
) -> str:
    """The failure section body: every held call, the last one marked and reproduced.

    ``total`` is how many calls the test made, when the recorder dropped the
    oldest, so the numbers stay the real ones. ``configured`` are the snapshots
    of the configuration's own credentials, which every line is checked against
    as it is against the calls.
    """
    held = len(calls)
    first = (held if total is None else total) - held + 1
    guards = distinct_secret_sets([*(call.request for call in calls), *configured])
    blocks = [
        "\n".join(
            render_call(
                first + index,
                call,
                failed_here=index == held - 1,
                reproduce=index == held - 1,
                guards=guards,
            )
        )
        for index, call in enumerate(calls)
    ]
    text = "\n\n".join(blocks)
    try:
        return require_safe_for_all(guards, text, "failure section")
    except DiagnosticRenderError:
        return WITHHELD_TEXT


def log_message(
    call: RecordedCall,
    level: str,
    *,
    others: Sequence[RecordedCall] = (),
    configured: Sequence[DiagnosticSnapshot] = (),
) -> str:
    """One log record for ``call``: a summary line, and at ``full`` its body too.

    The body is the failure section's lines without the heading, the marker or
    the ``curl`` command, which belong to the report of a failure. ``others``
    are the calls the same test made, and ``configured`` the snapshots of the
    configuration's own credentials, whose secrets the record is checked
    against too.
    """
    snapshot = call.request
    guards = distinct_secret_sets(
        [snapshot, *(other.request for other in others), *configured]
    )
    name = snapshot.operation or "<anonymous>"
    summary = _line(
        guards,
        "",
        f"{snapshot.kind} {name} {snapshot.method} {snapshot.url} -> {call.outcome} "
        f"(status {_status_text(call)}, {call.duration_ms:.1f} ms, "
        f"{call.error_count} error(s))",
    )
    if level != "full":
        return summary
    text = "\n".join([summary, *render_call(0, call, guards=guards)[1:]])
    try:
        return require_safe_for_all(guards, text, "log record")
    except DiagnosticRenderError:
        return WITHHELD_TEXT


# -- per-test state -----------------------------------------------------------


class ReportingRecorder(DiagnosticsRecorder):
    """The recorder of one test's clients: bounded, counting, and logging.

    ``total`` counts every call, including the ones the bound dropped, so the
    report can number them. ``sink`` is told of each call once it is recorded.
    """

    __slots__ = ("_sink", "total")

    def __init__(
        self,
        max_calls: int,
        sink: Callable[[RecordedCall, Sequence[RecordedCall]], None] | None = None,
        ledger: CredentialLedger | None = None,
    ) -> None:
        super().__init__(max_calls)
        if ledger is not None:
            self._ledger = ledger
        self._sink = sink
        self.total = 0

    def record(self, call: RecordedCall) -> None:
        super().record(call)
        self.total += 1
        if self._sink is not None:
            self._sink(call, self.calls)


class CallTrace:
    """What one test's client leaves for the report, all of it bounded.

    ``requests`` holds live requests, so a failed assertion can scrub the values
    it prints with the secrets those requests carried. It is never rendered, it
    holds at most as many requests as the recorder holds calls, and it goes with
    the trace when the test's protocol ends.
    """

    __slots__ = (
        "_configured",
        "config",
        "hooks_ran",
        "last_response",
        "nodeid",
        "recorder",
        "requests",
    )

    def __init__(
        self,
        nodeid: str,
        config: ClientConfig,
        sink: Callable[[RecordedCall, Sequence[RecordedCall]], None] | None = None,
        ledger: CredentialLedger | None = None,
    ) -> None:
        self.nodeid = nodeid
        self.config = config
        self.recorder = ReportingRecorder(config.max_recorded_calls, sink, ledger)
        self.requests: deque[RequestInfo] = deque(
            maxlen=max(config.max_recorded_calls, 1)
        )
        self.last_response: GraphQLResponse[Any] | None = None
        #: True once the report-section hook ran for this test.
        self.hooks_ran = False
        self._configured: list[DiagnosticSnapshot] | None = None

    def configured(self) -> list[DiagnosticSnapshot]:
        """The snapshots of the configuration's own credentials, made once."""
        if self._configured is None:
            self._configured = configured_snapshots(self.config)
        return self._configured

    def note_request(self, request: RequestInfo) -> None:
        self.requests.append(request)

    def note_response(self, response: GraphQLResponse[Any]) -> None:
        self.last_response = response


def configured_snapshots(config: ClientConfig) -> list[DiagnosticSnapshot]:
    """The snapshot of the one request that holds the configuration's secrets."""
    return [config_request(config).redacted()]


#: The trace of the test that is running, or none.
CURRENT: Final = pytest.StashKey[CallTrace]()

#: What the calls of the session sent. A test has a recorder of its own, and a
#: server can repeat a value in a later test as well as in a later call.
LEDGER: Final = pytest.StashKey[CredentialLedger]()


def session_ledger(config: pytest.Config) -> CredentialLedger:
    """The one ledger of the session, made on first use and bounded like any."""
    stash = config.stash
    if LEDGER not in stash:
        stash[LEDGER] = CredentialLedger()
    return stash[LEDGER]


def log_sink(
    level: str, config: ClientConfig
) -> Callable[[RecordedCall, Sequence[RecordedCall]], None]:
    """What the recorder tells of each call when ``--gql-log`` is on."""
    logger = logging.getLogger(LOGGER_NAME)
    configured: list[list[DiagnosticSnapshot]] = []

    def sink(call: RecordedCall, held: Sequence[RecordedCall]) -> None:
        if not logger.isEnabledFor(logging.INFO):
            return
        try:
            if not configured:
                configured.append(configured_snapshots(config))
            message = log_message(call, level, others=held, configured=configured[0])
        except Exception:
            # A log record must never replace the result of the call.
            return
        logger.info("%s", message)

    return sink


# -- the schema ---------------------------------------------------------------


@dataclass(frozen=True)
class SchemaFacts:
    """What the session knows about the schema it loaded, in counts and one label."""

    fingerprint: str
    types: int
    objects: int
    interfaces: int
    unions: int
    enums: int
    inputs: int
    scalars: int
    fields: int
    queries: int
    mutations: int
    subscriptions: int
    load_seconds: float | None
    #: How many times this process loaded the schema. The session fixture makes it one.
    loads: int = 1

    @classmethod
    def of(
        cls,
        schema: GraphQLSchema,
        fingerprint: str,
        load_seconds: float | None,
    ) -> SchemaFacts:
        counts = dict.fromkeys(
            ("objects", "interfaces", "unions", "enums", "inputs", "scalars", "fields"),
            0,
        )
        for name, type_ in schema.type_map.items():
            if name.startswith("__"):
                continue
            if isinstance(type_, GraphQLObjectType):
                counts["objects"] += 1
                counts["fields"] += len(type_.fields)
            elif isinstance(type_, GraphQLInterfaceType):
                counts["interfaces"] += 1
                counts["fields"] += len(type_.fields)
            elif isinstance(type_, GraphQLUnionType):
                counts["unions"] += 1
            elif isinstance(type_, GraphQLEnumType):
                counts["enums"] += 1
            elif isinstance(type_, GraphQLInputObjectType):
                counts["inputs"] += 1
            elif isinstance(type_, GraphQLScalarType):
                counts["scalars"] += 1
        return cls(
            fingerprint=fingerprint,
            types=sum(1 for name in schema.type_map if not name.startswith("__")),
            load_seconds=load_seconds,
            queries=_root_size(schema.query_type),
            mutations=_root_size(schema.mutation_type),
            subscriptions=_root_size(schema.subscription_type),
            **counts,
        )

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in _FACT_FIELDS}

    @classmethod
    def from_dict(cls, data: object) -> SchemaFacts | None:
        """The facts a worker sent, or ``None`` when they are not well formed."""
        if not isinstance(data, Mapping):
            return None
        try:
            values: dict[str, Any] = {}
            for name in _FACT_FIELDS:
                value = data[name]
                if name == "fingerprint":
                    values[name] = escape_control_characters(str(value))
                elif name == "load_seconds":
                    values[name] = None if value is None else float(value)
                else:
                    if isinstance(value, bool) or not isinstance(value, int):
                        return None
                    values[name] = value
        except (KeyError, TypeError, ValueError):
            return None
        return cls(**values)


_FACT_FIELDS: Final = (
    "fingerprint",
    "types",
    "objects",
    "interfaces",
    "unions",
    "enums",
    "inputs",
    "scalars",
    "fields",
    "queries",
    "mutations",
    "subscriptions",
    "load_seconds",
    "loads",
)


def _root_size(root: GraphQLObjectType | None) -> int:
    return 0 if root is None else len(root.fields)


#: What this process learned about its schema, set when the schema is ready.
FACTS: Final = pytest.StashKey[SchemaFacts]()
#: How long the ``gql_schema`` fixture took to set up, in seconds.
LOAD_SECONDS: Final = pytest.StashKey[float]()


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(
    fixturedef: Any, request: pytest.FixtureRequest
) -> Generator[None, Any, None]:
    """Time the ``gql_schema`` fixture, whether the default one or an override."""
    if fixturedef.argname != "gql_schema":
        yield
        return
    started = time.perf_counter()
    outcome = yield
    if outcome.excinfo is None:
        request.config.stash[LOAD_SECONDS] = time.perf_counter() - started


def record_schema(
    config: pytest.Config,
    schema: GraphQLSchema,
    source: SchemaSource,
    client_config: ClientConfig,
) -> None:
    """Keep the facts of the schema this process just made ready."""
    fingerprint = safe_excerpt(
        config_request(client_config), str(getattr(source, "fingerprint", ""))
    )
    previous = config.stash.get(FACTS, None)
    facts = SchemaFacts.of(schema, fingerprint, config.stash.get(LOAD_SECONDS, None))
    loads = 1 if previous is None else previous.loads + 1
    config.stash[FACTS] = SchemaFacts(**{**facts.as_dict(), "loads": loads})


# -- the session header and the summary ---------------------------------------


def config_request(
    config: ClientConfig, held: Sequence[tuple[str, str]] = ()
) -> RequestInfo:
    """A request whose secret set holds every credential ``config`` holds.

    The header, the summary and every line checked against the calls of a test
    include text that no call produced or that no single call's request can
    vouch for: an endpoint, a schema label, a server's echo of the schema
    identity. Each is scrubbed and checked with this request, so the secret set
    covers ``headers``, ``schema_headers``, ``cookies`` and ``proxy`` and not
    only the headers. One request holds them all, so a cut that follows the scrub
    has no second secret set left to run, and cannot split a secret that the
    scrub of another set would have removed. ``held`` adds what the calls of the
    test sent, for a text that was not built by one of them.
    """
    return RequestInfo(
        operation=None,
        kind="query",
        document="",
        variables={},
        headers=config.headers,
        url=config.url or "",
        redact_headers=frozenset(config.redact_headers),
        redact_variables=tuple(config.redact_variables),
        redact_values=config.redact_values,
        min_redacted_value_length=config.min_redacted_value_length,
        max_diagnostic_bytes=config.max_diagnostic_bytes,
        max_recorded_errors=config.max_recorded_errors,
        transport_credentials=(*configuration_credentials(config), *held),
    )


def _static_config(settings: Settings) -> ClientConfig:
    """The configuration the static sources give, which is all the header can know."""
    return settings.apply_cli(settings.base_config())


def effective_seed(settings: Settings) -> int:
    """The seed the flag, the environment or the ini option gives, else the default."""
    return int(_static_config(settings).seed)


def _is_controller(config: pytest.Config) -> bool:
    """True in the xdist process that starts the workers and prints the report."""
    return getattr(
        config, "workerinput", None
    ) is None and config.pluginmanager.hasplugin("dsession")


def header_lines(config: pytest.Config) -> list[str]:
    """The lines of the session header.

    The header prints before any test runs, and the schema loads when the first
    test needs it, so the header holds what the sources already say and the
    schema facts come in the summary at the end (DESIGN section 3, "Reporting").
    """
    settings = settings_of(config)
    client_config = _static_config(settings)
    request = config_request(client_config)
    controller = _is_controller(config)

    url = client_config.url
    endpoint = (
        "set by the gql_url fixture"
        if not url
        else safe_excerpt(request, request.redacted().url)
    )
    seed = f"seed {client_config.seed}" + (
        " (chosen by random)" if settings.seed_is_random else ""
    )
    if controller:
        shared = config.getoption("testrunuid", default=None)
        run = (
            f"run id {safe_excerpt(request, str(shared))}"
            if isinstance(shared, str) and shared
            else "run id assigned by xdist"
        )
        second = (
            "schema loaded once per worker, and each worker's schema is "
            "listed at the end of the run"
        )
    else:
        run = f"run id {run_id(config)}"
        second = (
            "schema loads when the first test needs it, and is listed at the end "
            "of the run"
        )
    return [f"graphql: endpoint {endpoint}, {seed}, {run}", f"graphql: {second}"]


@dataclass(frozen=True)
class WorkerReport:
    """What one xdist worker tells the controller when it finishes."""

    worker: str
    run_id: str
    seed: int
    schema: SchemaFacts | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker": self.worker,
            "run_id": self.run_id,
            "seed": self.seed,
            "schema": None if self.schema is None else self.schema.as_dict(),
        }

    @classmethod
    def from_dict(cls, data: object) -> WorkerReport | None:
        if not isinstance(data, Mapping):
            return None
        worker, run, seed = data.get("worker"), data.get("run_id"), data.get("seed")
        if not isinstance(worker, str) or not isinstance(run, str):
            return None
        if isinstance(seed, bool) or not isinstance(seed, int):
            return None
        return cls(
            worker=escape_control_characters(worker),
            run_id=escape_control_characters(run),
            seed=seed,
            schema=SchemaFacts.from_dict(data.get("schema")),
        )


#: The reports the controller collected, one per worker that finished.
NODES: Final = pytest.StashKey[list[WorkerReport]]()


def schema_line(worker: str, facts: SchemaFacts) -> str:
    load = (
        "load time not measured"
        if facts.load_seconds is None
        else f"loaded in {facts.load_seconds:.3f}s"
    )
    return (
        f"schema {worker}: {facts.fingerprint}, {_plural(facts.types, 'type')}, "
        f"{_plural(facts.queries, 'query', 'queries')}, "
        f"{_plural(facts.mutations, 'mutation')}, "
        f"{_plural(facts.subscriptions, 'subscription')}, {load}"
    )


def stats_lines(worker: str, facts: SchemaFacts) -> list[str]:
    """The detail block of ``--gql-show-schema-stats``."""
    load = (
        "not measured" if facts.load_seconds is None else f"{facts.load_seconds:.3f}s"
    )
    return [
        f"schema stats {worker}:",
        f"  fingerprint: {facts.fingerprint}",
        f"  types: {facts.types} ({_plural(facts.objects, 'object')}, "
        f"{_plural(facts.interfaces, 'interface')}, {_plural(facts.unions, 'union')}, "
        f"{_plural(facts.enums, 'enum')}, "
        f"{_plural(facts.inputs, 'input object')}, {_plural(facts.scalars, 'scalar')})",
        f"  fields: {facts.fields}",
        f"  operations: {_plural(facts.queries, 'query', 'queries')}, "
        f"{_plural(facts.mutations, 'mutation')}, "
        f"{_plural(facts.subscriptions, 'subscription')}",
        f"  load time: {load}",
    ]


def summary_lines(config: pytest.Config) -> list[str]:
    """The lines of the GraphQL section at the end of the run, or none."""
    settings = settings_of(config)
    own = config.stash.get(FACTS, None)
    reports: list[WorkerReport] | None = config.stash.get(NODES, None)
    nodes = sorted(reports or [], key=lambda node: node.worker)
    lines: list[str] = []
    if own is not None:
        lines.append(schema_line(worker_id(config), own))
        if settings.show_schema_stats:
            lines.extend(stats_lines(worker_id(config), own))
    loaded = [node for node in nodes if node.schema is not None]
    if loaded:
        for node in loaded:
            assert node.schema is not None
            lines.append(schema_line(node.worker, node.schema))
        if settings.show_schema_stats:
            for node in loaded:
                assert node.schema is not None
                lines.extend(stats_lines(node.worker, node.schema))
        once = all(
            node.schema is not None and node.schema.loads == 1 for node in loaded
        )
        lines.append(
            f"schema loaded by {len(loaded)} of {_plural(len(nodes), 'worker')}, "
            + ("once each" if once else "more than once on a worker")
        )
        runs = {node.run_id for node in nodes}
        seeds = {node.seed for node in nodes}
        if len(runs) == 1:
            lines.append(
                f"run id {next(iter(runs))}, shared by {_plural(len(nodes), 'worker')}"
            )
        else:
            lines.append("run ids differ between workers")
        if len(seeds) == 1:
            lines.append(
                f"seed {next(iter(seeds))}, the same on {_plural(len(nodes), 'worker')}"
            )
        else:
            lines.append("seeds differ between workers")
    return lines


def pytest_report_header(config: pytest.Config) -> list[str]:
    return header_lines(config)


def pytest_terminal_summary(terminalreporter: Any) -> None:
    lines = summary_lines(terminalreporter.config)
    if not lines:
        return
    terminalreporter.write_sep("=", "GraphQL")
    for line in lines:
        terminalreporter.write_line(line)


# -- xdist ----------------------------------------------------------------------


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: Any) -> None:
    """Give every worker the seed that ``--gql-seed=random`` chose on the controller."""
    settings = settings_of(node.config)
    if settings.seed_is_random:
        node.workerinput[SEED_KEY] = int(settings.cli_values["seed"])


def pytest_sessionfinish(session: pytest.Session) -> None:
    """A worker files its report where xdist sends it to the controller."""
    config = session.config
    output = getattr(config, "workeroutput", None)
    if not isinstance(output, dict):
        return
    output[WORKER_KEY] = WorkerReport(
        worker=worker_id(config),
        run_id=run_id(config),
        seed=effective_seed(settings_of(config)),
        schema=config.stash.get(FACTS, None),
    ).as_dict()


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: object) -> None:  # noqa: ARG001
    output = getattr(node, "workeroutput", None)
    if not isinstance(output, Mapping):
        return
    report = WorkerReport.from_dict(output.get(WORKER_KEY))
    if report is None:
        return
    reports = node.config.stash.get(NODES, None)
    if reports is None:
        reports = node.config.stash[NODES] = []
    reports.append(report)


# -- failure sections -----------------------------------------------------------


def _hook_name(implementation: Any) -> str:
    plugin_name = getattr(implementation.plugin, "__name__", None)
    name = plugin_name if isinstance(plugin_name, str) else implementation.plugin_name
    return escape_control_characters(str(name))


def hook_sections(
    item: pytest.Item, trace: CallTrace, calls: Sequence[RecordedCall]
) -> list[tuple[str, str]]:
    """What each ``pytest_graphql_report_section`` implementation adds, in hook order.

    Every implementation runs, so no section hides another, and each non-``None``
    result gets a heading of its own. The hook gets the last response the test
    received. A test with none has nothing to report on, and the hook is not
    called. It runs once for each test, at its first failed phase. Text a hook
    returns is escaped and checked against the secret set of every call and of
    the configuration before it is shown, because a hook sees a response whose
    messages are as the server sent them. Once the session's credentials are no
    longer all known, nothing can vouch for that text, and it is withheld.
    """
    response = trace.last_response
    if response is None or trace.hooks_ran:
        return []
    # A test can fail in more than one phase, and pytest makes a report for each.
    # The hook has side effects a project chose, so it runs once for the test,
    # at its first failed phase. Each failed phase still shows its calls.
    trace.hooks_ran = True
    config = item.config
    arguments = {"response": response, "item": item, "config": config}
    snapshots = [call.request for call in calls]
    snapshots.append(response.request)
    snapshots.extend(trace.configured())
    held = config_request(trace.config, trace.recorder._held())
    sections: list[tuple[str, str]] = []
    for implementation in reversed(
        config.hook.pytest_graphql_report_section.get_hookimpls()
    ):
        # A wrapper cannot return a section, and calling one makes a generator.
        if getattr(implementation, "wrapper", False) or implementation.hookwrapper:
            continue
        given = {
            name: value
            for name, value in arguments.items()
            if name in implementation.argnames
        }
        try:
            result = implementation.function(**given)
        except Exception as failure:
            text = f"the hook raised {type(failure).__name__}"
        else:
            if result is None:
                continue
            if not isinstance(result, str):
                text = f"the hook returned {type(result).__name__}, not a string"
            else:
                text = withhold_if_gapped(held, escape_control_characters(result))
        try:
            text = require_safe_for_all(snapshots, text, "report section")
        except DiagnosticRenderError:
            text = WITHHELD_TEXT
        sections.append((f"GraphQL report: {_hook_name(implementation)}", text))
    return sections


def failure_sections(item: pytest.Item, trace: CallTrace) -> list[tuple[str, str]]:
    """The report sections of one failed test, or none when it made no call."""
    calls = trace.recorder.calls
    if not calls:
        return []
    total = trace.recorder.total
    sections = [
        (
            section_title(len(calls), total),
            render_calls(calls, total=total, configured=trace.configured()),
        )
    ]
    sections.extend(hook_sections(item, trace, calls))
    return sections


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item) -> Generator[None, Any, None]:
    outcome = yield
    report = outcome.get_result()
    if not report.failed:
        return
    trace = item.config.stash.get(CURRENT, None)
    if trace is None or trace.nodeid != item.nodeid:
        return
    try:
        report.sections.extend(failure_sections(item, trace))
    except Exception as failure:
        # A fault in the report must not hide the failure it reports on.
        report.sections.append(
            ("GraphQL calls", f"the report failed with {type(failure).__name__}")
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, Any, None]:
    """Release the test's trace once all three of its reports are made."""
    try:
        yield
    finally:
        if CURRENT in item.config.stash:
            del item.config.stash[CURRENT]


# -- the matcher diff -----------------------------------------------------------


def _render_options(
    config: pytest.Config, trace: CallTrace | None
) -> tuple[RenderOptions, list[DiagnosticSnapshot]]:
    """How to print a diff: the redaction and scrub of the calls the test made."""
    if trace is None:
        base = settings_of(config).base_config()
        return (
            RenderOptions(
                redact_paths=tuple(base.redact_variables),
                max_value_bytes=min(DEFAULT_MAX_VALUE_BYTES, base.max_diagnostic_bytes),
            ),
            [],
        )
    scrubs = [
        text_tools(request)[0]
        for request in [
            *trace.requests,
            config_request(trace.config, trace.recorder._held()),
        ]
    ]

    def scrub(text: str) -> str:
        for each in scrubs:
            text = each(text)
        return escape_control_characters(text)

    return (
        RenderOptions(
            scrub=scrub,
            redact_paths=tuple(trace.config.redact_variables),
            max_value_bytes=min(
                DEFAULT_MAX_VALUE_BYTES, trace.config.max_diagnostic_bytes
            ),
        ),
        [*(call.request for call in trace.recorder.calls), *trace.configured()],
    )


def pytest_assertrepr_compare(
    config: pytest.Config, op: str, left: object, right: object
) -> list[str] | None:
    """A failed ``actual == matcher`` shows the matcher diff (SPEC 7.5).

    pytest indents every line after the first by two spaces, and the diff
    carries those two spaces already, so they are dropped here and the block
    shows as SPEC 7.5 prints it. The first line names the two sides: a matcher
    and a ``Node`` by their ``repr``, which shows no value, and any other value
    as the renderer shows a value. Once the session's credentials are no longer
    all known, the diff is one withheld notice, because a matcher can print text
    of its own.
    """
    if op != "==":
        return None
    if isinstance(left, Matcher) and not isinstance(right, Matcher):
        matcher, actual = left, right
    elif isinstance(right, Matcher) and not isinstance(left, Matcher):
        matcher, actual = right, left
    else:
        return None
    trace = config.stash.get(CURRENT, None)
    options, snapshots = _render_options(config, trace)
    try:
        lines = matcher.explain(actual, options)
        if not lines:
            return None
        text = "\n".join(lines)
        if trace is not None:
            # A matcher a project wrote can print text the renderer never scrubbed.
            held = config_request(trace.config, trace.recorder._held())
            shown = withhold_if_gapped(held, text)
            if shown != text:
                return [shown]
        require_safe_for_all(snapshots, text, "matcher diff")
        summary = f"{_operand(left, options)} == {_operand(right, options)}"
        require_safe_for_all(snapshots, summary, "matcher diff")
    except DiagnosticRenderError:
        return [WITHHELD_TEXT]
    except Exception:
        # Any other fault is the diff's own, and pytest's report is a safe fallback.
        return None
    return [summary, *(line[2:] if line.startswith("  ") else line for line in lines)]


def _operand(value: object, options: RenderOptions) -> str:
    if isinstance(value, Matcher):
        return repr(value)
    if isinstance(value, Node):
        return repr(value)
    return show_value(value, options)


# -- the log --------------------------------------------------------------------

#: The level the call logger had before ``--gql-log`` raised it.
_LOGGER_LEVEL: Final = pytest.StashKey[int]()


def configure(config: pytest.Config) -> None:
    """With ``--gql-log``, let the call logger emit records of every call."""
    if not settings_of(config).log:
        return
    logger = logging.getLogger(LOGGER_NAME)
    config.stash[_LOGGER_LEVEL] = logger.level
    logger.setLevel(logging.INFO)


def pytest_unconfigure(config: pytest.Config) -> None:
    previous = config.stash.get(_LOGGER_LEVEL, None)
    if previous is not None:
        logging.getLogger(LOGGER_NAME).setLevel(previous)
        del config.stash[_LOGGER_LEVEL]
