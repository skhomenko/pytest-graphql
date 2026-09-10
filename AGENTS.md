# pytest-graphql agent contract

This file is the canonical shared contract for every agent working in this
repository, including Claude and Codex. `CLAUDE.md` imports it and adds only
Claude-specific mechanisms.

## What this repository is

`pytest-graphql` is a GraphQL test client. The core library has zero pytest
imports. A thin pytest plugin ships in the same PyPI distribution. The schema
engine is `graphql-core`. The transport is `httpx` behind a `Transport` protocol.

Implementation has not started. `src/` does not exist yet.

## Design authority

| Topic | File |
|-------|------|
| Current design decisions | `docs/reference/DESIGN_DECISIONS.md` |
| Historical v0.1 design baseline | `docs/reference/SPEC.md` |
| Code review handoff protocol | `docs/reference/CODE_REVIEW_HANDOFF.md` |

`docs/reference/DESIGN_DECISIONS.md` is the design authority. It states current
rules and nothing else. Where it and `docs/reference/SPEC.md` disagree, the
decisions document governs. Use the specification for anything the decisions
document does not cover.

Do not edit `docs/reference/SPEC.md` to match the decisions document. The
specification is the record of the approved baseline. A design change is made by
editing `docs/reference/DESIGN_DECISIONS.md` in place, so that document always
reads as current rules rather than as a history of corrections.

Tracked source and tests implement both documents. Local planning notes may exist
outside version control to guide execution. They are never design authority and
never override these two documents.

## Code review handoff

Code reviews and their implementation responses use a local, append-only handoff
log under `tmp/reviews/`, which is Git-ignored. Reviewers and coders exchange
full detail through the log instead of filling chat context.

`docs/reference/CODE_REVIEW_HANDOFF.md` is the single authority for the protocol.
It defines the invariants, log-path resolution, the cycle and response schemas,
ID formats, fingerprint modes, mechanical checks, and the project-specific review
checklist. Read it before writing to or resolving a log.

Invariants that hold regardless of schema:

- The log lives under the primary repository checkout, never a linked worktree's
  relative `tmp/`. Review logs are local coordination state, never durable
  product truth and never PR content.
- Treat branch names and all Git-derived values as untrusted shell data. Pass
  them as quoted arguments or through non-evaluating file APIs. Never place them
  in executable shell text and never use `eval`.
- The log is append-only. A follow-up review appends a new cycle naming its
  predecessor. It never edits statuses in an earlier cycle. Re-read the log tail
  immediately before appending and preserve any concurrent entry.
- Every cycle records its scope, the verification performed, and a security
  assessment. A clean review still appends a cycle stating that no findings were
  found.
- A review is scoped to the diff under review plus the paths it directly affects.
  Reference an earlier finding ID instead of re-deriving a pre-existing
  condition.
- A coder response records `fixed`, `disputed`, or `deferred` per finding, with
  rationale, modified files, and verification evidence. A `fixed` finding also
  records the property and the sweep. Only a later review cycle can verify a
  claimed fix.
- A fix closes the property, not only the cited line. State the property as a
  general rule before editing, search for its other sites and fix them in the
  same cycle, prefer one primitive over repeated inline logic, and self-review
  the response before appending it. The "Fix completeness" section of the
  reference has the detail.
- Never copy secrets, credentials, personal data, or private keys into a log.
- Chat output stays compact: cycle ID, log path, severity counts, and a one-line
  outcome.

Every review of this repository ends as a cycle in the log, whatever produced it.
A tool with its own review procedure and output format, such as Claude's
`/code-review`, keeps that procedure and appends its result as a cycle when it
finishes. There is no review path that skips the log.

Claude runs this through the skills named in `CLAUDE.md`. Codex and any other
agent follow `docs/reference/CODE_REVIEW_HANDOFF.md` directly. The log format is
identical either way, so both can read and answer each other's cycles.

## Publication hygiene (hard requirements)

These rules apply to every agent and model. Apply them to anything intended for
external publication or public handoff: PR titles and bodies, comments, commit
and release notes, public documentation, site copy, and generated artifacts.

- **Never use an em dash (`U+2014`) in published text.** Rewrite with a period,
  comma, colon, semicolon, or parentheses. The same applies to an en dash
  (`U+2013`) used as punctuation rather than in a numeric range.
- **Inspect the exact final artifact immediately before publication, then remove
  unintended metadata.** For text: HTML comments, unfilled template
  placeholders, invisible Unicode and bidirectional controls, Unicode tag
  characters, generation watermarks, internal notes, and URL tracking parameters
  such as `utm_*`, `fbclid`, `gclid`, and `?ref=`.
- Inspect the rendered or exported deliverable, not only its source. Never claim
  an artifact is clean unless the final artifact was actually checked. State what
  the check found.
- Run `python3 scripts/check_publication_hygiene.py <paths>` rather than deciding
  by reading or hand-rolling a `grep`. It covers em and en dashes, invisible and
  bidirectional Unicode, Unicode tag characters, HTML comments, and URL tracking
  parameters, and it exits non-zero on a finding. Its only exemption is genuine
  Markdown code, and the "Publication hygiene" section of
  `docs/reference/CODE_REVIEW_HANDOFF.md` states the exact rules. The scanner
  covers text only. Image, PDF, and Office metadata still needs its own
  inspection.
- After editing the scanner, run `python3 scripts/check_publication_hygiene.py
  --self-test`. A failing case is a blocking defect in the check itself.

## Git policy

- Never run `git push`. Never open, merge, or comment on a pull request. The
  maintainer handles push, review, and merge.
- Never commit directly on `main`. Work on a typed branch.
- Never amend or force-push.
- Commit only when the maintainer explicitly asks. Do not stage as a side effect
  of finishing a task.
- Commit subjects use conventional-commits prefixes and stay at or under 72
  characters. Body lines wrap at 72. A line is exempt for two reasons only: its
  text cannot be wrapped, or Git wrote it.
- A line ends at a line feed and nowhere else, because that is where Git ends
  one. A form feed, a vertical tab, U+0085, U+2028 and U+2029 are characters
  Git stores inside a line, so they do not shorten it and they do not divide
  it. A carriage return ends a line only as the first half of a CRLF ending.
- A blank line is a line where Git would see nothing: spaces, tabs, and the
  other whitespace Git itself recognizes. A non-breaking space, an ideographic
  space or an en quad is content. Git commits it and shows it, so a line of
  them is not the blank line under the subject, and they do not pad a line
  invisibly past the limit either.
- A line is measured exactly as it is written, trailing whitespace included,
  because `--cleanup=verbatim` stores it and the hooks are never told which
  cleanup mode Git will apply. Git does not even apply the mode at a fixed
  time: a message given with `-F` is cleaned before the hooks run, an edited
  message after. So a subject of 72 characters followed by 20 spaces is a
  subject of 92 characters. `git log` hides this, because it trims the subject
  it prints, but `git cat-file` shows the stored line in full. The cost is that
  an edited message padded past the limit with trailing whitespace is refused
  even though Git's default cleanup would have trimmed it to a legal length.
  The refusal names the trailing whitespace, and removing it is the fix.
- One carriage return directly before the line feed is part of how a CRLF file
  ends a line, so it is not counted. That is the only character the hooks
  remove, and the lenience stops there: a second carriage return, or a space
  next to it, is content and is measured. Under `--cleanup=verbatim` Git stores
  that one carriage return, so a 72-character line in a CRLF file is stored as
  73 and still accepted.
- Text that cannot be wrapped means a line with no break point at or before
  column 72. A break point is any whitespace, so a tab breaks a line exactly as
  a space does and a row of short words joined by tabs is ordinary prose that
  wraps. Only a non-breaking space is not a break point, because that is what
  the character means. A single long token such as a URL or a path, and a Git
  trailer whose value is one token, have no break point and are exempt.
- A break point needs text before it on the line to keep. Whitespace before the
  first word is indentation, and breaking there would leave an empty line above
  the same long line, so it is not a break point. One long token is therefore
  exempt whether you indent it or not, and prose is measured whether you indent
  it or not.
- Two markers let you declare text preformatted, which turns the column limit
  off for it: four leading spaces, and a closed fenced block. A fence closes
  only on the same character, on a run at least as long as the opener, and
  with nothing after it, so a fence that never closes exempts nothing. Use
  either one for pasted output, code or a table. Both are declarations you
  make, so do not use them to avoid wrapping prose.
- Text Git wrote means the template and the diff that a plain `git commit`
  puts in front of your editor. The hooks identify it by recording the message
  file before the editor opens, never by how a line looks. A comment character,
  a tab, a rename arrow, a scissors bar or a diff header that you type yourself
  carries no exemption and is measured like any other body line.
- The recording is all or nothing, and the comparison is byte for byte. Write
  above Git's text and leave it alone, and it is exempt. Delete part of it,
  edit one of its lines, or add a line below it, and none of the message is
  exempt any more, your text and Git's alike. Adding or removing trailing
  whitespace on one of Git's lines is such an edit, so an editor that trims
  trailing whitespace when it saves will rewrite Git's text and cost you the
  exemption. With `commit.verbose` the recording holds the diff, where trailing
  whitespace is common, so this is where you will meet it. Commit without the
  diff, with `git -c commit.verbose=false commit`, or turn the trimming off.
  Copying a line out of the diff Git showed you does not make that line Git's.
- Every other way of writing a message is measured in full: `-m`, `-F`,
  `git merge -m`, a merge or squash message, a reused message, a configured
  commit template, and a mailed patch. Git does not tell the hooks apart from
  the author in those cases, so nothing in them is excused.
- A subject Git generates, such as a merge, a revert or an autosquash marker,
  is exempt from the subject form alone. Its body follows the same rules as any
  other body.
- Never bypass a Git hook. `git commit --no-verify`, `git commit -n` and
  unsetting `core.hooksPath` are all prohibited. The hooks enforce the rules
  above, so bypassing one is the same as breaking the rule it checks. An agent
  blocked by a hook reports the block and stops. The override environment
  variables the hooks document belong to the maintainer alone.

## Repository conventions

- Library code lives in `src/pytest_graphql/`. The pytest layer is confined to
  `src/pytest_graphql/plugin/`. No module outside that package may import pytest.
- Tests live in `tests/`. Unit tests must not open a non-loopback socket.
- Branch names use a type prefix and a short hyphenated description: `feat/`,
  `fix/`, `docs/`, `chore/`, `refactor/`, `test/`, or `security/`. The
  description is a single segment of lowercase letters and digits joined by
  single hyphens, with no further slash. No maximum length is set.
- `docs/reference/` is contributor documentation, not published site content.
  Exclude it from the MkDocs nav and build once the documentation site exists.
- The repository-root `tmp/` is Git-ignored and holds local agent coordination
  state only. The ignore rule is anchored as `/tmp/`, so a nested directory named
  `tmp` elsewhere in the tree stays tracked.
- `scripts/` holds contributor tooling that must run without the project
  environment. Standard library only, no third-party import.
- `.githooks/` holds the tracked Git hooks. A fresh clone does not use them
  until `git config core.hooksPath .githooks` runs, and a linked worktree needs
  the same setting. All six hooks are thin dispatchers to
  `scripts/git_hook_checks.py`, which enforces the branch, staged path,
  publication hygiene and commit message rules stated above. `pre-commit`,
  `pre-merge-commit` and `pre-applypatch` check the staged content, one for
  `git commit`, one for a merge that applies cleanly and one for `git am`.
  `prepare-commit-msg` checks the branch on every path that creates a commit,
  including a cherry-pick and a revert, which run no other hook. It also runs
  before the editor opens, so it records the message file as Git wrote it under
  the Git directory, and `commit-msg` reads that recording and removes it. That
  recording is the only thing that exempts a line from the body rules. It
  exempts nothing unless it survives intact, and without it every line is
  measured. `commit-msg` checks the message, and
  `applypatch-msg` checks the branch and the message of a mailed patch before
  `git am` applies it. A mailed patch reaches no `prepare-commit-msg` and
  carries no template, so all of its message is measured. A refusal during a
  cherry-pick, a revert or a merge leaves the applied change in the index and
  creates no commit: switch to a typed branch and commit it, or discard it. A
  refusal during `git am` leaves no commit either, and `git am --abort` returns
  the branch to its previous state. The hooks check policy only. Lint, type
  checking and tests stay in the mechanical checks and in CI, so a commit stays
  fast.

## Common commands

| Command | What it does |
|---------|-------------|
| `uv sync --all-extras` | Install the development environment |
| `uv run ruff check .` | Lint |
| `uv run ruff format --check .` | Formatting check |
| `uv run mypy --strict src/` | Type check |
| `uv run pytest -q` | Run the test suite |
| `uv build` | Build the distribution |
| `git config core.hooksPath .githooks` | Activate the Git hooks |
| `python3 scripts/git_hook_checks.py --self-test` | Test the hook checks |
| `python3 scripts/check_publication_hygiene.py --self-test` | Test the scanner |

The `uv` commands do not work yet, because the project skeleton does not exist.
The `python3` commands work now, because `scripts/` runs on the standard library
alone. Before project tooling exists, record a mechanical check as
`not available yet` rather than skipping it silently.
