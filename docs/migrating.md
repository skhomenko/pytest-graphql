# Migrating from a hand-rolled client

After this page you can move a test suite that uses its own GraphQL helper to
`pytest-graphql`, one test at a time, and you know which hand-written pattern each
feature replaces.

## What a hand-rolled client looks like

Most teams start the same way. A module holds the query strings. A helper posts
them with an HTTP library. Each test reads the answer as a dictionary and checks
it.

```python {.no-exec}
import requests

BASE_URL = "http://localhost:8000/graphql"

USER_QUERY = """
query User($id: ID!) {
  user(id: $id) { id name team { name } }
}
"""


def graphql(query, variables=None, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = requests.post(
        BASE_URL, json={"query": query, "variables": variables or {}}, headers=headers
    )
    response.raise_for_status()
    return response.json()


def test_user_has_a_name():
    body = graphql(USER_QUERY, {"id": "u1"}, token="admin-token-123")
    assert "errors" not in body
    assert body["data"]["user"]["name"] == "Ada Lovelace"
```

The block is only compiled. It needs a server, and `requests` is not a dependency
of this package. It stands for the code that your project may already have.

The same test with `pytest-graphql` has no query, no helper and no error check:

```python {.exec}
def test_user_has_a_name(gql):
    with gql.as_("admin-token-123") as admin:
        user = admin.query("user", id="u1")

    assert user.name == "Ada Lovelace"
```

The client reads the schema, builds the query, checks it before it sends it, sends
the token as a header, and raises if the server reports errors.

## Patterns and their replacement

| Hand-rolled | With `pytest-graphql` |
|---|---|
| A module of query strings | `gql.query("user", id="u1")`. The client builds the query. Use `fields=` when you need to choose the fields. See [Selections](selections.md). |
| A helper that posts `{"query": ..., "variables": ...}` | `gql.query()` and `gql.mutation()`. For a document that you keep as text, `gql.execute(document, variables)`. |
| `body["data"]["user"]["name"]` | `user.name`. See [Responses](responses.md). |
| `assert "errors" not in body` | Not needed. An answer with errors raises. See [Errors](errors.md). |
| `assert body["errors"][0]["extensions"]["code"] == "FORBIDDEN"` | `with gql.expect_error(code="FORBIDDEN"):` |
| `assert body["data"]["user"] == {...}` with the whole dictionary | `assert user == gql.expect.User(name="Ada Lovelace")` for part of an object, or `user.to_dict()` for all of it. See [Assertions](assertions.md). |
| `sorted(...)` or `set(...)` to compare lists without their order | `unordered(...)`, `contains(...)` and `length(...)` |
| A `headers = {"Authorization": ...}` helper | The `gql_auth` fixture, or `gql.as_(token)` for one call. See [Authentication](authentication.md). |
| A base URL from `os.environ` | `gql_url` in `pytest.ini`, or `PYTEST_GQL_URL`. See [Configuration](configuration.md). |
| A `requests.Session` in a fixture | The `gql` fixture. Tests share one connection pool, and each client keeps its own cookies. |
| A loop that retries on a connection error | The `gql_retries` setting. It covers connection failures of queries. |
| `time.sleep(2)` before a query | `gql.wait_until(...)`. See [Polling](polling.md). |
| `uuid.uuid4()` or a payload builder for test data | `gql.fake.CreatePostInput()` and `unique()`. See [Factory](factory.md). |
| `print(response.text)` to find out why a test failed | The `GraphQL calls` section of the report, and `--gql-log`. See [Diagnostics](diagnostics.md). |
| `datetime.fromisoformat(...)` on each date field | A `ScalarSpec` for the scalar. See [Factory](factory.md). |
| A copy of the schema in the repository, kept in step by hand | None. The client loads the schema from the server on every run. See the [FAQ](faq.md). |
| A fake response for a unit test | A transport of your own. See [Extending](extending.md). |

## Move one test at a time

Both styles can live in the same suite. You do not need to rewrite everything on
one day.

1. Install the package, and set `gql_url`. The first new test can use the `gql`
   fixture at once. See the [Quickstart](index.md).
2. Keep your helper. For a document that you still want to send as text, use
   `gql.execute(document, variables)`. It returns the response, and `unwrap()` gives
   the value of the one root field.
3. Replace the query strings, one test at a time, with `gql.query()` and
   `gql.mutation()`.
4. Replace the checks on `"errors"` and on the whole dictionary. Use
   `expect_error()` and `gql.expect`.
5. When no test calls the helper any more, delete it.

```python {.exec}
USER_QUERY = """
query User($id: ID!) {
  user(id: $id) { id name team { name } }
}
"""


def test_a_document_that_you_keep_runs_through_the_client(gql):
    user = gql.execute(USER_QUERY, {"id": "u1"}).unwrap()

    assert user.name == "Ada Lovelace"
    assert user.team.name == "Core"
```

## What changes in a test

Some behavior is new, and a test that was written for the old helper may fail for a
good reason.

- **The client checks the document before it sends it.** A test that sends a
  document that is invalid on purpose now fails in the client, before the server
  sees it. Add `validate=False` to that call. See [Errors](errors.md).
- **Errors raise.** A test that read an `errors` list from the body now gets an
  exception. Use `expect_error()`, or set `raise_on_error=False` on that call. See
  [Errors](errors.md).
- **Auto-selection asks for more fields than your old query did.** It follows the
  schema, to a depth of 3 by default. Use `fields=` to ask for exactly the fields
  you used to ask for.
- **A field that is not in the answer raises.** A test that expected `None` for a
  field it did not select now fails on that line. See [Responses](responses.md).
- **Names are snake_case in Python.** `user.joined_at` reads the field `joinedAt`,
  and `user.to_dict()` gives the names of the wire.
- **Lists match by position.** A test that compared lists as sets needs
  `unordered()`. See [Assertions](assertions.md).
