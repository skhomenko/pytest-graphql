"""The selection engine: the user's selection model, and auto-selection.

``policy.py`` holds ``SelectionPolicy`` and the excludes, relay and size-guard
helpers it owns. ``model.py`` holds what a user writes: ``Field``,
``Selection``, ``InlineFragment`` and ``AUTO``. ``builder.py`` walks the
schema and generates a selection set. ``normalize.py`` turns any of the four
documented input forms into one selection set, calling the builder wherever
``AUTO`` appears.
"""

from __future__ import annotations
