# pytest-graphql

Schema-aware GraphQL API testing for pytest.

## Status

Alpha. This release contains a working client, an `httpx` transport, a response
model and one pytest fixture. The API may still change before `0.1.0`.

What `0.1.0a1` contains:

- `GraphQLClient` and `build_client()`. The client reads your schema and builds
  the selection set for an operation. By default it checks the operation
  against the schema before it sends anything, and `validate=False` turns that
  check off. Arguments and fields are snake_case in Python and use the schema's
  own names on the wire.
- An `httpx` transport with explicit timeout, retry and response size limits.
  With this transport, tests share one connection pool and each client keeps
  its own cookie jar. A transport you supply manages its own connections and
  cookies.
- A response model. Failures carry a redacted summary of the request.
  `RequestInfo.as_curl()`, available to middleware, renders a `curl` command
  that reproduces a request, with credentials replaced by placeholders.
- Bearer and header auth, and request middleware.
- The pytest fixtures `gql`, `gql_url` and `gql_transport`, and the
  `--gql-url` flag. The schema loads once per session, and each test gets its
  own client.

Not in this release yet: ini options, environment variables and hooks, response
matching, fake data, error assertions and polling.

The changelog records what each release contains:
https://github.com/skhomenko/pytest-graphql/blob/main/CHANGELOG.md

## Usage

With pytest, pass the endpoint on the command line:

```
pytest --gql-url=http://localhost:8000/graphql
```

```python
def test_user_has_a_name(gql):
    user = gql.query("user", id="123")
    assert user.name
```

For a URL known only at run time, override the `gql_url` fixture in your
`conftest.py`. The `--gql-url` flag still wins when both are given.

Without pytest, use `build_client()`, and close the client when you are done:

```python
from pytest_graphql import build_client

with build_client(url="http://localhost:8000/graphql") as gql:
    user = gql.query("user", id="123")
```

## Install

```
pip install pytest-graphql
```

The required dependencies are `graphql-core`, `httpx`, and `certifi`. `pytest`
is optional, so the client can be used outside a test suite:

```
pip install "pytest-graphql[pytest]"
```

## Supported versions

Python 3.10 through 3.14, with pytest 7.4 or newer when the pytest extra is
installed. CI tests a representative sample of that range rather than the whole
cross product.

## License

MIT. See https://github.com/skhomenko/pytest-graphql/blob/main/LICENSE
