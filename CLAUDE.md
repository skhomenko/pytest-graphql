# pytest-graphql Claude instructions

@AGENTS.md

Root `AGENTS.md` (imported above) is the canonical shared contract for every
agent working in this repository, including Claude and Codex. Follow it
completely. This file contains only Claude-specific additions. Do not add shared
repository rules here. If anything here conflicts with `AGENTS.md`, `AGENTS.md`
governs shared repository policy. Higher-priority system and user instructions
still take precedence over both.

## Claude-specific instructions

- **Skills implement the shared procedures.** The repo skills under
  `.claude/skills/` are the Claude mechanism for the `AGENTS.md` section "Code
  review handoff". Use `review-handoff-write` when asked to review a branch, pull
  request, commit range, patch, design document, or working-tree change. Use
  `review-handoff-resolve` when asked to fix, answer, or push back on a recorded
  review. Both read `docs/reference/CODE_REVIEW_HANDOFF.md`, which is the
  authority for the log format.
- **`/code-review` keeps its own procedure, not its own record.** When the
  maintainer invokes it explicitly, that command owns the review procedure and
  its chat output format. Do not redirect it to the skill. Append its result to
  the handoff log as a cycle once it finishes, so the findings stay discoverable
  by the resolver and by later reviewers.
- Codex writes to the same log through the reference document directly. Do not
  assume a cycle in the log was written by Claude.
