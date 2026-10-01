"""The C2/C16 adversarial leak suite, end to end.

``docs/reference/DESIGN_DECISIONS.md`` section 7, "Coverage": a known
credential is planted in a header, a top-level variable, a nested input
object, a list element, response data and a server error message, and it
must appear in no output path. The unit tests in ``test_diagnostics.py``
prove each scrub rule on a ``RequestInfo``. This file proves the rules hold
on the live path, through a real ``HttpxTransport`` against a loopback
server that reflects everything it received, so a credential the transport
adds below the request's own redaction boundary is caught too.

The output paths are every text the library renders for a call: the raised
exception and every exception reachable from it, the snapshot each carries,
a body excerpt, the error objects of a rejected request, the response and
its error entries, the recorder dump, ``repr`` of the live request and
``as_curl()``. Log records and pytest report sections join this list when
the plugin that produces them exists; nothing in the core writes either.
"""

from __future__ import annotations

import base64
import json
import traceback
from collections.abc import Callable, Iterator, Mapping
from typing import Any, cast

import pytest
from graphql import GraphQLSchema
from graphql import build_schema as build_graphql_schema

from pytest_graphql import BaseMiddleware, GraphQLClient, RequestInfo, build_client
from pytest_graphql._core.client import reported_errors
from pytest_graphql._core.diagnostics import WITHHELD_TEXT
from pytest_graphql._core.errors import (
    DiagnosticRenderError,
    GraphQLExecutionError,
    GraphQLHTTPStatusError,
    GraphQLPartialDataError,
    GraphQLRequestError,
    GraphQLTransportError,
)
from pytest_graphql._core.response import GraphQLResponse
from tests.unit.local_http_server import PlannedResponse, local_server

_SDL = """
input Credentials { password: String!, secret: [String!]! }
input LoginInput { user: String!, credentials: Credentials! }
type Session { id: ID!, token: String }
type Query { ping: String }
type Mutation { login(input: LoginInput!, token: String!): Session }
"""

#: One credential per planted position. Each is long enough to be
#: scrubbable and is spelled so no two share a prefix.
_HEADER = "hdr-bearer-0123456789abcdef"
_API_KEY = "apikey-value-0123456789"
_TOP_LEVEL = "top-level-token-0123456789"
_NESTED = "nested-password-0123456789"
_LIST_FIRST = "list-element-first-0123456789"
_LIST_SECOND = "list-element-second-0123456789"
_QUERY = "query-access-0123456789"
_PLANTED = (_HEADER, _API_KEY, _TOP_LEVEL, _NESTED, _LIST_FIRST, _LIST_SECOND, _QUERY)

_GRAPHQL_JSON = "application/graphql-response+json"


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_graphql_schema(_SDL)


class _Capture(BaseMiddleware):
    """Keeps the live request, so ``repr`` and ``as_curl()`` can be rendered."""

    def __init__(self) -> None:
        self.live: list[RequestInfo] = []

    def before_request(self, request: RequestInfo) -> RequestInfo | None:
        self.live.append(request)
        return None


def _login(
    client: GraphQLClient, *, top_level: str = _TOP_LEVEL
) -> GraphQLResponse[Any]:
    response: GraphQLResponse[Any] = client.mutation(
        "login",
        input={
            "user": "someone",
            "credentials": {
                "password": _NESTED,
                "secret": [_LIST_FIRST, _LIST_SECOND],
            },
        },
        token=top_level,
        fields=["id", "token"],
        raw=True,
    )
    return response


def _reachable(exc: BaseException) -> Iterator[BaseException]:
    """Every exception a caller can reach from ``exc``, each once."""
    seen: set[int] = set()
    pending = [exc, *reported_errors(exc)]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending += [
            linked
            for linked in (current.__cause__, current.__context__)
            if linked is not None
        ]


#: What the harness records when a renderer refuses to render.
_REFUSED = "[render refused]"


def _exception_texts(error: BaseException) -> list[str]:
    """Every standard text an exception shows: what logging with ``%s`` or
    ``%r``, printing ``args`` and a Python traceback each print."""
    return [
        str(error),
        repr(error),
        repr(error.args),
        "".join(traceback.format_exception_only(error)),
    ]


def _shown(render: Callable[..., str], *args: object) -> str:
    """One rendering, or :data:`_REFUSED` when the renderer failed closed.

    A refusal is itself a public rendering, so every text it shows is kept
    after the marker and checked like any other output.
    """
    try:
        return render(*args)
    except DiagnosticRenderError as refusal:
        return "\n".join([_REFUSED, *_exception_texts(refusal)])


def _call_and_render(
    client: GraphQLClient,
    capture: _Capture,
    call: Callable[[GraphQLClient], GraphQLResponse[Any]] = _login,
) -> tuple[object, dict[str, str]]:
    """Make one call; return its outcome and every text rendered for it.

    The texts are grouped by output class, so a test can require the
    server's reflection to have reached a class before it asserts that no
    credential did: a class that never received the reflection proves
    nothing about the scrub.
    """
    outputs: dict[str, list[str]] = {
        "exception": [],
        "excerpt": [],
        "request_errors": [],
        "response": [],
        "response_errors": [],
        "recorder": [],
        "live_request": [],
    }
    outcome: object
    try:
        outcome = call(client)
    except Exception as exc:  # every failure is rendered
        outcome = exc
        for each in _reachable(exc):
            outputs["exception"] += _exception_texts(each)
            if hasattr(each, "request"):
                outputs["exception"].append(_shown(repr, each.request))
            if hasattr(each, "body_excerpt"):
                outputs["excerpt"] += [each.body_excerpt, repr(each.body_excerpt)]
            if isinstance(each, GraphQLRequestError):
                outputs["request_errors"].append(repr(each.errors))
    else:
        response = cast("GraphQLResponse[Any]", outcome)
        outputs["response"] += [
            _shown(repr, response),
            _shown(str, response),
            repr(response.http),
            repr(response.data),
        ]
        outputs["response_errors"].append(repr(response.errors))
    outputs["recorder"].append(_shown(client.recorder.dump))
    outputs["recorder"] += [
        _shown(repr, recorded) for recorded in client.recorder.calls
    ]
    for live in capture.live:
        outputs["live_request"] += [
            _shown(repr, live),
            _shown(str, live),
            _shown(repr, live.redacted()),
            _shown(live.as_curl),
        ]
    return outcome, {name: "\n".join(texts) for name, texts in outputs.items()}


def _reflection(body: bytes, headers: Mapping[str, str]) -> str:
    return f"REFLECTED body={body.decode()} headers={json.dumps(dict(headers))}"


class _Reflecting:
    """A server that quotes the whole request back in the given shape.

    ``hits`` counts the requests it answered, so a test can prove the call
    reached it rather than failing before any reflection existed.
    """

    def __init__(self, shape: str) -> None:
        self.shape = shape
        self.hits = 0

    def __call__(self, body: bytes, headers: dict[str, str]) -> PlannedResponse:
        self.hits += 1
        text = _reflection(body, headers)
        error = {
            "message": text,
            "path": ["login", text],
            "extensions": {"code": "ECHO", "echo": text},
        }
        if self.shape in ("text", "text-ok"):
            status = 502 if self.shape == "text" else 200
            return PlannedResponse(
                status, (("Content-Type", "text/plain"),), text.encode()
            )
        envelope: dict[str, Any] = {
            "rejected": {"errors": [error]},
            "execution": {"data": None, "errors": [error]},
            "partial": {
                "data": {"login": {"id": "1", "token": _TOP_LEVEL}},
                "errors": [error],
            },
            "data": {"data": {"login": {"id": "1", "token": _TOP_LEVEL}}},
        }[self.shape]
        status = 400 if self.shape == "rejected" else 200
        return PlannedResponse(
            status, (("Content-Type", _GRAPHQL_JSON),), json.dumps(envelope).encode()
        )


def _client(
    url: str, schema: GraphQLSchema, capture: _Capture, **config: Any
) -> GraphQLClient:
    options: dict[str, Any] = {
        "headers": {"Authorization": f"Bearer {_HEADER}", "X-API-Key": _API_KEY},
        **config,
    }
    return build_client(
        url=f"{url}?access_token={_QUERY}",
        schema=schema,
        middleware=[capture],
        **options,
    )


def _expect(outcome: object, expected: type, errors: int | None) -> None:
    """The call ended the way its shape and raise policy say it must."""
    assert type(outcome) is expected or (
        expected is GraphQLResponse and isinstance(outcome, GraphQLResponse)
    ), f"expected {expected.__name__}, got {type(outcome).__name__}"
    if errors is not None:
        assert len(cast("GraphQLResponse[Any]", outcome).errors) == errors


#: shape, raise policy, the outcome it must produce, the error count of a
#: returned response, and the output classes the reflection must reach.
_MATRIX = [
    ("text", {}, GraphQLHTTPStatusError, None, ("exception", "excerpt", "recorder")),
    ("text-ok", {}, GraphQLTransportError, None, ("exception", "excerpt", "recorder")),
    ("rejected", {}, GraphQLRequestError, None, ("request_errors",)),
    ("execution", {}, GraphQLExecutionError, None, ()),
    ("partial", {}, GraphQLPartialDataError, None, ()),
    ("partial", {"raise_on_partial": False}, GraphQLResponse, 1, ("response_errors",)),
    ("execution", {"raise_on_error": False}, GraphQLResponse, 1, ("response_errors",)),
    ("data", {}, GraphQLResponse, 0, ()),
]


@pytest.mark.parametrize(("shape", "config", "expected", "errors", "reached"), _MATRIX)
def test_a_reflected_credential_reaches_no_output_path(
    schema: GraphQLSchema,
    shape: str,
    config: dict[str, Any],
    expected: type,
    errors: int | None,
    reached: tuple[str, ...],
) -> None:
    server = _Reflecting(shape)
    capture = _Capture()
    with local_server(server) as url:
        client = _client(url, schema, capture, **config)
        try:
            outcome, outputs = _call_and_render(client, capture)
        finally:
            client.close()

    assert server.hits == 1
    _expect(outcome, expected, errors)
    for name in reached:
        assert "REFLECTED body=" in outputs[name], name
        assert "[redacted:" in outputs[name], name
    rendered = "\n".join(outputs.values())
    assert not [secret for secret in _PLANTED if secret in rendered]


def test_a_scrubbed_list_keeps_its_json_structure_in_an_excerpt(
    schema: GraphQLSchema,
) -> None:
    capture = _Capture()
    with local_server(_Reflecting("text")) as url:
        client = _client(url, schema, capture)
        try:
            _, outputs = _call_and_render(client, capture)
        finally:
            client.close()

    marker = "[redacted:input.credentials.secret]"
    assert f'"secret":["{marker}","{marker}"]' in outputs["excerpt"]


_URL_USER = "url-user-0123456789"
_URL_PASSWORD = "url-password-0123456789"
_URL_PAIR = f"{_URL_USER}:{_URL_PASSWORD}"
_URL_FORMS = (
    _URL_USER,
    _URL_PASSWORD,
    _URL_PAIR,
    base64.b64encode(_URL_PAIR.encode()).decode(),
)


@pytest.mark.parametrize(
    ("shape", "config", "expected", "errors", "reached"),
    [
        ("text", {}, GraphQLHTTPStatusError, None, ("exception", "excerpt")),
        ("rejected", {}, GraphQLRequestError, None, ("request_errors",)),
        ("execution", {}, GraphQLExecutionError, None, ()),
        (
            "execution",
            {"raise_on_error": False},
            GraphQLResponse,
            1,
            ("response_errors",),
        ),
    ],
)
def test_a_url_userinfo_credential_reaches_no_output_path(
    schema: GraphQLSchema,
    shape: str,
    config: dict[str, Any],
    expected: type,
    errors: int | None,
    reached: tuple[str, ...],
) -> None:
    # httpx turns userinfo into ``Authorization: Basic <base64>``, which no
    # form of the URL spells. The reflecting server quotes that header into
    # a body, a message, an extension and an error path.
    server = _Reflecting(shape)
    capture = _Capture()
    with local_server(server) as url:
        target = url.replace("http://", f"http://{_URL_USER}:{_URL_PASSWORD}@", 1)
        client = build_client(url=target, schema=schema, middleware=[capture], **config)
        try:
            outcome, outputs = _call_and_render(client, capture)
        finally:
            client.close()

    assert server.hits == 1
    _expect(outcome, expected, errors)
    for name in reached:
        assert '"authorization": "[redacted:authorization]"' in outputs[name], name
    rendered = "\n".join(outputs.values())
    assert not [form for form in _URL_FORMS if form in rendered]


_PROXY_USER = "proxy-user-0123456789"
_PROXY_PASSWORD = "proxy-password-0123456789"


def test_a_proxy_credential_reflected_into_an_error_path_is_scrubbed(
    schema: GraphQLSchema,
) -> None:
    # The proxy answers with a GraphQL envelope, so its reflection reaches
    # the response's error entries rather than an excerpt.
    pair = f"{_PROXY_USER}:{_PROXY_PASSWORD}"
    forms = (
        _PROXY_USER,
        _PROXY_PASSWORD,
        pair,
        base64.b64encode(pair.encode()).decode(),
    )
    server = _Reflecting("execution")
    capture = _Capture()
    with local_server(server) as proxy_url:
        proxy = proxy_url.replace(
            "http://", f"http://{_PROXY_USER}:{_PROXY_PASSWORD}@", 1
        )
        client = build_client(
            url="http://target.invalid/graphql",
            schema=schema,
            middleware=[capture],
            proxy=proxy,
            raise_on_error=False,
        )
        try:
            outcome, outputs = _call_and_render(client, capture)
        finally:
            client.close()

    assert server.hits == 1
    _expect(outcome, GraphQLResponse, 1)
    assert '"proxy-authorization": "[redacted:' in outputs["response_errors"]
    rendered = "\n".join(outputs.values())
    assert not [form for form in forms if form in rendered]


def test_a_credential_cut_by_excerpt_truncation_leaves_no_prefix(
    schema: GraphQLSchema,
) -> None:
    # The planted value straddles the excerpt bound in the raw body, so
    # truncating before the scrub would keep its first half.
    limit = 256
    padding = "p" * (limit - len(_HEADER) // 2)

    def respond(_body: bytes, headers: dict[str, str]) -> PlannedResponse:
        _, _, token = headers["authorization"].partition(" ")
        text = padding + token + "q" * limit
        return PlannedResponse(502, (("Content-Type", "text/plain"),), text.encode())

    capture = _Capture()
    with local_server(respond) as url:
        client = _client(url, schema, capture, max_diagnostic_bytes=limit)
        try:
            _, outputs = _call_and_render(client, capture)
        finally:
            client.close()

    rendered = "\n".join(outputs.values())
    assert "(truncated," in rendered
    assert _HEADER[:8] not in rendered


def test_a_secret_inside_a_longer_secret_is_replaced_whole(
    schema: GraphQLSchema,
) -> None:
    inner = "substring-secret-0123456789"
    outer = f"{inner}-extended-tail"

    def respond(_body: bytes, headers: dict[str, str]) -> PlannedResponse:
        text = f"outer={headers['authorization']} inner={inner}"
        return PlannedResponse(502, (("Content-Type", "text/plain"),), text.encode())

    capture = _Capture()
    with local_server(respond) as url:
        client = _client(
            url, schema, capture, headers={"Authorization": f"Bearer {outer}"}
        )
        try:
            _, outputs = _call_and_render(
                client, capture, lambda each: _login(each, top_level=inner)
            )
        finally:
            client.close()

    rendered = "\n".join(outputs.values())
    assert inner not in rendered
    assert "-extended-tail" not in rendered


def test_a_short_credential_is_redacted_at_its_path_but_not_scrubbed(
    schema: GraphQLSchema,
) -> None:
    # The documented limitation: below ``min_redacted_value_length`` a
    # value is still redacted where it was set, but free-form text keeps
    # it, because replacing a short string everywhere corrupts messages.
    short = "short7x"

    def respond(_body: bytes, headers: dict[str, str]) -> PlannedResponse:
        text = f"key was {headers['x-api-key']}"
        return PlannedResponse(502, (("Content-Type", "text/plain"),), text.encode())

    capture = _Capture()
    with local_server(respond) as url:
        client = _client(url, schema, capture, headers={"X-API-Key": short})
        try:
            with pytest.raises(Exception) as caught:
                _login(client)
        finally:
            client.close()

    snapshot = capture.live[0].redacted()
    assert short not in repr(snapshot)
    assert snapshot.headers["X-API-Key"] != short
    assert f"key was {short}" in getattr(caught.value, "body_excerpt", "")


def test_a_credential_split_by_a_zero_width_character_is_still_scrubbed(
    schema: GraphQLSchema,
) -> None:
    half = len(_HEADER) // 2
    split = f"{_HEADER[:half]}\N{ZERO WIDTH SPACE}{_HEADER[half:]}"

    def respond(_body: bytes, _headers: dict[str, str]) -> PlannedResponse:
        text = f"token was {split}"
        return PlannedResponse(502, (("Content-Type", "text/plain"),), text.encode())

    capture = _Capture()
    with local_server(respond) as url:
        client = _client(url, schema, capture)
        try:
            _, outputs = _call_and_render(client, capture)
        finally:
            client.close()

    rendered = "\n".join(outputs.values())
    assert "token was [redacted:Authorization]" in rendered
    assert _HEADER[:half] not in rendered
    assert _HEADER[half:] not in rendered


#: Two literal backslashes; the server echoes the text with one. Python's
#: ``repr`` doubles a backslash, so a clean echo would spell the secret.
_DOUBLED = "doubled\\\\backslash-0123"
_SINGLE = "doubled\\backslash-0123"


@pytest.mark.parametrize(
    ("shape", "config", "expected", "reached"),
    [
        ("text", {}, GraphQLHTTPStatusError, ("exception", "excerpt", "recorder")),
        ("rejected", {}, GraphQLRequestError, ("request_errors",)),
        ("execution", {"raise_on_error": False}, GraphQLResponse, ("response_errors",)),
    ],
)
def test_repr_cannot_recreate_a_credential_from_a_scrubbed_echo(
    schema: GraphQLSchema,
    shape: str,
    config: dict[str, Any],
    expected: type,
    reached: tuple[str, ...],
) -> None:
    def respond(_body: bytes, _headers: dict[str, str]) -> PlannedResponse:
        text = f"REFLECTED body= echo {_SINGLE} [redacted:none]"
        error = {"message": text, "path": ["login", text]}
        if shape == "text":
            return PlannedResponse(
                502, (("Content-Type", "text/plain"),), text.encode()
            )
        envelope: dict[str, Any] = {"errors": [error]}
        if shape == "execution":
            envelope["data"] = None
        status = 400 if shape == "rejected" else 200
        return PlannedResponse(
            status, (("Content-Type", _GRAPHQL_JSON),), json.dumps(envelope).encode()
        )

    capture = _Capture()
    with local_server(respond) as url:
        client = _client(
            url, schema, capture, headers={"X-API-Key": _DOUBLED}, **config
        )
        try:
            outcome, outputs = _call_and_render(client, capture)
        finally:
            client.close()

    _expect(outcome, expected, None)
    for name in reached:
        assert outputs[name], name
    rendered = "\n".join(outputs.values())
    assert _DOUBLED not in rendered


#: A credential spelled to equal text the renderer adds itself, so no single
#: field holds it: a class name, a field name with its separator, or a key
#: joined to its value. Each case names the output classes that must fail
#: closed, by refusing or by withholding the text.
_REFUSAL_REPR = "DiagnosticRenderError(repr()"
_REFUSAL_LINE = "DiagnosticRenderError: repr()"
_REFUSAL_ARGS = '("repr() could'

_COLLISIONS = [
    (
        "class name",
        {"Authorization": "Bearer DiagnosticSnapshot"},
        "DiagnosticSnapshot",
        ("exception", "recorder", "live_request"),
    ),
    (
        "response wrapper",
        {"X-API-Key": "GraphQLResponse(status="},
        "GraphQLResponse(status=",
        ("exception",),
    ),
    (
        "recorded call wrapper",
        {"X-API-Key": "RecordedCall(request="},
        "RecordedCall(request=",
        ("recorder",),
    ),
    (
        "key and separator",
        {"X-API-Key": "'user': 'someone'"},
        "'user': 'someone'",
        ("exception", "recorder", "live_request"),
    ),
    (
        "recorder line",
        {"X-API-Key": "1. mutation login ->"},
        "1. mutation login ->",
        ("recorder",),
    ),
    (
        "execution error traceback line",
        {"X-API-Key": "GraphQLExecutionError: the server returned"},
        "GraphQLExecutionError: the server returned",
        ("exception",),
    ),
    # Two secrets: the first makes a rendering refuse, the second equals
    # text the refusal itself shows around its message.
    (
        "refusal repr",
        {"Authorization": "Bearer DiagnosticSnapshot", "X-API-Key": _REFUSAL_REPR},
        _REFUSAL_REPR,
        ("exception", "recorder", "live_request"),
    ),
    (
        "refusal traceback line",
        {"Authorization": "Bearer DiagnosticSnapshot", "X-API-Key": _REFUSAL_LINE},
        _REFUSAL_LINE,
        ("exception", "recorder", "live_request"),
    ),
    (
        "refusal args repr",
        {"Authorization": "Bearer DiagnosticSnapshot", "X-API-Key": _REFUSAL_ARGS},
        _REFUSAL_ARGS,
        ("exception", "recorder", "live_request"),
    ),
]


@pytest.mark.parametrize(("case", "headers", "secret", "closed"), _COLLISIONS)
@pytest.mark.parametrize("shape", ["text", "execution"])
def test_a_credential_equal_to_rendered_syntax_reaches_no_output_path(
    schema: GraphQLSchema,
    case: str,
    headers: dict[str, str],
    secret: str,
    closed: tuple[str, ...],
    shape: str,
) -> None:
    server = _Reflecting(shape)
    capture = _Capture()
    with local_server(server) as url:
        client = _client(url, schema, capture, headers=headers)
        try:
            outcome, outputs = _call_and_render(client, capture)
        finally:
            client.close()

    assert server.hits == 1
    expected = GraphQLHTTPStatusError if shape == "text" else GraphQLExecutionError
    _expect(outcome, expected, None)
    for name in closed:
        if name == "exception" and shape == "text":
            continue  # a transport message embeds no snapshot representation
        assert _REFUSED in outputs[name] or WITHHELD_TEXT in outputs[name], (
            case,
            name,
        )
    rendered = "\n".join(outputs.values())
    assert secret not in rendered, case


def test_a_returned_response_repr_refuses_a_secret_equal_to_its_wrapper(
    schema: GraphQLSchema,
) -> None:
    # Nothing raises here, so only the response's own check stands between
    # its fixed text and the caller.
    secret = "GraphQLResponse(status="
    server = _Reflecting("execution")
    capture = _Capture()
    with local_server(server) as url:
        client = _client(
            url, schema, capture, headers={"X-API-Key": secret}, raise_on_error=False
        )
        try:
            outcome, outputs = _call_and_render(client, capture)
        finally:
            client.close()

    _expect(outcome, GraphQLResponse, 1)
    with pytest.raises(DiagnosticRenderError):
        repr(outcome)
    assert _REFUSED in outputs["response"]
    assert secret not in "\n".join(outputs.values())
