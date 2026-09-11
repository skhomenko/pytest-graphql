#!/usr/bin/env python3
"""Artifact checks for a built wheel and sdist.

The "Release integrity" section of ``docs/reference/DESIGN_DECISIONS.md``
requires these checks on every build: PEP 621 metadata completeness, wheel and
sdist contents, and ``__version__`` agreeing with the tag. Long-description
rendering and the rest of the packaging surface are checked by ``twine check``,
which runs beside this script rather than inside it.

Usage::

    python3 scripts/check_artifacts.py --dist dist --version 0.1.0a1
    python3 scripts/check_artifacts.py --dist dist --tag v0.1.0a1
    python3 scripts/check_artifacts.py --self-test

Exit codes: 0 clean, 1 findings reported, 2 usage or read error.

Stdlib only. ``zipfile`` and ``tarfile`` read the artifacts, and the metadata is
parsed with ``email.parser``, which is the format ``METADATA`` uses.
"""

from __future__ import annotations

import argparse
import sys
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

# PEP 621 fields this project promises. A missing one is a defect in
# pyproject.toml, not in the build.
REQUIRED_METADATA = (
    "Metadata-Version",
    "Name",
    "Version",
    "Summary",
    "Requires-Python",
    "Description-Content-Type",
    "License-Expression",
)

REQUIRED_METADATA_MULTI = (
    "Classifier",
    "Project-URL",
    "Requires-Dist",
)

# Paths every wheel must carry. ``py.typed`` is the typing promise, and the
# entry point is what pytest reads.
REQUIRED_WHEEL_PATHS = (
    "pytest_graphql/__init__.py",
    "pytest_graphql/py.typed",
    "pytest_graphql/_core/__init__.py",
    "pytest_graphql/plugin/__init__.py",
)

# Paths every sdist must carry, relative to its single top-level directory.
REQUIRED_SDIST_PATHS = (
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "src/pytest_graphql/__init__.py",
    "src/pytest_graphql/py.typed",
)

ENTRY_POINT_LINE = "graphql = pytest_graphql.plugin"


def version_from_tag(tag: str) -> str:
    """Return the version a release tag names."""
    return tag[1:] if tag.startswith("v") else tag


def _one(paths: list[Path], what: str, problems: list[str]) -> Path | None:
    if len(paths) == 1:
        return paths[0]
    problems.append(f"expected exactly one {what}, found {len(paths)}")
    return None


def check_metadata(text: bytes, expected_version: str, source: str) -> list[str]:
    """Check PEP 621 metadata completeness and the version."""
    problems: list[str] = []
    message = BytesParser().parsebytes(text)

    for field in REQUIRED_METADATA:
        if not message.get(field):
            problems.append(f"{source}: metadata field {field} is missing or empty")

    for field in REQUIRED_METADATA_MULTI:
        if not message.get_all(field):
            problems.append(f"{source}: metadata field {field} has no value")

    version = message.get("Version")
    if version and version != expected_version:
        problems.append(
            f"{source}: metadata version is {version}, expected {expected_version}"
        )

    body = message.get_payload(decode=True)
    if not body or not body.strip():
        problems.append(f"{source}: the long description is empty")

    return problems


def check_wheel(path: Path, expected_version: str) -> list[str]:
    """Check one wheel's contents, metadata and entry point."""
    problems: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())

        for required in REQUIRED_WHEEL_PATHS:
            if required not in names:
                problems.append(f"{path.name}: missing {required}")

        dist_infos = sorted(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        if len(dist_infos) != 1:
            problems.append(
                f"{path.name}: expected one .dist-info/METADATA, "
                f"found {len(dist_infos)}"
            )
            return problems

        problems.extend(
            check_metadata(archive.read(dist_infos[0]), expected_version, path.name)
        )

        prefix = dist_infos[0].rsplit("/", 1)[0]
        entry_points = f"{prefix}/entry_points.txt"
        if entry_points not in names:
            problems.append(f"{path.name}: missing {entry_points}")
        else:
            text = archive.read(entry_points).decode("utf-8")
            if "[pytest11]" not in text or ENTRY_POINT_LINE not in text:
                problems.append(
                    f"{path.name}: the pytest11 entry point is not declared"
                )

        licenses = [
            name
            for name in names
            if name.startswith(f"{prefix}/licenses/") and name.endswith("LICENSE")
        ]
        if not licenses:
            problems.append(f"{path.name}: no LICENSE under {prefix}/licenses/")

    return problems


def check_sdist(path: Path, expected_version: str) -> list[str]:
    """Check one sdist's contents and metadata."""
    problems: list[str] = []
    with tarfile.open(path, "r:gz") as archive:
        names = archive.getnames()
        roots = {name.split("/", 1)[0] for name in names}
        if len(roots) != 1:
            problems.append(
                f"{path.name}: expected one top-level directory, got {sorted(roots)}"
            )
            return problems
        root = roots.pop()

        for required in REQUIRED_SDIST_PATHS:
            if f"{root}/{required}" not in names:
                problems.append(f"{path.name}: missing {required}")

        pkg_info = f"{root}/PKG-INFO"
        if pkg_info not in names:
            problems.append(f"{path.name}: missing PKG-INFO")
            return problems
        handle = archive.extractfile(pkg_info)
        if handle is None:
            problems.append(f"{path.name}: PKG-INFO could not be read")
            return problems
        problems.extend(check_metadata(handle.read(), expected_version, path.name))

    return problems


def check_dist(dist: Path, expected_version: str) -> list[str]:
    """Check every artifact in ``dist``, and that there is one of each kind."""
    problems: list[str] = []
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))

    wheel = _one(wheels, "wheel", problems)
    sdist = _one(sdists, "sdist", problems)

    # A build manifest may be written beside the artifacts, and ``uv build``
    # writes a .gitignore into the directory. Neither is part of the release.
    unexpected = sorted(
        p.name
        for p in dist.iterdir()
        if p.is_file()
        and p not in wheels
        and p not in sdists
        and p.suffix != ".json"
        and not p.name.startswith(".")
    )
    if unexpected:
        problems.append(f"unexpected file(s) in {dist}: {', '.join(unexpected)}")

    if wheel is not None:
        problems.extend(check_wheel(wheel, expected_version))
    if sdist is not None:
        problems.extend(check_sdist(sdist, expected_version))
    return problems


_GOOD_METADATA = b"""\
Metadata-Version: 2.4
Name: pytest-graphql
Version: 0.1.0a1
Summary: Schema-aware GraphQL API testing for pytest.
Requires-Python: >=3.10
Description-Content-Type: text/markdown
License-Expression: MIT
Classifier: Framework :: Pytest
Project-URL: Source, https://example.invalid/repo
Requires-Dist: httpx>=0.27,<1

# pytest-graphql

Long description body.
"""


def self_test() -> int:
    """Check the metadata rules and the tag parsing."""
    failures = 0

    cases: tuple[tuple[str, bytes, str, int], ...] = (
        ("complete metadata passes", _GOOD_METADATA, "0.1.0a1", 0),
        (
            "a wrong version fails",
            _GOOD_METADATA,
            "0.2.0",
            1,
        ),
        (
            "a missing field fails",
            _GOOD_METADATA.replace(
                b"Summary: Schema-aware GraphQL API testing for pytest.\n", b""
            ),
            "0.1.0a1",
            1,
        ),
        (
            "no classifier fails",
            _GOOD_METADATA.replace(b"Classifier: Framework :: Pytest\n", b""),
            "0.1.0a1",
            1,
        ),
        (
            "an empty long description fails",
            _GOOD_METADATA.split(b"\n\n")[0] + b"\n\n",
            "0.1.0a1",
            1,
        ),
    )
    for name, text, version, expected in cases:
        problems = check_metadata(text, version, "case")
        if len(problems) != expected:
            print(f"FAIL {name}: expected {expected} problem(s), got {problems}")
            failures += 1

    tags = (("v0.1.0a1", "0.1.0a1"), ("0.1.0", "0.1.0"))
    for tag, expected_version in tags:
        if version_from_tag(tag) != expected_version:
            print(f"FAIL tag {tag}")
            failures += 1

    total = len(cases) + len(tags)
    if failures:
        print(f"\n{failures} of {total} self-test case(s) failed.")
        return 1
    print(f"self-test: {total} of {total} cases passed.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", default="dist")
    parser.add_argument("--version", default="")
    parser.add_argument("--tag", default="")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    if bool(args.version) == bool(args.tag):
        print("error: give exactly one of --version and --tag", file=sys.stderr)
        return 2
    expected = args.version or version_from_tag(args.tag)

    dist = Path(args.dist)
    if not dist.is_dir():
        print(f"error: not a directory: {dist}", file=sys.stderr)
        return 2

    problems = check_dist(dist, expected)
    if problems:
        print(f"artifact checks: {len(problems)} finding(s)", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    print(f"artifact checks: wheel and sdist are complete at version {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
