"""``DeterministicRandom``: a sampler the package owns (C10, C42).

``random.Random`` is not used for any value that reaches output. CPython
promises cross-version stability for only part of its interface, so this class
builds every draw from SHA-256 in counter mode and fixes the layout here.

Byte format:

- ``encode_parts`` writes each path segment as an 8 byte big-endian length
  followed by its UTF-8 bytes (``surrogatepass`` for a lone surrogate), so no
  two different paths encode to the same bytes.
- The stream key is ``sha256(DOMAIN + seed + encode_parts(*path))``, where
  ``DOMAIN`` is ``b"pytest-graphql/rng/v1\\0"`` and ``seed`` is 8 bytes,
  big-endian.
- Block ``i`` of the stream is ``sha256(key + i_8_bytes_big_endian)``, with
  ``i`` starting at 0. The stream is block 0, then block 1, and so on, read as
  one sequence of bytes.
- ``bits(k)`` takes ``ceil(k / 8)`` bytes, reads them as a big-endian integer
  and keeps the top ``k`` bits. ``bits(0)`` is 0 and takes no bytes.
- ``below(n)`` is rejection sampling: draw ``bits((n - 1).bit_length())`` until
  the value is below ``n``. It never uses a remainder, which would favour low
  values. ``below(1)`` is 0 and takes no bytes. Fewer than two draws are
  needed on average, because each is accepted with probability above one half.
- ``float_unit()`` is ``bits(53) / 2**53``: an exact multiple of ``2**-53`` in
  ``[0, 1)``, the same on every IEEE 754 platform.
- ``choice(seq)`` is ``seq[below(len(seq))]``.
- ``sample_string(length, alphabet)`` draws ``alphabet[below(len(alphabet))]``
  once per character.

State is a key, a counter and less than one block of unread bytes, whatever
the number of draws.

Changing any byte of this layout changes every generated value. That is a
versioned change recorded in the changelog and in the golden vectors.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")

DOMAIN = b"pytest-graphql/rng/v1\0"

#: The default alphabet of ``sample_string``: lowercase letters, then digits.
DEFAULT_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

_BLOCK_BYTES = 32


def encode_parts(*parts: str) -> bytes:
    """Each part as an 8 byte big-endian length and its UTF-8 bytes."""
    encoded = bytearray()
    for part in parts:
        data = part.encode("utf-8", "surrogatepass")
        encoded += len(data).to_bytes(8, "big")
        encoded += data
    return bytes(encoded)


def _segment(segment: str | int) -> str:
    if isinstance(segment, bool) or not isinstance(segment, (str, int)):
        raise TypeError(
            f"a path segment must be a str or an int, got {type(segment).__name__}."
        )
    return str(segment) if isinstance(segment, int) else segment


def _check_int(value: object, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} needs an int, got {type(value).__name__}.")


class DeterministicRandom:
    """A source of random values that is the same every time for the same inputs.

    A `ScalarSpec.fake` function receives one of these. The same seed and path
    give the same values on every machine and every supported Python version, so
    a test that uses seeded data repeats exactly. This is why the package does
    not use `random.Random` for any value that reaches output: its stream is
    only partly stable between Python versions.

    Create one only when you test your own fake functions. Reproducible values
    and unique values exclude each other. For a value that must differ on every
    run, use `unique()`.

    Args:
        seed: An integer from `0` up to but not including `2**64`.
        *path: Names for the value the stream is for, such as a type name and a
            field name. Each path gets its own stream, so adding a field does
            not change the values of the others. A segment is a string or an
            integer. An integer reads as its decimal text.

    Raises:
        TypeError: When `seed` or a path segment has the wrong type.
        ValueError: When `seed` is out of range.

    Examples:
        ```python {.exec}
        from pytest_graphql import DeterministicRandom

        name = DeterministicRandom(7, "User", "name").sample_string(8)
        assert name == DeterministicRandom(7, "User", "name").sample_string(8)

        email = DeterministicRandom(7, "User", "email").sample_string(8)
        assert email != name
        ```
    """

    __slots__ = ("_counter", "_key", "_pending")

    def __init__(self, seed: int, *path: str | int) -> None:
        _check_int(seed, "seed")
        if not 0 <= seed < 2**64:
            raise ValueError(f"seed must fit in unsigned 64 bits, got {seed}.")
        self._key = hashlib.sha256(
            DOMAIN + seed.to_bytes(8, "big") + encode_parts(*map(_segment, path))
        ).digest()
        self._counter = 0
        self._pending = b""

    def _take(self, count: int) -> bytes:
        while len(self._pending) < count:
            block = hashlib.sha256(
                self._key + self._counter.to_bytes(8, "big")
            ).digest()
            self._counter += 1
            self._pending += block
        taken = self._pending[:count]
        self._pending = self._pending[count:]
        return taken

    def bits(self, width: int) -> int:
        """Return an integer of `width` random bits, from `0` to `2**width - 1`.

        Args:
            width: The number of bits. `0` gives `0`.

        Returns:
            The integer.

        Raises:
            TypeError: When `width` is not an `int`.
            ValueError: When `width` is negative.

        Examples:
            ```python {.exec}
            from pytest_graphql import DeterministicRandom

            rng = DeterministicRandom(1, "demo")
            assert 0 <= rng.bits(8) < 256
            assert rng.bits(0) == 0
            ```
        """
        _check_int(width, "bits()")
        if width < 0:
            raise ValueError(f"bits() needs a width of 0 or more, got {width}.")
        if width == 0:
            return 0
        size = (width + 7) // 8
        return int.from_bytes(self._take(size), "big") >> (8 * size - width)

    def below(self, bound: int) -> int:
        """Return a uniform integer from `0` up to but not including `bound`.

        Every value has the same chance. The method never uses a remainder,
        which would favour small values.

        Args:
            bound: The exclusive upper limit. It must be at least `1`.

        Returns:
            The integer.

        Raises:
            TypeError: When `bound` is not an `int`.
            ValueError: When `bound` is less than `1`.

        Examples:
            ```python {.exec}
            from pytest_graphql import DeterministicRandom

            rng = DeterministicRandom(1, "demo")
            assert all(0 <= rng.below(6) < 6 for _ in range(100))
            assert rng.below(1) == 0
            ```
        """
        _check_int(bound, "below()")
        if bound < 1:
            raise ValueError(f"below() needs a bound of at least 1, got {bound}.")
        width = (bound - 1).bit_length()
        if width == 0:
            return 0
        while True:
            candidate = self.bits(width)
            if candidate < bound:
                return candidate

    def float_unit(self) -> float:
        """Return a float from `0` up to but not including `1`.

        The value is a multiple of `2**-53`, so it is the same on every platform.

        Returns:
            The float.

        Examples:
            ```python {.exec}
            from pytest_graphql import DeterministicRandom

            rng = DeterministicRandom(1, "demo")
            assert 0.0 <= rng.float_unit() < 1.0
            ```
        """
        return self.bits(53) / (1 << 53)

    def choice(self, items: Sequence[T]) -> T:
        """Return one element of a sequence.

        Args:
            items: A sequence with a stable order, such as a list or a tuple. A
                set is refused, because its order can change between runs.

        Returns:
            One of the elements.

        Raises:
            TypeError: When `items` is not a sequence.
            ValueError: When `items` is empty.

        Examples:
            ```python {.exec}
            from pytest_graphql import DeterministicRandom

            rng = DeterministicRandom(1, "demo")
            assert rng.choice(["red", "green", "blue"]) in {"red", "green", "blue"}
            ```
        """
        if not isinstance(items, Sequence):
            raise TypeError(
                "choice() needs a sequence, got "
                f"{type(items).__name__}, whose order is not stable."
            )
        if not items:
            raise ValueError("choice() cannot pick from an empty sequence.")
        return items[self.below(len(items))]

    def sample_string(self, length: int, alphabet: str = DEFAULT_ALPHABET) -> str:
        """Return a string of random characters.

        Args:
            length: The number of characters. It may be `0`.
            alphabet: The characters to draw from. The default is the lowercase
                letters and the digits.

        Returns:
            The string.

        Raises:
            TypeError: When `length` is not an `int` or `alphabet` is not a
                string.
            ValueError: When `length` is negative or `alphabet` is empty.

        Examples:
            ```python {.exec}
            from pytest_graphql import DeterministicRandom

            rng = DeterministicRandom(1, "demo")
            assert len(rng.sample_string(12)) == 12
            assert set(rng.sample_string(20, "01")) <= {"0", "1"}
            ```
        """
        _check_int(length, "sample_string()")
        if length < 0:
            raise ValueError(
                f"sample_string() needs a length of 0 or more, got {length}."
            )
        if not isinstance(alphabet, str):
            raise TypeError(
                f"the alphabet must be a str, got {type(alphabet).__name__}."
            )
        if not alphabet:
            raise ValueError("the alphabet must not be empty.")
        return "".join(alphabet[self.below(len(alphabet))] for _ in range(length))

    def __repr__(self) -> str:
        return "DeterministicRandom(...)"
