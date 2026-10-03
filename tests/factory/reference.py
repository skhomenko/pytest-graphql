"""An independent reference for the factory's byte formats.

This module restates the formats in ``src/pytest_graphql/_core/factory/rng.py``
and ``seed.py`` using only ``hashlib`` and integer arithmetic, written from the
documented byte layout and sharing no code with the package. A test that
compares the package against this module fails when either one drifts, which a
test that compared the package against its own output could not do.

The formats are part of the determinism promise (``DESIGN_DECISIONS.md``
section 4), so a change to either side is a versioned change.
"""

from __future__ import annotations

import hashlib

DOMAIN = b"pytest-graphql/rng/v1\0"
DEFAULT_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def seed_for(global_seed: int, node_id: str) -> int:
    """The B1 formula, written out exactly as the design states it."""
    text = f"{global_seed}\0{node_id}"
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")


def encode_parts(*parts: str) -> bytes:
    """Each part as an 8 byte big-endian length, then its UTF-8 bytes."""
    out = b""
    for part in parts:
        data = part.encode("utf-8", "surrogatepass")
        out += len(data).to_bytes(8, "big") + data
    return out


class ReferenceStream:
    """SHA-256 in counter mode, read as one byte stream."""

    def __init__(self, seed: int, *path: str) -> None:
        self._key = hashlib.sha256(
            DOMAIN + seed.to_bytes(8, "big") + encode_parts(*path)
        ).digest()
        self._counter = 0
        self._pending = b""

    def take(self, count: int) -> bytes:
        while len(self._pending) < count:
            block = hashlib.sha256(
                self._key + self._counter.to_bytes(8, "big")
            ).digest()
            self._counter += 1
            self._pending += block
        out, self._pending = self._pending[:count], self._pending[count:]
        return out

    def bits(self, width: int) -> int:
        size = (width + 7) // 8
        return int.from_bytes(self.take(size), "big") >> (8 * size - width)

    def below(self, bound: int) -> int:
        width = (bound - 1).bit_length()
        if width == 0:
            return 0
        while True:
            candidate = self.bits(width)
            if candidate < bound:
                return candidate

    def float_unit(self) -> float:
        return self.bits(53) / 2**53

    def sample_string(self, length: int, alphabet: str = DEFAULT_ALPHABET) -> str:
        return "".join(alphabet[self.below(len(alphabet))] for _ in range(length))
