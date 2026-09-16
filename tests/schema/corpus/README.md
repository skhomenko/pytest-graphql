# Calibration corpus

Project-authored synthetic GraphQL schemas, checked in as plain SDL under `sdl/`, so the
calibration gate and any later regression check are reproducible without a live endpoint of
any kind, real or synthetic. The domain-specific names and descriptions in these fixtures
are project-authored, and none of them is copied from, or models, any third-party API's
domain content. The pagination field and type names (`edges`, `node`, `cursor`, `pageInfo`,
`hasNextPage`, `endCursor`, and the `*Connection`/`*Edge` suffixes) intentionally conform
instead to the GraphQL Cursor Connections Specification: `facebook/relay`,
`website/spec/Connections.md` at commit `f9a7c64558c00221aa6baf6d79deb50731f74519`,
retrieved 2026-09-15, MIT License, Meta Platforms, Inc. and affiliates. No prose or example
text from that specification is copied; only its functional naming convention is reused.
`manifest.json` records, for each fixture, when it was authored and which calibration case
it exists to cover.

| File | Fixture | Covers |
|---|---|---|
| `sdl/catalog.graphql` | Product catalog | wide union, interface, self-reference, a confirmed nested-connection depth cap, a declared page-size default, a required-argument field |
| `sdl/feed.graphql` | Social feed | a non-`first` page argument, unconfirmed nested connections, both connection-heuristic mismatches, a root type with nothing selectable |

Run the measurement harness for the human-readable report:

```bash
uv run python tests/schema/corpus/measure.py
```

`tests/unit/test_corpus_calibration.py` pins the numbers this report produces, so a change to
either fixture or to the selection engine that moves one of them fails CI, not just this
script's output.
