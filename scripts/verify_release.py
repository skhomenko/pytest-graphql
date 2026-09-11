#!/usr/bin/env python3
"""Release manifest and install-back verification.

The publication path is defined in the "Release integrity" section of
``docs/reference/DESIGN_DECISIONS.md``. This script implements the two
mechanical parts of it.

``manifest`` records one digest for every file the single build produced.

``verify`` enumerates the release on an index before it downloads anything. It
reads the file list for the exact normalized project name from the simple index
API in its JSON form (PEP 691), keeps only the files whose version equals the
version under test, and compares that complete remote set with the build
manifest in both directions. A missing file, an extra file or a differing digest
fails the gate. Only then does it fetch each manifest file by the URL the index
gave for it and recheck the digest after the transfer.

Enumeration is what makes the comparison bidirectional. A download cannot do it.
``pip download`` runs the same resolution as ``pip install`` and returns one
selected distribution per requirement, so an extra file attached to the release
would never be fetched and never be noticed.

Usage::

    python3 scripts/verify_release.py manifest --dist dist --out dist/manifest.json
    python3 scripts/verify_release.py verify \
        --index-url https://test.pypi.org/simple \
        --name pytest-graphql --version 0.1.0a1 \
        --manifest manifest.json --download-dir candidate
    python3 scripts/verify_release.py --self-test

Exit codes: 0 clean, 1 the gate failed, 2 usage or transport error.

Stdlib only, so it runs in a job that holds no publishing credential and no
project environment.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

SIMPLE_JSON_ACCEPT = "application/vnd.pypi.simple.v1+json"

# Distribution file suffixes this project produces. A file on the release with
# any other suffix is an extra file, and the comparison reports it as one.
WHEEL_SUFFIX = ".whl"
SDIST_SUFFIXES = (".tar.gz", ".zip")

READ_CHUNK = 1024 * 1024
HTTP_TIMEOUT = 60

# A ceiling on any single response. The largest thing this script fetches is one
# distribution file, and this project's are a few hundred kilobytes. The limit
# exists so a hostile or broken index cannot exhaust the runner's memory.
MAX_RESPONSE_BYTES = 256 * 1024 * 1024

# Resolving a host is how a destination is judged, so the resolver is an
# argument. A test supplies its own and performs no lookup.
Resolver = Callable[[str, int], Sequence[Any]]


class FileEntry(NamedTuple):
    """One distribution file, by name and sha256 digest."""

    filename: str
    sha256: str


class Release(NamedTuple):
    """What an index lists for one project at one version.

    ``unattributed`` holds every file the index listed under the project whose
    name carries no version this script can read. Such a file cannot be placed
    inside or outside the release, so it is reported rather than dropped.
    """

    files: list[FileEntry]
    urls: dict[str, str]
    unattributed: list[str]


def normalize_name(name: str) -> str:
    """Return the PEP 503 normalized project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def is_safe_filename(name: str) -> bool:
    """Return True when ``name`` is a single, plain path component.

    A distribution filename arrives from a remote index, so it is untrusted
    input. Any name that is not one ordinary component can escape the directory
    it is joined to.
    """
    if not name or name in {".", ".."}:
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    return Path(name).name == name


def _filename_stem(filename: str) -> tuple[str, bool] | None:
    """Return the stem of a distribution filename and whether it is a wheel."""
    if filename.endswith(WHEEL_SUFFIX):
        return filename[: -len(WHEEL_SUFFIX)], True
    for suffix in SDIST_SUFFIXES:
        if filename.endswith(suffix):
            return filename[: -len(suffix)], False
    return None


def file_version(filename: str, project: str) -> str | None:
    """Return the version a distribution filename carries, or None.

    None means the filename is not a distribution of ``project``. The caller
    treats that as "not part of this release" rather than as an error, because
    an index lists every file of the project, not only this version.
    """
    parsed = _filename_stem(filename)
    if parsed is None:
        return None
    stem, is_wheel = parsed
    normalized = normalize_name(project)

    if is_wheel:
        parts = stem.split("-")
        # name, version, then at least the three compatibility tags.
        if len(parts) < 5 or normalize_name(parts[0]) != normalized:
            return None
        return parts[1]

    name, sep, version = stem.rpartition("-")
    if not sep or normalize_name(name) != normalized:
        return None
    return version


def same_version(left: str, right: str) -> bool:
    """Compare two version strings the way a filename spells them.

    A build writes the version into the filename itself, so the two sides come
    from the same string. Case and the underscore that a filename may carry in
    place of a separator are the only differences worth absorbing.
    """
    return left.lower().replace("_", "-") == right.lower().replace("_", "-")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(dist_dir: Path) -> list[FileEntry]:
    """Return one entry per distribution file in ``dist_dir``."""
    entries: list[FileEntry] = []
    for path in sorted(dist_dir.iterdir()):
        if not path.is_file():
            continue
        if _filename_stem(path.name) is None:
            continue
        entries.append(FileEntry(path.name, sha256_of(path)))
    return entries


def read_manifest(path: Path) -> list[FileEntry]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [FileEntry(item["filename"], item["sha256"]) for item in data["files"]]


def write_manifest(entries: list[FileEntry], version: str, out: Path) -> None:
    payload = {
        "version": version,
        "files": [{"filename": e.filename, "sha256": e.sha256} for e in entries],
    }
    out.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def compare(manifest: list[FileEntry], remote: list[FileEntry]) -> list[str]:
    """Return every difference between the manifest and the remote release.

    The comparison runs in both directions. A file in the manifest that is not
    on the release, a file on the release that is not in the manifest, and a
    file whose digest differs are all failures.
    """
    problems: list[str] = []
    local = {entry.filename: entry.sha256 for entry in manifest}
    published = {entry.filename: entry.sha256 for entry in remote}

    for filename in sorted(set(local) - set(published)):
        problems.append(f"missing from the release: {filename}")
    for filename in sorted(set(published) - set(local)):
        problems.append(f"attached to the release but not built: {filename}")
    for filename in sorted(set(local) & set(published)):
        if local[filename] != published[filename]:
            problems.append(
                f"digest differs for {filename}: "
                f"built {local[filename]}, published {published[filename]}"
            )
    return problems


def _is_public_address(address: str) -> bool:
    """Return True when ``address`` is a routable public address."""
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return not (
        parsed.is_loopback
        or parsed.is_private
        or parsed.is_link_local
        or parsed.is_reserved
        or parsed.is_multicast
        or parsed.is_unspecified
    )


def check_destination(url: str, resolve: Resolver | None = None) -> None:
    """Raise unless ``url`` is https and its host resolves to public addresses.

    Every URL this script fetches is either the operator's index or a location
    the index chose, and both are meant to be public https endpoints. Checking
    the scheme alone is not enough, because a redirect can move a request to
    plaintext or point a credential-free runner at a service on its own network.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"refusing a URL that is not https: {url}")
    host = parts.hostname
    if not host:
        raise ValueError(f"refusing a URL with no host: {url}")

    resolver = resolve if resolve is not None else _default_resolver
    try:
        infos = resolver(host, parts.port or 443)
    except OSError as exc:
        raise ValueError(f"could not resolve {host}: {exc}") from exc

    addresses = sorted({str(info[4][0]) for info in infos})
    if not addresses:
        raise ValueError(f"no address for {host}")
    for address in addresses:
        if not _is_public_address(address):
            raise ValueError(f"refusing {url}: {host} resolves to {address}")


def _default_resolver(host: str, port: int) -> Sequence[Any]:
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


class HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse any redirect whose destination fails ``check_destination``.

    Python's default handler follows an https request to an http location
    without complaint, which silently drops transport security.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        check_destination(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(HttpsOnlyRedirects)


def _read_capped(response: Any, url: str) -> bytes:
    """Read a response body, refusing anything above ``MAX_RESPONSE_BYTES``."""
    chunks: list[bytes] = []
    total = 0
    while chunk := response.read(READ_CHUNK):
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ValueError(
                f"refusing a response above {MAX_RESPONSE_BYTES} bytes: {url}"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _fetch(url: str, accept: str | None = None) -> bytes:
    """Fetch ``url`` over https, refusing a downgrade at every hop."""
    check_destination(url)
    request = urllib.request.Request(url)
    if accept:
        request.add_header("Accept", accept)
    with _OPENER.open(request, timeout=HTTP_TIMEOUT) as response:
        return _read_capped(response, url)


def select_release(payload: dict[str, Any], project: str, version: str) -> Release:
    """Split an index listing into this release, other versions and the rest.

    A file whose name parses to another version is out of scope for this
    release. A file whose name parses to nothing is not, because nothing places
    it, so it is returned in ``unattributed`` and the caller reports it.
    """
    entries: list[FileEntry] = []
    urls: dict[str, str] = {}
    unattributed: list[str] = []
    for item in payload.get("files", []):
        filename = item["filename"]
        found = file_version(filename, project)
        if found is None:
            unattributed.append(filename)
            continue
        if not same_version(found, version):
            continue
        hashes = item.get("hashes") or {}
        sha256 = hashes.get("sha256")
        if not sha256:
            raise ValueError(f"the index published no sha256 for {filename}")
        entries.append(FileEntry(filename, sha256))
        urls[filename] = item["url"]

    entries.sort()
    unattributed.sort()
    return Release(entries, urls, unattributed)


def enumerate_release(index_url: str, project: str, version: str) -> Release:
    """Return what the index lists for ``project`` at ``version``.

    Every file the index lists for this project is read, not only the ones a
    resolver would select. That is what lets an unexpected extra file on the
    release be seen at all.
    """
    normalized = normalize_name(project)
    url = f"{index_url.rstrip('/')}/{normalized}/"
    payload: dict[str, Any] = json.loads(_fetch(url, accept=SIMPLE_JSON_ACCEPT))
    return select_release(payload, project, version)


def download_and_recheck(
    entries: list[FileEntry], urls: dict[str, str], target: Path
) -> list[str]:
    """Fetch each file by its index URL and recheck its digest after transfer.

    Every name in ``entries`` came from a remote index, so this function checks
    each one itself before it builds a path. It does not rely on the caller
    having compared the names with a manifest first.
    """
    problems: list[str] = []
    target.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        if not is_safe_filename(entry.filename):
            problems.append(
                f"the index listed an unusable filename: {entry.filename!r}"
            )
            continue
        path = target / entry.filename
        path.write_bytes(_fetch(urls[entry.filename]))
        actual = sha256_of(path)
        if actual != entry.sha256:
            problems.append(
                f"digest changed in transfer for {entry.filename}: "
                f"expected {entry.sha256}, received {actual}"
            )
    return problems


def cmd_manifest(args: argparse.Namespace) -> int:
    dist = Path(args.dist)
    if not dist.is_dir():
        print(f"error: not a directory: {dist}", file=sys.stderr)
        return 2
    entries = build_manifest(dist)
    if not entries:
        print(f"error: no distribution file in {dist}", file=sys.stderr)
        return 2

    versions = {file_version(e.filename, args.name) for e in entries}
    versions.discard(None)
    if len(versions) != 1:
        print(
            f"error: the build produced mixed versions: {sorted(versions)}",
            file=sys.stderr,
        )
        return 1
    version = versions.pop()
    assert version is not None
    if args.version and not same_version(version, args.version):
        print(
            f"error: built {version} but {args.version} was expected",
            file=sys.stderr,
        )
        return 1

    write_manifest(entries, version, Path(args.out))
    print(f"manifest: {len(entries)} file(s) at version {version}")
    for entry in entries:
        print(f"  {entry.sha256}  {entry.filename}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    manifest = read_manifest(Path(args.manifest))
    try:
        release = enumerate_release(args.index_url, args.name, args.version)
    except (urllib.error.URLError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: could not enumerate the release: {exc}", file=sys.stderr)
        return 2

    remote = release.files
    print(f"release {args.name} {args.version}: {len(remote)} file(s) on the index")
    for entry in remote:
        print(f"  {entry.sha256}  {entry.filename}")

    problems = compare(manifest, remote)
    problems.extend(
        f"listed under the project but carries no readable version: {name}"
        for name in release.unattributed
    )
    if problems:
        print("the release does not match the build manifest:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    try:
        problems = download_and_recheck(remote, release.urls, Path(args.download_dir))
    except (urllib.error.URLError, ValueError) as exc:
        print(f"error: could not fetch a release file: {exc}", file=sys.stderr)
        return 2
    if problems:
        print(
            "a file changed between the index listing and the transfer:",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    print(f"verified {len(remote)} file(s) into {args.download_dir}")
    return 0


_NAME = "pytest-graphql"

_VERSION_CASES: tuple[tuple[str, str | None], ...] = (
    ("pytest_graphql-0.1.0a1-py3-none-any.whl", "0.1.0a1"),
    ("pytest_graphql-0.1.0a1.tar.gz", "0.1.0a1"),
    ("pytest_graphql-1.2.3-cp312-cp312-manylinux_2_17_x86_64.whl", "1.2.3"),
    ("pytest_graphql-0.1.0a1.zip", "0.1.0a1"),
    ("pytest-graphql-0.1.0a1.tar.gz", "0.1.0a1"),
    # Not a distribution of this project.
    ("pytest_graphql_extra-0.1.0a1.tar.gz", None),
    ("other-0.1.0a1.tar.gz", None),
    ("pytest_graphql-0.1.0a1.txt", None),
    ("pytest_graphql-0.1.0a1-py3-none-any.whl.asc", None),
)

_SAFE_NAME_CASES: tuple[tuple[str, bool], ...] = (
    ("pytest_graphql-0.1.0a1-py3-none-any.whl", True),
    ("pytest_graphql-0.1.0a1.tar.gz", True),
    ("", False),
    (".", False),
    ("..", False),
    ("../../etc/passwd", False),
    ("dist/pytest_graphql-0.1.0a1.tar.gz", False),
    ("..\\windows\\system32", False),
    ("evil\x00.whl", False),
)


def _resolver_for(mapping: dict[str, list[str]]) -> Resolver:
    """A resolver that answers from a table and performs no lookup."""

    def resolve(host: str, port: int) -> Sequence[Any]:
        if host not in mapping:
            raise OSError(f"no such host: {host}")
        return [(None, None, None, "", (address, port)) for address in mapping[host]]

    return resolve


_DESTINATION_CASES: tuple[tuple[str, str, bool], ...] = (
    ("a public https host is allowed", "https://index.invalid/simple/", True),
    ("plaintext is refused", "http://index.invalid/simple/", False),
    ("an ftp URL is refused", "ftp://index.invalid/simple/", False),
    ("a URL with no host is refused", "https:///simple/", False),
    ("a loopback host is refused", "https://local.invalid/x", False),
    ("a private host is refused", "https://internal.invalid/x", False),
    ("a link-local host is refused", "https://metadata.invalid/x", False),
    ("a host with no address is refused", "https://missing.invalid/x", False),
    ("one bad address among good ones is refused", "https://mixed.invalid/x", False),
)

_DESTINATION_HOSTS = {
    "index.invalid": ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
    "local.invalid": ["127.0.0.1"],
    "internal.invalid": ["10.1.2.3"],
    "metadata.invalid": ["169.254.169.254"],
    "mixed.invalid": ["93.184.216.34", "192.168.1.1"],
}


def self_test() -> int:
    """Check the parsing and the comparison against cases they must handle."""
    failures = 0

    for filename, expected in _VERSION_CASES:
        actual = file_version(filename, _NAME)
        if actual != expected:
            print(f"FAIL version of {filename}: expected {expected!r}, got {actual!r}")
            failures += 1

    if normalize_name("Pytest.GraphQL_Core") != "pytest-graphql-core":
        print("FAIL normalize_name")
        failures += 1

    built = [FileEntry("a.whl", "1"), FileEntry("a.tar.gz", "2")]

    cases: tuple[tuple[str, list[FileEntry], int], ...] = (
        ("identical sets pass", list(built), 0),
        ("a missing file fails", [FileEntry("a.whl", "1")], 1),
        (
            "an extra file fails",
            [*built, FileEntry("a-py2-none-any.whl", "3")],
            1,
        ),
        (
            "a changed digest fails",
            [FileEntry("a.whl", "9"), FileEntry("a.tar.gz", "2")],
            1,
        ),
        ("an empty release fails both files", [], 2),
    )
    for name, remote, expected_problems in cases:
        problems = compare(built, remote)
        if len(problems) != expected_problems:
            print(
                f"FAIL {name}: expected {expected_problems} problem(s), got {problems}"
            )
            failures += 1

    for name, expected_safe in _SAFE_NAME_CASES:
        if is_safe_filename(name) != expected_safe:
            print(f"FAIL is_safe_filename({name!r}): expected {expected_safe}")
            failures += 1

    # An unsafe name never becomes a path, even when nothing compared it first.
    with tempfile.TemporaryDirectory() as tmp:
        unsafe = download_and_recheck([FileEntry("../escape.whl", "1")], {}, Path(tmp))
    if len(unsafe) != 1:
        print(f"FAIL download_and_recheck did not reject an unsafe name: {unsafe}")
        failures += 1

    listing = {
        "files": [
            {
                "filename": "pytest_graphql-0.1.0a1.tar.gz",
                "url": "https://example.invalid/a",
                "hashes": {"sha256": "1"},
            },
            {
                "filename": "pytest_graphql-0.0.9.tar.gz",
                "url": "https://example.invalid/b",
                "hashes": {"sha256": "2"},
            },
            {
                "filename": "surprise.bin",
                "url": "https://example.invalid/c",
                "hashes": {"sha256": "3"},
            },
        ]
    }
    release = select_release(listing, _NAME, "0.1.0a1")
    selection_cases: tuple[tuple[str, object, object], ...] = (
        (
            "this version is selected",
            [e.filename for e in release.files],
            ["pytest_graphql-0.1.0a1.tar.gz"],
        ),
        (
            "another version is out of scope",
            "pytest_graphql-0.0.9.tar.gz" in release.unattributed,
            False,
        ),
        ("an unreadable name is reported", release.unattributed, ["surprise.bin"]),
    )
    for name, actual, expected in selection_cases:
        if actual != expected:
            print(f"FAIL {name}: expected {expected!r}, got {actual!r}")
            failures += 1

    resolve = _resolver_for(_DESTINATION_HOSTS)
    for name, url, should_pass in _DESTINATION_CASES:
        try:
            check_destination(url, resolve)
            passed = True
        except ValueError:
            passed = False
        if passed != should_pass:
            want = "accepted" if should_pass else "refused"
            print(f"FAIL {name}: expected {url} to be {want}")
            failures += 1

    # The default handler follows https to http. This one does not.
    redirect_cases: tuple[tuple[str, str, bool], ...] = (
        ("a redirect to plaintext is refused", "http://index.invalid/next", False),
        ("a redirect to loopback is refused", "https://local.invalid/next", False),
    )
    handler = HttpsOnlyRedirects()
    for name, newurl, should_pass in redirect_cases:
        try:
            handler.redirect_request(
                urllib.request.Request("https://index.invalid/start"),
                None,
                302,
                "Found",
                {},
                newurl,
            )
            passed = True
        except ValueError:
            passed = False
        if passed != should_pass:
            print(f"FAIL {name}")
            failures += 1

    total = (
        len(_VERSION_CASES)
        + 1
        + len(cases)
        + len(_SAFE_NAME_CASES)
        + 1
        + len(selection_cases)
        + len(_DESTINATION_CASES)
        + len(redirect_cases)
    )
    if failures:
        print(f"\n{failures} of {total} self-test case(s) failed.")
        return 1
    print(f"self-test: {total} of {total} cases passed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true", help="check this script")
    sub = parser.add_subparsers(dest="command")

    manifest = sub.add_parser("manifest", help="record a digest for every built file")
    manifest.add_argument("--dist", default="dist")
    manifest.add_argument("--out", default="dist/manifest.json")
    manifest.add_argument("--name", default=_NAME)
    manifest.add_argument("--version", default="")
    manifest.set_defaults(func=cmd_manifest)

    verify = sub.add_parser("verify", help="enumerate a release and verify it")
    verify.add_argument("--index-url", required=True)
    verify.add_argument("--name", default=_NAME)
    verify.add_argument("--version", required=True)
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--download-dir", required=True)
    verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if not getattr(args, "func", None):
        parser.print_usage(sys.stderr)
        return 2
    result = args.func(args)
    assert isinstance(result, int)
    return result


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
