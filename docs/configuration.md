# Configuration

After this page you can set any option from a file, the command line, an
environment variable or code. You can also tell which source wins when two of
them disagree.

## The options

Every setting has one name in `pytest.ini` (or `pyproject.toml`, `tox.ini` or
`setup.cfg`). Its environment variable is the ini name in upper case with the
prefix `PYTEST_GQL_`, so `gql_max_depth` becomes `PYTEST_GQL_MAX_DEPTH`. A few
settings also have a command line flag.

| Ini option | Flag | Environment variable | Default | Accepts |
|---|---|---|---|---|
| `gql_url` | `--gql-url URL` | `PYTEST_GQL_URL` | `none` | A URL with no spaces. |
| `gql_headers` | `none` | `PYTEST_GQL_HEADERS` | `empty` | Lines of `Name: value`. They are also sent when the schema is loaded. |
| `gql_timeout` | `--gql-timeout SECONDS` | `PYTEST_GQL_TIMEOUT` | `30` | A number of seconds above zero, or `inf` for no limit. |
| `gql_retries` | `none` | `PYTEST_GQL_RETRIES` | `2` | A whole number, 0 or more. |
| `gql_verify` | `none` | `PYTEST_GQL_VERIFY` | `true` | `true`, `false`, or the path of an existing CA bundle file. |
| `gql_max_depth` | `--gql-max-depth N` | `PYTEST_GQL_MAX_DEPTH` | `3` | A whole number, 1 or more. |
| `gql_cycle_policy` | `none` | `PYTEST_GQL_CYCLE_POLICY` | `shallow` | `stop`, `shallow` or `id_only`. |
| `gql_per_type_depth_cap` | `none` | `PYTEST_GQL_PER_TYPE_DEPTH_CAP` | `empty` | Lines of `Type=N`, where N is a whole number, 0 or more. |
| `gql_include_deprecated` | `none` | `PYTEST_GQL_INCLUDE_DEPRECATED` | `false` | `true` or `false`. |
| `gql_max_fields` | `none` | `PYTEST_GQL_MAX_FIELDS` | `2000` | A whole number, 1 or more. |
| `gql_exclude` | `none` | `PYTEST_GQL_EXCLUDE` | `empty` | Lines of `Type.field`, `*.field` or `Type.*`. |
| `gql_relay_aware` | `none` | `PYTEST_GQL_RELAY_AWARE` | `true` | `true` or `false`. |
| `gql_validate` | `--gql-no-validate` | `PYTEST_GQL_VALIDATE` | `true` | `true` or `false`. The flag turns validation off and takes no value. |
| `gql_seed` | `--gql-seed N` | `PYTEST_GQL_SEED` | `0` | A whole number. The flag also accepts `random`. |
| `gql_redact_headers` | `none` | `PYTEST_GQL_REDACT_HEADERS` | `authorization, cookie, proxy-authorization, x-api-key` | Header names, one per line. They are added to the default names. |
| `gql_schema_source` | `none` | `PYTEST_GQL_SCHEMA_SOURCE` | `pytest_graphql.IntrospectionSource` | The dotted path of a schema source object, such as `myproject.schemas.SOURCE`. |
| `none` | `--gql-log` | `none` | `off` | A switch. It logs every request and response, not only failures. |
| `none` | `--gql-log-level LEVEL` | `none` | `summary` | `summary` or `full`. It has an effect only with `--gql-log`. |
| `none` | `--gql-show-schema-stats` | `none` | `off` | A switch. It prints the schema size, the operation counts and the load time. |

A boolean is case-insensitive. `true`, `yes`, `y`, `on`, `t` and `1` mean true.
`false`, `no`, `n`, `off`, `f` and `0` mean false.

A list option holds one entry per line, in the ini file and in the environment
variable alike. `gql_redact_headers` also accepts a comma between two names.

```ini
# pytest.ini
[pytest]
gql_url = http://localhost:8000/graphql
gql_headers =
    X-Environment: test
    X-Request-Source: pytest
gql_max_depth = 2
gql_exclude =
    User.balance
    *.preferences
```

A value that is empty, or only spaces, counts as not set in every source. So
`PYTEST_GQL_URL=` does not hide the URL in your ini file.

The ini option `gql_headers` is not the same thing as the `gql_headers` fixture.
The option sets headers for the whole run, and the schema request carries them.
The fixture sets headers for one test. See [Authentication](authentication.md).

## Which source wins

For one setting, the sources rank like this, from the highest to the lowest:

1. A keyword on one call, such as `gql.query("user", id="u1", max_depth=1)`.
2. A command line flag.
3. A fixture: `gql_config`, `gql_url` or `gql_seed`.
4. The environment variable.
5. The ini option.
6. The built-in default.

The plugin builds the settings in the opposite order. The `gql_config` fixture
starts from the built-in defaults, then applies the ini options, then the
environment variables. If you override `gql_config`, your value replaces that
result. The command line flags are applied after that, so a flag also wins over
an overridden `gql_config`.

Each setting has one winner. A list is not merged across sources. If the
environment sets `gql_exclude`, its list replaces the ini list as a whole.
`gql_redact_headers` is the one exception to the default: a list adds names to
the four default names, and no spelling of the option removes one.

A project hook named `pytest_graphql_configure` runs after the flags are
applied. It sees the final values and may change them. See
[Extending](extending.md).

### One call

These settings can be given on a single call, and the call keyword has the same
name as the option: `max_depth`, `cycle_policy`, `per_type_depth_cap`,
`include_deprecated`, `max_fields`, `validate` and `timeout`. A per-call
`timeout` can only shorten the configured limit. `gql_retries` is set in
configuration and not on a call.

```python {.exec}
default = gql.query("user", id="u1", raw=True)
shallow = gql.query("user", id="u1", max_depth=1, raw=True)

assert "team {" in default.request.document
assert "team {" not in shallow.request.document
```

### A fixture

Use the `gql_config` fixture to change several settings in code. Ask for the
original fixture, and replace only the fields you change. The ini options and the
environment variables still apply to the fields you leave alone.

```python {.exec}
import dataclasses

import pytest


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(gql_config, max_depth=1)


def test_the_client_uses_the_replaced_value(gql):
    assert gql.config.max_depth == 1
    document = gql.query("user", id="u1", raw=True).request.document
    assert "team {" not in document
```

The `gql_config` fixture is also the way to set a field that has no ini option,
such as `proxy`, `redact_variables` or `raise_on_error`. Every field is listed
on [`ClientConfig`](api.md#pytest_graphql.ClientConfig).

`gql_url` and `gql_seed` follow the same shape. By default each one is the
effective value. Override it to give your tests another one. A flag still wins
over the override.

## When a value is wrong

pytest checks every source when it starts, even a source that a higher source
hides. A wrong value stops the run before any test starts. The message names
the setting and the source. It never shows the value, because a header or a URL
can hold a secret.

```text
ERROR: pytest-graphql: invalid value for gql_max_depth, set by the ini option gql_max_depth. Expected a whole number of at least 1. The value is not shown.
```

With no URL in any source, a test that uses `gql` fails at setup. The message
names the four places to set one:

```text
pytest-graphql: no GraphQL endpoint. Pass --gql-url=URL, set the PYTEST_GQL_URL environment variable or the gql_url ini option, or override the gql_url fixture.
```

## TLS and relative paths

`gql_verify` accepts the path of a CA bundle file. A relative path in the ini
file is read from the directory of that file. A relative path in the environment
is read from the directory where pytest was started.

## A random seed

`--gql-seed=random` chooses one seed when pytest starts and uses it for the
whole run. With pytest-xdist, the controller chooses it once and every worker
uses the same number. The report header shows which seed was used, so you can
repeat a run. See [Factory](factory.md).
