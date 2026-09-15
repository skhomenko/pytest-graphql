"""``SelectionPolicy`` (B8), its excludes, and the relay and size-guard rules.

The policy is one frozen dataclass, not a protocol: B8 collapsed the
specification's two readings into a single type with one overridable hook.
The defaults are SPEC 3.10 as corrected by C1, which turned
``include_deprecated`` off and added the three cost limits
(``connection_page_size``, ``max_union_members`` and ``max_connection_depth``).
Every one of these numbers is a candidate for the calibration gate, which
measures them against a corpus of real schemas. None of them may be changed
here to make a test pass.

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
    Undefined,
)

CyclePolicy = Literal["stop", "shallow", "id_only"]

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
    """How auto-selection walks a schema, and what it is allowed to cost.

    ``max_fields`` is a document-size guard and not a cost control (C1). The
    cost controls are ``include_deprecated``, the connection page size, and
    the two width and nesting caps.
    """

    max_depth: int = 3
    cycle_policy: CyclePolicy = "shallow"
    per_type_depth_cap: Mapping[str, int] = field(default_factory=dict)
    include_deprecated: bool = False
    max_fields: int = 2000
    exclude: Sequence[str] = ()
    relay_aware: bool = True
    connection_page_size: int = 10
    max_union_members: int = 10
    max_connection_depth: int = 1

    #: Decided from the class under C18, never passed in. Fields that are not
    #: constructor inputs stay out of the fingerprint, which describes inputs.
    memoizable: bool = field(init=False, repr=False, compare=False, default=True)

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
        """A stable key covering every constructor field of this policy.

        Two policies with equal fields behave identically, so the class name
        is deliberately not part of this value: a subclass that changes no
        behaviour should share the base class's cache entries.
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
        """The memoization key, or ``None`` when this policy is never cached.

        Every caller asks this rather than reading ``fingerprint`` directly,
        so the C18 rule is applied in exactly one place.
        """
        return self.fingerprint if self.memoizable else None

    def should_include(
        self,
        parent_type: str,
        field_name: str,
        path: tuple[str, ...],  # noqa: ARG002 -- part of the overridable contract
        depth: int,  # noqa: ARG002 -- part of the overridable contract
    ) -> bool:
        """Whether one field is selectable. The extension point (B8).

        The base implementation applies the ``exclude`` patterns. ``path``
        and ``depth`` are unused here and are passed so a subclass can decide
        by position, which is the reason the hook exists.
        """
        return not any(
            _matches_exclude(pattern, parent_type, field_name)
            for pattern in self.exclude
        )

    def depth_cap_for(self, type_name: str) -> int | None:
        """The stricter per-type cap for a named type, when it has one."""
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
    calibration gate reports whether this heuristic misfires on real schemas.
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
        if isinstance(argument.type, GraphQLNonNull)
        and argument.default_value is Undefined
        and name not in supplied
    )
