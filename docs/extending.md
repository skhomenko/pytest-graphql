# Extending

After this page you can replace the transport, load the schema from another
place, teach the client a custom scalar, run your own code around every call, and
use each of the six pytest hooks.

Every extension point is a small protocol or a plain function. You write a class
with a few methods and hand it to the client. You do not subclass the client.

| You want to change | Outside pytest | Under pytest |
|---|---|---|
| How a request reaches the server | `build_client(transport=...)` | Override the `gql_transport` fixture. |
| Where the schema comes from | `build_client(schema_source=...)` | Override the `gql_schema_source` fixture, or set the ini option. |
| What a custom scalar is in Python | `build_client(scalars=...)` | Override `gql_scalars`, or use the hook `pytest_graphql_register_scalars`. |
| Who the client is | `build_client(auth=...)` | The `gql_auth` fixture. See [Authentication](authentication.md). |
| What runs around every call | `build_client(middleware=[...])` | The hooks `pytest_graphql_before_request` and `pytest_graphql_after_response`. |
| The settings | `build_client(**options)` | The `gql_config` fixture, or the hook `pytest_graphql_configure`. |

## A custom transport

A transport sends one request and returns the answer. It is any object with two
methods:

- `send(request, *, timeout)` sends a [`RequestInfo`](api.md#pytest_graphql.RequestInfo)
  and returns a `RawResponse`.
- `close()` frees what the transport holds.

Both `RawResponse` and `DerivableTransportBase` are imported from
`pytest_graphql`.

A [`RawResponse`](api.md#pytest_graphql.RawResponse) has `status_code`,
`media_type`, `data`, `errors`, `extensions` and `headers`. Return one for every
answer that has a `data` entry, also when the answer holds errors. For an answer
that is not a GraphQL result, raise an exception. The `request` that `send` gets
holds real header values. Never print it. Use `request.redacted()` when you need
text, as [Errors](errors.md) shows.

This transport runs the query inside your process, with `graphql-core`, and opens
no socket. It tests a server that is written in Python, without HTTP and without
a port.

```python {.exec}
from graphql import graphql_sync

from pytest_graphql import RawResponse, build_client


class InProcessTransport:
    def __init__(self, schema):
        self.schema = schema

    def send(self, request, *, timeout):
        result = graphql_sync(
            self.schema,
            request.document,
            variable_values=dict(request.variables),
            operation_name=request.operation,
        )
        return RawResponse(
            status_code=200,
            media_type="application/graphql-response+json",
            data=result.data,
            errors=tuple(error.formatted for error in result.errors or ()),
            extensions=None,
            headers={},
        )

    def close(self):
        pass


# The page uses its demo schema. You pass the schema object of your own server.
server_schema = gql.schema

with build_client(
    url="http://localhost:8000/graphql",
    transport=InProcessTransport(server_schema),
) as client:
    assert client.query("user", id="u1").name == "Ada Lovelace"
```

The client got its schema from this transport, with the standard introspection
request, as it does from a real server. The `url` is only a label here.

A transport that you give to `build_client()` is yours, and the client does not
close it. Close it yourself. Under pytest, a `gql_transport` fixture that creates a
transport must close it, so write it with `yield`. The `gql` fixture never closes a
transport that it did not make. This block needs the schema module of a project, so
the documentation tests do not run it.

```python {.no-exec}
import pytest

from myproject.schema import schema


@pytest.fixture(scope="session")
def gql_transport():
    transport = InProcessTransport(schema)
    yield transport
    transport.close()
```

Another useful transport wraps another one. It calls the inner transport and then
passes the answer on, changes it, or raises. [Errors](errors.md) and
[Polling](polling.md) use this to play a server that fails or is slow.

### A transport with its own state

By default, every client that you clone with `as_()`, `with_headers()` or
`anonymous()` shares the transport of its parent. If your transport keeps state
for one client, such as cookies or a session, it must give each clone its own
copy. Inherit
[`DerivableTransportBase`](api.md#pytest_graphql.DerivableTransportBase), and
write `derive()`, a method that takes no argument and returns a new transport.

A clone closes its own transport when the clone closes. It never closes the
transport of its parent.

```python {.exec}
from pytest_graphql import DerivableTransportBase, build_client


class CountingTransport(DerivableTransportBase):
    # Counts the requests that it sends, and gives each clone a counter of its own.

    def __init__(self, inner):
        self.inner = inner
        self.sent = 0
        self.closed = False

    def send(self, request, *, timeout):
        self.sent += 1
        return self.inner.send(request, timeout=timeout)

    def derive(self):
        return CountingTransport(self.inner)

    def close(self):
        self.closed = True


parent = CountingTransport(gql.transport)
client = build_client(
    url="http://localhost:8000/graphql", transport=parent, schema=gql.schema
)
client.query("users", fields=["id"])

with client.as_("token-for-ada") as clone:
    clone.query("users", fields=["id"])
    clone.query("users", fields=["id"])
    assert clone.transport is not parent
    assert clone.transport.sent == 2

assert parent.sent == 1
assert not parent.closed
```

A transport that does not inherit the base is shared. An unrelated method named
`derive` is never called, so a plain two-method transport needs nothing more.

## A custom schema source

The client reads the schema once, from a source. The default source asks the
server, with the introspection query. Give it another source when the schema is a
file in your repository, or when the server turns introspection off.

A source is any object with a `load()` method and a `fingerprint` property.
`load()` returns a `graphql.GraphQLSchema`. `fingerprint` is a label that does not
change between runs for the same schema. It names the source in reports.

The client sends requests over its transport, and it uses the schema of the source
to build and to check them. So the file must describe the schema of the server that
you test.

```python {.exec}
from pathlib import Path

import pytest
from graphql import build_schema

from pytest_graphql import OperationNotFoundError

SDL = """
type Query { user(id: ID!): User }
type User { id: ID!  name: String! }
"""


class SdlSource:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        return build_schema(self.path.read_text(encoding="utf-8"))

    @property
    def fingerprint(self):
        return f"sdl:{self.path.name}"


@pytest.fixture(scope="session")
def gql_schema_source(tmp_path_factory):
    # Your file is in the repository. The example writes one so that it can run.
    path = tmp_path_factory.mktemp("schema") / "schema.graphql"
    path.write_text(SDL, encoding="utf-8")
    return SdlSource(path)


def test_the_client_uses_the_schema_of_the_file(gql):
    assert gql.query("user", id="u1").name == "Ada Lovelace"
    with pytest.raises(OperationNotFoundError):
        gql.query("users")
```

The example file holds only the `user` query, so `users` is not an operation, even
though the server has it.

Outside pytest, pass the source to `build_client(schema_source=SdlSource(path))`.
To choose a source from a file with no code in the test, set the ini option
`gql_schema_source` to the dotted path of an instance, such as
`myproject.schemas.SOURCE`. An override of the `gql_schema` fixture loads the schema
in another way, and then nothing asks the server for it.

## A custom scalar

A scalar that your schema defines, such as `Money`, is plain JSON on the wire. A
[`ScalarSpec`](api.md#pytest_graphql.ScalarSpec) says how to read it, how to write
it and how to make a value of it for `gql.fake`. [Factory](factory.md) explains the
fields. This is a complete spec for a decimal amount.

```python {.exec}
from decimal import Decimal

from pytest_graphql import ScalarRegistry, ScalarSpec, build_client

money = ScalarSpec(
    name="Money",
    parse=Decimal,
    serialize=str,
    fake=lambda rng: Decimal(rng.below(100_000)) / 100,
)

registry = ScalarRegistry()
registry.register(money)

with build_client(
    url="http://localhost:8000/graphql",
    transport=gql.transport,
    schema=gql.schema,
    scalars=registry,
) as client:
    user = client.query("user", id="u1", fields=["id", "balance"])
    assert user.balance == Decimal("100.00")
```

Without the spec, `user.balance` would be the string `"100.00"`. Under pytest,
register the spec in the `gql_scalars` fixture, as [Factory](factory.md) shows, or
in the hook `pytest_graphql_register_scalars` below.

## Middleware

Middleware runs code around every call. It is an object with a `before_request`
method, an `after_response` method, or both. Subclass `BaseMiddleware` to write only
the one that you need, and pass a list to `build_client(middleware=[...])`.

- `before_request(request)` runs before the call. It returns a new request to send
  instead, or `None` to keep the request. The request is immutable, so build a new
  one with `dataclasses.replace()`.
- `after_response(response)` runs after the answer came, and before the client
  decides whether to raise. It returns a new response, or `None`.

The list is ordered. `before_request` runs from the first item to the last, and
`after_response` from the last to the first, so each item wraps the ones after it.
A hook that raises stops the call, and the exception reaches the caller.

```python {.exec}
from dataclasses import replace

import pytest

from pytest_graphql import BaseMiddleware, build_client


class Tracing(BaseMiddleware):
    # Adds a trace header to each request, and keeps what happened to it.

    def __init__(self, name, events):
        self.name = name
        self.events = events

    def before_request(self, request):
        self.events.append(f"{self.name} before {request.operation}")
        return replace(request, headers={**request.headers, "X-Trace": self.name})

    def after_response(self, response):
        self.events.append(f"{self.name} after {response.http.status_code}")


class NoMutations(BaseMiddleware):
    # Stops a mutation, so a suite that runs against a shared server cannot change it.

    def before_request(self, request):
        if request.kind == "mutation":
            raise RuntimeError(f"{request.operation} is not allowed on this server")


events = []
client = build_client(
    url="http://localhost:8000/graphql",
    transport=gql.transport,
    schema=gql.schema,
    middleware=[Tracing("outer", events), Tracing("inner", events), NoMutations()],
)

response = client.query("user", id="u1", fields=["id"], raw=True)
assert events == ["outer before user", "inner before user", "inner after 200", "outer after 200"]
assert response.request.headers["X-Trace"] == "inner"

with pytest.raises(RuntimeError, match="updateUser is not allowed"):
    client.mutation("updateUser", id="u1", name="Ada", fields=["id"])
```

The `request` of `before_request` holds real header values, because `Auth` and
middleware need them. Do not print it. Use `request.redacted()`.

Under pytest the `gql` fixture is built for you, so you cannot pass a list to it.
The two request hooks below do the same job, and they apply to every test.

## The pytest hooks

The plugin has six hooks. A hook is a function with a fixed name. Pytest finds it
in a `conftest.py` or in a plugin, and calls it at a fixed moment of the run. Put
the functions in your `conftest.py`.

The examples on this page are single files that must run alone. So each one starts
with `pytest_plugins = [__name__]`, which makes the file a plugin of its own. In a
`conftest.py`, leave that line out.

Each hook has a kind, and the kind is part of its contract:

| Hook | Kind | Runs |
|---|---|---|
| `pytest_graphql_configure` | Observational | Once per session, before the transport is built. |
| `pytest_graphql_schema_loaded` | Observational | Once per session, when the schema is loaded. |
| `pytest_graphql_before_request` | Ordered fold | Before every request. |
| `pytest_graphql_after_response` | Ordered fold | After every response. |
| `pytest_graphql_register_scalars` | Observational | Once per session. |
| `pytest_graphql_report_section` | Report | For each failed test that made a call. |

- An **observational** hook runs every implementation, and what it returns is
  ignored.
- An **ordered fold** runs every implementation in pytest's hook order. A result
  that is not `None` becomes the input of the next one, so two plugins cannot
  discard each other's work. A hook wrapper has no place in a fold, and the plugin
  refuses it with a message that names the plugin.
- The **report** hook runs every implementation. Each text that it returns is
  added to the failure report under a heading of its own.

A function may take fewer arguments than the hook. Pytest passes only the ones that
it names. The three hooks that run once per session run after the fixture that you
may have overridden, so an override of `gql_config`, `gql_schema` or `gql_scalars`
does not skip them.

### `pytest_graphql_configure`

It receives the final `ClientConfig`, and you change it in place. It runs after the
fixture and the flags were applied, and before the transport is built, so what it
sets is final.

```python {.exec}
pytest_plugins = [__name__]


def pytest_graphql_configure(config):
    config.max_depth = 2
    config.headers = {**config.headers, "X-Environment": "ci"}


def test_the_hook_changed_the_configuration(gql):
    assert gql.config.max_depth == 2
    assert gql.config.headers["X-Environment"] == "ci"
```

### `pytest_graphql_schema_loaded`

It receives the schema and the source that it came from. Use it to keep a copy of
the schema that the run used, or to check that the schema still has what your
tests depend on.

```python {.exec}
from pathlib import Path

from graphql import print_schema

pytest_plugins = [__name__]


def pytest_graphql_schema_loaded(schema, source):
    text = f"# source: {source.fingerprint}\n{print_schema(schema)}"
    Path("schema.used.graphql").write_text(text, encoding="utf-8")


def test_the_hook_saved_the_schema(gql):
    saved = Path("schema.used.graphql").read_text(encoding="utf-8")
    assert saved.startswith("# source: introspection:")
    assert "type User implements Node" in saved
```

### `pytest_graphql_before_request`

It receives the request that is about to be sent. Return a new one, or `None` to
keep it. The request holds real header values, so do not print it.

```python {.exec}
import dataclasses

pytest_plugins = [__name__]


def pytest_graphql_before_request(request):
    headers = {**request.headers, "X-Test-Run": "docs"}
    return dataclasses.replace(request, headers=headers)


def test_the_hook_added_a_header(gql):
    response = gql.query("user", id="u1", fields=["id"], raw=True)

    assert response.request.headers["X-Test-Run"] == "docs"
```

### `pytest_graphql_after_response`

It receives the response, before the client decides whether to raise. Return a new
response, or `None`. This one keeps a note of each call, and of the calls that took
too long. It changes nothing, so it returns `None`.

```python {.exec}
pytest_plugins = [__name__]

CALLS = []
SLOW = []


def pytest_graphql_after_response(response):
    operation = response.request.operation
    CALLS.append((operation, response.http.status_code))
    if response.duration_ms > 1000:
        SLOW.append(operation)


def test_the_hook_saw_the_call(gql):
    gql.query("user", id="u1", fields=["id"])

    assert CALLS[-1] == ("user", 200)
    assert SLOW == []
```

### `pytest_graphql_register_scalars`

It receives the registry of the session, once. Register your scalars here when a
plugin, and not a project, supplies them. A name that is registered twice replaces
the first one and gives a warning.

```python {.exec}
from decimal import Decimal

from pytest_graphql import ScalarSpec

pytest_plugins = [__name__]


def pytest_graphql_register_scalars(registry):
    registry.register(
        ScalarSpec(
            name="Money",
            parse=Decimal,
            serialize=str,
            fake=lambda rng: Decimal(rng.below(100_000)) / 100,
        )
    )


def test_the_hook_registered_the_scalar(gql):
    user = gql.query("user", id="u1", fields=["balance"])

    assert user.balance == Decimal("100.00")
```

### `pytest_graphql_report_section`

It runs once for each failed test that made a call and got a response. It receives
the last response, the test item and the pytest config. Return the text of a
section, or `None` for no section. The text is added to the report under the heading
`GraphQL report: <name>`, where the name is the module name of the plugin.

The text is checked like the rest of the report. If it shows a value that a request
hid, it is replaced by a notice. If the hook raises, the section says that it
raised, and never shows the message.

A passing test has no report, so this example is a test that fails on purpose. The
documentation tests run it in a separate check, and compare the report below with
the real output.

```python {.no-exec title="test_report_section.py"}
pytest_plugins = [__name__]


def pytest_graphql_report_section(response, item, config):
    names = ", ".join(error.code or "no code" for error in response.errors)
    return f"{item.name}: status {response.http.status_code}, errors: {names}"


def test_a_missing_user_cannot_be_renamed(gql):
    gql.mutation("updateUser", id="missing", name="Ada", fields=["id"])
```

```bash
pytest test_report_section.py
```

```text
--------------------- GraphQL report: test_report_section ----------------------
test_a_missing_user_cannot_be_renamed: status 200, errors: no code
```
