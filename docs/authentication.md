# Authentication

After this page you can send a test's requests as a chosen user. You can run the
same call as several users, write your own `Auth` object, and refresh a token
that expires.

## Headers for the whole run

For a header that every request needs, set it once in configuration. The schema
request carries it too, so this is also the place for a token that the server
needs before it shows its schema.

```ini
# pytest.ini
[pytest]
gql_url = http://localhost:8000/graphql
gql_headers =
    X-Environment: test
```

The same setting is the `PYTEST_GQL_HEADERS` environment variable. See
[Configuration](configuration.md). In code, use the `headers` field of
`gql_config`.

## Headers and identity for one test

Two fixtures set what one test sends. Override either of them in a module, a
class or a test.

- `gql_headers` returns a `dict` of headers.
- `gql_auth` returns the identity. It can be a string, which is a bearer token, an
  `Auth` object, or `None` for none.

```python {.exec}
import pytest


@pytest.fixture
def gql_headers():
    return {"X-Tenant": "acme"}


@pytest.fixture
def gql_auth():
    return "admin-token-123"


def test_the_request_carries_the_headers_and_the_identity(gql):
    response = gql.query("user", id="u1", fields=["id"], raw=True)

    assert response.request.headers["X-Tenant"] == "acme"
    assert response.request.headers["Authorization"] == "[redacted:Authorization]"
```

The headers of a request in a report are redacted. A header whose name is in
`redact_headers` shows a placeholder. The default list is `authorization`,
`cookie`, `proxy-authorization` and `x-api-key`. A custom header name, such as
`X-Session`, shows its value until you add its name with `gql_redact_headers`.
See [Diagnostics](diagnostics.md).

The `gql_headers` fixture is not the `gql_headers` ini option. The fixture sets
headers for one test. The option sets them for the whole run.

### Which header wins

A header can come from five places. From the lowest to the highest:

1. The configuration: the ini option, the environment variable, or `gql_config`.
2. The `gql_headers` fixture.
3. The `Auth` object: the `gql_auth` fixture, `with_auth()` or `as_()`.
4. `with_headers()`, in the order of the calls.
5. The `headers=` option of one call.

A higher source replaces a lower one for the same header name. Names compare
without regard to case, and a header is not sent twice.

```python {.exec}
import dataclasses

import pytest

from pytest_graphql import HeaderAuth


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(gql_config, headers={"X-Layer": "config"})


@pytest.fixture
def gql_headers():
    return {"X-Layer": "fixture"}


@pytest.fixture
def gql_auth():
    return HeaderAuth({"X-Layer": "auth"})


def layer(client, **options):
    response = client.query("user", id="u1", fields=["id"], raw=True, **options)
    return response.request.headers["X-Layer"]


def test_a_higher_source_wins(gql):
    assert layer(gql) == "auth"
    assert layer(gql.anonymous()) == "fixture"
    with gql.with_headers({"X-Layer": "clone"}) as clone:
        assert layer(clone) == "clone"
        assert layer(clone, headers={"X-Layer": "call"}) == "call"
```

Userinfo in the URL, as in `https://user:pass@host/graphql`, supplies a Basic
`Authorization` header only when no layer above set `Authorization`.

### The schema is loaded once

The schema is loaded once for the session, and it is loaded with the
configuration headers. The `gql_headers` and `gql_auth` fixtures are per test, so
they never reach it. The client of a test keeps the session value as
`schema_headers`, so a header of one test cannot become the identity of the schema.

```python {.exec}
import dataclasses

import pytest


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(gql_config, headers={"X-Env": "test"})


@pytest.fixture
def gql_headers():
    return {"X-Tenant": "acme"}


def test_per_test_headers_never_reach_schema_loading(gql):
    assert dict(gql.config.headers) == {"X-Env": "test", "X-Tenant": "acme"}
    assert dict(gql.config.schema_headers) == {"X-Env": "test"}
```

If your schema differs by role, one schema per session is not enough. Override the
`gql_schema_source` fixture with a source of your own that keeps one schema for
each role.

## Run one call as another user

A client can make clones of itself with another identity. A clone shares the
schema, the settings and the scalars of its parent. The parent does not change.

| Method | The clone |
|---|---|
| `as_(token)` | Sends a bearer token. Pass an `Auth` object instead of a string for another kind. |
| `with_auth(auth)` | Uses an `Auth` object. |
| `with_headers(headers)` | Sends more headers. A name already set is replaced. |
| `anonymous()` | Sends no `Auth`. Headers set with `with_headers()` stay. |

```python {.exec}
users = gql.query("users", fields=["id"])

with gql.as_("admin-token-123") as admin, gql.as_("guest-token-456") as guest:
    for client in (admin, guest):
        response = client.query("users", fields=["id"], raw=True)
        assert response.request.headers["Authorization"] == "[redacted:Authorization]"
        assert response.data.users.ids() == users.ids()

    with admin.anonymous() as visitor:
        response = visitor.query("users", fields=["id"], raw=True)
        assert "Authorization" not in response.request.headers

assert "Authorization" not in gql.query("user", id="u1", fields=["id"], raw=True).request.headers
```

`with_headers()` takes a mapping as its first argument, because a header name with
a hyphen is not a keyword. You can also give keywords, and use both in one call.

```python {.exec}
with gql.with_headers({"X-Api-Key": "key-12345678"}, Accept="application/json") as client:
    headers = client.query("user", id="u1", fields=["id"], raw=True).request.headers
    assert headers["Accept"] == "application/json"
    assert headers["X-Api-Key"] == "[redacted:X-Api-Key]"
```

Close a clone you make, or use it in a `with` block. A clone has its own
connection state over the shared pool, and closing it closes only that state. The
`gql` fixture closes itself at the end of its test.

The same clones make a permission matrix. A test runs once for each role, and each
run uses a clone.

```python {.exec}
import pytest

ROLES = {"admin": "admin-token-123", "guest": "guest-token-456"}


@pytest.mark.parametrize("role", ROLES)
def test_each_role_reads_the_user(gql, role):
    with gql.as_(ROLES[role]) as client:
        assert client.query("user", id="u1").name == "Ada Lovelace"
```

## The `Auth` protocol

[`BearerAuth(token)`](api.md#pytest_graphql.BearerAuth) sets
`Authorization: Bearer <token>`.
[`HeaderAuth(headers)`](api.md#pytest_graphql.HeaderAuth) sets a fixed group of
headers, such as an API key. Both hide their secret in `repr()`.

For any other kind of login, write a class with one method. An object that has
`apply(request)` is an [`Auth`](api.md#pytest_graphql.Auth). The method receives
the request that is about to be sent and returns the request to send. The request
is immutable, so build a new one with `dataclasses.replace()`.

The example sends a session id in the `X-Session` header. The name of a header
that carries a secret must be in `redact_headers`, so the example adds it. Without
that, a report would show the session id.

```python {.exec}
import dataclasses
from dataclasses import replace

import pytest

from pytest_graphql import Auth, RequestInfo


class SessionAuth:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    def apply(self, request: RequestInfo) -> RequestInfo:
        return replace(request, headers={**request.headers, "X-Session": self.session_id})


@pytest.fixture(scope="session")
def gql_config(gql_config):
    names = (*gql_config.redact_headers, "x-session")
    return dataclasses.replace(gql_config, redact_headers=names)


def test_the_session_id_is_not_in_the_request_snapshot(gql):
    auth = SessionAuth("s-12345678")
    assert isinstance(auth, Auth)

    with gql.with_auth(auth) as client:
        response = client.query("user", id="u1", fields=["id"], raw=True)

    assert response.request.headers["X-Session"] == "[redacted:X-Session]"
    assert "s-12345678" not in repr(response.request)
```

The same setting in a file is `gql_redact_headers = X-Session`. See
[Configuration](configuration.md).

The request that `apply` receives holds the real values, because your code needs
them. Do not print it or write it to a log. Its `repr()` and `request.redacted()`
hide a header value only when the header name is in `redact_headers`. A header with
another name shows its value, so list every header that carries a secret.

```python {.exec}
from dataclasses import replace

from pytest_graphql import RequestInfo

request = RequestInfo(
    None, "query", "{ a }", {}, {"X-Session": "s-12345678"}, "http://localhost/graphql"
)

assert request.redacted().headers["X-Session"] == "s-12345678"

listed = replace(request, redact_headers=frozenset({"x-session"}))
assert listed.redacted().headers["X-Session"] == "[redacted:X-Session]"
```

## Refresh a token

The client calls `apply` once for every request. So the place to refresh a token
is inside `apply`. There is nothing more to set up. Keep the token and when it
expires, and fetch a new one when it is too old.

```python {.exec}
from pytest_graphql import BearerAuth


class RefreshingAuth:
    """Fetches a new token when the one in use has expired."""

    def __init__(self, fetch_token, clock):
        self._fetch_token = fetch_token
        self._clock = clock
        self._token = None
        self._expires_at = 0.0
        self.sent = []

    def apply(self, request):
        if self._token is None or self._clock() >= self._expires_at:
            self._token, lifetime = self._fetch_token()
            self._expires_at = self._clock() + lifetime
        signed = BearerAuth(self._token).apply(request)
        self.sent.append(signed.headers["Authorization"])
        return signed


now = [0.0]
tokens = iter(["token-aaaaaaaa", "token-bbbbbbbb"])
auth = RefreshingAuth(lambda: (next(tokens), 60.0), clock=lambda: now[0])

with gql.with_auth(auth) as client:
    client.query("user", id="u1", fields=["id"])
    now[0] = 30.0
    client.query("user", id="u1", fields=["id"])
    now[0] = 61.0
    client.query("user", id="u1", fields=["id"])

assert auth.sent == [
    "Bearer token-aaaaaaaa",
    "Bearer token-aaaaaaaa",
    "Bearer token-bbbbbbbb",
]
```

Give the same object to the `gql_auth` fixture to use it in every test of a
session. Create it once, with `scope="session"`, so tests share one token and one
fetch.

```python {.no-exec}
import time

import pytest

from myproject.auth import RefreshingAuth, fetch_test_token


@pytest.fixture(scope="session")
def refreshing_auth():
    return RefreshingAuth(fetch_test_token, clock=time.monotonic)


@pytest.fixture
def gql_auth(refreshing_auth):
    return refreshing_auth
```

## Cookies

By default a client keeps no cookies. It drops every cookie after each response,
so no state passes from one call to the next. That keeps tests apart. A clone
never gets the cookies of its parent either.

To keep the cookies of one client, set `cookie_scope` to `"client"` in
`gql_config`. The cookies belong to that client only. Two clients with different
identities share a connection pool and share no cookies.
