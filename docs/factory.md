# Factory

After this page you can build valid input for a mutation without writing the
values. You can also make a test repeat the same data on every run, make a value
differ on every run when it must, and teach the client about your custom scalars.

## Make a payload

`gql.fake.TypeName()` builds a payload for an input type of the schema. The
payload is a plain `dict` with the exact schema field names, in schema order. Pass
it as `input=payload`, or spread it as keyword arguments.

```python {.exec}
payload = gql.fake.CreatePostInput(author_id="u1")

assert list(payload) == ["title", "authorId"]
assert payload["authorId"] == "u1"

post = gql.mutation("createPost", input=payload, fields=["title", {"author": ["name"]}])
assert post.title == payload["title"]
assert post.author.name == "Ada Lovelace"

same = gql.mutation("createPost", **payload, fields=["title"])
assert same.title == payload["title"]
```

Give a field a value to override it. The key can be the exact name or the
snake_case name, and the payload always uses the exact name. An override replaces
the whole value of the field, a nested object too. A key that is not a field of
the type fails, and so does giving one field in two spellings.

```python {.exec}
import pytest

from pytest_graphql import SelectionError

payload = gql.fake.CreatePostInput(title="Hello", author_id="u1")
assert payload == {"title": "Hello", "authorId": "u1"}

with pytest.raises(SelectionError, match="no field 'nme' on CreatePostInput"):
    gql.fake.CreatePostInput(nme="x")

with pytest.raises(SelectionError, match="was given twice"):
    gql.fake.CreatePostInput(authorId="u1", author_id="u2")
```

`gql.fake` builds input objects only. It does not build an output type. A name
that is not an input type fails with a message that says so.

### What the values are

A required field is always filled. An optional field is filled too, unless you
pass `_required_only=True`. A filled optional field holds a value, never `null`.

- `String` is 8 lowercase letters and digits.
- `ID` is 12 lowercase hexadecimal characters.
- `Int` is a whole number from 1 to 1000.
- `Float` is a number from 0 to 999.99 with two decimal places.
- `Boolean` is true or false.
- An enum is one of its members.
- A list has one to three elements.
- A custom scalar is the value of the `fake` function of its spec. See
  [Custom scalars](#custom-scalars).

An input object inside an input object is filled in full to a depth of `_depth`
(2 by default). Deeper objects get their required fields only. This stops a
recursive type and still builds a required nested object.

```python {.exec}
payload = gql.fake.CreatePostInput(_required_only=True)

assert set(payload) == {"title", "authorId"}
assert len(payload["authorId"]) == 12
```

## Make a test repeat

A test that uses random data and fails once can be hard to reproduce. The factory
has no randomness of its own. The same inputs give the same data on every run.

The data depends on three inputs:

- the seed, which is `gql_seed` and by default `0`,
- the node id of the test, which is the path of the test file and the name of the
  test,
- the path of the field in the payload, which starts with the type name.

So a test gets the same payload on every run, on every machine, and on every
supported Python version. Two tests get different payloads, because their node
ids differ. A field keeps its value when you add, skip or override another field
of the payload, because each field draws from its own stream.

```python {.exec}
payloads = {}


def test_a_payload_repeats_inside_a_test(gql):
    payload = gql.fake.CreatePostInput()
    assert payload == gql.fake.CreatePostInput()
    payloads["first"] = payload


def test_another_test_gets_other_data(gql):
    assert gql.fake.CreatePostInput() != payloads["first"]


def test_a_field_keeps_its_value_when_another_is_overridden(gql):
    plain = gql.fake.CreatePostInput()
    assert gql.fake.CreatePostInput(author_id="u1")["title"] == plain["title"]
```

The promise holds for one version of the package. A change to the generated
values is a change of the package, and the changelog records it.

### Choose the seed

Set the seed in the places listed on [Configuration](configuration.md): the
`gql_seed` ini option, the `PYTEST_GQL_SEED` variable, the `--gql-seed` flag.
Another seed gives other data for every test.

`--gql-seed=random` chooses one seed when pytest starts. The report header says
which one, as in `seed 123456789 (chosen by random)`. To repeat that run, pass
the number: `--gql-seed=123456789`. With pytest-xdist, every worker uses the
same seed.

To give one test, one module or one class its own seed, override the `gql_seed`
fixture. It is the seed before it is mixed with the node id. A flag still wins
over the fixture.

```python {.exec}
import pytest


@pytest.fixture
def gql_seed():
    return 7


def test_this_test_has_its_own_seed(gql):
    assert gql.config.seed == 7
```

## Make a value differ

Reproducible and unique are opposite goals, and you choose per field. A payload
that repeats on every run breaks a test that writes to a database with a unique
constraint. The second run writes what the first run wrote, and the database
refuses it.

Put [`unique()`](api.md#pytest_graphql._core.factory.unique.unique) on the field that
must differ. The other fields keep their seeded values.

```python {.exec}
from pytest_graphql import unique


def test_a_unique_value_differs_on_every_call(gql):
    first = gql.fake.CreatePostInput(title=unique())
    second = gql.fake.CreatePostInput(title=unique())

    assert first["title"] != second["title"]
    assert first["authorId"] == second["authorId"]


def test_a_unique_email_is_on_a_reserved_domain(gql):
    email = gql.fake.CreatePostInput(title=unique("email"))["title"]

    assert email.endswith("@example.com")
```

A unique value differs between calls, between runs, and between parallel
workers. It can sit at any depth of an override, inside a `dict` or a list. The
kinds are `None` or `"string"` for a short token, and `"email"` for an address on
`example.com`. That domain is reserved for examples, so a value never reaches a
real mailbox.

The tradeoff is real. A unique value cannot repeat, so a failure that depends on
it cannot be repeated with the seed alone. Print the value in the test, or keep
it unique only where a constraint needs it.

## Custom scalars

A custom scalar is a scalar type that your schema defines, such as `DateTime` or
`Money`. On the wire it is plain JSON. A
[`ScalarSpec`](api.md#pytest_graphql.ScalarSpec) tells the client what to do with
it:

| Field | Used for |
|---|---|
| `name` | The scalar's name in the schema. |
| `parse` | Turn the JSON of a response into a Python value. Leave it out to keep the raw JSON. |
| `serialize` | Turn a Python value into JSON when you send it as a variable. |
| `fake` | Make a value for `gql.fake`. It receives a `DeterministicRandom`. |

Without a spec, a custom scalar reads as its raw JSON value in a response, and the
value you send is sent as it is. The factory cannot make a value for it. It fails
with a message that names the scalar, the field, and the spec that registers it.
It also says which field to override instead.

Register the specs in the `gql_scalars` fixture. The registry is the same one that
decodes responses, serializes variables and feeds `gql.fake`.

```python {.exec}
import datetime

import pytest

from pytest_graphql import ScalarSpec


@pytest.fixture(scope="session")
def gql_scalars(gql_scalars):
    gql_scalars.register(
        ScalarSpec(
            name="DateTime",
            serialize=lambda value: value.isoformat(),
            fake=lambda rng: datetime.datetime(
                2020, 1, 1 + rng.below(28), tzinfo=datetime.timezone.utc
            ),
            parse=datetime.datetime.fromisoformat,
        )
    )
    return gql_scalars


def test_a_registered_scalar_is_decoded(gql):
    user = gql.query("user", id="u1", fields=["joinedAt"])

    assert user.joined_at == datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)


def test_an_unregistered_scalar_stays_raw_json(gql):
    user = gql.query("user", id="u1", fields=["balance"])

    assert user.balance == "100.00"
```

A few more rules:

- `serialize` runs on every variable of that type, at any depth. Its result must be
  JSON: `None`, a boolean, a whole number, a finite float, a string, a list, or a
  `dict` with string keys. Anything else fails before the request is sent.
- `gql.fake` does not call `serialize`, so you can read and edit a payload as
  Python values.
- The five scalars of GraphQL itself (`String`, `ID`, `Int`, `Float` and
  `Boolean`) are handled by the package. A spec cannot use their names.
- A second spec with the same name is an error, unless you pass `replace=True` to
  `register()`.
- A scalar you register later takes effect on the next call.

### A `fake` function

A `fake` function must use the `DeterministicRandom` it receives. Do not use the
`random` module there. The stream is stable across Python versions, and the
standard one is not. The same seed and path give the same values.

```python {.exec}
from pytest_graphql import DeterministicRandom

first = DeterministicRandom(1, "Pay", "amount")
second = DeterministicRandom(1, "Pay", "amount")
other = DeterministicRandom(1, "Pay", "currency")

assert [first.below(100) for _ in range(5)] == [second.below(100) for _ in range(5)]
assert first.sample_string(6) == second.sample_string(6)
assert other.below(2**32) != DeterministicRandom(1, "Pay", "amount").below(2**32)
```

`DeterministicRandom` has `bits`, `below`, `float_unit`, `choice` and
`sample_string`. See [its entry](api.md#pytest_graphql.DeterministicRandom).
