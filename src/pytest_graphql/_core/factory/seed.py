"""Stable seed derivation (B1).

The seed for one test is

    int.from_bytes(sha256(f"{global_seed}\\0{node_id}".encode()).digest()[:8], "big")

which is the formula in ``docs/reference/DESIGN_DECISIONS.md`` section 4. The
builtin ``hash()`` is never used, because Python salts it per process.

Byte format, stated once so it never depends on the interpreter:

- ``global_seed`` is written as its decimal text, with a leading ``-`` when it
  is negative and no padding or separator characters.
- A single NUL byte separates it from the node id. The decimal text of an int
  holds no NUL, so the first NUL always ends the seed and the pair is
  unambiguous whatever the node id holds.
- ``node_id`` is encoded as UTF-8. A lone surrogate, which pytest can produce
  from an undecodable file name, is encoded as the three bytes
  ``surrogatepass`` gives it. Every text that plain ``str.encode()`` accepts
  produces the same bytes either way, so the documented formula is unchanged
  for it.
- The result is the first 8 bytes of the SHA-256 digest, read big-endian, so
  it is an integer in ``[0, 2**64)``.
"""

from __future__ import annotations

import hashlib


def derive_seed(global_seed: int, node_id: str) -> int:
    """The seed for ``node_id`` under ``global_seed``, stable on every machine."""
    if isinstance(global_seed, bool) or not isinstance(global_seed, int):
        raise TypeError(
            f"global_seed must be an int, got {type(global_seed).__name__}."
        )
    if not isinstance(node_id, str):
        raise TypeError(f"node_id must be a str, got {type(node_id).__name__}.")
    text = f"{global_seed}\0{node_id}".encode("utf-8", "surrogatepass")
    return int.from_bytes(hashlib.sha256(text).digest()[:8], "big")
