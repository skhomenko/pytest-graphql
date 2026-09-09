# Code Review Handoff Protocol

> **Status:** Reference runbook for local reviewer-to-coder coordination.
> **Audience:** agents and contributors. This file is repository documentation,
> not published site content. Exclude `docs/reference/` from the MkDocs nav and
> from the built site once the documentation site exists.

Code reviews and their implementation responses use a local, append-only handoff
log. Reviewers and coders exchange full detail through the log instead of filling
chat context. This document is the single authority for the protocol: log-path
resolution, invariants, ID formats, fingerprint modes, and the Markdown schemas.

Two skills use it:

- `.claude/skills/review-handoff-write` records a review cycle.
- `.claude/skills/review-handoff-resolve` records a coder response.

## Invariants

These hold regardless of schema:

- The log lives under the primary repository checkout, never a linked worktree's
  relative `tmp/`. `tmp/` is Git-ignored: review logs are local coordination
  state, never durable product truth and never PR content.
- Treat branch names and all Git-derived values as untrusted shell data. Pass
  them as quoted arguments or through non-evaluating file APIs. Never place them
  in executable shell text and never use `eval`.
- The log is append-only. Never rewrite or delete an earlier cycle or response. A
  follow-up review appends a new cycle naming its predecessor. It does not edit
  statuses in the old one. Re-read the log tail immediately before appending and
  preserve any concurrent entry.
- Every cycle records its scope, the verification performed, and a security
  assessment. A clean review still appends a cycle stating that no findings were
  found.
- A review is scoped to the diff under review plus the paths it directly affects.
  Do not re-derive a pre-existing condition that an earlier cycle already
  reported. Reference its finding ID instead.
- A coder response records `fixed`, `disputed`, or `deferred` for each finding,
  with rationale, modified files, and verification evidence. Do not alter the
  original finding and do not claim that a fix is reviewer-verified. Only a later
  review cycle can verify a claimed fix.
- Never copy secrets, credentials, personal data, private keys, or unnecessary
  proprietary content into a review log.
- Chat output stays compact: cycle ID, log path, severity counts, and a one-line
  outcome. Keep the complete findings or response in the log.

## Shared log location

Resolve the absolute Git common directory:

```text
git rev-parse --path-format=absolute --git-common-dir
```

For this non-bare repository, the parent of that directory is the primary
checkout. Store all branch logs below:

```text
<primary-checkout>/tmp/reviews/branches/<branch-name>/REVIEW_LOG.md
```

When HEAD is detached, there is no branch name to key the log on. Use the short
HEAD instead:

```text
<primary-checkout>/tmp/reviews/detached/<short-head>/REVIEW_LOG.md
```

Do not use a relative `tmp/` path from a linked worktree. Preserve branch slashes
as directories. Treat the branch name as data: use quoted arguments or a file
API, never executable shell interpolation.

## Review scope ceiling

Review the change, not the whole branch. The default scope is the diff under
review plus the paths it directly affects: callers, callees, tests covering the
changed behavior, and any file whose behavior the diff alters. Read a file in
full only when the diff touches it and surrounding context is needed to judge
the change.

A change that re-versions, republishes, or regenerates a complete artifact puts
that entire artifact in scope, not only its changed lines. Version bumps,
regenerated files, and vendored updates are republication: a condition that
predates the diff becomes newly published by it, and the publication-hygiene
rules apply to the exact final artifact. Discharge that obligation with a scan
over the whole file rather than by reading it end to end.

Do not re-derive pre-existing conditions on every cycle. A defect that predates
the diff is reported once, in the cycle that first found it, and referenced by
its finding ID thereafter. A follow-up cycle examines what changed since the
preceding cycle plus every finding still `unresolved`.

Widen the scope only when the diff's blast radius requires it, or when the user
asks for a branch-wide audit. State the widened scope and the reason in the
cycle's `Scope` field.

A cycle records the files it reviewed. That record is an audit trail, not an
instruction to re-read every listed file on the next iteration.

## Mechanical checks

Deterministic checks run as commands. Do not reconstruct by reading what a
command can decide. Run the narrowest form that covers the change:

| Check | Command |
|-------|---------|
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format --check .` |
| Types | `uv run mypy --strict src/` |
| Tests | `uv run pytest -q <narrowest path>` |
| Packaging (on packaging changes) | `uv build && uv run twine check dist/*` |
| Docs (on docs changes) | `uv run mkdocs build --strict` |
| Publication hygiene | `python3 scripts/check_publication_hygiene.py <paths>` |

Before project tooling exists, record the check as `not available yet` rather
than skipping it silently.

### Publication hygiene

Run the repository scanner over the exact final artifacts for any file that
leaves the repository under the maintainer's name: Markdown docs, README,
commit-message drafts, and generated site content.

```bash
python3 scripts/check_publication_hygiene.py <paths>
```

It exits 0 when clean, 1 when it reports findings, and 2 on a usage or read
error. It needs only Python 3.10 and the standard library, so it works before
project tooling exists and on any platform. Do not substitute an ad hoc `grep`. The
system `grep` on macOS is BSD grep, which rejects `-P` and cannot express these
patterns.

The scanner reports, as `path:line:column`:

- em dash `U+2014`, and en dash `U+2013` outside a numeric range;
- invisible characters: `U+00A0`, `U+00AD`, `U+180E`, `U+200B`, `U+200C`,
  `U+200D`, `U+2060`, `U+FEFF`, and any other Unicode format character;
- bidirectional controls: `U+200E`, `U+200F`, `U+202A` to `U+202E`, and `U+2066`
  to `U+2069`;
- Unicode tag characters, `U+E0000` to `U+E007F`;
- HTML comments;
- URL tracking parameters: `utm_*`, `fbclid`, `gclid`, `msclkid`, `igshid`, and
  a `ref` query parameter.

Exemptions are deliberately narrow, because a missed violation is a published
defect while a false positive costs one code span:

- Code exemptions apply to Markdown artifacts only, selected by file suffix. In
  any other file, backticks and fence markers carry no meaning and every line is
  scanned as visible text.
- A fenced block opens on a run of at least three backticks or tildes indented by
  at most three spaces. It closes only on a run of the same character that is at
  least as long and carries no info string. A different fence character does not
  close it, and a shorter run does not close it.
- A line indented by four or more spaces is never treated as a fence marker.
  CommonMark reads it as an indented code block, and its content is still
  scanned.
- A code span needs a closing backtick run of exactly the opening length. An
  unmatched or unequal run is literal text and stays scanned.
- Hidden-payload checks have no exemption at all. An invisible, bidirectional, or
  tag character is a finding wherever it sits, inside code and fences included.

The scanner carries its own regression suite covering these rules:

```bash
python3 scripts/check_publication_hygiene.py --self-test
```

Run it after any edit to the script. Treat a failure as a blocking defect in the
check itself. Until `tests/` exists, this is the only regression net the scanner
has, and the suite should become a pytest case then.

The scanner covers text only. Image, PDF, and Office metadata still needs its own
inspection before publication.

## Repository review checklist

These are project invariants from `docs/reference/DESIGN_DECISIONS.md` and
`docs/reference/SPEC.md`. Check the ones the diff can break. Where the two
documents disagree, the decisions document is the authority. The section named
after each invariant is where its full statement lives.

- **Core stays pytest-free (Product boundaries).** No pytest import outside
  `src/pytest_graphql/plugin/`. Verify by command:
  `grep -rn 'import pytest\|from pytest' src/pytest_graphql/ | grep -v '/plugin/'`
  must print nothing.
- **Exception naming (Product boundaries).** The base class is
  `GraphQLTestError`, never `GraphQLError`, which belongs to `graphql-core`.
- **snake_case boundary (Product boundaries).** snake_case in, snake_case out.
  Wire-format keys appear only inside the transport and document-assembly layers.
- **Deterministic seeding (Selection and deterministic data).** No use of the
  builtin `hash()` for any seed. Python salts it per process. Use the documented
  SHA-256 derivation.
- **Bounded state (Diagnostics and sensitive data).** The diagnostics recorder
  and any other accumulating structure must be bounded, default 50 calls.
- **Name resolution by lookup (Configuration and call grammar).** Resolve a
  snake_case name through the built index. Never regenerate a wire name from a
  snake name.
- **Auto-selection rules (Selection and deterministic data).** Skip any field
  with unsupplied required arguments, scalar fields included. Always emit
  `__typename` and do not count it against `max_fields`.
- **No network in unit tests (Compatibility and verification).** The socket
  guard allows loopback only. A new test that reaches a public host is a
  finding.
- **Configuration precedence (Configuration and call grammar).** Per-call
  argument, then CLI flag, then fixture, then environment variable, then ini,
  then default.

## Reviewed-content fingerprint

The cycle must identify the exact source that was reviewed. Select one mode:

- **Committed comparison or PR:** resolve every comparison endpoint to its full
  commit SHA and record the immutable `<base-sha>...<target-sha>` range. Do not
  record a moving symbolic ref such as `main` as the fingerprint. Unrelated dirty
  working-tree content is not part of this mode.
- **Working-tree comparison:** record the full HEAD SHA, exact scoped
  `git status --short` output, and a Git blob hash for every reviewed
  working-tree file using `git hash-object -- <path>`. Record `DELETED` when a
  reviewed file is absent. This mode includes reviewed untracked content.
- **Index-only comparison:** record the full HEAD SHA, exact scoped status, and
  each reviewed index blob from `git rev-parse :<path>`, or `DELETED`.
- **Supplied patch or artifact:** record a SHA-256 hash of the exact supplied
  artifact plus its source description.

Pass every ref and path as a quoted argument. Record the selected mode in the
cycle. The resolver recomputes the same mode before acting.

## Identifiers

- Cycle ID: `CR-<UTC-YYYYMMDDTHHMMSSZ>-<short-head>-<8-hex-random>`
- Response ID: `RESP-<UTC-YYYYMMDDTHHMMSSZ>-<short-head>-<8-hex-random>`
- Finding ID: `<cycle-id>-F01`, numbered in severity order within the cycle.

Regenerate the random suffix on collision. Inspect the existing log before
choosing an ID so a retry cannot reuse one.

## New log header

Create this only when the branch log does not yet exist:

```markdown
# Code Review Handoff Log

Branch: `<branch>`

This file is append-only. Add reviews and responses under their cycle IDs. Never
rewrite or remove an earlier cycle.
```

## Review cycle schema

Append exactly one cycle for every review iteration, including a clean review:

```markdown
## Cycle `<cycle-id>`

- Previous cycle: `<cycle-id>` or `None`
- Reviewer: `<agent>`
- Timestamp (UTC): `<YYYY-MM-DDTHH:MM:SSZ>`
- Branch: `<branch>`
- Reviewed HEAD: `<full-sha>`
- Comparison: `<resolved SHA range, working-tree/index base, or artifact source>`
- Fingerprint mode: `committed|working-tree|index-only|artifact`
- Working tree: `<exact scoped status, clean, or not part of comparison>`
- Scope: `<reviewed paths or components>`
- Result: `<severity counts or no findings>`

### Reviewed source

- `<immutable endpoint SHAs, per-file blob manifest, or artifact SHA-256>`

### Finding `<cycle-id>-F01` `[P0|P1|P2|P3]` `<concise title>`

- Status: `unresolved`
- Evidence: `<path:line and concrete observation>`
- Impact: `<why it matters>`
- Recommended resolution: `<minimal correction>`

### Verification

- `<check>`: `<result>`

### Security assessment

`<actionable security findings, or none, plus material residual risk>`
```

Repeat the finding section in severity order. For a clean cycle, omit finding
sections and write `Result: no findings`.

Severity guide for this repository:

- `P0` data loss, credential leak, or a defect that makes a released version
  unusable.
- `P1` incorrect behavior in a documented API, or a broken build, type check, or
  test suite.
- `P2` a correctness or design problem with a workaround, or a missing test for
  changed behavior.
- `P3` clarity, naming, or documentation.

## Fix completeness

A finding names one site. The defect is usually a property. Before appending a
response, close the property, not the line:

- **Sweep every site the invariant covers.** When a fix establishes a rule or
  raises an existing one, search the artifact for sibling sites and correct them
  in the same cycle. A copy left behind returns as the next cycle's finding.
- **Prefer one named primitive to repeated inline logic.** Cleanup sweeps,
  unwinding, and ownership transfer belong in a single routine that every caller
  uses. A later correction then lands in one place.
- **Test the cross product, not one case per path.** Where failure kinds can
  combine, such as an ordinary error together with an interrupt, exercise both
  orders.
- **Self-review before appending.** Re-read what you just wrote as the next
  reviewer would, and probe your own new code blocks. A defect introduced by a
  fix is indistinguishable, to the next cycle, from one that was missed.

A design change is written into `docs/reference/DESIGN_DECISIONS.md` in place,
as one current rule. Never append a correction beside the rule it corrects.
Layered corrections are review surface: every later reviewer has to rebuild the
current rule from superseded bullets and corrected tables.

## Coder response schema

Append responses at the end of the file. Never insert them into or edit the
original cycle:

```markdown
## Response `<response-id>` to `<cycle-id>`

- Responder: `<agent>`
- Timestamp (UTC): `<YYYY-MM-DDTHH:MM:SSZ>`
- Branch: `<branch>`
- Current HEAD: `<full-sha>`
- Working tree before response: `<exact scoped git status or clean>`

### Finding `<finding-id>`: `fixed|disputed|deferred`

- Rationale: `<what changed or why the finding is challenged or deferred>`
- Modified files: `<paths or None>`
- Verification: `<commands or evidence and results>`

### Remaining risk

`<risk, blocker, or None>`
```

A partial response disposes only the findings it lists. `fixed` and `disputed`
are final coder dispositions for that response. `deferred` remains eligible for a
later response. Only a new review cycle can verify a claimed fix.

## Design-document reviews

Before `src/` exists, reviews target `docs/reference/DESIGN_DECISIONS.md`,
`docs/reference/SPEC.md`, and repository configuration. The protocol is
unchanged, with three adjustments:

- Use the `working-tree` fingerprint mode when a reviewed document is untracked
  or freshly added.
- Record `not available yet` for lint, type, and test checks.
- Always run the publication-hygiene check. Both documents are published text
  under the maintainer's name.
