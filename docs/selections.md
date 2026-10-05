# Selections

After this page you can choose which fields a call asks for. You can write that
choice once and reuse it. You can also tune the fields the client picks for you.

## Let the client choose

Without `fields=`, the client reads the schema and selects the fields for you.
You write no query text, and a new field in the schema reaches your tests with no
edit.

```python {.exec}
user = gql.query("user", id="u1")

assert user.name == "Ada Lovelace"
assert user.team.name == "Core"
```

The client leaves some fields out on purpose:

- Deprecated fields. Turn them on with `include_deprecated`.
- A field with a required argument that you did not give, such as
  `User.posts(first: Int!)`.
- Fields past the depth limit, and repeats of a type on its own path. See
  [Tune auto-selection](#tune-auto-selection).

```python {.exec}
user = gql.query("user", id="u1")

assert "posts" not in user
assert "oldName" not in user
```

The default is `AUTO`. You rarely write it. You can write it to mix your own
fields with chosen ones. See [Mix your fields with chosen ones](#mix-your-fields-with-chosen-ones).

## Choose fields yourself

`fields=` takes four forms. They give the same result. Pick the shortest one that
says what you mean.

| Form | Example | Use it when |
|---|---|---|
| Auto | `fields=AUTO` | You want the whole object. This is the default. |
| List and dict | `fields=["id", {"team": ["name"]}]` | You know the names and need no arguments. |
| `Field` objects | `Field("posts", args={"first": 5}, fields=["title"])` | A field needs arguments, an alias, or a type condition. |
| Raw string | `fields="id team { name }"` | You have a selection written in GraphQL already. |

### A list of names

A string is a field name. A dict gives a field its own sub-selection. A name
works in its exact schema spelling and in snake_case.

```python {.exec}
user = gql.query("user", id="u1", fields=["id", "joined_at", {"team": ["name"]}])

assert user.team.name == "Core"
assert user.to_dict() == {
    "id": "u1",
    "joinedAt": "2020-01-01T00:00:00+00:00",
    "team": {"name": "Core"},
}
```

The response holds only the fields you named. A field you did not name is absent,
and reading it raises an error. See [Responses](responses.md).

A field that returns an object needs a sub-selection. If you write `"team"` and no
sub-selection, the call fails before it is sent, and the message shows how to fix
it. A typo in a name fails the same way, and the message suggests the closest
name.

### A `Field` object

A [`Field`](api.md#pytest_graphql.Field) takes arguments, an alias and a type
condition. An argument value is sent as a variable. It is never written into the
query text.

```python {.exec}
from pytest_graphql import Field

response = gql.query(
    "user",
    id="u1",
    fields=[
        "name",
        Field("posts", args={"first": 5}, alias="recent", fields=["title"]),
    ],
    raw=True,
)

assert "recent: posts(first: $posts_first)" in response.request.document
assert response.request.variables["posts_first"] == 5
assert response.data.user.recent[0].title == "Hello World"
```

Use an alias to ask for the same field twice with different arguments. An alias
must be unique in its selection.

### A raw string

A string is the body of a selection set, without the outer braces. The client
parses it and checks it against the schema before it sends anything. The text
goes into the query as you wrote it, so keep values out of it. Put a value in a
`Field` argument instead, where it becomes a variable.

```python {.exec}
user = gql.query("user", id="u1", fields="id name team { name }")

assert user.to_dict() == {"id": "u1", "name": "Ada Lovelace", "team": {"name": "Core"}}
```

Directives are not supported in `fields=`. To send a document with directives,
use [`execute()`](api.md#pytest_graphql.GraphQLClient.execute).

### Mix your fields with chosen ones

`AUTO` can stand in for the sub-selection of one field. The client chooses
there, and you choose everywhere else.

```python {.exec}
from pytest_graphql import AUTO, Field

user = gql.query("user", id="u1", fields=["name", {"team": AUTO}])
assert user.team.name == "Core"
assert "captain" in user.team

same = gql.query("user", id="u1", fields=["name", Field("team", fields=AUTO)])
assert same == user
```

`AUTO` cannot be one item in a list. Write `fields=AUTO`, or give it to one field.

### Interfaces and unions

A field that returns an interface or a union can return several types. To select
fields of one type, use `Selection.of("TypeName", ...)`. The client checks the
type name against the schema and suggests the closest one if it is wrong.

```python {.exec}
from pytest_graphql import Selection

hits = gql.query(
    "search",
    term="a",
    fields=[
        Selection.of("User", "name"),
        Selection.of("Team", "name"),
        Selection.of("Attachment", "filename"),
    ],
)

assert hits[0].name == "Ada Lovelace"
assert hits[-1].filename == "diagram.png"
```

Each type has its own fields, so a hit that is a `User` has `name`, and a hit that is an
`Attachment` has `filename`. A hit has only the fields you chose for its type.

Use `on=` on a `Field` for the same effect on one field:

```python {.exec}
from pytest_graphql import Field

node = gql.query("node", id="u1", fields=["id", Field("name", on="User")])

assert node.name == "Ada Lovelace"
```

## Reuse a selection

A `Selection` is a named group of fields. Define it once, use it in many tests,
and edit one place when the schema changes. Its parts can be any of the forms
above.

```python {.exec}
from pytest_graphql import Field, Selection

USER_BRIEF = Selection("id", "name")
USER_WITH_TEAM = USER_BRIEF + Selection({"team": ["id", "name"]})
USER_WITH_POSTS = USER_BRIEF + Selection(
    Field("posts", args={"first": 5}, fields=["title"])
)


def test_a_brief_user(gql):
    user = gql.query("user", id="u1", fields=USER_BRIEF)
    assert user.to_dict() == {"id": "u1", "name": "Ada Lovelace"}


def test_a_user_with_a_team(gql):
    user = gql.query("user", id="u2", fields=USER_WITH_TEAM)
    assert user.team.name == "Core"


def test_a_selection_can_hold_a_field_object(gql):
    user = gql.query("user", id="u1", fields=USER_WITH_POSTS)
    assert user.posts.pluck("title") == ["Hello World"]


def test_the_same_selection_works_for_another_field(gql):
    team = gql.query("team", id="t1", fields=Selection("name", {"members": USER_BRIEF}))
    assert team.members.pluck("name") == ["Ada Lovelace", "Grace Hopper"]
```

A `Selection` never changes. These operators return a new one:

- `a + b` joins two selections. Two fields with the same response key must be the
  same request. If they differ in arguments or alias, the call fails.
- `a - "name"` removes a field by its response key. A dotted path such as
  `"team.id"` removes a nested field. Removing a field that is not there fails,
  so a renamed field cannot quietly stop being removed.

```python {.exec}
from pytest_graphql import Selection

USER_WITH_TEAM = Selection("id", "name", {"team": ["id", "name"]})
WITHOUT_IDS = USER_WITH_TEAM - "id" - "team.id"

user = gql.query("user", id="u1", fields=WITHOUT_IDS)

assert "id" not in user
assert "id" not in user.team
assert user.team.name == "Core"
```

Both mistakes fail when the query is built, and the message says what is wrong.

```python {.exec}
import pytest

from pytest_graphql import Field, Selection

few = Selection(Field("posts", args={"first": 1}, fields=["title"]))
many = Selection(Field("posts", args={"first": 2}, fields=["id"]))

with pytest.raises(Exception, match="share the response key 'posts' but differ"):
    gql.query("user", id="u1", fields=few + many)

with pytest.raises(Exception, match="cannot remove 'nope'"):
    gql.query("user", id="u1", fields=Selection("id", "name") - "nope")
```

An `AUTO` inside a `Selection` means the same wherever you use the selection,
because each `AUTO` starts its own scope. See [Depth](#depth).

## Tune auto-selection

These settings shape what the client chooses. All of them are fields of
[`ClientConfig`](api.md#pytest_graphql.ClientConfig), and each has an ini option
and an environment variable. See [Configuration](configuration.md).

| Setting | Default | What it does |
|---|---|---|
| `max_depth` | `3` | How many levels of objects the client selects. |
| `cycle_policy` | `shallow` | What to do when a type repeats on its own path. |
| `per_type_depth_cap` | none | A stricter depth limit for named types. |
| `include_deprecated` | `false` | Whether deprecated fields are selected. |
| `max_fields` | `2000` | The most fields one query may select. |
| `exclude` | none | Patterns for fields that are never selected. |
| `relay_aware` | `true` | Whether Relay connections are selected in the standard page shape. |

A call can change the first five for that call alone. `exclude` and
`relay_aware` are set in configuration.

### Depth

The object that the call returns is level 1. `max_depth=1` selects its scalar
fields and no nested object.

```python {.exec}
default = gql.query("user", id="u1")
flat = gql.query("user", id="u1", max_depth=1)

assert "team" in default
assert "team" not in flat
assert flat.name == "Ada Lovelace"
```

A cap for one type is stricter than `max_depth`. It counts the levels below that
type, wherever the type appears. `{"Team": 0}` selects a team's scalar fields and
none of its members.

```python {.exec}
user = gql.query("user", id="u2", per_type_depth_cap={"Team": 0})

assert user.team.name == "Core"
assert "captain" not in user.team
assert "members" not in user.team
```

Depth limits the chosen fields only. A field that you write is never cut. An
`AUTO` counts depth from its own position. In `{"team": AUTO}`, the team is
level 1.

### Cycles

A cycle is a type that contains itself, such as `User.manager`, which is a `User`
again. The client checks each path from the top. The same type in two sibling
branches is not a cycle. `cycle_policy` says what to do at a repeat:

- `"shallow"` selects the scalar fields and the `id` of the repeated type, and
  nothing nested. This is the default.
- `"stop"` leaves the field out.
- `"id_only"` selects only the `id`.

```python {.exec}
shallow = gql.query("user", id="u2", max_depth=2)
assert "joined_at" in shallow.manager

stopped = gql.query("user", id="u2", max_depth=2, cycle_policy="stop")
assert "manager" not in stopped

id_only = gql.query("user", id="u2", max_depth=2, cycle_policy="id_only")
assert "id" in id_only.manager
assert "joined_at" not in id_only.manager
```

### Fields the test user may not read

A schema can have fields that your test identity is not allowed to read. The
server may refuse a query that selects one. List such fields in `exclude`. A pattern is
`Type.field`, `*.field` or `Type.*`. Any other shape is refused.

```python {.exec}
import dataclasses

import pytest


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(gql_config, exclude=["User.balance", "*.preferences"])


def test_excluded_fields_are_not_asked_for(gql):
    user = gql.query("user", id="u1")
    assert "balance" not in user
    assert "preferences" not in user
    assert user.name == "Ada Lovelace"
```

### Deprecated fields

A deprecated field is the one the schema author marked as not to be used. The
client skips it. Ask for it with `include_deprecated=True`, or name it in
`fields=`.

```python {.exec}
user = gql.query("user", id="u1", include_deprecated=True)
assert user.old_name == "Ada L."

named = gql.query("user", id="u1", fields=["old_name"])
assert named.old_name == "Ada L."
```

### A size limit

`max_fields` guards the size of the query that the client builds. It is not a
cost control. If a generated selection is larger, the call fails before it is
sent. The message names the type and lists ways to make the selection smaller.

```python {.exec}
import pytest

with pytest.raises(Exception, match=r"auto-selection of 'WideType'.*limit 100"):
    gql.query("wide", max_fields=100)

assert len(gql.query("wide")) == 301
```

### Connections and large unions

With `relay_aware` on, a type whose name ends in `Connection` and that has
`edges` and `pageInfo` is selected in the standard page shape. The client sends
`first` as a variable, 10 by default. A connection field that takes no page size
is skipped. A connection inside a connection is not expanded.

An interface or union with many types is expanded for the first 10 types. The
others collapse to `__typename` and `id`.

```python {.exec}
user = gql.query("user", id="u2")

assert user.posts_connection.edges[0].node.title == "Second Post"
assert user.posts_connection.page_info.has_next_page is False
```

## See what was left out

Every field the client skips on its own is recorded. A failed test report lists
them, so you can tell a missing field from a skipped one. See
[Diagnostics](diagnostics.md).
