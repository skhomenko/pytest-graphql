"""The golden vectors: what is stored, how it is rendered and how it is written.

``tests/factory/golden.json`` holds the output of every built-in scalar, the
sampler, the seed formula, ``unique()`` and two nested input objects, all at
fixed inputs. ``test_golden.py`` renders the same structure from the package
and compares the text with the file byte for byte, on every supported Python.

A changed value is a versioned change. It must be deliberate, recorded in
``CHANGELOG.md`` under the release that ships it, and the ``FORMAT`` below is
raised with it. The file is never regenerated to make a failing test pass: the
writer below refuses to overwrite an existing file unless it is told the
change is deliberate.

Floats are stored as ``float.hex()`` text, which is exact and does not depend
on how any Python version prints a float.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pytest_graphql._core.factory import (
    BUILTIN_FAKES,
    DeterministicRandom,
    ScalarRegistry,
    ScalarSpec,
    UniqueSource,
    derive_seed,
    unique,
)
from tests.factory.schemas import namespace

GOLDEN_PATH = Path(__file__).with_name("golden.json")

#: Raised when any stored value changes on purpose.
FORMAT = 1

GLOBAL_SEED = 20260101
NODE_ID = "tests/factory/test_golden.py::test_vector[param-1]"
RUN_ID = "run-golden"
WORKER_ID = "gw3"


def _encode(value: Any) -> Any:
    """A JSON-safe copy of ``value`` with floats written as exact hex text."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return {"float": value.hex()}
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    raise TypeError(f"golden vectors cannot store {type(value).__name__}")


def _rng(*path: str) -> DeterministicRandom:
    return DeterministicRandom(derive_seed(GLOBAL_SEED, NODE_ID), *path)


def _registry() -> ScalarRegistry:
    return ScalarRegistry(
        [
            ScalarSpec(
                name="Money",
                serialize=str,
                fake=lambda rng: rng.sample_string(6, "0123456789"),
            ),
            ScalarSpec(name="Stamp", serialize=str, fake=lambda rng: rng.bits(32)),
        ]
    )


def build() -> dict[str, Any]:
    """Every golden value, computed from the package."""
    sampler = _rng("sampler")
    below_rng = _rng("below")
    seeds = [
        {"global_seed": seed, "node_id": node, "derived": derive_seed(seed, node)}
        for seed, node in (
            (0, "tests/test_a.py::test_one"),
            (42, "tests/test_a.py::test_one"),
            (0, ""),
            (-1, "x"),
            (7, "tests/ünï.py::t[ä-1]"),
        )
    ]
    scalars = {
        name: fake(_rng("scalar", name)) for name, fake in sorted(BUILTIN_FAKES.items())
    }
    fake = namespace(
        seed=GLOBAL_SEED,
        node_id=NODE_ID,
        scalars=_registry(),
        worker_id=WORKER_ID,
        run_id=RUN_ID,
    )
    source = UniqueSource(RUN_ID, WORKER_ID)
    return {
        "format": FORMAT,
        "seeds": seeds,
        "sampler": {
            "bits_7": sampler.bits(7),
            "bits_64": sampler.bits(64),
            "bits_200": sampler.bits(200),
            "float_unit": [sampler.float_unit() for _ in range(3)],
            "sample_string_8": sampler.sample_string(8),
            "sample_string_ab": sampler.sample_string(12, "ab"),
            "choice": sampler.choice(["a", "b", "c", "d", "e"]),
            "below_1": [below_rng.below(1) for _ in range(3)],
            "below_3": [below_rng.below(3) for _ in range(12)],
            "below_5": [below_rng.below(5) for _ in range(12)],
            "below_256": [below_rng.below(256) for _ in range(6)],
            "below_1000": [below_rng.below(1000) for _ in range(6)],
            "below_2_70": [below_rng.below(2**70 + 1) for _ in range(3)],
        },
        "scalars": scalars,
        "inputs": {
            "CreateUserInput": fake.CreateUserInput(),
            "CreateUserInput_required_only": fake.CreateUserInput(_required_only=True),
            "TreeInput_depth_1": fake.TreeInput(_depth=1),
            "StampedInput": fake.StampedInput(),
            "PricedInput": fake.PricedInput(),
        },
        "unique": {
            "string": source.issue(None, NODE_ID, ("CreateUserInput", "name")),
            "email": source.issue("email", NODE_ID, ("CreateUserInput", "email")),
            "next": source.issue(None, NODE_ID, ("CreateUserInput", "name")),
            "via_factory": fake.CreateUserInput(
                name=unique(), nickname=unique("email")
            )["name"],
        },
    }


def render() -> str:
    """The canonical text of the golden file."""
    return (
        json.dumps(_encode(build()), indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    )


def main(argv: list[str]) -> int:
    text = render()
    if GOLDEN_PATH.exists() and "--deliberate-version-change" not in argv:
        print(
            f"{GOLDEN_PATH.name} exists. A changed golden value is a versioned "
            "change. Raise FORMAT, record it in CHANGELOG.md, and pass "
            "--deliberate-version-change.",
            file=sys.stderr,
        )
        return 1
    GOLDEN_PATH.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
