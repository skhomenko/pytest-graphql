"""``parse_header_lines``, the parser behind ``gql_headers`` and the command.

It moved out of the plugin so that the command can use it without importing
pytest. The plugin's behavior is unchanged, and no error shows a header value.
"""

from __future__ import annotations

import pytest

from pytest_graphql._core.headers import parse_header_lines

SENTINEL = "hdr-sentinel-77aa"


def test_lines_become_headers_and_a_later_line_wins() -> None:
    assert parse_header_lines(
        f"Authorization: Bearer {SENTINEL}\n\n  X-Env :  test  \nx-env: prod"
    ) == {"Authorization": f"Bearer {SENTINEL}", "X-Env": "test", "x-env": "prod"}


def test_empty_text_is_no_headers() -> None:
    assert parse_header_lines("  \n \n") == {}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (f"X-Ok: 1\nno separator {SENTINEL}", "line 2"),
        (f": {SENTINEL}", "line 1"),
        (f"Bad Name: {SENTINEL}", "line 1"),
        (f"X-Bad: {SENTINEL}\x00", "control characters (line 1)"),
        (f"X-Bad: {SENTINEL}\x7f", "control characters (line 1)"),
        (f"X-Ok: fine\nX-Bad: a\nb{SENTINEL}\x1b", "line 3"),
    ],
)
def test_a_refusal_names_the_line_and_never_the_value(text: str, message: str) -> None:
    with pytest.raises(ValueError, match="line") as raised:
        parse_header_lines(text)
    assert message in str(raised.value)
    assert SENTINEL not in str(raised.value)
    assert SENTINEL not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None or SENTINEL not in str(
        raised.value.__context__
    )
