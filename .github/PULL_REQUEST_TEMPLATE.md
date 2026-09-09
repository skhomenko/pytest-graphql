## Summary

_What changed and why, in 1 to 3 sentences. Replace this line._

## Related

_Related issues, and the sections of `docs/reference/DESIGN_DECISIONS.md` this
PR implements or changes. Write "None." if nothing applies. Replace this line._

## Changes

-

## Mechanical checks

_Check what ran. Mark a line `N/A: <reason>` instead of deleting it. Before
project tooling exists, mark the tooling lines `not available yet`._

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

- [ ] Core stays pytest-free. No pytest import outside
      `src/pytest_graphql/plugin/`. (Product boundaries)
- [ ] Exception naming. The base class is `GraphQLTestError`, never
      `GraphQLError`. (Product boundaries)
- [ ] snake_case boundary. Wire-format keys stay inside the transport and
      document-assembly layers. (Product boundaries)
- [ ] Deterministic seeding. No builtin `hash()` in any seed path. (Selection
      and deterministic data)
- [ ] Bounded state. Accumulating structures stay bounded. (Diagnostics and
      sensitive data)
- [ ] Name resolution by lookup. No wire name regenerated from a snake name.
      (Configuration and call grammar)
- [ ] Auto-selection rules. Fields with unsupplied required arguments are
      skipped. `__typename` is always emitted and never counted against
      `max_fields`. (Selection and deterministic data)
- [ ] No network in unit tests. Loopback only. (Compatibility and verification)
- [ ] Configuration precedence is unchanged, or the change is stated above.
      (Configuration and call grammar)

## Design authority

- [ ] `docs/reference/DESIGN_DECISIONS.md` updated in place if this PR takes a
      new decision or changes a current rule.
- [ ] `docs/reference/SPEC.md` left unedited, or the edit is a genuine change to
      the historical baseline and not a rewrite to match current decisions.
- [ ] Public API change is reflected in the docs, or `N/A`.

## Notes

_Risks, follow-up work, or open questions. Write "None." if nothing applies.
Replace this line._
