# Assertions

After this page you can check a response in three ways: with plain `assert`
statements, with matchers that check part of an object, and with helpers for
lists. You can also read the diff that a failed matcher prints.

## Plain asserts

A response is plain Python data, so a plain `assert` is often enough. Read the
field and compare it.

```python {.exec}
user = gql.query("user", id="u1")
users = gql.query("users", fields=["id", "name"])

assert user.name == "Ada Lovelace"
assert user.team.name == "Core"
assert users.ids() == ["u1", "u2", "u3"]
assert len(users) == 3
```

`node == a_dict` compares the whole object. The two sides must have exactly the
same fields. That is too strict for most tests, because a schema change adds a
field and every such test breaks. Use a matcher when you care about a few fields.

## Match part of an object

`gql.expect.Type(**fields)` builds a matcher for an object type. It checks only
the fields you name. The other fields of the response are ignored.

```python {.exec}
user = gql.query("user", id="u1")

assert user == gql.expect.User(name="Ada Lovelace")
assert user == gql.expect.User(id="u1", team={"name": "Core"})
assert user != gql.expect.User(name="Grace Hopper")
```

A matcher works on either side of `==`.

The type name and the field names are checked against the schema when you build
the matcher. A typo fails on that line, and not later when the data is compared.
A name works in its exact spelling and in snake_case.

```python {.exec}
import pytest

from pytest_graphql import SchemaError, SelectionError

with pytest.raises(SelectionError, match="no field 'frist_name' on User"):
    gql.expect.User(frist_name="Ada")

with pytest.raises(SchemaError, match="no type named 'Usr'"):
    gql.expect.Usr(name="Ada")

with pytest.raises(SchemaError, match="an enum, which has no fields to match"):
    gql.expect.PyKeyword
```

Some more rules:

- A value you give is compared with `==`. Give a helper from the next section to
  compare in another way.
- A plain `dict` is a partial match of a nested object. A plain `list` is
  compared by position and must have the same length.
- The matcher checks the type name of the response object. An interface or a union
  matcher accepts every type it can return. `__typename` is always a valid field
  to match. The check applies when the object carries a type name. A selection you
  write yourself has none unless you ask for `__typename`.
- A boolean never equals a number. `True` does not match `1`, although Python
  says `True == 1`.

```python {.exec}
from pytest_graphql import one_of

team = gql.query("team", id="t1", fields=["name", {"members": ["name"]}])

assert team == gql.expect.Team(members=[{"name": "Ada Lovelace"}, {"name": "Grace Hopper"}])
assert team != gql.expect.Team(members=[{"name": "Grace Hopper"}, {"name": "Ada Lovelace"}])
assert team != gql.expect.Team(members=[{"name": "Ada Lovelace"}])

assert gql.query("pingScalar") == one_of(True)
assert gql.query("pingScalar") != one_of(1)

node = gql.query("node", id="u1", fields=["id", "__typename"])
assert node == gql.expect.Node(id="u1")
assert node == gql.expect.User(id="u1")
assert node != gql.expect.Team(id="u1")
```

## Helpers for values

A helper goes in the place of a value. Each one is a function of
[`pytest_graphql`](api.md).

| Helper | Matches |
|---|---|
| [`any_value()`](api.md#pytest_graphql.any_value) | Any value, `null` included, when the field is in the response. |
| [`absent()`](api.md#pytest_graphql.absent) | A field that is not in the response. This is not the same as `null`. |
| [`matches(pattern)`](api.md#pytest_graphql.matches) | A string where the regular expression finds a match. It is a search, so write `^` and `$` when you need them. |
| [`gt(x)`, `gte(x)`, `lt(x)`, `lte(x)`](api.md#pytest_graphql.gt) | A number, a date or a string in that order. An ISO date string orders correctly. A value that cannot be ordered against the bound does not match and does not raise. |
| [`one_of(*values)`](api.md#pytest_graphql.one_of) | A value equal to one of the values. |
| [`any_length()`](api.md#pytest_graphql.any_length) | A list of any length, an empty one too. |
| [`length(n)`](api.md#pytest_graphql.length) | A list with exactly `n` elements. |

```python {.exec}
from pytest_graphql import absent, any_length, any_value, gt, length, matches, one_of

user = gql.query("user", id="u1", fields=["id", "name", "joinedAt", "avatar"])
assert user == gql.expect.User(
    id=matches(r"^u[0-9]+$"),
    name=one_of("Ada Lovelace", "Grace Hopper"),
    joined_at=gt("2019-12-31"),
    avatar=any_value(),
    team=absent(),
)

team = gql.query("team", id="t1")
assert team == gql.expect.Team(members=length(2), captain=any_value())
assert gql.query("users") == any_length()
```

## Match a list

Two matchers check a list. Each one pairs every item with a different element.

- [`contains(*items)`](api.md#pytest_graphql.contains): the list holds an element
  for each item, in any order. It may hold other elements too.
- [`unordered(*items)`](api.md#pytest_graphql.unordered): the list has the same
  elements as the items, in any order, and no others.

An item is a plain value, a `dict`, a matcher or a helper. The pairing is exact. An
item that could match several elements never takes the one element that another
item needs. Duplicate items need duplicate elements.

```python {.exec}
from pytest_graphql import contains, unordered

users = gql.query("users", fields=["id", "name"])

assert users == contains(gql.expect.User(name="Grace Hopper"), {"name": "Ada Lovelace"})
assert users != contains({"name": "Ada Lovelace"}, {"name": "Ada Lovelace"})
assert users.pluck("name") == unordered("Alan Turing", "Ada Lovelace", "Grace Hopper")
assert users.pluck("name") != unordered("Alan Turing", "Ada Lovelace")
```

To count or to look at some elements only, filter the list first. `where()`
returns a `NodeList`, so `len()` counts the matches. A filter value can be a
plain value or any helper.

```python {.exec}
from pytest_graphql import matches

users = gql.query("users", fields=["id", "name", {"team": ["name"]}])

assert len(users.where(team={"name": "Core"})) == 2
assert users.where(name=matches("^A")).ids() == ["u1", "u3"]
assert users.one(id="u3").team.name == "Platform"
```

`contains()` and `unordered()` compare every item with every element. If the
two sides together make more than one million pairs, the call raises an error
that tells you to narrow the list with `where()` first.

## Read the diff

When `actual == matcher` fails under pytest, the plugin prints a diff. It shows
every field that differs, so you fix all of them in one run. The first line of
the failed assertion shows the two sides by type and field names, with no values.

```python {.exec}
import pytest

from pytest_graphql import gt, matches


def test_the_diff_lists_every_field_that_differs(gql):
    user = gql.query("user", id="u1", fields=["id", "name", "joinedAt", {"team": ["name"]}])
    expected = gql.expect.User(
        id=matches("^x"),
        name="Grace Hopper",
        team={"name": "Core"},
        joined_at=gt("2019-12-31"),
    )

    with pytest.raises(AssertionError) as failure:
        assert user == expected

    assert str(failure.value).splitlines()[1:] == [
        "  User does not match (2 of 4 compared fields differ; 0 response fields ignored)",
        "    id     'u1'            !=  matches('^x')",
        "    name   'Ada Lovelace'  !=  'Grace Hopper'",
        "  matched: team.name, joined_at",
    ]
```

Read it from the top:

- **The title** says which type did not match. The count is of compared fields.
  A nested object counts for its own fields, and not for itself. The title also
  says how many fields of the response were ignored, which are the fields your
  matcher did not name.
- **A line with `!=`** is a field that differs. It shows the path, the actual value
  and the expected value. The path of a nested field is dotted, and a list index
  is in brackets. The columns line up.
- **`<missing>`** takes the place of the actual value when the response has no
  such field.
- **`matched:`** lists the paths that were equal, on one line.

A value that is an object or a list shows as a summary: `User(id, name)`,
`[2 items]` or `{3 fields}`. The diff never prints a whole object.

```python {.exec}
import pytest

from pytest_graphql import contains


def test_a_list_diff_names_the_item_that_found_no_element(gql):
    users = gql.query("users", fields=["id", "name"])

    with pytest.raises(AssertionError) as failure:
        assert users == contains(gql.expect.User(name="Nobody"), {"name": "Ada Lovelace"})

    assert str(failure.value).splitlines()[1:] == [
        "  contains(2 items) does not match (1 of 1 compared field differs; 0 response fields ignored)",
        "    value   [3 items]  !=  contains(2 items)",
        "      no element left for item 0: User(name='Nobody')",
    ]


def test_a_missing_field_reads_as_missing(gql):
    user = gql.query("user", id="u1", fields=["id"])

    with pytest.raises(AssertionError) as failure:
        assert user == gql.expect.User(name="Ada Lovelace")

    assert str(failure.value).splitlines()[2:] == [
        "    name   <missing>  !=  'Ada Lovelace'",
    ]
```

The diff is bounded, so a large mismatch stays readable. It lists the first 50
differences, cuts each value at 200 bytes, and lists up to 50 matched paths. The
counts in the title stay exact, and each cut says how many entries it left out.

Values in the diff go through the same redaction as the rest of a failure report.
A field whose name matches a pattern in `redact_variables`, such as `password`,
shows `[redacted]` on both sides. See [Diagnostics](diagnostics.md).

A matcher shows its type and its field names when you print it. For each
expected value it shows only the type of the value, never the value, because a
test can hold a credential in one.

```python {.exec}
from pytest_graphql import gt

matcher = gql.expect.User(name="Ada Lovelace", joined_at=gt("2019-12-31"))

assert repr(matcher) == "User(name=<str>, joined_at=gt(<str>))"
```

## When to use which

| You want to check | Use |
|---|---|
| One value | A plain `assert` on the field. |
| A few fields of an object | `gql.expect.Type(...)`, so a new field breaks nothing. |
| That a field holds a kind of value | A helper such as `matches()` or `gt()`. |
| That a list holds some elements | `contains()`. |
| That a list holds exactly these elements | `unordered()`, or a positional `list` in a matcher. |
| How many elements match | `len(users.where(...))`. |
| That a list has a given size, with a diff on failure | `length(3)` on the list. |
