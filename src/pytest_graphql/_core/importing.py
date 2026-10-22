"""Parse and import a ``module:attribute`` path (DESIGN 2, 13.3, 13.7).

One function imports the object a path names. The pytest plugin and the
``pytest-graphql`` command call it and differ only in how they report a
failure: the plugin fails the run with its own message, and the command exits
with a status. This module imports no pytest, so both can use it.
"""

from __future__ import annotations

import importlib
import re
from typing import Final

#: ``package.module:attribute``, the shape of ``gql_app`` and ``--app``.
APP_PATH: Final = re.compile(
    r"[_A-Za-z][_0-9A-Za-z]*(\.[_A-Za-z][_0-9A-Za-z]*)*:[_A-Za-z][_0-9A-Za-z]*"
)


class ImportPathError(Exception):
    """A path named a module or an attribute that does not exist."""


class MissingModuleError(ImportPathError):
    """There is no module with the name in the path."""

    def __init__(self, module_name: str) -> None:
        super().__init__(module_name)
        self.module_name = module_name


class MissingAttributeError(ImportPathError):
    """The module has no attribute with the name in the path."""

    def __init__(self, module_name: str, attribute: str) -> None:
        super().__init__(module_name, attribute)
        self.module_name = module_name
        self.attribute = attribute


def split_app_path(path: str) -> tuple[str, str]:
    """The module name and the attribute of a ``module:attribute`` path.

    Raises:
        ValueError: When the path is not of that shape. The message holds no
            part of the path.
    """
    if not APP_PATH.fullmatch(path):
        raise ValueError("a path such as package.module:attribute")
    module_name, _, attribute = path.partition(":")
    return module_name, attribute


def import_attribute(module_name: str, attribute: str) -> object:
    """The object ``attribute`` of module ``module_name``.

    Importing runs user code, so a caller imports when it first needs the
    object. A missing module and a missing attribute raise their own error. An
    exception raised by the imported module itself, and a missing module that
    the module itself imports, propagate unchanged: they are the user's own
    failure.

    Raises:
        MissingModuleError: There is no module named ``module_name``.
        MissingAttributeError: The module has no such attribute.
    """
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as missing:
        owner = missing.name or ""
        if module_name == owner or module_name.startswith(owner + "."):
            raise MissingModuleError(module_name) from None
        raise
    try:
        return getattr(module, attribute)
    except AttributeError:
        raise MissingAttributeError(module_name, attribute) from None
