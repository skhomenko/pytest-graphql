"""Seed derivation (B1): the formula, byte for byte."""

from __future__ import annotations

import pytest

from pytest_graphql._core.factory.seed import derive_seed
from tests.factory.reference import seed_for

# Computed outside the package with plain hashlib, so a typo in the formula
# cannot be copied into both the code and its test.
HAND_COMPUTED = [
    (0, "tests/test_a.py::test_one", 10921954168542581337),
    (42, "tests/test_a.py::test_one", 12469997269336330434),
    (0, "", 16498388624318395347),
    (-1, "x", 16404053735927228982),
    (7, "tests/ünï.py::t[ä-1]", 2580685048498743698),
]


@pytest.mark.parametrize(("global_seed", "node_id", "expected"), HAND_COMPUTED)
def test_the_seed_matches_the_hand_computed_value(
    global_seed: int, node_id: str, expected: int
) -> None:
    assert derive_seed(global_seed, node_id) == expected


@pytest.mark.parametrize(("global_seed", "node_id", "_"), HAND_COMPUTED)
def test_the_seed_matches_the_reference_formula(
    global_seed: int, node_id: str, _: int
) -> None:
    assert derive_seed(global_seed, node_id) == seed_for(global_seed, node_id)


def test_the_seed_is_an_unsigned_64_bit_integer() -> None:
    for number in range(50):
        assert 0 <= derive_seed(number, f"node-{number}") < 2**64


def test_the_seed_depends_on_both_inputs() -> None:
    base = derive_seed(1, "a")
    assert derive_seed(2, "a") != base
    assert derive_seed(1, "b") != base


def test_the_separator_keeps_the_two_inputs_apart() -> None:
    # Without a separator, (1, "23") and (12, "3") would hash the same text.
    assert derive_seed(1, "23") != derive_seed(12, "3")


def test_a_node_id_with_a_lone_surrogate_still_derives_a_seed() -> None:
    # pytest builds node ids from file names, which can carry undecodable
    # bytes. They reach Python as lone surrogates, which plain `.encode()`
    # refuses. The value for every encodable text must stay what the design
    # states, so only the unencodable case may differ from `.encode()`.
    first = derive_seed(0, "tests/\udcff.py::t")
    assert first == derive_seed(0, "tests/\udcff.py::t")
    assert first != derive_seed(0, "tests/.py::t")


@pytest.mark.parametrize("bad", [True, False, 1.0, "1", None])
def test_a_global_seed_must_be_an_int_and_not_a_bool(bad: object) -> None:
    with pytest.raises(TypeError, match="global_seed"):
        derive_seed(bad, "node")  # type: ignore[arg-type]


def test_a_node_id_must_be_text() -> None:
    with pytest.raises(TypeError, match="node_id"):
        derive_seed(0, b"node")  # type: ignore[arg-type]
