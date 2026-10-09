# pytest-graphql

The GraphQL test client for Python whose calls are built from the schema and checked against it before they are sent.

A test names an operation, and the client chooses the fields to ask for. A test has no query text, and a schema change does not mean editing query strings by hand.

Documentation: https://skhomenko.github.io/pytest-graphql/

## Use it when

- You test a GraphQL API from Python, over HTTP, against a server that is running.
- You want a wrong operation, argument or field name to fail before any request is sent, with a message that names the closest correct one.
- Your schema changes often, and you do not want to edit query strings in tests.
- A coding agent writes or updates your GraphQL tests.
- You want a failed test to show its calls and a `curl` line that repeats the failed call.

## Do not use it when

- Your API is REST. Use an HTTP client or a test tool made for REST.
- You write production client code. Use a general GraphQL client library.
- You need a mock GraphQL server. This package calls a server that you have. Use a mock server tool to fake one.
- You want to fuzz every operation of an API. Use a dedicated property-based API fuzzer.

## Quickstart

Install the package with its pytest extra:

```bash
pip install "pytest-graphql[pytest]"
```

Tell pytest where your GraphQL endpoint is:

```ini
# pytest.ini
[pytest]
gql_url = http://localhost:8000/graphql
```

Write a test. The `gql` fixture is a client that has already read your schema:

```python {.exec}
def test_user_has_a_name(gql):
    user = gql.query("user", id="u1")
    assert user.name == "Ada Lovelace"
```

Run `pytest`. The client chooses the fields to ask for, so the test contains no
query text.

## A fuller example

```python {.exec}
def test_a_new_post_has_the_title_that_was_sent(gql):
    payload = gql.fake.CreatePostInput(author_id="u1")

    post = gql.mutation("createPost", input=payload)

    assert post == gql.expect.Post(title=payload["title"])


def test_a_user_with_the_fields_that_the_test_needs(gql):
    user = gql.query("user", id="u1", fields=["id", "name", {"team": ["name"]}])

    assert user == gql.expect.User(name="Ada Lovelace", team={"name": "Core"})


def test_an_update_of_a_missing_user_fails(gql):
    with gql.expect_error(path=["updateUser"]):
        gql.mutation("updateUser", id="missing", name="Ada", fields=["id"])
```

## API at a glance

| Call | What it does |
|---|---|
| `gql` | The pytest fixture. A client that has read your schema. |
| `gql.query("user", id="u1")` | Runs a query by operation name. Arguments are snake_case keywords. The client chooses the fields. |
| `gql.mutation("createPost", input=payload)` | Runs a mutation by operation name, with the same rules as `query`. |
| `gql.execute(document, variables)` | Sends a document that you wrote, such as one with directives or several root fields. It checks the document and returns the response. |
| `fields=` | Chooses exact fields for a call: a list or dictionary of names, `Field` objects, a raw string, or a `Selection`. |
| `Selection` | A reusable choice of fields. |
| `gql.expect.User(name="Ada")` | A matcher for part of an object. A failure prints a diff. |
| `gql.expect_error(code=...)` | A context manager for a call that must return an error. |
| `gql.fake.CreatePostInput()` and `unique()` | Seeded fake input that repeats from run to run. `unique()` marks a field that must differ. |
| `gql.as_(token)` and `gql.with_headers(...)` | A clone of the client with another identity, or with extra headers. |
| `gql.wait_until("post", until=..., id=...)` | Repeats a query until a condition holds, or the deadline passes. |
| `build_client(url=...)` | A client for scripts and for `unittest`, with no pytest. |

## Compared with other tools

| Approach | Use it when | What this package adds |
|---|---|---|
| Query strings sent with an HTTP library | A few tests, or a suite that is not expected to grow. | Queries built from the schema, checked before they are sent, and responses with attribute access. See [Migrating from a hand-rolled client](https://skhomenko.github.io/pytest-graphql/migrating/). |
| A general GraphQL client library | Code that calls a GraphQL API from an application. | A design built for tests: auto-selection, matchers with a diff, errors that raise, seeded test input and failure reports. |
| The test client of your server framework | Fast tests of resolvers, in the same process as the server. | Tests over HTTP against any running server, in any language, through the same path that real clients use. |
| A property-based API fuzzer, such as Schemathesis | You want generated cases for every operation of an API. | Tests that you write for the behavior of one operation, with calls built from the schema and checked before they are sent. It does not generate cases for a whole API. |

## Features

- **Fields chosen for you.** Auto-selection builds the selection set from the
  schema, and `fields=` takes over when you need exact fields.
  [Selections](https://skhomenko.github.io/pytest-graphql/selections/)
- **Responses that read like Python.** Attribute access, snake_case names, lists
  with helpers, and an error for a field that is not there.
  [Responses](https://skhomenko.github.io/pytest-graphql/responses/)
- **Assertions with a readable diff.** Plain `assert`, partial matchers such as
  `gql.expect.User(...)`, and list helpers.
  [Assertions](https://skhomenko.github.io/pytest-graphql/assertions/)
- **Errors that raise.** A response with errors raises, and `gql.expect_error()`
  is for the test that expects one.
  [Errors](https://skhomenko.github.io/pytest-graphql/errors/)
- **Seeded fake input.** `gql.fake.<InputType>()` repeats from run to run, and
  `unique()` marks a field that must differ.
  [Factory](https://skhomenko.github.io/pytest-graphql/factory/)
- **Identity.** Clone a client as another user, write your own `Auth`, and
  refresh a token.
  [Authentication](https://skhomenko.github.io/pytest-graphql/authentication/)
- **Polling.** `gql.wait_until()` repeats a query with a deadline and a backoff.
  [Polling](https://skhomenko.github.io/pytest-graphql/polling/)
- **Reports with the calls in them.** A failed test lists its calls, hides the
  headers and variables that you list as secrets, and ends with a `curl` line that
  repeats the failed call.
  [Diagnostics](https://skhomenko.github.io/pytest-graphql/diagnostics/)
- **Open to change.** Replace the transport, load the schema from a file, register
  custom scalars, add middleware, or use one of six pytest hooks.
  [Extending](https://skhomenko.github.io/pytest-graphql/extending/)
- **No pytest needed.** The core imports no pytest. `build_client()` works in
  `unittest`, in scripts and in a shell.
  [Using without pytest](https://skhomenko.github.io/pytest-graphql/without-pytest/)
- **Parallel runs.** The plugin works with `pytest-xdist`.
  [Cookbook](https://skhomenko.github.io/pytest-graphql/cookbook/)

## More documentation

- [Why pytest-graphql?](https://skhomenko.github.io/pytest-graphql/why/):
  what the package changes, and how it compares with other approaches.
- [Using with coding agents](https://skhomenko.github.io/pytest-graphql/agents/):
  rules to paste into `AGENTS.md` or `CLAUDE.md`, and a skill file.
- [Configuration](https://skhomenko.github.io/pytest-graphql/configuration/):
  every option, flag and environment variable, and which one wins.
- [Migrating from a hand-rolled client](https://skhomenko.github.io/pytest-graphql/migrating/)
- [FAQ](https://skhomenko.github.io/pytest-graphql/faq/)
- [API reference](https://skhomenko.github.io/pytest-graphql/api/)
- [Changelog](https://github.com/skhomenko/pytest-graphql/blob/main/CHANGELOG.md)

## Install

```bash
pip install pytest-graphql
```

The required dependencies are `graphql-core`, `httpx` and `certifi`. `pytest` is
optional, so the client can be used outside a test suite. Add the extra when you
use the plugin:

```bash
pip install "pytest-graphql[pytest]"
```

## Supported versions

Python 3.10 through 3.14, graphql-core 3.2 and 3.3, and pytest 7.4 or newer when
the pytest extra is installed. CI tests a representative sample of that range
rather than the whole cross product.

## Status

Version 0.x. The API may change between minor versions, and the changelog records
every change. An on-disk schema cache, async clients, subscriptions and file
uploads are not part of `0.1.x`.

## License

MIT. See https://github.com/skhomenko/pytest-graphql/blob/main/LICENSE
