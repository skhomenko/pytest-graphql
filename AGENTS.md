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
- Every cycle records its scope, the verification performed, a security
  assessment, and a legal and licensing assessment. A clean review still
  appends a cycle stating that no findings were found.
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

## Legal and licensing assessment

Run a proportionate legal-risk pass while planning, implementing, and reviewing
every project task. First decide whether the work has a legal surface. Do not add
generic legal boilerplate to ordinary implementation work with no relevant
surface, but do not skip the assessment.

Treat a change as legally relevant when it involves any of the following:

- third-party code, schemas, data, documentation, media, fonts, models, generated
  output, examples, or other incorporated material;
- public or private APIs, scraping, crawling, introspection, automated access,
  caching, or redistribution of API output;
- licenses, notices, attribution, source-offer duties, copyleft, patents,
  trademarks, branding, or claims of endorsement;
- personal, confidential, regulated, or user-provided data, including fixtures,
  logs, telemetry, and generated artifacts;
- packaging, releases, documentation, hosted services, commercial use, export,
  or another activity that changes who receives an artifact or how it is used.

Before copying, generating from, committing, packaging, or publishing external
material, establish its provenance and the permission that covers the intended
use. Record the exact source, applicable license or terms version, required
notices, modifications, and redistribution conditions. Public accessibility,
enabled introspection, free access, or an `open source` label is not by itself a
redistribution grant. The repository's root license does not relicense
third-party material.

Inspect the actual distribution boundary, not only the source path. Check wheels,
source distributions, generated documentation, container images, release
archives, and Git history when relevant. Prefer project-authored synthetic
fixtures or summaries of non-expressive facts when third-party rights are
unclear. Never place credentials, personal data, proprietary content, or
unverified third-party artifacts in fixtures or logs.

Treat unresolved permission, incompatible terms, missing required notices, or
material privacy and regulatory uncertainty as a release blocker when the work
would be distributed or used commercially. State what is known, what remains
uncertain, and the least-risk correction. Recommend qualified legal counsel when
actual legal clearance is required. Never claim that an agent's assessment is
legal advice, a guarantee, or lawyer approval.

Every code review includes this assessment. Report concrete legal or licensing
problems as normal findings with the same severity ordering and file or artifact
grounding as correctness and security findings. A clean review still records
that no actionable legal or licensing findings were found and names any material
untested, jurisdiction-dependent, or provenance-dependent residual risk.

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
- `docs/reference/COMMIT_MESSAGE_RULES.md` states exactly how a line is
  measured, and how each exemption is earned and lost. Read it when a hook
  refuses a message, not before.
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
- `.githooks/` holds the tracked Git hooks. They do nothing until
  `git config core.hooksPath .githooks` runs in the clone, and the same
  setting is needed in every linked worktree.
  `docs/reference/GIT_HOOKS.md` describes which hook runs on which Git path
  and what a refusal leaves behind.

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
