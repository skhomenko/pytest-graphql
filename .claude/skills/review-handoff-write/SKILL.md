---
name: review-handoff-write
description: >
  Persist a full pytest-graphql code review in the branch-specific ignored handoff
  log while keeping chat output compact. ALWAYS use when asked in prose to review a
  branch, pull request, commit range, patch, design document, or working-tree
  changes. This skill records review findings; it does not implement fixes or
  respond to an existing review. Do NOT use it to replace an explicitly invoked
  `/code-review`, which owns its own review procedure and output format.
---

# Write a code review handoff

Perform the requested review under the repository rules, including the mandatory
security assessment. The log records the review; it does not replace evidence
gathering.

Scope the review with the "Review scope ceiling" section of
`docs/reference/CODE_REVIEW_HANDOFF.md`: the diff under review plus the paths it
directly affects, and not a re-derivation of pre-existing conditions an earlier
cycle already reported. Whole-file obligations still apply where the change
republishes or re-versions a complete artifact; discharge them by scanning the
file, not by reading it end to end. Run the deterministic checks listed under
"Mechanical checks" as commands instead of deciding them by reading.

Apply the "Repository review checklist" in the same reference to the invariants
the diff can break. Those come from `docs/reference/DESIGN_DECISIONS.md` and
`docs/reference/SPEC.md`. Where the two documents disagree, the decisions
document is the authority.

## Relationship to `/code-review`

This skill governs how a review is recorded, not which review procedure runs.
When the user explicitly invokes `/code-review`, that command owns the procedure
and its chat output format; do not silently redirect it here.

Recording is not optional. Every review of this repository ends as a cycle in the
log, whatever produced it. After `/code-review` reports its result, append that
result as a cycle using the schema in the reference, keeping its severity
assessment and evidence. An unrecorded review cannot be resolved, disputed, or
verified by a later cycle, and Codex cannot see it at all.

## Select the log

Read `docs/reference/CODE_REVIEW_HANDOFF.md`. Follow its primary-checkout path,
shell-safety, schema, and append-only contract. Resolve the current branch and
short HEAD from Git. Inspect the existing log before choosing the cycle ID so
retries cannot collide. Never fall back to a linked worktree's relative `tmp/`.

If the log does not exist, create the header defined in the reference.

## Append one review cycle

Append the exact review-cycle schema from the reference. Include every reviewed
artifact in the fingerprint mode appropriate to the comparison. Link the previous
review cycle when one exists.

Use review-quality file and line references. Do not create generic security or
hardening findings unrelated to the change. If no actionable findings exist,
append a clean cycle with the verification and residual-risk statement.

Separate the observed failure, root cause, impact, and recommended resolution.
Record an evidence-backed root cause and correction when established. Write
`Not established` for either one rather than speculate; an evidenced defect does
not become unactionable because the reviewer has not completed its diagnosis.

Re-read the log tail immediately before the final append. Preserve any concurrent
entry and choose a new cycle suffix if another reviewer used the intended ID.

Never modify source files while performing a review unless the user separately
asks for fixes.

## Chat handoff

Return only the cycle ID, log path, severity counts, and a one-line outcome. The
log contains the full report.
