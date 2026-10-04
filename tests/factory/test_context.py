"""``FakeContext``: the per-test inputs a client hands to ``gql.fake``."""

from __future__ import annotations

import dataclasses

import pytest

from pytest_graphql._core.factory import FakeContext, UniqueSource


def test_a_context_holds_a_node_id_and_a_unique_source() -> None:
    source = UniqueSource("run-1", "gw0")
    context = FakeContext(node_id="tests/test_x.py::test_a", unique_source=source)
    assert context.node_id == "tests/test_x.py::test_a"
    assert context.unique_source is source


def test_a_context_is_frozen() -> None:
    context = FakeContext("n", UniqueSource("r"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.node_id = "other"  # type: ignore[misc]


def test_a_context_checks_its_fields() -> None:
    with pytest.raises(TypeError, match="node_id"):
        FakeContext(1, UniqueSource("r"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="unique_source"):
        FakeContext("n", None)  # type: ignore[arg-type]


def test_a_standalone_context_has_a_fixed_node_id() -> None:
    assert FakeContext.standalone().node_id == "standalone"
    assert FakeContext.standalone().node_id == FakeContext.standalone().node_id


def test_a_standalone_context_has_its_own_run_id_each_time() -> None:
    first = FakeContext.standalone().unique_source
    second = FakeContext.standalone().unique_source
    assert first is not second
    assert first.run_id != second.run_id
    assert first.worker_id == "main"
