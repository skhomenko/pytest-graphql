# Security policy

## Supported versions

pytest-graphql is in the `0.x` series. Security fixes go into the newest
release only. A fix is released as a new version, and the changelog says
which versions are affected.

| Version | Supported |
|---|---|
| Newest `0.1.x` release, including release candidates | Yes |
| Older releases | No |

## Reporting a vulnerability

Report a vulnerability privately through GitHub:
https://github.com/skhomenko/pytest-graphql/security/advisories/new

Do not open a public issue, pull request or discussion for it.

Include what you can of the following:

- the affected version, and the Python, pytest, graphql-core and httpx
  versions;
- what an attacker can do, and under which conditions;
- the steps or a minimal example that shows the problem.

Do not include real credentials, tokens or personal data. Use made-up values
that show the same behavior.

## What to expect

- An acknowledgement within 7 days.
- A first assessment within 14 days: whether the report is accepted, and the
  planned next step.
- For an accepted report, a fixed release and a published GitHub security
  advisory. You are credited in the advisory unless you ask not to be.

This is a project maintained by one person, so these are goals, not
guarantees.

## Scope

In scope: the `pytest-graphql` package as published on PyPI, including its
pytest plugin, and this repository's release and documentation workflows.

Examples of security problems in this package:

- a credential, token, cookie or other secret that reaches a failure report,
  a log line, an exception message or a `curl` line without redaction;
- a request sent to a host the user did not configure;
- a release artifact that differs from what the release workflow built.

Out of scope: vulnerabilities in the GraphQL servers that you test with this
package, and in third-party dependencies (report those to their maintainers;
if pytest-graphql must change because of one, a report here is welcome).
