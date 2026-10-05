# Errors

After this page you can catch the exception that each kind of failure raises,
decide how a test treats errors and partial data, and write tests that expect a
call to fail.

## The exception hierarchy

Every exception that the library raises on purpose is a subclass of
[`GraphQLTestError`](api.md#pytest_graphql.GraphQLTestError). All of them can be
imported from `pytest_graphql`.

```text
GraphQLTestError
├── GraphQLClientError
│   ├── OperationNotFoundError
│   ├── ArgumentError
│   ├── SelectionError
│   ├── SelectionTooLargeError
│   ├── SchemaError
│   ├── ScalarNotRegisteredError
│   └── DiagnosticRenderError
├── GraphQLTransportError
│   ├── GraphQLConnectionError
│   ├── GraphQLTimeoutError
│   └── GraphQLHTTPStatusError
├── GraphQLRequestError
├── GraphQLExecutionError
│   └── GraphQLPartialDataError
├── GraphQLFieldError
├── ResponseShapeError
├── WaitTimeoutError
└── ExpectedErrorNotRaised
```

Catch a class to catch all of its subclasses. The three groups tell you where the
failure happened.

- `GraphQLClientError`: the client could not finish a step that it does itself,
  such as checking a call or loading the schema. Most often the call and the
  schema disagree, and a bad name, argument or selection is refused before
  anything is sent. The class does not say who caused the failure. `SchemaError`
  can follow the introspection request that loads the schema, and a server that
  refuses that request is then the cause. `DiagnosticRenderError` comes after a
  request was captured. Read the subclass and the message.
- `GraphQLTransportError`: the request was sent, or could have been sent, and
  there was no usable GraphQL answer.
- `GraphQLRequestError` and `GraphQLExecutionError`: the server answered in
  GraphQL, and the answer is a failure. The first means the server refused the
  request. The second means the operation ran and reported errors.

The other classes report a problem with a response, a poll or an assertion helper.

```python {.exec}
import pytest

from pytest_graphql import GraphQLClientError, GraphQLTestError, OperationNotFoundError

with pytest.raises(GraphQLClientError) as raised:
    gql.query("usr")

assert type(raised.value) is OperationNotFoundError
assert isinstance(raised.value, GraphQLTestError)
```

`GraphQLTestError` is not `graphql.GraphQLError` of graphql-core. They are two
separate classes. The library also raises a plain `TypeError` or `ValueError` for
a call that is wrong in an ordinary Python way, for example an `expect_error()`
count below 1. Those two are not in this tree.

`GraphQLFieldError` is also an `AttributeError` and a `KeyError`, so `hasattr()`
and `node.get("x")` work as they do for a normal object.

| Exception | Raised when |
|---|---|
| [`GraphQLTestError`](api.md#pytest_graphql.GraphQLTestError) | It is the base of the others. A few misuse cases raise it directly, such as `unwrap()` on a response with two root fields. |
| [`GraphQLClientError`](api.md#pytest_graphql.GraphQLClientError) | Never by itself. It is the base of the classes that follow it. |
| [`OperationNotFoundError`](api.md#pytest_graphql.OperationNotFoundError) | The schema has no query or mutation of that name. The message suggests close names. |
| [`ArgumentError`](api.md#pytest_graphql.ArgumentError) | An argument is unknown, missing, given twice or not valid. A keyword that is not a setting is refused too. |
| [`SelectionError`](api.md#pytest_graphql.SelectionError) | `fields` names a field that does not exist, or the document fails the schema check. |
| [`SelectionTooLargeError`](api.md#pytest_graphql.SelectionTooLargeError) | Auto-selection would ask for more fields than `max_fields`. |
| [`SchemaError`](api.md#pytest_graphql.SchemaError) | A type that you named is not in the schema, or the schema cannot be loaded. |
| [`ScalarNotRegisteredError`](api.md#pytest_graphql.ScalarNotRegisteredError) | `gql.fake` needs a value for a custom scalar that has no generator. |
| [`DiagnosticRenderError`](api.md#pytest_graphql.DiagnosticRenderError) | A request cannot be shown as text without showing a secret. |
| [`GraphQLTransportError`](api.md#pytest_graphql.GraphQLTransportError) | A 2xx answer is not a GraphQL response, the body is too large, or the request cannot be built. It is also the base of the next three. |
| [`GraphQLConnectionError`](api.md#pytest_graphql.GraphQLConnectionError) | The server cannot be reached, after the retries. |
| [`GraphQLTimeoutError`](api.md#pytest_graphql.GraphQLTimeoutError) | The server does not answer in time. It is never tried again. |
| [`GraphQLHTTPStatusError`](api.md#pytest_graphql.GraphQLHTTPStatusError) | The status is not 2xx and the body is not a GraphQL response. |
| [`GraphQLRequestError`](api.md#pytest_graphql.GraphQLRequestError) | The server refused the request before it ran it. The answer has errors and no `data` entry. |
| [`GraphQLExecutionError`](api.md#pytest_graphql.GraphQLExecutionError) | The operation ran, and the answer has errors and no usable data. |
| [`GraphQLPartialDataError`](api.md#pytest_graphql.GraphQLPartialDataError) | The answer has data and errors together. |
| [`GraphQLFieldError`](api.md#pytest_graphql.GraphQLFieldError) | You read a field that is not in the response, or a name that matches two fields. |
| [`ResponseShapeError`](api.md#pytest_graphql.ResponseShapeError) | A value in the response does not fit its schema type. |
| [`WaitTimeoutError`](api.md#pytest_graphql.WaitTimeoutError) | `wait_until()` reached its deadline. |
| [`ExpectedErrorNotRaised`](api.md#pytest_graphql.ExpectedErrorNotRaised) | An `expect_error()` block ended without the error that it expected. |

A connection failure of a query is tried again, up to `retries` times, before
`GraphQLConnectionError` is raised. A mutation is not tried again unless the call
has `idempotent=True`, because the server may have run it. A timeout is never
tried again, for the same reason.

## When the server returns errors

A GraphQL server can answer with HTTP status 200 and still report a failure in the
`errors` list of the response. The client checks the answer and decides what to
raise. You never get an error by accident in a variable.

| The answer has | The client raises |
|---|---|
| `errors`, and `data` is `null` | `GraphQLExecutionError` |
| `errors` and `data` together | `GraphQLPartialDataError` |
| No `errors`, and `data` is `null` | `GraphQLExecutionError`, because that breaks the protocol |
| `errors`, and no `data` entry at all | `GraphQLRequestError` |
| `data`, and no `errors` | Nothing. `query()` returns the value. |

`GraphQLPartialDataError` is a `GraphQLExecutionError`. One `except` clause covers
both. Both carry the whole `response` and its `errors`. Read the errors from there,
because the message of the exception never repeats a text from the server.

```python {.exec}
from pytest_graphql import GraphQLExecutionError

try:
    gql.mutation("updateUser", id="missing", name="Ada", fields=["id"])
except GraphQLExecutionError as error:
    assert error.response.data_state == "null"
    assert error.errors[0].path == ("updateUser",)
else:
    raise AssertionError("expected an error")
```

The test schema never returns partial data, so the next example puts a small
transport between the client and the schema. It adds one error to the answer, as
a server does when one field fails. It also shows the protocol violation, a `null`
answer with no error. A real test talks to a real server and needs no such class.

```python {.exec}
from dataclasses import replace

import pytest

from pytest_graphql import (
    GraphQLExecutionError,
    GraphQLPartialDataError,
    build_client,
)


class ChangesAnswers:
    # Changes each answer of another transport, to stand in for a server.

    def __init__(self, inner, change):
        self.inner = inner
        self.change = change

    def send(self, request, *, timeout):
        return self.change(self.inner.send(request, timeout=timeout))

    def close(self):
        pass


def client_for(change):
    return build_client(
        url="http://localhost:8000/graphql",
        transport=ChangesAnswers(gql.transport, change),
        schema=gql.schema,
    )


one_field_failed = client_for(
    lambda raw: replace(
        raw,
        errors=({"message": "no access", "path": ["user", "balance"]},),
    )
)
with pytest.raises(GraphQLPartialDataError) as partial:
    one_field_failed.query("user", id="u1", fields=["id", "balance"])
assert isinstance(partial.value, GraphQLExecutionError)
assert partial.value.response.data.user.id == "u1"

no_data_no_errors = client_for(lambda raw: replace(raw, data=None))
with pytest.raises(GraphQLExecutionError, match="protocol violation"):
    no_data_no_errors.query("user", id="u1", fields=["id"])
```

### Accept errors on purpose

Two settings change the policy. Both exist on `ClientConfig` and as keywords of
one call.

- `raise_on_partial=False`: a response with data and errors returns its data.
  Read the errors with `raw=True`.
- `raise_on_error=False`: no response raises for its errors, partial data
  included. It wins over `raise_on_partial`.

Use them to test what a server does, for example to check a field that fails. A
test that only wants a result should keep the default, because a hidden error is
a test that passes for the wrong reason.

```python {.exec}
from dataclasses import replace

from pytest_graphql import build_client


class AddsAnError:
    # Adds one error to each answer of another transport.

    def __init__(self, inner):
        self.inner = inner

    def send(self, request, *, timeout):
        raw = self.inner.send(request, timeout=timeout)
        error = {"message": "no access", "path": ["user", "balance"]}
        return replace(raw, errors=(error,))

    def close(self):
        pass


client = build_client(
    url="http://localhost:8000/graphql",
    transport=AddsAnError(gql.transport),
    schema=gql.schema,
)
response = client.query(
    "user", id="u1", fields=["id", "balance"], raise_on_partial=False, raw=True
)

assert response.data.user.id == "u1"
assert response.errors[0].path == ("user", "balance")

user = client.query("user", id="u1", fields=["id"], raise_on_partial=False)
assert user.id == "u1"
```

To change a setting for every test, replace it in the `gql_config` fixture, as
[Configuration](configuration.md) shows.

```python {.exec}
import dataclasses

import pytest


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(gql_config, raise_on_partial=False)


def test_the_client_accepts_partial_data(gql):
    assert gql.config.raise_on_partial is False
    assert gql.config.raise_on_error is True
```

## Expect an error

`gql.expect_error()` is a context manager for a test that must fail. The block
must end with a `GraphQLExecutionError`. A `GraphQLPartialDataError` counts, because
it is one. If the block ends without one, `ExpectedErrorNotRaised` is raised.

```python {.exec}
with gql.expect_error(path=["updateUser"], count=1) as caught:
    gql.mutation("updateUser", id="missing", name="Ada", fields=["id"])

assert caught.first.path == ("updateUser",)
assert len(caught.errors) == 1
assert caught.response.data_state == "null"
```

Read `caught` after the block. It holds `errors` (every error that the server
returned), `first` and `response`.

Each filter that you give must match at least one error of the response. The
filters need not match the same error. A wrong filter value fails when you call
`expect_error()`, before the block runs.

| Filter | Matches |
|---|---|
| `code` | An error whose `extensions["code"]` is equal to it. |
| `path` | An error whose whole path is equal to it, segment by segment. It is not a prefix. |
| `message_matches` | An error whose message has a match for the regular expression. It is a search. |
| `count` | The exact number of errors in the response. It does not depend on the other filters. |

The next example uses the same kind of stand-in transport as above, so that the
server returns an error with a code.

```python {.exec}
from dataclasses import replace

from pytest_graphql import build_client


class Forbids:
    # Adds a FORBIDDEN error to each answer of another transport.

    def __init__(self, inner):
        self.inner = inner

    def send(self, request, *, timeout):
        raw = self.inner.send(request, timeout=timeout)
        error = {
            "message": "You may not read this balance.",
            "path": ["user", "balance"],
            "extensions": {"code": "FORBIDDEN"},
        }
        return replace(raw, errors=(error,))

    def close(self):
        pass


client = build_client(
    url="http://localhost:8000/graphql",
    transport=Forbids(gql.transport),
    schema=gql.schema,
)
with client.expect_error(
    code="FORBIDDEN", path=["user", "balance"], message_matches="may not read"
) as caught:
    client.query("user", id="u1", fields=["id", "balance"])

assert caught.first.code == "FORBIDDEN"
```

A block sees the calls of every client in its context, so a call through a clone
such as `gql.as_(...)` counts.

### When the check fails

`ExpectedErrorNotRaised` says why. When the block ended without an error,
`errors` is empty. When a filter matched nothing, `unmatched` names the filters
and `errors` holds what the server returned.

```python {.exec}
import pytest

from pytest_graphql import ExpectedErrorNotRaised

with pytest.raises(ExpectedErrorNotRaised) as no_error:
    with gql.expect_error():
        gql.query("user", id="u1")
assert no_error.value.errors == ()
assert no_error.value.calls == 1

with pytest.raises(ExpectedErrorNotRaised) as wrong_code:
    with gql.expect_error(code="FORBIDDEN"):
        gql.mutation("updateUser", id="missing", fields=["id"])
assert wrong_code.value.unmatched == ("code",)
assert wrong_code.value.errors[0].path == ("updateUser",)
```

A client that does not raise never satisfies the block. With `raise_on_error=False`
or `raise_on_partial=False`, there is no exception for the block to see.

Any other exception leaves the block unchanged. A typo in an operation name is
still an `OperationNotFoundError`, and not a passing test.

```python {.exec}
import pytest

from pytest_graphql import OperationNotFoundError

with pytest.raises(OperationNotFoundError):
    with gql.expect_error():
        gql.query("usr")
```

## Patterns for negative tests

### Check a bad call

The client refuses a bad call before it sends anything. Use `pytest.raises` with the
narrowest class and a `match`. A test that expects `Exception` passes for any
mistake, including a typo in the test itself.

```python {.exec}
import pytest

from pytest_graphql import ArgumentError, SelectionError

with pytest.raises(ArgumentError, match="has no argument 'idd'"):
    gql.query("user", idd="u1")

with pytest.raises(ArgumentError, match="missing required argument 'id'"):
    gql.query("user")

with pytest.raises(SelectionError, match="no field 'nme' on User"):
    gql.query("user", id="u1", fields=["nme"])
```

This is useful for a helper of your own that builds calls. It is not needed for
every test, because a wrong call already fails the test that makes it.

### Check that a server refuses an action

Use `expect_error()` with the code, the path or the message that you expect. Then
check that the refusal had no effect.

```python {.exec}
before = gql.query("users", fields=["id", "name"])

with gql.expect_error(path=["updateUser"]):
    gql.mutation("updateUser", id="missing", name="Mallory", fields=["id"])

after = gql.query("users", fields=["id", "name"])
assert after == before
```

### Check that a field fails, and the rest still works

Accept partial data for the one call, and check the data and the error together.
The example in [Accept errors on purpose](#accept-errors-on-purpose) shows it.

### Check a server that rejects the document

A server answers a document that it cannot parse or validate with errors and no
`data` entry at all. The client raises `GraphQLRequestError`, and `expect_error()`
does not catch it. Send the document with `validate=False`, so that the client does
not refuse it first, and catch the error with `pytest.raises`.

This needs a real server, because the in-process test schema answers every
document with a `data` entry. So the block is not run by the documentation tests.

```python {.no-exec}
import pytest

from pytest_graphql import GraphQLRequestError


def test_the_server_rejects_a_document_it_cannot_run(gql):
    with pytest.raises(GraphQLRequestError) as rejected:
        gql.execute("{ doesNotExist }", validate=False)

    assert rejected.value.errors
```

### Check what your code does when the server is down

A connection failure raises `GraphQLConnectionError`. Set `retries=0` so that the
test does not wait for the tries. A local port that no server uses is enough. The block opens a real connection, so it is not run by the documentation
tests.

```python {.no-exec}
import pytest

from pytest_graphql import ClientConfig, GraphQLConnectionError, build_client


def test_the_client_reports_a_server_that_is_down(gql):
    with build_client(
        url="http://127.0.0.1:9/graphql",
        schema=gql.schema,
        config=ClientConfig(retries=0),
    ) as down:
        with pytest.raises(GraphQLConnectionError) as failed:
            down.query("users")

    assert failed.value.request.url == "http://127.0.0.1:9/graphql"
```

A failure that is hard to cause on a real server, such as a timeout or a 502
answer from a proxy, is easier to test with a transport of your own that raises
the exception. The transport is the object that talks to the server, and any
object with `send` and `close` can take its place. The exception must be built
with `request.redacted()`, as the example shows.

```python {.exec}
import pytest

from pytest_graphql import GraphQLHTTPStatusError, build_client


class BadGateway:
    # Answers every request as a proxy does when the server behind it is down.

    def send(self, request, *, timeout):
        raise GraphQLHTTPStatusError(
            "request failed with status 502: Bad Gateway",
            request=request.redacted(),
            status_code=502,
            body_excerpt="Bad Gateway",
        )

    def close(self):
        pass


client = build_client(
    url="http://localhost:8000/graphql",
    transport=BadGateway(),
    schema=gql.schema,
)
with pytest.raises(GraphQLHTTPStatusError) as failed:
    client.query("users")

assert failed.value.status_code == 502
```

### Read the request that failed

A transport exception holds the request in `request`, and an execution error holds
it in `response.request`. Both are a [`DiagnosticSnapshot`](api.md#pytest_graphql.DiagnosticSnapshot),
a copy of the request in which every secret is replaced by a marker. The message
of the exception is built from that copy, so a token in a header or a variable
does not enter your test report.
