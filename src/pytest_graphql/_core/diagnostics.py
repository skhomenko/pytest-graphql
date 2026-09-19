"""Diagnostics foundation: ``RequestInfo``, redaction, escaping and ``as_curl()``.

Per C59, this module is the diagnostics milestone that runs before M5a, because
``Transport.send()`` (SPEC 5.6) and ``Auth.apply`` / ``Middleware.before_request``
(B5, B6) all share one canonical ``RequestInfo`` type. Everything here is a pure
function over a constructed ``RequestInfo`` and its own redaction settings: no
``ClientConfig``, no live client, no transport.

The leak boundary is rendering, not the object (section 7 of
``docs/reference/DESIGN_DECISIONS.md``). ``RequestInfo`` keeps real values and is
the type ``Auth`` and ``Middleware`` see. ``RequestInfo.redacted()`` returns a
frozen ``DiagnosticSnapshot`` that never carries an unredacted value; only a
snapshot may reach an exception, a report section, a log record or
``as_curl()``. ``RequestInfo.__repr__``/``__str__`` always render the snapshot
form, never the live fields, and ``as_curl()`` builds its command from
``redacted()`` alone, never by re-deriving the scrub over live fields.

Redaction runs in three stages, in this order (C16 "Stage order"): collect the
secret set, scrub free-form text with it, then escape control and hidden
characters. Escaping runs after the scrub so an escape sequence can never split
a match, matching the same character classification
``scripts/check_publication_hygiene.py`` uses for "invisible" and "bidi": the
two are independent tools over the same threat (a hostile string smuggling
hidden text into a report or a terminal), and this module keeps its own copy of
that classification because ``scripts/`` is required to stay dependency-free
and import nothing from ``src/``.

A dotted ``redact_variables`` pattern matches a contiguous tail of the
variable path, counted from its end, the same way a bare pattern matches a
one-segment tail: this is what makes "matched ... at every depth" (section 7,
"Name-based and path-based redaction") hold for every pattern form, not only
the dot-free defaults. The Cookie request header is redacted like any other
``redact_headers`` entry for rendering, and separately, every individual
cookie value it carries enters the secret set, so the scrub still finds one
echoed elsewhere in free-form text (section 7, "every cookie value").

Every rendered string passes the scrub before it is escaped, including a
mapping key, a redaction placeholder's own label and a header name, not only
a value (section 7, "Coverage": "No text reaches a snapshot without passing
the scrub"). URL userinfo enters the secret set in both its original,
still-percent-encoded spelling and its decoded form. Percent-escape hex
digits are case-insensitive (RFC 3986), so the scrub itself, not only the
secret set's own stored spellings, case-folds a percent-escape's two hex
digits before comparing: this catches every case spelling a secret's
percent-encoded octets could be written in without enumerating the
exponential space of per-digit case combinations.

``max_diagnostic_bytes`` bounds each of ``operation``, ``document``, ``url``,
``method``, ``headers`` and ``variables`` to its own complete rendered form,
never only a leaf's characters, and each field's budget is independent of
how many entries a container holds. A field's charge is the exact byte cost
of its ``json.dumps`` encoding, escaping included, never an approximation
from raw string length, so a value made of quotes, backslashes or non-ASCII
text is bounded by what it actually serializes to. Every field but the last
has an unavoidable rendered floor of exactly two bytes; before a field may
spend beyond its own floor, every field still to come has its floor reserved
out of the shared 32768-byte total, so the true grand total across every
field, including every forced minimum, never exceeds that cap. A dropped
entry -- an omitted scalar, key, header or subtree -- is counted by exactly
one layer, the container that holds it, never also by the recursive call
that could not fit it.

``as_curl()`` renders every header from ``DiagnosticSnapshot.curl_headers``,
an ordered sequence this same pass builds once: it never re-derives a
header's name to look up in a mapping the limiter may have truncated or
dropped a key from. ``curl_headers`` is that pass's one shared header
budget, the same one the public ``headers`` field is a view over, not a
second, uncapped field of its own: a header entry is rendered only once its
complete structural minimum -- overhead plus an empty name and an empty
value -- is verified to fit, and its curl placeholder variable is bounded
against that same budget by its own derived length, never left to grow
unbounded because a JSON-cost bound on the source name does not bound what
``_curl_header_variable`` expands each character into. Every truncation
note in ``truncated`` is built only from a field's fixed name and a byte or
entry count, never from the request-controlled text being cut, so an
oversized key or value can shrink the field it lives in but can never grow
the note that reports the cut.

A secret's source label -- the header name or variable path a
``[redacted:<source>]`` placeholder names -- cannot be checked on its own:
the fixed template text around it, "[redacted:" and "]", is itself
compile-time, public text, and a qualifying secret can equal a substring of
that template regardless of what the label is. Every marker is therefore
built and validated as one complete string, never as a label checked in
isolation, and every marker-emitting path -- a header's own redacted-value
placeholder and a redacted variable subtree's own placeholder alike -- is
built through that same validated primitive, never hand-assembled
separately. The check itself validates the marker's *escaped* form, not its
pre-escape spelling: escaping (below) always runs over a marker after it is
inserted into surrounding text, and a raw control or hidden character
carried in from a label built from request-controlled text would otherwise
pass a pre-escape check and only turn into a byte sequence matching a
different qualifying secret once escaping expands it. No fixed,
compile-time replacement can be *guaranteed* free of this, since an
attacker who reads this source can always choose a secret value equal to
whatever constant it names: a marker that still collides after a fixed
fallback label is tried escalates through a bounded number of labels drawn
from a cryptographically random source -- generated one at a time, only
once every candidate so far has collided, so the ordinary already-safe case
draws no randomness at all -- and only once that bound is exhausted --
which is only possible when the template text itself, independent of any
label, contains a qualifying secret -- falls back first to a label-free
marker and finally to an empty replacement, so this always terminates and
never emits a known secret.

Escaping's own per-character map has no cross-character lookahead, but
checking a candidate's escaped form in isolation is only as strong as
checking the real, final rendered text when escaping is the *last*
transform that text passes through -- and a marker is not the only text
this can happen to. Any scrubbed-and-escaped text -- a mapping key, a
header name, a document, not only a constructed marker -- can have
escaping synthesize a qualifying secret's spelling from a raw control
character that was not that secret before. Every field-construction site
therefore runs its complete scrub, escape and re-scrub together as one
primitive (``_scrub_and_escape``), so this risk is closed everywhere text
reaches a snapshot field, not only where a marker is built. A further
transform can still run after that -- ``repr()``'s and ``json.dumps()``'s
own backslash-and-quote doubling in ``RequestInfo.__repr__`` and
``as_curl()``, and ``shlex.quote()``'s own quote-doubling in ``as_curl()``
-- and can itself synthesize a collision the same way, from a snapshot
field that was already safe before that call touched it. Re-scanning that
call's own already-produced text cannot close this safely: the match can
span and remove a delimiter the call itself just produced -- a quote
character bounding a shell word, for one -- since a qualifying secret's raw
text is exactly as attacker-controlled as anything else here, and nothing
stops it from being chosen to equal that call's own syntax. Every value
``repr()``, ``json.dumps()`` or ``shlex.quote()`` will see is therefore
checked against that same call's own output *before* the call runs on the
real complete structure, one leaf at a time (``_boundary_safe_structure``,
``_boundary_safe_render``): a leaf whose own rendered form would contain a
qualifying secret is replaced first, with one shared, syntax-neutral marker
(``_hardened_marker``) built only from ASCII letters, digits and the fixed
template punctuation -- never a request-controlled label -- so the
complete structure that reaches the real serialization call is already
safe.

A leaf-level check cannot see everything a caller's complete output is made
of, though. The fixed wrapper text a renderer adds around its leaves --
``RequestInfo.__repr__``'s own class name and dataclass field syntax, two
independently quoted shell segments joined into one ``as_curl()`` word --
is not itself a leaf, and a qualifying secret can equal or span that text
regardless of what any leaf's content is. Text generated *after* every leaf
check has already run -- a mapping key's disambiguating ``#<n>`` suffix,
built once two distinct keys collide under the same ``render`` -- is not
checked by construction either. Substituting the whole leaf is also not a
safe strategy for ``as_curl()``'s complete ``--data`` JSON document: unlike
a single field's value, that document is not free to become an arbitrary
replacement string without breaking the structured body the command
promises to send. Every complete text a caller is about to return --
``__repr__``'s finished string, ``as_curl()``'s finished command -- is
therefore validated once more, as a whole, immediately before it is
returned (``_require_boundary_safe``): finding a qualifying secret at this
point means no per-leaf substitution can close it, because the only
remaining sources are syntax the format cannot omit or a document that
cannot be rewritten without corrupting it, so this raises
``DiagnosticRenderError`` instead of returning unsafe or malformed text.
This is a validate-only backstop, never another substitution pass -- it
edits nothing -- so it carries none of the "match spans a delimiter" risk a
post-hoc scrub has.

The raised exception's own state is fixed, compile-time text -- its
descriptive message and its ``renderer`` label alike -- and is exactly as
exposed to this risk as any other fixed rendering syntax: a qualifying
secret can equal a substring of either one (DESIGN_DECISIONS.md section 7,
"Value scrub for free-form text"). A field's fixed provenance does not make
it safe; only checking it does, so ``_require_boundary_safe`` validates
every string it passes to the exception constructor against the same
qualifying set through one shared primitive (``_boundary_safe_field``), and
passes an empty string in place of whichever field collides. A qualifying
secret is never the empty string, so the empty fallback can never contain
one -- this closes the property completely rather than trading one
collision surface for another.

The curl placeholder variable's mapping from header name to variable is
deterministic and injective (module docstring, "as_curl()"), and truncating
a variable's own text is not: two distinct, unrelated names can share the
same truncated prefix. When the complete variable does not fit the header
budget, this never renders a shortened one; it falls back to the same
literal-placeholder rendering already used when even the variable's fixed
prefix cannot be charged.
"""

from __future__ import annotations

import json
import re
import shlex
import unicodedata
import urllib.parse
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from secrets import token_hex as _random_secret_token_hex
from types import MappingProxyType
from typing import Any, cast

from pytest_graphql._core.errors import DiagnosticRenderError
from pytest_graphql._core.naming import to_snake
from pytest_graphql._core.schema.info import OperationKind

#: SPEC section 7 / C2 defaults. Header names matched case-insensitively
#: after stripping.
DEFAULT_REDACT_HEADERS: frozenset[str] = frozenset(
    {"authorization", "cookie", "x-api-key", "proxy-authorization"}
)

#: C2 defaults. Dotted paths and glob patterns matched case-insensitively
#: against snake_case variable paths, as a tail of the path at any depth;
#: see the module docstring and ``_path_matches_any``.
DEFAULT_REDACT_VARIABLES: tuple[str, ...] = (
    "password",
    "token",
    "secret",
    "api_key",
    "access_token",
    "refresh_token",
    "authorization",
    "otp",
    "pin",
    "credit_card",
    "ssn",
)

#: C16/C2 defaults.
DEFAULT_MIN_REDACTED_VALUE_LENGTH = 8
DEFAULT_MAX_DIAGNOSTIC_BYTES = 4096
DEFAULT_MAX_RECORDED_ERRORS = 20

#: C2 total cap per snapshot, across every field.
_TOTAL_DIAGNOSTIC_BYTES_CAP = 32768

#: The curl placeholder variable namespace (C48, C54).
_CURL_HEADER_VARIABLE_PREFIX = "PYTEST_GQL_HEADER_"

#: The fixed, generic source label substituted for a secret's own source
#: label when that label contains another known secret (module docstring,
#: "A secret's source label").
_GENERIC_SECRET_SOURCE = "redacted-source"

#: How many cryptographically random labels ``_build_safe_marker`` draws
#: before giving up on a labeled marker entirely. Bounded so a secret set
#: that covers every short string over the random source's alphabet cannot
#: make this loop forever (module docstring, "A secret's source label").
_MAX_LABEL_ESCALATION_ATTEMPTS = 8

#: The label-free fallback marker, tried after every labeled candidate --
#: including every random one -- still collides.
_MARKER_NO_LABEL = "[redacted]"


# --------------------------------------------------------------------------
# Hidden-character classification, shared by escaping and by evasion-proof
# scrubbing. Mirrors scripts/check_publication_hygiene.py's INVISIBLE, BIDI
# and tag-character sets; kept as an independent copy because that script
# must stay stdlib-only and import nothing from this package.
# --------------------------------------------------------------------------

_INVISIBLE_CHARS: frozenset[str] = frozenset(
    chr(code)
    for code in (0x00A0, 0x00AD, 0x180E, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)
)

_BIDI_CHARS: frozenset[str] = frozenset(
    chr(code)
    for code in (
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    )
)


_HEX_DIGITS: frozenset[str] = frozenset("0123456789abcdefABCDEF")


def _is_tag_character(char: str) -> bool:
    return 0xE0000 <= ord(char) <= 0xE007F


def _is_hidden(char: str) -> bool:
    """Whether the publication-hygiene scanner would flag this character."""
    if char in _INVISIBLE_CHARS or char in _BIDI_CHARS:
        return True
    if _is_tag_character(char):
        return True
    return unicodedata.category(char) == "Cf"


def _needs_escape(char: str) -> bool:
    """C2/C45: every control and hidden character except tab and newline."""
    if char in ("\t", "\n"):
        return False
    code = ord(char)
    if code < 0x20 or code == 0x7F:
        return True
    if 0x80 <= code <= 0x9F:
        return True
    return _is_hidden(char)


def escape_control_characters(text: str) -> str:
    """Escape every control or hidden character in ``text`` (C2, C45).

    The escape form follows the code point: ``\\xNN`` below U+0100,
    ``\\uNNNN`` below U+10000, ``\\UNNNNNNNN`` at or above U+10000, per C45's
    correction of the four-digit-only form (it cannot represent a Unicode tag
    character).
    """
    pieces: list[str] = []
    for char in text:
        if not _needs_escape(char):
            pieces.append(char)
            continue
        code = ord(char)
        if code < 0x100:
            pieces.append(f"\\x{code:02x}")
        elif code < 0x10000:
            pieces.append(f"\\u{code:04x}")
        else:
            pieces.append(f"\\U{code:08x}")
    return "".join(pieces)


# --------------------------------------------------------------------------
# The free-form-text scrub (C16), exposed as its own function per C59 so
# M5a can run a response excerpt or a transport error message through it
# before either reaches an exception.
# --------------------------------------------------------------------------


def _canonicalize_percent_escapes(text: str) -> str:
    """Uppercase every percent-escape's two hex digits, in place.

    RFC 3986 percent-escape hex digits are case-insensitive, so ``%2f`` and
    ``%2F`` denote the same octet. This folds each escape's case without
    inserting, deleting or reordering a single character, so a position in
    the result always corresponds to the same position in ``text`` -- the
    property ``scrub_text`` relies on to keep its span-preserving match.
    """
    chars = list(text)
    length = len(chars)
    index = 0
    while index + 2 < length:
        if (
            chars[index] == "%"
            and chars[index + 1] in _HEX_DIGITS
            and (chars[index + 2] in _HEX_DIGITS)
        ):
            chars[index + 1] = chars[index + 1].upper()
            chars[index + 2] = chars[index + 2].upper()
            index += 3
        else:
            index += 1
    return "".join(chars)


def _marker_contains_secret(marker: str, qualifying_values: tuple[str, ...]) -> bool:
    """Whether ``marker`` contains a qualifying secret, by the same
    case-folded percent-escape equivalence :func:`scrub_text` matches with.
    """
    canon_marker = _canonicalize_percent_escapes(marker)
    return any(
        _canonicalize_percent_escapes(secret) in canon_marker
        for secret in qualifying_values
    )


def _is_safe_marker(marker: str, qualifying_values: tuple[str, ...]) -> bool:
    """Whether ``marker`` reaches a snapshot field free of every qualifying
    secret.

    A marker is never rendered as the literal text checked here: the stage
    order (module docstring, "Redaction runs in three stages") always runs
    :func:`escape_control_characters` over it afterward, as part of the
    larger text it was inserted into. That escape is a pure per-character
    map with no cross-character lookahead, so escaping the complete larger
    text always yields exactly ``escape_control_characters(marker)`` at the
    span the marker occupies, regardless of what surrounds it -- checking
    that escaped form in isolation is exactly as strong as checking the
    snapshot field the marker reaches. A raw control or hidden character
    inside ``marker`` (from a label built from request-controlled text)
    would otherwise pass a pre-escape check and only turn into a byte
    sequence that matches a different qualifying secret once escaping
    expands it. This is not the last transform a rendered field can pass
    through, though: :func:`_hardened_marker` and the pre-render check in
    :func:`_boundary_safe_structure` (used from ``RequestInfo.__repr__``
    and ``as_curl()``) cover the collision a *further* transform --
    ``repr()``'s, ``json.dumps()``'s or ``shlex.quote()``'s own escaping --
    can synthesize afterward (module docstring, "A secret's source label").
    """
    return not _marker_contains_secret(
        escape_control_characters(marker), qualifying_values
    )


def _escalating_marker(
    candidates: tuple[str, ...], qualifying_values: tuple[str, ...]
) -> str:
    """Try each of ``candidates`` as a marker's source label in order, then
    escalate through a bounded number of cryptographically random labels,
    then a label-free marker, then an empty replacement -- the shared
    escalation :func:`_build_safe_marker` and :func:`_hardened_marker` both
    run (module docstring, "A secret's source label").

    The fixed template text a marker is built from, "[redacted:" and "]",
    is itself compile-time, public text: if a qualifying secret happens to
    equal a substring of that template (the literal word "redacted", for
    example), the complete marker leaks it regardless of what the label is,
    so checking a label alone can never close this. Generating a random
    candidate only once every deterministic one has already collided keeps
    the ordinary, already-safe case free of any random draw at all; the
    bounded attempt count keeps this terminating even against a secret set
    that covers every short string over the random source's alphabet.
    """
    for candidate in candidates:
        marker = f"[redacted:{candidate}]"
        if _is_safe_marker(marker, qualifying_values):
            return marker
    for _ in range(_MAX_LABEL_ESCALATION_ATTEMPTS):
        marker = f"[redacted:{_random_secret_token_hex(16)}]"
        if _is_safe_marker(marker, qualifying_values):
            return marker
    if _is_safe_marker(_MARKER_NO_LABEL, qualifying_values):
        return _MARKER_NO_LABEL
    return ""


def _build_safe_marker(label: str, qualifying_values: tuple[str, ...]) -> str:
    """Build a ``[redacted:<source>]`` marker whose complete rendered text --
    not only ``label``, and not only its own pre-escape spelling -- contains
    no value in ``qualifying_values``.

    ``label`` is escaped before it is ever placed in the template, not
    after: ``label`` is request-controlled text (a header name or variable
    path) and can carry a raw control character of its own, and this marker
    can be inserted by :func:`_scrub_and_escape`'s *second* scrub pass, which
    runs after escaping and applies no escape pass of its own afterward. A
    raw control character embedded from an un-escaped label would otherwise
    reach a snapshot field exactly as-is -- not a different secret's
    spelling, but a plain escape-stage violation, and one that could still
    combine with neighboring text to spell one. Escaping the label first
    means the marker never carries a raw control character regardless of
    whether anything escapes the text it lands in afterward, so
    :func:`_is_safe_marker`'s own ``escape_control_characters`` call over the
    complete marker is already a no-op for this candidate. A raw quote or
    backslash in the escaped label is still safe here, since this marker is
    only ever inserted into a snapshot field that a later ``repr()`` or
    ``json.dumps()`` pass has not yet serialized (module docstring,
    "as_curl()"). :func:`_hardened_marker` is the label-free variant used at
    a rendering boundary that has already serialized its text, where a raw
    quote or backslash carried in from a label could instead break that
    serialization's own syntax.
    """
    return _escalating_marker(
        (escape_control_characters(label), _GENERIC_SECRET_SOURCE), qualifying_values
    )


def _hardened_marker(qualifying_values: tuple[str, ...]) -> str:
    """Build a replacement marker from fixed, syntax-neutral text only --
    never a label -- for the rendering-boundary defense in
    :meth:`RequestInfo.__repr__` and :meth:`RequestInfo.as_curl`.

    A regular marker's label component is request-controlled text and can
    carry a raw quote or backslash character; substituting that into text a
    renderer has already serialized -- a JSON string body, a Python
    ``repr()`` -- could break that renderer's own syntax at the substitution
    point, the same way a raw control character could once make escaping
    synthesize a different secret's spelling (module docstring, "A secret's
    source label"). This marker is built only from ASCII letters, digits,
    hyphens and the fixed template punctuation, so it can never do that.
    """
    return _escalating_marker((_GENERIC_SECRET_SOURCE,), qualifying_values)


def _boundary_safe_structure(
    value: Any, qualifying_values: tuple[str, ...], render: Callable[[str], str]
) -> Any:
    """Recursively replace any string leaf or mapping key in ``value`` whose
    own ``render`` output would contain a qualifying secret, with the one
    shared :func:`_hardened_marker`, leaving every other leaf untouched.

    ``render`` is a downstream serializer this module does not control --
    ``repr()``, ``json.dumps()``, ``shlex.quote()`` -- applied to one
    already-safe leaf at a time, the same unit that serializer will
    independently escape when the caller renders the complete structure.
    Checking and, if needed, substituting *before* that render call runs
    means the caller's own final serialization (over the returned,
    already-safe structure) is the only time ``render`` ever sees this data,
    so its output can never be spliced afterward: a match found by
    re-scanning already-serialized text can span and remove a delimiter the
    serializer itself produced -- the complete-quoted-value case this
    replaces (module docstring, "A further transform") -- because a
    qualifying secret's raw text is exactly as attacker-controlled as any
    other value here, and nothing stops it from being chosen to equal a
    serializer's own syntax. Substituting the whole leaf instead keeps every
    later ``render`` call over a plain, complete string, so its output stays
    syntactically valid by construction.

    A dataclass leaf (``DiagnosticSnapshot``, ``_HeaderEntry``) is walked
    field by field via ``dataclasses.replace``, since neither is a
    ``Mapping``. Two distinct mapping keys that both collide collapse to the
    same marker text; a numeric suffix keeps them distinct entries rather
    than silently overwriting one, without reintroducing any of the
    replaced keys' own text.
    """
    if isinstance(value, str):
        if qualifying_values and _marker_contains_secret(
            render(value), qualifying_values
        ):
            return _hardened_marker(qualifying_values)
        return value
    if isinstance(value, _HeaderEntry):
        return replace(
            value,
            name=_boundary_safe_structure(value.name, qualifying_values, render),
            value=_boundary_safe_structure(value.value, qualifying_values, render),
        )
    if isinstance(value, DiagnosticSnapshot):
        return replace(
            value,
            operation=(
                None
                if value.operation is None
                else _boundary_safe_structure(
                    value.operation, qualifying_values, render
                )
            ),
            document=_boundary_safe_structure(
                value.document, qualifying_values, render
            ),
            variables=_boundary_safe_structure(
                value.variables, qualifying_values, render
            ),
            headers=_boundary_safe_structure(value.headers, qualifying_values, render),
            method=_boundary_safe_structure(value.method, qualifying_values, render),
            url=_boundary_safe_structure(value.url, qualifying_values, render),
            curl_headers=_boundary_safe_structure(
                value.curl_headers, qualifying_values, render
            ),
        )
    if isinstance(value, Mapping):
        safe: dict[Any, Any] = {}
        for key, sub in value.items():
            safe_key = (
                _boundary_safe_structure(key, qualifying_values, render)
                if isinstance(key, str)
                else key
            )
            base_key, suffix = safe_key, 1
            while safe_key in safe:
                suffix += 1
                safe_key = f"{base_key}#{suffix}"
            safe[safe_key] = _boundary_safe_structure(sub, qualifying_values, render)
        return safe
    if isinstance(value, tuple):
        return tuple(
            _boundary_safe_structure(item, qualifying_values, render) for item in value
        )
    if isinstance(value, list):
        return [
            _boundary_safe_structure(item, qualifying_values, render) for item in value
        ]
    return value


def _boundary_safe_render(
    text: str, qualifying_values: tuple[str, ...], render: Callable[[str], str]
) -> str:
    """Check-then-render a standalone string (module docstring,
    "A further transform"): the single-leaf case of
    :func:`_boundary_safe_structure`, immediately rendered rather than
    returned for a caller to assemble into a larger structure first.
    """
    return render(_boundary_safe_structure(text, qualifying_values, render))


def _require_boundary_safe(
    rendered: str, qualifying_values: tuple[str, ...], renderer: str
) -> str:
    """Validate a *complete* rendered output, immediately before a caller
    returns it, as the final backstop behind :func:`_boundary_safe_structure`
    (module docstring, "A leaf-level check cannot see everything").

    A leaf-level check cannot see the fixed wrapper text a renderer adds
    around its leaves -- a dataclass ``repr()``'s own class name and field
    syntax, two independently quoted shell segments joined into one
    ``as_curl()`` word -- nor text generated after every leaf check already
    ran, such as a mapping key's disambiguating ``#<n>`` suffix. A qualifying
    secret can equal or span any of that text even though no single leaf's
    own render contains it. This performs no substitution: editing the
    complete text here would reintroduce the exact risk
    :func:`_boundary_safe_structure` replaced (module docstring, "A further
    transform") -- a match spanning and removing a delimiter the caller's
    own assembly just produced. When ``rendered`` still contains a
    qualifying secret at this point, no per-leaf substitution could have
    closed it, because the only remaining sources are syntax the format
    cannot omit (a class name, a shell delimiter) or a complete JSON
    document that cannot be rewritten without corrupting it, so this raises
    :class:`~pytest_graphql._core.errors.DiagnosticRenderError` instead of
    returning unsafe or malformed text.

    Every string the raised exception carries is validated the same way, not
    only its message: a qualifying secret can equal a substring of any fixed,
    compile-time text the exception stores, including the ``renderer`` label
    itself, exactly as it can equal a renderer's own wrapper syntax
    (DESIGN_DECISIONS.md section 7, "Value scrub for free-form text"). Fixed
    provenance does not make a field safe; only checking it against the
    qualifying set does. This never returns such a field
    unchecked; when one contains a qualifying secret, the exception carries
    an empty string in its place instead. A qualifying secret is never the
    empty string (:func:`_add_secret` never adds one), so the empty fallback
    can never contain one, regardless of what collided.
    """
    if not (qualifying_values and _marker_contains_secret(rendered, qualifying_values)):
        return rendered
    message = DiagnosticRenderError.default_message(renderer)
    raise DiagnosticRenderError(
        _boundary_safe_field(renderer, qualifying_values),
        _boundary_safe_field(message, qualifying_values),
    )


def _boundary_safe_field(text: str, qualifying_values: tuple[str, ...]) -> str:
    """A string bound for a raised :class:`DiagnosticRenderError`'s public
    state, replaced with ``""`` when it contains a qualifying secret.

    Shared by every field :func:`_require_boundary_safe` passes to the
    exception constructor, so a field is never classified safe by its origin
    (module-fixed text, a caller-supplied label) instead of by checking it
    (DESIGN_DECISIONS.md section 7, "Value scrub for free-form text").
    """
    return "" if _marker_contains_secret(text, qualifying_values) else text


def scrub_text(text: str, secrets: Mapping[str, str]) -> str:
    """Replace every known secret value in ``text`` with its marker.

    ``secrets`` maps a secret value (or one of its derived forms) to the
    complete, already safety-checked replacement text to render in its
    place (see :func:`_build_safe_marker`); this never builds or wraps a
    marker itself, so every replacement this emits was already validated as
    one complete string. Matching runs left to right over a copy of
    ``text`` with hidden and bidirectional characters removed, so a secret
    split by a zero-width character is still found (C16 "Evasion"); the
    replacement is then applied to the corresponding span of the *original*
    text, so a hidden character outside a match survives to be escaped by
    the next stage instead of silently vanishing here. Candidates are tried
    longest first, and a single left-to-right pass never rescans emitted
    output, so a placeholder can never be produced from another placeholder.

    Both the searched text and every candidate are also compared with their
    percent-escape hex digits case-folded (``_canonicalize_percent_escapes``),
    so a secret matches every case spelling of its own percent-encoded
    octets without enumerating the exponential space of per-digit case
    combinations; the replacement span is still taken from the original,
    uncanonicalized text.
    """
    if not secrets or not text:
        return text

    normalized_chars: list[str] = []
    orig_positions: list[int] = []
    for index, char in enumerate(text):
        if _is_hidden(char):
            continue
        normalized_chars.append(char)
        orig_positions.append(index)
    normalized = "".join(normalized_chars)
    canon_normalized = _canonicalize_percent_escapes(normalized)

    candidates = sorted((secret for secret in secrets if secret), key=len, reverse=True)
    canon_candidates = {
        candidate: _canonicalize_percent_escapes(candidate) for candidate in candidates
    }

    pieces: list[str] = []
    last_orig_end = 0
    cursor = 0
    length = len(canon_normalized)
    while cursor < length:
        match = next(
            (
                s
                for s in candidates
                if canon_normalized.startswith(canon_candidates[s], cursor)
            ),
            None,
        )
        if match is None:
            cursor += 1
            continue
        match_len = len(match)
        orig_start = orig_positions[cursor]
        orig_end = orig_positions[cursor + match_len - 1] + 1
        pieces.append(text[last_orig_end:orig_start])
        pieces.append(secrets[match])
        last_orig_end = orig_end
        cursor += match_len
    pieces.append(text[last_orig_end:])
    return "".join(pieces)


def _scrub_and_escape(text: str, secrets: Mapping[str, str]) -> str:
    """Scrub ``text``, escape it, then scrub the escaped result again.

    The documented stage order runs the first scrub before escaping, so an
    escape sequence can never split an existing match. But escaping can
    also *synthesize* a different qualifying secret's own literal spelling
    from a raw control or hidden character that was not that secret before
    escaping expanded it -- and this can happen to any escaped text, not
    only a constructed marker (module docstring, "A secret's source
    label"). The second scrub pass runs over text that already has no
    remaining control or hidden character -- escaping already removed every
    one -- so it only ever catches a spelling escaping just produced, and it
    never needs another escape pass of its own: this is every field's
    complete construction, not only a marker's.
    """
    return scrub_text(escape_control_characters(scrub_text(text, secrets)), secrets)


# --------------------------------------------------------------------------
# Path matching for redact_variables (and, later, response-path redaction).
# --------------------------------------------------------------------------


def _path_matches_any(patterns: Sequence[str], path: tuple[str, ...]) -> bool:
    """Whether a variable path matches one of ``patterns`` (C2, DESIGN_DECISIONS
    section 7, "Name-based and path-based redaction").

    Every pattern is matched against a contiguous tail of ``path``, counted
    from its end: a dot-free pattern is a one-segment tail (the field's own
    name), and an N-segment dotted pattern is an N-segment tail. Both forms
    therefore reach their target "at every depth", regardless of how deeply
    it is nested, rather than only when the pattern names the complete path
    from the document root. Either form may use ``fnmatch`` wildcards.
    """
    lowered = tuple(segment.lower() for segment in path)
    for raw in patterns:
        pattern = raw.lower()
        segments = pattern.split(".")
        if len(segments) > len(lowered):
            continue
        tail = ".".join(lowered[len(lowered) - len(segments) :])
        if fnmatchcase(tail, pattern):
            return True
    return False


def _normalize_header_name(name: str) -> str:
    return name.strip().lower()


def _walk_variables(
    value: Any,
    path: tuple[str, ...],
    patterns: tuple[str, ...],
    on_leaf: Any,
    on_match: Any,
    secrets: Mapping[str, str],
) -> Any:
    """Shared traversal for redaction and secret collection.

    Lists are transparent to the path: an element does not add a segment,
    per "matched ... inside input objects and inside lists". A dict key that
    matches a ``redact_variables`` pattern stops the walk there, whatever
    shape its value is, and hands the whole subtree to ``on_match``. Every
    output key is scrubbed of ``secrets`` before it is escaped, the same
    boundary a value crosses (section 7, "Coverage": "No text reaches a
    snapshot without passing the scrub" -- a key is text too), so a known
    secret used as a variable name cannot survive unredacted as a rendered
    key. The collection pass has no secret set yet, so it calls this with an
    empty mapping: ``scrub_text`` is then a no-op and the caller ignores this
    return value, using only the ``on_leaf``/``on_match`` side effects.
    """
    if isinstance(value, Mapping):
        result = {}
        for key, sub in value.items():
            segment = to_snake(str(key))
            new_path = (*path, segment)
            safe_key = _scrub_and_escape(str(key), secrets)
            if _path_matches_any(patterns, new_path):
                result[safe_key] = on_match(sub, new_path)
            else:
                result[safe_key] = _walk_variables(
                    sub, new_path, patterns, on_leaf, on_match, secrets
                )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _walk_variables(item, path, patterns, on_leaf, on_match, secrets)
            for item in value
        ]
    return on_leaf(value, path)


def _redact_variable_tree(
    variables: Mapping[str, Any],
    patterns: tuple[str, ...],
    secrets: Mapping[str, str],
    marker_for: Callable[[str], str],
) -> dict[str, Any]:
    def on_leaf(value: Any, _path: tuple[str, ...]) -> Any:
        if isinstance(value, str):
            return _scrub_and_escape(value, secrets)
        return value

    def on_match(_value: Any, path: tuple[str, ...]) -> str:
        marker = marker_for(".".join(path))
        return _scrub_and_escape(marker, secrets)

    # The top-level call always receives a Mapping, and _walk_variables'
    # Mapping branch always returns a dict, so this cast just states what
    # the untyped, self-recursive walker already guarantees.
    walked = _walk_variables(dict(variables), (), patterns, on_leaf, on_match, secrets)
    return cast("dict[str, Any]", walked)


def _collect_variable_secrets(
    variables: Mapping[str, Any],
    patterns: tuple[str, ...],
    sources: dict[str, set[str]],
) -> None:
    def on_leaf(_value: Any, _path: tuple[str, ...]) -> None:
        return

    def on_match(value: Any, path: tuple[str, ...]) -> None:
        label = ".".join(path)
        _add_secret(value, label, sources)
        _collect_leaf_secrets(value, label, sources)

    _walk_variables(dict(variables), (), patterns, on_leaf, on_match, {})


def _collect_leaf_secrets(value: Any, label: str, sources: dict[str, set[str]]) -> None:
    """Add every scalar nested inside ``value`` to the secret set (C16).

    A matched ``redact_variables`` subtree is already added whole, as its
    JSON text form, by ``_add_secret``. A structured or delimited value can
    also be echoed elsewhere as one of its individual parts rather than as
    that whole structure, so every leaf inside the match has to enter the
    set on its own too.
    """
    if isinstance(value, Mapping):
        for sub in value.values():
            _collect_leaf_secrets(sub, label, sources)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_leaf_secrets(item, label, sources)
    else:
        _add_secret(value, label, sources)


def _add_secret(value: object, label: str, sources: dict[str, set[str]]) -> None:
    """Add ``value`` and every derived form C16 names to the secret set.

    The scheme-suffix form (the text after a header value's first space,
    for a value such as ``Bearer <token>``) is derived from the verbatim
    value; the percent-encoded form is then derived from *each* form
    already collected, verbatim and scheme-suffix alike, so a caller who
    copies the percent-encoded spelling of either is still caught (section
    7, "Derived forms": "The percent-encoded form of each value is added").
    """
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    if not text:
        return
    forms = {text}
    _, _, scheme_rest = text.partition(" ")
    if scheme_rest:
        forms.add(scheme_rest)
    for form in list(forms):
        quoted = urllib.parse.quote(form, safe="")
        if quoted != form:
            forms.add(quoted)
    for form in forms:
        sources[form].add(label)


def _parse_cookie_values(header_value: str) -> list[str]:
    """Split a ``Cookie`` request header into its individual values (C16).

    ``Cookie`` carries ``name=value`` pairs separated by ``;``. This only
    needs to find values to add to the secret set, never to validate the
    header, so a pair with no ``=`` contributes nothing.
    """
    values: list[str] = []
    for pair in header_value.split(";"):
        _, sep, value = pair.strip().partition("=")
        if sep:
            values.append(value)
    return values


#: Matches a ``scheme://netloc`` prefix directly on raw URL text, without any
#: of ``urlsplit``'s own bracket or NFKC-normalization validation. Used only
#: as :func:`_lenient_userinfo`'s fallback once ``urlsplit`` itself has
#: already rejected the URL (F01): linear-time, no nested quantifiers, so it
#: cannot itself raise or hang on adversarial input.
_SCHEME_NETLOC_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#]*)")

#: Matches everything between a URL's scheme and its first ``?``, then
#: captures the query text up to any ``#`` fragment. Used only as
#: :func:`_lenient_query`'s fallback once ``urlsplit`` itself has already
#: rejected the URL (CR-20260919T014345Z-6ed5cad-f040aef1-F02): the query
#: delimiter is unambiguous in the raw text regardless of whether the
#: authority before it is well-formed, and the single ``[^?#]*`` run is
#: linear-time, so this cannot itself raise or hang on adversarial input.
_SCHEME_QUERY_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^?#]*\?([^#]*)")


def _try_urlsplit(url: str) -> urllib.parse.SplitResult | None:
    """``urllib.parse.urlsplit``, made total over the full malformed-input space.

    ``urlsplit`` itself raises ``ValueError`` for some malformed input --  an
    unmatched IPv6 bracket, or a netloc that fails NFKC normalization -- and
    that exception's own message embeds the raw netloc it rejected, userinfo
    included: exactly the credential this module exists to keep out of a
    raised exception (CR-20260919T012246Z-6ed5cad-c2157e06-F01). Every caller
    that needs a parsed URL for the redaction boundary goes through this
    instead of the raw stdlib call, and treats ``None`` as "no safe structure
    available" rather than letting the raw ``ValueError`` escape.
    """
    try:
        return urllib.parse.urlsplit(url)
    except ValueError:
        return None


def _lenient_userinfo(url: str) -> tuple[str | None, str | None]:
    """Best-effort ``username``, ``password`` extraction for a URL ``urlsplit``
    itself rejects.

    Matches directly on the raw ``scheme://netloc`` text, tolerant of the
    stricter IPv6-bracket and NFKC-normalization grammar ``urlsplit``
    enforces, so a userinfo segment is still found and added to the secret
    set even for a netloc ``urlsplit`` refuses to parse at all. Returns
    ``(None, None)`` when the text has no recognizable ``scheme://`` prefix
    or no ``@``-delimited userinfo within it.
    """
    match = _SCHEME_NETLOC_PATTERN.match(url)
    if match is None:
        return None, None
    userinfo, sep, _ = match.group(1).rpartition("@")
    if not sep:
        return None, None
    username, _, password = userinfo.partition(":")
    return (username or None), (password or None)


def _lenient_query(url: str) -> str:
    """Best-effort query-string extraction for a URL ``urlsplit`` itself
    rejects.

    Matches directly on the raw text after the first ``?`` following the
    scheme, tolerant of the same stricter authority grammar
    :func:`_lenient_userinfo` already works around, so a query-string
    credential is still found and added to the secret set even for a netloc
    ``urlsplit`` refuses to parse at all. Returns ``""`` when the text has no
    recognizable ``scheme://`` prefix or no ``?`` within it, matching
    ``SplitResult.query``'s own empty-string convention for "no query".
    """
    match = _SCHEME_QUERY_PATTERN.match(url)
    if match is None:
        return ""
    return match.group(1)


def _safe_url(url: str) -> str:
    """Strip userinfo and the query string from a URL (C2).

    An IPv6 literal's brackets are restored around the host: ``.hostname``
    strips them, and without restoring them ``[::1]:8000`` would rebuild as
    the invalid, silently-wrong ``::1:8000``.

    This must never raise: it runs while building the redacted snapshot for
    an exception a malformed ``url`` itself caused (a non-numeric port, for
    instance), and that is exactly the moment a caller most needs a safe
    representation back, not a second, unredacted exception in its place.
    ``.port`` raises ``ValueError`` for a non-numeric port where every other
    ``SplitResult`` accessor used here stays lenient, so a port that cannot
    be parsed is dropped rather than guessed at: reusing its raw text would
    risk reproducing, unscrubbed, whatever a malformed URL put there. A URL
    ``urlsplit`` itself rejects (:func:`_try_urlsplit` returns ``None``) has
    no safe structure to extract anything from at all, so this returns a
    fixed placeholder rather than any substring of the raw text, which could
    itself still carry the credential the rejection message did
    (CR-20260919T012246Z-6ed5cad-c2157e06-F01).
    """
    split = _try_urlsplit(url)
    if split is None:
        return "<url unavailable: could not be parsed>"
    hostname = split.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = hostname
    try:
        port = split.port
    except ValueError:
        port = None
    if port is not None:
        netloc = f"{netloc}:{port}"
    stripped = split._replace(netloc=netloc, query="")
    return urllib.parse.urlunsplit(stripped)


def _json_bytes(value: Any) -> int:
    """The exact byte length of ``value``'s canonical JSON encoding."""
    return len(json.dumps(value, sort_keys=True).encode("utf-8"))


def _json_string_body_len(text: str) -> int:
    """The exact byte length ``text`` occupies inside a JSON string body.

    This is ``json.dumps(text)``'s own encoding, minus its two surrounding
    quote bytes -- the real cost of quoting, escaping and (with the default
    ``ensure_ascii=True`` this module always relies on) ``\\uXXXX``-encoding
    every non-ASCII code point, not an approximation from ``text``'s raw
    UTF-8 length. A quote, a backslash, a tab or a newline each cost two
    bytes once encoded; a non-ASCII code point outside the Basic Multilingual
    Plane costs twelve, as its own surrogate pair.
    """
    return len(json.dumps(text).encode("utf-8")) - 2


def _truncate_json_string(text: str, limit: int) -> tuple[str, int]:
    """Truncate ``text`` so its JSON string-body encoding fits in ``limit`` bytes.

    Returns the truncated text and the number of encoded bytes cut from the
    complete text. The search is over code-point counts, so it can never
    split a character, and it measures the same ``json.dumps`` encoding the
    result is later charged against, so the returned text's real rendered
    cost never exceeds ``limit``.
    """
    limit = max(limit, 0)
    full_cost = _json_string_body_len(text)
    if full_cost <= limit:
        return text, 0
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if _json_string_body_len(text[:mid]) <= limit:
            low = mid
        else:
            high = mid - 1
    truncated = text[:low]
    return truncated, full_cost - _json_string_body_len(truncated)


#: JSON structural overhead (default ``json.dumps`` separators, ``", "`` and
#: ``": "``), charged in ``_apply_size_limits`` alongside leaf content so a
#: container's *complete* rendered form is what gets bounded.
_JSON_BRACKETS_COST = 2
_JSON_SEPARATOR_COST = 2
_JSON_KEY_SEP_COST = 2
_JSON_QUOTE_COST = 2


@dataclass(frozen=True)
class _HeaderEntry:
    """One header in ``DiagnosticSnapshot.curl_headers``, in request order (C48, C54).

    Already scrubbed, escaped and bounded by ``_apply_size_limits``.
    ``curl_variable`` is set exactly when the header matched
    ``redact_headers``, to the deterministic, injective mapping ``as_curl()``
    renders as a placeholder instead of a literal value; ``value`` is then
    the already-charged ``[redacted:<name>]`` report text, never the real
    header value, which never enters this sequence or a curl argument.
    ``as_curl()`` reads only this sequence, never a live header, so it can
    never look up a name the size limiter already truncated or dropped.
    """

    name: str
    value: str
    curl_variable: str | None


@dataclass(frozen=True)
class DiagnosticSnapshot:
    """The only form of a request that may reach an exception or a report (C2).

    Every field here has already passed redaction, the free-form-text
    scrub, control-character escaping and truncation. ``truncated`` states,
    in traversal order, which fields were cut and by how many bytes; it is
    empty when nothing was cut (C2: "truncation is visible, never silent").
    ``headers`` is a name-to-rendered-value view for reports; ``curl_headers``
    is the ordered, render-safe sequence ``as_curl()`` builds its command
    from, carrying the redaction flag a plain mapping cannot.
    """

    operation: str | None
    kind: OperationKind
    document: str
    variables: Mapping[str, Any]
    headers: Mapping[str, str]
    method: str
    url: str
    idempotent: bool
    curl_headers: tuple[_HeaderEntry, ...] = ()
    truncated: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        object.__setattr__(self, "variables", MappingProxyType(dict(self.variables)))


class _FieldBudget:
    """One field's byte budget, and the cut totals its one truncation note reports.

    ``charge`` refuses a cost that would exceed the budget and leaves it
    unmodified. ``force`` always applies a cost, for the unavoidable minimum
    rendered size (an empty string's two quote bytes, an empty mapping's two
    brackets) that a field cannot be represented without even when no budget
    remains -- so a later field's share of the shared total still reflects
    this field's true, complete cost. ``note_bytes``/``note_entries`` record
    what a caller cut, in one of two units: a byte count for a value
    truncated in place, an entry count for a whole key, header or subtree
    dropped outright. Neither ever receives request-controlled text (module
    docstring, "Every truncation note").
    """

    __slots__ = ("_limit", "_used", "cut_bytes", "cut_entries")

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._used = 0
        self.cut_bytes = 0
        self.cut_entries = 0

    @property
    def used(self) -> int:
        return self._used

    def charge(self, cost: int) -> bool:
        if self._used + cost > self._limit:
            return False
        self._used += cost
        return True

    def force(self, cost: int) -> None:
        self._used += cost

    def remaining(self) -> int:
        return max(self._limit - self._used, 0)

    def note_bytes(self, count: int) -> None:
        self.cut_bytes += count

    def note_entries(self, count: int) -> None:
        self.cut_entries += count


def _apply_size_limits(
    snapshot: DiagnosticSnapshot,
    per_field_limit: int,
    header_sources: tuple[tuple[str, str | None, bool], ...],
    secrets: Mapping[str, str],
    marker_for: Callable[[str], str],
) -> DiagnosticSnapshot:
    """C2 limits: cap each field's complete rendered form, then the 32768-byte total
    (DESIGN_DECISIONS.md section 7, "Stage order, escaping and limits").

    Every byte that would appear in a field's JSON representation counts
    against its budget: string content, a number or a boolean, and every
    mapping key and list entry, not only a string leaf's own characters, and
    a string's cost is its exact ``json.dumps`` encoding, escaping included
    (``_json_string_body_len``), never raw UTF-8 length. ``operation``,
    ``document``, ``url``, ``method``, ``headers`` and ``variables`` each get
    their own budget of at most ``per_field_limit`` bytes, charged against
    whatever remains of the shared 32768-byte total after the fields
    processed before it -- never only against a leaf's own per-call
    truncation, or a container with enough entries still renders unbounded
    even though every individual leaf was capped. A failed charge always
    stops that content from growing; it is never ignored. ``kind``,
    ``idempotent`` and ``truncated`` carry no request-controlled text and are
    not capped.

    Every field but the last has an unavoidable rendered floor of exactly
    two bytes (an empty string's quotes, or an empty mapping's brackets),
    forced even past a fully spent budget because there is no way to omit a
    mandatory field. Before a field is allowed to spend beyond its own
    floor, every field still to come has its floor reserved out of the
    shared total, so one oversized field can never leave a later field's
    forced minimum unaccounted for and the true grand total never exceeds
    the documented cap.

    ``header_sources`` is ``(name, value, is_redacted)`` per header, already
    scrubbed but not yet escaped or bounded; ``value`` is ``None`` exactly
    when ``is_redacted`` is ``True``. ``secrets`` is the same secret set that
    scrub already ran, threaded through here so a name or value can run
    :func:`_scrub_and_escape` -- escaping can synthesize a different
    qualifying secret's spelling from a raw control character (module
    docstring, "A secret's source label"), and a header's own text is no
    exception to that. ``marker_for`` builds a redacted header's own
    ``[redacted:<source>]`` placeholder from its (possibly truncated)
    rendered name, already validated as one complete string by
    :func:`_build_safe_marker`; this never hand-assembles that text itself,
    so a header's own placeholder is checked the same way a free-text
    replacement label is. A header entry is committed to only
    once its complete structural minimum -- overhead plus an empty name and
    an empty value -- is verified to fit the header field's own remaining
    budget; otherwise it and every header after it are dropped, matching "a
    header that cannot fit at all, including its own structural overhead,
    is dropped" (DESIGN_DECISIONS.md section 7). A committed header's name
    is rendered first, capped to leave its value's own floor available, so
    a long name can never starve the value below the minimum the gate
    already guaranteed it. The header's curl placeholder variable is
    bounded independently against the same header budget, by its own
    derived length rather than by the name's JSON cost -- one raw character
    can expand into several once ``_curl_header_variable`` escapes it, so a
    JSON-cost bound on the name does not bound the variable it feeds. When
    no budget remains for even the variable's fixed prefix, the header
    falls back to rendering its already-bounded placeholder text literally
    instead of an environment-variable indirection, which is still safe:
    that text names no real secret. ``headers`` and ``curl_headers`` are one
    budget, not two: both are views over the same sequence this pass builds
    once, and the variable's own bytes are drawn from it too.

    A dropped entry is counted by exactly one layer: the mapping or list
    iteration (or :func:`take_root_tree` at the root) that holds it, never
    also by the recursive call that produced the ``dropped`` sentinel --
    otherwise one omitted scalar, boolean, ``None``, nested mapping or list
    is counted twice.
    """
    notes: list[str] = []
    remaining_total = _TOTAL_DIAGNOSTIC_BYTES_CAP
    #: Sentinel for "this leaf, key or subtree does not fit at all". A
    #: container drops an entry that produces this rather than embed a
    #: substitute (``None``/``""``) whose own bytes were never charged --
    #: that gap is exactly how a truncated collection's *real* re-serialized
    #: size used to exceed its charged budget.
    dropped: Any = object()

    #: How many of the six top-level fields remain to be processed
    #: (including whichever is about to start), each with an unavoidable
    #: two-byte floor -- see ``take_field``.
    pending_fields = 5 + (1 if snapshot.operation is not None else 0)

    def take_field(field_name: str, builder: Callable[[_FieldBudget], Any]) -> Any:
        nonlocal remaining_total, pending_fields
        reserve_for_later = (pending_fields - 1) * _JSON_QUOTE_COST
        available = max(remaining_total - reserve_for_later, 0)
        budget = _FieldBudget(min(per_field_limit, available))
        result = builder(budget)
        remaining_total -= budget.used
        pending_fields -= 1
        parts = []
        if budget.cut_bytes:
            parts.append(f"{budget.cut_bytes} byte(s)")
        if budget.cut_entries:
            unit = "entry" if budget.cut_entries == 1 else "entries"
            parts.append(f"{budget.cut_entries} {unit} dropped")
        if parts:
            notes.append(f"{field_name}: truncated " + ", ".join(parts))
        return result

    def take_text(budget: _FieldBudget, text: str, cap: int | None = None) -> str:
        """Bound a standalone field, or a share of one: always returns ``str``.

        ``cap`` limits how many of the budget's remaining bytes (quotes and
        content together) this call may spend, for a caller that must leave
        room for a sibling it renders afterward; it defaults to all of
        ``budget.remaining()``. Below the two-byte quote floor, this still
        force-charges that floor and returns ``""`` -- there is no way to
        omit a value this call is responsible for rendering, so a later
        field's share of the total must still reflect this cost.
        """
        ceiling = budget.remaining() if cap is None else min(cap, budget.remaining())
        if ceiling < _JSON_QUOTE_COST:
            budget.force(_JSON_QUOTE_COST)
            if text:
                budget.note_bytes(_json_string_body_len(text))
            return ""
        fits, cut = _truncate_json_string(text, ceiling - _JSON_QUOTE_COST)
        budget.charge(_JSON_QUOTE_COST)
        budget.charge(_json_string_body_len(fits))
        if cut:
            budget.note_bytes(cut)
        return fits

    def take_leaf_text(budget: _FieldBudget, text: str) -> Any:
        """Bound a string nested inside ``take_tree``: may return ``dropped``.

        A container entry that cannot afford even the quote cost is omitted
        by its caller rather than rendered as an uncharged ``""``; the
        caller counts that omission as a dropped entry.
        """
        if not budget.charge(_JSON_QUOTE_COST):
            return dropped
        fits, cut = _truncate_json_string(text, budget.remaining())
        budget.charge(_json_string_body_len(fits))
        if cut:
            budget.note_bytes(cut)
        return fits

    def take_tree(budget: _FieldBudget, value: Any) -> Any:
        """Bound a JSON tree; a subtree that cannot fit at all becomes ``dropped``.

        Every byte the return value would occupy in its own JSON
        representation -- brackets, separators, key/value punctuation, quotes
        -- is charged before it is included, so a caller never has to embed a
        placeholder whose bytes were never accounted for. This never counts
        its own omission: the caller holding this value (a mapping or list
        iteration, or :func:`take_root_tree` at the root) is the only layer
        that turns a ``dropped`` result into exactly one omitted-entry
        count, so a value dropped one level down is never counted twice.
        """
        if isinstance(value, Mapping):
            items = list(value.items())
            if not budget.charge(_JSON_BRACKETS_COST):
                return dropped
            result: dict[str, Any] = {}
            for index, (key, sub) in enumerate(items):
                if budget.remaining() <= 0:
                    budget.note_entries(len(items) - index)
                    break
                overhead = _JSON_KEY_SEP_COST + (_JSON_SEPARATOR_COST if index else 0)
                if not budget.charge(overhead):
                    budget.note_entries(len(items) - index)
                    break
                safe_key = take_leaf_text(budget, str(key))
                if safe_key is dropped:
                    budget.note_entries(len(items) - index)
                    break
                rendered = take_tree(budget, sub)
                if rendered is dropped:
                    budget.note_entries(len(items) - index)
                    break
                result[safe_key] = rendered
            return result
        if isinstance(value, list):
            if not budget.charge(_JSON_BRACKETS_COST):
                return dropped
            rendered_items: list[Any] = []
            for index, item in enumerate(value):
                if budget.remaining() <= 0:
                    budget.note_entries(len(value) - index)
                    break
                if index and not budget.charge(_JSON_SEPARATOR_COST):
                    budget.note_entries(len(value) - index)
                    break
                rendered = take_tree(budget, item)
                if rendered is dropped:
                    budget.note_entries(len(value) - index)
                    break
                rendered_items.append(rendered)
            return rendered_items
        if isinstance(value, str):
            return take_leaf_text(budget, value)
        cost = _json_bytes(value)
        if budget.charge(cost):
            return value
        return dropped

    def take_root_tree(
        budget: _FieldBudget, value: Mapping[str, Any]
    ) -> dict[str, Any]:
        """The top-level ``take_tree`` call: always returns a ``dict``.

        Unlike a nested subtree, the field itself cannot be omitted -- an
        empty mapping is the correct, self-contained result when even its
        own brackets do not fit the field's budget, and its two bracket
        bytes are force-charged in that case for the same reason
        :func:`take_text` force-charges its quotes. There is no parent
        iteration here, so this is the one place that owns counting every
        top-level entry as dropped.
        """
        rendered = take_tree(budget, dict(value))
        if rendered is dropped:
            budget.note_entries(len(value))
            budget.force(_JSON_BRACKETS_COST)
            return {}
        return cast("dict[str, Any]", rendered)

    def take_curl_variable(budget: _FieldBudget, raw_name: str) -> str | None:
        """Bound the derived curl placeholder variable against this same budget.

        The name-to-variable mapping is deterministic and injective
        (module docstring, "as_curl()"): two distinct names never share a
        variable. Truncating the variable's own text breaks that, because
        two different, unrelated names can share the same truncated
        prefix -- so this only ever charges the complete, untruncated
        variable, never a shortened one. ``_curl_header_variable`` can also
        expand one raw character into several (a non-ASCII or symbol byte
        becomes ``_XX_``), so bounding the source name's JSON cost never
        bounds the derived variable's own length either way. When the
        complete variable does not fit, ``None`` tells the caller to fall
        back to the already-bounded placeholder text instead of a variable
        indirection, which is still safe: that text names no real secret.
        """
        variable = _curl_header_variable(raw_name)
        if budget.charge(len(variable)):
            return variable
        return None

    def take_headers(budget: _FieldBudget) -> tuple[_HeaderEntry, ...]:
        """Build the bounded, ordered header sequence (see the docstring above)."""
        result: list[_HeaderEntry] = []
        if not budget.charge(_JSON_BRACKETS_COST):
            budget.force(_JSON_BRACKETS_COST)
            budget.note_entries(len(header_sources))
            return tuple(result)
        for index, (name, value, is_redacted) in enumerate(header_sources):
            overhead = _JSON_KEY_SEP_COST + (_JSON_SEPARATOR_COST if index else 0)
            min_entry_cost = overhead + _JSON_QUOTE_COST + _JSON_QUOTE_COST
            if budget.remaining() < min_entry_cost:
                budget.note_entries(len(header_sources) - index)
                break
            budget.charge(overhead)
            available = budget.remaining()
            escaped_name = _scrub_and_escape(name, secrets)
            display_name = take_text(
                budget, escaped_name, cap=available - _JSON_QUOTE_COST
            )
            if is_redacted:
                placeholder = marker_for(display_name)
                entry_value = take_text(budget, placeholder)
                curl_variable = take_curl_variable(budget, name)
                result.append(
                    _HeaderEntry(
                        name=display_name,
                        value=entry_value,
                        curl_variable=curl_variable,
                    )
                )
                continue
            escaped_value = _scrub_and_escape(value or "", secrets)
            entry_value = take_text(budget, escaped_value)
            result.append(
                _HeaderEntry(name=display_name, value=entry_value, curl_variable=None)
            )
        return tuple(result)

    operation = snapshot.operation
    if operation is not None:
        current_operation = operation
        operation = take_field(
            "operation", lambda budget: take_text(budget, current_operation)
        )
    document = take_field(
        "document", lambda budget: take_text(budget, snapshot.document)
    )
    url = take_field("url", lambda budget: take_text(budget, snapshot.url))
    method = take_field("method", lambda budget: take_text(budget, snapshot.method))
    curl_headers = take_field("headers", take_headers)
    headers = {entry.name: entry.value for entry in curl_headers}
    variables = take_field(
        "variables", lambda budget: take_root_tree(budget, snapshot.variables)
    )

    return replace(
        snapshot,
        operation=operation,
        document=document,
        url=url,
        method=method,
        headers=headers,
        curl_headers=curl_headers,
        variables=variables,
        truncated=tuple(notes),
    )


@dataclass(frozen=True, repr=False)
class RequestInfo:
    """One outgoing GraphQL HTTP request, with real values (C2).

    This is the type ``Transport.send()`` (SPEC 5.6), ``Auth.apply`` (B5) and
    ``Middleware.before_request`` (B6) see. It gains no representation that
    shows a real header or variable value: ``__repr__`` and ``__str__``
    always render ``redacted()`` instead, and only ``redacted()`` or
    ``as_curl()`` may leave this object.

    The six redaction settings are constructor data with the documented
    defaults, the same pattern C13 gives ``HttpxTransport``'s operational
    limits, so neither this module nor M5a needs ``ClientConfig``.
    ``max_recorded_errors`` has no effect within this milestone: it exists so
    M5c's response/recorder code has a field to read once errors exist to
    bound.
    """

    operation: str | None
    kind: OperationKind
    document: str
    variables: Mapping[str, Any]
    headers: Mapping[str, str]
    url: str
    method: str = "POST"
    idempotent: bool = False
    redact_headers: frozenset[str] = DEFAULT_REDACT_HEADERS
    redact_variables: tuple[str, ...] = DEFAULT_REDACT_VARIABLES
    redact_values: bool = True
    min_redacted_value_length: int = DEFAULT_MIN_REDACTED_VALUE_LENGTH
    max_diagnostic_bytes: int = DEFAULT_MAX_DIAGNOSTIC_BYTES
    max_recorded_errors: int = DEFAULT_MAX_RECORDED_ERRORS

    def __post_init__(self) -> None:
        object.__setattr__(self, "variables", MappingProxyType(dict(self.variables)))
        headers = {str(k): str(v) for k, v in self.headers.items()}
        object.__setattr__(self, "headers", MappingProxyType(headers))
        object.__setattr__(
            self,
            "redact_headers",
            frozenset(_normalize_header_name(name) for name in self.redact_headers),
        )
        object.__setattr__(self, "redact_variables", tuple(self.redact_variables))

    # -- the secret set (C16) -------------------------------------------------

    def _redaction_context(self) -> tuple[Mapping[str, str], Callable[[str], str]]:
        """The C16 secret set, mapped to its complete replacement marker, and
        the same marker builder for a header's own redacted-value placeholder.

        Private, per C16 ("never returned by a public API"): :meth:`scrub`
        below is the request-bound operation C59 requires to be exposed, and
        it never hands the caller this set itself. Collection is name- and
        path-based redaction, independent of ``redact_values`` (which only
        gates whether :meth:`scrub` and :meth:`redacted`'s free-text pass
        uses the resulting map) -- a redacted header still needs a safe
        placeholder, and the marker builder this returns has to reflect the
        complete qualifying set regardless of that flag.

        Every marker :func:`_build_safe_marker` returns is already checked
        as one complete string, never as a label in isolation (module
        docstring, "A secret's source label"), so both this method's
        secret-value map and the header pipeline's own placeholder share
        one validated construction path.
        """
        sources: dict[str, set[str]] = defaultdict(set)

        for name, value in self.headers.items():
            normalized = _normalize_header_name(name)
            if normalized in self.redact_headers:
                _add_secret(value, name, sources)
            if normalized == "cookie":
                for cookie_value in _parse_cookie_values(value):
                    _add_secret(cookie_value, name, sources)

        _collect_variable_secrets(self.variables, self.redact_variables, sources)

        split = _try_urlsplit(self.url)
        if split is not None:
            username, password, query = split.username, split.password, split.query
        else:
            # ``urlsplit`` itself rejected this URL (F01): fall back to a
            # lenient, non-raising extraction so a userinfo or query-string
            # credential still enters the secret set instead of surviving
            # unscrubbed in whatever raw exception text the rejection
            # produces elsewhere (CR-20260919T014345Z-6ed5cad-f040aef1-F02
            # for the query half).
            username, password = _lenient_userinfo(self.url)
            query = _lenient_query(self.url)
        if username:
            _add_secret(username, "url", sources)
            _add_secret(urllib.parse.unquote(username), "url", sources)
        if password:
            _add_secret(password, "url", sources)
            _add_secret(urllib.parse.unquote(password), "url", sources)
        for _, value in urllib.parse.parse_qsl(query, keep_blank_values=True):
            _add_secret(value, "url", sources)

        min_length = self.min_redacted_value_length
        qualifying_values = tuple(
            value for value in sources if len(value.strip()) >= min_length
        )

        def marker_for(label: str) -> str:
            return _build_safe_marker(label, qualifying_values)

        secrets = {
            value: marker_for(min(labels))
            for value, labels in sources.items()
            if len(value.strip()) >= min_length
        }
        return secrets, marker_for

    def scrub(self, text: str) -> str:
        """Scrub ``text`` of every value in this request's secret set (C59).

        Exposed on its own, not only inside :meth:`redacted`, so a caller
        with free-form text of its own -- M5a's response excerpts and
        transport error messages -- can remove this request's
        known-sensitive values without ever holding the secret set itself
        (C16: the set is "never returned by a public API"). Like
        :meth:`redacted`, this only runs the scrub stage: a caller that
        renders the result into its own report is responsible for running
        it through :func:`escape_control_characters` too, the same as
        :meth:`redacted` does.
        """
        if not self.redact_values:
            return text
        secrets, _ = self._redaction_context()
        return scrub_text(text, secrets)

    # -- the rendering boundary (C2) ------------------------------------------

    def _build_snapshot(
        self, secrets: Mapping[str, str], marker_for: Callable[[str], str]
    ) -> tuple[DiagnosticSnapshot, Mapping[str, str]]:
        """Build the redacted snapshot from an already-computed secret set.

        Returns the snapshot alongside the *effective* secret set used to
        build it (``{}`` when ``redact_values`` is ``False``). A caller that
        renders a *further* representation of the snapshot -- ``__repr__``'s
        dataclass ``repr()``, ``as_curl()``'s ``json.dumps`` body -- reuses
        that same set to run one more scrub pass over its own complete
        output text: a downstream renderer's own escaping (``repr()``'s and
        ``json.dumps()``'s backslash-and-quote doubling) can itself
        synthesize a different qualifying secret's spelling from text that
        was already safe before that renderer touched it, the same way
        :func:`escape_control_characters` can (module docstring, "A
        secret's source label"). Calling :meth:`_redaction_context` a
        second time here instead would risk an independent random
        escalation draw diverging from the marker already embedded in the
        snapshot's own fields, and would draw randomness :func:`redacted`
        already avoided.
        """
        if not self.redact_values:
            secrets = {}

        operation = self.operation
        if operation is not None:
            operation = _scrub_and_escape(operation, secrets)
        document = _scrub_and_escape(self.document, secrets)
        variables = _redact_variable_tree(
            self.variables, self.redact_variables, secrets, marker_for
        )
        # Scrubbed here, alongside every other field; escaping and bounding
        # (including the header name a redacted entry's curl placeholder is
        # derived from) happen together in ``_apply_size_limits`` so a name
        # is never escaped and truncated in two places that could disagree.
        header_sources = tuple(
            (
                scrub_text(name, secrets),
                None
                if _normalize_header_name(name) in self.redact_headers
                else scrub_text(value, secrets),
                _normalize_header_name(name) in self.redact_headers,
            )
            for name, value in self.headers.items()
        )
        snapshot = DiagnosticSnapshot(
            operation=operation,
            kind=self.kind,
            document=document,
            variables=variables,
            headers={},
            method=_scrub_and_escape(self.method, secrets),
            url=_scrub_and_escape(_safe_url(self.url), secrets),
            idempotent=self.idempotent,
        )
        snapshot = _apply_size_limits(
            snapshot, self.max_diagnostic_bytes, header_sources, secrets, marker_for
        )
        return snapshot, secrets

    def redacted(self) -> DiagnosticSnapshot:
        """The only view of this request that may reach an exception or a report."""
        secrets, marker_for = self._redaction_context()
        snapshot, _ = self._build_snapshot(secrets, marker_for)
        return snapshot

    def __repr__(self) -> str:
        secrets, marker_for = self._redaction_context()
        snapshot, effective_secrets = self._build_snapshot(secrets, marker_for)
        qualifying_values = tuple(effective_secrets)
        safe_snapshot = _boundary_safe_structure(snapshot, qualifying_values, repr)
        return _require_boundary_safe(
            f"RequestInfo({safe_snapshot!r})", qualifying_values, "repr()"
        )

    def __str__(self) -> str:
        return repr(self)

    # -- as_curl() (C45, C48, C54) --------------------------------------------

    def as_curl(self) -> str:
        """A runnable ``curl`` command reproducing this request, secrets hidden.

        Built from :meth:`redacted`, the only view of this request that may
        cross the rendering boundary (section 7, "Boundary"): the method,
        URL and body come from the snapshot, and every header comes from
        ``snapshot.curl_headers`` -- an ordered sequence already scrubbed,
        escaped and bounded by that same pipeline, so this method never
        re-derives it over a live field and never looks up a name in a
        mapping the size limiter may have truncated or dropped a key from.
        A header matched by ``redact_headers`` never appears as a literal
        value, redacted or not: it renders as the C48/C54 placeholder, two
        quoted segments forming one shell word, so the command stays
        runnable once the caller exports the named environment variable.
        Every other literal component is quoted as data with
        ``shlex.quote``, never split across an expansion.

        Every value that reaches ``json.dumps`` or ``shlex.quote`` is
        checked against that same call's own output first, against the
        rendering-boundary risk :meth:`__repr__` also guards: either one's
        own backslash-, quote- or delimiter-doubling escaping can synthesize
        a different qualifying secret's spelling from text that was already
        safe before that call ran. A value that would fail this check is
        replaced before it is serialized or quoted, never spliced out of
        already-serialized or already-quoted text afterward -- doing that
        to a *quoted* command can remove the quote characters the match
        happens to span, turning inert request-controlled text into live
        shell syntax (module docstring, "A further transform"). This applies
        to every individual field the JSON body is built from -- ``query``,
        ``operationName``, each variable -- but not to the complete, already
        valid ``--data`` JSON document itself: substituting that whole
        document for a marker would discard the structured request body
        `as_curl()` promises to reproduce (module docstring, "A leaf-level
        check cannot see everything"), so it is quoted directly and, like
        the complete assembled command, checked once more as a whole
        immediately before this method returns.
        """
        secrets, marker_for = self._redaction_context()
        snapshot, effective_secrets = self._build_snapshot(secrets, marker_for)
        qualifying_values = tuple(effective_secrets)

        def quote(text: str) -> str:
            return _boundary_safe_render(text, qualifying_values, shlex.quote)

        parts = [
            "curl",
            "-sS",
            "-X",
            quote(snapshot.method),
            quote(snapshot.url),
        ]
        for entry in snapshot.curl_headers:
            if entry.curl_variable is not None:
                parts.append(
                    _curl_redacted_header_argument(
                        entry.name, entry.curl_variable, quote
                    )
                )
            else:
                parts.append(f"-H {quote(f'{entry.name}: {entry.value}')}")

        body: dict[str, Any] = {
            "query": _boundary_safe_structure(
                snapshot.document, qualifying_values, json.dumps
            )
        }
        if snapshot.operation is not None:
            body["operationName"] = _boundary_safe_structure(
                snapshot.operation, qualifying_values, json.dumps
            )
        body["variables"] = _boundary_safe_structure(
            dict(snapshot.variables), qualifying_values, json.dumps
        )
        data_json = json.dumps(body, sort_keys=True)
        parts.append(f"--data {shlex.quote(data_json)}")
        return _require_boundary_safe(" ".join(parts), qualifying_values, "as_curl()")


def _curl_header_variable(name: str) -> str:
    """The injective, deterministic mapping from a field name to its variable.

    Per C48, C54.

    An ASCII letter uppercases. An ASCII digit stays. Every other character
    -- including a literal underscore, so it cannot collide with the
    delimiter below -- becomes an underscore, the uppercase hex of its UTF-8
    bytes, and a closing underscore. Two names that differ only in ASCII
    case share a variable, because HTTP field names are case-insensitive;
    every other pair of distinct names maps to a distinct variable.
    """
    pieces: list[str] = []
    for char in name:
        if char.isascii() and char.isalpha():
            pieces.append(char.upper())
        elif char.isascii() and char.isdigit():
            pieces.append(char)
        else:
            pieces.append(f"_{char.encode('utf-8').hex().upper()}_")
    return _CURL_HEADER_VARIABLE_PREFIX + "".join(pieces)


def _curl_redacted_header_argument(
    name: str, variable: str, quote: Callable[[str], str]
) -> str:
    """The ``-H`` argument for a redacted header: two quoted segments, one word.

    ``name`` and ``variable`` are already the bounded, scrubbed, escaped
    values ``_apply_size_limits`` produced for this entry; this never
    re-derives either from a live header. The header name and its ``": "``
    separator are quoted as data with ``quote`` (``as_curl()``'s
    rendering-boundary-checked ``shlex.quote``) and are never inside double
    quotes; the environment expansion is double-quoted, so the shell neither
    word-splits nor glob-expands the exported value. Quoting is never
    applied to the expansion itself, because a single-quoted ``${...}``
    does not expand.
    """
    literal = quote(f"{name}: ")
    return f'-H {literal}"${{{variable}}}"'
