# Why pytest-graphql?

After this page you can decide whether this package fits your test suite, and you
know what it changes in the way you write and maintain GraphQL tests.

The package exists to make GraphQL tests easy to read and cheap to maintain. Its
main idea is that a test should not hold query text. The client reads the schema,
and it builds and checks each query for you.

## The cost of query strings

A typical GraphQL test keeps the query as a string and reads the answer as a
dictionary:

```python {.no-exec}
USER_QUERY = """
query User($id: ID!) {
  user(id: $id) { id name team { name } }
}
"""


def test_user_is_in_the_core_team(graphql):
    body = graphql(USER_QUERY, {"id": "u1"})
    assert "errors" not in body
    assert body["data"]["user"]["name"] == "Ada Lovelace"
    assert body["data"]["user"]["team"]["name"] == "Core"
```

The block is only compiled. `graphql` stands for a helper that your project may
already have.

This style works, but it costs more over time:

- **The string is a second copy of the schema.** Each query lists its fields again.
  When a field is renamed or removed, every string that names it must be found and
  changed by hand.
- **A typo is found late.** Your editor and Python do not check the text in the
  string. The mistake shows only when the server answers, and often only as an
  error inside the response.
- **The test is hard to read.** The query, the variables, the `["data"]` keys and
  the error check hide the one fact that the test checks.
- **A forgotten check passes.** A GraphQL server can answer with status 200 and an
  `errors` list. A test that does not look at that list passes for the wrong
  reason.

## The same test without strings

```python {.exec}
def test_user_is_in_the_core_team(gql):
    user = gql.query("user", id="u1")

    assert user == gql.expect.User(name="Ada Lovelace", team={"name": "Core"})
```

The test says what it checks, and nothing else. The client chooses the fields from
the schema, sends the query, raises if the server reports an error, and returns a
response that reads like a Python object.

## What you gain

**Readability**

- A call is one line of Python: `gql.query("user", id="u1")`. Use `fields=` when a
  test needs exact fields. See [Selections](selections.md).
- A response reads with attributes and snake_case names: `user.team.name`. See
  [Responses](responses.md).
- A matcher names only the fields that matter to the test, and a failure prints a
  diff of every field that differs. See [Assertions](assertions.md).

**Maintenance**

- By default, the client reads the schema from the server by introspection, once
  for each run. A renamed or removed field then fails the tests that use it at once,
  with a message that names the field. If you load the schema from a file or from
  your own source instead, the tests see that schema, and not the live server. See
  [Extending](extending.md).
- A new field reaches the tests that use auto-selection, with no edit, when
  auto-selection can select it. It leaves out deprecated fields, fields with a
  required argument that the test did not give, fields past the depth limit,
  repeats of a type on its own path, and fields in `exclude`. See
  [Selections](selections.md).
- A wrong operation, argument or field name fails in the client, before anything
  is sent. The message suggests the closest name.

```python {.exec}
import pytest

from pytest_graphql import OperationNotFoundError

with pytest.raises(OperationNotFoundError, match="Did you mean"):
    gql.query("usr")
```

**Safe defaults**

- An answer with errors raises. A test that expects an error says so with
  `gql.expect_error()`. See [Errors](errors.md).
- A field that is not in the response raises, and does not read as `None`. A typo
  cannot pass as a null value.

**Less helper code**

The package replaces the helpers that a suite tends to grow: tokens and other
identities ([Authentication](authentication.md)), retries
([Configuration](configuration.md)), waiting ([Polling](polling.md)), test input ([Factory](factory.md)) and failure reports
with a `curl` line that repeats the failed call ([Diagnostics](diagnostics.md)).

## Compared with other approaches

| Approach | Use it when | What this package adds |
|---|---|---|
| Query strings sent with an HTTP library | A few tests, or a suite that is not expected to grow. | Queries built from the schema, checked before they are sent, and responses with attribute access. See [Migrating from a hand-rolled client](migrating.md). |
| A general GraphQL client library | Code that calls a GraphQL API from an application. | A design built for tests: auto-selection, matchers with a diff, errors that raise, seeded test input and failure reports. |
| The test client of your server framework | Fast tests of resolvers, in the same process as the server. | Tests over HTTP against any running server, in any language, through the same path that real clients use. |
| A property-based API fuzzer, such as Schemathesis | You want generated cases for every operation of an API. | Tests that you write for the behavior of one operation, with calls built from the schema and checked before they are sent. It does not generate cases for a whole API. |

These approaches can live together. A suite can test resolvers with the test
client of its framework, and test the deployed API with this package.

## When it does not fit

- **You cannot get the schema.** The package needs it, from introspection or from
  a file. See [Extending](extending.md).
- **You need subscriptions, file uploads or an async client.** These are not part
  of `0.1.0`.
- **You want to record responses and replay them.** The package always tests the
  server that you have. See the [FAQ](faq.md).

## Next steps

- Write a first test with the [Quickstart](index.md).
- Move an existing suite one test at a time with
  [Migrating from a hand-rolled client](migrating.md).
