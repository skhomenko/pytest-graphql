"""``SelectionPolicy``: its defaults, its excludes, and the C18 cache rule.

The cache rule is the part of this milestone that fails silently when it is
wrong, so it is tested through observable behaviour rather than through the
flag: what matters is that a custom policy's decisions are never served from
an entry that was built under different decisions.
"""

from __future__ import annotations

import gc
import warnings
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from graphql import build_schema as build_sdl_schema

from pytest_graphql._core.selection import policy as policy_module
from pytest_graphql._core.selection.policy import (
    SelectionPolicy,
    is_connection_type,
    missing_required_arguments,
    page_size_argument,
)
from tests.schema.resolvers import build_schema


@pytest.fixture(autouse=True)
def _forget_warned_classes() -> Iterator[None]:
    """The C18 warning fires once per class, for the life of the process.

    Tests would otherwise depend on which of them ran first.
    """
    saved = set(policy_module._WARNED_CLASSES)
    policy_module._WARNED_CLASSES.clear()
    yield
    policy_module._WARNED_CLASSES.clear()
    policy_module._WARNED_CLASSES.update(saved)


def test_defaults_are_the_documented_ones() -> None:
    policy = SelectionPolicy()
    assert policy.max_depth == 3
    assert policy.cycle_policy == "shallow"
    assert policy.per_type_depth_cap == {}
    assert policy.include_deprecated is False
    assert policy.max_fields == 2000
    assert policy.exclude == ()
    assert policy.relay_aware is True
    assert policy.connection_page_size == 10
    assert policy.max_union_members == 10
    assert policy.max_connection_depth == 1


def test_a_policy_copies_the_containers_it_is_given() -> None:
    """A later mutation of the caller's dict must not change the fingerprint."""
    caps = {"User": 1}
    excludes = ["User.manager"]
    policy = SelectionPolicy(per_type_depth_cap=caps, exclude=excludes)
    before = policy.fingerprint

    caps["User"] = 99
    excludes.append("Team.captain")

    assert policy.per_type_depth_cap == {"User": 1}
    assert policy.exclude == ("User.manager",)
    assert policy.fingerprint == before


def test_equal_fields_give_equal_fingerprints() -> None:
    assert SelectionPolicy().fingerprint == SelectionPolicy().fingerprint
    assert (
        SelectionPolicy(per_type_depth_cap={"User": 1, "Team": 2}).fingerprint
        == SelectionPolicy(per_type_depth_cap={"Team": 2, "User": 1}).fingerprint
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"max_depth": 4},
        {"cycle_policy": "stop"},
        {"per_type_depth_cap": {"User": 1}},
        {"include_deprecated": True},
        {"max_fields": 10},
        {"exclude": ("User.manager",)},
        {"relay_aware": False},
        {"connection_page_size": 25},
        {"max_union_members": 2},
        {"max_connection_depth": 0},
    ],
)
def test_every_field_changes_the_fingerprint(changed: dict[str, object]) -> None:
    """A field left out of the key would let one policy read another's entry."""
    assert SelectionPolicy(**changed).fingerprint != SelectionPolicy().fingerprint  # type: ignore[arg-type]


def test_the_default_policy_is_cached() -> None:
    policy = SelectionPolicy()
    assert policy.memoizable is True
    assert policy.cache_key == policy.fingerprint


def test_a_subclass_that_changes_nothing_shares_the_base_key() -> None:
    class Same(SelectionPolicy):
        pass

    assert Same().cache_key == SelectionPolicy().cache_key


def test_a_subclass_field_is_part_of_the_key() -> None:
    @dataclass(frozen=True)
    class WithExtra(SelectionPolicy):
        tenant: str = "a"

    assert WithExtra(tenant="a").cache_key != WithExtra(tenant="b").cache_key


def test_an_undescribable_subclass_field_says_what_to_do() -> None:
    @dataclass(frozen=True)
    class WithObject(SelectionPolicy):
        marker: object = object()

    with pytest.raises(TypeError) as caught:
        WithObject().fingerprint  # noqa: B018
    assert "Override fingerprint" in str(caught.value)


class _OverridesHook(SelectionPolicy):
    """Overrides the hook and not the fingerprint: the C18 case."""

    def should_include(
        self,
        parent_type: str,  # noqa: ARG002 -- the hook's contract
        field_name: str,
        path: tuple[str, ...],  # noqa: ARG002 -- the hook's contract
        depth: int,  # noqa: ARG002 -- the hook's contract
    ) -> bool:
        return field_name != "name"


def test_overriding_the_hook_alone_disables_caching_and_warns() -> None:
    with pytest.warns(UserWarning) as caught:
        policy = _OverridesHook()
    assert policy.memoizable is False
    assert policy.cache_key is None
    message = str(caught[0].message)
    assert "_OverridesHook" in message
    assert "fingerprint" in message


def test_the_warning_is_emitted_once_per_class() -> None:
    with pytest.warns(UserWarning):
        _OverridesHook()
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        _OverridesHook()
    assert recorded == []


def test_overriding_both_opts_back_into_caching() -> None:
    class Owned(SelectionPolicy):
        def should_include(
            self,
            parent_type: str,  # noqa: ARG002 -- the hook's contract
            field_name: str,
            path: tuple[str, ...],  # noqa: ARG002 -- the hook's contract
            depth: int,  # noqa: ARG002 -- the hook's contract
        ) -> bool:
            return field_name != "name"

        @property
        def fingerprint(self) -> str:
            return "owned-v1"

    policy = Owned()
    assert policy.memoizable is True
    assert policy.cache_key == "owned-v1"


def test_two_short_lived_instances_never_share_a_key() -> None:
    """C18's first case, at the key level.

    Neither instance offers a key at all, so no entry can pass from one to
    the other however the runtime reuses memory. The behaviour this protects
    is asserted against a real build in ``test_selection_builder.py``.
    """
    with pytest.warns(UserWarning):
        first = _OverridesHook()
    assert first.cache_key is None
    del first
    gc.collect()
    assert _OverridesHook().cache_key is None


@pytest.mark.parametrize(
    ("pattern", "type_name", "field_name", "expected"),
    [
        ("User.manager", "User", "manager", False),
        ("User.manager", "Team", "manager", True),
        ("*.id", "Anything", "id", False),
        ("*.id", "Anything", "name", True),
        ("Settings.*", "Settings", "theme", False),
        ("Settings.*", "User", "theme", True),
    ],
)
def test_exclude_patterns_in_all_three_shapes(
    pattern: str, type_name: str, field_name: str, expected: bool
) -> None:
    policy = SelectionPolicy(exclude=[pattern])
    assert policy.should_include(type_name, field_name, (), 0) is expected


@pytest.mark.parametrize("pattern", ["manager", "User.manager.deep", ".field", "User."])
def test_a_pattern_that_could_never_match_is_refused(pattern: str) -> None:
    with pytest.raises(ValueError, match=r"Type\.field"):
        SelectionPolicy(exclude=[pattern])


def test_connection_detection_needs_the_name_and_the_shape() -> None:
    schema = build_schema()
    assert is_connection_type(schema.type_map["PostConnection"]) is True
    assert is_connection_type(schema.type_map["PostEdge"]) is False
    assert is_connection_type(schema.type_map["User"]) is False


def test_the_page_size_argument_is_found_through_a_non_null_wrapper() -> None:
    schema = build_schema()
    user = schema.type_map["User"]
    assert page_size_argument(user.fields["postsConnection"]) is not None
    # posts(first: Int!) is not a connection field, but the argument is still
    # the one C1 names, so the helper answers about the argument alone.
    assert page_size_argument(user.fields["posts"]) is not None
    assert page_size_argument(user.fields["friends"]) is None


def test_required_arguments_ignore_the_ones_already_supplied() -> None:
    schema = build_schema()
    posts = schema.type_map["User"].fields["posts"]
    assert missing_required_arguments(posts) == ("first",)
    assert missing_required_arguments(posts, ("first",)) == ()


def test_an_argument_with_a_default_is_not_required() -> None:
    schema = build_schema()
    friends = schema.type_map["User"].fields["friends"]
    assert missing_required_arguments(friends) == ()


# Regressions for cycle CR-20260911T230447Z-5c65bfb-0a8db502.


def test_a_page_size_argument_that_is_not_an_integer_is_not_one() -> None:
    """F01: the engine sends one variable of one declared type.

    Only ``Int`` and ``Int!`` are positions that variable is valid at. A list
    of integers is a different shape, so the field is not pageable by it.
    """
    schema = build_sdl_schema(
        """
        type Query {
          a(first: [Int]): Int
          b(first: [Int!]!): Int
          c(first: String): Int
          d(first: Int!): Int
        }
        """
    )
    fields = schema.type_map["Query"].fields
    assert page_size_argument(fields["a"]) is None
    assert page_size_argument(fields["b"]) is None
    assert page_size_argument(fields["c"]) is None
    assert page_size_argument(fields["d"]) is not None


def test_the_warned_class_registry_releases_classes_that_are_gone() -> None:
    """F05: no accumulator in this package grows for the life of the process.

    Subclassing is the documented extension point and a class can be built at
    run time, so a registry of class names would have no bound at all.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for index in range(300):
            created = type(
                f"Throwaway{index}",
                (SelectionPolicy,),
                {"should_include": _always_include},
            )
            created()
            del created
    gc.collect()
    assert len(policy_module._WARNED_CLASSES) == 0


def _always_include(
    self: SelectionPolicy,  # noqa: ARG001 -- the hook's contract
    type_name: str,  # noqa: ARG001 -- the hook's contract
    field_name: str,  # noqa: ARG001 -- the hook's contract
    path: tuple[str, ...],  # noqa: ARG001 -- the hook's contract
    depth: int,  # noqa: ARG001 -- the hook's contract
) -> bool:
    return True
