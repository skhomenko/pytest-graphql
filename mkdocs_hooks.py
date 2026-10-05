"""MkDocs hooks for the documentation site.

Registered in ``mkdocs.yml``. The rule these hooks keep is in "Documentation
dependencies" of ``docs/reference/DESIGN_DECISIONS.md``: the site publishes only
third-party files it needs and has a recorded licence basis for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mkdocs.exceptions import PluginError

if TYPE_CHECKING:
    from mkdocs.config.defaults import MkDocsConfig
    from mkdocs.structure.files import Files

#: Material ships the Lunr language packs as theme files, and MkDocs copies every
#: theme file into the site. Search loads one only when its language is not
#: English. The packs carry Mozilla Public License headers, so a pack that is
#: published without being used is a notice and source-offer duty for nothing.
_LUNR_DIR = "assets/javascripts/lunr/"


def on_files(files: Files, config: MkDocsConfig) -> Files:
    """Leave the unused Lunr language packs out of the site."""
    languages = list(config.plugins["material/search"].config.lang or [])
    if languages != ["en"]:
        # Dropping the packs would break search in that language, and keeping
        # them would publish MPL files without a notice. Neither is silent.
        raise PluginError(
            f"search languages {languages!r}: this site publishes English search "
            "only. Add a licence notice for the Lunr language packs and change "
            "mkdocs_hooks.py before enabling another language."
        )
    for file in [f for f in files if f.src_uri.startswith(_LUNR_DIR)]:
        files.remove(file)
    return files
