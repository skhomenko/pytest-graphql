#!/usr/bin/env python3
"""Artifact checks for a built wheel and sdist.

The "Release integrity" section of ``docs/reference/DESIGN_DECISIONS.md``
requires these checks on every build: PEP 621 metadata completeness, wheel and
sdist contents, ``__version__`` agreeing with the tag, and the Development
Status classifier agreeing with a prerelease's phase. It also tells the
release job whether the tag is a PEP 440 prerelease. Long-description
rendering and the rest of the packaging surface are checked by ``twine check``,
which runs beside this script rather than inside it.

Usage::

    python3 scripts/check_artifacts.py --dist dist --version 0.1.0a1
    python3 scripts/check_artifacts.py --dist dist --tag v0.1.0a1
    python3 scripts/check_artifacts.py --tag v0.1.0a1 --print-prerelease
    python3 scripts/check_artifacts.py --self-test

Exit codes: 0 clean, 1 findings reported, 2 usage or read error.

Stdlib only. ``zipfile`` and ``tarfile`` read the artifacts, and the metadata is
parsed with ``email.parser``, which is the format ``METADATA`` uses.
"""

from __future__ import annotations

import argparse
import re
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
# SECURITY.md is here because CONTRIBUTING.md sends security reporters to it.
REQUIRED_SDIST_PATHS = (
    "pyproject.toml",
    "README.md",
    "SECURITY.md",
    "LICENSE",
    "CHANGELOG.md",
    "src/pytest_graphql/__init__.py",
    "src/pytest_graphql/py.typed",
)

ENTRY_POINT_LINE = "graphql = pytest_graphql.plugin"

DEVELOPMENT_STATUS = "Development Status :: "

# The classifier a prerelease phase must carry, so the maturity PyPI shows
# agrees with the version. A final or a development-only version is not
# mapped: its classifier is the maintainer's statement at that gate.
PHASE_CLASSIFIERS = {
    "a": "Development Status :: 3 - Alpha",
    "b": "Development Status :: 4 - Beta",
    "rc": "Development Status :: 4 - Beta",
}

# PEP 440 spellings of each pre-release label, by normalized phase.
_PHASE_OF_LABEL = {
    "a": "a",
    "alpha": "a",
    "b": "b",
    "beta": "b",
    "c": "rc",
    "rc": "rc",
    "pre": "rc",
    "preview": "rc",
}


# The version grammar of PEP 440, Appendix B. PEP 440 is in the public domain.
# Adapted: the optional leading "v" and surrounding whitespace are dropped,
# because the version here is already taken from the tag, and the match is
# anchored at both ends.
_PEP440_VERSION = re.compile(
    r"""
    \A
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?P<pre>
        [-_.]?
        (?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)
        [-_.]?
        (?P<pre_n>[0-9]+)?
    )?
    (?P<post>
        (?:-(?P<post_n1>[0-9]+))
        |
        (?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?)
    )?
    (?P<dev>
        [-_.]?
        (?P<dev_l>dev)
        [-_.]?
        (?P<dev_n>[0-9]+)?
    )?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \Z
    """,
    re.VERBOSE | re.IGNORECASE,
)


def version_from_tag(tag: str) -> str:
    """Return the version a release tag names."""
    return tag[1:] if tag.startswith("v") else tag


def prerelease_phase(version: str) -> str | None:
    """The normalized pre-release phase, ``a``, ``b`` or ``rc``, or ``None``.

    A string that is not a PEP 440 version raises ``ValueError``.
    """
    match = _PEP440_VERSION.match(version)
    if match is None:
        raise ValueError(f"not a PEP 440 version: {version!r}")
    label = match.group("pre_l")
    return None if label is None else _PHASE_OF_LABEL[label.lower()]


def is_prerelease(version: str) -> bool:
    """Whether a PEP 440 version is a prerelease.

    A version with a pre-release or a development segment is a prerelease, as
    in PEP 440 and ``packaging``. A post-release or a local label alone is not.
    A string that is not a PEP 440 version raises ``ValueError``, so a release
    is never published under a guess.
    """
    match = _PEP440_VERSION.match(version)
    if match is None:
        raise ValueError(f"not a PEP 440 version: {version!r}")
    return match.group("pre") is not None or match.group("dev") is not None


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

    statuses = [
        value
        for value in message.get_all("Classifier") or []
        if value.startswith(DEVELOPMENT_STATUS)
    ]
    if len(statuses) != 1:
        problems.append(
            f"{source}: expected one Development Status classifier, "
            f"found {len(statuses)}"
        )
    else:
        try:
            phase = prerelease_phase(expected_version)
        except ValueError:
            phase = None
        wanted = PHASE_CLASSIFIERS.get(phase) if phase else None
        if wanted is not None and statuses[0] != wanted:
            problems.append(
                f"{source}: classifier is {statuses[0]!r}, "
                f"but version {expected_version} needs {wanted!r}"
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
Classifier: Development Status :: 3 - Alpha
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
            _GOOD_METADATA.replace(b"Classifier: Framework :: Pytest\n", b"").replace(
                b"Classifier: Development Status :: 3 - Alpha\n", b""
            ),
            "0.1.0a1",
            2,
        ),
        (
            "a beta with the alpha classifier fails",
            _GOOD_METADATA.replace(b"Version: 0.1.0a1", b"Version: 0.1.0b1"),
            "0.1.0b1",
            1,
        ),
        (
            "a beta with the beta classifier passes",
            _GOOD_METADATA.replace(b"Version: 0.1.0a1", b"Version: 0.1.0b1").replace(
                b"3 - Alpha", b"4 - Beta"
            ),
            "0.1.0b1",
            0,
        ),
        (
            "a release candidate needs the beta classifier",
            _GOOD_METADATA.replace(b"Version: 0.1.0a1", b"Version: 0.1.0rc1"),
            "0.1.0rc1",
            1,
        ),
        (
            "a final version is not mapped",
            _GOOD_METADATA.replace(b"Version: 0.1.0a1", b"Version: 0.1.0"),
            "0.1.0",
            0,
        ),
        (
            "two development status classifiers fail",
            _GOOD_METADATA.replace(
                b"Classifier: Framework :: Pytest\n",
                b"Classifier: Development Status :: 4 - Beta\n"
                b"Classifier: Framework :: Pytest\n",
            ),
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

    prereleases = (
        ("0.1.0a1", True),
        ("0.1.0b1", True),
        ("0.1.0rc2", True),
        ("0.1.0.dev0", True),
        ("0.1.0a1.dev0", True),
        ("1.0.0-beta.3", True),
        ("0.1.0", False),
        ("1.0.0.post1", False),
        ("1.0.0-1", False),
        ("1!2.0", False),
        ("1.0.0+local.7", False),
    )
    for version, expected_pre in prereleases:
        if is_prerelease(version) is not expected_pre:
            print(f"FAIL prerelease {version}: expected {expected_pre}")
            failures += 1

    invalid = ("", "v0.1.0", "0.1.0 ", "latest", "0.1.0-alpha-1-x", "0..1")
    for version in invalid:
        try:
            is_prerelease(version)
        except ValueError:
            continue
        print(f"FAIL prerelease {version!r}: expected ValueError")
        failures += 1

    total = len(cases) + len(tags) + len(prereleases) + len(invalid)
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
    parser.add_argument(
        "--print-prerelease",
        action="store_true",
        help="print true or false for whether the version is a prerelease",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    if bool(args.version) == bool(args.tag):
        print("error: give exactly one of --version and --tag", file=sys.stderr)
        return 2
    expected = args.version or version_from_tag(args.tag)

    if args.print_prerelease:
        try:
            print("true" if is_prerelease(expected) else "false")
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        return 0

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
