"""``FakeContext``: the per-test inputs a client hands to ``gql.fake``.

The global seed is configuration and lives on ``ClientConfig``. These two
values are objects of the run, so they belong to the client constructor
(B4, "Constructor and configuration split"):

- ``node_id`` names the test the data is for, so each test gets its own data.
- ``unique_source`` carries the run id and the worker id and the one counter
  behind ``unique()``. Clones of a client share it, because two sources with
  the same run id and worker id would each count from zero and repeat values.

A caller outside pytest gets :meth:`standalone`: a fixed node id, so seeded
data repeats between runs, and a fresh random run id, so ``unique()`` values do
not.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from pytest_graphql._core.factory.unique import UniqueSource

STANDALONE_NODE_ID = "standalone"


@dataclass(frozen=True)
class FakeContext:
    """The test a client makes data for, and the source of its unique values."""

    node_id: str
    unique_source: UniqueSource

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str):
            raise TypeError(
                f"node_id must be a str, got {type(self.node_id).__name__}."
            )
        if not isinstance(self.unique_source, UniqueSource):
            raise TypeError(
                "unique_source must be a UniqueSource, got "
                f"{type(self.unique_source).__name__}."
            )

    @classmethod
    def standalone(cls) -> FakeContext:
        """A context for a client built outside pytest."""
        return cls(STANDALONE_NODE_ID, UniqueSource(uuid.uuid4().hex))
