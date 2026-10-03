"""The determinism promise, checked against stored values (SPEC 11.3, C10).

The same seed, node id, field path and package version give the same value on
any machine and any supported Python. This compares text, not parsed values,
so a float printed another way or a key in another order fails too.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.factory.golden_support import FORMAT, GOLDEN_PATH, render

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_output_matches_the_stored_golden_vectors_byte_for_byte() -> None:
    stored = GOLDEN_PATH.read_bytes()
    assert render().encode("utf-8") == stored, (
        "A golden value changed. That is a versioned change: raise FORMAT in "
        "golden_support.py, record it in CHANGELOG.md, and rewrite the file "
        "with --deliberate-version-change. Do not regenerate it to pass."
    )


def test_the_stored_file_has_the_current_format() -> None:
    assert json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["format"] == FORMAT


def test_the_file_covers_every_built_in_scalar_and_a_nested_input() -> None:
    stored = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert set(stored["scalars"]) == {"String", "Int", "Float", "Boolean", "ID"}
    nested = stored["inputs"]["CreateUserInput"]["profile"]["address"]
    assert set(nested) == {"street", "city", "zip", "country"}


def test_the_file_is_plain_ascii_text_with_a_final_newline() -> None:
    raw = GOLDEN_PATH.read_bytes()
    raw.decode("ascii")
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    assert b"\r" not in raw


@pytest.mark.parametrize("hash_seed", ["0", "1", "424242"])
def test_a_fresh_process_with_another_hash_seed_gives_the_same_text(
    hash_seed: str,
) -> None:
    # Python salts `hash()` and set order per process. A different
    # PYTHONHASHSEED therefore changes any output that leans on either, which
    # the same-process comparison above cannot see. The text goes out as bytes,
    # because a text stream on Windows turns each LF into CRLF.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from tests.factory.golden_support import render; "
            "sys.stdout.buffer.write(render().encode('utf-8'))",
        ],
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
    )
    assert result.stdout == GOLDEN_PATH.read_bytes()
