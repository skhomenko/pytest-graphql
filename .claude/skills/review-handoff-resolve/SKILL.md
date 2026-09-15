---
name: review-handoff-resolve
description: >
  Resolve or answer pytest-graphql review findings from the branch-specific ignored
  handoff log. ALWAYS use when asked to fix, address, respond to, rebut, or provide
  pushback on a recorded code review. This skill implements authorized fixes and
  appends evidence-backed responses; it does not perform the follow-up review.
---

# Resolve a code review handoff

Read `docs/reference/CODE_REVIEW_HANDOFF.md`. Preserve its shared-path,
shell-safety, schema, and append-only contract.

## Select the review cycle

Resolve the current branch and its primary-checkout log path defined in the
reference. If the user names a cycle, use it. Otherwise select the latest cycle
with findings that do not yet have a final coder disposition. If the log or
matching cycle does not exist, report that briefly and do not invent findings.
Never fall back to a linked worktree's relative `tmp/`.

Confirm that the cycle belongs to the current branch. If it does not, stop and
ask the user to identify the intended branch or cycle. Compare its reviewed HEAD,
working-tree description, and content fingerprint with current Git state. If
reviewed content changed, re-read the affected artifacts and revalidate every
finding before editing. Never apply a stale recommendation blindly. Record an
already corrected finding as `fixed` with the later-change evidence.

## Address findings

Treat the user's request to address the review as authorization to make only the
minimal in-repository changes needed for those findings. It does not authorize
deployments, external messages, account changes, destructive actions, or
unrelated refactors. It does not authorize `git push`, a pull request, or a
commit on `main`.

For every finding you intend to fix, independently reproduce or otherwise
validate the defect and establish its root cause before editing product code.
The reviewer's root-cause analysis and recommendation are inputs to assess, not
instructions to copy. A missing analysis or recommendation is not a blocker and
does not require another user instruction once resolution has been requested.
If the cause cannot be established after safe in-scope investigation, do not
mark the finding `fixed`; dispute it with evidence or defer it with the remaining
blocker.

Write the property before you edit product code. The property is the general
rule the finding breaks, stated without the reviewer's example. Then search the
artifact for the property's other sites and fix them in the same response.
"Minimal" below means no unrelated refactor. It does not mean the cited line
alone: a fix that leaves a sibling site open is not minimal, it is incomplete,
and the next cycle reports it as a new finding. The reference's "Fix
completeness" section states the rule and the response fields that record it.

For every finding:

- `fixed`: state the property, sweep its other sites, implement the correction,
  and verify it in proportion to risk;
- `disputed`: make no code change for that finding and give concrete evidence and
  reasoning;
- `deferred`: explain the blocker, dependency, or explicit scope decision and
  state what would unblock it.

Assess the reviewer's root-cause analysis separately from the demonstrated
defect. For a fixed finding, accept it, correct it with evidence, or establish a
cause the reviewer left unknown; a fixed finding cannot leave the cause
unestablished. A dispute or deferral may state that it remains unestablished and
why. The recommended resolution is non-binding. Choose the correction that best
closes the property. If you implement a materially different approach, record
what you chose and why; that choice alone does not make the finding `disputed`.

Follow all applicable repository and domain skills while editing. Keep the
project invariants in the reference's "Repository review checklist" intact: a fix
must not introduce a pytest import into the core, an unbounded accumulator, a
builtin `hash()` seed, or a wire-format key outside the transport and
document-assembly layers.

Run the mechanical checks that cover the files you changed. Do not change the
original review text or status. Do not describe a fix as reviewer-verified; that
requires a later review cycle.

## Append the coder response

At the end of the log, append the exact response schema from
`docs/reference/CODE_REVIEW_HANDOFF.md`. Include the responder, UTC timestamp,
current HEAD and working-tree state, then one entry per finding with
disposition, property, root-cause assessment, resolution approach, rationale,
modified files, sweep and verification evidence. Follow the reference's rules
for which fields are substantive or `None` for each disposition. Include
remaining risks or blocked work. Re-read the log tail immediately before
appending so concurrent entries are preserved.

## Chat handoff

Return only the cycle ID, counts by disposition, verification result, log path,
and a one-line outcome. Keep detailed fixes and pushback in the log.
