# Responses

After this page you can read any value in a response. You can also say what a
call returns, read the whole response when you need the errors or the status, and
tell why a missing field raises an error.

## What a call returns

`query()` and `mutation()` return the value of the one field you asked for:

| The field returns | You get |
|---|---|
| An object, an interface or a union | a `Node` |
| A list of those | a `NodeList` |
| A scalar, an enum, or a list of them | the plain Python value |
| `null` | `None` |

A custom scalar is never wrapped. It is the raw JSON value, unless you registered
a `parse` function for it. See [Factory](factory.md).

```python {.exec}
user = gql.query("user", id="u1", fields=["name", "joinedAt"])
users = gql.query("users", fields=["name"])

assert type(user).__name__ == "Node"
assert type(users).__name__ == "NodeList"
assert gql.query("pingScalar") is True
assert gql.query("user", id="nobody", fields=["name"]) is None
assert user.joined_at == "2020-01-01T00:00:00+00:00"
```

## Read a field

A [`Node`](api.md#pytest_graphql.Node) is a read-only mapping. Read a field as an
attribute or as a key.

```python {.exec}
user = gql.query("user", id="u1")

assert user.name == "Ada Lovelace"
assert user["name"] == "Ada Lovelace"
assert user.team.name == "Core"
assert "name" in user
```

A field that holds an object is a `Node`. A field that holds a list of objects is
a `NodeList`.

### snake_case and exact names

Every field works in its exact schema spelling and in snake_case. `user.joined_at`
reads the field `joinedAt`. This holds for attributes, for keys, for `in`, and for
the names you give to `fields=` and to `where()`.

```python {.exec}
user = gql.query("user", id="u1", fields=["joinedAt"])

assert user.joined_at == user.joinedAt == user["joined_at"] == user["joinedAt"]
assert "joined_at" in user and "joinedAt" in user
```

The server's own names are never lost. Iteration, `len()`, `keys()` and
`to_dict()` use the keys exactly as the server sent them.

```python {.exec}
user = gql.query("user", id="u1", fields=["id", "joinedAt"])

assert list(user) == ["id", "joinedAt"]
assert len(user) == 2
assert user.to_dict() == {"id": "u1", "joinedAt": "2020-01-01T00:00:00+00:00"}
```

A name can be written two ways when a schema has both, such as `userId` and
`user_id`. The client never guesses between them:

- The exact name wins, and a warning says so. `user.user_id` reads the field
  `user_id`, and `user["userId"]` reads the other one.
- When two different names turn into one snake_case name and neither is the exact
  spelling, that name is ambiguous. Reading it raises an error that names both.
  The exact names still work.

```python {.exec}
import pytest

with pytest.warns(UserWarning, match="the exact name wins"):
    user = gql.query("user", id="u1", fields=["userId", "user_id"])
    assert user.user_id == "u1-snake"
assert user["userId"] == "u1"
```

### Names that Python already uses

A field can be named like a method of a mapping (`keys`, `items`, `values`) or
like a Python keyword (`from`, `class`). An attribute then reads the method, or
cannot be written. Use a key for these fields.

```python {.exec}
fields = ["id", "from", "class", "keys", "items", "to_dict"]
record = gql.query("reservedWordFields", fields=fields)

assert record["from"] == "origin"
assert record["class"] == "vip"
assert record["keys"] == ["a", "b"]
assert record["items"] == ["x", "y"]
assert record["to_dict"] == "ok"
assert callable(record.keys)
```

### Read a nested value

`at()` reads a dotted path. A segment is a field name or a list index. Write an
index as a number or in brackets. A missing segment raises, unless you give a
`default`.

```python {.exec}
user = gql.query("user", id="u2")

assert user.at("manager.name") == "Ada Lovelace"
assert user.at("friends[0].name") == "Ada Lovelace"
assert user.at("friends.0.name") == "Ada Lovelace"
assert user.at("manager.nickname", default=None) is None
```

`get()` reads one field and returns a default when the field is not in the
response.

```python {.exec}
user = gql.query("user", id="u1", fields=["id"])

assert user.get("id") == "u1"
assert user.get("name") is None
assert user.get("name", "unknown") == "unknown"
```

### Compare a node

`node == other_node` and `node == a_dict` compare all the data exactly. The two
sides must hold the same fields. For a partial comparison, use a matcher. See
[Assertions](assertions.md).

```python {.exec}
user = gql.query("user", id="u1", fields=["id", "name"])

assert user == {"id": "u1", "name": "Ada Lovelace"}
assert user != {"name": "Ada Lovelace"}
assert user == gql.expect.User(name="Ada Lovelace")
```

A `Node` is not hashable, so it cannot be a dictionary key or a set member.

`repr()` of a node shows the type and the field names and never a value. A
secret in the data cannot reach a test report through it.

```python {.exec}
user = gql.query("user", id="u1", fields=["id", "name"])

assert repr(user) == "User(id, name)"
assert "Ada" not in repr(user)
```

## Why access is strict

A field that is not in the response raises `GraphQLFieldError`. It does not give
`None`.

```python {.exec}
import pytest

user = gql.query("user", id="u1", fields=["id", "name"])

with pytest.raises(AttributeError) as error:
    user.nickname

assert str(error.value) == (
    "no field 'nickname' on User.\n"
    "  Available in this response: id, name\n"
    "  Did you mean 'name'?\n"
    "  Note: the field may exist on the type but not be in this selection set."
)
```

There are two reasons.

- A missing field and a `null` field are different facts. If a missing field read
  as `None`, `assert user.nickname is None` would pass for a field the call never
  asked for, and a typo would pass as well. Strict access fails on the line
  that is wrong.
- When the selection is the cause, the message says so. The field may exist in the
  schema and be absent from `fields=`, and the error lists what the response
  does hold.

A field that was selected and came back `null` reads as `None`. That is a normal
value.

```python {.exec}
user = gql.query("user", id="u1", fields=["name", "avatar"])

assert user.avatar is None
assert "avatar" in user
assert "nickname" not in user
```

The error is also an `AttributeError` and a `KeyError`. So `getattr(user, "x",
default)`, `hasattr(user, "x")` and `user.get("x")` work as usual, when you want a
test to accept a missing field.

## Lists

A [`NodeList`](api.md#pytest_graphql.NodeList) is a normal `list`. Indexing,
slicing, `len()` and iteration work. It adds helpers to filter the elements and to
read a field from each one.

| Helper | What it does |
|---|---|
| `where(**filters)` | The elements that match every filter, as a new `NodeList`. |
| `one(**filters)` | The one element that matches. It raises for none or for several. |
| `pluck(path)` | The value at `path` of every element, as a list. |
| `ids()` | `pluck("id")`. |
| `at(path)` | One value, with a path that starts with an index. |

```python {.exec}
from pytest_graphql import matches

users = gql.query("users", fields=["id", "name", {"team": ["name"]}])

assert users.ids() == ["u1", "u2", "u3"]
assert users.pluck("team.name") == ["Core", "Core", "Platform"]
assert users.one(id="u2").name == "Grace Hopper"
assert users.where(name=matches("^A")).ids() == ["u1", "u3"]
assert len(users.where(team={"name": "Core"})) == 2
assert users.at("[1].name") == "Grace Hopper"
```

A filter value is a plain value or a matcher. The match is partial: the other
fields of an element are ignored. With `strict=True` the element must have no
other field. A filter on a nested object takes a plain `dict`, which is also a
partial match.

`one()` raises an error that gives the number it found and the names of the
filters. It prints no response value.

`pluck()` raises for an element that lacks the path. Give a `default` when some
elements lack it.

```python {.exec}
users = gql.query("users", fields=["name", {"manager": ["name"]}])

assert users.pluck("manager.name", default=None) == [None, "Ada Lovelace", "Ada Lovelace"]
```

A list of scalars is a plain `list`, and a list of lists keeps its nesting.

```python {.exec}
assert gql.query("matrix") == [["a", "b"], ["c"]]
assert gql.query("nonNullListOfNullables") == ["x", None, "y"]
```

## The whole response

Pass `raw=True` to get the [`GraphQLResponse`](api.md#pytest_graphql.GraphQLResponse)
and not the unwrapped value. `execute()` always returns it, because a document can
select any number of root fields.

```python {.exec}
response = gql.query("user", id="u1", fields=["name"], raw=True)

assert response.data.user.name == "Ada Lovelace"
assert response.has_data
assert response.errors == ()
assert response.http.status_code == 200
assert response.raw == {"data": {"user": {"name": "Ada Lovelace"}}}
assert "errors" not in response.raw
assert response.at("user.name") == "Ada Lovelace"
```

| Field | What it holds |
|---|---|
| `data` | A `Node` with the root fields you selected, or `None` when the server sent no data. |
| `data_state` | `"present"` or `"null"`. |
| `has_data` | `True` when `data` is present. |
| `errors` | The server's errors, as a tuple, in the server's order. |
| `extensions` | The top-level `extensions` of the response, or an empty mapping. |
| `http` | The HTTP exchange: `status_code`, `media_type`, `url` and `headers`. |
| `request` | The request that was sent, in its redacted form. |
| `raw` | A mapping rebuilt from the parsed response. It holds `data`, and holds `errors` and `extensions` only when they are not empty. It cannot tell you whether the server sent an empty `errors` or `extensions`. It is not the bytes of the reply. |
| `duration_ms` | How long the transport call took, in milliseconds. |

`unwrap()` returns the value of the one root field that the document selected. It
is what `query()` does for you. It raises when the document selects two root
fields or none.

```python {.exec}
import pytest

response = gql.execute('{ user(id: "u1") { name } team(id: "t1") { name } }')

assert response.data.user.name == "Ada Lovelace"
assert response.data.team.name == "Core"
with pytest.raises(Exception, match="needs exactly one top-level field"):
    response.unwrap()
```

### Errors next to data

By default, a response with errors raises, and you never hold it. To read the
errors yourself, turn off raising for the call and ask for the whole response. An
error entry has a `message`, a `path`, `locations`, `extensions` and a `code`,
which is `extensions["code"]` when that is a string.

```python {.exec}
failed = gql.mutation(
    "updateUser", id="missing", fields=["id"], raise_on_error=False, raw=True
)

assert failed.data_state == "null"
assert failed.data is None
assert failed.has_data is False
error = failed.errors[0]
assert error.path == ("updateUser",)
assert error.locations == ((2, 3),)
assert error.code is None
```

The three states of `data` are absent, `null` and present. A server that sends no
`data` at all is a rejected request, and that raises before a response exists.
You see `"present"` and `"null"`. See [Errors](errors.md) for the rules on when a
response raises.

The `repr()` of a response and of an error entry shows the status, the data state,
the counts and the redacted request. It never shows a data value or an error
message.

```python {.exec}
response = gql.query("user", id="u1", fields=["name"], raw=True)

assert repr(response).startswith("GraphQLResponse(status=200, data=present, errors=0")
assert "Ada Lovelace" not in repr(response)
```

## Shape checks

Before you read anything, the client checks the whole `data` value against the
types of the schema. A value that contradicts its type fails the call with an
error that names the path and the schema type. The error never repeats a value
from the server. Examples are an object where the schema has a list, a string
where it has an `Int`, and a `__typename` that is not a possible type.

A key that the selection did not ask for is kept and is read as raw JSON.
