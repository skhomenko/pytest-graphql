"""The deterministic factory (M7): seeds, the sampler, scalars and ``gql.fake``.

Everything here is a pure function of explicit inputs. The pytest layer, and
any other caller, supplies the global seed, the node id, the run id and the
worker id. This package imports no pytest.
"""

from __future__ import annotations

from pytest_graphql._core.factory.context import FakeContext
from pytest_graphql._core.factory.namespace import FakeNamespace, TypeFactory
from pytest_graphql._core.factory.rng import DEFAULT_ALPHABET, DeterministicRandom
from pytest_graphql._core.factory.scalars import (
    BUILTIN_FAKES,
    ScalarRegistry,
    ScalarSpec,
)
from pytest_graphql._core.factory.seed import derive_seed
from pytest_graphql._core.factory.unique import Unique, UniqueSource, unique

__all__ = [
    "BUILTIN_FAKES",
    "DEFAULT_ALPHABET",
    "DeterministicRandom",
    "FakeContext",
    "FakeNamespace",
    "ScalarRegistry",
    "ScalarSpec",
    "TypeFactory",
    "Unique",
    "UniqueSource",
    "derive_seed",
    "unique",
]
