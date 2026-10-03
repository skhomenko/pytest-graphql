"""The input factory rules of SPEC 3.7, one test per rule."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from graphql import build_schema
from graphql.utilities import coerce_input_value
from hypothesis import given, settings
from hypothesis import strategies as st

from pytest_graphql._core.errors import (
    ScalarNotRegisteredError,
    SchemaError,
    SelectionError,
)
from pytest_graphql._core.factory import (
    FakeNamespace,
    ScalarRegistry,
    ScalarSpec,
    UniqueSource,
    unique,
)
from pytest_graphql._core.operation import assemble_operation
from pytest_graphql._core.selection.builder import SelectionBuilder
from pytest_graphql._core.selection.policy import SelectionPolicy
from tests.factory.schemas import SCHEMA, namespace


def money_registry() -> ScalarRegistry:
    return ScalarRegistry(
        [
            ScalarSpec(
                name="Money",
                serialize=str,
                fake=lambda rng: Decimal(rng.below(10_000)) / 100,
            ),
            ScalarSpec(name="Stamp", serialize=str, fake=lambda rng: rng.bits(16)),
        ]
    )


def nesting(value: Any, key: str) -> int:
    """How many objects deep a chain of ``key`` goes, the root counted as one."""
    if not isinstance(value, dict):
        return 0
    child = value.get(key)
    return 1 + (nesting(child, key) if child is not None else 0)


# -- shape ------------------------------------------------------------------


def test_the_result_is_a_plain_dict_all_the_way_down() -> None:
    payload = namespace().CreateUserInput()
    assert type(payload) is dict
    assert type(payload["profile"]) is dict
    assert type(payload["profile"]["address"]) is dict
    assert type(payload["profile"]["scores"]) is list


def test_keys_are_the_exact_schema_names_in_schema_order() -> None:
    payload = namespace().CreateUserInput()
    assert list(payload) == [
        "name",
        "nickname",
        "age",
        "height",
        "active",
        "ref",
        "role",
        "secondaryRole",
        "profile",
    ]


def test_built_in_scalars_get_python_values_of_the_matching_type() -> None:
    payload = namespace().CreateUserInput()
    assert isinstance(payload["name"], str)
    assert type(payload["age"]) is int
    assert type(payload["height"]) is float
    assert type(payload["active"]) is bool
    assert isinstance(payload["ref"], str)


# -- required and nullable --------------------------------------------------


def test_non_null_fields_are_always_filled() -> None:
    for seed in range(20):
        payload = namespace(seed=seed).CreateUserInput(_required_only=True)
        for required in ("name", "age", "active", "ref", "role"):
            assert payload[required] is not None


def test_nullable_fields_are_filled_by_default() -> None:
    payload = namespace().CreateUserInput()
    for optional in ("nickname", "height", "secondaryRole", "profile"):
        assert payload[optional] is not None


def test_required_only_skips_every_nullable_field() -> None:
    payload = namespace().CreateUserInput(_required_only=True)
    assert set(payload) == {"name", "age", "active", "ref", "role"}


def test_required_only_applies_at_every_depth() -> None:
    payload = namespace().ProfileInput(_required_only=True)
    assert set(payload) == {"address", "scores"}
    assert set(payload["address"]) == {"street"}


def test_a_filled_nullable_list_of_nullable_items_has_no_nulls() -> None:
    payload = namespace().ProfileInput()
    assert payload["maybes"]
    assert all(isinstance(item, str) for item in payload["maybes"])


def test_required_values_do_not_change_when_nullable_fields_are_skipped() -> None:
    # Each field draws from its own stream, keyed by its path, so leaving
    # other fields out never shifts the values of the ones that stay.
    full = namespace(seed=5).CreateUserInput()
    lean = namespace(seed=5).CreateUserInput(_required_only=True)
    for name in lean:
        assert lean[name] == full[name], name


# -- enums ------------------------------------------------------------------


def test_an_enum_value_is_a_member_name() -> None:
    for seed in range(30):
        assert namespace(seed=seed).CreateUserInput()["role"] in {
            "ADMIN",
            "EDITOR",
            "VIEWER",
            "GUEST",
        }


def test_an_enum_is_not_always_its_first_declared_value() -> None:
    picks = {namespace(seed=seed).CreateUserInput()["role"] for seed in range(60)}
    assert len(picks) == 4
    assert picks != {"ADMIN"}


def test_the_enum_pick_does_not_depend_on_declaration_order() -> None:
    reordered = build_schema(
        """
        enum Role { GUEST VIEWER EDITOR ADMIN }
        input RoleInput { role: Role! }
        type Query { a: String }
        """
    )
    original = build_schema(
        """
        enum Role { ADMIN EDITOR VIEWER GUEST }
        input RoleInput { role: Role! }
        type Query { a: String }
        """
    )
    for seed in range(25):
        assert (
            namespace(seed=seed, schema=reordered).RoleInput()
            == namespace(seed=seed, schema=original).RoleInput()
        )


# -- nesting ----------------------------------------------------------------


def test_nested_objects_expand_to_the_default_depth_of_two() -> None:
    # Level 0 is the root. Levels 0 to 2 are filled in full and every level
    # past the cap is filled with required fields only.
    profile = namespace().CreateUserInput()["profile"]
    assert profile["previous"] is not None  # level 2, still in full
    assert set(profile["address"]) == {"street", "city", "zip", "country"}


def test_a_recursive_type_is_cut_at_the_cap() -> None:
    for depth in range(5):
        payload = namespace().TreeInput(_depth=depth)
        assert nesting(payload, "parent") == depth + 2


def test_the_default_depth_is_two() -> None:
    assert nesting(namespace().TreeInput(), "parent") == 4
    assert nesting(namespace().TreeInput(_depth=2), "parent") == 4


def test_objects_past_the_cap_hold_required_fields_only() -> None:
    payload = namespace().TreeInput(_depth=1)
    last = payload["parent"]["parent"]
    assert set(last) == {"label"}


def test_objects_inside_the_cap_are_filled_in_full() -> None:
    payload = namespace().TreeInput(_depth=2)
    assert set(payload["parent"]["parent"]) == {"label", "parent", "children", "tags"}


def test_a_required_object_past_the_cap_is_still_built() -> None:
    # `ProfileInput.address` is non-null, so with a cap of zero it cannot be
    # left out. It is built with its required fields only.
    payload = namespace().ProfileInput(_depth=0)
    assert set(payload["address"]) == {"street"}


def test_depth_zero_builds_only_one_level_in_full() -> None:
    payload = namespace().CreateUserInput(_depth=0)
    assert set(payload["profile"]) == {"address", "scores"}


@pytest.mark.parametrize("depth", [-1, True, 1.5, "2", None])
def test_depth_must_be_a_non_negative_int(depth: object) -> None:
    with pytest.raises((TypeError, ValueError), match="_depth"):
        namespace().TreeInput(_depth=depth)


@pytest.mark.parametrize("flag", [1, "yes", None])
def test_required_only_must_be_a_bool(flag: object) -> None:
    with pytest.raises(TypeError, match="_required_only"):
        namespace().TreeInput(_required_only=flag)


# -- lists ------------------------------------------------------------------


def test_lists_get_one_to_three_elements() -> None:
    lengths = {len(namespace(seed=seed).ProfileInput()["scores"]) for seed in range(80)}
    assert lengths == {1, 2, 3}


def test_a_nested_list_has_one_to_three_elements_at_each_level() -> None:
    for seed in range(40):
        matrix = namespace(seed=seed).ProfileInput()["matrix"]
        assert 1 <= len(matrix) <= 3
        for row in matrix:
            assert 1 <= len(row) <= 3
            assert all(type(item) is int for item in row)


def test_a_list_of_input_objects_builds_each_element() -> None:
    children = namespace().TreeInput()["children"]
    assert 1 <= len(children) <= 3
    assert all("label" in child for child in children)


def test_list_elements_differ_from_each_other() -> None:
    for seed in range(20):
        scores = namespace(seed=seed).ProfileInput()["scores"]
        # Three draws from 1000 values collide about once in 170,000 times.
        if len(scores) == 3:
            assert len(set(scores)) == 3
            return
    pytest.fail("no three element list in 20 seeds")


# -- custom scalars ---------------------------------------------------------


def test_an_unregistered_required_scalar_raises_with_its_location() -> None:
    with pytest.raises(ScalarNotRegisteredError) as caught:
        namespace().PricedInput()
    assert caught.value.scalar_name == "Money"
    assert caught.value.location == "PricedInput.price"


def test_an_unregistered_scalar_inside_a_list_names_the_whole_path() -> None:
    registry = ScalarRegistry()
    with pytest.raises(ScalarNotRegisteredError) as caught:
        namespace(scalars=registry).StampedInput(_required_only=True)
    assert caught.value.location == "StampedInput.at"


def test_a_registered_scalar_uses_its_fake_and_keeps_the_python_value() -> None:
    payload = namespace(scalars=money_registry()).PricedInput()
    assert isinstance(payload["price"], Decimal)
    assert Decimal("0") <= payload["price"] < Decimal("100")


def test_a_registered_scalar_works_inside_a_list() -> None:
    payload = namespace(scalars=money_registry()).StampedInput()
    assert type(payload["at"]) is int
    assert all(type(item) is int for item in payload["items"])


def test_required_only_never_asks_for_a_nullable_unregistered_scalar() -> None:
    payload = namespace().OptionalPriceInput(_required_only=True)
    assert set(payload) == {"name"}


def test_a_nullable_unregistered_scalar_raises_when_it_would_be_filled() -> None:
    with pytest.raises(ScalarNotRegisteredError) as caught:
        namespace().OptionalPriceInput()
    assert caught.value.location == "OptionalPriceInput.price"


def test_an_override_skips_the_scalar_lookup() -> None:
    payload = namespace().PricedInput(price="9.99")
    assert payload["price"] == "9.99"


def test_the_error_location_shows_a_list_index() -> None:
    schema = build_schema(
        """
        scalar Money
        input LineInput { amount: Money! }
        input OrderInput { lines: [LineInput!]! }
        type Query { a: String }
        """
    )
    with pytest.raises(ScalarNotRegisteredError) as caught:
        namespace(schema=schema).OrderInput()
    assert caught.value.location == "OrderInput.lines[0].amount"


# -- overrides --------------------------------------------------------------


def test_an_override_wins() -> None:
    payload = namespace().CreateUserInput(name="fixed")
    assert payload["name"] == "fixed"


def test_an_override_can_use_the_snake_spelling() -> None:
    payload = namespace().CreateUserInput(secondary_role="GUEST")
    assert payload["secondaryRole"] == "GUEST"
    assert "secondary_role" not in payload


def test_an_override_can_be_none() -> None:
    payload = namespace().CreateUserInput(nickname=None)
    assert payload["nickname"] is None


def test_an_override_can_supply_a_field_that_required_only_would_skip() -> None:
    payload = namespace().CreateUserInput(_required_only=True, nickname="n")
    assert payload["nickname"] == "n"


def test_an_override_replaces_a_nested_object_whole() -> None:
    payload = namespace().CreateUserInput(profile={"bio": "x"})
    assert payload["profile"] == {"bio": "x"}


def test_an_override_value_is_not_copied_or_altered() -> None:
    value = {"street": "s"}
    payload = namespace().AddressInput(street=value)
    assert payload["street"] is value


def test_overrides_do_not_change_the_other_fields() -> None:
    plain = namespace(seed=3).CreateUserInput()
    changed = namespace(seed=3).CreateUserInput(name="fixed")
    assert {k: v for k, v in changed.items() if k != "name"} == {
        k: v for k, v in plain.items() if k != "name"
    }


def test_an_unknown_override_raises_with_the_available_fields() -> None:
    with pytest.raises(SelectionError) as caught:
        namespace().CreateUserInput(nmae="x")
    message = str(caught.value)
    assert "no field 'nmae' on CreateUserInput" in message
    assert "Available fields:" in message
    assert "Did you mean 'name'?" in message


def test_the_exact_and_snake_spelling_of_one_field_together_are_refused() -> None:
    with pytest.raises(SelectionError, match=r"secondaryRole.*twice"):
        namespace().CreateUserInput(secondaryRole="GUEST", secondary_role="ADMIN")


def test_an_ambiguous_snake_spelling_is_refused_and_names_every_spelling() -> None:
    with pytest.raises(SelectionError, match="ambiguous") as caught:
        namespace().TripleCollisionInput(user_id="x")
    for spelling in ("userId", "userID", "user_Id"):
        assert spelling in str(caught.value)


def test_an_exact_name_resolves_even_when_its_snake_form_collides() -> None:
    with pytest.warns(UserWarning, match="exact name wins"):
        payload = namespace().CollisionInput(user_id="a", userId="b")
    assert payload["user_id"] == "a"
    assert payload["userId"] == "b"


# -- determinism ------------------------------------------------------------


def test_the_same_inputs_give_the_same_payload() -> None:
    assert namespace(seed=4).CreateUserInput() == namespace(seed=4).CreateUserInput()


def test_calling_twice_in_one_test_gives_equal_payloads() -> None:
    # The value is a function of seed, node id and field path and nothing
    # else, so a second call repeats the first. `unique()` is the tool for a
    # value that must differ.
    fake = namespace(seed=4)
    assert fake.CreateUserInput() == fake.CreateUserInput()


def test_a_different_node_id_gives_a_different_payload() -> None:
    assert namespace(node_id="a::t1").CreateUserInput() != (
        namespace(node_id="a::t2").CreateUserInput()
    )


def test_a_different_global_seed_gives_a_different_payload() -> None:
    assert namespace(seed=1).CreateUserInput() != namespace(seed=2).CreateUserInput()


def test_different_input_types_with_the_same_field_name_differ() -> None:
    schema = build_schema(
        """
        input A { name: String! }
        input B { name: String! }
        type Query { a: String }
        """
    )
    fake = namespace(schema=schema)
    assert fake.A()["name"] != fake.B()["name"]


def test_a_nested_object_does_not_repeat_its_siblings_values() -> None:
    profile = namespace().CreateUserInput()["profile"]
    assert profile["address"] != profile["previous"]


# -- unique -----------------------------------------------------------------


def test_unique_in_an_override_is_resolved_to_a_string() -> None:
    payload = namespace().CreateUserInput(name=unique(), nickname=unique("email"))
    assert isinstance(payload["name"], str) and payload["name"].startswith("u")
    assert payload["nickname"].endswith("@example.com")


def test_unique_differs_on_every_call() -> None:
    fake = namespace()
    names = [fake.CreateUserInput(name=unique())["name"] for _ in range(50)]
    assert len(set(names)) == 50


def test_unique_differs_across_worker_ids() -> None:
    names = {
        namespace(worker_id=worker).CreateUserInput(name=unique())["name"]
        for worker in ("main", "gw0", "gw1")
    }
    assert len(names) == 3


def test_unique_inside_a_nested_override_is_resolved() -> None:
    payload = namespace().CreateUserInput(profile={"bio": unique(), "tags": [unique()]})
    assert isinstance(payload["profile"]["bio"], str)
    assert isinstance(payload["profile"]["tags"][0], str)


def test_the_rest_of_the_payload_does_not_change_when_unique_is_used() -> None:
    plain = namespace(seed=2).CreateUserInput()
    marked = namespace(seed=2).CreateUserInput(name=unique())
    assert {k: v for k, v in marked.items() if k != "name"} == {
        k: v for k, v in plain.items() if k != "name"
    }


# -- the namespace ----------------------------------------------------------


def test_an_unknown_type_raises_with_a_suggestion() -> None:
    with pytest.raises(SchemaError, match="Did you mean 'CreateUserInput'"):
        namespace().CreateUserInpt  # noqa: B018


@pytest.mark.parametrize("name", ["User", "Role", "String", "Money", "Query"])
def test_a_type_that_is_not_an_input_object_is_refused(name: str) -> None:
    with pytest.raises(SchemaError, match="input object"):
        getattr(namespace(), name)


def test_a_dunder_is_an_attribute_error() -> None:
    with pytest.raises(AttributeError):
        namespace().__wrapped__  # noqa: B018


def test_a_private_name_that_is_not_a_type_is_an_attribute_error() -> None:
    with pytest.raises(AttributeError):
        namespace()._ipython_canary_method_should_not_exist_  # noqa: B018


COLLIDING_SDL = """
input _cache { value: Int! }
input _base { value: Int! }
input _schema { value: Int! }
input _state { value: Int! }
input _names { value: Int! }
input _input_type { value: Int! }
input _is_input_name { value: Int! }
input FakeNamespace__state { value: Int! }
type Query { ok: Int }
"""


@pytest.mark.parametrize(
    "name",
    [
        "_cache",
        "_base",
        "_schema",
        "_state",
        "_names",
        "_input_type",
        "_is_input_name",
        "FakeNamespace__state",
    ],
)
def test_an_input_type_named_like_the_namespace_internals_is_still_reachable(
    name: str,
) -> None:
    fake = FakeNamespace(
        build_schema(COLLIDING_SDL),
        None,
        global_seed=0,
        node_id="n",
        unique_source=UniqueSource("run"),
    )
    factory = getattr(fake, name)
    assert repr(factory) == f"fake.{name}"
    assert set(factory()) == {"value"}
    assert name in dir(fake)
    assert getattr(fake, name) is factory


def test_dir_lists_the_input_object_types() -> None:
    names = dir(namespace())
    assert "CreateUserInput" in names
    assert "User" not in names
    assert names == sorted(names)


def test_the_repr_counts_the_input_types() -> None:
    assert repr(namespace()) == f"FakeNamespace({len(dir(namespace()))} input types)"


def test_a_type_factory_is_cached() -> None:
    fake = namespace()
    assert fake.CreateUserInput is fake.CreateUserInput


def test_the_type_factory_repr_names_the_type() -> None:
    assert repr(namespace().CreateUserInput) == "fake.CreateUserInput"


def test_the_namespace_is_a_real_class() -> None:
    assert isinstance(namespace(), FakeNamespace)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"global_seed": True}, "global_seed"),
        ({"node_id": 1}, "node_id"),
        ({"unique_source": None}, "unique_source"),
    ],
)
def test_the_namespace_checks_its_explicit_inputs(
    kwargs: dict[str, Any], match: str
) -> None:
    arguments: dict[str, Any] = {
        "global_seed": 0,
        "node_id": "n",
        "unique_source": UniqueSource("r"),
    }
    arguments.update(kwargs)
    with pytest.raises(TypeError, match=match):
        FakeNamespace(SCHEMA, None, **arguments)


# -- the payload is valid input ---------------------------------------------

INPUT_TYPES = [
    "AddressInput",
    "ProfileInput",
    "CreateUserInput",
    "TreeInput",
    "PricedInput",
    "OptionalPriceInput",
    "StampedInput",
    "CollisionInput",
    "EmptyRequiredInput",
]


@settings(max_examples=60)
@given(
    st.integers(-(2**31), 2**31),
    st.text(max_size=30),
    st.sampled_from(INPUT_TYPES),
    st.booleans(),
    st.integers(0, 4),
)
def test_every_payload_coerces_against_its_own_input_type(
    seed: int, node_id: str, type_name: str, required_only: bool, depth: int
) -> None:
    fake = namespace(seed=seed, node_id=node_id, scalars=_string_money())
    payload = getattr(fake, type_name)(_required_only=required_only, _depth=depth)
    # graphql-core accepts it with no error, so the payload is valid input.
    coerce_input_value(payload, SCHEMA.type_map[type_name])  # type: ignore[arg-type]


def _string_money() -> ScalarRegistry:
    return ScalarRegistry(
        [
            ScalarSpec(name="Money", serialize=str, fake=lambda _rng: "1.50"),
            ScalarSpec(name="Stamp", serialize=str, fake=lambda _rng: "t"),
        ]
    )


def test_the_payload_works_as_keywords_and_as_an_input_value() -> None:
    fake = namespace(seed=9)
    payload = fake.CreateUserInput()
    builder = SelectionBuilder(SCHEMA)
    policy = SelectionPolicy()

    flat = assemble_operation(
        schema=SCHEMA,
        kind="mutation",
        name="createUser",
        kwargs=dict(payload),
        policy=policy,
        builder=builder,
    )
    wrapped = assemble_operation(
        schema=SCHEMA,
        kind="mutation",
        name="createUser",
        kwargs={"input": payload},
        policy=policy,
        builder=builder,
    )

    assert flat.variables == wrapped.variables == {"input": payload}


def test_a_recursive_payload_assembles_into_an_operation() -> None:
    payload = namespace(seed=2).TreeInput()
    assembled = assemble_operation(
        schema=SCHEMA,
        kind="mutation",
        name="createTree",
        kwargs={"input": payload},
        policy=SelectionPolicy(),
        builder=SelectionBuilder(SCHEMA),
    )
    assert assembled.variables == {"input": payload}


def test_the_type_factory_reports_its_type_name() -> None:
    assert namespace().CreateUserInput.type_name == "CreateUserInput"


def test_the_namespace_checks_the_scalars_argument() -> None:
    with pytest.raises(TypeError, match="scalars"):
        FakeNamespace(
            SCHEMA,
            {"Money": 1},  # type: ignore[arg-type]
            global_seed=0,
            node_id="n",
            unique_source=UniqueSource("r"),
        )


def test_a_type_that_is_an_interface_or_a_union_says_so() -> None:
    schema = build_schema(
        """
        interface Shape { id: ID! }
        type Circle implements Shape { id: ID! }
        union Any = Circle
        input Plain { a: String }
        type Query { a: String }
        """
    )
    fake = namespace(schema=schema)
    with pytest.raises(SchemaError, match="an interface"):
        fake.Shape  # noqa: B018
    with pytest.raises(SchemaError, match="a union"):
        fake.Any  # noqa: B018
    with pytest.raises(SchemaError, match="an object type"):
        fake.Circle  # noqa: B018
