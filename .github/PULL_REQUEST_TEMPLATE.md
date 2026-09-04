## Summary

_What changed and why, in 1 to 3 sentences. Replace this line._

## Related

_Milestone from `PLAN.md` section 3 (M0 to M10), amendment IDs from section 2
(for example A3, B10, D1), and the `SPEC.md` sections involved. Write "None." if
nothing applies. Replace this line._

## Changes

-

## Mechanical checks

_Check what ran. Mark a line `N/A: <reason>` instead of deleting it. Before
milestone M0 lands, mark the tooling lines `not available yet`._

- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy --strict src/`
- [ ] `uv run pytest -q`
- [ ] `uv build && uv run twine check dist/*` (packaging changes)
- [ ] `uv run mkdocs build --strict` (docs changes)
- [ ] `python3 scripts/check_publication_hygiene.py <paths>` (any file that
      leaves the repository under the maintainer's name)
- [ ] `python3 scripts/check_publication_hygiene.py --self-test` (the scanner
      itself changed)

Manual verification steps:

-

## Project invariants

_Check the ones this diff can touch. Mark the rest `N/A`. The authority is the
checklist in `docs/reference/CODE_REVIEW_HANDOFF.md`._

- [ ] Core stays pytest-free (D1). No pytest import outside
      `src/pytest_graphql/plugin/`.
- [ ] Exception naming (D2). The base class is `GraphQLTestError`, never
      `GraphQLError`.
- [ ] snake_case boundary (D3). Wire-format keys stay inside the transport and
      document-assembly layers.
- [ ] Deterministic seeding (B1). No builtin `hash()` in any seed path.
- [ ] Bounded state (B3). Accumulating structures stay bounded.
- [ ] Name resolution by lookup (B10). No wire name regenerated from a snake
      name.
- [ ] Auto-selection rules (A6, B12). Fields with required arguments are
      skipped. `__typename` is always emitted and never counted against
      `max_fields`.
- [ ] No network in unit tests (A8). Loopback only.
- [ ] Configuration precedence (A3) is unchanged, or the change is stated above.

## Design authority

- [ ] `PLAN.md` updated if this PR takes a new decision or adds an amendment.
- [ ] `SPEC.md` left unedited, or the edit is a genuine spec change and not a
      rewrite to match `PLAN.md`.
- [ ] Public API change is reflected in the docs, or `N/A`.

## Notes

_Risks, follow-up work, or open questions. Write "None." if nothing applies.
Replace this line._
