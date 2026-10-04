"""Every credential a configuration holds is in the secret set of every request.

A configuration has four fields that carry a credential: ``headers``,
``schema_headers``, ``cookies`` and ``proxy``. Text built from a request is
scrubbed and then cut, so a request that did not know one of them could not
remove it, and a cut through it left the start of it in the text. These tests
state the rule at the core, on the text a call records, and at the header rule
that decides which header values count.
"""

from __future__ import annotations

import pytest
from graphql import GraphQLSchema

from pytest_graphql._core.client import ClientConfig, configuration_credentials
from pytest_graphql._core.diagnostics import (
    DEFAULT_MIN_REDACTED_VALUE_LENGTH,
    RequestInfo,
    header_credentials,
)
from tests.unit.scripted_steps import build_test_schema, envelope, failure, make_client

HEADER = "header-secret-value-0123456789"
SCHEMA = "schema-only-secret-value-0123456789"
COOKIE = "cookie-secret-value-0123456789"
PROXY = "proxy-password-secret-0123456789"
FIELDS = {
    "headers": {"headers": {"Authorization": f"Bearer {HEADER}"}},
    "schema_headers": {"schema_headers": {"Authorization": f"Bearer {SCHEMA}"}},
    "cookies": {"cookies": {"sid": COOKIE}},
    "proxy": {"proxy": f"http://agent:{PROXY}@proxy.example.test:3128"},
}
SECRETS = {
    "headers": HEADER,
    "schema_headers": SCHEMA,
    "cookies": COOKIE,
    "proxy": PROXY,
}


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_test_schema()


# -- the header rule ---------------------------------------------------------------


def test_a_header_in_the_rule_is_a_credential_whatever_its_case() -> None:
    found = header_credentials(
        {"X-API-Key": "abc", "Accept": "application/json"}, ["x-api-key"]
    )

    assert found == (("X-API-Key", "abc"),)


def test_every_value_of_a_cookie_header_is_a_credential() -> None:
    found = header_credentials({"Cookie": "a=1; b=two"}, [])

    assert found == (("Cookie", "1"), ("Cookie", "two"))


def test_a_cookie_header_in_the_rule_gives_the_whole_value_and_the_parts() -> None:
    found = header_credentials({"Cookie": "a=1; b=two"}, ["cookie"])

    assert found == (("Cookie", "a=1; b=two"), ("Cookie", "1"), ("Cookie", "two"))


def test_a_header_outside_the_rule_is_not_a_credential() -> None:
    assert header_credentials({"Accept": "x"}, ["authorization"]) == ()


def test_the_request_secret_set_is_the_one_the_header_rule_gives() -> None:
    headers = {"Authorization": f"Bearer {HEADER}", "Cookie": f"sid={COOKIE}"}
    request = RequestInfo(
        operation=None,
        kind="query",
        document="{ x }",
        variables={},
        headers=headers,
        url="http://example.test/graphql",
    )

    shown = request.scrub(f"{HEADER} {COOKIE}")

    assert HEADER not in shown
    assert COOKIE not in shown


# -- the configuration ----------------------------------------------------------------


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_each_field_gives_its_credential(field: str) -> None:
    found = configuration_credentials(ClientConfig(**FIELDS[field]))

    assert SECRETS[field] in {value for _, value in found} | {
        part for _, value in found for part in value.split()
    }


def test_the_credentials_of_all_four_fields_are_listed_together() -> None:
    merged: dict[str, object] = {}
    for fields in FIELDS.values():
        merged.update(fields)

    values = " ".join(
        value
        for _, value in configuration_credentials(
            ClientConfig(**merged)  # type: ignore[arg-type]
        )
    )

    for secret in SECRETS.values():
        assert secret in values


def test_a_configuration_with_no_credential_gives_none() -> None:
    assert configuration_credentials(ClientConfig()) == ()


def test_an_unusable_proxy_is_not_a_credential_and_not_an_error() -> None:
    assert configuration_credentials(ClientConfig(proxy="no-scheme-here")) == ()


# -- the text a call records -----------------------------------------------------------


def shows_a_prefix(text: str, secret: str) -> bool:
    return secret[:DEFAULT_MIN_REDACTED_VALUE_LENGTH] in text


@pytest.mark.parametrize("field", sorted(FIELDS))
@pytest.mark.parametrize("limit", range(0, 60, 3))
def test_a_cut_through_any_credential_leaves_no_start_of_it_in_a_recorded_call(
    schema: GraphQLSchema, field: str, limit: int
) -> None:
    secret = SECRETS[field]
    echo = f"the server said: {secret}"
    client, _ = make_client(
        schema,
        envelope({"user": {"id": echo}}, (failure(echo, code="LEAK"),)),
        max_diagnostic_bytes=limit,
        **FIELDS[field],
    )

    response = client.query(
        "user", id="u1", fields=["id"], raw=True, raise_on_error=False
    )

    (call,) = client.recorder.calls
    texts = [
        call.data,
        *call.errors,
        *(info._summary or "" for info in response.errors),
    ]
    for text in texts:
        assert not shows_a_prefix(text, secret), (field, limit, text)


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_a_per_call_header_does_not_take_a_configuration_credential_away(
    schema: GraphQLSchema, field: str
) -> None:
    # The call replaces the configured Authorization header. The configured
    # value is still a credential of the configuration, so it is still scrubbed.
    secret = SECRETS[field]
    echo = f"the server said: {secret}"
    client, _ = make_client(
        schema,
        envelope(None, (failure(echo, code="LEAK"),)),
        **FIELDS[field],
    )

    client.query(
        "user",
        id="u1",
        fields=["id"],
        raw=True,
        raise_on_error=False,
        headers={"Authorization": "Bearer something-else-entirely-0123"},
    )

    (call,) = client.recorder.calls
    assert all(secret not in text for text in call.errors)
