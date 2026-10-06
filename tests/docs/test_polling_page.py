"""What the Polling page states about delays and the timeout message is true.

The page has a table of the sleeps ``wait_until`` makes, and a ``text`` block
with the message of ``WaitTimeoutError``. Both are read from the page and
compared with what the library does. The delays come from a fake clock, so
nothing here sleeps in real time. The numbers in the message vary with the run,
so the comparison replaces every number with ``N`` on both sides.
"""

from __future__ import annotations

import re
from typing import NoReturn

import pytest

from pytest_graphql import (
    GraphQLHTTPStatusError,
    RequestInfo,
    WaitTimeoutError,
    build_client,
)
from pytest_graphql._core import polling
from tests.docs.blocks import ROOT, blocks_of
from tests.schema.fake_transport import FakeGraphQLTransport
from tests.schema.resolvers import build_schema
from tests.unit.scripted_steps import FakeClock

PAGE = ROOT / "docs" / "polling.md"
URL = "http://localhost:8000/graphql"

#: One row of the delay table: three settings, then the sleeps or ``none``.
ROW = re.compile(
    r"^\| `(?P<interval>[\d.]+)` \| `(?P<backoff>[\d.]+)` \| `(?P<timeout>[\d.]+)` "
    r"\| (?P<sleeps>[^|]+) \|$"
)
NUMBER = re.compile(r"\d+(?:\.\d+)?")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(polling, "_monotonic", fake.monotonic)
    monkeypatch.setattr(polling, "_sleep", fake.sleep)
    return fake


def _rows() -> list[re.Match[str]]:
    lines = PAGE.read_text(encoding="utf-8").splitlines()
    return [match for line in lines if (match := ROW.match(line))]


def test_the_page_has_a_delay_table() -> None:
    assert len(_rows()) >= 3


@pytest.mark.parametrize("row", _rows(), ids=lambda row: row.group(0)[:40])
def test_each_row_lists_the_sleeps_that_wait_until_makes(
    row: re.Match[str], clock: FakeClock
) -> None:
    schema = build_schema()
    client = build_client(
        url=URL, transport=FakeGraphQLTransport(schema), schema=schema
    )
    with pytest.raises(WaitTimeoutError):
        client.wait_until(
            "pingScalar",
            until=lambda _value: False,
            timeout=float(row["timeout"]),
            interval=float(row["interval"]),
            backoff=float(row["backoff"]),
        )
    sleeps = row["sleeps"].strip()
    shown = [] if sleeps == "none" else [float(part) for part in sleeps.split(",")]
    assert clock.sleeps == shown


class _AlwaysUnavailable:
    """The 503 transport of the page, with no calls left over."""

    def send(
        self,
        request: RequestInfo,
        *,
        timeout: float,  # noqa: ARG002 -- the Transport signature
    ) -> NoReturn:
        raise GraphQLHTTPStatusError(
            "request failed with status 503: Service Unavailable",
            request=request.redacted(),
            status_code=503,
            body_excerpt="Service Unavailable",
        )

    def close(self) -> None:
        pass


@pytest.mark.usefixtures("clock")
def test_the_message_block_is_the_message_of_the_timeout() -> None:
    shown = [
        block
        for block in blocks_of(PAGE)
        if block.language == "text" and block.source.startswith("wait_until(")
    ]
    assert len(shown) == 1
    schema = build_schema()
    client = build_client(url=URL, transport=_AlwaysUnavailable(), schema=schema)
    with pytest.raises(WaitTimeoutError) as raised:
        client.wait_until(
            "user",
            id="u1",
            fields=["id", "name"],
            until=lambda user: user.name,
            timeout=0.05,
            interval=0.01,
            ignore=GraphQLHTTPStatusError,
        )
    actual = str(raised.value).splitlines()
    expected = shown[0].source.splitlines()
    assert len(actual) == len(expected)
    for got, wanted in zip(actual, expected, strict=True):
        assert NUMBER.sub("N", got) == NUMBER.sub("N", wanted)
