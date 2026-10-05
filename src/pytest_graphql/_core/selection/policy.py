"""``SelectionPolicy`` (B8), its excludes, and the relay and size-guard rules.

The policy is one frozen dataclass, not a protocol: B8 collapsed the
specification's two readings into a single type with one overridable hook.
The defaults are SPEC 3.10 as corrected by C1, which turned
``include_deprecated`` off and added the three cost limits
(``connection_page_size``, ``max_union_members`` and ``max_connection_depth``).
Every one of these numbers is a candidate for the calibration gate, which
measures them against the checked-in schema corpus. None of them may be
changed here to make a test pass.

Memoization is the part of this module most easily got wrong, so C18 states
it exactly and ``cache_key`` is the only place that decides it. The
fingerprint is derived from the dataclass fields, so a subclass that only
adds fields keeps a correct key for free. A subclass that overrides
``should_include`` changes behaviour the fingerprint cannot see, and unless
it also overrides ``fingerprint`` its selections are never cached. There is
deliberately no identity-based fallback: ``id()`` is reused after collection,
so one instance could read an earlier instance's entry, and an identity key
also stays constant while a policy reads changing external state. Both give a
silently wrong selection, which is worse than rebuilding. Rebuilding is only
slower, never wrong, because the limits in this file bound every traversal.
"""

from __future__ import annotations

import hashlib
import json
import warnings
import weakref
from collections.abc import Container, Mapping, Sequence
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any, Literal

from graphql import (
    GraphQLArgument,
    GraphQLField,
    GraphQLInt,
    GraphQLNamedType,
    GraphQLNonNull,
    GraphQLObjectType,
    is_required_argument,
)

CyclePolicy = Literal["stop", "shallow", "id_only"]
"""What auto-selection does when a type appears again on its own path.

A cycle is a type that contains itself, directly or through other types, such
as `User.manager`, which is a `User` again. Auto-selection checks each path from
the top, so the same type in two sibling branches is not a cycle. The value is
one of three strings:

- `"stop"`: leave the field out.
- `"shallow"`: select the scalar fields of the repeated type and its `id`, and
  nothing nested. This is the default.
- `"id_only"`: select only the `id`, or nothing when the type has none.

Examples:
    ```python {.exec}
    from pytest_graphql import ClientConfig, SelectionPolicy

    config = ClientConfig(cycle_policy="id_only")
    assert config.selection_policy().cycle_policy == "id_only"
    assert SelectionPolicy().cycle_policy == "shallow"
    ```
"""

#: The argument that bounds how many rows a connection field returns. C1
#: names it, and a connection field without it is skipped rather than
#: expanded, because no field cap can bound the rows such a field returns.
PAGE_SIZE_ARGUMENT = "first"

#: Classes already warned about under C18. The references are weak, so the
#: registry never keeps a user's class alive and cannot hold more entries than
#: there are policy classes alive at that moment. A qualified-name registry
#: would be simpler but unbounded: subclassing is the documented extension
#: point, and a class can be built at run time, so the number of distinct
#: names a long-running session produces is not bounded by the program source.
_WARNED_CLASSES: weakref.WeakSet[type[SelectionPolicy]] = weakref.WeakSet()


class _UnserializableFieldError(Exception):
    """A subclass field the base fingerprint cannot describe."""


def _reject(value: object) -> Any:
    raise _UnserializableFieldError(type(value).__name__)


@dataclass(frozen=True)
class SelectionPolicy:
    """The rules the client follows when it chooses fields for you.

    When you call `query()` or `mutation()` without `fields=`, the client walks
    the schema from the returned type and selects fields. A `SelectionPolicy`
    sets how far it goes and what it leaves out, so that a generated query
    stays small enough to be fast and safe to run.

    A client builds its policy from the matching fields of `ClientConfig`, and
    `ClientConfig.selection_policy()` returns it. Three fields exist only here,
    and always have their defaults on a client: `connection_page_size`,
    `max_union_members` and `max_connection_depth`. In this version a client
    always builds a policy of this class. It does not use a subclass that you
    write.

    A policy never changes after it is built. Two policies with equal fields
    behave the same, and have the same `fingerprint`.

    Args:
        max_depth: How many levels of objects the client selects. The returned
            object is level 1, so `1` selects its own scalar fields and no
            nested object.
        cycle_policy: What to do when a type repeats on its own path. See
            `CyclePolicy`.
        per_type_depth_cap: A stricter limit for named types, as a mapping from
            type name to a number of levels. It counts the levels below that
            type, wherever the type appears.
        include_deprecated: Select fields that the schema marks as deprecated.
        max_fields: The most fields one query may select. This guards the size
            of the query. It does not control cost. Exceeding it raises an
            error that names the type and the ways to reduce the selection.
        exclude: Patterns for fields that are never selected. Each is
            `"Type.field"`, `"*.field"` or `"Type.*"`. Any other shape raises
            `ValueError`.
        relay_aware: Treat a type whose name ends in `Connection`, with `edges`
            and `pageInfo` fields, as a Relay connection and select it in the
            standard page shape.
        connection_page_size: The `first` value sent for a connection field.
        max_union_members: How many types of one interface or union are
            expanded. Types past the limit collapse to `__typename` and `id`.
        max_connection_depth: How many connections may be expanded inside each
            other. `1` expands a connection but not a connection inside it.

    Examples:
        ```python {.exec}
        from pytest_graphql import SelectionPolicy

        policy = SelectionPolicy(max_depth=2, exclude=["User.balance", "*.preferences"])
        assert policy.max_depth == 2
        assert policy.should_include("User", "name", ("name",), 0)
        assert not policy.should_include("User", "balance", ("balance",), 0)
        ```
    """

    max_depth: int = 3
    """How many levels of objects the client selects. `1` means scalar fields only.

    Examples:
        ```python {.exec}
        from pytest_graphql import SelectionPolicy

        assert SelectionPolicy().max_depth == 3
        assert SelectionPolicy(max_depth=1).max_depth == 1
        ```
    """
    cycle_policy: CyclePolicy = "shallow"
    """What to do when a type repeats on its own path. The default is `"shallow"`.

    Examples:
        ```python {.exec}
        from pytest_graphql import SelectionPolicy

        assert SelectionPolicy().cycle_policy == "shallow"
        assert SelectionPolicy(cycle_policy="id_only").cycle_policy == "id_only"
        ```
    """
    per_type_depth_cap: Mapping[str, int] = field(default_factory=dict)
    """Stricter depth limits by type name. Stored as a read-only mapping.

    Examples:
        ```python {.exec}
        from pytest_graphql import SelectionPolicy

        policy = SelectionPolicy(per_type_depth_cap={"User": 1})
        assert policy.per_type_depth_cap == {"User": 1}
        ```
    """
    include_deprecated: bool = False
    """Whether deprecated fields are selected. Off by default.

    Examples:
        ```python {.exec}
        from pytest_graphql import SelectionPolicy

        assert SelectionPolicy().include_deprecated is False
        ```
    """
    max_fields: int = 2000
    """The most fields one generated query may select.

    Examples:
        ```python {.exec}
        from pytest_graphql import SelectionPolicy

        assert SelectionPolicy().max_fields == 2000
        ```
    """
    exclude: Sequence[str] = ()
    """Patterns for fields that are never selected. Stored as a tuple.

    Examples:
        ```python {.exec}
        from pytest_graphql import SelectionPolicy

        policy = SelectionPolicy(exclude=["User.balance"])
        assert policy.exclude == ("User.balance",)

        try:
            SelectionPolicy(exclude=["balance"])
        except ValueError:
            pass
        else:
            raise AssertionError("expected an error")
        ```
    """
    relay_aware: bool = True
    """Whether Relay connection types are selected in the standard page shape.

    Examples:
        ```python {.exec}
        from pytest_graphql import SelectionPolicy

        assert SelectionPolicy().relay_aware is True
        ```
    """
    connection_page_size: int = 10
    """The `first` value that the client sends for a connection field.

    Examples:
        ```python {.exec}
        from pytest_graphql import SelectionPolicy

        assert SelectionPolicy().connection_page_size == 10
        assert SelectionPolicy(connection_page_size=25).connection_page_size == 25
        ```
    """
    max_union_members: int = 10
    """The most types of one interface or union that are expanded.

    Examples:
        ```python {.exec}
        from pytest_graphql import SelectionPolicy

        assert SelectionPolicy().max_union_members == 10
        ```
    """
    max_connection_depth: int = 1
    """How many connections may be expanded inside each other. The default is `1`.

    Examples:
        ```python {.exec}
        from pytest_graphql import SelectionPolicy

        assert SelectionPolicy().max_connection_depth == 1
        ```
    """

    #: Decided from the class under C18, never passed in. Fields that are not
    #: constructor inputs stay out of the fingerprint, which describes inputs.
    memoizable: bool = field(init=False, repr=False, compare=False, default=True)
    """Whether the client may reuse a selection built under this policy.

    It is decided from the class, and cannot be passed in. It is `False` for a
    subclass that overrides `should_include()` and not `fingerprint`, because
    the client cannot tell when such a policy would decide differently.

    Examples:
        ```python {.exec}
        import warnings

        from pytest_graphql import SelectionPolicy


        class NoSecrets(SelectionPolicy):
            def should_include(self, parent_type, field_name, path, depth):
                return field_name != "secret"


        assert SelectionPolicy().memoizable is True
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert NoSecrets().memoizable is False
        ```
    """

    def __post_init__(self) -> None:
        # A caller keeps a reference to whatever it passed in, so copying
        # here is what makes the fingerprint describe this policy for as
        # long as it lives.
        object.__setattr__(
            self, "per_type_depth_cap", MappingProxyType(dict(self.per_type_depth_cap))
        )
        object.__setattr__(self, "exclude", tuple(self.exclude))
        for pattern in self.exclude:
            _check_exclude_pattern(pattern)
        object.__setattr__(self, "memoizable", self._decide_memoization())

    def _decide_memoization(self) -> bool:
        """Apply C18, and warn once per class when caching is switched off."""
        cls = type(self)
        if cls.should_include is SelectionPolicy.should_include:
            return True
        if cls.fingerprint is not SelectionPolicy.fingerprint:
            return True
        if cls not in _WARNED_CLASSES:
            _WARNED_CLASSES.add(cls)
            warnings.warn(
                f"{cls.__name__} overrides should_include() without overriding "
                "fingerprint, so its selections are never cached and are "
                "rebuilt on every call. Override fingerprint to opt back in; "
                "the value must cover every input the policy's decisions "
                "depend on, including external state. See the extending guide.",
                stacklevel=4,
            )
        return False

    @property
    def fingerprint(self) -> str:
        """A stable text key that covers every field of this policy.

        The client reuses a selection it already built when the policy has the
        same fingerprint. Two policies with equal fields have the same
        fingerprint, even when they are different classes.

        A subclass whose decisions depend on more than its fields, for example
        on state outside the object, must override this property and return a
        value that covers that state too.

        Examples:
            ```python {.exec}
            from pytest_graphql import SelectionPolicy

            assert SelectionPolicy(max_depth=2).fingerprint == (
                SelectionPolicy(max_depth=2).fingerprint
            )
            assert SelectionPolicy(max_depth=2).fingerprint != (
                SelectionPolicy(max_depth=3).fingerprint
            )
            ```
        """
        payload: dict[str, Any] = {}
        for declared in fields(self):
            if not declared.init:
                continue
            value = getattr(self, declared.name)
            if isinstance(value, Mapping):
                payload[declared.name] = {str(key): value[key] for key in sorted(value)}
            elif isinstance(value, (list, tuple)):
                payload[declared.name] = list(value)
            else:
                payload[declared.name] = value
        try:
            encoded = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), default=_reject
            )
        except _UnserializableFieldError as error:
            raise TypeError(
                f"{type(self).__name__} has a field of type {error.args[0]} "
                "that the base fingerprint cannot describe. Override "
                "fingerprint on this class and return a value that covers it."
            ) from error
        return hashlib.sha256(encoded.encode()).hexdigest()

    @property
    def cache_key(self) -> str | None:
        """The key the client uses to reuse selections, or `None` for no reuse.

        It is the `fingerprint`, except for a policy whose `memoizable` is
        `False`, which is never reused.

        Examples:
            ```python {.exec}
            from pytest_graphql import SelectionPolicy

            policy = SelectionPolicy()
            assert policy.cache_key == policy.fingerprint
            ```
        """
        return self.fingerprint if self.memoizable else None

    def should_include(
        self,
        parent_type: str,
        field_name: str,
        path: tuple[str, ...],  # noqa: ARG002 -- part of the overridable contract
        depth: int,  # noqa: ARG002 -- part of the overridable contract
    ) -> bool:
        """Decide whether the client may select one field.

        The client asks this for each field it considers. The default answer is
        `False` for a field that matches an `exclude` pattern, and `True` for
        every other field. A subclass can override it to decide by position. The
        default ignores `path` and `depth`. A client does not use a subclass yet,
        so call it on a policy that you build when you test your own rules.

        Positions are measured from the start of the generated part of a query,
        not from the start of the whole document.

        Args:
            parent_type: The name of the type that has the field.
            field_name: The name of the field.
            path: The field names from the start of the generated part down to
                this field, including it.
            depth: How many levels of objects lie above this field. Fields at
                the start of the generated part have depth `0`.

        Returns:
            `True` to allow the field.

        Examples:
            ```python {.exec}
            from pytest_graphql import SelectionPolicy

            policy = SelectionPolicy(exclude=["User.*"])
            assert not policy.should_include("User", "name", ("name",), 0)
            assert policy.should_include("Team", "name", ("name",), 0)
            ```
        """
        return not any(
            _matches_exclude(pattern, parent_type, field_name)
            for pattern in self.exclude
        )

    def depth_cap_for(self, type_name: str) -> int | None:
        """The depth limit set for one type, or `None` when it has none.

        Args:
            type_name: The name of a schema type.

        Returns:
            The limit from `per_type_depth_cap`, if any.

        Examples:
            ```python {.exec}
            from pytest_graphql import SelectionPolicy

            policy = SelectionPolicy(per_type_depth_cap={"User": 1})
            assert policy.depth_cap_for("User") == 1
            assert policy.depth_cap_for("Team") is None
            ```
        """
        return self.per_type_depth_cap.get(type_name)


def _check_exclude_pattern(pattern: str) -> None:
    """Reject a pattern that can never match, instead of ignoring it.

    The three documented shapes are ``Type.field``, ``*.field`` and
    ``Type.*``. A pattern in any other shape is a typo that would otherwise
    remove nothing and say nothing.
    """
    type_part, separator, field_part = pattern.partition(".")
    if not separator or not type_part or not field_part or "." in field_part:
        raise ValueError(
            f"exclude pattern {pattern!r} is not one of Type.field, *.field or Type.* ."
        )


def _matches_exclude(pattern: str, type_name: str, field_name: str) -> bool:
    type_part, _, field_part = pattern.partition(".")
    if type_part not in ("*", type_name):
        return False
    return field_part in ("*", field_name)


def is_connection_type(type_: GraphQLNamedType) -> bool:
    """Whether a type is a Relay connection, by SPEC 5.4 rule 4.

    The rule is name-based and shape-based together: the name ends in
    ``Connection`` and the type has both ``edges`` and ``pageInfo``. The
    calibration gate reports whether this heuristic misfires on the corpus
    schemas.
    """
    if not isinstance(type_, GraphQLObjectType):
        return False
    if not type_.name.endswith("Connection"):
        return False
    return "edges" in type_.fields and "pageInfo" in type_.fields


def page_size_argument(field_: GraphQLField) -> GraphQLArgument | None:
    """The connection page-size argument of a field, when it accepts one.

    C1 makes this argument the condition for expanding a connection field at
    all, so the check is about the field and not about the type it returns.

    Only ``Int`` and ``Int!`` count. The engine sends the page size as one
    shared variable of a single declared type, so a shape that variable is not
    valid at, such as a list of integers, is not a page-size argument. Such a
    field is then skipped or refused, rather than expanded into a document
    that cannot pass validation.
    """
    argument = field_.args.get(PAGE_SIZE_ARGUMENT)
    if argument is None:
        return None
    named: Any = argument.type
    if isinstance(named, GraphQLNonNull):
        named = named.of_type
    return argument if named is GraphQLInt else None


def missing_required_arguments(
    field_: GraphQLField, supplied: Container[str] = ()
) -> tuple[str, ...]:
    """Required arguments of a field for which no value was supplied (A6).

    A6 widened SPEC 5.4 rule 3 from composite fields to every field: a scalar
    field with a required argument is just as invalid when emitted without
    one. An argument is required when its type is non-null and it declares no
    default.
    """
    return tuple(
        name
        for name, argument in field_.args.items()
        if is_required_argument(argument) and name not in supplied
    )
