"""``unique()``: values that differ on every call (C10).

A seeded value repeats on every run of a test, so a test that writes it to a
database with a unique constraint collides on its second run. ``unique()`` is
the opt-out, chosen per field. Reproducible and unique are mutually exclusive,
and the caller picks which one each field needs.

``unique()`` itself returns a marker. The factory replaces each marker with a
value once it knows the run id, the worker id, the node id and the field path,
none of which a bare ``unique()`` call can know.

A value is derived from the run id, the worker id (``"main"`` outside xdist),
the node id, the field path and a counter, so it is unique across runs and
across workers and can be traced back to the run that made it.

Bounded state. The counter is one integer for the whole source. It is not kept
per node id and field path, because that table would gain a row for every test
and field and never release one. One counter still separates repeated calls in
a test and repeats of one node id in a process, such as a rerun plugin
produces, where a counter that reset per test would repeat a value.

Byte format. The value is
``"u" + sha256(b"pytest-graphql/unique/v1\\0" + encode_parts(run_id, worker_id,
node_id, str(counter), *path_segments)).digest()[:8].hex()``, and an ``"email"``
value adds ``"@example.com"``. ``encode_parts`` is the length-prefixed encoding
of ``rng.py``, so no two inputs encode alike. ``example.com`` is reserved for
documentation by RFC 2606, so a generated address can never reach a real
mailbox.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pytest_graphql._core.factory.rng import encode_parts

DOMAIN = b"pytest-graphql/unique/v1\0"
EMAIL_DOMAIN = "example.com"

#: The kinds ``unique()`` accepts. ``None`` and ``"string"`` are the same.
KINDS = ("string", "email")


def _check_kind(kind: object) -> None:
    if kind is None or kind in KINDS:
        return
    detail = f"got {kind!r}" if isinstance(kind, str) else f"got {type(kind).__name__}"
    error = ValueError if isinstance(kind, str) else TypeError
    raise error(
        f"unique() kind must be None, 'string' or 'email'; {detail}.\n"
        "  unique() makes a plain token by default and an email address "
        "with unique('email')."
    )


@dataclass(frozen=True)
class Unique:
    """A placeholder the factory replaces with a value that differs per call."""

    kind: str | None = None


def unique(kind: str | None = None) -> Unique:
    """Mark a field value that must differ on every call and every run.

    ``unique()`` and ``unique("string")`` give a short token. ``unique("email")``
    gives an address on the reserved domain ``example.com``.
    """
    _check_kind(kind)
    return Unique(kind)


class UniqueSource:
    """Issues unique values for one process, with state of fixed size."""

    __slots__ = ("_issued", "_lock", "run_id", "worker_id")

    def __init__(self, run_id: str, worker_id: str = "main") -> None:
        for label, value in (("run_id", run_id), ("worker_id", worker_id)):
            if not isinstance(value, str):
                raise TypeError(f"{label} must be a str, got {type(value).__name__}.")
            if not value:
                raise ValueError(f"{label} must not be empty.")
        self.run_id = run_id
        self.worker_id = worker_id
        self._issued = 0
        self._lock = threading.Lock()

    @property
    def issued(self) -> int:
        """How many values this source has issued."""
        return self._issued

    def issue(self, kind: str | None, node_id: str, path: Sequence[str | int]) -> str:
        """A value for ``kind`` at ``path`` in the test ``node_id``."""
        _check_kind(kind)
        with self._lock:
            counter = self._issued
            self._issued = counter + 1
        digest = hashlib.sha256(
            DOMAIN
            + encode_parts(
                self.run_id,
                self.worker_id,
                node_id,
                str(counter),
                *(str(segment) for segment in path),
            )
        ).digest()
        token = "u" + digest[:8].hex()
        return f"{token}@{EMAIL_DOMAIN}" if kind == "email" else token


def resolve(
    value: Any, source: UniqueSource, node_id: str, path: Sequence[str | int]
) -> Any:
    """``value`` with every marker inside it replaced by an issued value.

    Dicts, lists and tuples are walked, and a marker takes the path of the key
    or index that holds it. A container with no marker inside is returned as
    the same object, so an override the caller passed in is never copied.
    """
    if isinstance(value, Unique):
        return source.issue(value.kind, node_id, path)
    if isinstance(value, dict):
        resolved = {
            key: resolve(item, source, node_id, (*path, str(key)))
            for key, item in value.items()
        }
        if all(resolved[key] is value[key] for key in value):
            return value
        return resolved
    if isinstance(value, (list, tuple)):
        items = [
            resolve(item, source, node_id, (*path, index))
            for index, item in enumerate(value)
        ]
        if all(new is old for new, old in zip(items, value, strict=True)):
            return value
        return tuple(items) if isinstance(value, tuple) else items
    return value
