# FAQ

After this page you can explain the five choices of this package that people
question most, and you know what to do when one of them does not fit your project.

## Why does it run introspection on every run?

The package tests the server that you have, and not the server that you had last
week. The schema is read from the server at the start of the run. If a developer
removes or renames a field, a test that uses it fails at once, with a message that
names the field. A stored copy of the schema would pass until somebody remembered
to update it.

The schema is also what makes the rest work. The client uses it to choose the
fields, to check a call before it sends it, to give a field its Python name and to
build the data of `gql.fake`.

A call that the schema does not allow fails in the client, before anything is
sent, and the message names the closest operation.

```python {.exec}
import pytest

from pytest_graphql import OperationNotFoundError

with pytest.raises(OperationNotFoundError, match="Did you mean"):
    gql.query("usr")
```

The cost is small. The schema loads once for the whole session, on the first test
that needs it, and not once for each test. With `pytest-xdist` each worker loads it
once. The `GraphQL` section at the end of a run shows how long it took. See
[Diagnostics](diagnostics.md).

If your server turns introspection off, or the schema is a file in your
repository, tell the client where to read it. Set `gql_schema_source`, or override
the `gql_schema` fixture. See [Extending](extending.md). A schema cache on disk is
not part of `0.1.0`.

## Why is auto-selection the default?

A test should say what it checks, and not which fields to fetch. With
auto-selection a test is one line, `gql.query("user", id="u1")`, and a field that
you add to the schema reaches every test with no edit. A hand-written selection
set must be changed in each test that needs the new field.

```python {.exec}
user = gql.query("user", id="u1")

assert user.name == "Ada Lovelace"
assert user.team.name == "Core"
```

Auto-selection is not free, and it is built so that you stay in control:

- It stops at a depth of 3, and it does not follow a type back onto itself.
- It leaves out deprecated fields and fields that need an argument.
- It refuses to build a document that is larger than `max_fields`.
- A failed test report lists every field that was left out, with the reason.

When a test needs exact fields, write `fields=`. When your whole project needs a
different shape, change `max_depth` or `exclude` once. See
[Selections](selections.md).

## Why do errors raise?

A GraphQL server can answer with status 200 and still report that something went
wrong. If the client returned the data and left the errors in a variable, a test
that forgot to look at them would pass for the wrong reason. A test that checks the
wrong thing and passes is worse than a test that fails.

So an answer with errors raises an exception, and the exception holds the whole
response.

```python {.exec}
import pytest

from pytest_graphql import GraphQLExecutionError

with pytest.raises(GraphQLExecutionError) as raised:
    gql.mutation("updateUser", id="missing", name="Ada", fields=["id"])

assert raised.value.response.errors
```

A test that wants an error says so with `expect_error()`. A test that
wants to accept partial data says so with `raise_on_partial=False`. See
[Errors](errors.md).

## Why is there no record and replay?

A recorded response is a copy of an old contract. When the server changes, the
replay still passes, and the test no longer tests anything. That defeats the first
reason on this page, which is to learn about a change in the schema on the day it
happens.

If you need a failure that a real server rarely gives, such as a timeout, a 502
answer or an error on one field, wrap the transport of the client and change what
it returns. See [Errors](errors.md) and [Extending](extending.md).

## Why is field access strict?

A field that is not in the response raises an error, and it does not give `None`.
A field that was not selected and a field that came back `null` are two different
facts. If both read as `None`, a typo in a field name would pass as "the value is
null". The message of the error lists the fields that the response holds, so a
selection that was too small is easy to see. See
[Why access is strict](responses.md#why-access-is-strict).

```python {.exec}
import pytest

user = gql.query("user", id="u1", fields=["id", "name"])

with pytest.raises(AttributeError, match="no field 'nickname'"):
    user.nickname
```

When you want a lenient read, `node.get("x")`, `hasattr(node, "x")` and
`getattr(node, "x", default)` work as for any Python object.
