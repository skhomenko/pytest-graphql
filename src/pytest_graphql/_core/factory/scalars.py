"""``ScalarSpec``, ``ScalarRegistry`` and the built-in scalar fakes (B7, C42).

One spec type describes a custom scalar for every use the package makes of it:
``fake`` builds a factory value, ``serialize`` turns a Python value into JSON
for variables, and ``parse`` turns JSON into a Python value for responses. The
default ``parse=None`` leaves the raw JSON in place, so the raw response is
never lost.

``ScalarRegistry`` is the one place specs are kept. Response decoding reads it
through :meth:`ScalarRegistry.parsers`, which is the ``name -> parse`` mapping
the response materializer already accepts, so no second registry exists.

The five scalars the GraphQL specification defines are not custom, are never
registered, and take their fake from ``BUILTIN_FAKES``. Their values are part
of the determinism promise and are pinned by ``tests/factory/golden.json``:

- ``String``: 8 characters from ``DEFAULT_ALPHABET``.
- ``ID``: 12 lowercase hexadecimal characters.
- ``Int``: an integer from 1 to 1000, inside the signed 32 bit range GraphQL
  allows and positive, so a field that expects a count or a size accepts it.
- ``Float``: a count from 0 to 99999 divided by 100, which is one correctly
  rounded division, so the value never depends on how a version prints a float.
- ``Boolean``: one draw of ``below(2)``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pytest_graphql._core.factory.rng import DeterministicRandom

_HEX = "0123456789abcdef"

#: The fake of each scalar the GraphQL specification defines.
BUILTIN_FAKES: Mapping[str, Callable[[DeterministicRandom], Any]] = MappingProxyType(
    {
        "String": lambda rng: rng.sample_string(8),
        "ID": lambda rng: rng.sample_string(12, _HEX),
        "Int": lambda rng: 1 + rng.below(1000),
        "Float": lambda rng: rng.below(100_000) / 100,
        "Boolean": lambda rng: rng.below(2) == 1,
    }
)

_GRAPHQL_NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")


@dataclass(frozen=True)
class ScalarSpec:
    """How to read, write and generate one custom scalar.

    A custom scalar is a scalar type that your schema defines, such as
    `DateTime` or `Money`. On the wire it is plain JSON. A `ScalarSpec` tells the
    client how to turn that JSON into a Python value, how to turn a Python value
    back into JSON for a variable, and how to make a test value. Register it in
    the client's `ScalarRegistry`, which you reach as `gql.scalars`.

    The five scalars that GraphQL itself defines (`String`, `ID`, `Int`,
    `Float` and `Boolean`) are handled by the package. A spec cannot use one of
    those names.

    The spec checks itself when you build it. The name must be a valid GraphQL
    name that does not start with two underscores. `serialize`, `fake` and a
    given `parse` must be callable.

    Args:
        name: The scalar's name in the schema.
        serialize: Turns a Python value into the JSON value that is sent. It
            runs on every variable of this type. The result must be JSON: `None`,
            a bool, an int, a finite float, a string, a list or tuple, or a
            `dict` with string keys. Anything else raises an error before the
            request is sent.
        fake: Makes a test value for `gql.fake`. It receives a
            `DeterministicRandom`, never `random.Random`, so the same seed gives
            the same value. Return a Python value. `gql.fake` does not call
            `serialize`, so you can edit a payload as Python values.
        parse: Turns the JSON value of a response into a Python value. Leave it
            as `None` to keep the raw JSON value in responses.

    Examples:
        ```python {.exec}
        import datetime

        from pytest_graphql import ScalarSpec

        spec = ScalarSpec(
            name="DateTime",
            serialize=lambda value: value.isoformat(),
            fake=lambda rng: datetime.datetime(
                2020, 1, 1 + rng.below(28), tzinfo=datetime.timezone.utc
            ),
            parse=datetime.datetime.fromisoformat,
        )
        gql.scalars.register(spec)

        user = gql.query("user", id="u1")
        assert user.joined_at == datetime.datetime(
            2020, 1, 1, tzinfo=datetime.timezone.utc
        )
        ```
    """

    name: str
    """The scalar's name in the schema.

    Examples:
        ```python {.exec}
        from pytest_graphql import DeterministicRandom, ScalarSpec

        spec = ScalarSpec(
            name="Money", serialize=str, fake=lambda rng: "1.00", parse=int
        )
        assert spec.name == "Money"
        ```
    """
    serialize: Callable[[Any], Any]
    """Turns a Python value into the JSON value that is sent in a variable.

    Examples:
        ```python {.exec}
        from pytest_graphql import DeterministicRandom, ScalarSpec

        spec = ScalarSpec(
            name="Money", serialize=str, fake=lambda rng: "1.00", parse=int
        )
        assert spec.serialize(5) == "5"
        ```
    """
    fake: Callable[[DeterministicRandom], Any]
    """Makes a test value from a `DeterministicRandom`.

    Examples:
        ```python {.exec}
        from pytest_graphql import DeterministicRandom, ScalarSpec

        spec = ScalarSpec(
            name="Money", serialize=str, fake=lambda rng: "1.00", parse=int
        )
        assert spec.fake(DeterministicRandom(1, "Pay", "amount")) == "1.00"
        ```
    """
    parse: Callable[[Any], Any] | None = None
    """Turns a JSON value from a response into a Python value, or `None` for none.

    Examples:
        ```python {.exec}
        from pytest_graphql import DeterministicRandom, ScalarSpec

        spec = ScalarSpec(
            name="Money", serialize=str, fake=lambda rng: "1.00", parse=int
        )
        assert spec.parse("5") == 5
        assert ScalarSpec(name="Date", serialize=str, fake=str).parse is None
        ```
    """

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError(
                f"a scalar name must be a str, got {type(self.name).__name__}."
            )
        if not _GRAPHQL_NAME.fullmatch(self.name) or self.name.startswith("__"):
            raise ValueError(
                f"{self.name!r} is not a GraphQL name.\n"
                "  A name starts with a letter or an underscore, followed by "
                "letters, digits or underscores, and never starts with '__'."
            )
        if self.name in BUILTIN_FAKES:
            raise ValueError(
                f"{self.name!r} is a built-in scalar and cannot be registered.\n"
                "  Built-in scalars are generated by the package. Register a "
                "custom scalar, or pass a value for the field."
            )
        for label, value in (("serialize", self.serialize), ("fake", self.fake)):
            if not callable(value):
                raise TypeError(
                    f"ScalarSpec {label} must be callable, got {type(value).__name__}."
                )
        if self.parse is not None and not callable(self.parse):
            raise TypeError(
                "ScalarSpec parse must be callable or None, got "
                f"{type(self.parse).__name__}."
            )


class ScalarRegistry:
    """The custom scalars that a client knows, kept by name.

    A client has one registry, available as `gql.scalars`. The same registry
    decodes responses, serializes variables and feeds `gql.fake`. It is read
    again for every call, so a scalar that you register at any time takes effect
    on the next one. A clone made with `with_headers()` or `as_()` shares its
    parent's registry.

    Iteration gives the registered names in sorted order. `in` tests a name, and
    `len()` counts the specs.

    Args:
        specs: Specs to register at once. A name that appears twice raises
            `ValueError`.

    Examples:
        ```python {.exec}
        from pytest_graphql import ScalarRegistry, ScalarSpec

        registry = ScalarRegistry(
            [ScalarSpec(name="Money", serialize=str, fake=lambda rng: "1.00")]
        )
        assert "Money" in registry
        assert list(registry) == ["Money"]
        assert len(registry) == 1
        ```
    """

    __slots__ = ("_specs",)

    def __init__(self, specs: Iterable[ScalarSpec] = ()) -> None:
        self._specs: dict[str, ScalarSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ScalarSpec, *, replace: bool = False) -> None:
        """Add a scalar spec to the registry.

        Two registrations of one name usually mean that two plugins disagree,
        so the second one is an error unless you say that you mean to replace.

        Args:
            spec: The spec to add.
            replace: Replace a spec that has the same name. Without it, a name
                that is already registered raises an error.

        Raises:
            TypeError: When `spec` is not a `ScalarSpec`.
            ValueError: When the name is registered and `replace` is false.

        Examples:
            ```python {.exec}
            from pytest_graphql import ScalarRegistry, ScalarSpec

            registry = ScalarRegistry()
            registry.register(ScalarSpec(name="Money", serialize=str, fake=str))
            registry.register(
                ScalarSpec(name="Money", serialize=repr, fake=str), replace=True
            )
            assert registry.get("Money").serialize is repr
            ```
        """
        if not isinstance(spec, ScalarSpec):
            raise TypeError(
                f"register() needs a ScalarSpec, got {type(spec).__name__}."
            )
        if spec.name in self._specs and not replace:
            raise ValueError(
                f"scalar {spec.name!r} is already registered.\n"
                "  Pass replace=True to swap it for this one."
            )
        self._specs[spec.name] = spec

    def get(self, name: str) -> ScalarSpec | None:
        """Return the spec registered under a name.

        Args:
            name: The scalar's name.

        Returns:
            The spec, or `None` when the name is not registered.

        Examples:
            ```python {.exec}
            from pytest_graphql import ScalarRegistry, ScalarSpec

            spec = ScalarSpec(name="Money", serialize=str, fake=str)
            registry = ScalarRegistry([spec])
            assert registry.get("Money") is spec
            assert registry.get("Date") is None
            ```
        """
        return self._specs.get(name)

    def parsers(self) -> dict[str, Callable[[Any], Any]]:
        """Return the parse function of every spec that has one, by name.

        This is a snapshot. It does not change when you register more specs. The
        client takes a new snapshot for each response.

        Returns:
            A new `dict` from scalar name to its `parse` function.

        Examples:
            ```python {.exec}
            from pytest_graphql import ScalarRegistry, ScalarSpec

            registry = ScalarRegistry(
                [
                    ScalarSpec(name="Money", serialize=str, fake=str),
                    ScalarSpec(name="Count", serialize=int, fake=int, parse=int),
                ]
            )
            assert registry.parsers() == {"Count": int}
            ```
        """
        return {
            name: spec.parse
            for name, spec in self._specs.items()
            if spec.parse is not None
        }

    def __contains__(self, name: object) -> bool:
        """Tell whether a name is registered.

        Examples:
            ```python {.exec}
            assert "DateTime" not in gql.scalars
            ```
        """
        return name in self._specs

    def __iter__(self) -> Iterator[str]:
        """Iterate over the registered names in sorted order.

        Examples:
            ```python {.exec}
            from pytest_graphql import ScalarRegistry, ScalarSpec

            registry = ScalarRegistry()
            for name in ("Money", "Date"):
                registry.register(ScalarSpec(name=name, serialize=str, fake=str))
            assert list(registry) == ["Date", "Money"]
            ```
        """
        return iter(sorted(self._specs))

    def __len__(self) -> int:
        """Count the registered specs.

        Examples:
            ```python {.exec}
            assert len(gql.scalars) == 0
            ```
        """
        return len(self._specs)

    def __repr__(self) -> str:
        return f"ScalarRegistry({', '.join(self)})"
