#!/usr/bin/env python3
"""Git hook checks for pytest-graphql.

Enforces the mechanical subset of the hard rules in ``AGENTS.md`` at commit
time. ``AGENTS.md`` states the rules. ``COMMIT_MESSAGE_RULES.md`` under
``docs/reference/`` states how a commit message line is measured, and
``GIT_HOOKS.md`` beside it describes the hooks themselves. This checks policy,
not code quality. Lint, type checking and tests stay in
the handoff mechanical checks and in CI, so this stays fast enough to run on
every commit.

Usage::

    python3 scripts/git_hook_checks.py pre-commit
    python3 scripts/git_hook_checks.py pre-merge-commit
    python3 scripts/git_hook_checks.py pre-applypatch
    python3 scripts/git_hook_checks.py prepare-commit-msg <message-file> [source]
    python3 scripts/git_hook_checks.py commit-msg <message-file>
    python3 scripts/git_hook_checks.py applypatch-msg <message-file>
    python3 scripts/git_hook_checks.py --self-test

Exit codes: 0 clean, 1 findings reported, 2 usage or internal error.

Stdlib only, and no project environment, so it runs before the project skeleton
exists. Git values are passed to ``subprocess`` as argument lists and never
through a shell, so a branch name or a path is data and is never executable
text.

Four decisions are worth stating, because each looks stricter than it needs
to be and each closes a real bypass:

- Every unit these rules name is defined here, in git's terms, and never
  borrowed from a language primitive that is nearly the same thing. A line ends
  at a line feed, because that is where git ends one, and ``str.splitlines``
  ends one at eleven further characters that git keeps inside a line. A line is
  blank when git would see nothing on it, and ``str.strip`` calls a row of
  non-breaking spaces empty when git commits and displays it as content.
  Trailing whitespace is the spaces and tabs an editor leaves, and ``str.rstrip``
  reaches past those into characters an author had to type. Each of those gaps
  was a bypass: a subject cut into short pieces at a form feed, a body line
  padded past the limit with ideographic spaces, and a whole template kept
  character for character with its line feeds swapped, which read as unchanged
  because both sides were cut at the same wrong places. ``message_lines`` and
  ``blank`` are the single answers, and nothing here splits or strips on its
  own.

  A definition answers the one question it is named for and no other. It may
  not fold in a caller's question, and above all not a question whose answer
  depends on something this hook cannot see. Git's cleanup mode is that case.
  It decides whether trailing whitespace is kept, and the hook is never told
  which mode is in force. Worse, git does not even apply it at a fixed time:
  a message given with ``-F`` is cleaned before this hook runs, and an edited
  message is cleaned after, so the same file contents mean 72 characters
  stored on one path and 92 on another. Every check here that meets a question
  like this answers it the same way, by refusing if any mode git could still
  apply would break the rule. ``message_lines`` broke that by removing
  trailing spaces while splitting, which is a cleanup answer wearing a
  splitter's name. Because it ran below ``message_views``, no view could see
  it or correct it, and ``full`` stopped being the verbatim view its own
  docstring described. Splitting now returns the line as written, so a line is
  measured as ``--cleanup=verbatim`` would store it. The cost is on the record
  in ``COMMIT_MESSAGE_RULES.md``: an edited subject padded past the limit
  with trailing whitespace is refused even though git's default cleanup would
  have trimmed it, and the refusal names the trailing whitespace so the author
  can see it.

  One step earlier than both of those is where the input arrives. A definition
  can only be exact about a value the program still holds. ``errors="replace"``
  is not a way of decoding bytes, it is a way of displaying them: every
  malformed sequence becomes one U+FFFD, so two inputs that differ arrive here
  identical and no comparison below can tell them apart. It sat on the commit
  message, the recorded template, the staged paths, the branch name and the
  comment string, and the byte-exact template comparison above rested on it, so
  one byte of git's own text could be swapped for a different one and the
  exemption survived the edit. ``decode_git`` is the single answer and it loses
  nothing. The loss belongs where a lossy view is itself the answer, which here
  is the moment a name is printed, and python's stderr already does that.
  Refusing malformed input at the boundary was the other option and it is
  wrong: git stores bytes, so a verbose commit whose diff touches a file that
  is not utf-8 is an ordinary commit, and refusing it would refuse the author
  for git's own text.
- The commit message is judged in several views, because git's cleanup mode
  decides what is actually committed and the hook cannot see that mode. Comment
  lines survive ``--cleanup=whitespace`` and ``--cleanup=verbatim``, which is
  what ``git commit -F`` uses by default, and are dropped by ``strip``. The
  subject rules therefore apply to the first line of the message as written and
  to the first line with comment lines removed. A message with no first line at
  all is refused here rather than left to git, because
  ``--allow-empty-message`` commits it. The attribution rule and the
  publication hygiene scan read the whole message, exactly as it is on disk,
  and are never truncated.
- Only one thing excuses a line from the blank-line rule and the column limit:
  git wrote it. Everything else is measured. The hook cannot learn who wrote a
  line by looking at it, because an author can type a comment character, a tab,
  a rename arrow, a scissors bar or a diff header as easily as git can, so no
  rule here reads a line's shape to decide that question. It reads evidence
  instead. ``prepare-commit-msg`` runs before the editor opens, and on the one
  path where the file then holds git's template and nothing else it records
  those lines under the git directory. ``commit-msg`` then asks one question:
  is that whole recording still there, unchanged, at the end of the message?
  If it is, those lines are git's and every line above them is the author's.
  If any of it was deleted, edited or moved, the author has been inside git's
  text, and then nothing in the message is excused at all. Part of a recording
  excuses nothing, because a line git left behind and the same line typed by
  hand are the same bytes. That matters most under ``-v``, where the recording
  holds the staged diff and the author chose its content.

  That path is a plain ``git commit`` opening an editor. Every other source is
  refused a recording, because in each of them the file already holds words the
  author chose: ``-m`` and ``-F`` under ``message``, ``git merge -m`` under
  ``merge``, and a configured template, a squash or a reused message under the
  rest. Git reports the same ``merge`` source for its own message and for
  ``-m``, so neither can be excused. With no recording, which is also what
  ``git am`` and a failed write give, every line is the author's. That refuses
  more than it should rather than less, and the cost is a merge, a squash or a
  reused message committed through an editor where git's own template lists a
  path past the column limit.
- An exemption from the column limit is granted for one further reason: the
  text cannot be wrapped. Unlike who wrote a line, that question can be
  answered from the line itself, so it is answered from the line and not from
  a substitute for it. A line can be wrapped when it holds a break point at or
  before the limit, and a break point is any whitespace except the spaces whose
  whole meaning is "do not break here". Asking for the space character alone,
  as this once did, made tab-separated prose look like a single long token.

  A break point also needs something before it on the line to keep. Whitespace
  before the first word is indentation, and breaking there leaves an empty line
  above the same long token, so it shortens nothing. Asking only whether the
  character is whitespace made indentation look like a break, and one leading
  space and two leading spaces then gave opposite answers about the same token.

  Two further exemptions are declarations rather than measurements. Four
  leading spaces and a closed fenced block both say "this text is preformatted,
  rewrapping it would change it", and both markers are documented in
  ``COMMIT_MESSAGE_RULES.md`` together with what they cost. Fences are parsed
  by the one parser this repository has, in ``check_publication_hygiene``, so a
  fence
  closes here on the same terms it closes there. A merge, a revert or an
  autosquash subject is excused from the conventional form alone, and that is
  the last rule that reads a shape to decide who wrote something. Its cost is
  stated in ``COMMIT_MESSAGE_RULES.md``: those subjects are the ones git
  generates, and their bodies follow the same layout rules as any other body.

- Protected-branch policy has no exception for a merge, a cherry-pick, a
  revert or a mailed patch, and it runs on every path that creates a commit.
  Git runs ``pre-commit`` for ``git commit`` alone. It runs ``pre-merge-commit``
  for a merge that does not conflict, and it runs ``prepare-commit-msg`` for
  all of them, including a cherry-pick and a revert, which reach no other hook.
  ``git am`` reaches none of those four: it runs ``applypatch-msg`` before it
  applies the patch and ``pre-applypatch`` before it commits. The branch check
  therefore runs from ``prepare-commit-msg`` and from both applypatch hooks, and
  the staged checks run from ``pre-applypatch`` as well. A rebase and a bisect
  detach HEAD, which leaves no branch name and is skipped for that reason
  alone.

Overrides exist for the maintainer only. An agent may not set them and may not
use ``git commit --no-verify``:

- ``PYTEST_GQL_ALLOW_MAIN=1`` permits a commit on ``main`` or ``master``.
- ``PYTEST_GQL_ALLOW_SPEC_EDIT=1`` permits a staged change to the historical
  specification, including its deletion or rename.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_publication_hygiene as hygiene

PROTECTED_BRANCHES = {"main", "master"}

BRANCH_PREFIXES = ("feat", "fix", "docs", "chore", "refactor", "test", "security")

# AGENTS.md: "a type prefix and a short hyphenated description". One segment,
# lowercase, hyphen separated. No length limit is enforced, because AGENTS.md
# states none.
BRANCH_DESCRIPTOR_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Conventional-commits types. The branch prefixes plus the standard remainder.
COMMIT_TYPES = (
    "feat",
    "fix",
    "docs",
    "chore",
    "refactor",
    "test",
    "security",
    "perf",
    "build",
    "ci",
    "style",
    "revert",
)

SUBJECT_MAX = 72
BODY_MAX = 72

# The rule an author must know before committing is in AGENTS.md: a subject at
# or under 72 characters, a body wrapped at 72, and two exemptions. How a line
# is measured, and how each exemption is earned and lost, is in the reference
# below. A refusal about a measurement names the reference rather than
# AGENTS.md, so the detail arrives at the moment it is needed.
COMMIT_RULES_DOC = "docs/reference/COMMIT_MESSAGE_RULES.md"

SUBJECT_RE = re.compile(
    r"^(?:" + "|".join(COMMIT_TYPES) + r")(?:\([a-z0-9][a-z0-9._/-]*\))?!?: \S"
)

TRAILER_RE = re.compile(r"^[A-Za-z][A-Za-z-]*: \S+$")

# The characters git's own cleanup treats as whitespace inside a commit
# message: C isspace() in the C locale, without the line feed that ends the
# line. Nothing outside this set is whitespace to git. A non-breaking space, an
# ideographic space and an en quad are content git stores and shows, so a line
# built from them is not a blank line and a line padded with them is not short.
GIT_BLANKS = " \t\v\f\r"

# The whitespace an author cannot wrap at. Every other whitespace character
# ends a word and so offers a break point. These three are spaces that exist
# to say "do not break here", so treating them as break points would measure a
# line the author deliberately joined.
NON_BREAKING_SPACES = "\u00a0\u2007\u202f"

# The documented marker for preformatted text in a commit body: four leading
# spaces, the same marker Markdown uses for an indented code block. It is an
# author declaration, not a measurement, and COMMIT_MESSAGE_RULES.md
# states its cost.
INDENT_MARKER = "    "

# Anchored at column zero, because a Git trailer is. A diff line carries a
# leading "+", "-" or space, so a verbose diff cannot trip this.
CO_AUTHOR_RE = re.compile(r"^co-authored-by\s*:", re.IGNORECASE)

# Subjects git writes itself. A merge, a revert and an autosquash marker are not
# authored text, so the conventional-commit rules do not apply to them. The
# hygiene and attribution rules still do.
GENERATED_SUBJECT_RE = re.compile(r'^(?:Merge |Revert "|fixup! |squash! |amend! )')

# Git's scissors line, matched exactly. It is recorded as part of the template
# when git writes it, and it grants nothing on its own, because an author can
# type the same bar.
SCISSORS_BAR = "-" * 24 + " >8 " + "-" * 24

# The file under the git directory where prepare-commit-msg records the message
# as git wrote it, before the author can edit it. commit-msg reads it and
# removes it, so a recording is never reused by a later commit.
TEMPLATE_STATE = "pytest-graphql-commit-template"

# The prepare-commit-msg sources where git composes the whole message file on
# its own. There is exactly one: an absent source, which is a plain "git commit"
# opening an editor on git's template.
#
# Every other source is left unrecorded, and each for the same reason. Under
# "message" the file holds text the author gave with "-m" or "-F". Under
# "merge" it holds the message of "git merge -m", which is author text too, and
# git reports the same source for a merge it wrote itself, so the two cannot be
# told apart. "template", "squash" and "commit" all mix in text that reaches
# git from outside this commit. Recording any of them would exempt words the
# author chose.
#
# The cost is a false refusal: one of those sources, combined with an editor
# and a staged path past the column limit, is refused because git's template is
# measured. That errs toward refusing, and the maintainer overrides exist for
# the case where it is wrong.
GENERATED_SOURCES = ("",)

# A text blob larger than this is reported rather than scanned. Nothing in this
# repository approaches it, so reaching it means something unexpected is being
# committed, and silence would be worse than a refusal. Binary content is
# classified first and is never measured against this limit.
MAX_SCAN_BYTES = 8 * 1024 * 1024

# Git decides whether a blob is binary from its first bytes. The hook reads the
# same window, so a large image, archive or fixture is skipped on its content
# rather than refused on its size.
BINARY_PROBE_BYTES = 8000

SECRET_EXEMPT_SUFFIXES = (".example", ".template", ".sample")
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")
SECRET_NAMES = {".env", ".netrc", ".pypirc", "credentials.json"}
SECRET_PREFIXES = (".env.", "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa")

LOCAL_ONLY_FILES = {"IMPLEMENTATION_PLAN.md", "PLAN.md"}

SPEC_PATH = "docs/reference/SPEC.md"
DECISIONS_PATH = "docs/reference/DESIGN_DECISIONS.md"


def enabled(name: str) -> bool:
    return os.environ.get(name, "0") == "1"


def decode_git(raw: bytes) -> str:
    """Bytes from git as text, losing nothing.

    Git stores bytes. A branch name, a path, a config value and a commit
    message are all byte strings, and none of them is promised to be utf-8.
    ``errors="replace"`` is not a way of decoding them, it is a way of
    displaying them: it maps every malformed sequence onto one U+FFFD, so two
    inputs that differ arrive here identical. Every question this file then
    asks about identity, whether this is the template git recorded, whether
    this is the path git staged, whether this is the configured comment
    string, has already lost the evidence that would answer it.

    ``surrogateescape`` keeps each byte that is not utf-8 as one lone
    surrogate, so the text round-trips back to the original bytes and two
    different inputs stay different. The loss belongs at the point where a
    lossy view is the answer, which here is the moment a name is printed. See
    ``printable``.
    """
    return raw.decode("utf-8", errors="surrogateescape")


def git_bytes(*args: str) -> bytes:
    result = subprocess.run(["git", *args], capture_output=True, check=True)
    return result.stdout


def git_text(*args: str) -> str:
    return decode_git(git_bytes(*args))


def current_branch() -> str:
    """The checked-out branch, or an empty string when HEAD is detached.

    A rebase and a bisect detach HEAD, so both leave branch policy with nothing
    to check. A merge, a cherry-pick and a revert stay on the branch and are
    checked like any other commit.
    """
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "-q", "HEAD"], capture_output=True
    )
    if result.returncode != 0:
        return ""
    return decode_git(result.stdout).strip()


def git_path(name: str) -> Path | None:
    """A file under the git directory of this worktree, or None.

    ``git rev-parse --git-path`` resolves a linked worktree to its own
    directory, so two worktrees never share a recording.
    """
    try:
        value = git_text("rev-parse", "--git-path", name).strip()
    except (subprocess.CalledProcessError, OSError):
        return None
    return Path(value) if value else None


def message_lines(text: str) -> list[str]:
    """The lines of a commit message, split where git ends a line.

    Git ends a line at a line feed and nowhere else. Python's ``splitlines``
    ends one at eleven further characters, among them the form feed, the
    vertical tab, U+0085, U+2028 and U+2029. Git stores every one of those
    inside a line, so splitting on them measured pieces that git will never
    treat as lines. An overlong subject read as two short ones, and a recording
    whose separators were all swapped read as unchanged, because both sides
    were cut at the same wrong places.

    Splitting is all this does. It decodes bytes into lines and returns each
    line exactly as it was written, because every caller measures or compares
    the line and none of them can be given a line that has already been
    changed. Removing trailing spaces and tabs here was not a splitting rule at
    all. It answered a different question, "what will git keep when it applies
    its cleanup mode", and it answered it by guessing one mode. The hook cannot
    see that mode. ``--cleanup=verbatim`` keeps every trailing space, and
    ``git commit -F`` reaches it, so a subject of 72 characters followed by 20
    spaces was measured as 72 and committed as 92. Worse, the guess sat below
    ``message_views``, so ``full`` was not the verbatim view its own name
    promised, and a recorded template line with a space added to it still
    compared as unchanged.

    Removing nothing is also what keeps the recording usable. The recording is
    taken in ``prepare-commit-msg``, before the editor opens, and it is
    compared in ``commit-msg``. Both sides are read through here, so both are
    raw and a comparison between them is byte for byte. Trimming one side and
    not the other would have made an edit look like no edit.

    One carriage return directly before the line feed is dropped, and that is
    the only character removed. It is part of how a CRLF file encodes a line
    ending, so dropping it is decoding, not cleanup. The lenience is exactly
    one character: a second carriage return, or a carriage return with a space
    beside it, is content and is measured. Its cost is on the record in
    ``COMMIT_MESSAGE_RULES.md``: under ``--cleanup=verbatim`` git stores that
    carriage return, so a 72-character line in a CRLF file is stored as 73 and
    accepted.
    """
    lines = text.split("\n")
    if lines and not lines[-1]:
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def blank(line: str) -> bool:
    """True when git would see no content on this line.

    Blankness decides where a body begins and which runs of a recording may be
    ignored, so it has to mean what it means to git. ``str.strip`` answers for
    Unicode instead, and by that answer a line of non-breaking spaces is empty.
    To git it is content, and it is committed and displayed as content.
    """
    return not line.strip(GIT_BLANKS)


def record_template(message_path: str, source: str) -> None:
    """Record the message file as git wrote it, before the author sees it.

    ``prepare-commit-msg`` runs before the editor opens, so at this moment the
    file holds git's text and nothing else. Recording it here is the only way
    ``commit-msg`` can tell git's own template from text an author typed, and
    it is evidence rather than a guess about a line's shape.

    A source outside ``GENERATED_SOURCES`` records nothing and removes any
    earlier recording, so an abandoned editor commit cannot lend its exemption
    to the next ``git commit -F``.
    """
    state = git_path(TEMPLATE_STATE)
    if state is None:
        return

    payload: dict[str, object] | None = None
    if source in GENERATED_SOURCES and message_path:
        try:
            raw = decode_git(Path(message_path).read_bytes())
            payload = {
                "message": str(Path(message_path).resolve()),
                "lines": message_lines(raw),
            }
        except OSError:
            payload = None

    try:
        if payload is None:
            state.unlink(missing_ok=True)
        else:
            state.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as error:
        # Not fatal. Without a recording the message is judged as if the author
        # wrote every line of it, which refuses more than it should rather than
        # less.
        print(
            f"prepare-commit-msg: cannot record git's template: {error}",
            file=sys.stderr,
        )


def recorded_template(message_path: str) -> list[str]:
    """The lines git wrote into this message file, or an empty list.

    The recording is removed as it is read, so it is used by the one commit
    that produced it and never by a later one. Anything unreadable, malformed,
    or written for a different message file is treated as no recording at all,
    which measures every line.
    """
    state = git_path(TEMPLATE_STATE)
    if state is None:
        return []

    try:
        payload = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = None
    finally:
        with contextlib.suppress(OSError):
            state.unlink(missing_ok=True)

    if not isinstance(payload, dict):
        return []
    try:
        if payload.get("message") != str(Path(message_path).resolve()):
            return []
    except OSError:
        return []
    lines = payload.get("lines")
    if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
        return []
    return lines


def trim_blank(lines: list[str]) -> list[str]:
    """The lines with every blank line at either end removed."""
    start = 0
    end = len(lines)
    while start < end and blank(lines[start]):
        start += 1
    while end > start and blank(lines[end - 1]):
        end -= 1
    return lines[start:end]


def generated_start(lines: list[str], recorded: list[str]) -> int:
    """Index of the first line git wrote, or the length of the message.

    The recording is one object, not a pool of lines to draw from. Git appended
    all of it to the end of the file in one piece, so either all of it is still
    there, unchanged and still at the end, or the author edited git's text.

    A partial match proves an edit, and after an edit nothing in the message
    can be attributed to git any more. A line git left behind and the same line
    typed by hand are the same bytes, and the hook cannot tell them apart. That
    is not a small loss: with ``-v`` the recording holds the staged diff, whose
    content the author chose, so accepting a partial match would let an author
    stage any text, copy it under the subject, delete the rest of the template
    and have that text excused. Either the whole recording is intact or nothing
    is excused.

    Blank runs at two places are ignored on purpose. The recording's leading
    blank line is the space git leaves for the subject, and the author is meant
    to write over it. A blank line at the very end of either side is what an
    editor adds or drops on its own. Neither can be over the column limit and
    neither carries text, so ignoring them excuses nothing.

    An empty recording, or a recording that no longer matches, returns the
    length of the message, so every line is treated as authored. That is the
    answer whenever there is no evidence.
    """
    tail = trim_blank(recorded)
    if not tail:
        return len(lines)

    end = len(lines)
    while end > 0 and blank(lines[end - 1]):
        end -= 1

    start = end - len(tail)
    if start < 0 or lines[start:end] != tail:
        return len(lines)

    return start


def comment_prefix() -> str:
    """The configured comment character, defaulting to git's own default."""
    for key in ("core.commentString", "core.commentChar"):
        result = subprocess.run(["git", "config", "--get", key], capture_output=True)
        value = decode_git(result.stdout).strip()
        if result.returncode == 0 and value and value != "auto":
            return value
    return "#"


def check_branch(branch: str) -> list[str]:
    """Branch policy. An empty branch name means detached HEAD, which is skipped."""
    if not branch:
        return []

    if branch in PROTECTED_BRANCHES:
        if enabled("PYTEST_GQL_ALLOW_MAIN"):
            return []
        return [
            f"refusing to commit on {branch!r}. AGENTS.md forbids it. "
            "Create a typed branch first."
        ]

    head, slash, rest = branch.partition("/")
    if head not in BRANCH_PREFIXES or not slash:
        allowed = ", ".join(f"{prefix}/" for prefix in BRANCH_PREFIXES)
        return [
            f"branch {branch!r} has no valid type prefix. "
            f"Use one of: {allowed} followed by a short hyphenated description."
        ]

    if not BRANCH_DESCRIPTOR_RE.match(rest):
        return [
            f"branch {branch!r} does not end in a short hyphenated description. "
            "Use lowercase letters and digits separated by single hyphens, "
            "with no further slash."
        ]

    return []


def looks_like_secret(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    lowered = base.lower()

    if lowered.endswith(SECRET_EXEMPT_SUFFIXES):
        return False
    if lowered in SECRET_NAMES:
        return True
    if lowered.startswith(SECRET_PREFIXES):
        return True
    return lowered.endswith(SECRET_SUFFIXES)


def check_path(path: str, *, present: bool) -> list[str]:
    """Path policy for one staged path.

    ``present`` is True when the path exists in the tree this commit produces.
    A deletion and a rename source are absent, so the rules that keep a file out
    of the repository do not apply to them: removing such a file is the remedy,
    not the offence. The specification guard applies either way, because moving
    or deleting the historical record is exactly what it exists to prevent.
    """
    problems: list[str] = []

    if path == SPEC_PATH and not enabled("PYTEST_GQL_ALLOW_SPEC_EDIT"):
        problems.append(
            f"refusing to commit a change to {path!r}: the specification is the "
            f"historical record. A design change is made in {DECISIONS_PATH}."
        )

    if not present:
        return problems

    if path == "tmp" or path.startswith("tmp/"):
        problems.append(
            f"refusing to commit {path!r}: review handoff logs and local "
            "coordination state are never committed."
        )

    if path in LOCAL_ONLY_FILES:
        problems.append(
            f"refusing to commit {path!r}: local planning file, never "
            "version-controlled."
        )

    if looks_like_secret(path):
        problems.append(
            f"refusing to commit {path!r}: the name looks like a credential or "
            "a private key."
        )

    return problems


def parse_name_status(raw: bytes) -> list[tuple[str, list[str]]]:
    """Parse NUL-delimited ``git diff --cached --name-status -z`` records.

    A record is a status field followed by one path, or by two paths when the
    status is a rename or a copy.
    """
    fields = [decode_git(field) for field in raw.split(b"\0")]
    if fields and fields[-1] == "":
        fields.pop()

    records: list[tuple[str, list[str]]] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        if not status:
            raise ValueError("empty status field in git diff output")
        wanted = 2 if status[0] in ("R", "C") else 1
        paths = fields[index + 1 : index + 1 + wanted]
        if len(paths) != wanted:
            raise ValueError(f"truncated record for status {status!r}")
        records.append((status[0], paths))
        index += 1 + wanted

    return records


def staged_paths() -> list[tuple[str, bool]]:
    """Every path this commit touches, paired with whether it survives the commit.

    A rename contributes both of its paths: the source is absent, the
    destination is present.
    """
    records = parse_name_status(git_bytes("diff", "--cached", "--name-status", "-z"))

    paths: list[tuple[str, bool]] = []
    for status, record in records:
        if status in ("R", "C"):
            paths.append((record[0], False))
            paths.append((record[1], True))
        elif status in ("D", "U"):
            # A deletion has no blob. An unmerged path has no stage-zero blob.
            paths.append((record[0], False))
        else:
            paths.append((record[0], True))

    return paths


def read_staged_blob(path: str, size: int) -> tuple[bytes | None, str]:
    """Read a staged blob far enough to classify it.

    Returns the bytes to scan, or ``None`` and a reason. Classification comes
    before the scan limit, so a binary artifact of any size is skipped instead
    of refused. Only text is measured against the limit, and the blob is read
    in two steps so an oversized one never has to be held in memory.
    """
    process = subprocess.Popen(
        ["git", "cat-file", "blob", f":{path}"],
        stdout=subprocess.PIPE,
        # Closing the pipe early ends git with a broken pipe. That is expected
        # here, so its message would only be noise inside a hook. The size call
        # above already proved the blob is readable.
        stderr=subprocess.DEVNULL,
    )
    stream = process.stdout
    if stream is None:  # pragma: no cover - Popen with a pipe always sets it
        return None, "oversize"

    try:
        head = stream.read(BINARY_PROBE_BYTES)
        if b"\0" in head:
            return None, "binary"
        if size > MAX_SCAN_BYTES:
            return None, "oversize"
        return head + stream.read(), ""
    finally:
        stream.close()
        process.wait()


def scan_staged_content(paths: list[tuple[str, bool]]) -> list[str]:
    """Publication hygiene over staged blobs, not the working tree.

    A partial ``git add`` is checked as it will actually be committed. Every
    surviving blob is classified and every text blob is read. Binary content is
    skipped by the same NUL rule the scanner itself uses, and the suffix decides
    only whether the Markdown code exemptions apply.
    """
    problems: list[str] = []

    for path, present in paths:
        if not present:
            continue

        size = int(git_text("cat-file", "-s", f":{path}").strip() or 0)
        raw, reason = read_staged_blob(path, size)

        if reason == "binary":
            continue  # not a text artifact, whatever its size
        if reason == "oversize" or raw is None:
            problems.append(
                f"refusing to commit {path!r}: {size} bytes of text is too large "
                f"to scan for publication hygiene. The limit is "
                f"{MAX_SCAN_BYTES} bytes."
            )
            continue

        markdown = Path(path).suffix.lower() in hygiene.MARKDOWN_SUFFIXES
        text = decode_git(raw)
        problems.extend(
            finding.render()
            for finding in hygiene.scan_text(path, text, markdown=markdown)
        )

    if problems:
        problems.append(
            "publication hygiene findings in staged content. Fix them, restage, "
            "and commit again."
        )
    return problems


def staged_checks() -> list[str]:
    """Branch, path and content policy for whatever is staged right now."""
    problems: list[str] = []

    problems.extend(check_branch(current_branch()))

    paths = staged_paths()
    for path, present in paths:
        problems.extend(check_path(path, present=present))

    problems.extend(scan_staged_content(paths))

    return problems


def pre_commit() -> int:
    return report("pre-commit", staged_checks())


def pre_merge_commit() -> int:
    """The same checks for a merge that does not conflict.

    Git runs ``pre-commit`` only for ``git commit``. A merge that applies
    cleanly commits without it, and reaches this hook instead. A merge that
    conflicts is finished by ``git commit`` and reaches ``pre-commit``.
    """
    return report("pre-merge-commit", staged_checks())


def pre_applypatch() -> int:
    """The same checks for a patch ``git am`` has already applied.

    ``git am`` runs none of the commit hooks. This one is its counterpart to
    ``pre-commit``: the patch is in the index and the commit has not been made
    yet, so the staged path and content rules apply unchanged.
    """
    return report("pre-applypatch", staged_checks())


def prepare_commit_msg(path: str, source: str) -> int:
    """Branch policy, and a recording of the message as git wrote it.

    A cherry-pick and a revert that apply cleanly run neither ``pre-commit``
    nor ``commit-msg``. This hook is the only one they run, so the protected
    branch rule lives here as well. Nothing else is checked here, because a
    plain commit has already been through ``pre-commit`` by this point.

    A refusal here leaves the applied change in the index. Recovering from it
    is the same work as recovering from a refused ``git commit``: switch to a
    typed branch and commit, or discard the change.

    This hook also runs before the editor opens, which makes it the only point
    where git's own template can be seen without the author's text mixed into
    it. Recording it here is what lets ``commit-msg`` excuse git's lines
    without guessing from their shape.
    """
    record_template(path, source)
    return report("prepare-commit-msg", check_branch(current_branch()))


class MessageViews(NamedTuple):
    """One commit message, seen the several ways git may commit it."""

    full: list[str]
    """Every line. What ``--cleanup=verbatim`` commits, and what is scanned."""

    authored: list[str]
    """Every line git did not write. The view the author is answerable for."""

    stripped: list[str]
    """The authored view without comment lines. What ``--cleanup=strip`` commits."""


def message_views(text: str, prefix: str, recorded: list[str]) -> MessageViews:
    """Split the message into what git wrote and what the author wrote.

    The split is the recorded suffix and nothing else. No line is classified by
    its shape, so a comment character, a tab, a scissors bar or a diff header
    an author types carries no weight at all.
    """
    lines = message_lines(text)
    authored = lines[: generated_start(lines, recorded)]

    return MessageViews(
        full=trim_blank(lines),
        authored=trim_blank(authored),
        stripped=trim_blank([line for line in authored if not line.startswith(prefix)]),
    )


def break_point(char: str) -> bool:
    """True when a line may be wrapped at this character.

    Any whitespace ends a word and so offers a place to break: a space, a tab,
    a form feed. A non-breaking space does not, because that is the one thing
    the character means. Asking for whitespace is the question the column rule
    actually asks. Asking for one particular character, as this used to, left
    every other break point looking like part of a single long token.
    """
    return char.isspace() and char not in NON_BREAKING_SPACES


def breakable(line: str) -> bool:
    """True when the line can be broken into a first line within the limit.

    A break point exists only where there is something before it to keep.
    Whitespace before the first word is indentation: breaking there leaves an
    empty line above and the same long token below, so it shortens nothing.
    Asking only "is this character whitespace" counted indentation as a break,
    which is why one leading space and two leading spaces gave opposite answers
    about the same long token.

    The break has to fall at or before the limit, because the text before it is
    the line that stays.
    """
    content = False
    for char in line[: BODY_MAX + 1]:
        if break_point(char):
            if content:
                return True
        elif not char.isspace():
            content = True
    return False


def wrappable(line: str) -> bool:
    """True when the line is over the limit and had somewhere to break.

    A line with no break point at all is a single long token, such as a URL or
    a trailer value. It cannot be wrapped and it is not a violation, and
    indenting that token by one space or by three does not change that.

    Two exemptions above it are author declarations rather than measurements.
    An indented line and a closed fenced block say "this text is preformatted,
    rewrapping it would change it", and the repository documents both markers
    in ``COMMIT_MESSAGE_RULES.md``. The cost is on the record there: four
    leading spaces turn the column limit off for that line, exactly as a fence
    does for its block.
    """
    if len(line) <= BODY_MAX:
        return False
    if line.startswith(INDENT_MARKER):
        return False  # an indented block: pasted output or code
    if TRAILER_RE.match(line):
        return False
    return breakable(line)


def trailing_note(line: str, limit: int) -> str:
    """An explanation, when trailing whitespace is why a line is over the limit.

    Trailing whitespace is measured because git stores it under
    ``--cleanup=verbatim``, but nothing an author looks at shows it. An editor
    draws it as empty space, and ``git log`` trims it from the subject it
    prints, so ``git log`` displays 72 characters for a line git stored as 92.
    A bare count would therefore look wrong to the one person who has to fix
    it, and this names the cause.

    It is a message and never a measurement. It is added only when removing the
    trailing whitespace would bring the line within the limit, so it never
    explains away a line that is too long on its own, and the refusal stands
    either way. The set removed here is git's own, because the question is what
    git's other cleanup modes would have taken off.
    """
    trimmed = line.rstrip(GIT_BLANKS)
    if len(trimmed) == len(line) or len(trimmed) > limit:
        return ""
    count = len(line) - len(trimmed)
    plural = "" if count == 1 else "s"
    return (
        f" Its last {count} character{plural} {'is' if count == 1 else 'are'} "
        "trailing whitespace, which git keeps under '--cleanup=verbatim'."
    )


def check_subject(subject: str, qualifier: str) -> list[str]:
    if GENERATED_SUBJECT_RE.match(subject):
        return []

    problems: list[str] = []

    if len(subject) > SUBJECT_MAX:
        problems.append(
            f"subject{qualifier} is {len(subject)} characters. "
            f"The limit is {SUBJECT_MAX}."
            + trailing_note(subject, SUBJECT_MAX)
            + f" {COMMIT_RULES_DOC} states how a line is measured."
        )

    if not SUBJECT_RE.match(subject):
        allowed = ", ".join(COMMIT_TYPES)
        problems.append(
            f"subject {subject!r}{qualifier} is not a conventional commit. "
            f"Use '<type>: <description>' with one of: {allowed}."
        )

    return problems


def check_body(lines: list[str]) -> list[str]:
    """The blank line after the subject, on the lines the author wrote.

    Git's own editor template starts on the line under the subject, so it would
    read as a body with no blank line before it. It is removed from this view
    because git wrote it, not because it starts with a comment character. A
    comment line the author typed stays, and ``--cleanup=verbatim`` commits it,
    so it counts as the body and needs the blank line like any other.
    """
    if len(lines) > 1 and not blank(lines[1]):
        return [
            "leave a blank line between the subject and the body. "
            f"{COMMIT_RULES_DOC} states what counts as blank."
        ]

    return []


def fenced_lines(lines: list[str]) -> set[int]:
    """The line numbers inside a closed fenced block, counting from one.

    A fenced block cannot be wrapped, so it is exempt. The repository already
    has one fence parser, in ``check_publication_hygiene``, and this uses it
    rather than repeating a weaker copy: a closer must use the same character,
    run at least as long as the opener, and carry no info string, and an
    opener may be indented by up to three spaces and may use tildes.

    A fence that never closes is not a block. It is one line an author typed,
    so only the lines between an opener and a valid closer are exempt.
    """
    exempt: set[int] = set()
    fence: hygiene.Fence | None = None
    opened_at = 0

    for number, line in enumerate(lines, start=1):
        if fence is None:
            found = hygiene.open_fence(line)
            if found is not None:
                fence, opened_at = found, number
        elif hygiene.closes_fence(line, fence):
            exempt.update(range(opened_at, number + 1))
            fence = None

    return exempt


def check_layout(lines: list[str]) -> list[str]:
    """The column limit, on every line the author wrote.

    Nothing here asks what a line looks like. A comment line, a line after a
    tab, a line below a scissors bar and a line that starts like diff output
    are all measured, because an author can type any of them. The only lines
    that escape are the ones git recorded as its own before the author could
    edit the file, and those never reach this view.

    This runs on every message, whatever the subject looks like. A subject is
    text an author can type, so it cannot turn a body rule off.
    """
    problems: list[str] = []
    exempt = fenced_lines(lines)

    for number, line in enumerate(lines[1:], start=2):
        if number in exempt:
            continue
        if wrappable(line):
            problems.append(
                f"body line {number} is {len(line)} characters. "
                f"Wrap the body at {BODY_MAX}."
                + trailing_note(line, BODY_MAX)
                + f" {COMMIT_RULES_DOC} states how a line is measured and"
                " when one is exempt."
            )

    return problems


def check_message(
    text: str, *, prefix: str = "#", recorded: list[str] | None = None
) -> list[str]:
    """Message policy, given whatever git recorded of its own template.

    ``recorded`` is empty whenever there is no evidence, and then every line is
    judged as the author's. That is the strict direction, and it is what the
    ``git am`` path gets, because a mailed message carries no template.
    """
    problems: list[str] = []
    views = message_views(text, prefix, recorded or [])

    subjects: list[tuple[str, str]] = []
    for view, lines in (
        ("as written", views.full),
        ("after comment removal", views.stripped),
    ):
        subject = lines[0] if lines else ""
        if subject and subject not in [existing for _, existing in subjects]:
            subjects.append((view, subject))

    if not subjects:
        # Not left to git. It aborts an empty message by default, but
        # "--allow-empty-message" commits one, and a message of invisible
        # characters alone reaches here empty as well.
        problems.append(
            "the message has no subject line. "
            "Write '<type>: <description>' on the first line."
        )

    for view, subject in subjects:
        qualifier = f" ({view})" if len(subjects) > 1 else ""
        problems.extend(check_subject(subject, qualifier))

    # The generated-subject exemption lives in check_subject and nowhere else.
    # A merge, a revert and an autosquash marker are subjects git writes and
    # they do not follow the conventional form, but their bodies follow the
    # same layout rules as any other body, and the subject is text an author
    # can type. Running these two checks always costs a revert or a merge whose
    # body carries an overlong wrappable line, which the author wraps by hand.
    #
    # Both read the authored view, which is the message minus the suffix git
    # recorded as its own. That is the one place a line is excused, and it
    # rests on evidence taken before the author could edit the file.
    problems.extend(check_body(views.authored))
    problems.extend(check_layout(views.authored))

    # Attribution is judged on the whole message, with no truncation. A comment
    # line is committed under the cleanup modes "git commit -F" uses, and the
    # hook cannot see which mode git will apply.
    for number, line in enumerate(views.full, start=1):
        if CO_AUTHOR_RE.match(line):
            problems.append(
                f"line {number} carries a Co-Authored-By trailer. "
                "This repository does not use it."
            )

    # Hygiene reads the raw message, not a view of it. No truncation, and no
    # rstrip either, because a trailing invisible character survives every
    # cleanup mode and rstrip would hide it. A scissors bar is text an author
    # can type, and git applies its cleanup mode after this hook runs, so
    # anything below the bar may still be committed. The cost is that under
    # commit.verbose the diff git appends is scanned as well, and a prohibited
    # character inside it refuses the commit even though git would have dropped
    # it. That errs toward refusing. The remedy is to commit without the diff,
    # with "git -c commit.verbose=false commit".
    problems.extend(
        finding.render()
        for finding in hygiene.scan_text("<commit message>", text, markdown=False)
    )

    return problems


def message_hook(hook: str, path: str, *, branch: bool, template: bool) -> int:
    """Message policy from a hook that git hands a message file."""
    try:
        text = decode_git(Path(path).read_bytes())
    except OSError as error:
        print(f"{hook}: cannot read {path}: {error}", file=sys.stderr)
        return 2

    recorded = recorded_template(path) if template else []
    problems = check_branch(current_branch()) if branch else []
    problems.extend(check_message(text, prefix=comment_prefix(), recorded=recorded))
    return report(hook, problems)


def commit_msg(path: str) -> int:
    return message_hook("commit-msg", path, branch=False, template=True)


def applypatch_msg(path: str) -> int:
    """Branch and message policy for a mailed patch, before it is applied.

    This is the first hook ``git am`` runs, and a refusal here stops the import
    before the index or the working tree changes. The branch check runs here as
    well as in ``pre-applypatch``, so the cheap refusal comes first.

    A mailed message carries no template and reaches no ``prepare-commit-msg``,
    so nothing is recorded and every line is judged as the author's.
    """
    return message_hook("applypatch-msg", path, branch=True, template=False)


def report(hook: str, problems: list[str]) -> int:
    """Print every problem and say whether the commit is refused.

    A path or a branch name that is not utf-8 reaches here as lone surrogates,
    which utf-8 cannot encode. Nothing escapes them here because nothing needs
    to: python gives ``sys.stderr`` the ``backslashreplace`` error handler by
    default, so the byte is written as an escape and the refusal still reaches
    the author. Do not reconfigure this stream with strict errors.
    """
    for problem in problems:
        print(f"{hook}: {problem}", file=sys.stderr)
    if problems:
        print(f"{hook}: commit refused.", file=sys.stderr)
        return 1
    return 0


BRANCH_CASES: list[tuple[str, str, bool]] = [
    ("detached head is skipped", "", False),
    ("main is refused", "main", True),
    ("master is refused", "master", True),
    ("typed branch is accepted", "feat/m0-skeleton", False),
    ("security prefix is accepted", "security/header-escaping", False),
    ("single word description is accepted", "chore/hooks", False),
    ("digits are accepted", "feat/m5c-lifecycle", False),
    ("bare prefix is refused", "feat/", True),
    ("no prefix is refused", "my-work", True),
    ("unknown prefix is refused", "wip/thing", True),
    ("underscore is refused", "feat/not_hyphenated", True),
    ("nested slash is refused", "feat/topic/extra", True),
    ("uppercase is refused", "feat/Topic", True),
    ("double hyphen is refused", "feat/a--b", True),
    ("leading hyphen is refused", "feat/-a", True),
    ("trailing hyphen is refused", "feat/a-", True),
    ("dot is refused", "feat/a.b", True),
]

PATH_CASES: list[tuple[str, str, bool, bool]] = [
    ("review log is refused", "tmp/reviews/x/REVIEW_LOG.md", True, True),
    ("nested tmp elsewhere is allowed", "src/pytest_graphql/tmp/keep.py", True, False),
    ("local plan is refused", "PLAN.md", True, True),
    ("local implementation plan is refused", "IMPLEMENTATION_PLAN.md", True, True),
    ("specification change is refused", SPEC_PATH, True, True),
    ("specification deletion is refused", SPEC_PATH, False, True),
    ("decisions edit is allowed", DECISIONS_PATH, True, False),
    ("dotenv is refused", ".env", True, True),
    ("dotenv variant is refused", "config/.env.production", True, True),
    ("dotenv example is allowed", ".env.example", True, False),
    ("private key is refused", "certs/server.pem", True, True),
    ("ssh key is refused", "id_rsa", True, True),
    ("removing a secret is allowed", "certs/server.pem", False, False),
    ("removing a review log is allowed", "tmp/reviews/x/REVIEW_LOG.md", False, False),
    ("ordinary source is allowed", "src/pytest_graphql/client.py", True, False),
]

NAME_STATUS_CASES: list[tuple[str, bytes, list[tuple[str, bool]]]] = [
    ("addition", b"A\x00new.txt\x00", [("new.txt", True)]),
    ("modification", b"M\x00keep.txt\x00", [("keep.txt", True)]),
    ("deletion", b"D\x00gone.txt\x00", [("gone.txt", False)]),
    ("type change", b"T\x00link.txt\x00", [("link.txt", True)]),
    (
        "rename reports both paths",
        b"R100\x00old.txt\x00new.txt\x00",
        [("old.txt", False), ("new.txt", True)],
    ),
    (
        "copy reports both paths",
        b"C075\x00src.txt\x00copy.txt\x00",
        [("src.txt", False), ("copy.txt", True)],
    ),
    (
        "several records",
        b"D\x00a\x00M\x00b\x00R100\x00c\x00d\x00",
        [("a", False), ("b", True), ("c", False), ("d", True)],
    ),
    ("a path with a space", b"A\x00a file.txt\x00", [("a file.txt", True)]),
    ("empty output", b"", []),
]

SCISSORS = "# " + SCISSORS_BAR
LONG_DIR = "a-directory-with-a-long-name/another-one/and-another-one/"

# Git's editor template, written the way git writes it: a blank first line for
# the subject, then comment lines, one of which lists a path past the column
# limit. It is used twice, once as bytes an author transcribed, where it is
# measured, and once as bytes git recorded, where it is excused.
TEMPLATE = (
    "\n"
    "# Please enter the commit message for your changes. Lines starting\n"
    "# with '#' will be ignored, and an empty message aborts the commit.\n"
    "#\n"
    "# On branch chore/git-hooks\n"
    "#\n"
    "# Changes to be committed:\n"
    "#\tnew file:   " + LONG_DIR + "a-file-with-a-long-name.md\n"
    "#\n"
)

# The same template with the block "commit.verbose" appends, including a diff
# line past the column limit.
VERBOSE_TEMPLATE = (
    TEMPLATE
    + SCISSORS
    + "\n"
    + "# Do not modify or remove the line above.\n"
    + "# Everything below it will be ignored.\n"
    + "diff --git a/x.md b/x.md\n"
    + "index 0000000..1111111 100644\n"
    + "--- a/x.md\n+++ b/x.md\n@@ -1 +1 @@\n-"
    + "word " * 20
    + "\n+one\n"
)

# The same verbose block carrying a byte that is not utf-8. Git stores bytes,
# so a diff of a file that is not utf-8 puts them in the message file, and the
# recording and the final message both hold them. The second fixture changes
# only that byte, to a truncated sequence. The two byte strings differ, but
# under errors="replace" both rendered as one U+FFFD and compared equal, so the
# edit kept the exemption it should have cost.
RAW_BYTE_TEMPLATE = VERBOSE_TEMPLATE.replace("+one\n", decode_git(b"+one \xff\n"), 1)
RAW_BYTE_EDITED = VERBOSE_TEMPLATE.replace("+one\n", decode_git(b"+one \xe2\x82\n"), 1)

# decode_git has to return text that goes back to the bytes it came from. The
# template comparison, the staged-path lookup and the comment prefix all rest
# on that, because each of them asks whether two values are the same thing.
DECODE_CASES: list[tuple[str, bytes]] = [
    ("plain ascii", b"feat: a subject\n"),
    ("valid utf-8", "naive cafe\u0301\n".encode("utf-8")),
    ("one malformed byte", b"# a line \xff here\n"),
    ("a truncated sequence", b"# a line \xe2\x82 here\n"),
    ("a malformed byte at the end", b"# a line\xff"),
    ("every byte value", bytes(range(256))),
]


# A break point is any whitespace that is not a non-breaking space. These cases
# read wrappable() on its own, because the column rule turns on this one
# question and a message-level case cannot show which whitespace was found.
WRAP_CASES: list[tuple[str, str, bool]] = [
    ("a short line is never wrappable", "word " * 5, False),
    ("spaces are break points", "word " * 20, True),
    ("tabs are break points", "\t".join(["word"] * 20), True),
    ("one tab is enough", "x" * 60 + "\t" + "y" * 40, True),
    ("a form feed is a break point", "x" * 60 + "\f" + "y" * 40, True),
    ("a vertical tab is a break point", "x" * 60 + "\v" + "y" * 40, True),
    ("mixed tabs and spaces are break points", "word\tword " * 12, True),
    ("a single long token is not wrappable", "https://example.com/" + "a" * 90, False),
    (
        "a non-breaking space is not a break point",
        "word\u00a0" * 20,
        False,
    ),
    (
        "a narrow non-breaking space is not a break point",
        "word\u202f" * 20,
        False,
    ),
    (
        "a break point past the limit does not count",
        "x" * 80 + " " + "y" * 10,
        False,
    ),
    (
        "a break point at column one does not count",
        " " + "x" * 90,
        False,
    ),
    # Zero through four leading spaces before one long token. Indentation is
    # not a break point, so every width answers the same way, and the fourth
    # space is a declaration that answers the same way for a different reason.
    ("a bare long token is not wrappable", "x" * 90, False),
    ("a long token indented by one space is not wrappable", " " + "x" * 90, False),
    ("a long token indented by two spaces is not wrappable", "  " + "x" * 90, False),
    (
        "a long token indented by three spaces is not wrappable",
        "   " + "x" * 90,
        False,
    ),
    (
        "a long token indented by four spaces is declared preformatted",
        "    " + "x" * 90,
        False,
    ),
    ("a long token indented by a tab is not wrappable", "\t" + "x" * 90, False),
    (
        "a long token indented by two tabs is not wrappable",
        "\t\t" + "x" * 90,
        False,
    ),
    # The same widths before prose that really can be wrapped. Indentation must
    # not excuse it, so every width under four answers the same way here too.
    ("bare prose is wrappable", "word " * 20, True),
    ("prose indented by one space is wrappable", " " + "word " * 20, True),
    ("prose indented by two spaces is wrappable", "  " + "word " * 20, True),
    ("prose indented by three spaces is wrappable", "   " + "word " * 20, True),
    ("prose indented by a tab is wrappable", "\t" + "word " * 20, True),
    ("prose indented by two tabs is wrappable", "\t\t" + "word " * 20, True),
    ("an indented block is declared preformatted", "    " + "word " * 20, False),
    (
        "a trailer value is not wrappable",
        "Refs: https://example.com/" + "a" * 80,
        False,
    ),
    ("a tab inside a trailer value is measured", "Refs: word\tword " * 8, True),
]

# A line, the limit it broke, and whether the refusal should name trailing
# whitespace as the cause. The note explains a length the author cannot see; it
# never excuses one, so a line that is too long on its own must not get it.
NOTE_CASES: list[tuple[str, str, int, bool]] = [
    ("no trailing whitespace gives no note", "x" * 80, 72, False),
    ("trailing spaces give a note", "x" * 70 + " " * 10, 72, True),
    ("trailing tabs give a note", "x" * 70 + "\t" * 10, 72, True),
    (
        "a line over the limit on its own gives no note",
        "x" * 80 + " " * 10,
        72,
        False,
    ),
    (
        "a line exactly at the limit without its padding gives a note",
        "x" * 72 + " ",
        72,
        True,
    ),
]

MESSAGE_CASES: list[tuple[str, str, bool]] = [
    ("clean subject only", "feat: add the selection engine", False),
    (
        "clean subject and body",
        "fix: reject an unwrapped custom scalar\n\n"
        "The response model wraps a\nJSON scalar object.",
        False,
    ),
    ("trailing comment is ignored", "docs: update the readme\n\n# please enter", False),
    ("missing type is refused", "add the selection engine", True),
    ("unknown type is refused", "wip: add the selection engine", True),
    ("scope is accepted", "feat(selection): memoize by policy fingerprint", False),
    ("breaking marker is accepted", "feat!: drop the legacy transport", False),
    ("long subject is refused", "feat: " + "x" * 70, True),
    ("missing blank line is refused", "feat: add a thing\nbody starts here", True),
    ("long body line is refused", "feat: add a thing\n\n" + "word " * 20, True),
    (
        # Twenty short words with many places to break. Only the separator
        # differs from the case above it.
        "a tab-separated body line is refused",
        "feat: add a thing\n\n" + "\t".join(["word"] * 20),
        True,
    ),
    (
        "a short tab-separated body line is accepted",
        "feat: add a thing\n\n" + "\t".join(["word"] * 8),
        False,
    ),
    (
        "a body line broken only past the limit is accepted",
        "feat: add a thing\n\n" + "x" * 80 + " " + "y" * 10,
        False,
    ),
    # One long subject per separator that Python calls a line boundary and git
    # does not. Git stores every one of these as a single line well over the
    # limit, so every one of them has to be refused as a single line.
    (
        "a form feed does not end the subject",
        "feat: short\f\f" + "x" * 70,
        True,
    ),
    (
        "a vertical tab does not end the subject",
        "feat: short\v\v" + "x" * 70,
        True,
    ),
    (
        "a next-line control does not end the subject",
        "feat: short\x85\x85" + "x" * 70,
        True,
    ),
    (
        "a line separator does not end the subject",
        "feat: short\u2028\u2028" + "x" * 70,
        True,
    ),
    (
        "a paragraph separator does not end the subject",
        "feat: short\u2029\u2029" + "x" * 70,
        True,
    ),
    (
        "a file separator does not end the subject",
        "feat: short\x1c\x1c" + "x" * 70,
        True,
    ),
    (
        "a form feed does not end a body line either",
        "feat: add a thing\n\nshort\f" + "x" * 90,
        True,
    ),
    (
        "a form feed inside a body line is still a break point",
        "feat: add a thing\n\n" + "\f".join(["word"] * 20),
        True,
    ),
    (
        "a lone carriage return does not end the subject",
        "feat: short\r\r" + "x" * 70,
        True,
    ),
    (
        "a windows line ending does end a line",
        "feat: add a thing\r\n\r\nshort body\r\n",
        False,
    ),
    (
        "long url in the body is allowed",
        "docs: cite the source\n\nhttps://example.com/" + "a" * 90,
        False,
    ),
    (
        "long trailer is allowed",
        "docs: cite the source\n\nRefs: https://example.com/" + "a" * 80,
        False,
    ),
    (
        "co-authored-by is refused",
        "feat: add a thing\n\nCo-Authored-By: Someone <someone@example.com>",
        True,
    ),
    (
        "lowercase co-authored-by is refused",
        "feat: add a thing\n\nco-authored-by: Someone <someone@example.com>",
        True,
    ),
    ("em dash is refused", "feat: add a thing " + chr(0x2014) + " and another", True),
    ("hidden character is refused", "feat: add a" + chr(0x200B) + " thing", True),
    ("an empty message is refused", "", True),
    ("a whitespace-only message is refused", "\n\n   \n", True),
    (
        "a message of invisible characters alone is refused",
        chr(0x00A0) + "\n",
        True,
    ),
    (
        "a line of ideographic spaces is not the blank line after the subject",
        "feat: add a thing\n\u3000\u3000\u3000\nbody text\n",
        True,
    ),
    (
        "a subject padded with ideographic spaces is measured in full",
        "feat: " + "x" * 60 + "\u3000" * 20,
        True,
    ),
    (
        "a subject padded with trailing spaces is measured in full",
        "feat: " + "x" * 60 + " " * 20,
        True,
    ),
    (
        "a subject padded with trailing tabs is measured in full",
        "feat: " + "x" * 60 + "\t" * 20,
        True,
    ),
    (
        "a valid subject with trailing spaces past the limit is refused",
        "feat: " + "x" * 66 + " " * 20,
        True,
    ),
    (
        "a short subject with trailing spaces is still accepted",
        "feat: add a thing   ",
        False,
    ),
    (
        "a body line padded past the limit with trailing spaces is refused",
        "feat: add a thing\n\n" + "word " * 14 + " " * 30,
        True,
    ),
    (
        "a body line padded past the limit with trailing tabs is refused",
        "feat: add a thing\n\n" + "word " * 14 + "\t" * 30,
        True,
    ),
    (
        "a short body line with trailing spaces is still accepted",
        "feat: add a thing\n\nshort body line   \n",
        False,
    ),
    (
        "a long token followed by trailing spaces stays exempt",
        "docs: cite the source\n\nhttps://example.com/" + "a" * 90 + "   ",
        False,
    ),
    (
        # The bounded lenience the CRLF decode buys. One carriage return is
        # part of how the file ends a line, so a subject at the limit in a
        # CRLF file is 72 and not 73. The two cases below show the lenience
        # stops at exactly one character.
        "a subject at the limit in a CRLF file is accepted",
        "feat: " + "x" * 66 + "\r\n\r\nbody\r\n",
        False,
    ),
    (
        "a body line at the limit in a CRLF file is accepted",
        "feat: add a thing\r\n\r\n" + "word " * 14 + "wo\r\n",
        False,
    ),
    (
        "two carriage returns before the line feed are content",
        "feat: " + "x" * 66 + "\r\r\n\nbody\n",
        True,
    ),
    (
        "a space before the carriage return is content",
        "feat: " + "x" * 66 + " " * 6 + "\r\n\nbody\n",
        True,
    ),
    ("merge subject is exempt", "Merge branch 'feat/branch-a'", False),
    (
        "merge subject with a conflict body is exempt",
        "Merge branch 'feat/branch-a'\n\n# Conflicts:\n#\tconflict.txt\n",
        False,
    ),
    (
        "revert subject is exempt",
        'Revert "feat: ' + "x" * 70 + '"\n\nThis reverts commit 0123456789abcdef.',
        False,
    ),
    ("autosquash marker is exempt", "fixup! feat: add a thing", False),
    (
        "a generated subject is still scanned for hygiene",
        "Merge branch 'x' " + chr(0x2014) + " done",
        True,
    ),
    (
        "a generated subject is still checked for attribution",
        "Merge branch 'x'\n\nCo-Authored-By: Someone <someone@example.com>",
        True,
    ),
    # Cleanup-mode holes. A comment line is committed by "git commit -F".
    ("a commented subject is refused", "# add the selection engine", True),
    (
        "a commented subject above a valid one is refused",
        "# hidden subject\nfeat: add the selection engine",
        True,
    ),
    (
        "a trailer under a false scissors marker is refused",
        "feat: add a thing\n\n" + SCISSORS + "\nCo-Authored-By: X <x@example.com>",
        True,
    ),
    (
        "an em dash under a false scissors marker is refused",
        "feat: add a thing\n\n" + SCISSORS + "\nnote " + chr(0x2014) + " here",
        True,
    ),
    (
        "an em dash below a genuine scissors marker is refused",
        "feat: add a thing\n\n"
        + SCISSORS
        + "\n# Do not modify or remove the line above.\n"
        + "A note "
        + chr(0x2014)
        + " below the bar.\n",
        True,
    ),
    (
        "an em dash inside a verbose diff is refused as well",
        "feat: add a thing\n\n"
        + SCISSORS
        + "\n# Do not modify or remove the line above.\n"
        + "diff --git a/x.md b/x.md\n+A rule "
        + chr(0x2014)
        + " stated.\n",
        True,
    ),
    (
        "a trailer inside a genuine diff is not an attribution finding",
        "feat: add a thing\n\n"
        + SCISSORS
        + "\n# Do not modify or remove the line above.\n"
        + "diff --git a/x.md b/x.md\n+Co-Authored-By: X <x@example.com>\n",
        False,
    ),
    (
        "a trailing invisible character is refused",
        "feat: add a thing\n\nA body line." + chr(0x00A0) + "\n",
        True,
    ),
    (
        "an invisible character on a trailing blank line is refused",
        "feat: add a thing\n\nA body line.\n\n" + chr(0x00A0) + "\n",
        True,
    ),
    (
        "an overlong line below a typed scissors marker is refused",
        "feat: add a thing\n\n"
        + SCISSORS
        + "\n# Do not modify or remove the line above.\n"
        + "word " * 20
        + "\n",
        True,
    ),
    (
        # A diff header grants nothing. Without a recording from git, a line
        # that looks like diff output is a line an author typed.
        "an overlong line under a transcribed diff header is refused",
        "feat: add a thing\n\n"
        + SCISSORS
        + "\n# Do not modify or remove the line above.\n"
        + "diff --git a/x.md b/x.md\n+"
        + "word " * 20
        + "\n",
        True,
    ),
    (
        "an overlong comment line below a typed scissors marker is refused",
        "feat: add a thing\n\n"
        + SCISSORS
        + "\n# Do not modify or remove the line above.\n"
        + "# "
        + "word " * 20
        + "\n",
        True,
    ),
    (
        "an overlong comment line above a typed scissors marker is refused",
        "feat: add a thing\n\n# " + "word " * 20 + "\n" + SCISSORS + "\n",
        True,
    ),
    (
        # A transcription of git's template is not git's template. With no
        # recording it is measured like any other typed text, and the same
        # bytes are accepted in TEMPLATE_CASES once git has recorded them.
        "a transcribed editor template is refused",
        "feat: add a thing" + TEMPLATE,
        True,
    ),
    (
        "overlong prose after a comment and a tab is refused",
        "feat: add a thing\n\n#\t" + "word " * 20 + "\n",
        True,
    ),
    (
        "overlong prose after a comment, a tab and a label is refused",
        "feat: add a thing\n\n#\tNote:  " + "word " * 20 + "\n",
        True,
    ),
    (
        "git's untracked listing is accepted",
        "feat: add a thing\n\n# Untracked files:\n#\t"
        + "a-directory-with-a-long-name/another-one/a-file-with-a-long-name.md\n",
        False,
    ),
    (
        "an overlong translated listing label is refused",
        "feat: add a thing\n\n#\tneue Datei:   "
        + "a-directory-with-a-long-name/another-one/a-file-with-a-long-name.md\n",
        True,
    ),
    (
        "an overlong rename listing is refused",
        "feat: add a thing\n\n#\trenamed:    "
        + "a-directory-with-a-long-name/an-old-file-name.md -> "
        + "a-directory-with-a-long-name/a-new-file-name.md\n",
        True,
    ),
    (
        "an overlong listed path containing a space is refused",
        "feat: add a thing\n\n#\tnew file:   "
        + "a-directory-with-a-long-name/another-one/and-another/"
        + "a file name with spaces.md\n",
        True,
    ),
    (
        # The review case for one arrow. Two ordinary phrases joined by a
        # single arrow are prose, and no reading of the shape can tell them
        # from a rename. Nothing reads the shape any more, so the line is
        # measured and refused.
        "two wrappable phrases joined by one arrow are refused",
        "feat: add a thing\n\n#\t"
        + "the first ordinary phrase of ten words written out here"
        + " -> "
        + "the second ordinary phrase of ten words written out here\n",
        True,
    ),
    (
        "overlong prose below a typed diff header is refused",
        "feat: add a thing\n\n"
        + SCISSORS
        + "\ndiff --git a/x.md b/x.md\n"
        + "word " * 20
        + "\n",
        True,
    ),
    (
        # Git writes one arrow in a rename or copy listing. Short words joined
        # by more of them are prose, so the split stops after the first arrow
        # and the rest is measured as one path.
        "repeated arrow separators after a tab are measured",
        "feat: add a thing\n\n#\t" + "w -> " * 25 + "w\n",
        True,
    ),
    (
        # The cost of splitting once. A real path that contains the separator
        # is measured together with the path after it, so a long pair like
        # this one is refused and the author renames or wraps by hand.
        "a renamed path containing the separator is measured",
        "feat: add a thing\n\n#\trenamed:    old.md -> "
        + LONG_DIR
        + "and-one-more/a -> b.md\n",
        True,
    ),
    (
        "overlong prose in a comment below a diff header is refused",
        "feat: add a thing\n\n"
        + SCISSORS
        + "\ndiff --git a/x.md b/x.md\n# "
        + "word " * 20
        + "\n",
        True,
    ),
    (
        "overlong prose in a comment above a diff header is refused",
        "feat: add a thing\n\n"
        + SCISSORS
        + "\n# "
        + "word " * 20
        + "\ndiff --git a/x.md b/x.md\n",
        True,
    ),
    (
        # The same bytes are accepted in TEMPLATE_CASES, where git recorded
        # them. Transcribed into a message by hand, they are measured.
        "a transcribed verbose block is refused",
        "feat: add a thing" + VERBOSE_TEMPLATE,
        True,
    ),
    (
        "a closed fenced block is exempt",
        "feat: add a thing\n\n```\n" + "word " * 20 + "\n```\n",
        False,
    ),
    (
        "an unclosed fence exempts nothing",
        "feat: add a thing\n\n```\n" + "word " * 20 + "\n",
        True,
    ),
    (
        "a merge subject does not exempt an overlong body",
        "Merge branch 'feat/branch-a'\n\n" + "word " * 20 + "\n",
        True,
    ),
    (
        "a merge subject does not exempt a missing blank line",
        "Merge branch 'feat/branch-a'\nstraight into the body\n",
        True,
    ),
    (
        "an autosquash subject does not exempt an overlong body",
        "fixup! feat: add a thing\n\n" + "word " * 20 + "\n",
        True,
    ),
    (
        "a trailer below a transcribed scissors line is still refused",
        "feat: add a thing\n\n"
        + SCISSORS
        + "\n# Do not modify or remove the line above.\n"
        + "Co-Authored-By: X <x@example.com>\n",
        True,
    ),
    # Fences, parsed by the repository's one parser. A block is exempt only
    # when a valid closer is found, on the same terms the hygiene scanner uses.
    (
        "a fence closed by a longer run is exempt",
        "feat: add a thing\n\n```\n" + "word " * 20 + "\n`````\n",
        False,
    ),
    (
        "a fence closed by a shorter run is not closed",
        "feat: add a thing\n\n`````\n" + "word " * 20 + "\n```\n",
        True,
    ),
    (
        "a closer carrying an info string does not close the fence",
        "feat: add a thing\n\n```python\n" + "word " * 20 + "\n```not-a-close\n",
        True,
    ),
    (
        "a closer of the other fence character does not close the fence",
        "feat: add a thing\n\n```\n" + "word " * 20 + "\n~~~\n",
        True,
    ),
    (
        "a tilde fence is exempt",
        "feat: add a thing\n\n~~~\n" + "word " * 20 + "\n~~~\n",
        False,
    ),
    (
        "a tilde fence closer may carry no info string either",
        "feat: add a thing\n\n~~~yaml\n" + "word " * 20 + "\n~~~yaml\n",
        True,
    ),
    (
        "a fence indented by three spaces is exempt",
        "feat: add a thing\n\n   ```\n" + "word " * 20 + "\n   ```\n",
        False,
    ),
    (
        # Four spaces is an indented block, which is exempt for its own
        # reason. The line inside it is exempt either way, so the case that
        # matters is the one after the block, which is measured.
        "text after an indented opener is measured",
        "feat: add a thing\n\n    ```\n" + "word " * 20 + "\n",
        True,
    ),
    (
        "a backtick fence with a backtick in its info string opens nothing",
        "feat: add a thing\n\n```a`b\n" + "word " * 20 + "\n```\n",
        True,
    ),
    # The blank-line rule, on the lines the author wrote. A comment character
    # does not remove a line from the body.
    (
        "a comment line straight under the subject is refused",
        "feat: add a thing\n# author body text\n",
        True,
    ),
    (
        "an overlong comment line straight under the subject is refused",
        "feat: add a thing\n# " + "word " * 20 + "\n",
        True,
    ),
]


# A refusal that sends an author to a document has to name that document.
# MESSAGE_CASES and BRANCH_CASES ask only whether a refusal happened, so
# deleting a pointer leaves them all passing. These cases assert the pointer
# itself, one per refusal that carries one.
POINTER_CASES: list[tuple[str, str, str]] = [
    ("an overlong subject", "fix: " + "w" * 90, COMMIT_RULES_DOC),
    ("a body with no blank line above it", "fix: a subject\nbody", COMMIT_RULES_DOC),
    ("an overlong body line", "fix: a subject\n\n" + "word " * 20, COMMIT_RULES_DOC),
]

# A message and the template git recorded for it, taken before an editor could
# add anything. The recording is the only thing that excuses a line, so each
# case pairs one with a message and states what the pair should produce.
TEMPLATE_CASES: list[tuple[str, str, str, bool]] = [
    (
        "git's recorded template is excused",
        TEMPLATE,
        "feat: add a thing" + TEMPLATE,
        False,
    ),
    (
        # A space added to one of git's own lines is an edit, and the recording
        # is all or nothing, so nothing in the message is excused any more.
        # The long path line inside the template is then measured and refused.
        "a space added to a recorded template line excuses nothing",
        TEMPLATE,
        "feat: add a thing" + TEMPLATE.replace("# On branch", " # On branch", 1),
        True,
    ),
    (
        "a space appended to a recorded template line excuses nothing",
        TEMPLATE,
        "feat: add a thing"
        + TEMPLATE.replace("chore/git-hooks\n", "chore/git-hooks \n", 1),
        True,
    ),
    (
        # The same edit made with a tab, and the same answer.
        "a tab appended to a recorded template line excuses nothing",
        TEMPLATE,
        "feat: add a thing"
        + TEMPLATE.replace("chore/git-hooks\n", "chore/git-hooks\t\n", 1),
        True,
    ),
    (
        # Trailing whitespace removed from git's text is an edit as well.
        # An editor that trims on save has rewritten git's lines.
        "trailing whitespace removed from a recorded line excuses nothing",
        TEMPLATE.replace("chore/git-hooks\n", "chore/git-hooks  \n", 1),
        "feat: add a thing" + TEMPLATE,
        True,
    ),
    (
        "a recorded verbose block is excused",
        VERBOSE_TEMPLATE,
        "feat: add a thing" + VERBOSE_TEMPLATE,
        False,
    ),
    (
        # Git wrote the byte and the author left it alone, so the recording
        # still holds and the long diff line below it stays excused.
        "a recorded template carrying a raw byte is excused",
        RAW_BYTE_TEMPLATE,
        "feat: add a thing" + RAW_BYTE_TEMPLATE,
        False,
    ),
    (
        # One byte of git's text swapped for a different one. Both render as a
        # single replacement character, so a lossy decode saw no change. The
        # bytes differ, which makes this an edit, and an edit costs the whole
        # exemption.
        "a raw byte swapped for a different one excuses nothing",
        RAW_BYTE_TEMPLATE,
        "feat: add a thing" + RAW_BYTE_EDITED,
        True,
    ),
    (
        "a recorded template does not trip the blank-line rule",
        TEMPLATE,
        "feat: add a thing" + TEMPLATE,
        False,
    ),
    (
        "an author body above a recorded template is accepted",
        TEMPLATE,
        "feat: add a thing\n\nA short body line." + TEMPLATE,
        False,
    ),
    (
        "an overlong author line above a recorded template is refused",
        TEMPLATE,
        "feat: add a thing\n\n" + "word " * 20 + TEMPLATE,
        True,
    ),
    (
        "a missing blank line above a recorded template is refused",
        TEMPLATE,
        "feat: add a thing\nstraight into the body" + TEMPLATE,
        True,
    ),
    (
        "an author comment above a recorded template is still the body",
        TEMPLATE,
        "feat: add a thing\n# author body text" + TEMPLATE,
        True,
    ),
    (
        # Anything below the recording breaks the suffix, so the recording
        # excuses nothing at all and the whole message is measured.
        "a line added below the recorded template excuses nothing",
        TEMPLATE,
        "feat: add a thing" + TEMPLATE + "word " * 20 + "\n",
        True,
    ),
    (
        # The author edited one template line, so the recording is no longer
        # intact and the whole message is measured, this line included.
        "an edited template line is measured",
        TEMPLATE,
        "feat: add a thing\n#\tnew file:   "
        + LONG_DIR
        + "a-file-with-a-different-long-name.md\n#\n",
        True,
    ),
    (
        # The recording is for this commit's template. A second copy of an
        # overlong template line, typed above it, is not part of the suffix.
        "a second copy of a recorded line is measured",
        TEMPLATE,
        "feat: add a thing\n#\tnew file:   "
        + LONG_DIR
        + "a-file-with-a-long-name.md"
        + TEMPLATE,
        True,
    ),
    (
        # The reviewed bypass. The author stages any text, lets "-v" record it
        # as a diff line, then deletes everything git wrote except that one
        # line. A partial suffix would have excused it. The recording is one
        # object, so a cut template excuses nothing.
        "a template cut down to its last recorded lines excuses nothing",
        VERBOSE_TEMPLATE,
        "feat: add a thing\n-" + "word " * 20 + "\n+one\n",
        True,
    ),
    (
        "a template cut down to its last comment line excuses nothing",
        TEMPLATE,
        "feat: add a thing\n#\tnew file:   "
        + LONG_DIR
        + "a-file-with-a-long-name.md\n#\n",
        True,
    ),
    (
        # One line removed from the middle. Everything below it still matches,
        # and none of it is excused.
        "a template with its middle removed excuses nothing",
        TEMPLATE,
        "feat: add a thing\n"
        + TEMPLATE.replace("# On branch chore/git-hooks\n", "", 1),
        True,
    ),
    (
        "an empty recording excuses nothing",
        "",
        "feat: add a thing" + TEMPLATE,
        True,
    ),
    (
        # An editor that leaves one blank line at the end of the file has not
        # touched git's text. A blank line carries nothing and cannot be over
        # the limit, so it does not break the match.
        "a blank line below a recorded template is tolerated",
        TEMPLATE,
        "feat: add a thing" + TEMPLATE + "\n",
        False,
    ),
    (
        # The recording's own leading blank line is where the subject goes.
        "the recorded blank subject line does not have to survive",
        TEMPLATE,
        "feat: add a thing" + TEMPLATE,
        False,
    ),
    (
        "a recorded template is still scanned for hygiene",
        TEMPLATE,
        "feat: add a thing\n\nA note " + chr(0x2014) + " here." + TEMPLATE,
        True,
    ),
    # Separator substitution. The author keeps every character git wrote and
    # replaces the line feeds between them. Git then stores the whole template
    # as one very long line, so the recording did not survive and nothing in
    # the message is excused any more.
    (
        "a template whose line feeds became form feeds excuses nothing",
        TEMPLATE,
        "feat: add a thing" + TEMPLATE.replace("\n", "\f"),
        True,
    ),
    (
        "a template whose line feeds became line separators excuses nothing",
        TEMPLATE,
        "feat: add a thing" + TEMPLATE.replace("\n", "\u2028"),
        True,
    ),
    (
        "a template with one line feed replaced excuses nothing",
        TEMPLATE,
        "feat: add a thing" + TEMPLATE.replace("\n", "\f", 1),
        True,
    ),
    (
        "a verbose template whose line feeds became form feeds excuses nothing",
        VERBOSE_TEMPLATE,
        "feat: add a thing" + VERBOSE_TEMPLATE.replace("\n", "\f"),
        True,
    ),
    (
        "a recorded template is still checked for attribution",
        TEMPLATE,
        "feat: add a thing\n\nCo-Authored-By: X <x@example.com>" + TEMPLATE,
        True,
    ),
]


def hook_env(repo: Path, **overrides: str) -> dict[str, str]:
    """A predictable environment for an integration probe.

    Every inherited ``GIT_`` variable is dropped, user and system configuration
    are pointed at files that do not exist, and both maintainer overrides start
    unset, so the probe measures the hook and not the machine it runs on.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and not key.startswith("PYTEST_GQL_")
    }
    env.update(
        {
            "GIT_CONFIG_GLOBAL": str(repo / "no-global-config"),
            "GIT_CONFIG_SYSTEM": str(repo / "no-system-config"),
            "GIT_AUTHOR_NAME": "Self Test",
            "GIT_AUTHOR_EMAIL": "self-test@example.invalid",
            "GIT_COMMITTER_NAME": "Self Test",
            "GIT_COMMITTER_EMAIL": "self-test@example.invalid",
            "LC_ALL": "C",
        }
    )
    env.update(overrides)
    return env


def in_repo(
    repo: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, env=env or hook_env(repo)
    )


def build_probe_repo(root: Path) -> Path:
    """A temporary repository with these hooks installed."""
    here = Path(__file__).resolve().parent
    hooks = here.parent / ".githooks"

    repo = root / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".githooks").mkdir()
    (repo / "docs" / "reference").mkdir(parents=True)

    for name in ("git_hook_checks.py", "check_publication_hygiene.py"):
        shutil.copy(here / name, repo / "scripts" / name)
    for name in (
        "pre-commit",
        "pre-merge-commit",
        "pre-applypatch",
        "prepare-commit-msg",
        "commit-msg",
        "applypatch-msg",
    ):
        target = repo / ".githooks" / name
        shutil.copy(hooks / name, target)
        target.chmod(0o755)

    (repo / "README.md").write_text("Seed.\n", encoding="utf-8")
    (repo / "LICENSE").write_text("Seed licence.\n", encoding="utf-8")
    (repo / SPEC_PATH).write_text("Specification.\n", encoding="utf-8")

    in_repo(repo, "init", "-q", "-b", "main", ".")
    in_repo(repo, "config", "core.hooksPath", ".githooks")
    in_repo(repo, "add", "-A")
    in_repo(
        repo,
        "commit",
        "-q",
        "-m",
        "chore: seed the repository",
        env=hook_env(repo, PYTEST_GQL_ALLOW_MAIN="1", PYTEST_GQL_ALLOW_SPEC_EDIT="1"),
    )
    return repo


def integration_test() -> tuple[int, int]:
    """Drive the installed hooks with real git in a temporary repository.

    The unit cases above check the rules. These check that git reaches them, on
    the paths where a rule can be walked around rather than broken: a merge, a
    cherry-pick, a revert or a mailed patch that commits onto a protected
    branch, with and without a conflict, a deletion or a rename that never
    appears as an ordinary modification, a message supplied by file, where git
    keeps the comment lines and where ``--cleanup=verbatim`` keeps everything,
    and a blob whose size decides how it is read.
    """
    failures = 0
    total = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures, total
        total += 1
        if not ok:
            failures += 1
            print(f"FAIL integration/{name}: {detail}")

    if shutil.which("git") is None:
        check("git is available", False, "git was not found on PATH")
        return failures, total

    if not (Path(__file__).resolve().parent.parent / ".githooks").is_dir():
        check(".githooks is present", False, "the hook directory was not found")
        return failures, total

    with tempfile.TemporaryDirectory() as name:
        repo = build_probe_repo(Path(name))

        def head_count() -> int:
            out = in_repo(repo, "rev-list", "--count", "HEAD").stdout
            return int(out.decode().strip() or 0)

        if head_count() != 1:
            check("the probe repository was seeded", False, "no seed commit")
            return failures, total

        def stage(path: str, content: str) -> None:
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            in_repo(repo, "add", "--", path)

        def commit(
            message: str, env: dict[str, str] | None = None
        ) -> subprocess.CompletedProcess[bytes]:
            message_file = repo / ".git" / "PROBE_MSG"
            message_file.write_text(message, encoding="utf-8")
            return in_repo(repo, "commit", "-q", "-F", str(message_file), env=env)

        def commit_cleanup(
            message: str, mode: str
        ) -> subprocess.CompletedProcess[bytes]:
            """Commit by file under a cleanup mode the probe names."""
            message_file = repo / ".git" / "PROBE_MSG"
            message_file.write_text(message, encoding="utf-8")
            return in_repo(
                repo, "commit", "-q", f"--cleanup={mode}", "-F", str(message_file)
            )

        def reset() -> None:
            in_repo(repo, "reset", "-q", "--hard", "HEAD")
            in_repo(repo, "clean", "-qfd")

        def refused(
            name: str, result: subprocess.CompletedProcess[bytes], needle: str
        ) -> None:
            text = result.stderr.decode("utf-8", errors="replace")
            check(
                name,
                result.returncode != 0 and needle in text,
                f"rc={result.returncode}, stderr={text.strip()[:240]!r}",
            )

        def accepted(name: str, result: subprocess.CompletedProcess[bytes]) -> None:
            text = result.stderr.decode("utf-8", errors="replace")
            check(
                name,
                result.returncode == 0,
                f"rc={result.returncode}, stderr={text.strip()[:240]!r}",
            )

        # Protected branch, ordinary commit.
        stage("README.md", "One.\n")
        refused("commit on main", commit("chore: touch the readme"), "on 'main'")
        accepted(
            "commit on main with the override",
            commit(
                "chore: touch the readme", hook_env(repo, PYTEST_GQL_ALLOW_MAIN="1")
            ),
        )

        # Branch naming.
        in_repo(repo, "switch", "-q", "-c", "feat/good-name")
        stage("README.md", "Two.\n")
        accepted("commit on a typed branch", commit("feat: extend the readme"))

        for branch, label in (
            ("feat/not_hyphenated", "underscore branch"),
            ("feat/topic/extra", "nested branch"),
        ):
            in_repo(repo, "switch", "-q", "-c", branch)
            stage("README.md", f"Probe {label}.\n")
            refused(label, commit("feat: probe the branch name"), "hyphenated")
            reset()
            in_repo(repo, "switch", "-q", "feat/good-name")

        # A merge and a cherry-pick commit onto the current branch.
        in_repo(repo, "switch", "-q", "-c", "feat/branch-a", "main")
        stage("conflict.txt", "A\n")
        accepted("seed branch a", commit("feat: add branch a"))

        in_repo(repo, "switch", "-q", "-c", "feat/branch-b", "main")
        stage("conflict.txt", "B\n")
        accepted("seed branch b", commit("feat: add branch b"))

        in_repo(repo, "switch", "-q", "main")
        in_repo(repo, "merge", "-q", "--no-edit", "feat/branch-a")
        in_repo(repo, "merge", "--no-edit", "feat/branch-b")
        stage("conflict.txt", "C\n")
        refused(
            "merge commit on main",
            in_repo(repo, "commit", "-q", "--no-edit"),
            "on 'main'",
        )
        in_repo(repo, "merge", "--abort")

        in_repo(repo, "cherry-pick", "feat/branch-b")
        stage("conflict.txt", "D\n")
        refused(
            "cherry-pick commit on main",
            in_repo(repo, "commit", "-q", "--no-edit"),
            "on 'main'",
        )
        in_repo(repo, "cherry-pick", "--abort")
        reset()

        # Deletion and rename of the historical specification.
        in_repo(repo, "switch", "-q", "-c", "chore/spec-probe", "main")
        in_repo(repo, "rm", "-q", "--", SPEC_PATH)
        refused(
            "specification deletion",
            commit("chore: remove the specification"),
            "historical record",
        )
        reset()

        in_repo(repo, "mv", SPEC_PATH, "docs/reference/OLD_SPEC.md")
        in_repo(repo, "add", "-A")
        refused(
            "specification rename",
            commit("chore: move the specification"),
            "historical record",
        )
        reset()

        # Message content that survives the cleanup "git commit -F" applies.
        stage("README.md", "Three.\n")
        refused(
            "commented subject",
            commit("# arbitrary non-conventional subject"),
            "conventional commit",
        )
        refused(
            "trailer under a false scissors marker",
            commit(
                "feat: probe the message\n\n"
                + SCISSORS
                + "\nCo-Authored-By: X <x@e.invalid>"
            ),
            "Co-Authored-By",
        )
        accepted("clean message by file", commit("feat: probe the message"))

        # Content scanning, whatever the suffix.
        stage("LICENSE", "A licence " + chr(0x2014) + " restated.\n")
        refused("suffixless text file", commit("chore: restate the licence"), "em-dash")
        reset()

        stage("site/index.html", "<p>A page " + chr(0x2014) + " here</p>\n")
        refused("site text format", commit("chore: add a page"), "em-dash")
        reset()

        (repo / "logo.bin").write_bytes(bytes(range(32)))
        in_repo(repo, "add", "--", "logo.bin")
        accepted("binary file", commit("chore: add a binary asset"))

        # Size decides how a blob is read, never whether it is allowed.
        (repo / "large.bin").write_bytes(b"\0" * (MAX_SCAN_BYTES + 1))
        in_repo(repo, "add", "--", "large.bin")
        accepted("large binary file", commit("chore: add a large binary asset"))

        (repo / "large.txt").write_text("a" * (MAX_SCAN_BYTES + 1), encoding="utf-8")
        in_repo(repo, "add", "--", "large.txt")
        refused(
            "large text file",
            commit("chore: add a large text file"),
            "too large",
        )
        reset()

        # Hygiene below a genuine scissors marker, supplied by file, where git
        # applies --cleanup=whitespace and commits the text anyway.
        stage("README.md", "Four.\n")
        refused(
            "prohibited character below a genuine scissors marker",
            commit(
                "feat: probe the message\n\n"
                + SCISSORS
                + "\n# Do not modify or remove the line above.\n"
                + "A note "
                + chr(0x2014)
                + " below the bar.\n"
            ),
            "em-dash",
        )
        reset()

        # The commit paths that never run pre-commit, without a conflict.
        in_repo(repo, "switch", "-q", "-c", "feat/clean-source", "main")
        stage("clean-source.txt", "Clean.\n")
        accepted("seed an unconflicting branch", commit("feat: add a clean file"))

        in_repo(repo, "switch", "-q", "main")
        for label, args in (
            ("merge", ("merge", "--no-ff", "--no-edit", "feat/clean-source")),
            ("cherry-pick", ("cherry-pick", "feat/clean-source")),
            ("revert", ("revert", "--no-edit", "HEAD")),
        ):
            before = head_count()
            refused(f"unconflicted {label} on main", in_repo(repo, *args), "on 'main'")
            check(
                f"unconflicted {label} on main created no commit",
                head_count() == before,
                f"HEAD moved from {before} to {head_count()} commits",
            )
            in_repo(repo, args[0], "--abort")
            reset()

        in_repo(repo, "switch", "-q", "-c", "feat/merge-target", "main")
        accepted(
            "unconflicted merge on a typed branch",
            in_repo(repo, "merge", "--no-ff", "--no-edit", "feat/clean-source"),
        )

        # Git reports the source "merge" both for a message it wrote itself and
        # for one the author gave with "git merge -m", so neither is recorded
        # and the body is measured either way. Without that restriction the
        # author's own text would be excused as if git had written it.
        in_repo(repo, "switch", "-q", "-c", "feat/merge-message", "main")
        refused(
            "an overlong body given to git merge -m",
            in_repo(
                repo,
                "merge",
                "--no-ff",
                "-m",
                "feat: merge the clean source\n\n" + "word " * 20,
                "feat/clean-source",
            ),
            "Wrap the body",
        )
        in_repo(repo, "merge", "--abort")
        reset()

        in_repo(repo, "switch", "-q", "-c", "feat/merge-wrapped", "main")
        accepted(
            "a wrapped body given to git merge -m",
            in_repo(
                repo,
                "merge",
                "--no-ff",
                "-m",
                "feat: merge the clean source\n\nA short body line.",
                "feat/clean-source",
            ),
        )

        # A merge carries in staged paths that pre-commit never saw. The branch
        # below is built with the hooks switched off, the way history that
        # arrives from a remote is.
        (repo / "no-hooks").mkdir()
        without_hooks = hook_env(repo)
        in_repo(repo, "switch", "-q", "-c", "chore/keep-out-source", "main")
        stage("tmp/notes.md", "Local coordination state.\n")
        in_repo(
            repo,
            "-c",
            f"core.hooksPath={repo / 'no-hooks'}",
            "commit",
            "-q",
            "-m",
            "chore: add local state",
            env=without_hooks,
        )
        in_repo(repo, "switch", "-q", "-c", "chore/keep-out-target", "main")
        refused(
            "keep-out path arriving through a merge",
            in_repo(repo, "merge", "--no-ff", "--no-edit", "chore/keep-out-source"),
            "coordination state",
        )
        in_repo(repo, "merge", "--abort")
        reset()

        # "git commit --cleanup=verbatim" commits the message untouched, so
        # everything in the file is judged, including a trailing invisible
        # character and the lines below a bar an author typed.
        in_repo(repo, "switch", "-q", "-c", "chore/verbatim-probe", "main")
        # Each of the three stages its own change, so an accepted commit cannot
        # turn the next probe into an empty one.
        stage("README.md", "Five.\n")
        refused(
            "trailing invisible character under verbatim cleanup",
            commit_cleanup(
                "feat: probe the message\n\nA body line." + chr(0x00A0) + "\n",
                "verbatim",
            ),
            "invisible",
        )
        stage("README.md", "Six.\n")
        refused(
            "overlong line below a typed scissors marker under verbatim cleanup",
            commit_cleanup(
                "feat: probe the message\n\n"
                + SCISSORS
                + "\n# Do not modify or remove the line above.\n"
                + "word " * 20
                + "\n",
                "verbatim",
            ),
            "Wrap the body",
        )
        stage("README.md", "Seven.\n")
        refused(
            "overlong comment line below a typed scissors marker",
            commit_cleanup(
                "feat: probe the message\n\n"
                + SCISSORS
                + "\n# Do not modify or remove the line above.\n"
                + "# "
                + "word " * 20
                + "\n",
                "verbatim",
            ),
            "Wrap the body",
        )
        stage("README.md", "Eight.\n")
        refused(
            # Not "no subject line" any more, and correctly so. A non-breaking
            # space is content to git: it is committed, and it is displayed.
            # The subject here is one invisible character, so it is measured as
            # a subject and refused as one.
            "an invisible subject under verbatim cleanup is measured, not empty",
            commit_cleanup(chr(0x00A0) + "\n", "verbatim"),
            "is not a conventional commit",
        )
        stage("README.md", "Nine.\n")
        message_file = repo / ".git" / "PROBE_MSG"
        message_file.write_text("", encoding="utf-8")
        refused(
            "an empty message that git was told to allow",
            in_repo(
                repo,
                "commit",
                "-q",
                "--allow-empty-message",
                "-F",
                str(message_file),
            ),
            "no subject line",
        )
        stage("README.md", "Ten.\n")
        refused(
            "overlong prose after a comment and a tab under verbatim cleanup",
            commit_cleanup(
                "feat: probe the message\n\n#\t" + "word " * 20 + "\n",
                "verbatim",
            ),
            "Wrap the body",
        )
        stage("README.md", "An arrow probe.\n")
        refused(
            "repeated arrow separators after a tab under verbatim cleanup",
            commit_cleanup(
                "feat: probe the message\n\n#\t" + "w -> " * 25 + "w\n",
                "verbatim",
            ),
            "Wrap the body",
        )
        stage("README.md", "A one-arrow probe.\n")
        refused(
            "two wrappable phrases joined by one arrow under verbatim cleanup",
            commit_cleanup(
                "feat: probe the message\n\n#\t"
                + "the first ordinary phrase of ten words written out here"
                + " -> "
                + "the second ordinary phrase of ten words written out here\n",
                "verbatim",
            ),
            "Wrap the body",
        )
        stage("README.md", "A comment-body probe.\n")
        refused(
            "a comment line straight under the subject under verbatim cleanup",
            commit_cleanup(
                "feat: probe the message\n# author body text\n",
                "verbatim",
            ),
            "blank line",
        )
        stage("README.md", "A false-closer probe.\n")
        refused(
            "a fence closed by an info string under verbatim cleanup",
            commit_cleanup(
                "feat: probe the message\n\n```python\n"
                + "word " * 20
                + "\n```not-a-close\n",
                "verbatim",
            ),
            "Wrap the body",
        )
        stage("README.md", "A tilde-fence probe.\n")
        accepted(
            "a tilde fence under verbatim cleanup",
            commit_cleanup(
                "feat: probe the message\n\n~~~\n" + "word " * 20 + "\n~~~\n",
                "verbatim",
            ),
        )
        stage("README.md", "A transcribed-template probe.\n")
        refused(
            "a transcribed editor template supplied by file",
            commit_cleanup("feat: probe the message" + TEMPLATE, "verbatim"),
            "Wrap the body",
        )
        stage("README.md", "A comment-after-header probe.\n")
        refused(
            "an overlong comment below a typed diff header under verbatim cleanup",
            commit_cleanup(
                "feat: probe the message\n\n"
                + SCISSORS
                + "\ndiff --git a/x.md b/x.md\n# "
                + "word " * 20
                + "\n",
                "verbatim",
            ),
            "Wrap the body",
        )
        stage("README.md", "A generated-subject probe.\n")
        refused(
            "an overlong body under a merge subject under verbatim cleanup",
            commit_cleanup(
                "Merge branch 'feat/branch-a'\n\n" + "word " * 20 + "\n",
                "verbatim",
            ),
            "Wrap the body",
        )
        stage("README.md", "Eleven.\n")
        accepted(
            "clean message under verbatim cleanup",
            commit_cleanup("feat: probe the message\n", "verbatim"),
        )

        # The two exemptions, measured against git rather than against a
        # transcription of it. The template lists a path past the column limit,
        # and "-v" puts a real diff below a real scissors bar. An editor that
        # rewrites the subject and keeps everything else leaves both in place.
        editor = Path(name) / "editor.py"
        editor.write_text(
            "import pathlib, sys\n"
            "path = pathlib.Path(sys.argv[1])\n"
            "lines = path.read_text(encoding='utf-8').split('\\n')\n"
            "lines[0] = 'feat: probe the editor template'\n"
            "path.write_text('\\n'.join(lines), encoding='utf-8')\n",
            encoding="utf-8",
        )
        stage(
            "a-directory-with-a-long-name/another-one/a-file-with-a-long-name.md",
            "Twelve.\n",
        )
        accepted(
            "an editor commit whose template lists a long path, with a verbose diff",
            in_repo(
                repo,
                "commit",
                "-q",
                "-v",
                env=hook_env(repo, GIT_EDITOR=f'"{sys.executable}" "{editor}"'),
            ),
        )
        reset()

        # The same editor, plus one overlong line appended below everything git
        # wrote. That breaks the recorded suffix, so nothing is excused and the
        # template's own long path is measured along with the added line.
        below = Path(name) / "editor-below.py"
        below.write_text(
            "import pathlib, sys\n"
            "path = pathlib.Path(sys.argv[1])\n"
            "lines = path.read_text(encoding='utf-8').split('\\n')\n"
            "lines[0] = 'feat: probe the editor template'\n"
            "lines.append('word ' * 20)\n"
            "path.write_text('\\n'.join(lines), encoding='utf-8')\n",
            encoding="utf-8",
        )
        stage(
            "a-directory-with-a-long-name/another-one/a-second-long-name.md",
            "Thirteen.\n",
        )
        refused(
            "an editor commit with a line appended below git's template",
            in_repo(
                repo,
                "commit",
                "-q",
                "-v",
                env=hook_env(repo, GIT_EDITOR=f'"{sys.executable}" "{below}"'),
            ),
            "Wrap the body",
        )
        reset()

        # The reviewed bypass, driven by a real editor. The staged file holds a
        # long line, so "-v" records it as a diff line. The editor then throws
        # git's text away and keeps a subject plus a copy of that one line. A
        # partial suffix match would have excused it.
        cut = Path(name) / "editor-cut.py"
        cut.write_text(
            "import pathlib, sys\n"
            "path = pathlib.Path(sys.argv[1])\n"
            "lines = path.read_text(encoding='utf-8').split('\\n')\n"
            "kept = [x for x in lines if x.startswith('+') "
            "and not x.startswith('+++')][-1]\n"
            "path.write_text('feat: probe a cut template\\n' + kept + '\\n', "
            "encoding='utf-8')\n",
            encoding="utf-8",
        )
        stage("long-line.md", "word " * 20 + "\n")
        refused(
            "an editor commit that keeps one recorded diff line is refused",
            in_repo(
                repo,
                "commit",
                "-q",
                "-v",
                env=hook_env(repo, GIT_EDITOR=f'"{sys.executable}" "{cut}"'),
            ),
            "Wrap the body",
        )
        reset()

        # One line removed from the middle of git's text. Everything below it
        # still matches the recording, and none of it is excused.
        gap = Path(name) / "editor-gap.py"
        gap.write_text(
            "import pathlib, sys\n"
            "path = pathlib.Path(sys.argv[1])\n"
            "lines = path.read_text(encoding='utf-8').split('\\n')\n"
            "lines[0] = 'feat: probe a gapped template'\n"
            "cut = next(i for i, x in enumerate(lines) "
            "if x.startswith('# On branch'))\n"
            "del lines[cut]\n"
            "path.write_text('\\n'.join(lines), encoding='utf-8')\n",
            encoding="utf-8",
        )
        stage(
            "a-directory-with-a-long-name/another-one/a-fourth-long-name.md",
            "Fifteen.\n",
        )
        refused(
            "an editor commit that removes one template line is refused",
            in_repo(
                repo,
                "commit",
                "-q",
                "-v",
                env=hook_env(repo, GIT_EDITOR=f'"{sys.executable}" "{gap}"'),
            ),
            "Wrap the body",
        )
        reset()

        # Tab-separated prose, committed the way the review committed it.
        # "verbatim" keeps the line exactly as written, so the hook measures
        # what git will store.
        stage("tabbed.md", "Sixteen.\n")
        refused(
            "an overlong tab-separated body under verbatim cleanup",
            commit_cleanup(
                "feat: probe tab separation\n\n" + "\t".join(["word"] * 20) + "\n",
                "verbatim",
            ),
            "Wrap the body",
        )
        reset()
        stage("tabbed-short.md", "Seventeen.\n")
        accepted(
            "a short tab-separated body under verbatim cleanup",
            commit_cleanup(
                "feat: probe tab separation\n\n" + "\t".join(["word"] * 8) + "\n",
                "verbatim",
            ),
        )
        reset()

        # The reviewed separator bypass, driven by a real commit. Git stores
        # this subject as one line of 83 bytes. Python's splitlines cut it into
        # three short pieces, and all three passed.
        stage("form-feed.md", "Eighteen.\n")
        refused(
            "an overlong subject split by form feeds under verbatim cleanup",
            commit_cleanup("feat: short\f\f" + "x" * 70 + "\n", "verbatim"),
            "subject is 83 characters",
        )
        reset()
        stage("line-separator.md", "Nineteen.\n")
        refused(
            "an overlong subject split by line separators under verbatim cleanup",
            commit_cleanup("feat: short\u2028\u2028" + "x" * 70 + "\n", "verbatim"),
            "subject is 83 characters",
        )
        reset()

        # The reviewed template bypass, driven by a real editor. Every line
        # feed inside git's text becomes a form feed. Not one character is
        # removed, so a splitter that treats a form feed as a line ending sees
        # the recording unchanged. Git sees one line of several hundred bytes,
        # and so the recording did not survive and nothing is excused.
        swap = Path(name) / "editor-swap.py"
        swap.write_text(
            "import pathlib, sys\n"
            "path = pathlib.Path(sys.argv[1])\n"
            "text = path.read_text(encoding='utf-8')\n"
            "path.write_text('feat: probe a swapped template\\n'\n"
            "                + text.replace('\\n', '\\f'), encoding='utf-8')\n",
            encoding="utf-8",
        )
        stage(
            "a-directory-with-a-long-name/another-one/a-fifth-long-name.md",
            "Twenty.\n",
        )
        refused(
            "an editor commit that swaps the template's line endings is refused",
            in_repo(
                repo,
                "commit",
                "-q",
                "-v",
                env=hook_env(repo, GIT_EDITOR=f'"{sys.executable}" "{swap}"'),
            ),
            "Wrap the body",
        )
        reset()

        # The reviewed trailing-whitespace bypass, driven by a real commit.
        # The subject is a valid 72-character subject with 20 spaces after it.
        # Git stores all 92 characters, and only "git log" hides them, so the
        # hook has to measure the line as written.
        stage("trailing-subject.md", "Twenty-three.\n")
        refused(
            "an overlong subject padded with spaces under verbatim cleanup",
            commit_cleanup("feat: " + "x" * 66 + " " * 20 + "\n", "verbatim"),
            "subject is 92 characters",
        )
        reset()
        stage("trailing-subject-tab.md", "Twenty-four.\n")
        refused(
            "an overlong subject padded with tabs under verbatim cleanup",
            commit_cleanup("feat: " + "x" * 66 + "\t" * 20 + "\n", "verbatim"),
            "subject is 92 characters",
        )
        reset()
        stage("trailing-body.md", "Twenty-five.\n")
        refused(
            "an overlong body line padded with spaces under verbatim cleanup",
            commit_cleanup(
                "feat: probe trailing space\n\n" + "word " * 14 + " " * 30 + "\n",
                "verbatim",
            ),
            "Wrap the body",
        )
        reset()

        # The same padding under a mode that removes it, by file. Git cleans a
        # message given with "-F" before it runs this hook, so the hook is
        # handed the 72-character subject git will store and accepts it. The
        # hook and git agree here, and measuring the line as written is what
        # makes them agree.
        stage("trailing-stripped.md", "Twenty-six.\n")
        accepted(
            "a subject padded with spaces under strip cleanup is cleaned first",
            commit_cleanup("feat: " + "x" * 66 + " " * 20 + "\n", "strip"),
        )
        reset()

        # The editor path, which is where the mode is genuinely invisible. Git
        # cleans a message given with "-F" before this hook and an edited
        # message after it, so on this path the hook is handed the padded
        # subject whatever mode is in force. It cannot tell a default commit,
        # which would store 72, from a verbatim one, which would store 92, so
        # it answers for the mode that stores the most and refuses. The cost is
        # on the record: this commit would have been legal after git's default
        # cleanup, and the message names the trailing whitespace so the author
        # can see what to remove.
        pad = Path(name) / "editor-pad.py"
        pad.write_text(
            "import pathlib, sys\n"
            "pathlib.Path(sys.argv[1]).write_text(\n"
            "    'feat: ' + 'x' * 66 + ' ' * 20 + '\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        stage("trailing-editor.md", "Twenty-eight.\n")
        refused(
            "an editor subject padded with spaces is refused under any mode",
            in_repo(
                repo,
                "commit",
                "-q",
                env=hook_env(repo, GIT_EDITOR=f'"{sys.executable}" "{pad}"'),
            ),
            "trailing whitespace",
        )
        reset()

        # Trailing whitespace below the limit is not a violation. Nothing here
        # forbids it, and this fix did not start forbidding it.
        stage("trailing-short.md", "Twenty-seven.\n")
        accepted(
            "a short subject with trailing spaces under verbatim cleanup",
            commit_cleanup("feat: probe short trailing   \n", "verbatim"),
        )
        reset()

        # Leading indentation is not a break point, so an indented long token
        # is accepted at every width, exactly as the bare token is.
        stage("indented-token.md", "Twenty-one.\n")
        accepted(
            "a long token indented by two spaces under verbatim cleanup",
            commit_cleanup(
                "feat: probe indentation\n\n  https://example.com/" + "a" * 80 + "\n",
                "verbatim",
            ),
        )
        reset()
        stage("indented-prose.md", "Twenty-two.\n")
        refused(
            "prose indented by two spaces under verbatim cleanup",
            commit_cleanup(
                "feat: probe indentation\n\n  " + "word " * 20 + "\n",
                "verbatim",
            ),
            "Wrap the body",
        )
        reset()

        # An abandoned editor commit must not lend its recording to the next
        # one. The editor here fails, so git records the template and makes no
        # commit. The message supplied by file afterwards is that exact
        # template with a subject on its first line, which is the strongest
        # form of the reuse: only a recording that survived could excuse it.
        stage(
            "a-directory-with-a-long-name/another-one/a-third-long-name.md",
            "Fourteen.\n",
        )
        in_repo(repo, "commit", "-q", "-v", env=hook_env(repo, GIT_EDITOR="false"))
        left_behind = (repo / ".git" / "COMMIT_EDITMSG").read_text(encoding="utf-8")
        check(
            "the abandoned editor commit left git's template behind",
            "Changes to be committed" in left_behind and "diff --git" in left_behind,
            f"template was {left_behind[:120]!r}",
        )
        reused = left_behind.split("\n")
        reused[0] = "feat: probe the message"
        refused(
            "a recording is not reused by the next commit",
            commit_cleanup("\n".join(reused), "verbatim"),
            "Wrap the body",
        )
        reset()

        # "git am" runs none of the four commit hooks. It runs applypatch-msg
        # before it applies the patch and pre-applypatch before it commits, so
        # both carry policy. Each patch below is made with the hooks switched
        # off, the way history that arrives by mail is.
        # Both live outside the working tree, so "git clean" cannot remove
        # them between probes.
        no_hooks = Path(name) / "no-hooks"
        no_hooks.mkdir(exist_ok=True)
        patches = Path(name) / "patches"

        def mail_patch(name: str, path: str, content: str, message: str) -> str:
            """One patch file, committed on a throwaway branch without hooks."""
            in_repo(repo, "switch", "-q", "-c", f"chore/mail-{name}", "main")
            stage(path, content)
            in_repo(
                repo,
                "-c",
                f"core.hooksPath={no_hooks}",
                "commit",
                "-q",
                "-m",
                message,
                env=hook_env(repo),
            )
            in_repo(
                repo, "format-patch", "-1", "-o", str(patches / name), "--no-signature"
            )
            in_repo(repo, "switch", "-q", "main")
            return str(sorted((patches / name).glob("*.patch"))[0])

        clean_patch = mail_patch(
            "clean", "mailed.txt", "Mailed.\n", "feat: add a mailed file"
        )
        subject_patch = mail_patch(
            "subject", "mailed-subject.txt", "Mailed.\n", "arbitrary mailed subject"
        )
        keep_out_patch = mail_patch(
            "keepout",
            "tmp/mailed-notes.md",
            "Local state " + chr(0x2014) + " mailed.\n",
            "chore: add local state",
        )

        def am_refused(name: str, patch: str, needle: str) -> None:
            before = head_count()
            refused(name, in_repo(repo, "am", patch), needle)
            check(
                f"{name} created no commit",
                head_count() == before,
                f"HEAD moved from {before} to {head_count()} commits",
            )
            in_repo(repo, "am", "--abort")
            reset()

        am_refused("mailed patch on main", clean_patch, "on 'main'")

        in_repo(repo, "switch", "-q", "-c", "chore/mail-target", "main")
        am_refused("mailed patch with a bad subject", subject_patch, "conventional")
        am_refused("mailed keep-out path", keep_out_patch, "coordination state")
        accepted(
            "clean mailed patch on a typed branch", in_repo(repo, "am", clean_patch)
        )
        reset()

        # Bytes that are not utf-8, driven through real git. Git stores bytes,
        # so both a path and a diff can carry them, and the hooks have to keep
        # two byte strings apart that render the same.
        #
        # The path probe stages its name through the index rather than the
        # working tree, because a filesystem may refuse to create it.
        raw_blob = (
            subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=repo,
                input=("Local state " + chr(0x2014) + " staged.\n").encode("utf-8"),
                capture_output=True,
                env=hook_env(repo),
            )
            .stdout.decode()
            .strip()
        )
        in_repo(
            repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{raw_blob},note-\udcff.md",
        )
        refused(
            "a staged path that is not utf-8 is still scanned",
            commit("chore: probe a path that is not utf-8"),
            "em dash",
        )
        in_repo(repo, "read-tree", "HEAD")
        reset()

        # A verbose commit whose diff carries such a byte. The recording and
        # the message hold the same bytes, so the template survives and the
        # commit is accepted. Refusing malformed input at the boundary instead
        # would have refused this ordinary commit.
        # The staged line is past the column limit as well as malformed, so
        # the diff below the scissors bar is exempt only while the recording
        # holds. That is what the next two probes turn on.
        raw_line = b"latin \xff bytes " + b"word " * 20 + b"\n"
        (repo / "raw-bytes.txt").write_bytes(raw_line)
        in_repo(repo, "add", "--", "raw-bytes.txt")
        keep = Path(name) / "editor-keep.py"
        keep.write_text(
            "import pathlib, sys\n"
            "path = pathlib.Path(sys.argv[1])\n"
            "path.write_bytes(b'chore: probe a diff that is not utf-8\\n'\n"
            "                 + path.read_bytes())\n",
            encoding="utf-8",
        )
        accepted(
            "a verbose diff carrying a raw byte is excused",
            in_repo(
                repo,
                "-c",
                "commit.verbose=true",
                "commit",
                "-q",
                env=hook_env(repo, GIT_EDITOR=f'"{sys.executable}" "{keep}"'),
            ),
        )
        reset()

        # The same commit with that one byte changed to a different malformed
        # sequence. Both render as one replacement character, so a lossy decode
        # compared the two templates equal and kept the exemption. The bytes
        # differ, so this is an edit to git's text, and the overlong diff line
        # below it is measured like any other body line.
        (repo / "raw-bytes-again.txt").write_bytes(raw_line)
        in_repo(repo, "add", "--", "raw-bytes-again.txt")
        swap_byte = Path(name) / "editor-swap-byte.py"
        swap_byte.write_text(
            "import pathlib, sys\n"
            "path = pathlib.Path(sys.argv[1])\n"
            "raw = path.read_bytes().replace(b'\\xff', b'\\xe2\\x82', 1)\n"
            "path.write_bytes(b'chore: probe a swapped raw byte\\n' + raw)\n",
            encoding="utf-8",
        )
        refused(
            "a raw byte swapped inside git's own text excuses nothing",
            in_repo(
                repo,
                "-c",
                "commit.verbose=true",
                "commit",
                "-q",
                env=hook_env(repo, GIT_EDITOR=f'"{sys.executable}" "{swap_byte}"'),
            ),
            "Wrap the body",
        )
        reset()

    return failures, total


def self_test() -> int:
    for name in ("PYTEST_GQL_ALLOW_MAIN", "PYTEST_GQL_ALLOW_SPEC_EDIT"):
        os.environ.pop(name, None)

    failures = 0
    total = 0

    for name, branch, expect_problem in BRANCH_CASES:
        total += 1
        problems = check_branch(branch)
        if bool(problems) != expect_problem:
            failures += 1
            print(f"FAIL branch/{name}: got {problems}")

    for name, path, present, expect_problem in PATH_CASES:
        total += 1
        problems = check_path(path, present=present)
        if bool(problems) != expect_problem:
            failures += 1
            print(f"FAIL path/{name}: got {problems}")

    for name, raw, expected in NAME_STATUS_CASES:
        total += 1
        records = parse_name_status(raw)
        paths: list[tuple[str, bool]] = []
        for status, record in records:
            if status in ("R", "C"):
                paths.extend([(record[0], False), (record[1], True)])
            elif status in ("D", "U"):
                paths.append((record[0], False))
            else:
                paths.append((record[0], True))
        if paths != expected:
            failures += 1
            print(f"FAIL name-status/{name}: expected {expected}, got {paths}")

    for name, line, expect_wrappable in WRAP_CASES:
        total += 1
        if wrappable(line) != expect_wrappable:
            failures += 1
            print(f"FAIL wrap/{name}: got {wrappable(line)}")

    for name, line, limit, expect_note in NOTE_CASES:
        total += 1
        if bool(trailing_note(line, limit)) != expect_note:
            failures += 1
            print(f"FAIL note/{name}: got {trailing_note(line, limit)!r}")

    for name, message, expect_problem in MESSAGE_CASES:
        total += 1
        problems = check_message(message)
        if bool(problems) != expect_problem:
            failures += 1
            print(f"FAIL message/{name}: got {problems}")

    for name, template, message, expect_problem in TEMPLATE_CASES:
        total += 1
        recorded = message_lines(template)
        problems = check_message(message, recorded=recorded)
        if bool(problems) != expect_problem:
            failures += 1
            print(f"FAIL template/{name}: got {problems}")

    for name, message, pointer in POINTER_CASES:
        total += 1
        problems = check_message(message)
        if not any(pointer in problem for problem in problems):
            failures += 1
            print(f"FAIL pointer/{name}: got {problems}")

    # The other two refusals that name a document. The branch rule stayed in
    # AGENTS.md when the measurement detail moved out, so its refusal still
    # points there. Both run before the override cases below set the
    # environment variables that would turn these refusals off.
    total += 1
    problems = check_branch("main")
    if not any("AGENTS.md" in problem for problem in problems):
        failures += 1
        print(f"FAIL pointer/a protected branch: got {problems}")

    total += 1
    problems = check_path(SPEC_PATH, present=True)
    if not any(DECISIONS_PATH in problem for problem in problems):
        failures += 1
        print(f"FAIL pointer/the specification guard: got {problems}")

    for name, raw in DECODE_CASES:
        total += 1
        if decode_git(raw).encode("utf-8", "surrogateescape") != raw:
            failures += 1
            print(f"FAIL decode/{name}: the text does not return to its bytes")

    total += 1
    if decode_git(b"\xff") == decode_git(b"\xe2\x82"):
        failures += 1
        print("FAIL decode/distinct: two different byte strings decoded the same")

    # report() writes a path or a branch name that is not utf-8 without
    # escaping it, on the strength of the error handler python gives stderr.
    # This case is what says so out loud.
    total += 1
    try:
        raw_name = b"note-\xff.md"
        refusal = f"refusing to commit {decode_git(raw_name)!r}"
        refusal.encode(sys.stderr.encoding or "utf-8", sys.stderr.errors or "strict")
    except UnicodeEncodeError:
        failures += 1
        print("FAIL decode/stderr: a refusal naming a raw byte cannot be written")

    total += 1
    os.environ["PYTEST_GQL_ALLOW_MAIN"] = "1"
    if check_branch("main"):
        failures += 1
        print("FAIL branch/override: PYTEST_GQL_ALLOW_MAIN did not permit main")
    os.environ.pop("PYTEST_GQL_ALLOW_MAIN")

    total += 1
    os.environ["PYTEST_GQL_ALLOW_SPEC_EDIT"] = "1"
    if check_path(SPEC_PATH, present=True) or check_path(SPEC_PATH, present=False):
        failures += 1
        print(
            "FAIL path/override: PYTEST_GQL_ALLOW_SPEC_EDIT did not permit the change"
        )
    os.environ.pop("PYTEST_GQL_ALLOW_SPEC_EDIT")

    integration_failures, integration_total = integration_test()
    failures += integration_failures
    total += integration_total

    if failures:
        print(f"\n{failures} of {total} self-test case(s) failed.")
        return 1

    print(f"self-test: {total} of {total} cases passed.")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2

    command = argv[0]

    if command == "--self-test":
        if len(argv) > 1:
            print("error: --self-test takes no arguments", file=sys.stderr)
            return 2
        return self_test()

    if command == "pre-commit":
        if len(argv) > 1:
            print("error: pre-commit takes no arguments", file=sys.stderr)
            return 2
        return pre_commit()

    if command == "pre-merge-commit":
        if len(argv) > 1:
            print("error: pre-merge-commit takes no arguments", file=sys.stderr)
            return 2
        return pre_merge_commit()

    if command == "pre-applypatch":
        if len(argv) > 1:
            print("error: pre-applypatch takes no arguments", file=sys.stderr)
            return 2
        return pre_applypatch()

    if command == "prepare-commit-msg":
        # Git passes the message file, and sometimes a source and a commit. The
        # commit is not read, and the arguments are not rejected when git adds
        # one, because that would only break the hook.
        return prepare_commit_msg(
            argv[1] if len(argv) > 1 else "",
            argv[2] if len(argv) > 2 else "",
        )

    if command == "commit-msg":
        if len(argv) != 2:
            print("error: commit-msg takes one message file", file=sys.stderr)
            return 2
        return commit_msg(argv[1])

    if command == "applypatch-msg":
        if len(argv) != 2:
            print("error: applypatch-msg takes one message file", file=sys.stderr)
            return 2
        return applypatch_msg(argv[1])

    print(f"error: unknown command {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
