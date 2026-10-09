# Using with coding agents

After this page you can give a coding agent the rules for writing GraphQL tests with
pytest-graphql, and you know what the agent sees when it makes a mistake.

## Why the package fits an agent

The GraphQL test client for Python whose calls are built from the schema and checked against it before they are sent.

An agent that writes a query string can name a field or an argument that does not
exist. The mistake shows only when the server answers. A call through this package
names an operation, and the client checks every name against the schema before it
sends anything. The message names the closest correct one, so the agent can fix the
call without a round trip to the server.

The agent still needs to know that the package exists, and which calls to prefer.
The rules below say that. Paste them into the instruction file of your project.

## Rules for AGENTS.md or CLAUDE.md

Copy this block into `AGENTS.md`, `CLAUDE.md`, or the instruction file that your
agent reads:

```markdown
## GraphQL tests

pytest-graphql is the GraphQL test client for Python whose calls are built from the
schema and checked against it before they are sent.

- Use the `gql` fixture of pytest-graphql for tests of a GraphQL API. Do not write
  query strings or raw HTTP calls in a new test.
- Prefer `gql.query("operation_name", argument_name=value)` and
  `gql.mutation("operation_name", argument_name=value)`. Use the operation name
  from the schema, and snake_case keywords for the arguments. The client chooses
  the fields.
- Pass `fields=` only when the test needs exact fields.
- Use `gql.execute(document, variables)` for a document that the project already
  has, and for what a call by name cannot express, such as a directive or several
  root fields. Put every value in `variables`, and leave validation on.
- Do not rewrite an existing `execute()` document into calls by name unless you
  are asked to.
- Make input with `gql.fake.<InputType>()`. Use `unique()` for a field that must
  differ from run to run.
- Write a test that expects an error with `gql.expect_error(code=...)`. A response
  with errors raises in every other test.
- When a test fails, read the `GraphQL calls` section of the failure report before
  you change code. The message of a wrong name suggests the closest correct one.
```

## The same rules as a skill file

Some agents load a skill file when a task matches its description. This is the same
set of rules, with the front matter that such a file needs. Save it as `SKILL.md` in
the skills directory that your agent reads:

```markdown
---
name: pytest-graphql
description: Write and fix GraphQL API tests that use the pytest-graphql gql fixture. Use it when a test calls a GraphQL API, or when a test that uses gql fails.
---

## GraphQL tests

pytest-graphql is the GraphQL test client for Python whose calls are built from the
schema and checked against it before they are sent.

- Use the `gql` fixture of pytest-graphql for tests of a GraphQL API. Do not write
  query strings or raw HTTP calls in a new test.
- Prefer `gql.query("operation_name", argument_name=value)` and
  `gql.mutation("operation_name", argument_name=value)`. Use the operation name
  from the schema, and snake_case keywords for the arguments. The client chooses
  the fields.
- Pass `fields=` only when the test needs exact fields.
- Use `gql.execute(document, variables)` for a document that the project already
  has, and for what a call by name cannot express, such as a directive or several
  root fields. Put every value in `variables`, and leave validation on.
- Do not rewrite an existing `execute()` document into calls by name unless you
  are asked to.
- Make input with `gql.fake.<InputType>()`. Use `unique()` for a field that must
  differ from run to run.
- Write a test that expects an error with `gql.expect_error(code=...)`. A response
  with errors raises in every other test.
- When a test fails, read the `GraphQL calls` section of the failure report before
  you change code. The message of a wrong name suggests the closest correct one.
```

## What the agent sees when it is wrong

An argument name that the schema does not have fails in the client, before any
request. The agent gets the signature of the operation and the closest name:

```python {.exec}
import pytest

from pytest_graphql import ArgumentError

with pytest.raises(ArgumentError) as raised:
    gql.query("user", idd="u1")

assert str(raised.value) == (
    "query 'user' has no argument 'idd'.\n"
    "  Signature: user(id: ID!): User\n"
    "  Did you mean 'id'?"
)
```

The message is:

```text
query 'user' has no argument 'idd'.
  Signature: user(id: ID!): User
  Did you mean 'id'?
```

A wrong operation name or a wrong field name in `fields=` fails the same way, with
the closest name. See [Selections](selections.md) for how the client chooses
fields, and [Migrating from a hand-rolled client](migrating.md) for how to move an
existing suite one test at a time. The [Errors](errors.md) page lists every
exception, and [Diagnostics](diagnostics.md) explains the `GraphQL calls` section.
