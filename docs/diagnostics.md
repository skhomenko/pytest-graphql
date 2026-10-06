# Diagnostics

After this page you can read the report of a failed test, log every call of a
passing run, keep secrets out of reports, and repeat a failed call with `curl`.

The examples on this page use this configuration. Every report below comes from a
real run of the files that are shown above it.

```ini {title="pytest.ini"}
[pytest]
gql_url = http://localhost:8000/graphql
```

## Read a failure section

When a test fails, pytest adds a section to its report. The section is called
`GraphQL calls`. It lists the calls that the test made, so you do not need to
add `print()` calls and run the test again.

This test fails on purpose. A user that does not exist cannot be renamed. The
documentation tests do not run this block as a normal example, because a passing
test is what they look for. They run it in a separate check and compare the
report below with the real output, line by line.

```python {.no-exec title="test_rename.py"}
import pytest


@pytest.fixture
def gql_auth():
    return "token-PLACEHOLDER"


@pytest.fixture
def gql_headers():
    return {"X-Session": "session-PLACEHOLDER"}


def test_rename_a_user_that_does_not_exist(gql):
    gql.query("user", id="u1", fields=["id", "name"])
    gql.mutation(
        "updateUser", id="secret-PLACEHOLDER", name="Grace", fields=["id", "name"]
    )
```

```bash
pytest test_rename.py
```

Pytest prints the traceback first. The section follows it. The times in your run
will differ. This is the section:

```text
------------------------------ GraphQL calls (2) -------------------------------
[1] query user  200  1ms
    query user($id: ID!) { user(id: $id) { id name } }
    variables: {"id": "u1"}
    data: {"user": {"id": "u1", "name": "Ada Lovelace"}}

[2] mutation updateUser  200  1ms   <-- FAILED HERE
    mutation updateUser($id: ID!, $name: String) { updateUser(id: $id, name: $name) { id name } }
    variables: {"id": "secret-PLACEHOLDER", "name": "Grace"}
    errors:
      - (no code) at ["updateUser"]: 'secret-PLACEHOLDER'
    reproduce:
      curl -sS -X POST http://localhost:8000/graphql -H 'X-Session: session-PLACEHOLDER' -H 'Authorization: '"${PYTEST_GQL_HEADER_AUTHORIZATION}" --data '{"operationName": "updateUser", "query": "mutation updateUser($id: ID!, $name: String) {\n  updateUser(id: $id, name: $name) {\n    id\n    name\n  }\n}", "variables": {"id": "secret-PLACEHOLDER", "name": "Grace"}}'
```

The title shows how many calls the test made. A call has a heading and some
lines under it. A line is shown only when the call has something to put in it.

- **The heading** is `[number] kind operation  status  time`. The kind is `query`
  or `mutation`. The status is the HTTP status, or `-` when no answer came. The
  time is in milliseconds.
- **`FAILED HERE`** marks the last call that the test made. That call is the
  nearest to the failure. The section lists every call, so a test that failed on
  the result of an earlier call still shows it.
- **The document** is on one line. The `curl` line has the exact text.
- **`variables`** shows the variables as JSON.
- **`skipped`** lists the fields that auto-selection left out, with the reason.
  See [Selections](selections.md). These tests used `fields=`, so there are none.
- **`data`** shows the data of the answer, cut when it is long. A cut ends with
  `(truncated, N fields)`.
- **`errors`** shows the first errors of the answer, one line each. The server's
  text is in the line, after the code and the path. A message that has line breaks
  continues under its own entry.
- **`failure`** appears when the call raised before any answer came, for example a
  connection error.
- **`reproduce`** is a `curl` command. Only the last call has one.

The client keeps the last 50 calls of a test. If it dropped older ones, the title
reads `GraphQL calls (last 50 of 73)`, and the numbers continue from the real
count. `--show-capture=no` hides the section, as it hides every captured section.

## Log every call

The failure section covers only failed tests. To see the calls of tests that pass,
use `--gql-log`. Each call is written to the logger `pytest_graphql.calls` at
level `INFO`. Pytest shows the log of a test that fails. To see all of it as the
run goes, ask pytest to show live logs:

```python {.exec title="test_users.py"}
def test_users_are_listed(gql):
    users = gql.query("users", fields=["id", "name"])
    assert len(users) == 3


def test_a_user_can_be_renamed(gql):
    user = gql.mutation("updateUser", id="u1", name="Ada King", fields=["id", "name"])
    assert user.name == "Ada King"
```

```bash
pytest test_users.py --gql-log -o log_cli=true -o log_cli_level=INFO -o log_cli_format="%(message)s"
```

The log lines are:

```text
query users POST http://localhost:8000/graphql -> ok (status 200, 0.4 ms, 0 error(s))
mutation updateUser POST http://localhost:8000/graphql -> ok (status 200, 0.7 ms, 0 error(s))
```

A line says the kind, the operation, the method, the URL, the outcome, the status,
the time, and how many errors the answer had. The outcome is `ok`, `errors` or
`failed`.

`--gql-log-level full` adds the lines of the failure section for each call, without
the heading, the marker and the `reproduce:` line:

```bash
pytest test_users.py::test_a_user_can_be_renamed --gql-log --gql-log-level full -o log_cli=true -o log_cli_level=INFO -o log_cli_format="%(message)s"
```

```text
mutation updateUser POST http://localhost:8000/graphql -> ok (status 200, 0.7 ms, 0 error(s))
    mutation updateUser($id: ID!, $name: String) { updateUser(id: $id, name: $name) { id name } }
    variables: {"id": "u1", "name": "Ada King"}
    data: {"updateUser": {"id": "u1", "name": "Ada King"}}
```

The log goes through the same redaction as the failure section, which the next
section describes. The plugin sets the
level of its logger for the session and restores it after. What pytest shows is
decided by the logging options of pytest, so you can send it to a file with
`--log-file` and `--log-file-level=INFO`.

## Keep secrets out of the report

A report is read by many people and is often saved by CI. So the section hides
every value that it can recognize as a secret. It recognizes a value by the name
of the header or the variable that carries it, and by having seen the value
before. A secret under a name that no rule lists is not recognized, and the report
shows it. Before you share reports, list the name of every header and variable
that holds a secret.

Look at the first report again. The test sent a token in the `Authorization`
header, and it is not in the report. The `curl` line has a shell variable in its
place.

Three kinds of values are hidden:

- **Header values.** Names in `redact_headers` are hidden. The default names are
  `authorization`, `cookie`, `proxy-authorization` and `x-api-key`.
- **Variables.** Patterns in `redact_variables` hide a variable at any depth, and
  the same patterns hide a field of the answer with that name. The default
  patterns are `password`, `token`, `secret`, `api_key`, `access_token`,
  `refresh_token`, `authorization`, `otp`, `pin`, `credit_card` and `ssn`.
  A pattern is a name or a dotted path, and it can use `*`.
- **Known values in free text.** A server can repeat a secret in an error message.
  A value that the client knows to be a secret is removed from every text that
  comes from outside, if it has 8 or more characters. The value counts as known when it is
  the value of a hidden header, of a hidden variable, a cookie, or a part of the
  URL.

The first report shows that a name that is not listed is not hidden. `X-Session`
carries a session, but it is not in the default list, so its value is in the
`curl` line. In the same way, the test sent `id="secret-PLACEHOLDER"` and the
server repeated it in its error message. Neither `id` nor `X-Session` is secret by
default. Here the example treats both as secrets. Add the header name in the ini
file, and the variable name in `gql_config`:

```ini {title="pytest.ini"}
[pytest]
gql_url = http://localhost:8000/graphql
gql_redact_headers = X-Session
```

```python {.no-exec title="conftest.py"}
import dataclasses

import pytest


@pytest.fixture(scope="session")
def gql_config(gql_config):
    names = (*gql_config.redact_variables, "id")
    return dataclasses.replace(gql_config, redact_variables=names)
```

The option `gql_redact_headers` adds names to the defaults. A list that you give to
`ClientConfig.redact_headers` replaces them, so include the default names there.
There is no ini option for variable patterns. Set `redact_variables` in
`gql_config`, as shown.

Run the failing test again:

```bash
pytest test_rename.py
```

```text
------------------------------ GraphQL calls (2) -------------------------------
[1] query user  200  1ms
    query user($id: ID!) { user(id: $id) { id name } }
    variables: {"id": "[redacted:id]"}
    data: {"user": {"id": "[redacted]", "name": "Ada Lovelace"}}

[2] mutation updateUser  200  1ms   <-- FAILED HERE
    mutation updateUser($id: ID!, $name: String) { updateUser(id: $id, name: $name) { id name } }
    variables: {"id": "[redacted:id]", "name": "Grace"}
    errors:
      - (no code) at ["updateUser"]: '[redacted:id]'
    reproduce:
      curl -sS -X POST http://localhost:8000/graphql -H 'X-Session: '"${PYTEST_GQL_HEADER_X_2D_SESSION}" -H 'Authorization: '"${PYTEST_GQL_HEADER_AUTHORIZATION}" --data '{"operationName": "updateUser", "query": "mutation updateUser($id: ID!, $name: String) {\n  updateUser(id: $id, name: $name) {\n    id\n    name\n  }\n}", "variables": {"id": "[redacted:id]", "name": "Grace"}}'
```

The variable is a marker with its name. The `id` field of the answer is a bare
`[redacted]`. The server's error message holds the secret too, and there it is
replaced by the same marker. The `X-Session` header is a shell variable now.

There are limits that you should know:

- A secret that no rule covers is not hidden. List the name of every header and
  variable that holds one.
- A known value that is shorter than `min_redacted_value_length` (8 by default) is
  not removed from free text, because it would match ordinary words. Give test
  accounts credentials that are at least that long.
- `redact_values=False` turns the removal from free text off. Only the listed
  names are hidden then.
- When the client cannot be sure that a text is free of a secret, it shows
  `[withheld: this text would show a redacted value]` instead of the text. This
  includes the message of an exception. A withheld text means that a hidden value
  came back somewhere you did not expect.
- The URL has no user name, no password and no query string in a report. A cookie
  is shown by name only.

The `RequestInfo` that `Auth` and middleware receive holds the real values,
because they need them. Do not print it or write it to a log. Use
`request.redacted()`, and list every header name that carries a secret, because
a header with another name shows its value. See [Extending](extending.md).

## Reproduce a call with `curl`

The `reproduce:` line of the report is a `curl` command. It posts the same
document and variables to the same URL. A header with a hidden value is written as
a shell variable. The name of the variable is `PYTEST_GQL_HEADER_` and the header
name in capital letters. A character that is not a letter or a digit becomes an
underscore, the hexadecimal code of the character, and an underscore. So
`X-Session` becomes `X_2D_SESSION`, and `Authorization` stays `AUTHORIZATION`.

Put the real value in the shell, and paste the command:

```bash
export PYTEST_GQL_HEADER_AUTHORIZATION='Bearer <your token>'
export PYTEST_GQL_HEADER_X_2D_SESSION='<your session>'
curl -sS -X POST http://localhost:8000/graphql ...
```

The command comes from the call as the client sent it, after `Auth` and middleware
changed it. A call that is not in a report, for example in a script, is on the
recorder of the client. Each recorded call keeps its `curl` text.

```python {.exec}
response = gql.query("user", id="u1", fields=["id", "name"], raw=True)

command = gql.recorder.calls[-1].curl

assert command.startswith(f"curl -sS -X POST {response.request.url}")
assert '"variables": {"id": "u1"}' in command
```

## The header and the summary

The plugin prints two lines at the start of a run, and a `GraphQL` section at the
end. A line of three dots stands for the lines that pytest prints between them.

```bash
pytest test_users.py
```

```text
graphql: endpoint http://localhost:8000/graphql, seed 0, run id 444136ffab844da6b11687476182758a
graphql: schema loads when the first test needs it, and is listed at the end of the run
...
=================================== GraphQL ====================================
schema main: introspection:endpoint, 26 types, 16 queries, 3 mutations, 0 subscriptions, loaded in 0.016s
```

The endpoint has no user name and no query string. The seed is the one that
[`gql.fake`](factory.md) uses. The run id is the one that `unique()` values carry.

The schema loads when the first test needs it, so its facts come at the end of the
run. There is one line for each process that loaded a schema. Outside `pytest-xdist`
the process is `main`. With `-n`, each worker has a line, and a last line says how
many workers loaded the schema. See [Cookbook](cookbook.md). The load time is on
the line, so a slow start has an explanation. `--gql-show-schema-stats` adds the
fingerprint, the number of types of each kind, the number of fields and the load
time.

## Add your own section

A project can add text to the failure report of a test, for example the state of
a database row. That is the hook `pytest_graphql_report_section`. See
[Extending](extending.md).
