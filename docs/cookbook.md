# Cookbook

After this page you can copy a working pattern for paging through a list, testing
create, update and delete, checking what each role may do, seeding data and
cleaning it up, running tests in parallel and running them in CI.

The examples run against a small demo schema, and some of them use a stand-in for
the part of a server that the demo does not have. A comment says so each time.

## Page through a list

A list that has pages needs a loop. Ask for a page, collect its items, and ask
again with the cursor that the server gave, until the server says that there is no
next page. A server that has a bug can return the same cursor again and again, so
the loop has a limit.

```python {.exec}
from pytest_graphql import Field


def all_titles(gql, user_id, page_size=2, max_pages=100):
    titles = []
    cursor = None
    for _ in range(max_pages):
        args = {"first": page_size}
        if cursor is not None:
            args["after"] = cursor
        connection = Field(
            "postsConnection",
            args=args,
            fields=[
                {"edges": ["cursor", {"node": ["title"]}]},
                {"pageInfo": ["hasNextPage", "endCursor"]},
            ],
        )
        page = gql.query("user", id=user_id, fields=[connection]).posts_connection
        titles += [edge.node.title for edge in page.edges]
        if not page.page_info.has_next_page:
            return titles
        cursor = page.page_info.end_cursor
    raise AssertionError(f"the server still had a next page after {max_pages} pages")


assert all_titles(gql, "u1") == ["Hello World"]
assert all_titles(gql, "u1", page_size=1) == ["Hello World"]
```

Each user of the demo schema has one post, so the loop runs once. Against your data
it runs once for each page. A test that checks the paging itself should create more
items than one page holds, and should use a small `page_size`.

Auto-selection also handles a connection. It asks for the first page only, 10 items
by default. See [Selections](selections.md).

## Create, read, update and delete

A round trip test makes a record, reads it back, changes it, reads it again,
removes it, and checks that it is gone. Data comes from the factory, so the test
needs no hand-written payload. See [Factory](factory.md). This page leaves out file
uploads, which `0.1.x` does not send.

The demo schema stores nothing, so the parts that read a record back need your
server. First, what the demo can show: the answer to a mutation holds the new
record, and the test can check it right away.

```python {.exec}
def test_create_a_post_from_a_generated_payload(gql):
    payload = gql.fake.CreatePostInput(author_id="u1")

    post = gql.mutation("createPost", input=payload)

    assert post == gql.expect.Post(title=payload["title"])
    assert post.author.id == "u1"


def test_an_update_returns_the_changed_record(gql):
    user = gql.mutation("updateUser", id="u1", name="Ada King", fields=["id", "name"])

    assert user.to_dict() == {"id": "u1", "name": "Ada King"}
```

Then the whole round trip against a server that keeps what it is given. It needs
such a server, so the block is not run by the documentation tests.

```python {.no-exec}
from pytest_graphql import unique


def test_a_post_can_be_created_read_updated_and_deleted(gql):
    payload = gql.fake.CreatePostInput(author_id="u1", title=unique())

    created = gql.mutation("createPost", input=payload)
    assert gql.query("post", id=created.id) == gql.expect.Post(title=payload["title"])

    gql.mutation("updatePost", id=created.id, title="A new title")
    assert gql.query("post", id=created.id).title == "A new title"

    gql.mutation("deletePost", id=created.id)
    assert gql.query("post", id=created.id) is None
```

If the test fails half way, the record stays. The next section removes it in a
fixture, so the cleanup runs in every case.

## Test who may do what

A permission matrix lists the roles and what each one may do. Write the matrix as
data. Run one test for each cell, so that a failure names the role and the
operation. A clone of the client, made with `as_()`, sends the identity of a role.
See [Authentication](authentication.md).

A call that is not allowed must fail with the error that your server uses. The
example expects `FORBIDDEN`. The `Gatekeeper` class is the stand-in for the access
rules of a server. Delete it, and the `gql_transport` fixture that wraps it, to
test your own server.

```python {.exec}
import dataclasses

import pytest

TOKENS = {
    "admin": "admin-token-123",
    "editor": "editor-token-456",
    "guest": "guest-token-789",
}

# The matrix. A role may call the operations in its set, and no others.
MAY = {
    "admin": {"users", "updateUser", "createPost"},
    "editor": {"users", "updateUser"},
    "guest": {"users"},
}

CALLS = {
    "users": ("query", {"fields": ["id"]}),
    "updateUser": ("mutation", {"id": "u1", "name": "Ada", "fields": ["id"]}),
    "createPost": ("mutation", {"title": "Hello", "author_id": "u1", "fields": ["id"]}),
}


class Gatekeeper:
    # Plays the server. It answers FORBIDDEN when the role of the token may not
    # call the operation, and passes every other request on.

    def __init__(self, inner):
        self.inner = inner

    def send(self, request, *, timeout):
        raw = self.inner.send(request, timeout=timeout)
        if request.operation not in CALLS:
            return raw
        header = request.headers.get("Authorization", "")
        role = next((r for r, t in TOKENS.items() if header == f"Bearer {t}"), None)
        if request.operation in MAY.get(role, ()):
            return raw
        error = {
            "message": "Not allowed.",
            "path": [request.operation],
            "extensions": {"code": "FORBIDDEN"},
        }
        return dataclasses.replace(raw, data=None, errors=(error,))

    def close(self):
        self.inner.close()


@pytest.fixture(scope="session")
def gql_transport(gql_transport):
    return Gatekeeper(gql_transport)


CELLS = [(role, name) for role in MAY for name in CALLS]


@pytest.mark.parametrize(("role", "name"), CELLS, ids=["-".join(c) for c in CELLS])
def test_each_role_may_call_only_what_the_matrix_says(gql, role, name):
    kind, arguments = CALLS[name]

    with gql.as_(TOKENS[role]) as client:
        call = getattr(client, kind)
        if name in MAY[role]:
            call(name, **arguments)
        else:
            with client.expect_error(code="FORBIDDEN"):
                call(name, **arguments)


def test_a_call_without_a_token_is_not_allowed(gql):
    with gql.anonymous() as visitor, visitor.expect_error(code="FORBIDDEN"):
        visitor.query("users", fields=["id"])
```

The matrix is data, so a new role is one more line. Keep the cells that must fail.
A matrix that checks only what each role may do will not notice a role that may do
too much.

## Seed data and clean it up

A test that needs a record makes it in a fixture. The code after `yield` runs when
the test ends, whether the test passed or failed, so it is the place for cleanup.
Use `unique()` for a field that has a unique constraint, so that parallel runs and
repeated runs do not collide. See [Factory](factory.md).

The demo has no delete mutation, so the cleanup in the example records the id. The
comment shows the call that a real suite makes.

The creation and the cleanup are one context manager, so a test can check that the
cleanup ran, inside that test. The fixture wraps the same context manager.

```python {.exec}
from contextlib import contextmanager

import pytest

from pytest_graphql import unique


@contextmanager
def temporary_post(gql, removed):
    payload = gql.fake.CreatePostInput(author_id="u1", title=unique())
    created = gql.mutation("createPost", input=payload)
    try:
        yield created
    finally:
        # A real suite removes the record here:
        #
        #     gql.mutation("deletePost", id=created.id)
        #
        removed.append(created.id)


@pytest.fixture
def post(gql):
    with temporary_post(gql, removed=[]) as created:
        yield created


def test_the_post_has_the_title_that_was_sent(post):
    assert post.title
    assert post.author.id == "u1"


def test_the_cleanup_runs_when_the_block_fails(gql):
    removed = []

    with pytest.raises(RuntimeError):
        with temporary_post(gql, removed) as created:
            assert removed == []
            raise RuntimeError("the test body failed")

    assert removed == [created.id]
```

Data that many tests share is cheaper to make once. A fixture with
`scope="session"` cannot use `gql`, which belongs to one test. Build a client of
its own from the session fixtures instead, and close it.

```python {.exec}
import pytest

from pytest_graphql import build_client


@pytest.fixture(scope="session")
def shared_user(gql_url, gql_transport, gql_schema, gql_config):
    with build_client(
        url=gql_url, transport=gql_transport, schema=gql_schema, config=gql_config
    ) as client:
        yield client.mutation("updateUser", id="u1", name="Shared", fields=["id", "name"])


def test_a_test_reads_the_shared_user(shared_user):
    assert shared_user.name == "Shared"


def test_another_test_gets_the_same_one(shared_user):
    assert shared_user.id == "u1"
```

Keep shared data read-only. A test that changes it makes the result of the others
depend on the order.

## Run tests in parallel

With `pytest-xdist`, `pytest -n auto` starts one worker process for each core.

```bash
pip install pytest-xdist
pytest -n auto
```

The plugin works with it, and some facts follow from the workers being separate
processes.

- **Each worker loads the schema once.** There is one introspection request for
  each worker, not for each test. The `GraphQL` section at the end of the run has a
  line for each worker.
- **The seed is the same on every worker.** `--gql-seed=random` is chosen once, by
  the controller, and sent to the workers. A test makes the same payload on every
  run, on whichever worker takes it. A field with a unique constraint still needs
  `unique()`.
- **`unique()` values differ between workers.** They carry the run id and the id of
  the worker.
- **A session fixture runs once for each worker.** A fixture that seeds data in a
  shared server seeds it once for each worker. Give the data a `unique()` name, or
  make the fixture safe to run twice.
- **Tests must not depend on each other.** A worker takes the next test that is
  free, so the order changes from run to run. `--dist loadfile` keeps the tests of
  one file on one worker, which helps a file whose tests share data.

```bash
pytest -n auto --dist loadfile
```

## Run in CI

A CI job needs the endpoint, a token and a way to repeat a failure. Set the first
two with environment variables. The names are in
[Configuration](configuration.md). `PYTEST_GQL_HEADERS` sets headers for the whole
run, and the schema request carries them too. Take the token from the secret store
of the CI system, and never write it in the file.

`--gql-seed=random` chooses a new seed for every run. The report header shows it,
so a failed run can be repeated with `--gql-seed=<that number>`.

```yaml
name: api-tests

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  api:
    runs-on: ubuntu-latest
    env:
      PYTEST_GQL_URL: ${{ vars.STAGING_GRAPHQL_URL }}
      PYTEST_GQL_HEADERS: |
        Authorization: Bearer ${{ secrets.STAGING_GRAPHQL_TOKEN }}
        X-Environment: ci
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install "pytest-graphql[pytest]" pytest-xdist
      - run: pytest -n auto --gql-seed=random
```

The `Authorization` header is hidden in every report, because its name is in the
default list. The failure section of a failed test is in the job log. See
[Diagnostics](diagnostics.md). To keep a name like `X-Session` out of the log too,
add it with `PYTEST_GQL_REDACT_HEADERS`.
