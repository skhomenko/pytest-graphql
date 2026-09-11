# pytest-graphql

Schema-aware GraphQL API testing for pytest.

## Status

Early development. The package is being built milestone by milestone, and this
release contains the packaging skeleton only. There is no client, no transport
and no fixture yet. Install it today only if you want to track the work.

What is planned for `0.1.0`, in the order it lands:

- A GraphQL client that reads your schema and builds selection sets for you.
- An `httpx` transport with explicit timeout, retry and limit settings.
- A response model with clear failure output, including a redacted request
  summary and a reproducible `curl` command.
- One pytest fixture, then the full plugin: ini options, CLI flags and hooks.
- Response matching, deterministic fake data, error assertions and polling.

The changelog records what each release actually contains:
https://github.com/skhomenko/pytest-graphql/blob/main/CHANGELOG.md

## Install

```
pip install pytest-graphql
```

The required dependencies are `graphql-core` and `httpx`. `pytest` is optional,
so the client can be used outside a test suite:

```
pip install "pytest-graphql[pytest]"
```

## Supported versions

Python 3.10 through 3.14, with pytest 7.4 or newer when the pytest extra is
installed. CI tests a representative sample of that range rather than the whole
cross product.

## License

MIT. See https://github.com/skhomenko/pytest-graphql/blob/main/LICENSE
