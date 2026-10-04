"""The option table: ini options, environment variables and CLI flags.

One table, :data:`OPTIONS`, describes every setting the plugin reads. The ini
registration, the environment variable names, the flags, the parsing and the
error messages are all generated from it, so the sources cannot drift apart
(DESIGN section 2, "Settings precedence").

Per setting, from the highest source to the lowest, the order is: per-call
argument, CLI flag, fixture, environment variable, ini option, built-in
default. This module owns the three sources it can read without a running
session:

- ``cli_values``: the fields a flag set. They sit above every fixture, so
  :meth:`Settings.apply_cli` is applied after the ``gql_config`` fixture.
- ``file_values``: the fields an environment variable or an ini option set,
  environment first. They are the default content of ``gql_config``.
- Everything else is the built-in default, which is whatever ``ClientConfig()``
  holds, so a default is written once.

Every source is checked when the session is configured, whether or not a
higher source hides it, so a broken ini file or a stale environment variable is
reported instead of waiting to take effect. A refusal is a ``pytest.UsageError``
that names the setting and the source. It never shows the value, because a
header line, a URL or a path can hold a credential and a message is an output.

A value that is empty or only whitespace counts as not set, in every source.
That keeps ``PYTEST_GQL_URL=`` and an empty ini line from overriding a lower
source with nothing.
"""

from __future__ import annotations

import dataclasses
import os
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal

import pytest

from pytest_graphql._core.client import ClientConfig
from pytest_graphql._core.diagnostics import DEFAULT_REDACT_HEADERS
from pytest_graphql._core.selection.policy import SelectionPolicy
from pytest_graphql._core.transport.httpx_transport import checked_seconds

ENV_PREFIX: Final = "PYTEST_GQL_"

#: The ``workerinput`` key the xdist controller files its chosen random seed
#: under. The reporting module writes it, and this module reads it.
SEED_KEY: Final = "pytest_graphql_seed"

#: The ini value that names the built-in source. SPEC 7.2 gives it as the
#: default of ``gql_schema_source``. The top-level package does not export
#: ``IntrospectionSource``, so the name is a label and is never imported.
DEFAULT_SCHEMA_SOURCE: Final = "pytest_graphql.IntrospectionSource"

_TRUE: Final = frozenset({"1", "true", "yes", "y", "on", "t"})
_FALSE: Final = frozenset({"0", "false", "no", "n", "off", "f"})
_CYCLE_POLICIES: Final = ("stop", "shallow", "id_only")
_LOG_LEVELS: Final = ("summary", "full")

#: RFC 9110 token characters, which is what a header name is made of.
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_GRAPHQL_NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")
_DOTTED_PATH = re.compile(r"[_A-Za-z][_0-9A-Za-z]*(\.[_A-Za-z][_0-9A-Za-z]*)+")


class _InvalidError(Exception):
    """A value was refused. The message says what is accepted, never the value."""


class _RandomSeed:
    """``--gql-seed=random``, before a seed has been chosen."""


# -- parsers ------------------------------------------------------------------
#
# A parser takes the text of one source and the directory a relative path
# resolves against. A line list reaches it as one text, joined by newlines, so
# every source hands over the same shape.


Parser = Callable[[str, Path], Any]


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _text(text: str, _base: Path) -> str:
    return text.strip()


def _url(text: str, _base: Path) -> str:
    value = text.strip()
    if any(char.isspace() for char in value):
        raise _InvalidError("a URL without spaces or line breaks")
    return value


def _whole(minimum: int) -> Parser:
    def parse(text: str, _base: Path) -> int:
        try:
            value = int(text.strip(), 10)
        except ValueError:
            raise _InvalidError(f"a whole number of at least {minimum}") from None
        if value < minimum:
            raise _InvalidError(f"a whole number of at least {minimum}")
        return value

    return parse


def _seed(text: str, _base: Path) -> int:
    try:
        return int(text.strip(), 10)
    except ValueError:
        raise _InvalidError("a whole number") from None


def _cli_seed(text: str, base: Path) -> int | _RandomSeed:
    if text.strip() == "random":
        return _RandomSeed()
    try:
        return _seed(text, base)
    except _InvalidError:
        raise _InvalidError("a whole number or the word random") from None


def _seconds(text: str, _base: Path) -> float:
    expected = "a number of seconds greater than zero, or inf for no limit"
    try:
        value = float(text.strip())
        return checked_seconds(value, source="a timeout")
    except (TypeError, ValueError):
        raise _InvalidError(expected) from None


def _boolean(text: str, _base: Path) -> bool:
    word = text.strip().lower()
    if word in _TRUE:
        return True
    if word in _FALSE:
        return False
    raise _InvalidError("true or false")


def _verify(text: str, base: Path) -> bool | str:
    """``true``, ``false``, or the path of an existing CA bundle file."""
    word = text.strip()
    if word.lower() in _TRUE:
        return True
    if word.lower() in _FALSE:
        return False
    path = Path(word).expanduser()
    if not path.is_absolute():
        path = base / path
    if not path.is_file():
        raise _InvalidError("true, false, or the path of an existing CA bundle file")
    return str(path)


def _choice(*names: str) -> Parser:
    def parse(text: str, _base: Path) -> str:
        word = text.strip()
        if word not in names:
            raise _InvalidError("one of " + ", ".join(names))
        return word

    return parse


def _headers(text: str, _base: Path) -> dict[str, str]:
    """``Name: value`` lines. A later line replaces an earlier one of that name."""
    found: dict[str, str] = {}
    for index, line in enumerate(_lines(text), start=1):
        name, separator, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if not separator or not _HEADER_NAME.fullmatch(name):
            raise _InvalidError(f"lines of the form Name: value (line {index} is not)")
        if any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in value):
            raise _InvalidError(
                f"header values without control characters (line {index})"
            )
        found[name] = value
    return found


def _depth_caps(text: str, _base: Path) -> dict[str, int]:
    found: dict[str, int] = {}
    for index, line in enumerate(_lines(text), start=1):
        name, separator, number = line.partition("=")
        name, number = name.strip(), number.strip()
        try:
            cap = int(number, 10)
        except ValueError:
            cap = -1
        if not separator or not _GRAPHQL_NAME.fullmatch(name) or cap < 0:
            raise _InvalidError(f"lines of the form Type=N (line {index} is not)")
        found[name] = cap
    return found


def _patterns(text: str, _base: Path) -> tuple[str, ...]:
    patterns = tuple(_lines(text))
    try:
        SelectionPolicy(exclude=patterns)
    except ValueError:
        raise _InvalidError(
            "patterns of the form Type.field, *.field or Type.*"
        ) from None
    return patterns


def _header_names(text: str, _base: Path) -> tuple[str, ...]:
    """The default names, then the listed ones, each name once.

    The option extends the default list, so a project that adds one header
    keeps ``authorization`` and ``cookie`` redacted. Names are one per line, and
    a comma also separates two of them, because a header name cannot hold one.
    A name already present under any capitalization is not added again. There
    is no spelling that removes a default: a project that needs a smaller list
    builds the ``ClientConfig`` itself.
    """
    listed = [
        name.strip()
        for line in _lines(text)
        for name in line.split(",")
        if name.strip()
    ]
    if not all(_HEADER_NAME.fullmatch(name) for name in listed):
        raise _InvalidError("header names, one per line")
    names = sorted(DEFAULT_REDACT_HEADERS)
    seen = set(names)
    for name in listed:
        if name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return tuple(names)


def _schema_source(text: str, _base: Path) -> str | None:
    path = text.strip()
    if path == DEFAULT_SCHEMA_SOURCE:
        return None
    if not _DOTTED_PATH.fullmatch(path):
        raise _InvalidError("a dotted path such as package.module.attribute")
    return path


# -- the table ----------------------------------------------------------------


@dataclass(frozen=True)
class Flag:
    """The CLI spelling of one option."""

    name: str
    help: str
    metavar: str | None = None
    #: A flag that takes no value. Setting it gives the option the value
    #: ``set_to``, which is how ``--gql-no-validate`` turns validation off.
    set_to: bool | None = None
    #: Replaces the option's own parser for the flag, where the flag accepts
    #: more than the other sources do.
    parse: Parser | None = None

    @property
    def dest(self) -> str:
        return self.name[2:].replace("-", "_")


@dataclass(frozen=True)
class Option:
    """One setting: its ini name, the field it sets, and how it is read."""

    ini: str
    #: The ``ClientConfig`` field, or ``None`` for a setting that is not
    #: configuration data and so is read straight from ``Settings``.
    field: str | None
    kind: Literal["string", "linelist"]
    parse: Parser
    help: str
    flag: Flag | None = None

    @property
    def env(self) -> str:
        """``gql_max_depth`` becomes ``PYTEST_GQL_MAX_DEPTH``."""
        return ENV_PREFIX + self.ini.removeprefix("gql_").upper()

    @property
    def key(self) -> str:
        return self.field if self.field is not None else self.ini


OPTIONS: Final[tuple[Option, ...]] = (
    Option(
        "gql_url",
        "url",
        "string",
        _url,
        "GraphQL endpoint.",
        Flag("--gql-url", "GraphQL endpoint. Overrides the gql_url fixture.", "URL"),
    ),
    Option(
        "gql_headers",
        "headers",
        "linelist",
        _headers,
        "Headers sent on every request, one 'Name: value' per line. They also "
        "load the schema.",
    ),
    Option(
        "gql_timeout",
        "timeout",
        "string",
        _seconds,
        "Request timeout in seconds.",
        Flag("--gql-timeout", "Override the timeout for one run.", "SECONDS"),
    ),
    Option("gql_retries", "retries", "string", _whole(0), "Retries after a failure."),
    Option(
        "gql_verify",
        "verify",
        "string",
        _verify,
        "TLS verification: true, false, or the path of a CA bundle file.",
    ),
    Option(
        "gql_max_depth",
        "max_depth",
        "string",
        _whole(1),
        "Auto-selection depth cap.",
        Flag("--gql-max-depth", "Override the depth cap for one run.", "N"),
    ),
    Option(
        "gql_cycle_policy",
        "cycle_policy",
        "string",
        _choice(*_CYCLE_POLICIES),
        "What auto-selection does at a cycle: stop, shallow or id_only.",
    ),
    Option(
        "gql_per_type_depth_cap",
        "per_type_depth_cap",
        "linelist",
        _depth_caps,
        "A depth cap for one type, one 'Type=N' per line.",
    ),
    Option(
        "gql_include_deprecated",
        "include_deprecated",
        "string",
        _boolean,
        "Whether auto-selection includes deprecated fields.",
    ),
    Option(
        "gql_max_fields",
        "max_fields",
        "string",
        _whole(1),
        "The most fields one auto-selected document may hold.",
    ),
    Option(
        "gql_exclude",
        "exclude",
        "linelist",
        _patterns,
        "Fields auto-selection leaves out: Type.field, *.field or Type.*.",
    ),
    Option(
        "gql_relay_aware",
        "relay_aware",
        "string",
        _boolean,
        "Whether auto-selection treats Relay connections specially.",
    ),
    Option(
        "gql_validate",
        "validate",
        "string",
        _boolean,
        "Whether documents are validated locally before they are sent.",
        Flag(
            "--gql-no-validate",
            "Disable local validation globally.",
            set_to=False,
        ),
    ),
    Option(
        "gql_seed",
        "seed",
        "string",
        _seed,
        "The factory seed.",
        Flag(
            "--gql-seed",
            "Override the factory seed. 'random' chooses one and records it.",
            "N|random",
            parse=_cli_seed,
        ),
    ),
    Option(
        "gql_redact_headers",
        "redact_headers",
        "linelist",
        _header_names,
        "Header names to redact, besides authorization, cookie, x-api-key and "
        "proxy-authorization, which are always redacted.",
    ),
    Option(
        "gql_schema_source",
        None,
        "string",
        _schema_source,
        "Dotted path of a SchemaSource instance. Unset means introspection.",
    ),
)

#: Flags with no setting behind them. They only choose what the reporting module prints.
LOG_FLAG: Final = "--gql-log"
LOG_LEVEL_FLAG: Final = "--gql-log-level"
SCHEMA_STATS_FLAG: Final = "--gql-show-schema-stats"


#: ``--gql-log-level`` has no ini option or field. It is described as an option
#: only so a refusal reads like every other.
_LOG_LEVEL_SETTING: Final = Option(
    "gql_log_level", None, "string", _choice(*_LOG_LEVELS), ""
)


def register(parser: pytest.Parser) -> None:
    """Register every ini option and every flag in the table."""
    group = parser.getgroup("graphql", "GraphQL testing (pytest-graphql)")
    for option in OPTIONS:
        parser.addini(
            option.ini,
            f"{option.help} Environment variable: {option.env}.",
            type=option.kind,
            default=[] if option.kind == "linelist" else "",
        )
        flag = option.flag
        if flag is None:
            continue
        if flag.set_to is not None:
            group.addoption(
                flag.name, dest=flag.dest, action="store_true", help=flag.help
            )
        else:
            group.addoption(
                flag.name,
                dest=flag.dest,
                default=None,
                metavar=flag.metavar,
                help=flag.help,
            )
    group.addoption(
        LOG_FLAG,
        dest="gql_log",
        action="store_true",
        help="Log every request and response, not only failures.",
    )
    group.addoption(
        LOG_LEVEL_FLAG,
        dest="gql_log_level",
        default=None,
        metavar="LEVEL",
        help="How much to log: summary or full. Default summary.",
    )
    group.addoption(
        SCHEMA_STATS_FLAG,
        dest="gql_show_schema_stats",
        action="store_true",
        help="Print schema size, operation counts and load time.",
    )


# -- sources ------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """Where one value came from, in the words an error message uses."""

    kind: Literal["cli", "env", "ini"]
    name: str

    def describe(self) -> str:
        if self.kind == "cli":
            return f"the command line option {self.name}"
        if self.kind == "env":
            return f"the environment variable {self.name}"
        return f"the ini option {self.name}"


def _refuse(option: Option, source: Source, expected: str) -> pytest.UsageError:
    return pytest.UsageError(
        f"pytest-graphql: invalid value for {option.ini}, set by "
        f"{source.describe()}. Expected {expected}. The value is not shown."
    )


def _parse(option: Option, source: Source, parse: Parser, text: str, base: Path) -> Any:
    try:
        return parse(text, base)
    except _InvalidError as invalid:
        raise _refuse(option, source, str(invalid)) from None


# -- settings -----------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class Settings:
    """What the three static sources say, checked, before any fixture runs.

    A value here can be a header or a URL, so :meth:`__repr__` shows field
    names and no values.
    """

    #: ``ClientConfig`` fields an environment variable or an ini option set.
    file_values: Mapping[str, Any]
    #: ``ClientConfig`` fields a flag set. They outrank every fixture.
    cli_values: Mapping[str, Any]
    #: For each field in either mapping, where the winning value came from.
    where: Mapping[str, str]
    #: The dotted path of the schema source, or ``None`` for introspection.
    schema_source: str | None = None
    schema_source_where: str | None = None
    log: bool = False
    log_level: str = "summary"
    show_schema_stats: bool = False
    #: True when ``--gql-seed=random`` chose the seed in ``cli_values``.
    seed_is_random: bool = False

    def __repr__(self) -> str:
        fields = sorted({*self.file_values, *self.cli_values})
        return f"Settings(fields={fields})"

    def base_config(self) -> ClientConfig:
        """The default ``gql_config``: the built-in defaults, then ini, then env."""
        return dataclasses.replace(ClientConfig(), **_copied(self.file_values))

    def apply_cli(self, config: ClientConfig) -> ClientConfig:
        """``config`` with every field a flag set, which outranks any fixture."""
        return dataclasses.replace(config, **_copied(self.cli_values))

    def where_is(self, field: str) -> str:
        """Where a field's value came from, for the header and reports."""
        return self.where.get(field, "the built-in default")


def _shared_seed(config: pytest.Config) -> int | None:
    """The seed the xdist controller chose, which a worker reads from its input.

    A worker that parsed ``--gql-seed=random`` for itself would choose its own
    seed, and every worker would generate different data. The controller chooses
    once and hands the number to each worker in ``workerinput`` (DESIGN section
    2, "The pytest plugin's sources").
    """
    workerinput = getattr(config, "workerinput", None)
    if isinstance(workerinput, Mapping):
        seed = workerinput.get(SEED_KEY)
        if isinstance(seed, int) and not isinstance(seed, bool):
            return seed
    return None


def _copied(values: Mapping[str, Any]) -> dict[str, Any]:
    """The values, with each mapping copied so a caller cannot change a source."""
    return {
        name: dict(value) if isinstance(value, Mapping) else value
        for name, value in values.items()
    }


def _ini_text(config: pytest.Config, option: Option) -> str | None:
    raw = config.getini(option.ini)
    text = "\n".join(raw) if isinstance(raw, list) else str(raw)
    return text if text.strip() else None


def _env_text(environ: Mapping[str, str], option: Option) -> str | None:
    text = environ.get(option.env, "")
    return text if text.strip() else None


def resolve(config: pytest.Config, environ: Mapping[str, str]) -> Settings:
    """Read, check and rank the three static sources.

    Raises ``pytest.UsageError`` for the first value any source refuses.
    """
    ini_base = config.inipath.parent if config.inipath is not None else config.rootpath
    env_base = config.invocation_params.dir
    file_values: dict[str, Any] = {}
    cli_values: dict[str, Any] = {}
    where: dict[str, str] = {}
    schema_source: str | None = None
    schema_source_where: str | None = None
    seed_is_random = False

    for option in OPTIONS:
        chosen: tuple[Any, str] | None = None
        ini_text = _ini_text(config, option)
        if ini_text is not None:
            source = Source("ini", option.ini)
            chosen = (
                _parse(option, source, option.parse, ini_text, ini_base),
                source.describe(),
            )
        env_text = _env_text(environ, option)
        if env_text is not None:
            source = Source("env", option.env)
            chosen = (
                _parse(option, source, option.parse, env_text, env_base),
                source.describe(),
            )
        if chosen is not None and chosen[0] is not None:
            if option.field is None:
                schema_source, schema_source_where = chosen
            else:
                file_values[option.field] = chosen[0]
                where[option.field] = chosen[1]

        flag = option.flag
        if flag is None or option.field is None:
            continue
        given = config.getoption(flag.dest, default=None)
        if flag.set_to is not None:
            if given:
                cli_values[option.field] = flag.set_to
                where[option.field] = Source("cli", flag.name).describe()
            continue
        if not isinstance(given, str) or not given.strip():
            continue
        source = Source("cli", flag.name)
        value = _parse(option, source, flag.parse or option.parse, given, env_base)
        if isinstance(value, _RandomSeed):
            value = _shared_seed(config)
            if value is None:
                value = secrets.randbits(32)
            seed_is_random = True
        cli_values[option.field] = value
        where[option.field] = source.describe()

    log_level = config.getoption("gql_log_level", default=None)
    level = "summary"
    if isinstance(log_level, str) and log_level.strip():
        level = _parse(
            _LOG_LEVEL_SETTING,
            Source("cli", LOG_LEVEL_FLAG),
            _LOG_LEVEL_SETTING.parse,
            log_level,
            env_base,
        )

    return Settings(
        file_values=MappingProxyType(file_values),
        cli_values=MappingProxyType(cli_values),
        where=MappingProxyType(where),
        schema_source=schema_source,
        schema_source_where=schema_source_where,
        log=bool(config.getoption("gql_log", default=False)),
        log_level=level,
        show_schema_stats=bool(
            config.getoption("gql_show_schema_stats", default=False)
        ),
        seed_is_random=seed_is_random,
    )


#: Where the resolved settings are kept for the session.
SETTINGS: Final = pytest.StashKey[Settings]()


def settings_of(config: pytest.Config) -> Settings:
    """The resolved settings of this session, resolved on first use."""
    found = config.stash.get(SETTINGS, None)
    if found is None:
        found = resolve(config, os.environ)
        config.stash[SETTINGS] = found
    return found
