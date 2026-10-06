# Using without pytest

After this page you can use the client in a `unittest` suite, in a script or in a
Python shell, and you know what the pytest plugin did for you that you must now do
yourself.

```python {.exec}
from pytest_graphql import build_client
```

This package does not install pytest. The core imports no pytest, so it works in
any Python program. `pip install pytest-graphql` is enough.

## Build a client

`build_client()` returns a [`GraphQLClient`](api.md#pytest_graphql.GraphQLClient).
It is the same client that the `gql` fixture gives you. Without a `transport`, it
opens a connection pool and loads the schema from the server with one
introspection request. So the client owns a pool, and you must close it. Use a
`with` block, or call `close()`.

The page has no server, so its examples pass a `transport` and a `schema` of a
demo client. These two arguments are the only difference from a real run. Against
your server, leave them out.

```python {.exec}
from pytest_graphql import build_client

# Against a real server this is all you write:
#
#     with build_client(url="http://localhost:8000/graphql") as client:
#
with build_client(
    url="http://localhost:8000/graphql",
    transport=gql.transport,
    schema=gql.schema,
) as client:
    user = client.query("user", id="u1")
    assert user.name == "Ada Lovelace"
```

Every other keyword is a field of
[`ClientConfig`](api.md#pytest_graphql.ClientConfig), such as `headers`, `timeout`,
`max_depth` or `raise_on_error`. A name that is not a field raises `ArgumentError`
and lists the valid names, so a misspelled option never leaves you on a default.

```python {.exec}
from pytest_graphql import ArgumentError, build_client

try:
    build_client(
        url="http://localhost:8000/graphql",
        schema=gql.schema,
        transport=gql.transport,
        max_deph=2,
    )
except ArgumentError as error:
    assert "max_deph" in str(error)
else:
    raise AssertionError("a misspelled option must be refused")
```

The `headers` that you give here are also sent with the schema request. An
endpoint that needs a login before it shows its schema needs them there. To run
as a user, pass `auth=BearerAuth("...")`, or use `with_auth()` and `as_()` on the
client. See [Authentication](authentication.md).

## A `unittest` suite

There is no built-in `unittest` base class in `0.1.0`. Build the client in
`setUpClass` and close it in `tearDownClass`. A client is made once for the class,
because every `build_client()` without a `schema` makes one introspection request.

The next example has a small function, `connect()`, for the demo. Its comment shows
the line that you write against a real server.

```python {.exec}
import unittest

from pytest_graphql import OperationNotFoundError, build_client


def connect():
    # Against a real server, this is the whole function:
    #
    #     return build_client(url="http://localhost:8000/graphql")
    #
    return build_client(
        url="http://localhost:8000/graphql",
        transport=gql.transport,
        schema=gql.schema,
    )


class UserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = connect()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_user_has_a_name(self):
        user = self.client.query("user", id="u1")
        self.assertEqual(user.name, "Ada Lovelace")

    def test_a_user_matches_part_of_an_object(self):
        user = self.client.query("user", id="u1")
        self.assertEqual(user, self.client.expect.User(name="Ada Lovelace"))

    def test_an_unknown_query_name_is_refused(self):
        with self.assertRaises(OperationNotFoundError):
            self.client.query("usr")

    def test_a_missing_user_is_an_error_that_the_test_expects(self):
        with self.client.expect_error(count=1):
            self.client.mutation("updateUser", id="missing", name="Ada", fields=["id"])


suite = unittest.defaultTestLoader.loadTestsFromTestCase(UserTests)
result = unittest.TextTestRunner(verbosity=2).run(suite)

assert result.wasSuccessful()
assert result.testsRun == 4
```

The matchers, `expect_error()`, `wait_until()` and the factory are methods of the
client, so they work in `unittest` as they do in pytest. See
[Assertions](assertions.md), [Errors](errors.md), [Polling](polling.md) and
[Factory](factory.md).

The last three lines of the example run the suite from code and check the result.
In a file of your own, end the file with `unittest.main()` instead, and run it with
`python test_users.py` or `python -m unittest`. `unittest.main()` ends the process
with `SystemExit` after the run, unless you pass `exit=False`.

## A script

A script that checks a server, or prepares data, needs the same three things: a
client, a `try` block that closes it, and an exit code. Put the work in a function
that takes the client. The function is easy to test, and the entry point stays
small.

```python {.exec}
import sys


def main(client):
    users = client.query("users", fields=["id", "name"])
    missing = [user.id for user in users if not user.name]
    for user in users:
        print(f"{user.id}: {user.name}")
    if missing:
        print(f"users without a name: {missing}", file=sys.stderr)
        return 1
    return 0


assert main(gql) == 0
```

The entry point builds the client, runs `main()` and passes its result to the
shell. This block needs a server, so it is not run by the documentation tests.

```python {.no-exec}
import os

from pytest_graphql import build_client

if __name__ == "__main__":
    url = os.environ["GRAPHQL_URL"]
    headers = {"Authorization": f"Bearer {os.environ['GRAPHQL_TOKEN']}"}
    with build_client(url=url, headers=headers) as client:
        raise SystemExit(main(client))
```

The core reads no `PYTEST_GQL_*` environment variable. Read your own variables, as
the example does, and pass the values as arguments.

## In a Python shell

Build one client, and keep it for the whole session. Each `build_client()` call
loads the schema again. When you are done, call `client.close()`.

## What the plugin did, and what you do now

| Under pytest | Without pytest |
|---|---|
| The `gql` fixture builds and closes the client. | You call `build_client()` and `close()`. |
| Ini options, flags and `PYTEST_GQL_*` variables set the configuration. | You pass keywords or a `ClientConfig`. |
| `gql_auth` and `gql_headers` set the identity of a test. | You pass `auth=` or call `with_auth()`. |
| One schema load for the whole session. | One load for each client. Build the client once. |
| The six hooks. | Middleware. It runs around every call. See [Extending](extending.md). |
| The `GraphQL calls` section and `--gql-log`. | The exception messages, and the `recorder` of the client. See [Diagnostics](diagnostics.md). |
| `gql.fake` is seeded from the id of the test. | `gql.fake` is seeded with 0 and a fixed name, so every test gets the same payload. Mark a field with `unique()` to make it differ. |
| `unique()` values carry the run id and the worker id. | `unique()` values carry a random id of the process. |
