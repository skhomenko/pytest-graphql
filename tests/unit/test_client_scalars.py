"""The client's scalar registry, ``gql.fake`` and variable serialization.

A client holds one ``ScalarRegistry`` and one ``FakeContext``, and a clone
shares both. The registry feeds response decoding, the factory and variable
serialization, so a scalar registered after the client was built is seen by
all three on the next call.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from graphql import GraphQLSchema

from pytest_graphql import Field
from pytest_graphql._core.client import ClientConfig, GraphQLClient, build_client
from pytest_graphql._core.errors import ArgumentError
from pytest_graphql._core.factory import (
    FakeContext,
    FakeNamespace,
    ScalarRegistry,
    ScalarSpec,
    UniqueSource,
    unique,
)
from pytest_graphql._core.transport.base import RawResponse
from tests.factory.schemas import SCHEMA
from tests.schema.fake_transport import FakeGraphQLTransport

URL = "https://example.test/graphql"


def money_spec(**changes: Any) -> ScalarSpec:
    fields: dict[str, Any] = {
        "name": "Money",
        "serialize": str,
        "fake": lambda rng: Decimal(rng.below(10_000)) / 100,
        "parse": Decimal,
    }
    fields.update(changes)
    return ScalarSpec(**fields)


class PriceTransport:
    """A transport that always answers with one stored price."""

    def __init__(self, price: str) -> None:
        self.price = price
        self.sent: list[Any] = []

    def send(self, request: Any, *, timeout: float) -> RawResponse:  # noqa: ARG002
        self.sent.append(request)
        return RawResponse(
            status_code=200,
            media_type="application/graphql-response+json",
            data={"price": self.price},
            errors=(),
            extensions=None,
            headers={},
        )

    def close(self) -> None:
        return


def make(
    *,
    scalars: ScalarRegistry | None = None,
    fake_context: FakeContext | None = None,
    seed: int = 0,
    schema: GraphQLSchema = SCHEMA,
) -> tuple[GraphQLClient, FakeGraphQLTransport]:
    transport = FakeGraphQLTransport(schema)
    client = GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(url=URL, seed=seed),
        scalars=scalars,
        fake_context=fake_context,
    )
    return client, transport


# -- serialization on the three call paths ----------------------------------


def test_query_serializes_custom_scalars_in_its_variables() -> None:
    client, transport = make(scalars=ScalarRegistry([money_spec()]))
    client.query("total", amount=Decimal("1.50"), many=[Decimal("1"), Decimal("2")])
    assert transport.sent[-1].variables == {"amount": "1.50", "many": ["1", "2"]}


def test_mutation_serializes_flat_keywords_wrapped_into_the_input() -> None:
    client, transport = make(scalars=ScalarRegistry([money_spec()]))
    client.mutation(
        "placeOrder", lines=[{"price": Decimal("2.25")}], total=Decimal("2.25")
    )
    assert transport.sent[-1].variables == {
        "input": {"lines": [{"price": "2.25"}], "total": "2.25"}
    }


def test_mutation_serializes_an_explicit_input_value() -> None:
    client, transport = make(scalars=ScalarRegistry([money_spec()]))
    client.mutation("placeOrder", input={"lines": [{"price": Decimal("3")}]})
    assert transport.sent[-1].variables == {"input": {"lines": [{"price": "3"}]}}


def test_the_variables_escape_hatch_is_serialized_too() -> None:
    client, transport = make(scalars=ScalarRegistry([money_spec()]))
    client.query("total", variables={"amount": Decimal("4")})
    assert transport.sent[-1].variables == {"amount": "4"}


def test_execute_serializes_declared_variables() -> None:
    client, transport = make(scalars=ScalarRegistry([money_spec()]))
    client.execute(
        "query Q($a: Money!, $m: [Money!]) { total(amount: $a, many: $m) }",
        {"a": Decimal("3"), "m": [Decimal("5")]},
    )
    assert transport.sent[-1].variables == {"a": "3", "m": ["5"]}


def test_execute_serializes_inside_an_input_object() -> None:
    client, transport = make(scalars=ScalarRegistry([money_spec()]))
    client.execute(
        "mutation M($i: OrderInput!) { placeOrder(input: $i) }",
        {"i": {"lines": [{"price": Decimal("7")}], "grid": [[Decimal("8")]]}},
    )
    assert transport.sent[-1].variables == {
        "i": {"lines": [{"price": "7"}], "grid": [["8"]]}
    }


@pytest.mark.parametrize("call", ["query", "mutation", "execute"])
def test_the_values_on_the_wire_are_json(call: str) -> None:
    client, transport = make(scalars=ScalarRegistry([money_spec()]))
    if call == "query":
        client.query("total", amount=Decimal("1.5"))
    elif call == "mutation":
        client.mutation("placeOrder", lines=[{"price": Decimal("1.5")}])
    else:
        client.execute(
            "query Q($a: Money!) { total(amount: $a) }", {"a": Decimal("1.5")}
        )
    assert json.loads(json.dumps(dict(transport.sent[-1].variables))) == dict(
        transport.sent[-1].variables
    )


@pytest.mark.parametrize("call", ["query", "mutation", "execute"])
def test_a_non_json_value_is_refused_before_anything_is_sent(call: str) -> None:
    client, transport = make()
    with pytest.raises(ArgumentError, match="ScalarSpec"):
        if call == "query":
            client.query("total", amount=Decimal("1.5"))
        elif call == "mutation":
            client.mutation("placeOrder", lines=[{"price": Decimal("1.5")}])
        else:
            client.execute(
                "query Q($a: Money!) { total(amount: $a) }", {"a": Decimal("1.5")}
            )
    assert transport.sent == []


def test_a_field_argument_of_an_explicit_selection_is_serialized() -> None:
    client, transport = make(scalars=ScalarRegistry([money_spec()]))
    client.query(
        "user",
        fields=[
            Field(
                "convert",
                args={
                    "amount": Decimal("6.5"),
                    "order": {"lines": [{"price": Decimal("1")}]},
                },
            )
        ],
    )
    (value,) = [
        v for v in transport.sent[-1].variables.values() if not isinstance(v, dict)
    ]
    assert value == "6.5"
    (order,) = [v for v in transport.sent[-1].variables.values() if isinstance(v, dict)]
    assert order == {"lines": [{"price": "1"}]}


def test_validate_false_does_not_skip_serialization() -> None:
    client, transport = make(scalars=ScalarRegistry([money_spec()]))
    client.query("total", amount=Decimal("1.5"), validate=False)
    assert transport.sent[-1].variables == {"amount": "1.5"}


def test_a_spec_registered_after_the_client_exists_is_used() -> None:
    client, transport = make()
    client.scalars.register(money_spec())
    client.query("total", amount=Decimal("9"))
    assert transport.sent[-1].variables == {"amount": "9"}


# -- the regression: a custom fake that returns a Decimal -------------------


def test_a_decimal_from_a_custom_fake_reaches_the_wire_as_json() -> None:
    client, transport = make(scalars=ScalarRegistry([money_spec()]))
    payload = client.fake.PricedInput()
    assert isinstance(payload["price"], Decimal)

    client.mutation("pricedUser", input=payload)
    flat = dict(payload)
    client.mutation("pricedUser", **flat)

    for request in transport.sent:
        assert request.variables == {
            "input": {"name": payload["name"], "price": str(payload["price"])}
        }
        assert isinstance(request.variables["input"]["price"], str)
        json.dumps(dict(request.variables))


def test_the_factory_itself_does_not_serialize() -> None:
    client, _ = make(scalars=ScalarRegistry([money_spec()]))
    assert isinstance(client.fake.PricedInput()["price"], Decimal)


# -- decoding reads the same registry ---------------------------------------


def test_a_parser_registered_after_the_client_exists_decodes_the_response() -> None:
    transport = PriceTransport("12.50")
    client = GraphQLClient(
        transport=transport, schema=SCHEMA, config=ClientConfig(url=URL)
    )
    assert client.query("price") == "12.50"
    client.scalars.register(money_spec())
    assert client.query("price") == Decimal("12.50")


def test_a_spec_without_parse_leaves_the_raw_value() -> None:
    transport = PriceTransport("12.50")
    client = GraphQLClient(
        transport=transport,
        schema=SCHEMA,
        config=ClientConfig(url=URL),
        scalars=ScalarRegistry([money_spec(parse=None)]),
    )
    assert client.query("price") == "12.50"


# -- the registry and the constructor ---------------------------------------


def test_the_client_exposes_the_registry_it_was_given() -> None:
    scalars = ScalarRegistry()
    client, _ = make(scalars=scalars)
    assert client.scalars is scalars


def test_a_client_without_a_registry_gets_an_empty_one() -> None:
    client, _ = make()
    assert isinstance(client.scalars, ScalarRegistry)
    assert len(client.scalars) == 0


def test_a_clone_shares_the_registry() -> None:
    client, _ = make()
    clones = [
        client.with_headers(a="b"),
        client.with_auth(_bearer()),
        client.as_("token-value-0123"),
        client.anonymous(),
    ]
    assert all(clone.scalars is client.scalars for clone in clones)


def _bearer() -> Any:
    from pytest_graphql import BearerAuth

    return BearerAuth("token-value-0123")


def test_a_scalar_registered_on_the_parent_after_cloning_reaches_the_clone() -> None:
    client, _ = make()
    clone = client.with_headers(a="b")
    client.scalars.register(money_spec())
    assert "Money" in clone.scalars
    assert clone.fake.PricedInput()["price"] is not None


def test_the_old_parsers_keyword_is_gone() -> None:
    with pytest.raises(TypeError, match="parsers"):
        GraphQLClient(
            transport=FakeGraphQLTransport(SCHEMA),
            schema=SCHEMA,
            parsers={},  # type: ignore[call-arg]
        )


def test_the_registry_argument_is_checked() -> None:
    with pytest.raises(TypeError, match="scalars"):
        make(scalars={"Money": money_spec()})  # type: ignore[arg-type]


def test_the_fake_context_argument_is_checked() -> None:
    with pytest.raises(TypeError, match="fake_context"):
        make(fake_context="node")  # type: ignore[arg-type]


# -- gql.fake ---------------------------------------------------------------


def test_fake_uses_the_config_seed_and_the_context() -> None:
    source = UniqueSource("run-1", "gw2")
    context = FakeContext("tests/test_x.py::test_a", source)
    client, _ = make(seed=7, fake_context=context)
    expected = FakeNamespace(
        SCHEMA,
        None,
        global_seed=7,
        node_id="tests/test_x.py::test_a",
        unique_source=source,
    ).CreateUserInput()
    assert client.fake.CreateUserInput() == expected


def test_fake_changes_with_the_config_seed() -> None:
    context = FakeContext("n", UniqueSource("r"))
    first, _ = make(seed=1, fake_context=context)
    second, _ = make(seed=2, fake_context=context)
    assert first.fake.CreateUserInput() != second.fake.CreateUserInput()


def test_fake_is_cached_per_client() -> None:
    client, _ = make()
    assert client.fake is client.fake


def test_two_standalone_clients_make_the_same_seeded_data() -> None:
    first, _ = make()
    second, _ = make()
    assert first.fake.CreateUserInput() == second.fake.CreateUserInput()


def test_two_standalone_clients_make_different_unique_values() -> None:
    first, _ = make()
    second, _ = make()
    assert (
        first.fake.CreateUserInput(name=unique())["name"]
        != second.fake.CreateUserInput(name=unique())["name"]
    )


def test_a_clone_shares_the_unique_source_so_values_never_repeat() -> None:
    source = UniqueSource("run-1")
    client, _ = make(fake_context=FakeContext("n", source))
    clone = client.with_headers(a="b")
    other = client.as_("token-value-0123")
    values = [
        who.fake.CreateUserInput(name=unique())["name"]
        for who in (client, clone, other, client, clone, other)
    ]
    assert len(set(values)) == 6
    assert source.issued == 6


def test_a_clone_makes_the_same_seeded_data_as_its_parent() -> None:
    client, _ = make(fake_context=FakeContext("n", UniqueSource("r")), seed=3)
    assert (
        client.fake.CreateUserInput()
        == client.with_headers(a="b").fake.CreateUserInput()
    )


# -- build_client -----------------------------------------------------------


def test_build_client_passes_the_registry_and_the_context_through() -> None:
    scalars = ScalarRegistry([money_spec()])
    source = UniqueSource("run-1")
    client = build_client(
        url=URL,
        transport=FakeGraphQLTransport(SCHEMA),
        schema=SCHEMA,
        scalars=scalars,
        fake_context=FakeContext("n", source),
        seed=5,
    )
    assert client.scalars is scalars
    assert client.config.seed == 5
    client.fake.PricedInput(name=unique())
    assert source.issued == 1


def test_build_client_refuses_the_old_parsers_keyword() -> None:
    with pytest.raises(ArgumentError, match="parsers"):
        build_client(
            url=URL,
            transport=FakeGraphQLTransport(SCHEMA),
            schema=SCHEMA,
            parsers={},
        )
