"""``unique()``: distinct values, and state that stays bounded."""

from __future__ import annotations

import gc
import tracemalloc

import pytest

from pytest_graphql._core.factory.unique import Unique, UniqueSource, resolve, unique

NODE = "tests/test_x.py::test_one"
PATH = ("CreateUserInput", "email")


def test_unique_returns_a_marker_that_remembers_its_kind() -> None:
    assert unique() == Unique(None)
    assert unique("email") == Unique("email")
    assert unique("email").kind == "email"


@pytest.mark.parametrize("kind", ["phone", "", "Email", 1])
def test_an_unknown_kind_is_refused_with_the_allowed_ones(kind: object) -> None:
    with pytest.raises((ValueError, TypeError), match="email"):
        unique(kind)  # type: ignore[arg-type]


def test_repeated_calls_in_one_test_differ() -> None:
    source = UniqueSource("run-1")
    values = [source.issue(None, NODE, PATH) for _ in range(500)]
    assert len(set(values)) == 500


def test_values_differ_across_xdist_worker_ids() -> None:
    # Two workers are two processes, so each starts its own counter at zero
    # and sees the same node id and path. Only the worker id tells them apart.
    values = {
        UniqueSource("run-1", worker).issue(None, NODE, PATH)
        for worker in ("main", "gw0", "gw1", "gw2")
    }
    assert len(values) == 4


def test_values_differ_across_runs() -> None:
    first = UniqueSource("run-1").issue(None, NODE, PATH)
    second = UniqueSource("run-2").issue(None, NODE, PATH)
    assert first != second


def test_values_differ_across_node_ids_and_paths() -> None:
    values = {
        UniqueSource("r").issue(None, NODE, PATH),
        UniqueSource("r").issue(None, NODE + "2", PATH),
        UniqueSource("r").issue(None, NODE, ("CreateUserInput", "username")),
    }
    assert len(values) == 3


def test_the_same_source_state_gives_the_same_value() -> None:
    # The derivation is a pure function of run id, worker id, node id, path
    # and counter, which is what makes a value traceable to its run.
    assert UniqueSource("r", "gw0").issue(None, NODE, PATH) == (
        UniqueSource("r", "gw0").issue(None, NODE, PATH)
    )


def test_the_default_worker_id_is_main() -> None:
    assert UniqueSource("r").worker_id == "main"
    assert UniqueSource("r").issue(None, NODE, PATH) == (
        UniqueSource("r", "main").issue(None, NODE, PATH)
    )


def test_path_segments_cannot_be_confused_with_each_other() -> None:
    left = UniqueSource("r").issue(None, NODE, ("ab", "c"))
    right = UniqueSource("r").issue(None, NODE, ("a", "bc"))
    assert left != right


def test_the_node_id_cannot_leak_into_the_path() -> None:
    left = UniqueSource("r").issue(None, "a", ("b",))
    right = UniqueSource("r").issue(None, "ab", ())
    assert left != right


def test_a_plain_value_is_a_short_lowercase_token() -> None:
    value = UniqueSource("r").issue(None, NODE, PATH)
    assert value.startswith("u")
    assert len(value) == 17
    assert value[1:] == value[1:].lower()
    int(value[1:], 16)


def test_an_email_value_has_a_reserved_documentation_domain() -> None:
    value = UniqueSource("r").issue("email", NODE, PATH)
    local, _, domain = value.partition("@")
    assert domain == "example.com"
    assert local.startswith("u") and len(local) == 17


def test_string_is_the_same_kind_as_none() -> None:
    assert UniqueSource("r").issue("string", NODE, PATH) == (
        UniqueSource("r").issue(None, NODE, PATH)
    )


def test_the_counter_counts_each_issue_once() -> None:
    source = UniqueSource("r")
    for _ in range(7):
        source.issue(None, NODE, PATH)
    assert source.issued == 7


def test_the_state_does_not_grow_with_node_ids_or_paths() -> None:
    # PLAN C10 kept one counter per node id and field path, which grows with
    # every test and every field. This source keeps one integer, so the
    # state is the same size after one test or after a million.
    source = UniqueSource("r", "gw1")

    def exercise(count: int, start: int) -> None:
        for number in range(start, start + count):
            source.issue(
                None, f"tests/test_{number}.py::test_{number}", ("T", f"f{number}")
            )

    exercise(500, 0)
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        exercise(20_000, 500)
        gc.collect()
        after = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()

    assert after - before < 16 * 1024
    assert source.issued == 20_500


def test_the_state_holds_only_scalars_and_a_lock() -> None:
    source = UniqueSource("r", "gw1")
    for number in range(100):
        source.issue(None, f"node-{number}", ("T", f"f{number}"))
    held = {name: getattr(source, name) for name in UniqueSource.__slots__}
    for name, value in held.items():
        assert not isinstance(value, (dict, list, set, tuple, frozenset)), name


def test_a_source_needs_text_ids() -> None:
    with pytest.raises(TypeError, match="run_id"):
        UniqueSource(1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="worker_id"):
        UniqueSource("r", 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="run_id"):
        UniqueSource("")
    with pytest.raises(ValueError, match="worker_id"):
        UniqueSource("r", "")


def test_resolve_replaces_a_marker_with_a_value() -> None:
    source = UniqueSource("r")
    assert resolve(unique(), source, NODE, PATH) == source_value(NODE, PATH, 0)
    assert source.issued == 1


def source_value(node: str, path: tuple[str, ...], issued: int) -> str:
    source = UniqueSource("r")
    for _ in range(issued):
        source.issue(None, node, path)
    return source.issue(None, node, path)


def test_resolve_walks_dicts_lists_and_tuples() -> None:
    source = UniqueSource("r")
    result = resolve(
        {"a": unique(), "b": [unique("email"), {"c": unique()}], "d": (unique(),)},
        source,
        NODE,
        ("T", "f"),
    )
    assert isinstance(result["a"], str)
    assert result["b"][0].endswith("@example.com")
    assert isinstance(result["b"][1]["c"], str)
    assert isinstance(result["d"], tuple)
    assert source.issued == 4
    flat = [result["a"], result["b"][0], result["b"][1]["c"], result["d"][0]]
    assert len(set(flat)) == 4


def test_resolve_leaves_other_values_alone() -> None:
    source = UniqueSource("r")
    value = {"a": 1, "b": [None, "x", 2.5], "c": {"d": True}}
    assert resolve(value, source, NODE, ("T",)) == value
    assert source.issued == 0


def test_resolve_does_not_mutate_its_input() -> None:
    source = UniqueSource("r")
    original = {"a": [unique()]}
    resolve(original, source, NODE, ("T",))
    assert isinstance(original["a"][0], Unique)


def test_resolve_uses_the_location_of_each_marker() -> None:
    # Two markers at different places differ even on equal counters, because
    # the path is part of the derivation. Check by replaying the counter.
    source = UniqueSource("r")
    result = resolve({"a": unique(), "b": unique()}, source, NODE, ("T",))
    replay = UniqueSource("r")
    assert result["a"] == replay.issue(None, NODE, ("T", "a"))
    assert result["b"] == replay.issue(None, NODE, ("T", "b"))
