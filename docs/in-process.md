# Testing an app in process

After this page you can test your WSGI or ASGI GraphQL application without
starting a server. The client calls the application in the same process as the
test. Requests are built, checked and reported the same way as over HTTP, so the
rest of this guide still applies.

## Configuration

Name the application in `pytest.ini`, as `package.module:attribute`:

```ini
[pytest]
gql_app = myproject.asgi:app
```

The environment variable `PYTEST_GQL_APP` works the same way. The plugin
imports the application when the first test needs it. When no source sets a
URL, the client uses `http://testserver/graphql`. Set `gql_url` to use another
path on that host.

The `pytest-graphql` command reads the schema of the same application with
`--app module:attribute` or `PYTEST_GQL_APP`, as described on the Configuration page.

An application that a factory builds comes from a `gql_app` fixture in
`conftest.py` instead:

```python {.no-exec}
import pytest

from myproject import create_app


@pytest.fixture(scope="session")
def gql_app():
    return create_app(testing=True)
```

There is no network, so `gql_verify` or the `ClientConfig` fields `proxy`,
`verify`, `trust_env` and `http2` cannot apply. Setting one of them together with
`gql_app` stops the test with a message that names both.

## Strawberry

Checked with Strawberry 0.336.0. The `GraphQL` application of `strawberry.asgi`
does not support the ASGI lifespan events, and the default `lifespan="auto"`
goes on without them.

```python {.no-exec}
# myproject/asgi.py, named by gql_app = myproject.asgi:app
import strawberry
from strawberry.asgi import GraphQL


@strawberry.type
class Query:
    @strawberry.field
    def hello(self) -> str:
        return "world"


app = GraphQL(strawberry.Schema(query=Query))
```

```python {.no-exec}
def test_hello(gql):
    assert gql.query("hello") == "world"
```

## Ariadne

Checked with Ariadne 1.1.1. Its `GraphQL` application of `ariadne.asgi` does not
support the lifespan events either, and `lifespan="auto"` goes on without them.

```python {.no-exec}
# myproject/asgi.py, named by gql_app = myproject.asgi:app
from ariadne import QueryType, make_executable_schema
from ariadne.asgi import GraphQL

query = QueryType()


@query.field("hello")
def resolve_hello(*_):
    return "world"


app = GraphQL(make_executable_schema("type Query { hello: String! }", query))
```

## FastAPI

Checked with FastAPI 0.146.0 and Strawberry 0.336.0. The lifespan of the
application runs its startup before the first test and its shutdown at the end
of the session. The state it yields reaches every request.

```python {.no-exec}
# myproject/main.py, named by gql_app = myproject.main:app
from contextlib import asynccontextmanager

import strawberry
from fastapi import FastAPI
from strawberry.fastapi import GraphQLRouter


@asynccontextmanager
async def lifespan(app):
    yield {"greeting": "hello from lifespan"}


@strawberry.type
class Query:
    @strawberry.field
    def greeting(self, info: strawberry.Info) -> str:
        return info.context["request"].state.greeting


app = FastAPI(lifespan=lifespan)
app.include_router(GraphQLRouter(strawberry.Schema(query=Query)), prefix="/graphql")
```

## Graphene with Django

Checked with Graphene 3.4.3, graphene-django 3.2.3, Django 6.1.2 and
pytest-django 4.14.0. Django runs over WSGI, so the application runs in the
test's own thread and sees the test database of pytest-django. A row that a test
creates is visible to the application, and each test starts from an empty
database.

```ini
[pytest]
DJANGO_SETTINGS_MODULE = mysite.settings
gql_app = mysite.wsgi:application
```

```python {.no-exec}
# mysite/urls.py
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from graphene_django.views import GraphQLView

urlpatterns = [path("graphql", csrf_exempt(GraphQLView.as_view()))]
```

```python {.no-exec}
import pytest

from books.models import Book


@pytest.mark.django_db
def test_a_row_the_test_creates_is_seen_by_the_app(gql):
    Book.objects.create(title="Dune")
    assert [book.title for book in gql.query("books")] == ["Dune"]
```

## Without pytest

[`HttpxTransport.for_app()`](api.md#pytest_graphql._core.transport.httpx_transport.HttpxTransport.for_app)
builds the same transport. Close it when you are done. The URL can be a path,
which is joined to `http://testserver`.

```python {.exec}
import json

from pytest_graphql import build_client
from pytest_graphql._core.transport.httpx_transport import HttpxTransport


def app(environ, start_response):
    start_response("200 OK", [("Content-Type", "application/json")])
    return [json.dumps({"data": {"users": []}}).encode()]


transport = HttpxTransport.for_app(app)
try:
    client = build_client(url="/graphql", transport=transport, schema=gql.schema)
    assert client.query("users", fields=["id"]) == []
finally:
    transport.close()
```

The application is ASGI when it is an `async` function, or an object whose
`__call__` is one. Anything else is called as WSGI. Pass `interface="asgi"` or
`interface="wsgi"` to choose.

## Timeouts

The two interfaces differ here, and neither can stop application code that is
already running.

- **ASGI.** The application runs on an event loop in a background thread. The
  response head and each part of the body wait at most the timeout. The request
  is then cancelled with `GraphQLTimeoutError`, as a read timeout over HTTP is.
  A cancellation takes effect only at an `await`. An application that blocks
  the event loop, for example with `time.sleep()`, keeps it busy, and each later
  request times out the same way until the loop is free. The body is read part
  by part, so `max_response_bytes` stops a large response before the rest of it
  is in memory.
  After the lifespan shutdown, closing the transport waits at most twice the
  connect timeout, of which a task still running gets at most the connect
  timeout to finish its cleanup.
- **WSGI.** The application runs in the test's own thread, so the timeout limits
  nothing. A slow application delays the test, and a hung one hangs it. This
  keeps per-thread state working, such as the Django test database. Use
  [pytest-timeout](https://pypi.org/project/pytest-timeout/) for a deadline on
  the whole test.

## Lifespan

For an ASGI application, the transport sends the lifespan startup event when it
starts and the shutdown event when it closes. Each waits at most the connect
timeout. `lifespan="auto"`, the default, goes on without the events when the
application does not support them. `lifespan="on"` makes that an error.
`lifespan="off"` sends neither event. A WSGI application has no lifespan.

A startup that fails ends in one of three ways:

| What happens | What is raised |
|---|---|
| The application raises an exception, under `lifespan="on"` | That exception, unchanged. |
| The application sends `lifespan.startup.failed` | `RuntimeError` with the message the application sent. This holds under `"auto"` too. |
| The startup takes longer than the connect timeout | `TimeoutError`. |

In each case the event loop and its thread are stopped before the error reaches
the test.

## Application errors

An exception that the application raises reaches the test unchanged, as with the
test client of a framework, because it is the test's own process. This holds wherever the
application's code runs, including inside a WSGI body generator. Pass
`raise_app_errors=False` to `HttpxTransport.for_app()` to receive a 500 response
instead, which the client reports as `GraphQLHTTPStatusError`. When the response
body has already started, the error ends the body there instead, and the client
reports the incomplete body as `GraphQLTransportError`.

A `SystemExit` that an ASGI application raises is handled the same way, in a
request and in the lifespan. It does not stop the event loop.

An `httpx` error that the application raises itself also reaches the test
unchanged. An error that `httpx` raises while it reads the response, for example
for a body that is not valid gzip, is reported as over HTTP.
