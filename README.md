# pytest-graphql

Schema-aware GraphQL API testing for pytest.

The client reads your server's schema, chooses the fields to ask for, checks each
call before it sends anything, and returns responses that read like Python
objects. A test has no query text in it.

Documentation: https://skhomenko.github.io/pytest-graphql/

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
uploads are not part of `0.1.0`.

## License

MIT. See https://github.com/skhomenko/pytest-graphql/blob/main/LICENSE
