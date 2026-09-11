"""The core purity check runs against the real tree and against a violation.

The last test in this file is a different kind of check. The script models how
Python resolves a name, and a model is only worth its agreement with the
interpreter. So a corpus of small programs is run for real, and every program
that truly imports pytest must be reported. The script's own ``--self-test``
says what each decision should be; this says what Python actually does.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
PACKAGE_ROOT = REPO_ROOT / "src" / "pytest_graphql"


def _load_script(name: str) -> ModuleType:
    """Import a ``scripts/`` module by path, without adding it to the path."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


purity = _load_script("check_core_purity")


def test_package_has_no_pytest_import_outside_the_plugin() -> None:
    findings = purity.scan_tree(PACKAGE_ROOT)
    assert findings == [], [f.render(PACKAGE_ROOT.parent) for f in findings]


def test_the_check_reports_a_deliberate_violation(tmp_path: Path) -> None:
    """A pytest import added under ``_core/`` on purpose fails the check."""
    core = tmp_path / "_core"
    core.mkdir()
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (core / "__init__.py").write_text("", encoding="utf-8")
    (core / "client.py").write_text("import pytest\n", encoding="utf-8")

    findings = purity.scan_tree(tmp_path)

    assert len(findings) == 1
    assert findings[0].path.name == "client.py"
    assert "imports pytest" in findings[0].detail


def test_the_plugin_package_may_import_pytest(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (plugin / "__init__.py").write_text("import pytest\n", encoding="utf-8")

    assert purity.scan_tree(tmp_path) == []


# Run in a child process, one program after another, reporting for each whether
# pytest reached ``sys.modules``.
RUNNER = """
import json, sys

results = []
for source in json.loads(sys.stdin.read()):
    for name in ("pytest", "_pytest"):
        sys.modules.pop(name, None)
    try:
        exec(compile(source, "<program>", "exec"), {"__name__": "program"})
    except BaseException:
        pass
    results.append("pytest" in sys.modules or "_pytest" in sys.modules)
print(json.dumps(results))
"""

# A harmless callable to shadow the importer with, and the importer beside it.
SAFE = "def safe(*args, **rest):\n    return 0\n\n\n"
HEAD = "from importlib import import_module as load\n\n\n" + SAFE
BOX = (
    "class Box:\n    def __getitem__(self, key):\n        return 0\n\n"
    "    def __setitem__(self, key, value):\n        pass\n\n\nbox = Box()\n"
)

# Programs that cover one resolution rule each, in both directions where the
# rule has two. A program must finish on its own and import nothing but pytest.
MODEL_PROGRAMS: tuple[tuple[str, str], ...] = (
    ("a plain dynamic import", HEAD + 'load("pytest")\n'),
    ("a rebinding above the call", HEAD + 'load = safe\nload("pytest")\n'),
    (
        "a function reaching an outer alias",
        HEAD + 'def run():\n    return load("pytest")\n\n\nrun()\n',
    ),
    (
        "a local name shadowing the alias",
        HEAD + 'def run():\n    load = safe\n    return load("pytest")\n\n\nrun()\n',
    ),
    (
        "a parameter shadowing the alias",
        HEAD + 'def run(load):\n    return load("pytest")\n\n\nrun(safe)\n',
    ),
    (
        "a class body reaching the module",
        HEAD + 'class Thing:\n    value = load("pytest")\n',
    ),
    (
        "a class attribute shadowing the alias",
        HEAD + 'class Thing:\n    load = safe\n    value = load("pytest")\n',
    ),
    (
        "a method body skipping the class namespace",
        HEAD + "class Thing:\n    load = safe\n\n"
        '    def run(self):\n        return load("pytest")\n\n\nThing().run()\n',
    ),
    (
        "a comprehension target shadowing the alias",
        HEAD + 'ROWS = [load("pytest") for load in [safe]]\n',
    ),
    (
        "a closure reaching the function around it",
        HEAD + "def outer():\n    load = safe\n\n"
        '    def inner():\n        return load("pytest")\n\n    return inner\n\n\n'
        "outer()()\n",
    ),
    (
        "a global write beside the call",
        HEAD + "def run():\n    global load\n\n    load = safe\n\n"
        '    return load("pytest")\n\n\nrun()\n',
    ),
    (
        "a global write beside the call, the other way round",
        "def safe(*args, **rest):\n    return 0\n\n\nload = safe\n\n\n"
        "def run():\n    global load\n\n"
        "    from importlib import import_module as load\n\n"
        '    return load("pytest")\n\n\nrun()\n',
    ),
    (
        "a nonlocal write beside the call",
        HEAD + "def outer():\n    load = safe\n\n"
        "    def inner():\n        nonlocal load\n\n"
        "        from importlib import import_module as load\n\n"
        '        return load("pytest")\n\n    return inner\n\n\nouter()()\n',
    ),
    (
        "a nonlocal passing over a function that does not bind the name",
        HEAD + "def outer():\n    from importlib import import_module as load\n\n"
        "    def middle():\n        def inner():\n            nonlocal load\n\n"
        '            load = load("pytest")\n\n        return inner\n\n'
        "    return middle\n\n\nouter()()()\n",
    ),
    (
        "an augmented assignment reading its target first",
        HEAD + BOX + 'box[load("pytest")] += (load := safe)\n',
    ),
    (
        "an augmented assignment storing its target last",
        HEAD + BOX + 'box[(load := safe)] += load("pytest")\n',
    ),
    (
        "a dictionary key running with its value",
        HEAD + 'ROWS = {0: load("pytest"), (load := safe): 1}\n',
    ),
    (
        "a dictionary key below a value",
        HEAD + 'ROWS = {0: (load := safe), load("pytest"): 1}\n',
    ),
    (
        "a deletion in a class body",
        HEAD + "class Thing:\n    load = safe\n    del load\n\n"
        '    value = load("pytest")\n',
    ),
    (
        "an except target cleared after its handler",
        HEAD + "class Thing:\n    try:\n        raise RuntimeError\n"
        "    except RuntimeError as load:\n        pass\n\n"
        '    value = load("pytest")\n',
    ),
    (
        "an except target used inside its handler",
        HEAD + "try:\n    raise RuntimeError\nexcept RuntimeError as load:\n"
        '    load("pytest")\n',
    ),
    (
        "a deletion in a function",
        HEAD + "def run():\n    load = safe\n    del load\n\n"
        '    return load("pytest")\n\n\n'
        "try:\n    run()\nexcept UnboundLocalError:\n    pass\n",
    ),
    (
        "a walrus leaving the comprehension it stands in",
        HEAD + 'ROWS = [(load := safe) for value in range(1)]\nload("pytest")\n',
    ),
    (
        "a walrus in a generator expression, which never ran",
        HEAD + 'ROWS = ((load := safe) for value in range(1))\nload("pytest")\n',
    ),
    ("the builtin importer", '__import__("pytest")\n'),
    (
        "a shadowed builtin importer",
        '__import__ = lambda *args, **rest: 0\n__import__("pytest")\n',
    ),
    (
        "a deletion giving the builtin importer back",
        '__import__ = lambda *args, **rest: 0\ndel __import__\n__import__("pytest")\n',
    ),
    (
        "a decorator running outside the body it decorates",
        HEAD + '@load("pytest")\ndef run():\n    load = safe\n\n    return load\n',
    ),
    (
        "a default running before the annotation beside it",
        HEAD + 'def run(first=(load := safe), *, second: load("pytest") = None):\n'
        "    return first, second\n",
    ),
    (
        "an annotated assignment storing before it annotates",
        HEAD + 'load: load("pytest") = safe\n',
    ),
    (
        "a class name bound only once its body has run",
        HEAD + 'class load:\n    pass\n\n\nload("pytest")\n',
    ),
    (
        "a helper called between a global write and the call",
        SAFE + "def helper():\n    global load\n\n"
        "    from importlib import import_module as load\n\n\n"
        "load = safe\n\n\n"
        "def run():\n    global load\n\n    load = safe\n    helper()\n\n"
        '    return load("pytest")\n\n\nrun()\n',
    ),
    (
        "a helper called between a nonlocal write and the call",
        SAFE + "def outer():\n    load = safe\n\n"
        "    def helper():\n        nonlocal load\n\n"
        "        from importlib import import_module as load\n\n"
        "    def run():\n        nonlocal load\n\n"
        "        load = safe\n        helper()\n\n"
        '        return load("pytest")\n\n    return run\n\n\nouter()()\n',
    ),
    (
        "a second entry to a function finding the global written",
        SAFE + "load = safe\n\n\n"
        "def run():\n    global load\n\n"
        '    value = load("pytest")\n'
        "    from importlib import import_module as load\n\n    return value\n\n\n"
        "run()\nrun()\n",
    ),
    (
        "a call above every binding of a local name",
        SAFE + "def run():\n"
        '    value = load("pytest")\n'
        "    from importlib import import_module as load\n\n    return value\n\n\n"
        "try:\n    run()\nexcept UnboundLocalError:\n    pass\n",
    ),
    (
        "the builtin importer above a later shadow",
        SAFE + '__import__("pytest")\n__import__ = safe\n',
    ),
    (
        "a declared global reaching the builtin past the scope around it",
        SAFE + "def outer():\n    __import__ = safe\n\n"
        "    def go():\n        global __import__\n\n"
        '        return __import__("pytest")\n\n    return go\n\n\nouter()()\n',
    ),
    (
        "a loop reaching a rebinding below the call",
        SAFE + "load = safe\nfor index in range(2):\n"
        '    load("pytest")\n'
        "    from importlib import import_module as load\n",
    ),
    (
        "a rebinding a branch skips",
        HEAD + "if not print:\n    load = safe\n\n" + 'load("pytest")\n',
    ),
    (
        "a rebinding in the branch the call is not in",
        HEAD + "if not print:\n    load = safe\nelse:\n" + '    load("pytest")\n',
    ),
    (
        "the left side of an and running before the right",
        HEAD + 'VALUES = (load := safe) and load("pytest")\n',
    ),
    (
        "a call running after the argument that writes the name",
        SAFE + "def switch(value):\n    global load\n\n"
        "    from importlib import import_module as load\n\n\n"
        "load = safe\n\n\n"
        "def run():\n    global load\n\n"
        "    switch(load := safe)\n"
        '    return load("pytest")\n\n\nrun()\n',
    ),
    (
        "a while body reaching the next test",
        SAFE + "def truthy(*args, **rest):\n    return 1\n\n\n"
        "def run(counter):\n    load = truthy\n"
        '    while load("pytest"):\n'
        "        counter -= 1\n"
        "        if counter < 0:\n            load = safe\n"
        "        else:\n"
        "            from importlib import import_module as load\n\n\n"
        "run(1)\n",
    ),
    (
        "a for target that is never bound",
        HEAD + "for load in []:\n    pass\n\n" + 'load("pytest")\n',
    ),
)

# The same, for syntax that Python 3.12 introduced.
GENERIC_PROGRAMS: tuple[tuple[str, str], ...] = (
    (
        "a type parameter hiding the importer module",
        "import importlib\n\n\n"
        'def run[importlib](value: importlib.import_module("pytest")) -> None:\n'
        "    return None\n\n\nrun.__annotations__\n",
    ),
    (
        "a generic annotation reaching the importer module",
        "import importlib\n\n\n"
        'def run[T](value: importlib.import_module("pytest")) -> None:\n'
        "    return None\n\n\nrun.__annotations__\n",
    ),
    (
        "a class body reaching an annotation nested in it",
        "class Thing:\n    import importlib\n\n"
        '    def method[T](self) -> importlib.import_module("pytest"):\n'
        "        return None\n\n\nThing.method.__annotations__\n",
    ),
    (
        "a class body reaching a type parameter bound nested in it",
        "class Thing:\n    import importlib\n\n"
        '    def method[T: importlib.import_module("pytest")](self):\n'
        "        return None\n\n\nThing.method.__type_params__[0].__bound__\n",
    ),
    (
        "a class body reaching the value of a type alias nested in it",
        "class Thing:\n    import importlib\n\n"
        '    type Alias[T] = importlib.import_module("pytest")\n\n\n'
        "Thing.Alias.__value__\n",
    ),
    (
        "a type parameter beside a class-local importer",
        "class Thing:\n    import importlib\n\n"
        '    def method[importlib](self) -> importlib.import_module("pytest"):\n'
        "        return None\n\n\n"
        "try:\n    Thing.method.__annotations__\nexcept AttributeError:\n    pass\n",
    ),
)

# The Python version that first parses the generic syntax above.
GENERIC_SYNTAX = (3, 12)


def _imports_pytest_for_real(sources: list[str], tmp_path: Path) -> list[bool]:
    """Run each program in a child process and say which ones imported pytest.

    A stub stands in for the real pytest, so a program costs nothing to run and
    the answer does not depend on what is installed. The child runs outside the
    socket guard in ``conftest.py``, as that module describes, so no program
    here may reach the network.
    """
    stub = tmp_path / "stub"
    (stub / "_pytest").mkdir(parents=True)
    (stub / "pytest.py").write_text("", encoding="utf-8")
    (stub / "_pytest" / "__init__.py").write_text("", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-c", RUNNER],
        input=json.dumps(sources),
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        cwd=stub,
        env={**os.environ, "PYTHONPATH": str(stub)},
    )
    answer = json.loads(completed.stdout.strip().splitlines()[-1])
    assert isinstance(answer, list)
    return [bool(value) for value in answer]


def test_every_program_that_really_imports_pytest_is_reported(tmp_path: Path) -> None:
    """The model may report more than Python runs. It may never report less."""
    programs = list(MODEL_PROGRAMS)
    if sys.version_info >= GENERIC_SYNTAX:
        programs += list(GENERIC_PROGRAMS)

    for name, source in programs:
        # A program that does not compile would be judged on a parse error
        # rather than on what it does, which reads as a pass for the wrong
        # reason.
        compile(source, f"<{name}>", "exec")

    real = _imports_pytest_for_real([source for _, source in programs], tmp_path)

    # A corpus that imports nothing, or everything, would pass while measuring
    # nothing at all.
    assert any(real), "no program in the corpus imports pytest"
    assert not all(real), "every program in the corpus imports pytest"

    missed = [
        name
        for (name, source), imported in zip(programs, real, strict=True)
        if imported and not purity.scan_source(source, Path(f"<{name}>"))
    ]
    assert missed == [], f"a real pytest import was not reported: {missed}"
