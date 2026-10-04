# pytest-graphql

Schema-aware GraphQL API testing for pytest.

## Status

Beta. The API may still change before `0.1.0`, and the documentation site
arrives with `0.1.0`.

What `0.1.0b1` contains:

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
- Response matching: `gql.expect.<Type>(...)` matchers that are checked
  against the schema, helpers such as `contains`, `unordered` and `absent`, and
  a field-by-field diff when a match fails.
- Seeded fake input data with `gql.fake.<InputType>()`, and `unique()` for
  values that must differ on every run.
- `gql.expect_error()` for expected GraphQL errors, and `gql.wait_until()` for
  polling a query.
- Bearer and header auth, request middleware, and custom scalars through
  `ScalarRegistry`.
- The pytest plugin: ini options, `PYTEST_GQL_*` environment variables, `--gql-*`
  flags, a fixture for each piece (`gql`, `gql_url`, `gql_config`, `gql_auth`,
  `gql_headers`, `gql_schema` and more), six hooks, a `GraphQL calls` section in
  the report of a failed test, and pytest-xdist support.

Not in this release yet: the documentation site, an on-disk schema cache, and
support for graphql-core 3.3.

The changelog records what each release contains:
https://github.com/skhomenko/pytest-graphql/blob/main/CHANGELOG.md

## Usage

Set the endpoint in your pytest configuration, or pass it with `--gql-url`:

```ini
# pytest.ini
[pytest]
gql_url = http://localhost:8000/graphql
```

Each test gets its own client through the `gql` fixture:

```python
def test_user_has_a_name(gql):
    user = gql.query("user", id="123")
    assert user == gql.expect.User(id="123", name="Ann")

def test_create_post(gql):
    payload = gql.fake.CreatePostInput(author_id="123")
    post = gql.mutation("createPost", input=payload)
    assert post.title == payload["title"]

def test_unknown_user(gql):
    with gql.expect_error(code="NOT_FOUND"):
        gql.query("user", id="missing")

def test_post_appears(gql):
    gql.wait_until("post", id="42", until=lambda post: post is not None, timeout=10)
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
