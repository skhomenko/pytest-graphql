# pytest-graphql design decisions

This document records the current design of pytest-graphql. It states rules, not history.

Authority order:

1. This document.
2. `SPEC.md` in this directory, the historical v0.1 design baseline, for anything this
   document does not cover.
3. Source and tests, which implement both.

Where this document and `SPEC.md` disagree, this document governs. `SPEC.md` is kept as the
record of the approved baseline and is not edited to match this one.

This is contributor documentation. It is excluded from the MkDocs navigation and from the
published site, along with the rest of `docs/reference/`.

---

## 1. Product boundaries

- One PyPI distribution named `pytest-graphql`. The import package is `pytest_graphql`.
- `pytest` is an optional dependency. The required dependencies are `graphql-core`,
  `httpx`, and `certifi`. `pytest` lives in a `pytest` extra and in the `dev` extra.
  `certifi` was already an unconditional transitive dependency of `httpx`; it is a
  direct dependency here because the transport imports it itself, to supply
  `httpx`'s own default CA bundle (the one `httpx._config.create_ssl_context`
  loads via `certifi.where()` when a caller does not pass its own) to the
  locally reconstructed TLS context that keeps `SSLKEYLOGFILE` from being read
  (see "Operational limits").
- The `pytest11` entry point is always declared. It is inert when pytest is absent, because
  only pytest reads it.
- The core library imports no pytest. Only modules under the pytest plugin package may
  import it. A purity check enforces this in CI.
- The base exception is `GraphQLTestError`. Leaf exception classes keep their specification
  names. The base is renamed to avoid a clash with `graphql.GraphQLError`, which comes from
  a required dependency.
- Arguments and fields are looked up by their snake_case form or their exact schema name,
  under "Naming resolution" in section 2. `gql.mutation("createUser", first_name="John")`
  sends `firstName`, and a response field reads as `user.first_name`. Factory payloads use
  the exact schema field names, under "Input factory rules" in section 4, and both
  `**payload` and `input=payload` accept them. Exact schema spellings stay reachable
  through `variables={...}`.
- Middleware ships as part of v0.1. Hooks run only under pytest, so middleware is the only
  request hook available to a standalone user. The plugin installs one hook-delivering
  middleware into the core chain, so there is one mechanism exposed twice.

---

## 2. Configuration and call grammar

### Settings precedence

Per setting, highest to lowest: per-call argument, CLI flag, fixture, environment variable,
ini option, built-in default. Fixtures that return objects with no CLI equivalent, such as
the transport, schema source, auth and scalar fixtures, are outside this ordering.

Environment variables are generated from the ini option table, upper-cased with a
`PYTEST_GQL_` prefix. `gql_max_depth` becomes `PYTEST_GQL_MAX_DEPTH`. Generating them from
one table keeps the two from drifting.

### The pytest plugin's sources

One table holds every option. The ini registration, the environment variable names, the
flags, the parsers and the error messages are generated from it. The ini options are those
of `docs/reference/SPEC.md` section 7.2 without the schema cache options, and the flags are
those of section 7.3 without `--gql-refresh-schema`.

- Each setting has one winning source among the flag, the environment variable, the ini
  option and the built-in default. A list is not merged across sources: the environment
  value replaces the ini value as a whole. The built-in default is whatever `ClientConfig()`
  holds, so a default is written once. That is why `gql_include_deprecated` defaults to
  `False`, as `ClientConfig` and C1 say, and not to the `True` of SPEC 7.2.
- An empty or whitespace-only value counts as not set, in every source.
- A list option is one entry per line, in the ini file and in the environment variable
  alike. `gql_headers` entries are `Name: value`. `gql_redact_headers` entries may also be
  separated by commas, because a header name cannot hold one.
- Every source is checked when pytest configures, whether or not a higher source hides it.
  A refusal is a usage error that names the setting and the source, such as the ini option,
  the environment variable or the flag. It never shows the value, because a header line, a
  URL or a path can hold a credential.
- `gql_verify` is `true`, `false` or the path of an existing CA bundle file. A relative path
  in the ini file resolves against the directory of that file. A relative path in the
  environment resolves against the directory pytest was started in.
- `gql_redact_headers` extends the default list instead of replacing it. A project that adds
  one header keeps `authorization`, `cookie`, `x-api-key` and `proxy-authorization` redacted,
  because dropping one by accident would expose a credential. A name already in the list under
  any capitalization is not added twice. No spelling of the option removes a default. A project
  that needs a smaller list builds the `ClientConfig` itself, which holds exactly the names it is
  given. This is separate from the rule above that one source replaces another: the environment
  list replaces the ini list as a whole, and each of them extends the default.
- `--gql-seed=random` chooses a 32-bit seed once, when pytest configures, and records that it
  did. The seed is read back from the settings. Under xdist the controller chooses it, once,
  and hands it to every worker in the worker input under the key `pytest_graphql_seed`, so
  every worker generates data from one seed. A worker that parses `--gql-seed=random` and finds
  that key uses the number and records that the seed was random. A worker with no such key
  chooses its own, which happens only when xdist did not start it.
- `--gql-log`, `--gql-log-level` and `--gql-show-schema-stats` have no setting behind them.
  They choose what the reporting section of section 3 prints. `--gql-log-level` has no effect
  without `--gql-log`.

Where the fixtures sit in the order:

- `gql_config` returns the built-in defaults, then the ini options, then the environment
  variables. A flag is applied on top of whatever `gql_config` returns, so a flag outranks
  an overridden `gql_config` as well. A project that changes a few fields asks for the
  original `gql_config` and replaces those fields.
- `gql_url` returns the flag, else the URL of `gql_config`. An overridden `gql_url` replaces
  that, and the flag still outranks it. `gql_seed` follows the same shape: by default it is
  the effective seed, an override replaces it for one test, and the flag outranks it.
- The configuration every client starts from is a copy. The flags are applied to it, then
  `pytest_graphql_configure` runs on it, and a hook's change is final. A hook is code the
  project installed, so it sits above the flags and sees them already applied.
- `gql_schema_source` is outside the ordering. By default it is the instance that the ini
  option or the environment variable names, and the environment wins. A fixture override
  replaces it, and then the named path is never imported. The source is an instance of a
  class with a `load()` method and a `fingerprint` string. A class is refused with a hint to
  point at an instance. The path is imported when a test first needs the schema, because
  importing runs user code. A missing module, a missing attribute and an object that is not a
  source each fail with their own message. An exception that the module itself raises, and a
  missing module that the module itself imports, propagate unchanged. The value
  `pytest_graphql.IntrospectionSource` is the label of the built-in source. It means
  introspection of the endpoint and is never imported, because the top-level package does
  not export that class.
- With no path set, the default `gql_schema_source` returns a marker that means "introspect
  over the session transport". An `IntrospectionSource` needs a transport, and the session
  transport is built after the source is read, so the default cannot be a ready one.

### Variables and options

- Keyword arguments are the documented way to pass variables. `variables=` is the
  exact-name escape hatch. Supplying one argument through both raises `ArgumentError` and
  names it. Nothing is silently overwritten.
- Any keyword in the per-call option table is an option. Every other keyword is a variable.
  A schema argument whose name collides with an option is reachable only through
  `variables=`, and the error says so.
- Every variable value is checked against its input type after assembly. A failure becomes
  `ArgumentError` naming the path (`$input.lines[1].price`) and the declared type. When the
  failure sits on an input object, the message names the missing required fields and the
  fields the schema does not declare, at most five of each. Every name, type and path in it is
  clipped to 60 characters. It never repeats a value, and it never uses graphql-core's
  own text, which repeats the value and differs between graphql-core 3.2 and 3.3. Document
  validation alone does not check values, so this closes the gap where a wrongly typed
  variable reached the network.
- Variables are serialized before they are checked, on every path that sends one: `query()`,
  `mutation()` and `execute()`, whether the value came from a keyword, from `variables=`, from
  a generated payload or from a field argument of an explicit selection. Each custom scalar
  with a registered `ScalarSpec` goes through its `serialize`, wherever it sits: at the top,
  in a list or a list of lists, in an input object, or in a list of input objects. `None` is
  left as `None`. A custom scalar with no spec keeps its value. `validate=False` does not skip
  serialization.
- The serialized value is the wire value. The check runs on it and never replaces it, because
  what the schema's coercion returns is Python-side: a custom scalar's parser output, an
  enum's internal value, a stored input field default. A built-in scalar is the one exception
  to leaving a value alone: it goes through its own parser when that accepts the value, so
  `5` for an `ID` is sent as `"5"`. A value it refuses stays as given, and the check reports
  it. An enum value is its member name. A list slot takes any iterable that is not a string
  or a mapping, and a single value becomes a list of one, as graphql-core does. An input
  field the caller left out stays out, so the server applies its default.
- The value put on the wire for a custom scalar must be JSON: `None`, a bool, an int, a finite
  float, a str, a list or tuple, or a dict with str keys. The check cannot verify this,
  because a custom scalar accepts any value, so serialization does. A value that is not JSON
  raises `ArgumentError` before anything is sent. The message names the place
  (`$input.lines[0].price`) and the type of the offending value, and for a scalar with no spec
  it gives the snippet that registers one. It never repeats the value, or the message of an
  exception raised by `serialize`, because either can hold data a test keeps out of its
  output. The error is raised with no exception chained to it, so that message is in no
  traceback either. The caller's value is never changed.

### Naming resolution

One module owns every conversion. Resolution is always by lookup and never by regenerating
a name.

- For each argument list, input object and response object, a map is built from both the
  exact schema name and its snake form to the target.
- Normalization is recursive. A mapping bound to an input-object type resolves its keys
  through the same map at every depth and inside lists. A key matching no field raises
  `ArgumentError` naming the type and the candidate fields.
- When a mapping carries both an exact schema name and its snake form for one field, the
  exact name wins and a warning is emitted.
- When two distinct schema names collapse to one snake key, that key is ambiguous.
  Attribute access and keyword use raise, naming both spellings. The exact spellings still
  resolve.
- `to_camel` exists for error messages only.

### Auto-wrap

Flat keyword arguments are wrapped into a single input-object argument only when all three
hold:

1. The operation has exactly one argument whose unwrapped type is an input object.
2. No supplied keyword matches any of the operation's own argument names.
3. At least one supplied keyword matches a field of that input object.

Mixing operation arguments and input fields turns wrapping off. Explicit `input=payload`
always wins. A keyword matching neither set raises `ArgumentError` listing both name sets.

### Selection grammar

- A literal argument value on a nested field is hoisted into a generated variable named
  from the field path and the argument name, with a counter suffix on collision. No user
  value is ever inlined into document text.
- `Selection.of(TypeName)` resolves the name against the schema at build time and emits an
  inline fragment. An unknown name raises `SchemaError` with a suggestion. A type that is
  not a possible type of the parent raises.
- An alias must be unique within its selection set. A duplicate raises, even when the two
  requests would have merged, and a response key written once plain and once as an alias of
  the same name raises for the same reason. Materialization keys on the alias.
- Two selections of one response key are compared by a canonical form built from the
  declared input type and the argument names the caller wrote, so the same request written
  in different input forms compares equal. An `ID` written `1` and `"1"`, an input object
  written with its fields in either order or with a default left out, a bare value at a list
  position and the same value in a one-item list, and a block string and an ordinary string
  are all the same request.
- Only scalar and enum values are compared through the schema's own coercion. An input
  object is walked field by field under the names the caller wrote, because a coerced input
  object is a Python mapping keyed by `out_name`, which the schema may set freely, and two
  different requests can share one such mapping.
- An input field's default is stored already coerced, so it is read under `out_name` and
  written back under the schema field name. A default that cannot be read that way has no
  canonical form: the type carries a custom `out_type`, the stored value is not a mapping,
  it holds a key no declared field claims, or two declared fields store under the same key
  (a shared `out_name`, or one field's `out_name` colliding with another's schema name), so
  a stored value under that key cannot be attributed to one field over the other.
- A value the declared input type rejects has no canonical form either, and the type-level
  rules count as much as the field-level ones. A one-of input object holds exactly one
  field and that field is not null; anything else is invalid.
- A stored default, at any depth, has a canonical form only when rendering it to a literal
  and coercing that literal back reproduces the exact stored value, in both type and
  structure, checked once over the whole stored value rather than once per leaf, and checked
  at every depth reached inside that value rather than only at the type the top-level call
  was made with. Python's `==` alone is not this check: `1 == 1.0`, and that holds inside a
  list or a mapping too, so comparing an assembled list or mapping only by `==` misses a
  nested `Int` default that round-trips to a `Float`, or the reverse. Execution delivers an
  omitted argument's default exactly as stored, with nothing filled in further at any depth,
  so a default that round-trips to something else, at any depth, does not describe the same
  resolver-visible request. That single recursive check is what has to catch every shape the
  mismatch can take, not a rule stated for each shape: a value the type cannot serialize at
  all is the same case as any other value with no canonical form, not a raised error; a
  stored list default is left uncanonical rather than wrapped when it is not already
  list-shaped, since coercion never turns a bare value into a one-item list the way the
  literal shorthand does, and a stored tuple or set is left uncanonical for the same reason,
  since coercion never produces anything but a `list`; a stored object default missing a
  field is left uncanonical rather than filled from that field's own default, since a stored
  default is delivered exactly as stored and a literal's own-default filling only ever
  applies to a literal; a stored `Int` nested under a list or an object is left uncanonical
  against an explicit `Float` at the same position, and the reverse, since the two are
  different concrete types even where they are numerically equal; a mapping's own keys carry
  this same exact type-and-value rule, matched by content rather than position, so a stored
  key of a `str` subclass is left uncanonical against an explicit plain-`str` key even where
  the two compare equal and hash alike. This check never trusts a value's own `__eq__` or
  `__hash__` to decide sameness, because a caller-controlled `str` subclass, such as one used
  as an input field's `out_name`, can override both so that two different payloads compare
  and hash equal to each other. It also never trusts `isinstance` to recognize a shape,
  because a subclass defeats content matching in a second way that has nothing to do with an
  overridden `__eq__`: it can carry its own extra instance state that the base
  implementation never inspects, so two instances sharing a base payload but holding
  different attached state, such as a custom scalar's parsed value paired with a hidden tag,
  would still compare equal even with no override at all. So each recognized shape is
  recognized only when a value's concrete type *is* that exact builtin, never a subclass of
  it, and a subclass of any of them is treated the same as a wholly unrecognized type. A
  plain `str` is compared through the base `str` implementation directly, reaching the real
  payload no override can hide, and a mapping's keys are matched against that same content
  check rather than through a `dict` keyed by the operands' own keys, since building or
  indexing such a `dict` is exactly the trust in the override this rule removes. This never
  trusts a fallback `==` for any other shape either: `bool`, `int`, `float`, and `bytes` are
  each recognized only as that exact type and compared through that builtin's own
  implementation, the same override-proof and state-proof pattern as `str`. A `float`
  carries one further correction beyond exact-type matching: plain `float.__eq__` calls
  `0.0` and `-0.0` equal, but the two carry a different sign a custom scalar's own
  serializer is free to print, so two `float` operands are matched by value and by the sign
  of zero together. A `dict` is recognized only as that exact concrete type and compared by
  its own `items`, never a `Mapping` subclass more broadly, and `tuple` and `list` are each
  recognized only as that exact concrete type and compared elementwise by recursion, never
  through their own `__eq__`, `__len__`, or iteration: a container subclass can override
  exactly those operations to present a fabricated view that agrees for two instances whose
  real backing content differs, the same trust problem for a container that an overridden
  `__eq__` is for a scalar. A value of any other type, or a subclass of any of the recognized
  ones, such as an opaque object a custom scalar's coercion returns, has no known base
  implementation to compare through, so it is never proven equal by content. Two
  independently produced values of such a type compare equal only when they are the same
  object, since identity is the one override-proof and state-proof test available for an
  arbitrary type; a custom scalar whose output does not match a recognized shape exactly is
  therefore left uncanonical even when its own equality would call two values the same, and
  even when the two values are in fact interchangeable, because there is no way to verify
  that without trusting a subclass's own overridable or unobserved behavior.
- A value with no canonical form is compared as written. That covers a literal that refers
  to a variable, whose meaning belongs to its own document, and every invalid value: an
  undeclared or repeated input field, a missing required field, a one-of object that breaks
  its own rule, and a value the declared type cannot coerce. Invalid text is validation's
  finding to report, so it has to reach validation as the caller wrote it rather than be
  normalized away here. A Python value with no canonical form therefore never compares
  equal to a written literal with none, because merging the two would keep only one of
  them.
- `+` unions selections recursively. Two fields sharing a response key but differing in
  arguments or alias conflict and raise. `-` removes by response key, or by dotted path when
  given a path string. Removing a name that is not present raises, so a renamed field cannot
  silently stop being removed.
- Directives are not supported in v0.1. Using one raises an error naming raw `execute()` as
  the way to send a document with directives.

### Paths and namespaces

- `at()` and `pluck()` take dotted segments. An integer segment indexes a list, and both
  `orders.0.total` and `orders[0].total` are accepted. Each segment resolves like
  `Node.__getitem__`, so exact and snake keys both work. A missing segment raises
  `GraphQLFieldError` unless a default is given.
- Operation namespaces use the snake form as the documented spelling and also resolve the
  camelCase spelling. Type-name namespaces use the exact type name.
- `with_headers` accepts a positional mapping as well as keywords, because HTTP header names
  contain hyphens.

---

## 3. Public API and extensions

### Top-level surface

The top-level `__all__` holds only what a user constructs, catches, annotates or calls:
`GraphQLClient`, `ClientConfig`, `build_client`, the exception hierarchy,
`Selection`, `Field`, `AUTO`, `SelectionPolicy`, `CyclePolicy`, `ScalarSpec`,
`ScalarRegistry`, `DeterministicRandom`, the matcher helpers, `unique`, the `Transport`,
`SchemaSource`, `Auth` and
`Middleware` protocols, `BaseMiddleware`, `BearerAuth`, `HeaderAuth`, `RequestInfo`,
`DiagnosticSnapshot`, `GraphQLResponse`, `Node`, `NodeList`, and `__version__`.

A custom transport needs two more names, so they are in the top-level surface as well:
`RawResponse`, which `Transport.send()` returns, and `DerivableTransportBase`, the base class
that opts a transport into derivation. The compatibility promise for both is at
`pytest_graphql`. They are still defined in `pytest_graphql._core.transport.base`, and an
import from that path keeps working, but the promise and the documentation name the top-level
path only.

`0.1.0` has no built-in `unittest` base class: a `unittest` suite uses `build_client()` and closes
the client in `tearDownClass`.

Importable from their own modules, and carrying a compatibility promise only at that path:
`OperationNamespace`, `FakeNamespace`, `ExpectNamespace`, `HttpxTransport`,
`IntrospectionSource`, `SDLFileSource`, `CapturedErrors`, `SelectionInput`,
`FakeContext`.

### Constructor and configuration split

`ClientConfig` holds data. The constructor holds objects: `transport`, `schema`, `scalars`,
`fake_context` and `middleware`. `seed` is configuration and lives on `ClientConfig` only,
never on the client constructor.

`scalars` is a `ScalarRegistry`. A client without one gets an empty registry of its own, and
reaches it through `client.scalars`. The one registry serves response decoding, variable
serialization and `gql.fake`, and decoding reads `registry.parsers()` on every response, so a
scalar registered at any time is seen by all three on the next call. A clone shares the
parent's registry. There is no separate `parsers` argument.

`fake_context` is a `FakeContext`: the `node_id` of the test the data is for, and the
`UniqueSource` that holds the run id, the worker id and the one counter behind `unique()`.
`gql.fake` is built from it and from `ClientConfig.seed`. A clone shares the same context,
and so the same `UniqueSource`, because two sources with one run id and worker id would each
count from zero and repeat values. A client built without one gets `FakeContext.standalone()`:
the fixed node id `"standalone"`, so seeded data repeats between runs, and a fresh random run
id, so `unique()` values do not. The pytest plugin gives each test's client its pytest node
id and one `UniqueSource` per session in each process. The worker id is the xdist worker id,
or `"main"`. The run id is the xdist run id, which every worker shares, or a new one for each
session. The plugin reads it from `workerinput["testrunuid"]`, the key behind the `testrun_uid`
fixture of pytest-xdist 3.8.0. That key is checked against the xdist source. Tests cover it
with a fake `workerinput` and with a real `-n 2` run, and a run under a rerun plugin is not
part of the suite.

`build_client()` is the standalone spelling of the same split, so the same rule decides
where each of its keywords goes. `url`, `transport`, `cleanup`, `config`, `schema`,
`schema_source`, `scalars`, `fake_context`, `middleware` and `auth` are its own; every other keyword is a
`ClientConfig` field and is applied to the configuration the whole build uses, overriding
the same field on a supplied `config`. An unknown name raises `ArgumentError` rather than
being ignored, so a misspelled option cannot leave a client on a default the caller
believed they had replaced. Data options land on the configuration and not on the client,
which is what makes C4's default schema identity apply to them: `headers` given to the
factory is what introspection sends. A header that must sit above `Auth` is set on the
returned client with `with_headers()`, which is the layer C4 gives that precedence to.

### Typing promise

The package is fully typed, ships `py.typed`, and passes `mypy --strict`. The dynamic
namespaces and `query()` return `Any` and are typed at runtime by design, because static
types for them would require code generation from the schema, which is a non-goal.
`GraphQLResponse` is generic in its data type, `GraphQLResponse[T]` defaulting to `Any`, so
a project can annotate its own results without code generation.

### Auth and middleware

```python
class Auth(Protocol):
    def apply(self, request: RequestInfo) -> RequestInfo: ...


class Middleware(Protocol):
    def before_request(self, request: RequestInfo) -> RequestInfo | None: ...
    def after_response(self, response: GraphQLResponse) -> GraphQLResponse | None: ...
```

`BearerAuth(token)` and `HeaderAuth(**headers)` implement `Auth`, and `as_("tok")` wraps a
string in `BearerAuth`. `Auth.apply` is called once per request, so token refresh needs no
extra machinery. A `BaseMiddleware` with no-op defaults ships, so an implementer overrides
one method.

Middleware is an ordered list. `before_request` runs first to last and `after_response` runs
last to first, so each middleware wraps the ones after it. `None` means no change, a
returned object replaces the value for the rest of the chain, and an exception aborts the
call and propagates unchanged. The plugin's hook-delivering middleware is appended last, so
user middleware runs before the pytest hooks.

### pytest hooks

```python
def pytest_graphql_configure(config: ClientConfig) -> None: ...
def pytest_graphql_schema_loaded(schema: GraphQLSchema, source: SchemaSource) -> None: ...
def pytest_graphql_before_request(request: RequestInfo) -> RequestInfo | None: ...
def pytest_graphql_after_response(response: GraphQLResponse) -> GraphQLResponse | None: ...
def pytest_graphql_register_scalars(registry: ScalarRegistry) -> None: ...
def pytest_graphql_report_section(
    response: GraphQLResponse, item: pytest.Item, config: pytest.Config
) -> str | None: ...
```

The hookspec module lives under `src/pytest_graphql/plugin/`, so it may import pytest and
name `pytest.Item` and `pytest.Config` directly. Every public callback signature is fully
annotated and passes `mypy --strict`. A signature in this document is the contract: an
implementation that widens or narrows it is a defect in the implementation.

Each hook is classified, and the classification is part of the contract.

- Observational: `pytest_graphql_configure`, `pytest_graphql_schema_loaded` and
  `pytest_graphql_register_scalars`. Every implementation runs and the return value is
  ignored. Registering a scalar name twice replaces it and warns.
- Ordered folds: `pytest_graphql_before_request` and `pytest_graphql_after_response`. Every
  implementation runs in pytest hook order, and a non-`None` return becomes the input to the
  next, so two plugins cannot discard each other's work. They are deliberately not
  `firstresult`.
- `pytest_graphql_report_section` runs every implementation, in hook order, once for each
  failed test that made a call and received a response. A test that fails in more than one
  phase gets it once, at its first failed phase, because pytest makes a report for each phase
  and the hook has side effects a project chose. Each failed phase still shows its calls. Its `response` is the last response
  the test received. A test that received none has nothing to report on, so the hook is not
  called for it. Each result that is not `None` becomes a report section of its own, headed
  `GraphQL report: <name>`, where the name is the module name of the plugin. The hook is not
  `firstresult`. A hook wrapper cannot return a section and is ignored. An implementation that
  raises shows as `the hook raised <ExceptionType>`, never with its message. A result that is
  not a string shows as `the hook returned <type>, not a string`. A section is escaped, and it
  is replaced with the withheld notice when it shows a value that any call of the test
  redacted, because the response a hook receives holds the server's text as the server sent
  it.

Delivery rules:

- pluggy calls every implementation of a hook with the same arguments, so it cannot fold.
  The two fold hooks are therefore walked by the plugin, in the order pluggy would call the
  implementations, including `tryfirst` and `trylast`. An implementation may declare fewer
  arguments than the hook. An exception from an implementation propagates unchanged. A hook
  wrapper has no place in a fold, so a wrapper on either fold hook is refused with a message
  that names the plugin.
- The hook-delivering middleware belongs to each per-test client, and clones share it, so
  `with_headers()`, `as_()` and `anonymous()` deliver the hooks too.
- `pytest_graphql_configure`, `pytest_graphql_schema_loaded` and
  `pytest_graphql_register_scalars` each run once per session. They run after the fixture a
  project may override, so overriding `gql_config`, `gql_schema` or `gql_scalars` does not
  skip them.
- An overridable fixture does the work of its own default and no other fixture does it
  early. The default `gql_transport` opens the pool and derives the session transport, and
  asks the endpoint for nothing. The default `gql_schema` loads the schema over a probe
  derived from the session transport, and closes the probe. A project that overrides
  `gql_schema` therefore sends no introspection request, so an endpoint that refuses
  introspection still works.
- `pytest_graphql_register_scalars` receives a view of the session registry that replaces a
  name registered twice and warns. A call that passes `replace=True` says it means to
  replace and gets no warning. A client's own registry keeps the stricter rule of `ScalarRegistry`,
  which refuses a duplicate, so the leniency belongs to the hook alone.

### Reporting

Everything the plugin prints about GraphQL is written by `plugin/reporting.py`.

**The session header.** `pytest_report_header` prints before any test runs, and the schema
loads when the first test needs it. A header cannot hold facts that do not exist yet, so the
header holds what the sources already say, and the schema facts print at the end of the run.
This moves the fingerprint, the counts and the load time that SPEC 7.5 lists for the header to
the GraphQL section below. The header is two lines:

```
graphql: endpoint <url>, seed <n>[ (chosen by random)], run id <id>
graphql: schema loads when the first test needs it, and is listed at the end of the run
```

- The endpoint is the flag, else the environment variable, else the ini option, with the
  userinfo and the query string removed as they are from a recorded URL. When no source
  names one it reads `set by the gql_url fixture`, because a fixture is not known at session
  start. The seed is what the flag, the environment variable or the ini option gives, else the
  default. A fixture override of `gql_seed` or `gql_config` is not known at session start
  either. The run id is the one `unique()` values carry. Outside xdist it is made once for the
  session and kept, so the header and the values agree.
- Text that no call produced, which is the endpoint and the schema label, passes the scrub of
  one request that carries every credential the configuration holds, then is escaped and cut
  at `max_diagnostic_bytes`. One request holds them all, so the cut follows a single scrub and
  cannot split a secret that a second scrub would have removed. The same request is checked
  against every line the failure section, the call log, a hook section and the matcher diff
  show, beside the calls of the test, because a server can echo a credential that no call of
  the test carried.
- Under xdist the controller prints the header and the workers load the schema. The first
  line ends `run id <id>` when `--testrunuid` is given. Otherwise xdist makes the id after the
  header, so it ends `run id assigned by xdist` and the summary gives the number. The second
  line reads `graphql: schema loaded once per worker, and each worker's schema is listed at
  the end of the run`.

**The GraphQL section at the end of the run.** `pytest_terminal_summary` prints a `GraphQL`
section when this process or any worker loaded a schema, and nothing when none did.

- One line for each process that loaded a schema:
  `schema <worker>: <fingerprint>, <n> types, <n> queries, <n> mutations, <n> subscriptions,
  loaded in <s>s`. The worker is `main` outside xdist and the worker id under it. The
  fingerprint is that of the schema source. The time is how long the `gql_schema` fixture took
  to set up, whether the default or an override, and the line ends `load time not measured`
  when no time was taken. Counts leave out the introspection types, whose names start with
  `__`. A count of one is written in the singular.
- `--gql-show-schema-stats` adds a block for each of them: the fingerprint, the type count
  with its kinds (objects, interfaces, unions, enums, input objects, scalars), the field count
  of objects and interfaces, the operation counts and the load time.
- Each worker files `{worker, run_id, seed, schema}` in its `workeroutput` when its session
  ends, and the controller collects them in `pytest_testnodedown`. A report that is not well
  formed is dropped. After the worker lines the controller prints
  `schema loaded by <k> of <n> workers, once each`, `run id <id>, shared by <n> workers` and
  `seed <n>, the same on <n> workers`. When they do not hold, the lines read `more than once on
  a worker`, `run ids differ between workers` and `seeds differ between workers`. A worker that
  ran no test needing the schema loaded none, so it counts in `<n>` and has no line of its own.
- No module imports xdist. The hooks that only xdist declares are `optionalhook`, so they do
  nothing when it is absent.

**The failure section.** `pytest_runtest_makereport` adds a section titled
`GraphQL calls (<n>)` to the report of a failed phase (setup, call or teardown) of a test whose
client recorded a call. pytest prints a section under a rule that carries its title, which is
the rule line of SPEC 7.5. `--show-capture` other than `all` hides it, as it hides every
captured section. The section is built from the recorder alone, and the calls are numbered in
the order the test made them.

```
[1] query user  200  143ms
    query user($id: ID!) { user(id: $id) { id name email settings { id theme } } }
    variables: {"id": "123"}
    skipped (require arguments): User.orders, User.auditEntries
    data: {"user": {"id": "123", "name": "John", ...}}   (truncated, 41 fields)

[2] mutation updateUser  200  201ms   <-- FAILED HERE
    mutation updateUser($id: ID!, $name: String!) { updateUser(id: $id, name: $name) { id name } }
    variables: {"id": "123", "name": "New"}
    errors:
      - CONFLICT at ["updateUser"]: name already taken
    reproduce:
      curl -sS -X POST http://localhost:8000/graphql -H 'authorization: '"${PYTEST_GQL_HEADER_AUTHORIZATION}" --data '...'
```

- The heading is `[<number>] <kind> <operation>  <status>  <duration>ms`, two spaces between
  the fields. The operation is `<anonymous>` for an unnamed one, the status is `-` for a call
  that got no response, and the duration is whole milliseconds. Calls are separated by one
  blank line.
- `FAILED HERE` marks the last call the test made, with three spaces before the arrow. That
  call is the nearest to the failure, and the section lists every call, so a test that failed
  on the result of an earlier one still shows it. Only that call has a `reproduce:` line.
- Under a heading, indented four spaces and each only when it has content: the document on one
  line, where each run of whitespace becomes one space (the `curl` line carries the exact
  text); `variables:` with the snapshot's variables as JSON, so a redacted path shows its
  marker; `truncated:` with the snapshot's own cut notes; one `skipped (<reason>):` line for
  each omission reason, in first-seen order, listing `Type.field`; `data:`; `errors:` with
  one `- <summary>` entry each, indented six spaces; `failure:` for a call that raised before
  it got a response; and `reproduce:` with the recorded `as_curl()` text on the next line,
  indented six spaces.
- `as_curl()` returns one line, which is shown as it is. The multi-line layout SPEC 7.5
  draws is not reproduced, because breaking the text of a quoted command is not safe.
- Reasons read `require arguments`, `deprecated`, `connection with no page-size argument`,
  `connection depth cap`, `depth cap`, `cycle`, `excluded by the selection policy` and
  `union member cap`. The 50 records a snapshot keeps are listed, and the rest are counted in
  `skipped: <n> more not listed`.
- `data:` shows the excerpt the recorder kept. It is omitted when the data is absent or null.
  A cut excerpt ends `   (truncated, <n> field[s])`, where the count is every object member of
  the whole value.
- `errors:` shows the first `max_recorded_errors` errors. SPEC 7.5 says errors are never
  truncated, and section 7 governs: the entries past the limit are counted in a final
  `- ... <n> more not shown` entry, and the count is exact. A message with newlines
  continues under its own entry's indent, so it cannot pass for another entry.
- The recorder is bounded. When it dropped older calls the numbers continue from the true
  count, and the title reads `GraphQL calls (last <k> of <n>)`.
- Each line is checked against the secret set of every call in the section, because a server
  can echo a credential that one call sent into the response to another. A line that fails is
  replaced with its label and the withheld notice, and the finished section is checked again,
  so a secret that spans lines withholds the section. The same holds for a log record.
- The hook sections follow the calls section of the first failed phase. A fault in building the report adds a section that names
  the exception class and nothing else, and the failure it reports on is still shown.

**The matcher diff.** `pytest_assertrepr_compare` handles `==` when exactly one side is a
`Matcher`, and leaves every other comparison to pytest. The diff lines come from
`Matcher.explain` with the options of the calls the test made: their redaction paths, their
scrubs, and `max_diagnostic_bytes`. A test that made no call gets the defaults of the
settings. pytest indents every line after the first by two spaces, and the diff carries
those two already, so the hook drops them and the block reads as SPEC 7.5 prints it. The first
line names the two sides: a matcher and a `Node` by their `repr`, which shows no value, and
any other value as the renderer shows a value. The finished text is checked against every
call of the test. A render refusal gives the withheld notice, and any other fault falls back to
pytest's own report.

**The call log.** With `--gql-log` each call is logged to the logger `pytest_graphql.calls`
at `INFO`, once it is recorded, from the recorded call alone. The plugin sets that logger to
`INFO` for the session and restores it after, so what pytest shows is decided by its own
logging options. `summary` is one line, `<kind> <operation> <method> <url> -> <outcome>
(status <s>, <ms> ms, <n> error(s))`. `full` adds the lines of the failure section for that
call without the heading, the marker or the `reproduce:` line. A fault in building a record
never changes the result of the call.

**State.** One slot in the session stash holds the trace of the running test: the recorder,
at most as many live requests as the recorder holds calls, and the last response. The live
requests exist only to scrub a failed assertion with the secrets those requests carried, and
the secrets that the session's ledger holds for the calls the deque dropped, and they are
never rendered. A second slot holds that ledger, shared by the recorder of every test. The
slot of the trace is replaced by the next test's trace and cleared when the
protocol of a test ends, after all three of its reports. Nothing in the plugin grows with the
number of tests or calls in a session.

---

## 4. Selection and deterministic data

### Auto-selection

Auto-selection is recursive by default. A test should see the whole object without listing
fields, and adding a field to the schema should not require editing tests. A project that
wants a shallow client sets `SelectionPolicy(max_depth=1)` once.

`max_fields` is a document-size guard, not a cost control. The cost controls are these:

- `include_deprecated` defaults to `False`. A deprecated field is the one the schema author
  marked as not to be used, so it is the most likely to be slow or backed by a compatibility
  shim. A test that needs one asks with `fields=`.
- A field whose unwrapped return type is a Relay connection is expanded only when the field
  accepts a page-size argument of type `Int` or `Int!`. Auto-selection then supplies
  `first: connection_page_size` as one generated variable declared `Int!`, default 10,
  unless the caller already supplied one. The declaration is non-null because the engine
  always sends a value, and a non-null `Int` is accepted at both argument shapes. A
  connection field whose page-size argument has any other shape cannot be expanded, because
  one variable of one declared type cannot be valid there. Auto-selection skips such a
  field, and a caller who names it and asks for `AUTO` gets an error. Supplying `first`
  does not change that: the caller's own page size chooses the value, not whether the field
  can be bounded at all.
- `max_union_members`, default 10, caps how many implementations of one interface or union
  are expanded. Members past the cap collapse to `__typename` plus `id` when the type has
  one.
- `max_connection_depth`, default 1, caps nested connection expansion, so a connection
  inside a connection is not expanded by default.

Authorization cost stays a project concern. Fields the test identity cannot read are removed
through the `exclude` patterns on `SelectionPolicy`.

Any field with at least one required argument, meaning non-null with no default, for which
no value was supplied, is skipped, whatever its return type. Skipping only composite fields
would emit a scalar field without its required argument and produce an invalid document.

Every automatic omission is recorded and surfaced in diagnostics, not only this one. Eight
reasons drop a field: a required argument with no supplied value, a deprecated field, a
connection with no page-size argument, the connection-depth limit, the depth limit, the
cycle policy, `should_include`, and the `max_union_members` cap. Selection normalization
returns a structured record for each, carrying the parent type, the field path relative to
its scope, and the reason, and never an argument value.

- A rule that removes a whole field records that one field.
- A rule that keeps a type but reduces its selection records each field it removed. This
  covers a member past the `max_union_members` cap, which keeps `__typename` and `id`, and
  the cycle policy's `id_only`, which keeps `id`.
- A rule that removes an interface or union member's fragment records each field of that
  member, because a fragment has no field of its own. A fragment adds no path component, so
  the parent type is what tells two members' records apart.
- A field removed by a reducing rule that `should_include` or the deprecation rule would
  have removed anyway is recorded under that reason instead.
- The cycle policy's `shallow` spends the depth budget to zero, so a composite field it
  removes is recorded as `cycle`, not as `depth`.
- A member field that the interface already selects at the same position in the same
  automatic selection is not an omission, whichever rule removed it from the member's
  fragment, because the response carries it for every member. The records describe one
  automatic scope, so a field that an explicit selection around a spliced `AUTO` selects is
  still recorded when that scope left it out.
- A field that no rule selects in the first place is not an omission. Examples are a
  connection field outside the rule 4 template, and an interface's composite field, which
  rule 6 selects through each member's fragment instead, so a member that loses it records
  it there.
- An interface that no object type implements has no member fragment, so its composite
  fields are neither selected nor recorded. No value of that interface can exist, so the
  server never returns one, and no data is left out.

Diagnostics prefixes those relative paths when it composes a nested
or cached automatic selection, so a reader sees the field's position in the finished
document. The records are bounded on their own, because a skipped field is not counted by
`max_fields`: the first 50 in traversal order are retained, the total is tracked, and the
number omitted is reported visibly. The total is carried across every stage that passes the
records on, from selection through assembly and the request to the snapshot a report reads.
A stage that recounts it from the retained list instead can only ever report that nothing
was dropped, which is the one thing this rule exists to prevent.

`__typename` is emitted on every object selection, not only on interfaces and unions, and it
does not count against `max_fields`. Explicit `fields=` adds nothing, so `Node.__typename__`
is `None` there unless the user asked for it. A matcher checks the type name only when the
response object carries one.

`AUTO` is a scope. Every policy value that describes a position is relative to the generated
selection it appears in, never to the document that contains it:

- Each `AUTO` starts a new scope. `max_depth`, `per_type_depth_cap`, the cycle ancestors and
  the position arguments of `should_include` are all measured from that scope's root.
- Fields at a scope root receive `path=()` and `depth=0`.
- A path holds GraphQL field names. An inline fragment adds no component, because a fragment
  is not a field. The generated variable names derived from a field path follow the same
  rule.
- An explicit prefix above an `AUTO` does not consume depth, does not become a cycle
  ancestor and does not contribute to connection depth.
- A scope whose own root type is a Relay connection starts at connection depth 1, so asking
  for `AUTO` at a connection type cannot reset the nesting cap.
- An explicit selection can therefore make the finished document deeper than `max_depth`.
  That is intended: explicit selections belong to the caller, and `max_depth` caps
  auto-selection.

This is what keeps a reusable `Selection` meaning the same thing wherever it is inserted, and
what keeps the memoization key below at `(type name, policy fingerprint)` and nothing else.

`max_fields` runs the other way, because it guards document size rather than traversal. It
counts the complete normalized selection: every explicit field, plus every field of every
nested or sibling `AUTO`, excluding `__typename`. A generated selection carries its own field
count so that a cached entry can be composed into a larger selection without rebuilding it.

The default numeric limits above, along with `max_fields`, `max_depth` and `cycle_policy`,
are validated against a checked-in corpus of project-authored synthetic schema fixtures,
plain GraphQL SDL built directly with graphql-core's `build_schema`, before any layer is
built on them. The domain-specific names and prose in each fixture are project-authored,
and none of them is copied from, or models, any third-party API's domain content. The
pagination field and type names (`edges`, `node`, `cursor`, `pageInfo`, `hasNextPage`,
`endCursor`, and the `*Connection`/`*Edge` suffixes) intentionally conform instead to the
GraphQL Cursor Connections Specification, because the Relay-detection heuristic under test
depends on exactly that convention: `facebook/relay`, `website/spec/Connections.md` at
commit `f9a7c64558c00221aa6baf6d79deb50731f74519`, retrieved 2026-09-15, MIT License, Meta
Platforms, Inc. and affiliates. No prose or example text from that specification is copied;
only its functional naming convention is reused. The validation is reproducible and needs
no live endpoint of any kind, real or synthetic.

### Calibration

The calibration gate runs against `tests/schema/corpus/sdl/` (`catalog.graphql` and
`feed.graphql`, two small fixtures authored specifically to exercise this gate; see
`tests/schema/corpus/README.md` for what each one covers), using
`tests/schema/corpus/measure.py`. Every number below is printed by that script as it is
checked in; running `uv run python tests/schema/corpus/measure.py` reproduces each one
exactly, rather than requiring a reader to trust a figure computed outside the committed
harness. `tests/unit/test_corpus_calibration.py` pins the same numbers as a regression test,
so a change to either fixture or to the selection engine that moves one of them fails CI.
Three of the defaults below are kept on direct corpus measurement, and two more are kept on
an explicit, recorded rationale where no schema, real or synthetic, has anything left to
measure. The last two, `max_fields` and `max_depth`, are carried over unchanged rather than
settled by this gate: the corpus does not exercise either near its boundary, so SPEC open
question 5 stays open.

- `max_union_members=10`: exercised directly. `catalog.graphql`'s `SearchResult` union has 12
  members; the cap collapses the 2 members past 10 to `__typename` plus `id`. `Node`, the
  fixture's interface, has 3 implementers, a representative width under the cap.
- `max_connection_depth=1`: exercised directly, at both the schema and the engine level, with
  a differential check rather than a single build: the harness builds each outer connection's
  selection once under the default cap and once with the cap raised by one, and only counts a
  case as caused by the cap when the inner connection field is absent at the default and
  present once the cap is raised. This separates the cap from an unrelated reason a field can
  also be absent, such as a connection whose page-size argument is not named `first`.
  `catalog.graphql` has one connection nested inside another connection's `node` type,
  `CategoryConnection.products` returning `ProductConnection`, and it passes the differential
  check: the cap does not merely exist in the fixture's shape, it demonstrably holds when the
  engine runs. `feed.graphql` has two such nested connections
  (`PostConnection.comments` and `CommentConnection.replies`, both returning
  `CommentConnection`), and neither passes the check: both stay absent at both depths, because
  `feed.graphql`'s connections use `perPage` rather than `first` (see the out-of-scope note
  below) and are never expanded regardless of the cap. One fixture confirms the cap
  engine-side; the other is schema-shape evidence that the unconfirmed case is not a bug.
- `include_deprecated=False`: confirmed reasonable. Deprecated fields exist in the corpus
  (2 of 42 fields in `catalog.graphql`, 1 of 31 in `feed.graphql`), so excluding them by
  default avoids surfacing known-legacy fields unless a test asks for one.
- `connection_page_size=10`: kept, on rationale rather than corpus measurement. GraphQL SDL
  can declare a default for a page-size argument (read through `default_of`), so the
  harness checks for one directly rather than assuming its absence: of `catalog.graphql`'s 3
  recognized `first` arguments, 1 (`Query.categories`) declares a default; the other 2, plus
  all of `feed.graphql`'s connections, do not. Only a server's undeclared,
  implementation-side runtime default stays outside what any schema document, introspected or
  authored, could ever reveal, and that gap is what the value falls back to the conventional
  Relay default for.
- `cycle_policy="shallow"`: kept, on rationale rather than corpus measurement. Both fixtures
  confirm self-reference is a normal schema shape (`Category.parent`/`Category.children` in
  `catalog.graphql`, `Comment.replies` in `feed.graphql`), which is why cycle handling matters
  at all, but a cycle *policy* is a choice about what a client should see when a cycle is
  reached, not a fact a schema graph carries. No schema document can reveal which of
  `"shallow"`, `"stop"` or `"id_only"` a real API's consumers would want; that is a product
  decision, made once for this library's default and left open to any caller through
  `SelectionPolicy(cycle_policy=...)`.
- `max_fields=2000` and `max_depth=3`: the synthetic corpus does not exercise either cap near
  its boundary, and that is stated here rather than implied by silence. The largest
  default-policy build in the corpus is 62 fields at an AST depth of 6
  (`catalog.graphql`'s `search` field), far under both limits; the deepest uncapped type-depth
  probe reaches 5 on `catalog.graphql` and 6 on `feed.graphql`, against the probe's own cap of
  20, well short of either limit; a small, readable, authored fixture cannot stress that
  boundary without stopping being either small or readable. What the corpus still
  demonstrates directly is that both fixtures build cleanly well under both caps, and that
  `Category` and `Comment`'s self-reference is exactly the shape a
  depth cap exists to bound, confirmed by the built selections' AST depth exceeding the
  numeric value of `max_depth=3` itself (the connection template's `edges`/`node` scaffolding
  does not consume the traversal budget, by engine design, so it adds AST levels the `depth`
  counter never charges for). Neither number is re-tuned by this corpus; both remain the
  pre-existing defaults, carried over rather than re-derived.

Spec open question 7 (interface/union expansion cost) closes on this evidence: it is
exercised and adequate, not merely assumed. Open question 5 (`max_fields` default) stays open
in the narrow sense above: this corpus shows the cap does not misfire at ordinary scale, but
does not stress it near the boundary, and no claim to the contrary is made. Open question 6
(relay detection heuristic) closes by direct construction rather than by absence of a
counterexample: `feed.graphql` includes one type named like a
connection that is not shaped like one (`LegacyThreadConnection`, missing `pageInfo`) and one
shaped like a connection that is not named like one (`AttachmentGroup`), and the heuristic
correctly classifies both as mismatches rather than as detected connections.

One related finding is explicitly out of scope here and left unchanged: `feed.graphql`'s
connection fields use `perPage`, not `first`, so none of them carry a recognized page-size
argument (`PAGE_SIZE_ARGUMENT` above) and none are ever expanded. This leaves the
`feedStats` query field, whose only fields are connections, with nothing selectable, which
correctly raises per the "root type that yields nothing raises" rule. This is the documented
fallback behaving exactly as designed, not a defect, and changing `PAGE_SIZE_ARGUMENT` is a
separate design decision this gate does not make.

### Selection policy

`SelectionPolicy` is one frozen dataclass holding `max_depth`, `cycle_policy`,
`per_type_depth_cap`, `include_deprecated`, `max_fields`, `exclude` and `relay_aware`, plus
a `fingerprint` property and one overridable hook:

```python
def should_include(self, parent_type: str, field_name: str,
                   path: tuple[str, ...], depth: int) -> bool: ...
```

The default implementation applies the `exclude` patterns. Subclassing is the extension
point.

Selection memoization keys on `(type name, policy fingerprint)` and on nothing else. The
base class derives the fingerprint from its dataclass fields. When the base class detects
that `should_include` is overridden and `fingerprint` is not, memoization is disabled
entirely for that policy and a warning naming the class is emitted once per class. No
identity-based fallback key exists: an object identifier is reused after collection, so a
later instance could receive an earlier instance's entry, and such a key also stays stable
when a policy reads mutable external state. Both produce a silently wrong selection.
Building fresh costs time and not correctness, because the limits above bound every
traversal. A custom policy opts back into caching by overriding `fingerprint`, and the value
must cover every input its decisions depend on, including external state.

That invitation is also what makes the cache unbounded if nothing bounds it: a policy whose
fingerprint covers state that changes per test produces a new key on every build. Each
builder's memo is therefore a least-recently-used cache capped at 128 entries. Eviction
affects performance only, by the same argument as above, because the limits bound every
traversal. The bound is internal: it is not a `SelectionPolicy` field, not a user-facing
setting, and not one of the numbers the calibration gate measures.

### Deterministic data

- Seeds derive as
  `int.from_bytes(sha256(f"{global_seed}\0{node_id}".encode()).digest()[:8], "big")`. The
  builtin `hash()` is never used, because it is salted per process.
- The determinism promise is: the same seed, node id, field path and package version produce
  the same value on any machine and any supported Python.
- `random.Random` is not used for any value that reaches output. A `DeterministicRandom`
  built on SHA-256 in counter mode provides `bits`, `below`, `choice`, `float_unit` and
  `sample_string`, so the package owns its sampling and cross-version stability holds.
- `DeterministicRandom` is public and exported. It is the only random-number type that
  appears in a public callback signature, `ScalarSpec.fake` included. No public signature
  accepts `random.Random`, because a caller given one could produce output that is not
  reproducible.
- Golden vectors store the output of every built-in scalar, of the seed formula, of the
  sampler, of `unique()` and of nested input objects, at fixed inputs, in
  `tests/factory/golden.json`. The test compares text byte for byte on every supported
  Python. A changed value fails the suite, so any change is deliberate, versioned and recorded
  in the changelog. The file is never regenerated to make a failing test pass.
- Every hashed input has one documented byte format, stated in the module that hashes it
  (`seed.py`, `rng.py`, `unique.py`). Nothing that reaches output uses `random`, the builtin
  `hash()`, iteration over a set, a default byte order, or the printed form of a float.
- The seed text is encoded as UTF-8 with `surrogatepass`. This gives the same bytes as
  `.encode()` for every text that `.encode()` accepts, and gives a lone surrogate from an
  undecodable file name a seed instead of an exception.
- `DeterministicRandom(seed, *path)` takes a seed in `[0, 2**64)` and a path. Its stream is
  `sha256(domain + seed + encoded path)` as a key, then `sha256(key + counter)` for
  counter 0, 1, 2 and so on. `below(n)` is rejection sampling on `(n - 1).bit_length()` bits,
  never a remainder. `float_unit()` is exactly 53 bits divided by `2**53`. `sample_string`
  draws one character per index from its alphabet, which defaults to lowercase letters and
  digits.
- `unique(kind=None)` returns a marker. The factory replaces it with a value derived from run
  id, xdist worker id or `"main"`, node id, field path and one monotonic per-process counter.
  `kind` is `None`, `"string"` or `"email"`. The email domain is `example.com`, which RFC
  2606 reserves, so a value never reaches a real mailbox. Repeated calls in one test differ,
  and values differ across workers and across runs. Reproducibility and uniqueness are
  mutually exclusive by design.
- The `unique()` counter is one integer for the whole process. It is not kept per node id and
  field path, because such a table gains a row for every test and field and never releases
  one, which breaks the bounded-state rule. A rerun of one node id in one process, such as a
  rerun plugin makes, still gets new values, which a counter reset for each test would not
  give. A test holds the bound by showing that the state is only scalars and that memory does
  not grow with the number of node ids and paths.
- The factory core takes the global seed, the node id and a `UniqueSource` (run id and worker
  id) as explicit arguments, and reads no environment, clock or global state. A client supplies
  them from `ClientConfig.seed` and its `FakeContext` (section 3, "Constructor and
  configuration split").

### Input factory rules

- `gql.fake.<Name>` reaches every input object type of the schema, and a name that GraphQL
  allows is never shadowed. The namespace has no attribute of its own besides one under a
  reserved `__` name, because GraphQL reserves that prefix and no type can use it.
- A payload is a plain `dict` with the exact schema field names, in schema order. The exact
  names are the one spelling that both `**payload` and `input=payload` accept, and two
  snake_case forms can collide where exact names cannot.
- An override key may be the exact name or its snake_case form, under the naming rules of
  section 2. An unknown key raises `SelectionError`, and two spellings of one field raise
  `SelectionError`. An override replaces the whole field value, a nested one included, and a
  `unique()` marker inside it is resolved at its own path.
- Every value draws from its own `DeterministicRandom`, keyed by the seed and the full field
  path, and the path starts with the input type name. A field keeps its value when another
  field is skipped, added or overridden. Two calls with the same inputs return equal
  payloads, so a value that must differ between calls uses `unique()`.
- A non-null field is always filled. A nullable field is filled unless `_required_only` is
  set. A nullable field that is filled holds a value, never `null`. Deprecation is ignored.
- An enum value is picked by the sampler from the member names sorted by code point, so the
  pick does not depend on declaration order or on a server that reorders its enum.
- The root input object is level 0, a nested input object is one level deeper than its
  parent, and a list adds no level. Objects at levels 0 to `_depth` (default 2) are filled in
  full. Objects at deeper levels are filled with required fields only, which cuts a recursive
  type at the cap and still builds a required nested object. A recursive type therefore nests
  `_depth + 2` objects at most.
- A list gets one to three elements.
- A custom scalar resolves through the `ScalarRegistry`. The lookup happens only for a field
  that would be filled and not overridden, so `_required_only` and an override never raise
  for an unregistered scalar. The factory returns the value `fake` produced, without calling
  `serialize`. Serialization belongs to the variable send path (section 2, "Variables and
  options"), which also covers values a user supplies, so a payload may be edited and asserted
  on as Python values.
- Built-in scalar values: `String` is 8 characters of lowercase letters and digits, `ID` is 12
  lowercase hexadecimal characters, `Int` is an integer from 1 to 1000, `Float` is a whole
  count from 0 to 99999 divided by 100, and `Boolean` is one draw of two.

### Scalars

Decoding is opt in per scalar. The default `parse=None` leaves the raw JSON value in place,
so the raw response is never lost.

```python
@dataclass(frozen=True)
class ScalarSpec:
    name: str
    serialize: Callable[[Any], Any]                 # Python value to JSON, for variables
    fake: Callable[[DeterministicRandom], Any]      # factory value
    parse: Callable[[Any], Any] | None = None       # JSON to Python, for responses
```

- `ScalarRegistry` is the one place specs are kept. It offers `register(spec, *, replace=False)`,
  `get(name)`, `parsers()`, membership, iteration in sorted name order, and `len`. A second
  registration of one name raises `ValueError` unless `replace=True`, because two
  registrations of one name are usually two plugins that disagree.
- `parsers()` returns a snapshot of `name -> parse` for the specs that decode. It is the
  mapping the response materializer reads, and the client takes a new snapshot for each
  response, so decoding, serialization and the factory share one registry and the package has
  no second scalar spec type.
- The five scalars the GraphQL specification defines are never registered. A spec cannot take
  one of their names, because the package generates them.
- A spec checks itself when it is built: the name is a GraphQL name that does not start with
  `__`, and `serialize`, `fake` and a given `parse` are callable.
- `ScalarNotRegisteredError` names the scalar, the place the factory needed it (type, field
  names and list indices), the `ScalarSpec` snippet that registers it, and the field to
  override instead.

---

## 5. Responses, matching, and polling

### Materialization

The client walks the selection set it built alongside the JSON, so the GraphQL type of every
value is known. Aliases resolve to the alias name, fragments and inline fragments flatten
onto the parent, and list nesting follows the type, including `[[String!]!]!`.

A value whose type is a custom scalar is never wrapped. It goes to that scalar's `parse`
when one is registered and stays raw JSON otherwise, dictionaries and lists included. Only
object, interface and union values become `Node`, and only lists of those become `NodeList`.
There is no shape-based guess.

`GraphQLResponse.raw` holds the untouched envelope, captured before any parsing.

Validation is eager and wrapping is lazy. `build_response` checks the whole `data` value
against its declared types once, then a `Node` wraps each field on first read and caches
it. A value that contradicts its type raises `ResponseShapeError`, which names the response
path and the schema type and never echoes a server value. The checks are: a list where the
type is a list, an object where it is an object, the built-in scalar kinds (`Int` and
`Float` refuse `bool`), a string for an enum, and a `__typename` that names a possible type
of the abstract parent. A `null` is accepted at any position. A key the selection did not
ask for is kept and read as raw JSON. Object and list nesting past 128 levels raises, wherever
it sits in `data`, `errors` or `extensions`, including inside a custom scalar value and under
an unselected key. The bound counts every `Mapping`, list and tuple, because a custom
`Transport` may return any of them, not only the `dict` and `list` a JSON decoder builds. When
an abstract value carries no `__typename`, every type-conditioned fragment applies to it.

`GraphQLResponse.http` is `HttpInfo`: `status_code`, `media_type`, `url` (from the redacted
request snapshot) and `headers`. It carries no reason phrase, because HTTP/2 has none, and no
elapsed time, which is `GraphQLResponse.duration_ms`.

`GraphQLResponse.raw` is rebuilt from the transport's parsed result, so `errors` and
`extensions` appear only when present and non-empty. The `data` value inside it is the
object the transport parsed, unmodified.

`Node.__repr__`, `GraphQLResponse.__repr__` and `GraphQLErrorInfo.__repr__` render field
names, counts, the status and the redacted request snapshot. They never render a response
value or an error message.

### Response states and raising

`GraphQLResponse` distinguishes three states: `data` absent, `data` null, and `data`
present. `has_data` is true only for the third.

| errors | data | `raise_on_error` | `raise_on_partial` | Result |
|---|---|---|---|---|
| yes | no | true | any | `GraphQLExecutionError` |
| yes | yes | true | true | `GraphQLPartialDataError` |
| yes | yes | true | false | no raise, data returned |
| yes | any | false | any | no raise, data returned |
| no | null | any | any | `GraphQLExecutionError` (protocol violation) |

`raise_on_error=False` disables all raising, partial included. `raise_on_partial` applies
only when `raise_on_error=True`. Absent `data` with no errors is a protocol violation,
`data: null` with errors is `GraphQLExecutionError`, and `data: null` with no errors is a
protocol violation.

`GraphQLExecutionError(message)` stays valid for a caller that builds one, as in the published
alpha. Every instance the library raises also carries `response`, the response that failed,
and `errors`, its error list. An instance built without a response has `response` set to
`None` and an empty `errors`, and `expect_error` does not capture it.

`execute()` always returns `GraphQLResponse`. Convenience unwrapping stays on `query()` and
`mutation()`, which have exactly one known top-level field. `GraphQLResponse.unwrap()` is
the explicit opt-in for a raw document. It counts the fields the document selects at the top
level, whether or not the server returned them, and it raises `GraphQLTestError` naming the fields
when that count is not one.

### `Node` as a mapping

`__iter__` and `__len__` use the original server keys only. `dict(node)` yields original keys
with lazily wrapped values. `node == dict` compares `to_dict()` to the dict exactly, not
partially, because partial comparison is what matchers are for. `node == other_node`
compares raw dicts. `node == Matcher` returns `NotImplemented`.

### Matching

- `contains(*items)` is multiset containment solved as maximum bipartite matching between
  items and elements, so an item that could match several elements never consumes the one
  element another item needs. It is exact and polynomial, with no exponential backtracking.
- `unordered(*items)` uses the same matching and additionally requires equal lengths.
  Duplicates on either side are handled, because matching is over distinct elements.
- `NodeList.where(**filters)` is partial by default: an element matches when every named
  field matches and other fields are ignored. `where(strict=True, ...)` additionally requires
  the element to carry exactly the named fields.
- `one(**filters)` raises unless exactly one element matches, and names the count it found.
- `absent()` takes no argument. It is a value placed on the field:
  `gql.expect.User(deleted_at=absent())`.
- `count` applies after filters. `NodeList.where()` returns a filtered `NodeList`, and a
  `count=` assertion counts that filtered result.

Matcher comparison rules the list above leaves open:

- A boolean never equals a number. `contains(1)` does not match `[True]`, and `one_of(1)`
  does not match `true`, although Python says `True == 1`.
- A plain `dict` inside a matcher is a nested partial match and a plain `list` is positional
  with equal length. Neither is schema-validated.
- The type name of an `expect` matcher is checked against the response object's runtime type
  name. An interface or union matcher accepts every possible type. `__typename` is always a
  valid field name to match, including on a union, which has no other fields.
- `matches` is a regular-expression search, so anchors are the author's to write. It matches
  strings only.
- `gt`, `gte`, `lt` and `lte` compare numbers, dates and strings, so ISO date strings order
  correctly. A boolean is not a number. A value that cannot be ordered against the bound
  does not match and does not raise.
- `NodeList.where` ignores an element that is not a `Node`. A filter name resolves like a
  `Node` key, exact or snake case. `where` and `one` take no `count` argument. Counting is
  `len()` of the filtered list, and any `count=` assertion counts that same list. `strict`
  is a keyword of `where` and `one`, so a field of that name is filtered through a nested
  matcher on the element instead.
- `contains` and `unordered` build one table of item and element pair results before they
  match. The table is capped at `MAX_MATCH_PAIRS` (one million pairs), and a larger call
  raises `GraphQLTestError` and names `where()` as the way to narrow the list first.
- A matcher's `repr` names its type and its field names only, never an expected value,
  like `Node`. `one()` names filter fields and counts, never a response value.
- `gql.expect` is reachable as `client.expect`. A type with no selectable fields (an enum,
  an input object or a scalar) raises `SchemaError` when `gql.expect.<Name>` is read.

### Matcher diff

`Matcher.explain(actual)` returns the lines of the SPEC 7.5 matcher diff, or an empty list
when the value matches. The lines keep the SPEC's own indent, so the `pytest_assertrepr_compare`
hook adds only the `assert` line above them.

- A compared field is one leaf comparison: a scalar, a helper, or a list-level matcher such
  as `contains`. A nested object contributes its own fields, not itself. The title counts
  compared fields and the compared fields that differ.
- An ignored field is a field of a compared object that the matcher did not name. Fields
  below an ignored field are not counted. A field whose type name check fails stops the
  comparison of that object.
- Differing leaves print as `path  actual  !=  expected`, in comparison order. The path
  column is padded to the longest path plus three spaces, and the actual column to the
  widest actual value. A missing field prints as `<missing>`. An object or a list prints as
  a summary (`User(id, name)`, `[2 items]`, `{3 fields}`), never as its content.
- The matched leaves print on one `matched:` line.
- The listing is bounded: `max_lines` differences (default 50), each value cut at
  `max_value_bytes` (default 200) with a visible byte count, and `max_matched` names
  (default 50). The counts in the title stay exact, and each cut says how many entries it
  left out.
- Every printed value follows section 7: scrub, then escape control characters, then
  truncate. A string is scrubbed before `repr` quotes it, because `repr` doubles backslashes
  and escapes quotes, and the quoted form is scrubbed again. A field whose path matches
  `redact_variables`, or sits below a path that does, prints `[redacted]` on both sides and
  no detail lines. When the options carry a diagnostic snapshot, the finished text is checked
  against it and the renderer raises `DiagnosticRenderError` if a secret is still in it.

### Error assertions

`GraphQLClient.expect_error(code=None, path=None, message_matches=None, count=None)` returns a context
manager that yields `CapturedErrors`. The filter values are checked when `expect_error()` is
called, so a bad one fails at the call and not after the block has run.

- The block must end in a `GraphQLExecutionError` whose response carries at least one error.
  `GraphQLPartialDataError` is one. A protocol violation is a `GraphQLExecutionError` with no
  errors, so it propagates unchanged: a broken server cannot satisfy the block. Every other
  exception, `BaseException` included, propagates unchanged and is never chained.
- A block that ends without raising fails with `ExpectedErrorNotRaised`, which names the last
  response the block received and how many it received. A client that does not raise, through
  `raise_on_error=False` or `raise_on_partial=False`, never satisfies the block, and the
  message says so.
- Each filter is checked on its own, and each must match at least one error. They need not
  match the same error, which is the rule SPEC 3.8 states. A filter that matches none fails
  with the same class, `ExpectedErrorNotRaised`, because SPEC 8.1 names no other. Its
  `unmatched` names the filters, its `errors` holds every error the server returned, and its
  message lists them, bounded by `max_recorded_errors` with the count left out stated.
- `code` equals `extensions["code"]` exactly. `path` equals the whole error path, segment by
  segment: it is not a prefix, a name never equals an index, and a boolean is no index.
  `message_matches` is `re.search`, like `matches()`, so anchors are the author's to write.
  It takes a `str` or a compiled pattern, whose flags apply.
- `count` is an exact assertion on the total number of errors the server returned. It is
  not a matching filter: it counts every error, whatever `code`, `path` and `message_matches`
  matched, and it is checked beside them. A mismatch fails like an unmatched filter, with
  `count` in `unmatched` and the full error list in the message. It takes an integer of at
  least 1, because a block that raises has at least one error. A `bool`, a non-integer or a
  value below 1 is refused when `expect_error()` is called.
- `CapturedErrors.errors` is every error the server returned, never only the matching ones.
  `.first` is the first of them and `.response` is the response. They are readable once the
  block has ended, and reading one earlier raises `GraphQLTestError`. A failed filter still
  fills them before it raises. Its `repr()` shows a count and no server text.
- A block sees the responses of every client in its context, so a call made through
  `gql.as_(...)` counts, and nested blocks each count the calls made inside them. Each block
  keeps its last response and a call count, nothing else.
- A failed filter chains the error it judged as the cause only when that error's chain shows
  no secret of its response's request, by the cause rule under "Polling".

### Polling

`wait_until(name, *, until, timeout=30.0, interval=1.0, backoff=1.0, ignore=(), **variables)`
computes its deadline once from `time.monotonic()`. An attempt is counted when
the call is made, and at least one attempt always runs, `timeout=0` included. The sleep after
a failed attempt is `min(interval * backoff ** (attempt - 1), remaining)`, and the loop
raises once `remaining` reaches zero.

That formula is the whole rule. SPEC 3.9 also capped a delay at `interval * 10`, and that cap
no longer applies: the deadline is the only bound on a sleep, so an exact sleep sequence is
fixed by the three numbers alone. A float overflow in `backoff ** (attempt - 1)` means a
delay larger than any deadline, so it is clamped to `remaining`, and a zero interval stays
zero. The clock and the sleep are read through the private names `_monotonic` and `_sleep` of
`pytest_graphql._core.polling`, which a test replaces.

`timeout` and `interval` are finite numbers of at least zero and `backoff` is a finite number
of at least one. A boolean or a non-number raises `TypeError` and a number outside that
range raises `ValueError`, both before the first call. An infinite timeout is refused,
because a poll that cannot end is not a bounded wait.

`ignore` accepts a class or a tuple of classes, and only `Exception` subclasses. Passing
anything else raises `TypeError` at call time, so a `BaseException` such as `KeyboardInterrupt`
is never swallowed by a poll loop, during an attempt or during the sleep. An exception
outside `ignore` propagates unchanged. `ignore` covers the whole attempt: the call, the
unwrapping of its response and `until` itself.

`until` receives what `query()` returns for the same arguments, or the whole response when
`raw=True`, and `wait_until` returns that same value. Every other keyword is read as
`query()` reads it, so `timeout` here is the deadline of the whole poll and the HTTP timeout
of one attempt is `ClientConfig.timeout`.

`WaitTimeoutError` carries attempts, elapsed monotonic time, the limit, the operation name,
the last response and the last swallowed exception. The last response is the most recent
response any attempt received, including the one a swallowed `GraphQLExecutionError` carries.
These two values and the redacted request of the attempt that raised the exception
(`last_exception_request`) are the only per-attempt state a poll keeps. That request is the
attempt's own, never an earlier one, and it is empty when the attempt failed before any
request existed. The exception text is shown only after it was checked against that request.
With no request to check against, the text is withheld and the exception stays on
`last_exception`. The exception is chained as the cause only when it is a `GraphQLTestError`
and neither it nor anything a traceback prints with it (its notes, its cause, its context,
the members of an exception group) shows a secret of those requests. The class alone proves
nothing, because a caller can build a `GraphQLTestError` that no request ever checked.

Polling covers queries only. The schema decides: a name that resolves to a mutation or a
subscription raises `ArgumentError` before any call, whatever the name looks like, and a name
that is both a query and a mutation is polled as the query.

---

## 6. Transport and protocol

### Request

A POST with `Content-Type: application/json`, a UTF-8 JSON body carrying `query`,
`variables` and `operationName`, and
`Accept: application/graphql-response+json, application/json;q=0.9`.

### Classification

Classification runs on media type and envelope shape before status.

1. `application/graphql-response+json`: parsed as a GraphQL response at any status.
2. `application/json`, legacy mode: parsed at 2xx; parsed at non-2xx when the body is a valid
   envelope; otherwise a transport failure.
3. Anything else, or a body that is not a valid envelope: `GraphQLHTTPStatusError` for
   non-2xx and `GraphQLTransportError` for an unparsable 2xx, both carrying a capped body
   excerpt.

`GraphQLRequestError` covers a GraphQL request error, meaning the server rejected the
request before execution. It carries the structured errors, the status and the media type,
and is distinct from `GraphQLExecutionError`, which means execution ran.

Only UTF-8 is assumed. Another declared charset is decoded when Python knows the codec, and
otherwise becomes a transport error naming it.

### Operational limits

- `ClientConfig.timeout` accepts a float applied to all four phases, or a
  `Timeout(connect, read, write, pool)` value. The default is 30 seconds per phase.
- Every timeout has one domain: a real number greater than zero and at most 1e6 seconds
  (about 11.5 days), or `math.inf`, which means "no limit". A `Timeout` phase may also be
  `None`. A larger finite value cannot become a socket deadline on every platform, so it
  is refused rather than read as "no limit". The tightest platform is Windows, where
  CPython refuses a socket timeout above `INT_MAX` milliseconds (about 24.8 days). NaN, zero, any negative value (negative
  infinity included) and a value above the maximum raise `ValueError`. A value that is
  not a number, `None` given as the per-call option included, raises `TypeError`. Only
  an omitted per-call option means "use the configured timeout". The domain holds for
  the value after conversion to `float` as well as before it, so a positive value that
  rounds to zero, such as `Fraction(1, 10**400)`, raises `ValueError`. Every accepted
  value, `Timeout` phases included, is used as that `float`. The check runs before any
  I/O, on `ClientConfig.timeout`, the per-call `timeout` option, and the `timeout` given
  to `HttpxTransport`'s constructor and to its `send()`.
- `Transport.send()`'s mandatory per-call `timeout: float` (SPEC.md 5.6) is a ceiling on
  each of the transport's own four configured phases, not a replacement of them: the
  request actually sent uses `min(configured_phase, call_timeout)` for connect, read,
  write and pool individually. A phase-specific `ClientConfig.timeout`/transport-constructor
  value therefore keeps governing a request whenever it is already at or under the
  per-call budget, and only a phase configured looser than that budget gets clamped down
  to it. This is the one combination rule that needs no information beyond what
  `HttpxTransport.send()` already has on hand: it never has to guess whether the caller's
  scalar is a deliberate override or a passed-through default, because it does not matter
  to a ceiling either way.
- The client's own side of that rule: `ClientConfig.call_timeout(override)` produces the
  scalar every call hands to `send()`. A per-call `timeout` option is used exactly as
  given, which is how one call tightens a configured phase. With no per-call option, a
  scalar `ClientConfig.timeout` is passed through, and a phase-specific one produces the
  largest configured phase, which is the smallest scalar that clamps no phase the project
  set. A phase left at `None` is a deliberate "no limit", so any `None` phase produces no
  bound at all rather than a value the ceiling would impose. The client therefore never
  shortens a configured phase on its own: only an explicit per-call `timeout` does that.
  At the transport, an unbounded phase reaches `httpx` as `None`, its spelling of "no
  limit". An infinite number never reaches it, because a socket rejects one.
- Retries are connect-failure only. `max_attempts` defaults to 3, so at most two retries.
  Backoff is `min(0.1 * 2 ** (attempt - 1), 2.0)` seconds with full jitter. A request that
  reached the server is never retried.
- `max_response_bytes` defaults to 32 MiB. Reading past the cap raises
  `GraphQLTransportError` without buffering the rest. The cap counts decompressed bytes.
- `trust_env` defaults to `False`, so ambient `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`,
  `SSLKEYLOGFILE` and netrc are ignored unless a project opts in through
  `ClientConfig.trust_env` or `--gql-trust-env`. This keeps runs reproducible and stops
  requests from silently routing through an ambient proxy. With `trust_env=True` the
  transport resolves the proxy variables itself, with `httpx`'s own reader and matching
  rule, `NO_PROXY` included, and routes inside the shared pool, so every client derived
  from it uses them. `httpx.Client` cannot do this here, because it ignores the environment
  whenever it is handed a transport.
- `ClientConfig.proxy` and `ClientConfig.verify` follow `httpx` semantics. An explicit
  `proxy` takes every request and outranks the environment. A proxy URL the transport
  cannot use is refused with a message that names the problem and never quotes the URL,
  because the URL is where a proxy password lives. `verify` accepts `True`, a CA bundle
  path, or an `ssl.SSLContext`. `verify=False` emits a warning.
- Redirects are not followed.

### Derivation

Derivation is an optional capability, opted into by inheriting one abstract base:

```python
class DerivableTransportBase(ABC):
    @abstractmethod
    def derive(self) -> Transport: ...
```

Detection is `isinstance` against that base, which is a type guard, so the returned bound
method is fully typed. A wrong signature is a type-checker error where the subclass is
written, not at a distant call site. The library never registers virtual subclasses, so
opting in is by inheritance only.

A transport that does not inherit the base is shared, whatever methods it has, and an
unrelated member named `derive` is never looked up. A transport implementing only `send()`
and `close()` constructs, clones and closes with no extra method. The public `Transport`
protocol is unchanged.

`HttpxTransport.derive(*, own_pool: bool = False)` returns a transport whose `close()`
closes its own client and cookie jar over a non-closing pool wrapper and leaves the pool
open. With `own_pool=True` the derived transport closes its own client and then the root
pool, exactly once. Only the factory that created a root pool may pass `own_pool=True`, and
only to one client.

---

## 7. Diagnostics and sensitive data

The leak boundary is rendering, not the object. Auth and middleware must see real header
values to do their work. Nothing else should.

### Boundary

- `RequestInfo` keeps the real values and stays the type passed to `Auth.apply` and
  `Middleware.before_request`. It gains no representation that shows them.
- `RequestInfo.redacted()` returns a frozen `DiagnosticSnapshot`. Only a snapshot reaches an
  exception, a pytest report section, the diagnostics recorder, a log record, or
  `as_curl()`. `__repr__` and `__str__` on `RequestInfo`, `GraphQLResponse` and every
  exception render the snapshot form.
- A recorded call holds a snapshot and finished text, never a live request or a response. The
  finished text is the one-line summary of each of the first `max_recorded_errors` errors, a
  cut excerpt of the response data, and the `as_curl()` command. Each is built where the live
  request is in hand, because that is the one moment its secrets are known, and a recorded
  call is the only form the report, the log and the failure section read.
- `ClientConfig` is plain data that no redaction stage runs over, so its `repr()` leaves
  out every field that carries a credential: `headers`, `schema_headers`, `cookies` and
  `proxy`.
- Every request the client builds lists all four of those credentials in its secret set,
  whichever of them the call sends, and so does the request that loads the schema. The header
  fields go through the header rule, so one name can hold a different value in each, and a
  per-call header that replaces a configured one does not remove the configured value from
  the set. A cookie value and what the proxy makes the pool send are always credentials. This
  matters because text built from a request is scrubbed and then cut: a request that did not
  know a credential could not remove it, and a cut through it would leave its start in the
  text.
- A credential that an earlier call sent is also in the secret set of every later call,
  because a server can repeat a value it was sent and a cap can end inside it. The recorder
  carries a ledger of the credentials that the calls sent: the real header values, redacted
  variables, URL userinfo and query values, and the cookies a response set. Each call puts
  its own into the ledger as the request goes out, after middleware, and the text built for
  that call is scrubbed with the ledger as well. The ledger belongs to the recorder, so
  every clone of a client shares it, and `clear()` leaves it alone. The plugin gives the
  recorder of every test the same ledger, one for the session, because a server can repeat
  a value in a later test as well as in a later call. The transport is handed
  the request with the ledger, because it cuts the text of its own errors. It holds each
  value once, at most 256 values and 262144 characters, and drops the oldest value first,
  keeping the newest even when it alone is over the size. It has no text form of what it
  holds, and nothing renders it.
- A ledger that dropped a value cannot vouch for any text, so it withholds it. Nothing can
  scrub a value that is not known, and a server can repeat one the ledger dropped: a cap
  would leave the start of it, and no cap would leave all of it. The ledger remembers that it
  dropped a value, for good. Every request built from it after that carries one entry in
  `transport_credentials`, `HISTORY_GAP`, which has no value, is not a credential, and is
  never held or rendered. While a request carries it, the free-form text that comes from
  outside the request is replaced by a fixed notice: the text of an error summary, a transport
  failure, a quoted body, a data excerpt, a diff value, and `RequestInfo.scrub()`. An empty
  text stays empty. The cap still applies to the notice. The fields of the request itself are
  not withheld, because the client built them, and neither are the plain values of a
  `GraphQLErrorInfo`, which a test matches. A request with `redact_values=False` is not
  scrubbed at all, so nothing is withheld from it. The plugin's ledger covers the session, so
  in a suite that sends more distinct tokens than the ledger holds, the text of a server is
  withheld from the call that dropped the first value onward. A call recorded before that
  keeps its text. Text that code of the project wrote is raw, so it cannot be scrubbed and
  nothing can vouch for it once there is a gap: the result of a `pytest_graphql_report_section`
  implementation is replaced by the notice, and so is the whole matcher diff of a failed
  `actual == matcher`, because a matcher of the project can print text the renderer never
  scrubbed. Both obey `withhold_if_gapped`, the one function for text of that kind, and
  like every other withholding it does nothing for a request with `redact_values=False`.
  Whether a request withholds is decided in one place, `has_history_gap`, which checks that
  opt-out first. A credential that a client with another recorder sent is not in the set,
  and nothing tells this client of it. The set is checked whole where a report renders several calls.
- A `GraphQLErrorInfo` keeps the server's `message`, `path` and `extensions` as sent, so
  a test can match them, and its `repr()` shows none of that text raw. `message` and
  `extensions` are left out. The `repr()` showing `path` is built once, when the response
  is built: each string segment goes through the scrub of the request that produced the
  error, and then the finished text is scrubbed again as a whole, because a server can
  echo a credential into a path as easily as into a message, and the tuple's own
  quoting can complete one across two segments.
- A `GraphQLErrorInfo` also keeps one summary line, `CODE at ["path"]: message`, built when the
  response is built, because a response holds only a digest-only snapshot and cannot scrub
  server text afterward. Each piece goes through the request's scrub, escape and cut, and the
  joined line is scrubbed again. Only the first `max_recorded_errors` errors get one. Every
  failure text that shows server errors, which is the unmatched-filter message of
  `expect_error`, is made of these lines and never of a raw message. A failure text is also
  checked line by line against the snapshot of each request it names, and a failing line is
  replaced with the withheld notice on its own, so one hostile entry cannot hide the rest. The
  finished message then follows the exception rule below. A server message with a newline
  continues under its own entry's indent, so it cannot pass for another entry.
  `WaitTimeoutError` shows the last swallowed exception as its class name and message,
  escaped and cut at `max_diagnostic_bytes`, and checks that line against the snapshot of the
  attempt that raised it and of any request or response the exception carries. An exception
  from an attempt with no snapshot is shown as its class name and the withheld notice. The
  same rule governs every exception a failure attaches as its cause: it is attached only after
  its whole chain was checked against a snapshot, and never when none is known.
- `repr()` is a further transform: it doubles every backslash and escapes quotes after the
  last scrub ran, so text with no secret in it can render one. Every value field the
  library builds, in a snapshot, an excerpt, a scrubbed error object or a recorded call,
  is therefore also checked under `repr()` where it is built, and is replaced with the
  hardened marker when its `repr()` would contain a qualifying secret. Keys and header
  names are output too, so the finished snapshot checks each of them under `repr()` in one
  pass, and a numeric suffix keeps two replaced keys apart instead of merging them. An
  exception's `repr()` shows its message as it is, without escaping it again.
- A field checked on its own cannot see the text a representation adds around it: a class
  name, a field name, a separator, a key joined to its value, or the fixed text between two
  recorded calls. A qualifying secret can equal or span that text. Every representation the
  library composes from a snapshot is therefore checked whole before it is returned: the
  `repr()` and `str()` of `DiagnosticSnapshot`, `RecordedCall` and `GraphQLResponse`, and the
  recorder dump, as well as `RequestInfo.__repr__` and `as_curl()`. A snapshot does not keep
  the qualifying set. It keeps keyed digests of each value, under a key drawn once per process
  and never stored with them, which can check text for a value but cannot show one. When the
  check finds a value, the renderer raises `DiagnosticRenderError`. The recorder dump checks
  each call's values against the whole dump, because a value can span from one call's lines
  into the next.
- An exception cannot refuse to exist, so every exception built from a request checks its
  message instead. This includes the `DiagnosticRenderError` a refusal raises, which is a
  public rendering too. The check covers each complete text the exception shows: its
  `repr()`, the `repr()` of its `args`, and the last line of a Python traceback, which adds
  the module path and `: `. A failing message is replaced with a fixed withheld notice, and
  when the notice fails too, the exception has no message. When even the empty `repr()`
  would show a value, the `repr()` is the class name alone. A value contained in the class
  name or its module path cannot be hidden, because Python's own traceback prints them. An
  execution error whose message would include the response shows
  the withheld notice in place of a response `repr()` that refused to render, so the caller
  still receives the error it expects.
- Python's own representation of a container taken out of a snapshot field, such as
  `repr(snapshot.variables)` or `repr(snapshot.omissions)`, is checked through its content but
  not through its wrapper text: `mappingproxy(`, brackets and separators are Python's, not a
  representation the library builds.

### Name-based and path-based redaction

- Header names match `redact_headers` case-insensitively after stripping. The default is
  `authorization`, `cookie`, `x-api-key`, `proxy-authorization`.
- `ClientConfig.redact_variables` holds dotted paths and glob patterns, matched
  case-insensitively against snake_case variable paths at every depth, inside input objects
  and inside lists. A pattern matches a contiguous tail of the path, counted from its end: a
  dot-free pattern is a one-segment tail (the field's own name), and an N-segment dotted
  pattern is an N-segment tail, so both forms apply at every depth rather than only when the
  pattern names the complete path from the document root. The default is `password`, `token`,
  `secret`, `api_key`, `access_token`, `refresh_token`, `authorization`, `otp`, `pin`,
  `credit_card`, `ssn`. The same patterns apply to captured response data.
- URL userinfo and the query string are stripped from any recorded URL.
- Cookies are recorded by name only, never by value, on both the request and the response
  side.

### Value scrub for free-form text

A server error message, a body excerpt or an exception message has no path, so path-based
redaction cannot cover it. The scrub closes that gap.

- Secret set. For each request and response pair the redaction stage collects the values it
  already knows are sensitive: every header value matched by `redact_headers`, the value at
  every variable path and response path matched by `redact_variables`, every cookie value,
  and the userinfo and query-string values stripped from the URL. It also holds every
  credential the transport sends outside the request's headers: a proxy's username, its
  password, the pair they form, the `Proxy-Authorization` value built from them, and the
  value of every header the proxy was given, each value of a repeated header on its own,
  as it goes on the wire. They also include every cookie value the transport's own jar
  sends in its `Cookie` line, and every value a response sets in a `Set-Cookie` line, under
  either `cookie_scope`. Those are built below the request's own redaction boundary, yet a
  proxy or server can quote them back into a body the transport excerpts or into a GraphQL
  response, so they join the set whatever `redact_headers` says, and the transport hands
  them back with the response so the client renders it with the same set. Target URL
  userinfo is sent as an `Authorization: Basic` value built from the decoded pair, which
  no spelling of the URL contains, so the request derives the pair and that header value
  from its own URL and adds them beside the two halves, for every renderer alike. The set exists
  only inside the stage. It is never stored on a snapshot, never logged, and never returned
  by a public API.
- Derived forms. For a value carrying a scheme, such as `Bearer <token>`, the part after
  the first space is added as well. A value carries a scheme when the text before its
  first space is an RFC 9110 `token`, which is the grammar of an auth scheme. That holds
  for a header or a variable alike, since a variable such as `authorization` can hold one.
  The JSON text of a list or object never starts with a token, so it gets no such form:
  its tail after the first space would be a fragment such as `"b"]`, and replacing that
  would cut quotes and brackets out of the text around it. The percent-encoded form of each value is added.
  Non-string values are converted with the same JSON text form used for rendering before
  being added. URL userinfo is added in both its original, still-percent-encoded spelling
  and its decoded form. A percent-escape's hex digits are case-insensitive, so matching
  itself case-folds a percent-escape's two hex digits on both sides of the comparison
  before comparing, rather than enumerating every case spelling a value's own escapes could
  be written in: that space is exponential in the number of escaped octets, so an
  enumeration approach could never cover it, while case-folding the comparison covers every
  spelling in one pass.
- Source label safety. A match's replacement is `[redacted:<source>]`, so a source label
  that itself contains another value already in the secret set would place that second
  value into the text the first value's redaction produces, undetected by the single-pass
  rule above. The fixed template text around the label, `[redacted:` and `]`, is itself
  compile-time, public text, so a colliding source label is not the only way this can
  happen: a qualifying secret can equal a substring of the template text directly,
  independent of the label, so the label cannot be checked on its own. The complete marker
  is built and validated as one string, using the same case-folded percent-escape
  comparison the scrub itself uses, and every marker-emitting path -- a redacted header's
  own placeholder and a redacted variable subtree's own placeholder alike -- is built by
  this same validated construction, never assembled separately from an unchecked label.
  The validation checks the marker's *escaped* form, not its pre-escape spelling: escaping
  always runs over a marker after it is inserted into the surrounding text (see "Stage
  order, escaping and limits" below), and a raw control or hidden character carried in from
  a label built from request-controlled text would otherwise pass a pre-escape check and
  only turn into a byte sequence matching a different qualifying secret once escaping
  expands it. No fixed, compile-time replacement can be a guaranteed-safe substitute for a
  colliding marker: an attacker who reads the implementation can always choose a secret value
  equal to whatever constant it names, so a marker that still collides after a fixed fallback
  label is tried escalates through a bounded number of labels drawn from a cryptographically
  random source, each checked the same way and generated one at a time -- only once every
  candidate tried so far has collided, so the ordinary, already-safe case draws no randomness
  at all. A secret set that covers every short string the random source can produce makes
  every one of those attempts collide too; when the bound is exhausted, this falls back first
  to a label-free marker and finally to an empty replacement rather than retry without limit,
  so constructing a safe replacement always terminates and never emits a known secret. The
  label itself is escaped before it is ever placed in the marker template, not after: the
  marker can be inserted by a scrub pass that runs after escaping and applies no escape pass
  of its own afterward, and a raw control character carried in from an un-escaped label would
  otherwise reach a snapshot field exactly as-is, a plain escape-stage violation regardless of
  whether it also happens to spell a second secret.
  Escaping's per-character map has no cross-character lookahead, but checking a candidate's
  escaped form in isolation is only as strong as checking the real, final rendered text when
  escaping is the *last* transform that text passes through -- and a marker is not the only
  text this risk applies to. Any scrubbed-and-escaped text can have escaping synthesize a
  qualifying secret's spelling this way, not only a constructed marker, so every field's
  complete construction runs its scrub, escape and a second scrub together as one step,
  closing this wherever text reaches a snapshot field. A further transform can still run
  after a field is built -- `repr()`'s and `json.dumps()`'s own backslash-and-quote doubling in
  the object's `repr()` and in `as_curl()`'s JSON body, and `shlex.quote()`'s own quote-doubling
  in `as_curl()`'s complete command -- and can synthesize a collision the same way from a field
  that was already safe before that call touched it. Re-scanning that call's own already-
  produced text cannot close this safely: the match can span and remove a delimiter the call
  itself just produced, since a qualifying secret's raw text is exactly as attacker-controlled
  as anything else here and nothing stops it from being chosen to equal that call's own syntax
  -- for example a secret equal to the shell-quote-and-JSON prefix or suffix of `as_curl()`'s
  `--data` argument, whose removal exposes the argument's own content, unquoted, to the shell.
  Every value `repr()`, `json.dumps()` or `shlex.quote()` will see is therefore checked against
  that same call's own output *before* the call runs on the real complete structure, one leaf
  at a time; a leaf whose own rendered form would contain a qualifying secret is replaced
  first, with one shared marker built only from fixed, syntax-neutral text -- ASCII letters,
  digits and the template punctuation, never a request-controlled label -- so the structure
  that reaches the real `repr()`, `json.dumps()` or `shlex.quote()` call is already safe.
  A leaf-level check cannot see everything the complete output is made of, though. The fixed
  wrapper text a renderer adds around its leaves -- `RequestInfo.__repr__`'s own class name and
  dataclass field syntax, two independently quoted shell segments joined into one `as_curl()`
  word -- is not itself a leaf, and a qualifying secret can equal or span that text regardless
  of what any leaf's content is; the same is true of text generated only after every leaf check
  has already run, such as a mapping key's disambiguating suffix once two distinct keys
  collide under the same render call. Substituting the whole value is also not a safe strategy
  for `as_curl()`'s complete `--data` JSON document, since replacing an already-valid document
  with a marker string would discard the structured request body the command promises to
  reproduce, rather than preserve it the way replacing one field's value does. The complete
  text a caller is about to return -- `__repr__`'s finished string, `as_curl()`'s finished
  command -- is therefore validated once more, as a whole, immediately before it is returned.
  This is a validate-only backstop, never another substitution pass: finding a qualifying
  secret in the complete text at this point means no per-leaf substitution could have closed
  it, because the only remaining sources are syntax the format cannot omit or a document that
  cannot be rewritten without corrupting it. When that happens, `RequestInfo.__repr__`,
  `as_curl()` and every other representation named under "Boundary" raise
  `DiagnosticRenderError` instead of returning unsafe or malformed text. The
  exception's own text is fixed, compile-time text and is exactly as exposed to this risk as any
  other fixed rendering syntax. Its `renderer` label is stored on the exception, so the label is
  replaced with an empty string when it contains a qualifying value; a qualifying value is never
  the empty string, so the empty label can never repeat one. Its message is not checked field by
  field: it follows the rule for every exception built from a request ("Boundary" above), which
  checks each complete text the exception shows and falls back to the withheld notice, then to
  no message. `DiagnosticRenderError`
  is part of the top-level exception hierarchy ("Top-level surface" above) and is exported from
  `pytest_graphql`. The ordinary case, where no qualifying value collides with fixed or generated
  syntax, never reaches this path at all.
- Minimum length. Only values of at least `ClientConfig.min_redacted_value_length`
  characters after stripping whitespace enter the set. The default is 8. A shorter value is
  still redacted at its own path, but it is not scrubbed from free-form text, because
  replacing a very short string across a message corrupts unrelated text and hides real
  failures. This limitation is documented on the security page, together with the
  recommendation that test credentials be long enough to be scrubbable.
- Replacement. Values are applied longest first, so a longer secret containing a shorter one
  is replaced first. Matching is a single left-to-right pass, so replacement output is never
  rescanned and a placeholder cannot be produced from another placeholder. A match becomes
  `[redacted:<source>]`, where the source is the header name or the variable or response
  path it came from. When one value has several sources, the first in sorted order is used.
- Evasion. Before matching, a normalized copy of the text is built with invisible and
  bidirectional characters removed. A match found only in the normalized copy replaces the
  corresponding span of the original, so a server cannot defeat the scrub by splitting a
  token with a zero-width character.
- `ClientConfig.redact_values` defaults to `True`. Setting it to `False` disables the scrub
  only. Name-based and path-based redaction still apply.

### Stage order, escaping and limits

The stages run in this order: collect, scrub, escape control characters, then truncate.
Truncation runs last so a cut cannot leave part of a secret behind, and escaping runs after
the scrub so an escape sequence cannot break a match.

Before display, every ASCII control character except tab and newline, every character in
the U+0080 to U+009F range, DEL, and every character the publication-hygiene scanner
classifies as invisible or bidirectional is escaped, so a hostile server error cannot
rewrite terminal output or hide text in a report. The escape form follows the code point:
`\xNN` below U+0100, `\uNNNN` below U+10000, and `\UNNNNNNNN` at or above U+10000. The
eight-digit form is required, because the scanner also classifies the Unicode tag
characters at U+E0000 to U+E007F, which no four-digit escape can represent.

`max_diagnostic_bytes` caps each snapshot field that carries request-controlled text or
structure, default 4096, with a total cap of 32768 per snapshot that the true rendered
grand total never exceeds. A field's cap bounds its complete rendered form: every byte
that would appear in its JSON representation counts against the cap, including mapping
keys, list structure and a non-string value such as a number or a boolean, not only a
string leaf's own characters, and a string's cost is what `json.dumps` actually encodes it
as, escaping included, never its raw character or UTF-8 byte count. Fields are capped in a
fixed order: operation, document, url, method, headers, then variables, and each field is
capped to whatever remains of the total budget after the fields before it.
`DiagnosticSnapshot.curl_headers` shares the `headers` field's one budget; it is the
ordered sequence that budget is built from, not a second, uncapped field of its own.
`kind`, `idempotent` and `truncated` carry no request-controlled text; their size is fixed
by the type or generated by the capping stage itself, so this cap does not apply to them,
and a truncation note names only the field and a byte or entry count, never the key or
value content that was cut. An omitted entry -- a scalar, key, header or subtree that could
not fit -- is counted by exactly one layer, the container that holds it, so it is never
counted twice.

A field limit below the smallest representable form of its type (an empty string, an
empty mapping) does not omit a mandatory field: `operation`, `document`, `url`, `method`,
`headers` and `variables` each still render that smallest form when nothing else fits, and
the form's unavoidable byte cost is still charged even past the field's own nominal share,
so a later field's budget still reflects this field's true cost. Every field but the last
processed has this floor reserved out of the shared total before the current field may
spend beyond its own floor, so one oversized field can never leave a later field's forced
minimum unaccounted for; this is what keeps the true grand total inside the documented cap
rather than merely each field's nominal share. A header entry is different: it is a member
of a container that can legitimately hold fewer entries than it started with, so it is
never force-rendered past what its own structural minimum -- its overhead plus an empty
name and an empty value -- can afford. A header whose bounded name or value does not fully
fit in what remains, once that minimum is confirmed to fit, is truncated in place like any
other field; a header whose own structural minimum cannot fit at all is dropped along with
every header after it, the same "entries dropped" outcome already defined for a mapping or
a list. The header's curl placeholder variable is bounded against this same shared budget
by its own derived length, not by the source name's JSON cost: one raw character can expand
into several once the variable-naming mapping below escapes it, so a name bounded by JSON
cost alone does not bound the variable it feeds. That mapping is deterministic and
injective, so only the complete, untruncated variable is ever charged: truncating the
variable's own text is not injective, since two different, unrelated names can share the
same truncated prefix, which would send one header's exported secret in another header's
place. When the complete variable does not fit, or when no budget remains for even the
variable's fixed prefix, the header renders its already-bounded placeholder text literally
instead of an environment-variable indirection, which is still safe because that text names
no real secret. The error list truncates at `max_recorded_errors`, default 20. Truncation
is visible, never silent, and states how many bytes or entries were cut. The diagnostics
recorder is a bounded `deque`, default 50 calls, set by `ClientConfig.max_recorded_calls`,
and the plugin clears it per test. The credential ledger in section 7 is bounded too, in
values and in characters, and text is withheld once it drops a value. A client records exactly one call for every request it
hands to the transport, and records it before any exception leaves the call. That includes
a call that fails after the transport returned, through a response that contradicts the
schema or a raising `after_response`, which is recorded as failed with its status code. A
call refused before it is sent records nothing. Recording never replaces the exception the
caller receives: a failure whose message cannot be rendered is recorded by its type name.

The data excerpt of a recorded call follows the same stage order. A key whose path matches
`redact_variables` shows `[redacted]` in place of its value, whatever shape the value has, so
the patterns that apply to variables apply to response data too. Every other key and string
passes the request's scrub and the escape, and the finished text passes the scrub once more,
because the JSON escaping adds backslashes that can spell a secret. The cut comes last. The
text is the JSON `json.dumps` writes with its default separators, with `...` for what was left
out, cut on an entry boundary so a cut never leaves part of a secret behind. A value that fits
whole is never cut. The text never exceeds `max_diagnostic_bytes` (default 4096) for one call,
for any limit from zero up: a limit below the three bytes of `...` shows as much of it as
fits, and a negative limit is no room at all. That is the one size option section 7
already has, and no option is added for the data. A string too long for the room left is
shortened with the visible note `... (truncated, <n> byte(s) cut)`. The field count covers the
whole value and is counted without copying it. The excerpt is checked once against the
snapshot before it is kept, and a refusal keeps the withheld notice instead of the call
failing.

`as_curl()` quotes every literal component with `shlex.quote`. A redacted header is not a
literal component. It renders as two adjacent quoted segments that form one shell word and
one `-H` argument: the header name and its `": "` separator passed through `shlex.quote` as
data, then a double-quoted parameter expansion. For the `authorization` header that is
`-H 'authorization: '"${PYTEST_GQL_HEADER_AUTHORIZATION}"`, so the command stays runnable
after the user exports the value. The two segments are never emitted as two arguments.

A header name is never placed inside double quotes and never left unquoted. RFC 9110
defines a field name as a token, and its `tchar` alphabet already contains `$`, an
apostrophe and a backtick. It excludes `;`, a double quote and a space. `httpx` is more
permissive than the grammar and also accepts nonconforming names that carry those three
characters. Inside double quotes a shell expands `$HOME` and runs a backtick command
substitution, so a diagnostic the user copies and runs would execute text taken from a
field name. Quoting the name as data removes that. The expansion is double-quoted, so the
exported value is not word-split or glob-expanded.

The variable name comes from the ASCII-lowercased field name by one deterministic and
injective mapping. An ASCII letter becomes its uppercase form. An ASCII digit stays. Every
other character becomes an underscore, then the uppercase hexadecimal of its UTF-8 bytes,
then a closing underscore. The result is prefixed with `PYTEST_GQL_HEADER_`. So `x-api-key`
gives `PYTEST_GQL_HEADER_X_2D_API_2D_KEY` and `x_api_key` gives
`PYTEST_GQL_HEADER_X_5F_API_5F_KEY`. Two field names that differ after ASCII case
normalization never share a variable, so a generated command cannot send one header's
secret in another header's place. Case variants of one name share a variable on purpose,
because HTTP field names are case-insensitive. The `_HEADER_`
segment also keeps these names out of the `PYTEST_GQL_` option namespace defined in
"Configuration and call grammar".

`shlex.quote` is never applied to a whole argument that carries a placeholder. It would
produce a single-quoted string, and a shell does not expand a variable inside single
quotes, so the command would print the literal text instead of the exported value. No
option prints a live credential.

The suite renders a header for every character RFC 9110 permits in a field name, and also
for nonconforming names carrying `;`, a double quote and a space that `httpx` still
accepts. On a POSIX platform it runs each generated command through `/bin/sh` and asserts
that the name arrives unchanged and that no expansion or substitution ran; the check is
skipped where no POSIX shell exists, since it verifies `as_curl()`'s output against a POSIX
shell by design, not against the platform running the test suite. It also asserts that `x-api-key` and
`x_api_key` resolve to different variables, and that `X-API-Key` and `x-api-key` resolve to
the same one.

### Coverage

No text reaches a snapshot without passing the scrub. That includes GraphQL error messages,
rendered error extensions, response body excerpts, exception messages, the recorder dump,
log records, pytest report sections and the `as_curl()` output.

An adversarial leak suite plants a known credential in a header, a top-level variable, a
nested input object, a list element, response data and a server error message, and asserts
it appears in no output path. It covers a credential echoed inside a body excerpt that is
then truncated, one secret value that is a substring of another, a value shorter than the
minimum length with its documented behavior asserted, and a token split by a zero-width
character.

---

## 8. Connection and identity isolation

Only connection pools are shared. Cookie jars, headers, limits and identity state are per
client.

- What is shared. One session-level `HttpxTransport` holds a single `httpx.HTTPTransport`
  pool. That pool, and nothing above it, is what every client reuses. The async form shares
  an `httpx.AsyncHTTPTransport` the same way.
- What is not shared. Every logical client builds its own `httpx.Client` over that shared
  pool, with `follow_redirects=False`, its own cookie jar, its own header defaults and its
  own limits. A `Set-Cookie` response can never reach another client.
- Cloning. `with_headers()` and every other clone helper derive their own transport from the
  same pool when the transport supports derivation, so a clone starts with an empty cookie
  jar. Cookie state is identity state and is never inherited. Closing a clone closes only
  that clone's derived transport.
- Cookie isolation is a property of the HTTPX transport, not of the client. A custom
  transport is responsible for its own identity state. A fake transport holds no cookie jar,
  so sharing one between clones is safe.
- `ClientConfig.cookie_scope` defaults to `"none"`, which clears the client's jar after every
  response, so no `Set-Cookie` survives a call. `"client"` keeps the jar for that logical
  client only.

Header precedence, lowest to highest: `ClientConfig.headers`, the `gql_headers` fixture,
`Auth.apply`, `with_headers()` in clone order, then per-call `headers=`. Userinfo in the
target URL sits below all of them: it supplies `Authorization: Basic` only when no layer set
`Authorization`, and an explicit header is what goes on the wire. The transport decides
this itself, because `httpx` would otherwise replace the explicit header. Names compare
case-insensitively after stripping, and a later source replaces an earlier one for the same
name instead of adding a second line. A genuinely repeated header uses an explicit list
value.

The plugin builds the `gql_headers` layer into the per-test client's `ClientConfig.headers`,
merged over the session value, which puts it below `Auth.apply` and above the ini and
environment headers. It first pins `schema_headers` to the session value, so a per-test header
can never become the identity the session schema was loaded with. The `gql_auth` fixture may
return a string, which is a bearer token, as `as_()` does.

Schema identity is explicit. `ClientConfig.schema_headers` defaults to
`ClientConfig.headers`. Schema loading uses it alone, and the function-scoped `gql_auth`
fixture does not affect it, because the schema is session-scoped. A schema that varies by
role is out of scope for v0.1, and the documentation says so. Such a project overrides the
schema source fixture with its own role-keyed cache.

Cross-test isolation is tested: two tests with different auth must not observe each other's
cookies or headers, and two clients with different auth must share a connection pool and
share no cookies.

---

## 9. Ownership and failure reporting

This is the most safety-critical contract in the project. It has one normative statement,
and the sections below are it.

### 9.1 Ownership

`owns_transport` is a constructor argument, never a type check, and it is readable on the
client. It is true exactly when closing the client closes the transport the client holds.
The cleanup list is the only close mechanism. When no list is supplied the flag builds it.
When a list is supplied it is already complete, and the constructor never appends to it.

| Path | Root pool owner | Client `owns_transport` |
|---|---|---|
| `build_client()` building its own transport | the factory's cleanup list, which the returned client inherits, through `derive(own_pool=False)` | `True` |
| `build_client(transport=...)` with an injected transport | the caller | `False` |
| session transport fixture | the fixture, closed at session teardown | not applicable |
| per-test client fixture over that pool | the session fixture | `True`, for its derived transport only |
| clone of a client on a derivable transport | unchanged | `True`, for its derived transport only |
| clone of a client on any other transport | unchanged | `False` |

A client never calls `derive()` itself. A clone owns a transport only when it derived one.
An internal caller that supplies a cleanup list must supply one that closes the transport it
declares. There is exactly one such caller, the factory, and tests hold that invariant
rather than the constructor.

No supported path creates a pool that nothing closes, and no path closes one twice.

### 9.2 Adoption before acquisition

Registration happens before acquisition, not after a constructor returns. An internal owned
wrapper is adopted by the cleanup list before it acquires anything, so no interrupt landing
between acquiring a resource and recording its owner can strand it. The wrapper is generic
over the acquisition callable's parameters and over a result bound to a `close()`-bearing
protocol, so the call site keeps its argument types and no `Any` crosses the transport
boundary. A wrong argument, a missing argument, or a result with no `close()` is a strict
type-check error.

The transport constructor follows the same rule for the pools it builds. Each pool, the
direct one and one per ambient proxy, is adopted by the constructor's own cleanup list
before it is built. A failure before the transport's `httpx` client exists closes every pool
already built, exactly once. A pool the constructor was given, as a derived transport is, is
not its own and is never closed by its failure.

### 9.3 One sweep, one close authority

There are six releasing call sites: the client, the factory unwinding path, the unwinding
path that builds a client over a derived transport (a clone or a per-test client), the
transport constructor unwinding path, the proxy router closing its pools, and the session
transport fixture's teardown. They share one sweep and one report implementation, so a later
correction cannot reach one and miss another. A transport derived for a new client is
adopted before it is derived, so a failure while that client is built closes it.

- Every item is attempted exactly once, in reverse order, whatever the earlier ones did. A
  failing transport teardown cannot strand the root pool.
- The exception that wins is chosen before the chain is built.
- A `BaseException` that is not an `Exception` always wins. An interrupt is raised as itself
  rather than chained behind a transport error, so an `except Exception` around the call
  cannot swallow it.
- Among ordinary exceptions the caller's preferred exception wins. The client prefers the
  last failure in sweep order, which is the earliest-acquired resource, and so do the proxy
  router, whose direct pool is built first, and the session fixture's teardown. The factory,
  the derived-client path and the transport constructor prefer the construction failure,
  because that is why the caller's call failed.
- `close()` is idempotent, and so is leaving the context manager twice. The closed flag is
  set before the sweep, so a second close adds no calls even when the first raised.
- `BaseExceptionGroup` is not used, because it arrives in Python 3.11 and the floor is 3.10.
  Behavior that differs across the supported matrix is worse here than one raised exception
  with the rest reachable from it.

### 9.4 The report

Five properties govern reporting.

**Bounded.** Every walk of an exception graph is bounded by a set of visited identities,
because a caller can hand back an exception whose context chain is cyclic and Python permits
that.

**Non-destructive.** No link that carries diagnostic state is overwritten unless what it
held stays reachable afterwards, because a cleanup failure can arrive with a cause and a
context of its own.

**Sealed.** Everything that must survive is placed where the coming `raise` cannot reach it,
because a `raise` inside an active `except` block replaces `__context__` on the exception it
raises whatever that link already held. When the slot that survives the raise has been
refused, or holds an explicit cause whose chain refuses the report, what is left is the
exception the raise itself writes into that link, so the report hangs under that one instead
of above it. Before the seal is written, the reported exception is taken out of every link
into it that a walk of the graph sees, not only the printed one. A failure raised `from`
another while the factory handles its construction failure carries a context link into that
failure under its own cause, and such a link holds only the exception that becomes the head,
so cutting it drops nothing. An explicit cause of the reported exception is never spliced into
the chain under it, because the reported exception keeps that cause and reaches it already.

**Hook-independent.** Reporting reads and writes only the storage the interpreter itself
uses for these names, taken from `BaseException`, and stores its record in the real instance
dictionary through unbound accessors. The exception types involved come from a caller's
transport, and on such a type an ordinary read can raise or lie and an ordinary write can be
accepted and drop the value. A subclass hook, a shadowing descriptor or a lying `dict`
subclass therefore cannot hijack a read, a write or the retrieval. The record write is proved
by reading it back.

**Ranked.** No write reporting performs may escape or be forgotten, in this order of rank.

1. An interrupted write becomes the exception that leaves, whatever any channel does, unless
   the selected failure is already an interrupt, in which case the selected one stays and the
   refused interrupt is reported. An ordinary refusal never replaces the selected failure.
2. Every failure the caller's cleanup produced stays in the report while the record channel
   accepts a write. While the record is gone it stays in the rendered chain, subject to the
   two loss shapes in 9.5.
3. Every refusal reporting's own writes produce is itself reported, by a later write than the
   one that caused it, as far as the channels that are left allow, subject to the two
   omission shapes in 9.5.

Reporting asks every question about reachability of the graph the `raise` leaves rather than
the one it finds, because the `raise` writes last and an answer given about the earlier graph
cannot be corrected once it has run. It asks about both public links rather than about the
rendered chain, because an explicit suppression hides a link from a traceback and leaves it
in place for everything that walks the graph itself. That holds when reporting's own write of
the `raise`'s link is refused too: a refusal does not stop the interpreter making the link.

A refusal counts as reported only once it is somewhere the caller reaches, never on the
strength of having been offered a place. The chain is built to be printed, so no placement in
it writes a context link under a populated cause, where the link would never appear in a
traceback. Those links are still links the caller reaches, so a value that every rendering
placement passed over is put in one of them rather than nowhere. That last-resort placement
runs only while the record is not carrying the report, and only into an empty slot the value
does not reach back to, so it closes no cycle.

It runs for the caller's own failures before it runs for reporting's own refusals, which is
the rank above. A link given to a refusal is one a failure cannot then have, so offering them
in the other order would drop a failure to keep a refusal. A value that reaches every empty
slot is held there by some link in its own chain. That link is cut only where what it holds
stays reachable from the head anyway, which is what the non-destructive property allows, and
the cut makes the value a leaf rather than a second head. The link is looked for through the
whole of the value's own chain and not only on the value itself, because a failure raised
while handling another failure leads back through the middle one, and cutting at the value
alone would leave that path in place. One link is cut at a time and the placement is tried
again after each, so no cut is made that the placement did not need. A chain that refused a
write earlier in the same pass is not offered the caller's failures at all, because a chain
that just refused is not a channel right now and the next pass offers them again.

The chain ranks under the record rather than beside it. While the record carries the report
the chain is best effort, and it is never given a cycle the caller's own graph did not arrive
with.

### 9.5 `reported_errors` and the permitted losses

`reported_errors` reads the record back and falls back to the rendered chain, which is where
failures are put when the record cannot hold them. It carries every distinct failure the
report covers, raised one first, including what the raised exception itself already carried,
because the coming `raise` can take that out of the chain. Refused writes are reported too,
so a type that interferes with reporting cannot also hide that it did. A failure that an
earlier report raised keeps everything that report covered: a transport constructor that
fails and closes what it built reports inside the factory's sweep, and a proxy router that
fails to close its pools reports inside the client's, so the outer report takes in the inner
record rather than replacing it.

A single-link exception graph cannot always express every failure at once, and
`BaseExceptionGroup` is unavailable at the supported floor. What can be lost is bounded and
named.

**Two permitted caller-failure loss shapes, and no others.**

1. Every link that could carry the failure is refused, with the record gone.
2. The caller's own graph leaves the chain no room, which happens when the record is gone and
   two or more cleanup failures carry an explicit cause that leads back to the reported
   exception. What is lost is always one of those failures and never any other.

**Two permitted refusal-omission shapes, and no others.**

1. The refusals the final unanswered pass makes after the last record the report carried,
   which have no later write to carry them.
2. A refusal dropped in a run that also loses a caller failure to the second shape above,
   where the chain has no room for either.

A run that keeps every caller failure keeps the refusal too. There is no run in which
reporting loses only its own write failure.

The caller-failure loss set of the second shape is asserted by membership and by count, and
the membership is the part that carries the rule. What is lost is a subset of the cleanup
failures whose explicit cause leads back to the reported exception. No free failure, no
failure that leads back through a context link, no failure carrying a chain of its own, and
nothing the reported exception itself arrived with may be lost. The count is at most one
fewer than that subset holds, and it is fewer when the reported exception or an active
handler brings a link the chain can use. Which member of that subset is lost is not asserted:
its members are alike in structure, and the order the cleanup list happens to hold them in
gives that identity no separate meaning. The first shape and both refusal-omission sets are
asserted by identity and by set equality.

The bound comes from three exhaustive products over reported-exception state, handler state,
record presence, cleanup-failure shape and refused-write ordinal, the widest running to five
cleanup failures. It is a measured bound and not a proof for a sequence of any length, and
nothing is claimed beyond five.

### 9.6 Termination and cost

Reporting is bounded in passes and in write attempts. It writes the record at most four
times. Against a surface that refuses every write, with one cleanup failure, reporting ends
after **56 attempts outside an active exception handler** and **59 inside one**, with an
ordinary refusal and with an interrupt alike. Inside a handler the extra attempts are the
crowning and anchoring writes. Each further cleanup failure adds write attempts and no pass.
A change that moves either number has changed behavior.

Three channel cases bound what the report can promise, measured inside an active handler and
outside one.

- Only the record refuses: nothing is lost, and every refusal reaches the chain.
- Only the chain refuses: every failure the caller produced is still in the record, and the
  refusals left out are exactly those the final pass produced after the last record the
  report carried.
- Both channels refuse every write: nothing can be reported, because there is nowhere to put
  it. This is the documented terminal case.

In all three the selection rule holds and an interrupt still leaves the call.

### 9.7 Residual risks

These are stated rather than claimed away.

- **Cyclic public graphs.** One rule can add a cycle to a public exception graph, and only
  while the record cannot hold the report and the alternative is losing a caller failure. An
  acyclic graph handed in with a working record is handed back acyclic. Every walk in this
  design bounds itself, and so do the `traceback` module and the interpreter's own display. A
  third-party renderer, logger or monitor that follows `__cause__` and `__context__` without
  tracking identities can loop on such a chain.
- **Hostile display.** Both the built-in traceback display and the `traceback` module read
  the chain through ordinary attribute access, so an exception type that lies about its own
  links prints that lie, whatever the real storage holds. Reporting cannot own another type's
  display. `reported_errors` is the channel that does not depend on it.
- **Supported Python coverage.** Termination and display bounds were measured on CPython
  3.14 and 3.11. The rest of the supported matrix is left to CI.
- **Traversal length.** The loss sets are asserted through five cleanup failures. Longer
  sequences are not claimed.
- **Acquisition interrupts.** Two boundaries stay open and are named. The `RETURN_VALUE`
  instruction of the factory, where the client exists, no name outside the frame holds it,
  and the exception table does not cover that instruction. The pytest path removes it,
  because the session transport fixture creates the cleanup list and registers its teardown
  before any step that can fail and then passes the list in. A direct caller of the factory
  keeps it. The second is the two boundaries inside each owned-wrapper constructor between
  the underlying constructor returning and the store into the wrapper. Windows inside a
  third-party constructor sit below this and are not reachable from this code.
- **Unwinding interrupts.** An interrupt on the unwinding path leaves open every item the
  sweep has not yet attempted. At a releasing call site, the boundaries from the decision to
  release up to the call into the sweep strand every item. Inside the sweep, the boundaries
  between one item's attempt and the next strand the items not yet attempted. This is the
  one case where the rule in 9.3 that every item is attempted does not hold. No handler can
  close it, because a handler that catches an interrupt there has an entry of its own. An
  interrupt inside one item's attempt, before its `close()` runs, is that item's close
  failure: it is reported, and every later item is still attempted. The client marks itself
  closed before it sweeps, so a second `close()` does not retry. The pytest path removes the
  releasing call-site and between-item windows for the factory's list, as it removes the
  return boundary: the session fixture sweeps the list it supplied again at teardown, and
  every wrapper on that list closes at most once. A list that a transport constructor builds
  for its own pools has no second sweep, so those windows stay open there.
- **Once-only close interrupts.** Every close that guards a resource runs at most once: the
  owned wrapper, the transport, and the wrapper that closes a shared pool for its owner. Each
  marks itself closed before it releases. An interrupt from that mark up to the call that
  releases leaves that one resource open, and nothing retries it: not a second `close()`, and
  not the fixture's second sweep, which skips a wrapper already marked closed. The pytest path
  does not remove this window. Marking after the release would instead let an interrupt
  between the two close the resource twice, which section 9.1 rules out.
- **Final-pass omissions.** The refusals of the final unanswered pass are a stated omission
  rather than a claim this design meets.

---

## 10. Compatibility and verification

### Supported versions and the CI matrix

The supported set is Python 3.10 through 3.14 with pytest 7.4 or newer, which is what
`docs/reference/SPEC.md` section 9 requires and what the dependency metadata declares. That
range is the compatibility promise.

CI does not run the whole cross product of that range. It runs the representative jobs in
the table below, one job per row. The table is the CI matrix, not a narrower supported set.

| Python | pytest | graphql-core | Blocking |
|---|---|---|---|
| 3.10 | 7.4, the oldest supported | locked, the newest 3.3 | Yes |
| 3.10 | current stable | locked, the newest 3.3 | Yes |
| 3.11 | current stable | locked, the newest 3.3 | Yes |
| 3.12 | current stable | locked, the newest 3.3 | Yes |
| 3.13 | current stable | locked, the newest 3.3 | Yes |
| 3.14 | current stable | locked, the newest 3.3 | Yes |
| 3.14 | current stable | newest 3.2 | Yes |
| 3.14 | latest prerelease | locked, the newest 3.3 | No |

`current stable`, `latest prerelease` and `newest 3.2` resolve at install time. `locked` is
the version `uv.lock` pins. Every other value is fixed. The Python floor is 3.10 and the
pytest floor is 7.4. The `newest 3.2` row replaces only graphql-core and leaves every other
package as locked. Besides the suite, that row runs `mypy --strict`, because the two
graphql-core lines type some AST fields differently and the lint job sees only the locked
one.

The range is a bound in both directions, and `requires-python` declares both ends as
`>=3.10,<3.15`. A floor alone would let an installer treat an untested future interpreter as
supported, which says more than this project tests. Classifiers do not constrain an install,
so they cannot carry the promise. Raising the ceiling is one change that edits this section,
the metadata and the matrix together.

`current stable` means the newest stable pytest release, whatever its major version. It is
not pinned to a major. Pinning it would make CI pass while the current pytest major went
untested. When a new major is released, these rows start testing it at once, and a failure
there is a real incompatibility rather than a matrix defect. The oldest supported pytest
and the prerelease probe stay as their own rows.

The pytest majors between 7.4 and the current stable get no job of their own. That is
sampling, which is the coverage `docs/reference/SPEC.md` section 9 chose when it required
CI to test the oldest supported and the newest. It is not a narrowing of support. Those
versions stay inside the declared dependency range, and a defect reported against one of
them is a real defect, not an unsupported configuration. Narrowing the supported range
would be a separate decision that edits this section and the dependency metadata together.

The rows change only by a deliberate edit here. Adding or removing a job is one change that
edits this table and the CI workflow together, in the same commit, and records the reason
in the changelog. A CI matrix that does not match these rows is a defect in CI. The
documentation states the supported range and, separately, which pairs CI actually tests.

Plugin code that reads an attribute only some pluggy versions have is covered twice. A test
fakes the older shape, so every row checks that code path. The oldest-pytest row then runs it
against the pluggy that pytest 7.4 resolves, which is the only place the real combination is
checked.

A no-pytest job installs without the `pytest` extra, imports the package, builds a client on
a fake transport, and asserts `pytest` is absent from `sys.modules`. The client half of that
check applies from the point where a client exists.

### Runtime dependency bounds

`graphql-core` is declared as `>=3.2,<3.4`, the 3.2 and the 3.3 lines. `docs/reference/SPEC.md`
section 9 allows `<4`, and this document narrows that to the lines the suite passes on. A
bound above what CI tests would say more than this project tests, and a bound below what
users run would keep a project that already uses graphql-core 3.3 from installing this
package.

- The library behaves the same on both lines. Where graphql-core 3.3 changed something the
  library relies on, the library keeps its documented behavior and does not pass the change
  on to users.
- `src/pytest_graphql/_core/graphql_compat.py` is the only module that reads a graphql-core
  shape that differs between the lines. It detects a difference by what graphql-core offers,
  never by comparing version numbers, and each of its functions is tested on both lines.
  One flag, `LINE_3_3`, set by the public `GraphQLDefaultInput` class, decides every choice
  between the two behaviors. A 3.3 export the module needs that is missing fails the import,
  and never selects the 3.2 behavior, which would be wrong on 3.3 without any error. A module
  that needs a new difference absorbed adds a function there.
- The differences the library absorbs, so a user sees one behavior:
  - An absent AST collection (field arguments, operation variable definitions) is `None`
    in 3.3 and an empty tuple in 3.2. `ast_tuple` reads both as a tuple.
  - `print_ast` spells an object literal `{ a: 1 }` in 3.3 and `{a: 1}` in 3.2. The
    selection layer builds literals by hand and compares text, so `print_value` gives both
    lines the 3.2 spelling.
  - 3.3 keeps a default declared in SDL or received by introspection as a literal and leaves
    `default_value` as `Undefined`. `default_of` returns the coerced value on both lines, and
    required-argument checks use graphql-core's own `is_required_argument`.
  - 3.3 removed the `on_error` callback of `coerce_input_value`. `invalid_input_path` asks
    where a value fails by using `validate_input_value` where it exists and the callback
    where it does not, and returns only the path. The first failure ends the walk, so memory
    and calls into a scalar's hooks do not grow with the number of failures in a value.
  - `is_abstract_type` narrows types in 3.3 only, so the code narrows with `isinstance`.
- The differences the library does not absorb, because the text or the verdict is
  graphql-core's own and the library passes it through by design:
  - The message of a document validation error (SPEC 8.2 asks for graphql-core's message).
    The wording of a few rules differs between the lines, for example a missing required
    argument.
  - The whitespace of a printed document, which carries the same GraphQL on both lines.
  - An integer that a float cannot hold exactly, given to a `Float` variable. 3.3 refuses it
    and 3.2 accepts it. `tests/unit/test_graphql_compat.py` states this per line.
  - The strictness of schema validation. 3.3 reports a default value that does not fit its
    declared type as a schema error, and 3.2 does not. The wording of the schema errors both
    lines share, such as a circular required input object, differs.
- `uv.lock` pins the newest 3.3. One blocking CI row installs the newest 3.2 over it, as the
  matrix above records. Both lines must pass the whole suite, and a failure on either is a
  defect in the library.
- Raising the ceiling to a later line is one change that edits this section, the metadata,
  the lock file and the matrix together. It happens only after the whole suite passes on
  that line, and it gives the new line a row of its own. `graphql_compat.py` is read first,
  because every deprecated name 3.3 still accepts (`parse_value`, `serialize`,
  `default_value`) is a candidate for removal there.

### Development dependencies

`pytest-xdist` is in the `dev` extra, at `>=3.5`, because the suite runs real workers with
`-n 2` to test the xdist behavior of the plugin. It is a development dependency only.

- Its licence is MIT (`License-Expression: MIT` in its metadata), and so is that of `execnet`,
  which it requires. Neither is bundled, copied or imported by any module under `src/`.
- The built wheel names it only as the marker-gated requirement `pytest-xdist>=3.5; extra ==
  "dev"`, as it names every other `dev` requirement, so `pip install pytest-graphql` does not
  install it and the wheel contains none of its code. The plugin talks to xdist through hooks
  and the worker input and output dictionaries, and the hooks only xdist declares are
  `optionalhook`, so an installation without xdist runs unchanged.
- Raising or dropping the lower bound is one change that edits the metadata and the lock file
  together.

### Documentation dependencies

The `docs` extra builds the documentation site. It is a development dependency in the same
sense as `pytest-xdist`: nothing under `src/` imports it, and the built wheel names each
package only as a requirement gated on `extra == "docs"`, so `pip install pytest-graphql`
installs none of it and the wheel contains none of its code. `tests/docs/` asserts that no
docs package is an unconditional requirement.

| Package | Bound | Licence |
|---|---|---|
| `mkdocs` | `>=1.6,<2` | BSD-2-Clause |
| `mkdocs-material` | `>=9.5,<10` | MIT |
| `mkdocstrings` | `>=0.26` | ISC |
| `mkdocstrings-python` | `>=1.12` | ISC |

- The licences are read from the package metadata of the locked versions (1.6.1, 9.7.7,
  1.0.6 and 2.0.9). The other packages the extra installs are under BSD, MIT, ISC or
  Apache-2.0 terms, or a dual of them, except `certifi` and `pathspec`, which are MPL-2.0.
  `certifi` is already a runtime dependency. `pathspec` carries a file-level copyleft
  licence that applies to its own files, which sit in a development environment and are
  never copied into this project, the wheel or the site.
- The upper bound on `mkdocs` is part of the contract. Material for MkDocs states that MkDocs
  2 removes the plugin system and breaks existing themes, and `mkdocstrings` is a plugin.
  Raising the bound is a change that edits the metadata, the lock file and this table
  together.
- Two packages put files on the published site, Material (the theme) and `mkdocstrings` (one
  stylesheet, ISC), besides the pages and files MkDocs generates. `SITE_FILES` in
  `tests/docs/test_site.py` lists every file the build may publish and whose it is. A file
  outside that list fails the build check, so a theme upgrade or a plugin that adds a file
  blocks deployment until its licence basis is recorded here and there. The same test file
  fails on any published file that names a copyleft licence (MPL, GPL, LGPL, AGPL, SSPL or a
  Creative Commons ShareAlike or NonCommercial licence).
- Material's theme directory holds the Lunr language packs, which carry Mozilla Public
  License headers (Lunr Languages and Snowball). MkDocs copies every theme file. Search loads
  a pack only for a language other than English, and this site's search is English only, so
  none is ever loaded. `mkdocs_hooks.py` leaves them out of the site, and it stops the build
  if the search language changes, so a pack is never published without a notice and a source
  offer. English search needs no pack, because the Lunr core is bundled in the search worker.
- Material's scripts and stylesheet bundle these libraries, read from the source maps of the
  locked version (9.7.7): rxjs (Apache-2.0), tslib (0BSD terms), `escape-html`, `clipboard.js`,
  Lunr and `material-design-color` (MIT), and `focus-visible` (W3C Software and Document
  License). Material's own code is MIT. The maps carry the notice text for `escape-html`,
  `clipboard.js`, tslib and Lunr, and the minified files carry none. No separate notice page
  is published. Whether that placement meets the notice terms of the MIT, Apache-2.0 and W3C
  licences is a residual licensing question for the maintainer to settle before the site is
  published. A notice page, if added, is one more entry in `SITE_FILES`.
- The site loads nothing from a third party. `theme.font: false` stops the Google Fonts
  requests that Material makes by default, so a visitor's browser contacts only the host
  of the site. `repo_url` is not set, because Material would then ask the GitHub API for
  repository facts in every visitor's browser. `extra.generator: false` removes the footer
  credit. The theme still carries code for mermaid diagrams (fetched from a CDN) and for
  those repository facts, and both stay inactive while the features that call them are not
  configured. GitHub Pages serves the site, so GitHub sees each request. That cannot be
  avoided on Pages.
- Pages and examples contain project-authored text and the project test schema only. No
  third-party schema, data or media is published.

### Operating systems

Linux, macOS and Windows are supported, which is what `docs/reference/SPEC.md` section 9
requires. The table above runs on Linux. Two further blocking jobs run the same test suite on
macOS and on Windows, each at the newest supported Python with the current stable pytest.

Those two jobs are not rows of the table. The table samples Python and pytest; these jobs
sample the operating system. Each dimension is sampled on its own, because the cross product
of the two is not worth its runtime and neither dimension is left untested.

The lint, type, coverage and no-pytest jobs run on Linux only. What they check does not vary
by operating system.

### Test constraints

- Unit tests never open a non-loopback socket. The guard allows loopback, so integration
  tests may run a real local HTTP server. A non-loopback connection fails the test.
- `tests/docs/` is part of the tree. It executes documentation examples and tests the built
  site, as the next subsection states.
- The full suite runs serially. Under `-n` it fails at collection, because xdist needs every
  worker to collect the same tests and some parametrized and property tests do not. This is a
  limit of the suite and not of the plugin. The xdist behavior of the plugin is tested by
  `tests/unit/test_plugin_xdist.py`, which starts real workers with `-n 2` inside an inner
  session, so that limit does not affect it.

### Documentation examples

`tests/docs/` checks every Python block in the Markdown pages that the site publishes (every
page under `docs/` except `docs/reference/`) and in `README.md`.

**Which blocks.** Fenced blocks only, on three or more backticks or tildes at any indent, as
Material's `pymdownx.superfences` reads them. An indented code block or raw HTML is not
extracted, so a page does not use either for Python. `pymdownx.snippets` is not enabled,
because it would pull code into a page that the check never sees. Docstrings reach the site
through `mkdocstrings` and not through `docs/`, so this check does not see their examples. The
block rules below apply to them too, and "Docstring examples" states how they are run.

**Marker syntax.** A Python block carries one marker, written as an attribute list after the
language: `python {.exec}` or `python {.no-exec}`. Material renders the marker as a CSS class
on the block and still highlights the block as Python. It also works with other attributes,
for example `python {.exec title="test_users.py"}`. The bare form `python exec` is not valid
there. The fence stops being a fence, the following fence pairs with the wrong line, and the
rest of the page is rendered wrongly with no warning, even under `--strict`. The lint
therefore rejects it. GitHub and PyPI read the first word of the info string as the
language, so the README highlights correctly too.

**Lint.** Each of these fails the test for the block, and none depends on the block's
content:

- a Python block with neither marker, or with both, or with a repeated one;
- a block with no language, or with an info string that is not a language followed by at most
  one attribute list;
- a Python language word other than `python` (`py`, `python3`, `pycon`);
- a marker on a block that is not Python;
- a fence that is never closed.

**`no-exec`.** The block is compiled and never run. It is for code that needs a server or
credentials that the test schema does not have, and for fragments.

**`exec`.** A block that defines a test (a top-level `test*` function or `Test*` class) runs
as a file in an inner pytest session with the plugin loaded, so `gql` and every other plugin
fixture behave as they do for a user. The inner project has no ini file and no conftest of its
own. `gql_transport` is the in-process fake over the test schema, and `gql_url` returns a
label that is never connected to, as in `tests/unit/plugin_inner.py`. The tests must pass, and
at least one must run. Any other `exec` block runs in this process in a fresh namespace. The
only names it has, besides the builtins and `__name__` (`__docs_example__`), are:

- `gql`: a `GraphQLClient` over the test schema in `tests/schema/`, built with
  `build_client(url=..., transport=..., schema=...)` on the in-process fake transport. It is
  new for every block and closed when the block ends.

A block imports everything else it uses, as a reader's file would. Blocks run in the order of
the page, but share nothing, and a block must not change process-wide state. `SystemExit` and
any exception fail the block. A traceback names the line of the page.

**Network.** Examples run over the in-process transport, so none opens a socket. `tests/docs/`
carries the same non-loopback socket guard as the unit tests, and a block that tried to
connect would fail.

**Serial run.** `tests/docs/` collects the same blocks in the same order in every process, so
it does not depend on `-n`, and the full suite still runs serially.

**Proof that the check checks.** `tests/docs/test_runner.py` sends deliberately broken blocks
through the same `check` function the real pages use: a raising block, a failed assertion, a
syntax error, an exit, a failing test, an unknown fixture, an unmarked block and each lint case.
A runner that skipped unmarked blocks or ignored a failure would fail those tests.

**The built site.** `tests/docs/test_site.py` builds the site with `mkdocs build --strict` in
a subprocess and checks that:

- the nav is the fifteen pages of `docs/reference/SPEC.md` section 13 in its order, then the
  API reference, and every file under `docs/` is in it;
- the output holds no page, sitemap entry or search entry from `docs/reference/`;
- no page or stylesheet loads anything from a third-party host;
- the build publishes only the files in `SITE_FILES`, no Lunr language pack, and no file that
  names a copyleft licence;
- no hand-written page names a contributor document, a numbered rule (B or C number), a
  milestone or an unrendered Sphinx role;
- the API reference renders every name in `pytest_graphql.__all__`;
- the Quickstart is under 30 lines and holds an install line, one ini line and one `exec`
  test.

These tests import `mkdocs`, so they need the `docs` extra, which `uv sync --all-extras`
installs, as every test needs the `dev` extra.

**The API reference.** `docs/api.md` is generated by `mkdocstrings` from `pytest_graphql`
and so from `__all__`. `show_if_no_docstring` is on, so a name without a docstring still
appears and the gap is visible. One name needs a second directive. `unique` is a function
and also the name of the private module that defines it, and static analysis resolves
`pytest_graphql.unique` to the module, so the page renders it from its defining path with
the path hidden from the heading. The site test lists that exception.

The API page republishes the source docstrings, so they are written for readers. The contributor
reference check of the built site covers the API page like every other page: a docstring that
cites a SPEC section, a B or C number, a milestone, a path under `docs/reference/` or an
unrendered Sphinx role fails it. Private modules and private names keep their contributor
references, because the page does not show them.

**Docstring examples.** `tests/docs/test_docstring_examples.py` checks what the API page renders.

- *Style.* Docstrings are Google style, with `Args`, `Returns`, `Raises` and `Examples`
  sections, and `docstring_style: google` in `mkdocs.yml` renders them. A data field is
  documented by the string under it. A constructor is documented in its class docstring, and
  `merge_init_into_class` shows it with the class. The page leaves out `__slots__`, `__repr__`,
  `__str__`, `__eq__`, `__hash__` and `__post_init__`, which say nothing a reader can use.
- *Selection.* The objects are the ones the page renders. The test reads the `filters` and
  `merge_init_into_class` options from `mkdocs.yml` and the `:::` lines of `docs/api.md`, and
  applies the filters as `mkdocstrings` does, so a change to the page changes the selection and no
  list is kept in the test. Griffe reads the source statically, as the build does.
- *Blocks.* A Python block in a docstring carries `{.exec}` or `{.no-exec}`, as in a page. The
  `exec` blocks run through the same `check` as the pages, with the same `gql` over the test
  schema. A failure names the symbol, the file and the source line of the fence, and a traceback
  inside an example names a source line. `check` returns `"ran"` or `"compiled"`, and the test
  asserts it, so an `exec` block that was only compiled fails. A `no-exec` block is for code that
  needs a server, and it is compiled and never run.
- *The rule.* SPEC section 13 asks every public symbol for a docstring with an example. The
  public API symbols are the names in `pytest_graphql.__all__`, the package docstring, and every
  method, property and class that the page shows under them. Each needs a docstring and at least
  one `exec` block, because a block that is only compiled does not prove the example works. A
  plain data field, such as a dataclass field, needs a docstring and not an example. The rule
  does not apply to every object Griffe can load, so an implementation detail that the page does
  not render is not held to it. Every rendered object also needs a docstring, because
  `show_if_no_docstring` would show the gap.

`tests/docs/test_runner.py` holds the proof that `check` reports `"ran"`, `"compiled"` and
`"not-python"` correctly. These tests need the `docs` extra, like the site tests.

### What an exit criterion may claim

- Property tests use bounded Hypothesis strategies with stated limits on type count, fields
  per type and depth, including cycles, interfaces, unions and nested lists, plus policies
  drawn from their documented ranges. The invariants are named: the document validates
  against the schema, traversal terminates, `max_depth` holds, no field with a required
  argument is emitted without it, and `__typename` is present on every object selection.
  Diamond reuse is not a cycle.
- Determinism is checked against stored golden vectors under the narrowed promise in section
  4, not as byte-identical output across Python versions.
- Documentation blocks marked `exec` are extracted and run by `tests/docs/`, under the rules in
  "Documentation examples". Blocks marked `no-exec` are illustrative and only syntax-checked.
  A lint rule requires every Python block to carry one of the two, so a block cannot be
  skipped by accident.
- A release exit is a verified artifact set, not a publication.

---

## 11. Release integrity

### Workflow and artifact controls

- Every GitHub Actions step is pinned to a full commit SHA, with the human-readable version
  in a trailing comment. Dependabot updates the pins.
- The programs that run the build are pinned too, because a digest proves only that the
  artifact was kept, never that the reviewed program produced it. Every job installs one
  named uv version rather than the newest release, and the release build runs with the build
  dependencies in `requirements/build.txt` as constraints, so the backend and its own
  dependencies are the versions in the tree. `build-system.requires` keeps a range, which is
  what a consumer building the sdist resolves against. The constraints file is what this
  project's own release build uses. Dependabot updates that file, and the uv pin moves by
  commit.
- Workflow permissions default to `contents: read`. Only the publish job adds
  `id-token: write`, and only the release job adds `contents: write`.
- The publish job runs in a protected `pypi` environment with a required reviewer.
- Trusted publishing is used for both indexes. No long-lived upload token is stored.
- One build, then promotion. A single build job produces the sdist and the wheel, records a
  digest for every file, and uploads them as one artifact. Every later job downloads that
  exact artifact. Nothing is rebuilt between indexes, because a rebuild is a different
  artifact.
- An install test installs the wheel and the sdist, each in its own clean environment, and
  runs a smoke test plus the no-pytest import test in each. `twine check` runs on the
  promoted files, and attestations and provenance are retained.
- Artifact checks run on every build: `twine check`, PEP 621 metadata completeness,
  long-description rendering, wheel and sdist contents, and `__version__` agreeing with the
  tag.
- The tag must be a PEP 440 version, or the build job stops. A version with a pre-release or
  a development segment is a prerelease, as in PEP 440, and its GitHub release is marked as a
  prerelease, so an alpha, beta or release candidate never becomes the latest release. A
  post-release or a local label alone is not a prerelease. `scripts/check_artifacts.py`
  makes the decision, and the release job passes it to `gh release create` as an explicit
  true or false.
- The built metadata carries exactly one `Development Status` classifier, and a prerelease
  carries the one its phase names: `3 - Alpha` for an alpha, `4 - Beta` for a beta or a
  release candidate. A final or a development-only version is not mapped, because its
  classifier is the maintainer's statement at that gate. The artifact checks refuse a
  mismatch, so the maturity PyPI shows always agrees with the version.

### Documentation workflow

The `docs` workflow, `.github/workflows/docs.yml`, builds the site, checks it and deploys it
to GitHub Pages. It follows the controls above.

**What it checks.** On every pull request, one job builds the site with `mkdocs build
--strict`, runs `tests/docs/`, and runs `scripts/check_publication_hygiene.py` over every
published page (each Markdown file under `docs/` except `docs/reference/`, and the README)
and over the rendered site (its HTML and XML files and the search index). The rendered site
is scanned as well, because a reader gets the rendered text, and a docstring reaches it
without passing through a page. The theme's own scripts and styles are third-party code and
are not scanned. No pull request run deploys.

**When it deploys.** Only in two cases:

- After the `release` workflow finishes, when that run uploaded to PyPI. A push of a `v*`
  tag does not trigger it. The `gate` job reads the finished run through the Actions API and
  requires that its `upload to PyPI` job concluded `success`. The result of the run is not
  enough. While the `RELEASE_PUBLISH` variable is unset, the run ends as a success with
  every upload job skipped, and that run must deploy nothing. A step that fails after the
  upload, such as the GitHub release, does not stop the docs of a version that is on PyPI.
  The job name is the `name` of the `publish-pypi` job in `release.yml`, and a test keeps the
  two in step. The run must be a push from this repository. A prerelease that reaches PyPI
  deploys like any other version.
- By a manual run from `main`, for a correction to the docs alone. It deploys the head of
  `main`. A manual run from any other ref fails in the gate.

Pushing to `main` does not deploy. A change to the docs reaches the site with the next
release or the next manual run, so the site never describes an API that PyPI does not have,
except by a manual run that the maintainer chose to make.

**What it deploys.** After a release, the build checks out the commit that was released, not
the head of `main`, so the site matches the version on PyPI. One job builds and checks the
site and uploads it as the Pages artifact. The deploy job publishes that artifact and builds
nothing, as in "One build, then promotion" above.

**Controls.**

- The default permission is `contents: read`. The `gate` job adds `actions: read` for the
  one API read. Only the `deploy` job adds `pages: write` and `id-token: write`.
- Every action is pinned to a full commit SHA with its version in a trailing comment, and the
  checkout does not persist the token. The actions are `actions/checkout`,
  `astral-sh/setup-uv`, `actions/upload-pages-artifact` and `actions/deploy-pages`.
- The `deploy` job runs in the `github-pages` environment. A run started by a finished
  workflow or by hand runs in the context of `main`, so the environment's default rule, which
  limits deployments to the default branch, is met. A required reviewer on that environment is
  the maintainer's choice, and the `pypi` environment has already approved the upload that
  starts an automatic deploy.
- Deployments run one at a time, and a running deployment is never cancelled. A newer run of
  the same pull request replaces the older one.
- The workflow never changes a repository setting. Enabling Pages with the source set to
  GitHub Actions is the maintainer's step.

`tests/docs/test_workflow.py` reads the workflow and holds these rules: the pins, the
triggers, the permissions, the environment and the name of the upload job.

### Publication path

Every publication runs the same path.

1. Build once. One job produces the sdist and the wheel, records a digest for every file,
   and uploads them as a single artifact.
2. Upload that artifact set to TestPyPI.
3. Enumerate the release, in a job that holds no publishing credential. Read the file list
   for the exact normalized project name from the TestPyPI simple index API in its JSON
   form (PEP 691), and keep only the files whose version equals the exact version under
   test. This lists every file attached to that release, including any file the build did
   not produce.
4. Compare that complete remote file set, filename by filename and digest by digest, with
   the build manifest in both directions. Every manifest file must be present, and no file
   outside the manifest may be attached to the release. A missing file, an extra file, or a
   differing digest fails the gate. The enumeration is what makes the comparison
   bidirectional. A download cannot do it: `pip download` runs the same resolution as
   `pip install` and returns one selected distribution per requirement, so an extra
   uploaded file would never be fetched and never be noticed.
5. Fetch each manifest file by the URL the index gave for it, and check its digest again
   after the transfer. Then install and test each artifact in its own clean environment,
   from the local file by path, with the index and dependency resolution both disabled
   (`--no-index --no-deps`). Install the locked runtime dependencies into that environment
   from PyPI first, and for the sdist the locked build dependencies as well. The sdist is
   installed with build isolation disabled and build dependencies checked
   (`--no-build-isolation --check-build-dependencies`). Isolation is disabled rather than
   left to the pip default, because an isolated build fetches its backend from an index and
   this job has turned the index off. Each environment then runs the smoke test and the
   no-pytest import test.
6. Upload the same files to PyPI.

Two indexes are never combined in one resolution. `--extra-index-url` is not used at any
step. Combining indexes allows dependency confusion, where a resolver takes a same-named
distribution from the wrong index, and a verification job that installs an unknown
distribution no longer proves anything about the artifact it was meant to test.
Dependencies come from PyPI, the candidate comes from TestPyPI, and the two never share a
resolution.

A version can be uploaded once per index and cannot be replaced, only yanked. The PyPI upload
therefore runs only after the TestPyPI install-back has passed, and a failed check burns that
version number rather than reusing it.

Publication to PyPI is a step the maintainer authorizes and performs. No agent publishes.

### Capability gates

| Gate | Version | Capability required |
|---|---|---|
| Alpha | `0.1.0a1` | Working client, transport, response model and a minimal pytest fixture |
| Beta | `0.1.0b1` | Complete pytest plugin |
| Stable | `0.1.0` | Complete documentation and release acceptance |

The release machinery is built and validated before the first gate and publishes nothing
until then. The trusted-publishing token exchange is the one part that cannot be proven
without an upload, because a pending publisher is only exercised by a real one, so it is
proven at the alpha gate.

A prerelease is not selected by `pip install` when a stable version satisfies the
requirement. It is selected when none does. Before `0.1.0` exists, no stable version
satisfies `pytest-graphql`, so an ordinary unpinned install can select the alpha. Publishing
the alpha to PyPI therefore exposes it to ordinary installs, and the maintainer accepts that
exposure explicitly at the gate. The alternative is to keep the alpha on TestPyPI until a
stable version exists.

No check relies on prerelease selection. Every install in the release path names an exact
version. The alpha README states what is present rather than what is planned.

The prerelease cadence and the compatibility policy are independent. 0.x carries no
deprecation promise, which is about what may change between versions. The gates above are
about when a version is published.

---

## 12. Deferred scope

Excluded from v0.1 and tracked for a later release:

- Disk schema cache: `gql_schema_cache_dir`, `gql_schema_cache_ttl`,
  `--gql-refresh-schema`, `ClientConfig.schema_cache_dir` and
  `ClientConfig.schema_cache_ttl`. The open question about disk cache concurrency is
  therefore not blocking v0.1.
- The `faker` extra.
- The `approx` matcher helper and `NodeList.first`.
- The `gql_selection_policy` fixture and an ini option for the connection page size. The
  fixture set and the option table of the pytest plugin are those of `docs/reference/SPEC.md`
  sections 7.2 and 7.4. `SelectionPolicy.connection_page_size` and the other policy fields
  still apply to every client, and the existing options set the fields that have one.

Async support is a later major version. Every module except the transport package and the
client is pure and free of I/O, so adding an async client means adding an async transport
protocol and an async client class that reuse the same core, with no change to the existing
public API.

If standalone use later becomes a real audience, the reversible move is to publish the core
as a second distribution and make `pytest-graphql` depend on it. Nothing in the current name
blocks that.
