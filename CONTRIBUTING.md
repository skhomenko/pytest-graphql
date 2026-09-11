# Contributing

## Development setup

```bash
git clone git@github.com:skhomenko/pytest-graphql.git
cd pytest-graphql
uv sync --all-extras
git config core.hooksPath .githooks
```

The hooks are tracked in `.githooks/` and do nothing until that last command
runs. A linked worktree needs the same setting of its own.

## Checks

Run all of these before you open a pull request. CI runs the same set.

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src/
uv run pytest -q
python3 scripts/check_core_purity.py
```

`scripts/` holds contributor tooling that runs on the standard library alone, so
those scripts work before the project environment exists. Each one has a
`--self-test` mode, and the test suite runs it.

Text intended for publication is scanned separately:

```bash
python3 scripts/check_publication_hygiene.py README.md CHANGELOG.md
```

## Layout rules

- Library code lives in `src/pytest_graphql/`.
- The pytest layer is confined to `src/pytest_graphql/plugin/`. No module
  outside that package may import pytest. `scripts/check_core_purity.py`
  enforces this, and CI fails when it reports a finding.
- Tests live in `tests/`. A unit test never opens a non-loopback socket.

## Branches and commits

Branch names use a type prefix and a short hyphenated description: `feat/`,
`fix/`, `docs/`, `chore/`, `refactor/`, `test/` or `security/`.

Commit subjects use conventional-commits prefixes and stay at or under 72
characters. Body lines wrap at 72. The commit-message hook enforces both, and
`docs/reference/COMMIT_MESSAGE_RULES.md` states exactly how a line is measured.
Never bypass a hook.

## Supported versions

The supported set is Python 3.10 through 3.14 with pytest 7.4 or newer. That
range is the compatibility promise and it is what the dependency metadata
declares.

CI does not run the whole cross product. It runs one job per row of the
representative matrix in the compatibility section of
`docs/reference/DESIGN_DECISIONS.md`. Those rows are a sample of the supported
range, never a narrower supported set. Adding or removing a row is one change
that edits that table and `.github/workflows/test.yml` together.

Linux, macOS and Windows are supported. The matrix rows run on Linux. Two more
jobs run the test suite on macOS and on Windows at the newest supported Python.
The same section states that rule.

`requires-python` declares both ends of the range, `>=3.10,<3.15`. Raising the
ceiling means editing that section, `pyproject.toml` and the matrix together,
then running `uv lock`.

## Pinned build tooling

Two programs run during a release build, and both are pinned so the reviewed
tree selects them.

- The uv version lives in the `UV_VERSION` variable at the top of each workflow.
  Dependabot moves action SHAs but not this value, so bump it by commit.
- `requirements/build.txt` pins the build backend and its own dependencies.
  `requirements/build.in` holds the direct requirement. Regenerate the pinned
  file with:

```
uv pip compile --universal --python-version 3.10 requirements/build.in \
    -o requirements/build.txt
```

The release build passes that file to `uv build --build-constraints`, and the
sdist install-back job installs it before building with isolation disabled.

## Release checklist

Publication happens at three gates. The release machinery exists from the first
milestone and publishes nothing before the alpha gate.

| Gate | Version | Requires |
|---|---|---|
| Alpha | `0.1.0a1` | Working client, transport, response model and one fixture |
| Beta | `0.1.0b1` | Complete pytest plugin |
| Stable | `0.1.0` | Complete documentation and release acceptance |

Every gate runs the same steps. Steps 1 to 6 are the contributor's. Steps 7 and
8 are the maintainer's, and no agent performs them.

1. Confirm the branch is green.

   ```bash
   uv run ruff check . && uv run ruff format --check . \
     && uv run mypy --strict src/ && uv run pytest -q
   ```

2. Set the version in one place and check it.

   ```bash
   $EDITOR src/pytest_graphql/__init__.py
   uv run python -c "import pytest_graphql; print(pytest_graphql.__version__)"
   ```

3. Move the `Unreleased` entries into a dated section for that version in
   `CHANGELOG.md`, and scan the text that will be published.

   ```bash
   python3 scripts/check_publication_hygiene.py README.md CHANGELOG.md
   ```

4. Merge the release branch, then tag the merge commit. The tag is `v` followed
   by the exact version. The release workflow refuses a tag that disagrees with
   `__version__`.

   ```bash
   git tag "v$(uv run python -c 'import pytest_graphql; print(pytest_graphql.__version__)')"
   git push origin --tags
   ```

5. Watch the `release` workflow. It builds one artifact set, records a digest
   for every file, and runs the artifact checks: `twine check`, PEP 621
   metadata completeness, long-description rendering, wheel and sdist contents,
   and `__version__` agreeing with the tag.

6. Before the first gate the workflow stops there, leaving the artifact set
   built, checked and retained. The upload jobs are gated on the repository
   variable `RELEASE_PUBLISH`, which is unset until the maintainer opens the
   alpha gate. From that point the run continues into the upload jobs, which
   wait for review in the protected `pypi` environment.

7. Approve the TestPyPI upload. The workflow then enumerates that release
   through the TestPyPI index, compares the complete remote file set with the
   build manifest in both directions, fetches each file by its index URL,
   rechecks every digest, and installs and tests the wheel and the sdist in
   separate clean environments.

8. Approve the PyPI upload. It uploads the same files that TestPyPI verified.
   Nothing is rebuilt between the two indexes, because a rebuild is a different
   artifact.

Two properties of the indexes shape this checklist. A version can be uploaded
once per index and cannot be replaced, only yanked, so a failed check burns that
version number and the next attempt increments it. Before `0.1.0` exists, no
stable version satisfies `pytest-graphql`, so an ordinary unpinned install can
select `0.1.0a1`. Publishing the alpha to PyPI therefore exposes it to ordinary
installs, and the maintainer accepts that exposure at the gate. The alternative
is to keep the alpha on TestPyPI until a stable version exists.

Trusted publishing is configured as a pending publisher on PyPI and on TestPyPI,
so no upload token is stored. The token exchange is first exercised at the alpha
gate, because a pending publisher cannot be tested without a real upload.

### Repository configuration

These settings live in GitHub and on the two indexes, not in the repository, so
the maintainer applies them once. The release workflow assumes all of them.

| Where | Setting |
|---|---|
| PyPI | A pending publisher for `pytest-graphql`, owner `skhomenko`, repository `pytest-graphql`, workflow `release.yml`, environment `pypi` |
| TestPyPI | The same pending publisher, with the same values |
| GitHub environment `pypi` | Protected, with a required reviewer. Both upload jobs run in it |
| GitHub variable `RELEASE_PUBLISH` | Unset until the alpha gate. Set it to `true` to let the upload jobs run |

No upload token is stored anywhere. The install-back job runs outside the
environment and holds no publishing credential of any kind.
