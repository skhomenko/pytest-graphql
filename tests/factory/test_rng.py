"""``DeterministicRandom``: the byte stream, each method and its edges."""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pytest_graphql._core.factory.rng import DEFAULT_ALPHABET, DeterministicRandom
from tests.factory.reference import ReferenceStream

# The first 16 bytes of the stream for three keys. Pinned as literals so the
# format cannot drift on both sides of a comparison at once.
STREAM_HEAD = [
    (0, (), "b5d5f91e47d7fa6495513d6471996161"),
    (1, ("T", "f"), "2449c6ee29276a4a20800feeeb6114be"),
    (
        10921954168542581337,
        ("CreateUserInput", "name"),
        "1dcb0c196d0f08c32f1a98b02498d9c9",
    ),
]


@pytest.mark.parametrize(("seed", "path", "head"), STREAM_HEAD)
def test_the_stream_starts_with_the_pinned_bytes(
    seed: int, path: tuple[str, ...], head: str
) -> None:
    value = DeterministicRandom(seed, *path).bits(128)
    assert value == int(head, 16)


@given(
    st.integers(0, 2**64 - 1),
    st.lists(st.text(max_size=8), max_size=4),
    st.lists(st.integers(1, 300), min_size=1, max_size=20),
)
def test_bits_follow_the_reference_stream(
    seed: int, path: list[str], widths: list[int]
) -> None:
    rng = DeterministicRandom(seed, *path)
    reference = ReferenceStream(seed, *path)
    for width in widths:
        assert rng.bits(width) == reference.bits(width)


@given(
    st.integers(0, 2**64 - 1),
    st.lists(st.integers(1, 2**70), min_size=1, max_size=12),
)
def test_below_follows_the_reference_stream(seed: int, bounds: list[int]) -> None:
    rng = DeterministicRandom(seed, "ref")
    reference = ReferenceStream(seed, "ref")
    for bound in bounds:
        assert rng.below(bound) == reference.below(bound)


@given(st.integers(0, 2**64 - 1), st.integers(0, 400))
def test_bits_stay_below_two_to_the_width(seed: int, width: int) -> None:
    assert 0 <= DeterministicRandom(seed).bits(width) < 2**width


def test_zero_bits_is_zero_and_consumes_nothing() -> None:
    rng = DeterministicRandom(5, "z")
    assert rng.bits(0) == 0
    assert rng.bits(64) == ReferenceStream(5, "z").bits(64)


def test_below_one_is_zero_and_consumes_nothing() -> None:
    rng = DeterministicRandom(5, "one")
    assert [rng.below(1) for _ in range(5)] == [0] * 5
    assert rng.bits(64) == ReferenceStream(5, "one").bits(64)


@pytest.mark.parametrize("power", [1, 2, 3, 8, 16, 63, 64, 65, 200])
def test_below_a_power_of_two_never_rejects(power: int) -> None:
    # A power of two fits its width exactly, so every candidate is accepted
    # and the draw costs exactly one `bits` call.
    bound = 2**power
    rng = DeterministicRandom(11, "pow")
    reference = ReferenceStream(11, "pow")
    for _ in range(20):
        assert rng.below(bound) == reference.bits(power)


def test_below_rejects_a_candidate_at_or_above_the_bound() -> None:
    # Seed 1 and path ("rej",): the first 3 bit candidate is 6, which is not
    # below 5. Rejection sampling discards it and draws again, so the result
    # comes from the second byte and exactly two bytes are consumed. A modulo
    # would return 6 % 5 == 1 from the first byte.
    reference = ReferenceStream(1, "rej")
    assert reference.bits(3) == 6
    rng = DeterministicRandom(1, "rej")
    assert rng.below(5) == 4
    assert rng.bits(16) == _stream_after(1, "rej", consumed=2).bits(16)


def _stream_after(seed: int, label: str, *, consumed: int) -> ReferenceStream:
    stream = ReferenceStream(seed, label)
    stream.take(consumed)
    return stream


def test_below_a_large_bound_uses_the_whole_width() -> None:
    bound = 2**200 + 12345
    rng = DeterministicRandom(3, "big")
    reference = ReferenceStream(3, "big")
    for _ in range(10):
        value = rng.below(bound)
        assert value == reference.below(bound)
        assert 0 <= value < bound


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_below_needs_a_positive_bound(bad: int) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        DeterministicRandom(0).below(bad)


@pytest.mark.parametrize("bad", [True, 2.0, "3", None])
def test_below_needs_an_int(bad: object) -> None:
    with pytest.raises(TypeError, match="below"):
        DeterministicRandom(0).below(bad)  # type: ignore[arg-type]


def test_bits_need_a_non_negative_int() -> None:
    with pytest.raises(ValueError, match="0 or more"):
        DeterministicRandom(0).bits(-1)
    with pytest.raises(TypeError, match="bits"):
        DeterministicRandom(0).bits(2.5)  # type: ignore[arg-type]


@settings(max_examples=40)
@given(st.integers(0, 2**64 - 1), st.integers(2, 40))
def test_below_is_uniform_over_a_small_range(seed: int, bound: int) -> None:
    # A chi-square test with a threshold far in the tail (p about 1e-7), so a
    # correct sampler essentially never fails it. The bound is small and not a
    # power of two in most draws, which is exactly where a plain modulo over
    # the minimal width piles probability onto the low values: for a bound of
    # 3 it returns 0 half the time and 1 and 2 a quarter each.
    draws = 6000
    rng = DeterministicRandom(seed, "uniform")
    counts = [0] * bound
    for _ in range(draws):
        counts[rng.below(bound)] += 1
    expected = draws / bound
    statistic = sum((count - expected) ** 2 / expected for count in counts)
    assert statistic < _chi_square_limit(bound - 1)


def _chi_square_limit(degrees: int) -> float:
    # Wilson-Hilferty approximation of the 1 - 1e-7 quantile (z = 5.2).
    z = 5.2
    return degrees * (1 - 2 / (9 * degrees) + z * math.sqrt(2 / (9 * degrees))) ** 3


@given(st.integers(0, 2**64 - 1))
def test_float_unit_uses_exactly_53_bits(seed: int) -> None:
    rng = DeterministicRandom(seed, "f")
    reference = ReferenceStream(seed, "f")
    value = rng.float_unit()
    assert value == reference.bits(53) / 2**53
    assert 0.0 <= value < 1.0
    # 53 bits means the value is an exact multiple of 2**-53.
    assert (value * 2**53).is_integer()


def test_float_unit_consumes_seven_bytes() -> None:
    rng = DeterministicRandom(9, "f7")
    rng.float_unit()
    assert rng.bits(8) == _stream_after(9, "f7", consumed=7).bits(8)


def test_choice_picks_by_index_from_a_sequence() -> None:
    items = ["a", "b", "c", "d", "e"]
    rng = DeterministicRandom(4, "c")
    reference = ReferenceStream(4, "c")
    for _ in range(20):
        assert rng.choice(items) == items[reference.below(len(items))]


def test_choice_works_on_tuples_ranges_and_strings() -> None:
    rng = DeterministicRandom(4, "c2")
    assert rng.choice((7,)) == 7
    assert rng.choice(range(10, 11)) == 10
    assert rng.choice("x") == "x"


def test_choice_refuses_an_empty_sequence() -> None:
    with pytest.raises(ValueError, match="empty"):
        DeterministicRandom(0).choice([])


@pytest.mark.parametrize("unordered", [{1, 2, 3}, frozenset({1, 2}), {1: "a"}])
def test_choice_refuses_a_collection_with_no_stable_order(
    unordered: object,
) -> None:
    # Iterating a set depends on string hashing, which is salted per process.
    with pytest.raises(TypeError, match="sequence"):
        DeterministicRandom(0).choice(unordered)  # type: ignore[arg-type]


def test_sample_string_draws_one_character_per_index() -> None:
    rng = DeterministicRandom(8, "s")
    reference = ReferenceStream(8, "s")
    expected = "".join(
        DEFAULT_ALPHABET[reference.below(len(DEFAULT_ALPHABET))] for _ in range(24)
    )
    assert rng.sample_string(24) == expected
    assert set(expected) <= set(DEFAULT_ALPHABET)


def test_sample_string_takes_a_custom_alphabet() -> None:
    value = DeterministicRandom(8, "s2").sample_string(50, "ab")
    assert len(value) == 50
    assert set(value) <= {"a", "b"}


def test_sample_string_of_length_zero_is_empty() -> None:
    assert DeterministicRandom(0).sample_string(0) == ""


def test_sample_string_validates_its_arguments() -> None:
    rng = DeterministicRandom(0)
    with pytest.raises(ValueError, match="0 or more"):
        rng.sample_string(-1)
    with pytest.raises(ValueError, match="alphabet"):
        rng.sample_string(3, "")
    with pytest.raises(TypeError, match="alphabet"):
        rng.sample_string(3, ["a"])  # type: ignore[arg-type]


def test_the_default_alphabet_is_lowercase_letters_and_digits() -> None:
    assert DEFAULT_ALPHABET == "abcdefghijklmnopqrstuvwxyz0123456789"


def test_streams_for_different_paths_differ() -> None:
    a = DeterministicRandom(1, "T", "a").bits(128)
    b = DeterministicRandom(1, "T", "b").bits(128)
    c = DeterministicRandom(1, "T").bits(128)
    assert len({a, b, c}) == 3


def test_path_segments_cannot_be_confused_with_each_other() -> None:
    # ("ab", "c") and ("a", "bc") join to the same text, so the encoding must
    # carry lengths and not just concatenate.
    left = DeterministicRandom(1, "ab", "c").bits(128)
    right = DeterministicRandom(1, "a", "bc").bits(128)
    assert left != right


def test_an_integer_segment_reads_as_its_decimal_text() -> None:
    assert DeterministicRandom(1, "xs", 3).bits(64) == (
        DeterministicRandom(1, "xs", "3").bits(64)
    )


def test_the_same_inputs_give_the_same_stream() -> None:
    first = DeterministicRandom(77, "T", "f")
    second = DeterministicRandom(77, "T", "f")
    assert [first.bits(32) for _ in range(40)] == [second.bits(32) for _ in range(40)]


@pytest.mark.parametrize("bad", [-1, 2**64])
def test_the_seed_must_fit_unsigned_64_bits(bad: int) -> None:
    with pytest.raises(ValueError, match="seed"):
        DeterministicRandom(bad)


@pytest.mark.parametrize("bad", [True, 1.5, "1", None])
def test_the_seed_must_be_an_int(bad: object) -> None:
    with pytest.raises(TypeError, match="seed"):
        DeterministicRandom(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [True, 1.5, None, b"x"])
def test_a_path_segment_must_be_text_or_an_int(bad: object) -> None:
    with pytest.raises(TypeError, match="path"):
        DeterministicRandom(0, bad)  # type: ignore[arg-type]


def test_a_long_run_keeps_a_small_buffer() -> None:
    # The state is a key, a counter and less than one block of unread bytes,
    # however many values are drawn.
    rng = DeterministicRandom(2, "long")
    for _ in range(5000):
        rng.bits(64)
    assert len(rng._pending) < 32


def test_the_repr_does_not_show_the_seed() -> None:
    assert repr(DeterministicRandom(123456789)) == "DeterministicRandom(...)"
