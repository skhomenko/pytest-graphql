"""``ScalarSpec``, ``ScalarRegistry`` and the built-in scalar fakes."""

from __future__ import annotations

from decimal import Decimal

import pytest

from pytest_graphql._core.factory.rng import DeterministicRandom
from pytest_graphql._core.factory.scalars import (
    BUILTIN_FAKES,
    ScalarRegistry,
    ScalarSpec,
)


def money_spec(**changes: object) -> ScalarSpec:
    fields: dict[str, object] = {
        "name": "Money",
        "serialize": str,
        "fake": lambda rng: Decimal(rng.below(10_000)) / 100,
        "parse": Decimal,
    }
    fields.update(changes)
    return ScalarSpec(**fields)  # type: ignore[arg-type]


def test_a_spec_is_a_frozen_dataclass_with_the_documented_fields() -> None:
    spec = money_spec()
    assert (spec.name, spec.serialize, spec.parse) == ("Money", str, Decimal)
    with pytest.raises(AttributeError):
        spec.name = "Other"  # type: ignore[misc]


def test_parse_defaults_to_none_so_the_raw_value_stays() -> None:
    spec = ScalarSpec(name="Stamp", serialize=str, fake=lambda rng: rng.bits(8))
    assert spec.parse is None


@pytest.mark.parametrize("name", ["", "1Money", "Mo ney", "Mo-ney", "__Money"])
def test_a_spec_name_must_be_a_graphql_name(name: str) -> None:
    with pytest.raises(ValueError, match="not a GraphQL name"):
        money_spec(name=name)


@pytest.mark.parametrize("name", ["String", "Int", "Float", "Boolean", "ID"])
def test_a_spec_cannot_take_a_built_in_scalar_name(name: str) -> None:
    with pytest.raises(ValueError, match="built-in"):
        money_spec(name=name)


def test_a_spec_name_must_be_text() -> None:
    with pytest.raises(TypeError, match="name"):
        money_spec(name=1)


@pytest.mark.parametrize("field", ["serialize", "fake"])
def test_serialize_and_fake_must_be_callable(field: str) -> None:
    with pytest.raises(TypeError, match=field):
        money_spec(**{field: "nope"})


def test_parse_must_be_callable_or_none() -> None:
    with pytest.raises(TypeError, match="parse"):
        money_spec(parse="nope")


def test_the_registry_registers_and_finds_a_spec() -> None:
    registry = ScalarRegistry()
    spec = money_spec()
    registry.register(spec)
    assert registry.get("Money") is spec
    assert "Money" in registry
    assert len(registry) == 1
    assert list(registry) == ["Money"]


def test_the_registry_returns_none_for_an_unknown_name() -> None:
    assert ScalarRegistry().get("Money") is None
    assert "Money" not in ScalarRegistry()


def test_the_registry_can_start_from_specs() -> None:
    registry = ScalarRegistry([money_spec(), money_spec(name="Stamp")])
    assert list(registry) == ["Money", "Stamp"]


def test_names_iterate_in_sorted_order() -> None:
    registry = ScalarRegistry()
    for name in ("Zed", "Alpha", "Mid"):
        registry.register(money_spec(name=name))
    assert list(registry) == ["Alpha", "Mid", "Zed"]


def test_registering_a_name_twice_is_refused_by_default() -> None:
    registry = ScalarRegistry([money_spec()])
    with pytest.raises(ValueError, match=r"(?s)already registered.*replace=True"):
        registry.register(money_spec())


def test_replace_swaps_the_spec() -> None:
    registry = ScalarRegistry([money_spec()])
    other = money_spec(parse=None)
    registry.register(other, replace=True)
    assert registry.get("Money") is other
    assert len(registry) == 1


def test_register_needs_a_spec() -> None:
    with pytest.raises(TypeError, match="ScalarSpec"):
        ScalarRegistry().register("Money")  # type: ignore[arg-type]


def test_parsers_hold_only_the_specs_that_decode() -> None:
    registry = ScalarRegistry([money_spec(), money_spec(name="Stamp", parse=None)])
    assert registry.parsers() == {"Money": Decimal}


def test_parsers_are_a_snapshot_not_a_view() -> None:
    registry = ScalarRegistry()
    snapshot = registry.parsers()
    registry.register(money_spec())
    assert snapshot == {}
    assert registry.parsers() == {"Money": Decimal}


def test_the_built_in_fakes_cover_exactly_the_specified_scalars() -> None:
    assert set(BUILTIN_FAKES) == {"String", "Int", "Float", "Boolean", "ID"}


def _draw(name: str, seed: int = 3) -> object:
    return BUILTIN_FAKES[name](DeterministicRandom(seed, "t", name))


@pytest.mark.parametrize("seed", range(25))
def test_each_built_in_fake_makes_a_valid_value_of_its_type(seed: int) -> None:
    string = _draw("String", seed)
    assert isinstance(string, str) and len(string) == 8 and string.isalnum()
    identifier = _draw("ID", seed)
    assert isinstance(identifier, str) and len(identifier) == 12
    assert set(identifier) <= set("0123456789abcdef")
    integer = _draw("Int", seed)
    assert type(integer) is int and 1 <= integer <= 1000
    number = _draw("Float", seed)
    assert type(number) is float and 0.0 <= number < 1000.0
    assert type(_draw("Boolean", seed)) is bool


def test_the_float_fake_has_two_decimal_places_by_construction() -> None:
    # An integer count over 100 is one correctly rounded division, so the
    # value does not depend on how any version prints a float.
    for seed in range(50):
        value = BUILTIN_FAKES["Float"](DeterministicRandom(seed, "f"))
        assert value == round(value * 100) / 100


def test_every_boolean_value_occurs() -> None:
    seen = {BUILTIN_FAKES["Boolean"](DeterministicRandom(n, "b")) for n in range(40)}
    assert seen == {True, False}


def test_the_registry_repr_lists_its_names() -> None:
    registry = ScalarRegistry([money_spec(name="Zed"), money_spec(name="Alpha")])
    assert repr(registry) == "ScalarRegistry(Alpha, Zed)"
    assert repr(ScalarRegistry()) == "ScalarRegistry()"
