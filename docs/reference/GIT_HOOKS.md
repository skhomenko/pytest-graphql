# Git hooks

This file describes the tracked Git hooks: which hook runs on which Git path,
what each one checks, and what a refusal leaves behind. It is reference
material, not a rule you must read before you act.

`AGENTS.md` holds the rules the hooks enforce, including the prohibition on
bypassing a hook. `docs/reference/COMMIT_MESSAGE_RULES.md` states how a commit
message line is measured.

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
