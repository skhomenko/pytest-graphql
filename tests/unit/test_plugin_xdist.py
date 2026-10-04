"""xdist (SPEC 7.6, DESIGN section 2 and section 3 "Reporting").

Each worker loads the schema once and says so. ``--gql-seed=random`` is chosen
once on the controller and handed to every worker. The run id is the one xdist
gives all workers. The sessions here start real workers with ``-n 2``, and every
worker writes what it saw to a file, because a worker is another process.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pytest_graphql.plugin import options, reporting
from pytest_graphql.plugin.reporting import (
    NODES,
    SEED_KEY,
    WORKER_KEY,
    SchemaFacts,
    WorkerReport,
    summary_lines,
)

ROOT = Path(__file__).resolve().parents[2]

CONFTEST = """
import os

import pytest
from tests.schema.fake_transport import FakeGraphQLTransport
from tests.schema.resolvers import build_schema

OUT = os.environ["M9B_OUT"]
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")


def note(line):
    # One file for each process: two workers appending to one file at the same
    # moment lose a line on Windows, where an append is not atomic.
    with open(OUT + "." + WORKER, "a") as handle:
        handle.write(line + "\\n")


class CountingSource:
    fingerprint = "counting:source"

    def load(self):
        note("load " + os.environ.get("PYTEST_XDIST_WORKER", "main"))
        return build_schema()


@pytest.fixture(scope="session")
def gql_transport():
    return FakeGraphQLTransport(build_schema())


@pytest.fixture(scope="session")
def gql_url():
    return "http://127.0.0.1:9/graphql"


@pytest.fixture(scope="session")
def gql_schema_source():
    return CountingSource()
"""

TESTS = """
from conftest import note


def test_one(gql):
    source = gql._fake_context.unique_source
    note(f"test {source.worker_id} {source.run_id} {gql.config.seed}")
"""


def run_distributed(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *args: str,
    tests: str = TESTS,
) -> tuple[pytest.RunResult, list[list[str]]]:
    out = tmp_path / "workers"
    monkeypatch.setenv("M9B_OUT", str(out))
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])),
    )
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(test_inner=tests)
    result = pytester.runpytest_subprocess("-p", "no:hypothesispytest", *args)
    lines = [
        line
        for part in sorted(tmp_path.glob("workers.*"))
        for line in part.read_text().splitlines()
    ]
    return result, [line.split() for line in lines]


def of_kind(lines: list[list[str]], kind: str) -> list[list[str]]:
    return [line[1:] for line in lines if line[0] == kind]


def header(result: pytest.RunResult) -> str:
    return "\n".join(
        line for line in result.stdout.lines if line.startswith("graphql: ")
    )


def test_every_worker_loads_the_schema_once_and_says_so(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, lines = run_distributed(
        pytester, monkeypatch, tmp_path, "-n", "2", "--dist=each", "--gql-seed=7"
    )
    result.assert_outcomes(passed=2)
    assert Counter(worker for [worker] in of_kind(lines, "load")) == {
        "gw0": 1,
        "gw1": 1,
    }
    assert (
        "graphql: schema loaded once per worker, and each worker's schema is "
        "listed at the end of the run"
    ) in header(result)
    result.stdout.fnmatch_lines(
        [
            "schema gw0: counting:source, * types, * queries, * mutations, "
            "0 subscriptions, loaded in *s",
            "schema gw1: counting:source, * types, * queries, * mutations, "
            "0 subscriptions, loaded in *s",
            "schema loaded by 2 of 2 workers, once each",
        ]
    )


def test_a_worker_that_ran_no_test_loaded_no_schema(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, lines = run_distributed(pytester, monkeypatch, tmp_path, "-n", "2")
    result.assert_outcomes(passed=1)
    assert len(of_kind(lines, "load")) == 1
    result.stdout.fnmatch_lines(["schema loaded by 1 of 2 workers, once each"])
    assert (
        len([line for line in result.stdout.lines if line.startswith("schema gw")]) == 1
    )


def test_every_worker_uses_the_run_id_of_xdist(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, lines = run_distributed(
        pytester, monkeypatch, tmp_path, "-n", "2", "--dist=each"
    )
    runs = {run for _, run, _ in of_kind(lines, "test")}
    assert len(runs) == 1
    [run] = runs
    result.stdout.fnmatch_lines([f"run id {run}, shared by 2 workers"])
    assert "run id assigned by xdist" in header(result)


def test_a_given_run_id_is_the_run_id_of_every_worker_and_the_header(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, lines = run_distributed(
        pytester,
        monkeypatch,
        tmp_path,
        "-n",
        "2",
        "--dist=each",
        "--testrunuid=fixedrun123",
    )
    assert {run for _, run, _ in of_kind(lines, "test")} == {"fixedrun123"}
    assert "run id fixedrun123" in header(result)
    result.stdout.fnmatch_lines(["run id fixedrun123, shared by 2 workers"])


def test_a_random_seed_is_chosen_once_and_every_worker_uses_it(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, lines = run_distributed(
        pytester, monkeypatch, tmp_path, "-n", "2", "--dist=each", "--gql-seed=random"
    )
    result.assert_outcomes(passed=2)
    seeds = {seed for _, _, seed in of_kind(lines, "test")}
    assert len(seeds) == 1
    [seed] = seeds
    found = re.search(r"seed (\d+) \(chosen by random\)", header(result))
    assert found is not None
    assert found[1] == seed
    result.stdout.fnmatch_lines([f"seed {seed}, the same on 2 workers"])


def test_a_literal_seed_reaches_every_worker(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, lines = run_distributed(
        pytester, monkeypatch, tmp_path, "-n", "2", "--dist=each", "--gql-seed=5"
    )
    assert {seed for _, _, seed in of_kind(lines, "test")} == {"5"}


def test_two_sessions_choose_different_random_seeds(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seeds = []
    for index in range(2):
        folder = tmp_path / str(index)
        folder.mkdir()
        _, lines = run_distributed(
            pytester, monkeypatch, folder, "-n", "1", "--gql-seed=random"
        )
        seeds.append(of_kind(lines, "test")[0][2])
    assert seeds[0] != seeds[1]


# -- the controller's side, without workers -------------------------------------


def node_of(config: Any) -> Any:
    return SimpleNamespace(config=config, workerinput={}, workeroutput={})


def test_the_controller_hands_a_random_seed_to_each_worker(
    pytester: pytest.Pytester,
) -> None:
    config = pytester.parseconfig("--gql-seed=random")
    node = node_of(config)
    reporting.pytest_configure_node(node)
    chosen = options.settings_of(config).cli_values["seed"]
    assert node.workerinput == {SEED_KEY: chosen}


@pytest.mark.parametrize("flag", ["--gql-seed=12", "--gql-timeout=5"])
def test_the_controller_hands_nothing_over_for_a_literal_seed(
    pytester: pytest.Pytester, flag: str
) -> None:
    node = node_of(pytester.parseconfig(flag))
    reporting.pytest_configure_node(node)
    assert node.workerinput == {}


def test_a_worker_takes_the_seed_the_controller_chose(
    pytester: pytest.Pytester,
) -> None:
    config = pytester.parseconfig("--gql-seed=random")
    config.workerinput = {SEED_KEY: 31337}  # type: ignore[attr-defined]
    settings = options.resolve(config, {})
    assert settings.cli_values["seed"] == 31337
    assert settings.seed_is_random


def test_a_worker_without_a_shared_seed_chooses_its_own(
    pytester: pytest.Pytester,
) -> None:
    config = pytester.parseconfig("--gql-seed=random")
    config.workerinput = {"workerid": "gw0"}  # type: ignore[attr-defined]
    settings = options.resolve(config, {})
    assert settings.seed_is_random
    assert 0 <= settings.cli_values["seed"] < 2**32


@pytest.mark.parametrize("bad", [True, "5", 5.5, None])
def test_a_malformed_shared_seed_is_ignored(
    pytester: pytest.Pytester, bad: object
) -> None:
    config = pytester.parseconfig("--gql-seed=random")
    config.workerinput = {SEED_KEY: bad}  # type: ignore[attr-defined]
    assert options.resolve(config, {}).seed_is_random


def test_a_worker_files_its_report_in_the_output_xdist_sends_back(
    pytester: pytest.Pytester,
) -> None:
    config = pytester.parseconfig("--gql-seed=3")
    config.workeroutput = {}  # type: ignore[attr-defined]
    config.workerinput = {"workerid": "gw4", "testrunuid": "abc"}  # type: ignore[attr-defined]
    reporting.pytest_sessionfinish(SimpleNamespace(config=config))  # type: ignore[arg-type]
    report = WorkerReport.from_dict(config.workeroutput[WORKER_KEY])  # type: ignore[attr-defined]
    assert report == WorkerReport(worker="gw4", run_id="abc", seed=3, schema=None)


def test_a_process_that_is_not_a_worker_files_nothing(
    pytester: pytest.Pytester,
) -> None:
    config = pytester.parseconfig()
    reporting.pytest_sessionfinish(SimpleNamespace(config=config))  # type: ignore[arg-type]
    assert not hasattr(config, "workeroutput")


def facts(loads: int = 1) -> SchemaFacts:
    return SchemaFacts(
        fingerprint="label",
        types=3,
        objects=1,
        interfaces=0,
        unions=0,
        enums=0,
        inputs=0,
        scalars=2,
        fields=1,
        queries=1,
        mutations=0,
        subscriptions=0,
        load_seconds=0.5,
        loads=loads,
    )


def controller_lines(
    pytester: pytest.Pytester, reports: list[WorkerReport], *flags: str
) -> list[str]:
    config = pytester.parseconfig(*flags)
    config.stash[NODES] = reports
    return summary_lines(config)


def test_the_controller_collects_each_workers_report(
    pytester: pytest.Pytester,
) -> None:
    config = pytester.parseconfig()
    report = WorkerReport("gw0", "run", 1, facts())
    node = node_of(config)
    node.workeroutput = {WORKER_KEY: report.as_dict()}
    reporting.pytest_testnodedown(node, None)
    assert config.stash[NODES] == [report]


@pytest.mark.parametrize(
    "output",
    [
        {},
        {WORKER_KEY: None},
        {WORKER_KEY: {"worker": "gw0"}},
        {WORKER_KEY: {"worker": "gw0", "run_id": "r", "seed": True}},
        {WORKER_KEY: {"worker": 1, "run_id": "r", "seed": 1}},
    ],
)
def test_a_report_that_is_not_well_formed_is_dropped(
    pytester: pytest.Pytester, output: object
) -> None:
    config = pytester.parseconfig()
    node = node_of(config)
    node.workeroutput = output
    reporting.pytest_testnodedown(node, None)
    assert NODES not in config.stash


def test_a_worker_with_no_output_at_all_is_dropped(pytester: pytest.Pytester) -> None:
    config = pytester.parseconfig()
    reporting.pytest_testnodedown(SimpleNamespace(config=config), None)
    assert NODES not in config.stash


def test_the_summary_lists_the_workers_in_name_order(
    pytester: pytest.Pytester,
) -> None:
    lines = controller_lines(
        pytester,
        [
            WorkerReport("gw1", "run", 2, facts()),
            WorkerReport("gw0", "run", 2, facts()),
        ],
    )
    assert [line.split(":")[0] for line in lines if line.startswith("schema gw")] == [
        "schema gw0",
        "schema gw1",
    ]
    assert lines[-3:] == [
        "schema loaded by 2 of 2 workers, once each",
        "run id run, shared by 2 workers",
        "seed 2, the same on 2 workers",
    ]


def test_the_summary_flags_a_worker_that_loaded_more_than_once(
    pytester: pytest.Pytester,
) -> None:
    lines = controller_lines(pytester, [WorkerReport("gw0", "run", 2, facts(loads=2))])
    assert "schema loaded by 1 of 1 worker, more than once on a worker" in lines


def test_the_summary_flags_workers_that_disagree_on_the_run_or_the_seed(
    pytester: pytest.Pytester,
) -> None:
    lines = controller_lines(
        pytester,
        [WorkerReport("gw0", "a", 1, facts()), WorkerReport("gw1", "b", 2, facts())],
    )
    assert "run ids differ between workers" in lines
    assert "seeds differ between workers" in lines


def test_a_worker_that_loaded_no_schema_is_counted_but_not_listed(
    pytester: pytest.Pytester,
) -> None:
    lines = controller_lines(
        pytester,
        [WorkerReport("gw0", "run", 1, facts()), WorkerReport("gw1", "run", 1, None)],
    )
    assert "schema loaded by 1 of 2 workers, once each" in lines
    assert not any(line.startswith("schema gw1") for line in lines)


def test_the_stats_block_is_listed_for_each_worker_that_loaded(
    pytester: pytest.Pytester,
) -> None:
    lines = controller_lines(
        pytester,
        [
            WorkerReport("gw0", "run", 1, facts()),
            WorkerReport("gw1", "run", 1, facts()),
        ],
        "--gql-show-schema-stats",
    )
    assert "schema stats gw0:" in lines
    assert "schema stats gw1:" in lines


def test_workers_that_loaded_no_schema_print_no_section(
    pytester: pytest.Pytester,
) -> None:
    lines = controller_lines(
        pytester,
        [WorkerReport("gw0", "run", 1, None), WorkerReport("gw1", "run", 1, None)],
    )
    assert lines == []
