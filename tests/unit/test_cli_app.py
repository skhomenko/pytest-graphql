"""``pytest-graphql --app`` and ``PYTEST_GQL_APP`` (DESIGN 13.7).

The applications are the project-authored ASGI and WSGI applications of the
in-process tests, built from ``graphql-core`` and the standard library. A local
module, written into a temporary directory, stands for the user's own code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, MutableMapping
from pathlib import Path
from typing import Any

import pytest

from pytest_graphql._cli import main
from tests.unit.in_process_apps import SDL, AsgiApp, asgi_function, wsgi_app

HEADER_SECRET = "hdr-secret-4d2a91"
IMPORT_SECRET = "import-secret-7f03b6"
ASGI = "tests.unit.test_cli_app:asgi_app"
WSGI = "tests.unit.in_process_apps:wsgi_app"
THIS = "tests.unit.test_cli_app"

asgi_app = AsgiApp()
failing_lifespan = AsgiApp(lifespan="failed")
not_an_app = 42


def guarded_wsgi(environ: dict[str, Any], start_response: Callable[..., Any]) -> Any:
    """The WSGI application, behind a header it requires."""
    if environ.get("HTTP_X_TOKEN") != HEADER_SECRET:
        start_response("401 Unauthorized", [("Content-Type", "text/plain")])
        return [b"denied"]
    return wsgi_app(environ, start_response)


async def guarded_asgi(
    scope: MutableMapping[str, Any], receive: Any, send: Any
) -> None:
    """The ASGI application, behind a header it requires."""
    wanted = (b"x-token", HEADER_SECRET.encode())
    if scope["type"] == "http" and wanted not in scope["headers"]:
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b"denied"})
        return
    await asgi_function(scope, receive, send)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PYTEST_GQL_URL", "PYTEST_GQL_HEADERS", "PYTEST_GQL_APP"):
        monkeypatch.delenv(name, raising=False)


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    status = main(list(argv))
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def operation_names(out: str) -> list[str]:
    return [line.split()[1] for line in out.splitlines()]


@pytest.fixture
def local_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str, str], None]]:
    """Write a module into a temporary current directory; forget it afterwards."""
    monkeypatch.chdir(tmp_path)
    written: list[str] = []

    def write(name: str, source: str) -> None:
        (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
        written.append(name)

    yield write
    for name in written:
        sys.modules.pop(name, None)


# -- the sources ---------------------------------------------------------------


@pytest.mark.parametrize("path", [ASGI, WSGI])
def test_app_flag_loads_the_schema_in_process(
    capsys: pytest.CaptureFixture[str], path: str
) -> None:
    status, out, err = run(capsys, "operations", "--app", path)
    assert (status, err) == (0, "")
    assert operation_names(out) == [
        "hello",
        "slow",
        "block",
        "whoami",
        "greeting",
        "login",
        "upload",
    ]


def test_app_describes_an_operation(capsys: pytest.CaptureFixture[str]) -> None:
    status, out, err = run(capsys, "describe", "login", "--app", WSGI)
    assert (status, err) == (0, "")
    assert "mutation login" in out
    assert WSGI not in out


def test_env_app_is_a_source(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_GQL_APP", WSGI)
    status, out, err = run(capsys, "operations")
    assert (status, err) == (0, "")
    assert "hello" in out


def test_flag_beats_the_environment_app(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_GQL_APP", "no_such_module_here:app")
    status, _, err = run(capsys, "operations", "--app", WSGI)
    assert (status, err) == (0, "")


def test_app_flag_beats_the_url(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_GQL_URL", "http://127.0.0.1:9/graphql")
    status, out, err = run(capsys, "operations", "--app", WSGI)
    assert (status, err) == (0, "")
    assert "hello" in out


def test_url_beats_the_environment_app(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_GQL_URL", "http://127.0.0.1:9/graphql")
    monkeypatch.setenv("PYTEST_GQL_APP", WSGI)
    status, out, err = run(capsys, "operations")
    # The endpoint is tried and refuses the connection, so the app was not used.
    assert (status, out) == (3, "")
    assert "endpoint in PYTEST_GQL_URL" in err


def test_schema_and_app_together_are_a_usage_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    sdl = tmp_path / "s.graphql"
    sdl.write_text("type Query { a: Int }", encoding="utf-8")
    status, out, err = run(capsys, "operations", "--schema", str(sdl), "--app", WSGI)
    assert (status, out) == (2, "")
    assert "--schema" in err
    assert "--app" in err


def test_the_missing_source_message_names_every_source(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status, out, err = run(capsys, "operations")
    assert (status, out) == (2, "")
    for name in ("--schema", "--app", "PYTEST_GQL_URL", "PYTEST_GQL_APP"):
        assert name in err


def test_help_describes_the_flag_and_the_variable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["operations", "--help"]) == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "--app MODULE:ATTRIBUTE" in text
    assert "current directory is searched last" in text
    assert "PYTEST_GQL_APP" in text


def test_json_output_is_the_same_for_every_source(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    sdl = tmp_path / "s.graphql"
    sdl.write_text(SDL, encoding="utf-8")
    _, from_file, _ = run(capsys, "describe", "hello", "--json", "--schema", str(sdl))
    _, from_app, _ = run(capsys, "describe", "hello", "--json", "--app", WSGI)
    assert json.loads(from_file) == json.loads(from_app)
    assert "tests.unit" not in from_app


# -- usage errors and load failures --------------------------------------------


@pytest.mark.parametrize(
    "value", ["secretNoColon", ":secretattr", "secretmod:", "secret b:c", "secret:b:c"]
)
def test_a_malformed_app_value_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], value: str
) -> None:
    status, out, err = run(capsys, "operations", "--app", value)
    assert (status, out) == (2, "")
    assert "package.module:attribute" in err
    assert "secret" not in err


def test_a_malformed_environment_app_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_GQL_APP", "bad-value-no-colon")
    status, out, err = run(capsys, "operations")
    assert (status, out) == (2, "")
    assert "PYTEST_GQL_APP" in err
    assert "bad-value-no-colon" not in err


@pytest.mark.parametrize(
    ("path", "words"),
    [
        ("no_such_module_xyz:app", "no module named"),
        ("tests.unit.in_process_apps:nothing", "no attribute 'nothing'"),
        (f"{THIS}:not_an_app", "not callable"),
        (f"{THIS}:failing_lifespan", "RuntimeError"),
    ],
)
def test_load_failures_exit_3(
    capsys: pytest.CaptureFixture[str], path: str, words: str
) -> None:
    status, out, err = run(capsys, "operations", "--app", path)
    assert (status, out) == (3, "")
    assert words in err
    assert path in err


def test_a_failing_import_shows_only_the_class(
    capsys: pytest.CaptureFixture[str],
    local_modules: Callable[[str, str], None],
) -> None:
    local_modules(
        "leaky_app_module", f"raise RuntimeError('https://u:{IMPORT_SECRET}@h/x')\n"
    )
    status, out, err = run(capsys, "operations", "--app", "leaky_app_module:app")
    assert (status, out) == (3, "")
    assert "RuntimeError" in err
    assert IMPORT_SECRET not in err


# -- headers -------------------------------------------------------------------


@pytest.mark.parametrize("app", ["guarded_wsgi", "guarded_asgi"])
def test_headers_reach_the_application(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, app: str
) -> None:
    path = f"{THIS}:{app}"
    assert run(capsys, "operations", "--app", path)[0] == 3
    monkeypatch.setenv("PYTEST_GQL_HEADERS", f"X-Token: {HEADER_SECRET}")
    status, out, err = run(capsys, "operations", "--app", path)
    assert (status, err) == (0, "")
    assert HEADER_SECRET not in out


def test_a_header_value_is_in_no_message(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_GQL_HEADERS", f"X-Token: {HEADER_SECRET}-wrong")
    status, out, err = run(capsys, "operations", "--app", f"{THIS}:guarded_wsgi")
    assert status == 3
    assert HEADER_SECRET not in out + err


# -- threads -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "status"),
    [(ASGI, 0), (f"{THIS}:failing_lifespan", 3), (f"{THIS}:guarded_asgi", 3)],
)
def test_no_thread_survives_the_command(
    capsys: pytest.CaptureFixture[str], path: str, status: int
) -> None:
    before = set(threading.enumerate())
    assert run(capsys, "operations", "--app", path)[0] == status
    assert set(threading.enumerate()) <= before


# -- the current directory -----------------------------------------------------

LOCAL_APP = "from tests.unit.in_process_apps import wsgi_app as app\n"


def test_a_local_app_works_and_sys_path_is_restored(
    capsys: pytest.CaptureFixture[str],
    local_modules: Callable[[str, str], None],
    tmp_path: Path,
) -> None:
    local_modules("app", LOCAL_APP)
    before = list(sys.path)
    status, out, err = run(capsys, "operations", "--app", "app:app")
    assert (status, err) == (0, "")
    assert "hello" in out
    assert sys.path == before
    assert str(tmp_path) not in sys.path


def test_sys_path_is_restored_after_a_failure(
    capsys: pytest.CaptureFixture[str],
    local_modules: Callable[[str, str], None],
) -> None:
    local_modules("broken_app_module", "raise RuntimeError('boom')\n")
    before = list(sys.path)
    status, _, _ = run(capsys, "operations", "--app", "broken_app_module:app")
    assert status == 3
    assert sys.path == before


def test_the_current_directory_is_searched_last(
    capsys: pytest.CaptureFixture[str],
    local_modules: Callable[[str, str], None],
) -> None:
    # A local file named like an installed package loses to the package.
    local_modules("graphql", "app = None\n")
    status, _, err = run(capsys, "operations", "--app", "graphql:app")
    assert status == 3
    assert "has no attribute 'app'" in err


@pytest.mark.parametrize("alias", ["path", "empty"])
def test_an_existing_current_directory_entry_moves_behind_the_others(
    capsys: pytest.CaptureFixture[str],
    local_modules: Callable[[str, str], None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    alias: str,
) -> None:
    local_modules("present_app", LOCAL_APP + "import sys\nSEEN = list(sys.path)\n")
    monkeypatch.setattr(
        sys, "path", [str(tmp_path) if alias == "path" else "", *sys.path]
    )
    before = list(sys.path)
    status, _, _ = run(capsys, "operations", "--app", "present_app:app")
    assert status == 0
    seen = sys.modules["present_app"].SEEN
    assert seen[-1] == str(tmp_path.resolve())
    assert len([e for e in seen if Path(e or ".").resolve() == tmp_path.resolve()]) == 1
    assert sys.path == before


# -- a fresh process -----------------------------------------------------------

LAUNCHERS = {
    # ``python -m`` puts the current directory first on the path.
    "module": ["-m", "pytest_graphql"],
    # A console script puts its own directory there, and not the current one.
    "script": None,
    # ``python -c`` puts ``""`` first.
    "alias": [
        "-c",
        "import sys\nfrom pytest_graphql._cli import main\n"
        "sys.exit(main(sys.argv[1:]))",
    ],
}


def fresh(
    launcher: str, directory: Path, *argv: str, environ: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """The command in a new interpreter, started in ``directory``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_GQL_")}
    env.update(environ or {})
    args = LAUNCHERS[launcher]
    if args is None:
        script = directory.parent / f"{directory.name}_console.py"
        script.write_text(
            "import sys\nfrom pytest_graphql._cli import main\n"
            "sys.exit(main(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        args = [str(script)]
    return subprocess.run(
        [sys.executable, *args, *argv],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@pytest.mark.parametrize("launcher", sorted(LAUNCHERS))
def test_an_installed_module_beats_a_local_namesake_in_a_fresh_process(
    tmp_path: Path, launcher: str
) -> None:
    # ``colorsys`` is installed with Python and no module of the command imports it,
    # so only the order of the path decides which one is found.
    (tmp_path / "colorsys.py").write_text("raise RuntimeError('local')\n")
    done = fresh(launcher, tmp_path, "operations", "--app", "colorsys:rgb_to_hsv")
    assert done.returncode == 3
    assert "RuntimeError" not in done.stderr
    assert "TypeError" in done.stderr


@pytest.mark.parametrize("launcher", sorted(LAUNCHERS))
def test_a_local_app_loads_in_a_fresh_process(tmp_path: Path, launcher: str) -> None:
    (tmp_path / "localapp.py").write_text(
        "from graphql import build_schema\n"
        "def app(environ, start_response):\n"
        "    raise RuntimeError('unused')\n",
        encoding="utf-8",
    )
    done = fresh(launcher, tmp_path, "operations", "--app", "localapp:app")
    # The module was found. The application then fails, so the error is not
    # about a missing module.
    assert done.returncode == 3
    assert "no module named" not in done.stderr
    assert "RuntimeError" in done.stderr


# -- an exit raised by the application -----------------------------------------

EXITING_APPS = """
def wsgi(environ, start_response):
    raise SystemExit(environ["HTTP_X_TOKEN"])


async def asgi(scope, receive, send):
    if scope["type"] == "lifespan":
        raise RuntimeError("no lifespan")
    token = dict(scope["headers"])[b"x-token"].decode()
    raise SystemExit(token)
"""


@pytest.mark.parametrize("app", ["wsgi", "asgi"])
def test_an_exit_in_the_application_shows_only_its_class(
    tmp_path: Path, app: str
) -> None:
    (tmp_path / "exiting.py").write_text(EXITING_APPS, encoding="utf-8")
    done = fresh(
        "module",
        tmp_path,
        "operations",
        "--app",
        f"exiting:{app}",
        environ={"PYTEST_GQL_HEADERS": f"X-Token: {HEADER_SECRET}"},
    )
    assert done.returncode == 3
    assert "could not load the schema from the application" in done.stderr
    assert HEADER_SECRET not in done.stdout + done.stderr


def test_an_exit_during_import_shows_only_its_class(
    capsys: pytest.CaptureFixture[str],
    local_modules: Callable[[str, str], None],
) -> None:
    local_modules("exiting_import", f"raise SystemExit('{IMPORT_SECRET}')\n")
    status, out, err = run(capsys, "operations", "--app", "exiting_import:app")
    assert (status, out) == (3, "")
    assert "SystemExit" in err
    assert IMPORT_SECRET not in err
