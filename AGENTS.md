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
| Approved v0.1 design | `SPEC.md` |
| Decisions, spec amendments, build order | `PLAN.md` |
| Code review handoff protocol | `docs/reference/CODE_REVIEW_HANDOFF.md` |

Where `SPEC.md` and `PLAN.md` disagree, `PLAN.md` section 2 governs. It records
31 amendments resolved after the spec was written. Do not implement a spec rule
that section 2 supersedes.

Do not edit `SPEC.md` to match `PLAN.md`. The spec is the historical record of
the approved design. Amendments accumulate in the plan.

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
  rationale, modified files, and verification evidence. Only a later review cycle
  can verify a claimed fix.
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
  characters. Body lines wrap at 72.

## Repository conventions

- Library code lives in `src/pytest_graphql/`. The pytest layer is confined to
  `src/pytest_graphql/plugin/`. No module outside that package may import pytest.
- Tests live in `tests/`. Unit tests must not open a non-loopback socket.
- Branch names use a type prefix and a short hyphenated description: `feat/`,
  `fix/`, `docs/`, `chore/`, `refactor/`, `test/`, or `security/`.
- `docs/reference/` is contributor documentation, not published site content.
  Exclude it from the MkDocs nav and build when the docs milestone lands.
- The repository-root `tmp/` is Git-ignored and holds local agent coordination
  state only. The ignore rule is anchored as `/tmp/`, so a nested directory named
  `tmp` elsewhere in the tree stays tracked.
- `scripts/` holds contributor tooling that must run without the project
  environment. Standard library only, no third-party import.

## Common commands

| Command | What it does |
|---------|-------------|
| `uv sync --all-extras` | Install the development environment |
| `uv run ruff check .` | Lint |
| `uv run ruff format --check .` | Formatting check |
| `uv run mypy --strict src/` | Type check |
| `uv run pytest -q` | Run the test suite |
| `uv build` | Build the distribution |

None of these work yet. Milestone M0 in `PLAN.md` creates the project skeleton.
Until then, record a mechanical check as `not available yet` rather than skipping
it silently.
