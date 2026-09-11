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
- `pytest` is an optional dependency. The required dependencies are `graphql-core` and
  `httpx`. `pytest` lives in a `pytest` extra and in the `dev` extra.
- The `pytest11` entry point is always declared. It is inert when pytest is absent, because
  only pytest reads it.
- The core library imports no pytest. Only modules under the pytest plugin package may
  import it. A purity check enforces this in CI.
- The base exception is `GraphQLTestError`. Leaf exception classes keep their specification
  names. The base is renamed to avoid a clash with `graphql.GraphQLError`, which comes from
  a required dependency.
- Argument and field naming is snake_case in and snake_case out.
  `gql.mutation("createUser", first_name="John")` sends `firstName`. The factory returns
  snake_case keys, so `**payload` round-trips. Exact schema spellings stay reachable through
  `variables={...}`.
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

### Variables and options

- Keyword arguments are the documented way to pass variables. `variables=` is the
  exact-name escape hatch. Supplying one argument through both raises `ArgumentError` and
  names it. Nothing is silently overwritten.
- Any keyword in the per-call option table is an option. Every other keyword is a variable.
  A schema argument whose name collides with an option is reachable only through
  `variables=`, and the error says so.
- Every variable value is coerced against its input type after assembly. A coercion failure
  becomes `ArgumentError` naming the path. Document validation alone does not check values,
  so this closes the gap where a wrongly typed variable reached the network.

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
- An alias must be unique within its selection set. A duplicate raises. Materialization keys
  on the alias.
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
`GraphQLClient`, `ClientConfig`, `build_client`, `GraphQLTestCase`, the exception hierarchy,
`Selection`, `Field`, `AUTO`, `SelectionPolicy`, `CyclePolicy`, `ScalarSpec`,
`ScalarRegistry`, the matcher helpers, `unique`, the `Transport`, `SchemaSource`, `Auth` and
`Middleware` protocols, `BaseMiddleware`, `BearerAuth`, `HeaderAuth`, `RequestInfo`,
`DiagnosticSnapshot`, `GraphQLResponse`, `Node`, `NodeList`, and `__version__`.

Importable from their own modules, and carrying a compatibility promise only at that path:
`OperationNamespace`, `FakeNamespace`, `ExpectNamespace`, `HttpxTransport`,
`IntrospectionSource`, `SDLFileSource`, `RawResponse`, `CapturedErrors`, `SelectionInput`.

### Constructor and configuration split

`ClientConfig` holds data. The constructor holds objects: `transport`, `schema`, `scalars`
and `middleware`. `seed` is configuration and lives on `ClientConfig` only, never on the
client constructor.

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
- `pytest_graphql_report_section` runs every implementation and concatenates the results in
  hook order, each under its own heading.

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
  accepts a page-size argument. Auto-selection then supplies `first: connection_page_size`
  as a generated variable, default 10, unless the caller already supplied one. A connection
  field with no page-size argument is skipped.
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

`__typename` is emitted on every object selection, not only on interfaces and unions, and it
does not count against `max_fields`. Explicit `fields=` adds nothing, so `Node.__typename__`
is `None` there unless the user asked for it. A matcher checks the type name only when the
response object carries one.

The default numeric limits above, along with `max_fields`, `max_depth` and `cycle_policy`,
are validated against a checked-in corpus of introspection documents captured from real
public schemas before any layer is built on them. Each corpus document records its source
and retrieval date, so the validation is reproducible and CI never depends on a live
third-party endpoint.

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
- Golden vectors store the output of every built-in scalar at a fixed seed. A changed value
  fails the suite, so any change is deliberate, versioned and recorded in the changelog.
- `unique()` derives from run id, xdist worker id or `"main"`, node id, field path, and a
  monotonic per-process counter kept per node id and field path. Repeated calls in one test
  differ, and values differ across workers. Reproducibility and uniqueness are mutually
  exclusive by design.

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

`execute()` always returns `GraphQLResponse`. Convenience unwrapping stays on `query()` and
`mutation()`, which have exactly one known top-level field. `GraphQLResponse.unwrap()` is
the explicit opt-in for a raw document, and it raises `GraphQLTestError` naming the fields
when the document has more than one top-level field.

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

### Polling

`wait_until` computes its deadline once from `time.monotonic()`. An attempt is counted when
the call is made, and at least one attempt always runs, `timeout=0` included. The sleep after
a failed attempt is `min(interval * backoff ** (attempt - 1), remaining)`, and the loop
raises once `remaining` reaches zero.

`ignore` accepts only `Exception` subclasses. Passing anything else raises `TypeError` at
call time, so a `BaseException` such as `KeyboardInterrupt` is never swallowed by a poll
loop. An exception outside `ignore` propagates. `WaitTimeoutError` carries attempts, elapsed
monotonic time, the last response and the last swallowed exception.

Polling covers queries only.

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
- Retries are connect-failure only. `max_attempts` defaults to 3, so at most two retries.
  Backoff is `min(0.1 * 2 ** (attempt - 1), 2.0)` seconds with full jitter. A request that
  reached the server is never retried.
- `max_response_bytes` defaults to 32 MiB. Reading past the cap raises
  `GraphQLTransportError` without buffering the rest. The cap counts decompressed bytes.
- `trust_env` defaults to `False`, so ambient `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`,
  `SSLKEYLOGFILE` and netrc are ignored unless a project opts in through
  `ClientConfig.trust_env` or `--gql-trust-env`. This keeps runs reproducible and stops
  requests from silently routing through an ambient proxy.
- `ClientConfig.proxy` and `ClientConfig.verify` follow `httpx` semantics. `verify` accepts
  `True`, a CA bundle path, or an `ssl.SSLContext`. `verify=False` emits a warning.
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

### Name-based and path-based redaction

- Header names match `redact_headers` case-insensitively after stripping. The default is
  `authorization`, `cookie`, `x-api-key`, `proxy-authorization`.
- `ClientConfig.redact_variables` holds dotted paths and glob patterns, matched
  case-insensitively against snake_case variable paths at every depth, inside input objects
  and inside lists. The default is `password`, `token`, `secret`, `api_key`, `access_token`,
  `refresh_token`, `authorization`, `otp`, `pin`, `credit_card`, `ssn`. The same patterns
  apply to captured response data.
- URL userinfo and the query string are stripped from any recorded URL.
- Cookies are recorded by name only, never by value, on both the request and the response
  side.

### Value scrub for free-form text

A server error message, a body excerpt or an exception message has no path, so path-based
redaction cannot cover it. The scrub closes that gap.

- Secret set. For each request and response pair the redaction stage collects the values it
  already knows are sensitive: every header value matched by `redact_headers`, the value at
  every variable path and response path matched by `redact_variables`, every cookie value,
  and the userinfo and query-string values stripped from the URL. The set exists only inside
  the stage. It is never stored on a snapshot, never logged, and never returned by a public
  API.
- Derived forms. For a header value carrying a scheme, such as `Bearer <token>`, the part
  after the first space is added as well. The percent-encoded form of each value is added.
  Non-string values are converted with the same JSON text form used for rendering before
  being added.
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

`max_diagnostic_bytes` caps each snapshot field, default 4096, with a total cap of 32768 per
snapshot. The error list truncates at `max_recorded_errors`, default 20. Truncation is
visible, never silent, and states how many bytes or entries were cut. The diagnostics
recorder is a bounded `deque`, default 50 calls, set by `ClientConfig.max_recorded_calls`,
and the plugin clears it per test.

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
accepts. It runs each generated command through `/bin/sh` and asserts that the name arrives
unchanged and that no expansion or substitution ran. It also asserts that `x-api-key` and
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
`Auth.apply`, `with_headers()` in clone order, then per-call `headers=`. Names compare
case-insensitively after stripping, and a later source replaces an earlier one for the same
name instead of adding a second line. A genuinely repeated header uses an explicit list
value.

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

### 9.3 One sweep, one close authority

There are two releasing call sites, the client and the factory unwinding path. They share
one sweep implementation, so a later correction cannot reach one and miss the other.

- Every item is attempted exactly once, in reverse order, whatever the earlier ones did. A
  failing transport teardown cannot strand the root pool.
- The exception that wins is chosen before the chain is built.
- A `BaseException` that is not an `Exception` always wins. An interrupt is raised as itself
  rather than chained behind a transport error, so an `except Exception` around the call
  cannot swallow it.
- Among ordinary exceptions the caller's preferred exception wins. The client prefers the
  last failure in sweep order, which is the earliest-acquired resource. The factory prefers
  the construction failure, because that is why the caller's call failed.
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
refused, what is left is the exception the raise itself writes into that link, so the report
hangs under that one instead of above it.

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
traceback. Those links are still links the caller reaches, so a refusal that every rendering
placement passed over is put in one of them rather than nowhere. That last-resort placement
runs only for reporting's own refusals, only while the record is not carrying the report, and
only into an empty slot the value does not reach back to, so it drops nothing and closes no
cycle.

The chain ranks under the record rather than beside it. While the record carries the report
the chain is best effort, and it is never given a cycle the caller's own graph did not arrive
with.

### 9.5 `reported_errors` and the permitted losses

`reported_errors` reads the record back and falls back to the rendered chain, which is where
failures are put when the record cannot hold them. It carries every distinct failure the
report covers, raised one first, including what the raised exception itself already carried,
because the coming `raise` can take that out of the chain. Refused writes are reported too,
so a type that interferes with reporting cannot also hide that it did.

A single-link exception graph cannot always express every failure at once, and
`BaseExceptionGroup` is unavailable at the supported floor. What can be lost is bounded and
named.

**Two permitted caller-failure loss shapes, and no others.**

1. Every link that could carry the failure is refused, with the record gone.
2. The caller's own graph leaves the chain no room, which happens when the record is gone and
   two or more cleanup failures carry an explicit cause that leads back to the reported
   exception. Each such run loses exactly one fewer failure than it carries.

**Two permitted refusal-omission shapes, and no others.**

1. The refusals the final unanswered pass makes after the last record the report carried,
   which have no later write to carry them.
2. A refusal dropped in a run that also loses a caller failure to the second shape above,
   where the chain has no room for either.

A run that keeps every caller failure keeps the refusal too. There is no run in which
reporting loses only its own write failure.

Both loss sets are asserted by identity and by set equality, never by count. The bound comes
from three exhaustive products over reported-exception state, handler state, record presence,
cleanup-failure shape and refused-write ordinal, the widest running to five cleanup failures.
It is a measured bound and not a proof for a sequence of any length, and nothing is claimed
beyond five.

### 9.6 Termination and cost

Reporting is bounded in passes and in write attempts. Against a surface that refuses every
write, reporting ends after **56 attempts outside an active exception handler** and **59
inside one**, with an ordinary refusal and with an interrupt alike. Inside a handler the
extra attempts are the crowning and anchoring writes. A change that moves either number has
changed behavior.

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

| Python | pytest | Blocking |
|---|---|---|
| 3.10 | 7.4, the oldest supported | Yes |
| 3.10 | current stable | Yes |
| 3.11 | current stable | Yes |
| 3.12 | current stable | Yes |
| 3.13 | current stable | Yes |
| 3.14 | current stable | Yes |
| 3.14 | latest prerelease | No |

`current stable` and `latest prerelease` resolve at install time. Every other value is
fixed. The Python floor is 3.10 and the pytest floor is 7.4.

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

A no-pytest job installs without the `pytest` extra, imports the package, builds a client on
a fake transport, and asserts `pytest` is absent from `sys.modules`. The client half of that
check applies from the point where a client exists.

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
- `tests/docs/` is part of the tree and executes documentation examples.

### What an exit criterion may claim

- Property tests use bounded Hypothesis strategies with stated limits on type count, fields
  per type and depth, including cycles, interfaces, unions and nested lists, plus policies
  drawn from their documented ranges. The invariants are named: the document validates
  against the schema, traversal terminates, `max_depth` holds, no field with a required
  argument is emitted without it, and `__typename` is present on every object selection.
  Diamond reuse is not a cycle.
- Determinism is checked against stored golden vectors under the narrowed promise in section
  4, not as byte-identical output across Python versions.
- Documentation blocks carrying the `exec` info string are extracted and run by
  `tests/docs/`. Unmarked blocks are illustrative and only syntax-checked. A lint rule
  requires every Python block to carry `exec` or `no-exec`, so a block cannot be skipped by
  accident.
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

Async support is a later major version. Every module except the transport package and the
client is pure and free of I/O, so adding an async client means adding an async transport
protocol and an async client class that reuse the same core, with no change to the existing
public API.

If standalone use later becomes a real audience, the reversible move is to publish the core
as a second distribution and make `pytest-graphql` depend on it. Nothing in the current name
blocks that.
