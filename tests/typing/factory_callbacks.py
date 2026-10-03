"""C42: the factory's public callback signature passes the strict check as written.

``ScalarSpec.fake`` takes a ``DeterministicRandom``. A callback written against
the standard ``random.Random`` must fail the strict check where it is passed,
because a value drawn from it would not be reproducible across Python versions.

This file is checked by ``tests/typing/test_typing_fixtures.py`` and never
run.
"""

from __future__ import annotations

import random

from pytest_graphql import DeterministicRandom, ScalarRegistry, ScalarSpec, unique


def _fake(rng: DeterministicRandom) -> int:
    reveal_type(rng.below(9))  # reveal: int
    return rng.below(9)


def _stdlib(rng: random.Random) -> int:
    return rng.randint(0, 9)


good = ScalarSpec(name="Money", serialize=str, fake=_fake)
inferred = ScalarSpec(name="Stamp", serialize=str, fake=lambda rng: rng.bits(8))
decoded = ScalarSpec(name="Cents", serialize=str, fake=_fake, parse=int)
bad = ScalarSpec(name="Money", serialize=str, fake=_stdlib)  # expect: arg-type

registry = ScalarRegistry([good])
registry.register(inferred)
reveal_type(registry.get("Money"))  # reveal: ScalarSpec | None
reveal_type(unique("email"))  # reveal: Unique
