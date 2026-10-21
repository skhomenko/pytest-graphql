"""File uploads (DESIGN_DECISIONS.md section 13.2).

The properties these tests pin:

- An ``UploadFile`` is accepted only at a variable position whose named type is
  an upload scalar, at every depth the serializer reaches. Everywhere else it is
  refused before anything is sent, with the path and the accepted scalars.
- The wire is the GraphQL multipart request: ``operations``, ``map``, then one
  part per distinct file, numbered in depth-first order.
- The content never reaches a rendered view: a snapshot, a ``repr()``, an
  exception, ``as_curl()``, the recorder dump, a log record, the failure
  section or the JSON report.
- A filename cannot break the multipart header or the ``as_curl()`` line.

Every server here is project-authored: the hostile test schema over the
in-memory transport or the loopback HTTP server, and the in-process ASGI app.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import string
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from graphql import GraphQLSchema, build_schema, graphql_sync

from pytest_graphql import (
    ArgumentError,
    ClientConfig,
    GraphQLConnectionError,
    GraphQLExecutionError,
    RawResponse,
    RequestInfo,
    ScalarRegistry,
    ScalarSpec,
    UploadFile,
    build_client,
)
from pytest_graphql._core.client import GraphQLClient
from pytest_graphql._core.serialization import serialize_variable
from pytest_graphql._core.transport.httpx_transport import HttpxTransport
from pytest_graphql._core.upload import DEFAULT_MAX_UPLOAD_BYTES, UploadRules
from tests.schema.fake_transport import FakeGraphQLTransport
from tests.schema.multipart import Part, decode_filename, graphql_payload, parse_parts
from tests.schema.resolvers import build_schema as build_hostile_schema
from tests.unit.in_process_apps import SDL as APP_SDL
from tests.unit.in_process_apps import AsgiApp
from tests.unit.local_http_server import PlannedResponse, local_server

#: A distinctive content, searched for on every output surface.
CONTENT = b"ZQXV-upload-content-7f3a9c"
CONTENT_TEXT = CONTENT.decode("ascii")

MEDIA_TYPE = "application/graphql-response+json"


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_hostile_schema()


@pytest.fixture
def transport(schema: GraphQLSchema) -> FakeGraphQLTransport:
    return FakeGraphQLTransport(schema)


def client_over(schema: GraphQLSchema, transport: Any, **config: Any) -> GraphQLClient:
    return GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(url="http://127.0.0.1:9/graphql", **config),
    )


@pytest.fixture
def client(schema: GraphQLSchema, transport: FakeGraphQLTransport) -> GraphQLClient:
    return client_over(schema, transport)


Received = list[tuple[bytes, dict[str, str]]]


def graphql_responder(
    schema: GraphQLSchema, received: Received
) -> Callable[[bytes, dict[str, str]], PlannedResponse]:
    """A loopback GraphQL server over ``schema`` that reads multipart bodies."""

    def respond(body: bytes, headers: dict[str, str]) -> PlannedResponse:
        received.append((body, headers))
        payload = graphql_payload(body, headers.get("content-type", ""))
        result = graphql_sync(
            schema,
            payload["query"],
            variable_values=payload.get("variables"),
            operation_name=payload.get("operationName"),
        )
        envelope: dict[str, Any] = {"data": result.data}
        if result.errors:
            envelope["errors"] = [error.formatted for error in result.errors]
        return PlannedResponse(
            200, (("Content-Type", MEDIA_TYPE),), json.dumps(envelope).encode()
        )

    return respond


@pytest.fixture
def http(schema: GraphQLSchema) -> Iterator[tuple[GraphQLClient, Received]]:
    """A client over ``HttpxTransport`` and a loopback server, and what it got."""
    received: Received = []
    with local_server(graphql_responder(schema, received)) as url:
        transport = HttpxTransport()
        try:
            yield (
                build_client(url=url, transport=transport, schema=schema),
                received,
            )
        finally:
            transport.close()


def parts_of(received: Received) -> list[Part]:
    body, headers = received[-1]
    return parse_parts(body, headers["content-type"])


# -- the value ----------------------------------------------------------------


def test_repr_shows_the_name_size_and_type_and_never_the_content() -> None:
    file = UploadFile(CONTENT, filename="a.bin")
    assert repr(file) == (
        f"UploadFile(filename='a.bin', size={len(CONTENT)}, "
        "content_type='application/octet-stream')"
    )
    assert CONTENT_TEXT not in repr(file) and CONTENT_TEXT not in str(file)


def test_from_path_reads_once_guesses_the_type_and_keeps_the_basename(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "fixtures" / "img"
    folder.mkdir(parents=True)
    path = folder / "avatar.png"
    path.write_bytes(CONTENT)
    file = UploadFile.from_path(path)
    path.write_bytes(b"changed after the call")
    assert file.filename == "avatar.png"
    assert file.content == CONTENT
    assert file.content_type == "image/png"
    assert UploadFile.from_path(str(path), content_type="text/plain").content_type == (
        "text/plain"
    )
    (folder / "data.zzunknown").write_bytes(b"x")
    assert UploadFile.from_path(folder / "data.zzunknown").content_type == (
        "application/octet-stream"
    )


HOSTILE_TYPES = [
    pytest.param("text/plain\rX-Evil: SECRET-TYPE-VALUE", "a control", id="CR"),
    pytest.param("text/plain\nX-Evil: SECRET-TYPE-VALUE", "a control", id="LF"),
    pytest.param("text/plain\x00SECRET-TYPE-VALUE", "a control", id="NUL"),
    pytest.param("text/plain\x01SECRET-TYPE-VALUE", "a control", id="SOH"),
    pytest.param("text/plain\tSECRET-TYPE-VALUE", "a control", id="tab"),
    pytest.param("text/plain\x7fSECRET-TYPE-VALUE", "a control", id="DEL"),
    pytest.param("text/plain\x85SECRET-TYPE-VALUE", "outside ASCII", id="C1"),
    pytest.param("text/pläin SECRET-TYPE-VALUE", "outside ASCII", id="latin"),
    pytest.param("text/plain\u2028SECRET-TYPE-VALUE", "outside ASCII", id="U+2028"),
]


def _refused(build: Callable[[], object], found: str) -> None:
    with pytest.raises(ArgumentError) as caught:
        build()
    message = str(caught.value)
    assert message.startswith("UploadFile.content_type holds ")
    assert found in message.splitlines()[0]
    assert "  Fix: pass a media type made of printable ASCII characters" in message
    for text in (message, repr(caught.value), repr(caught.value.args)):
        assert "SECRET-TYPE-VALUE" not in text
        assert "X-Evil" not in text
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(("content_type", "found"), HOSTILE_TYPES)
def test_a_content_type_with_a_control_or_non_ascii_character_is_refused(
    content_type: str, found: str, tmp_path: Path
) -> None:
    _refused(lambda: UploadFile(b"x", filename="a", content_type=content_type), found)
    valid = UploadFile(b"x", filename="a", content_type="text/plain")
    _refused(lambda: dataclasses.replace(valid, content_type=content_type), found)
    path = tmp_path / "a.txt"
    path.write_bytes(b"x")
    _refused(lambda: UploadFile.from_path(path, content_type=content_type), found)


# -- where a file is accepted -------------------------------------------------


def test_a_file_at_an_upload_argument_is_sent_as_the_same_object(
    client: GraphQLClient, transport: FakeGraphQLTransport
) -> None:
    file = UploadFile(CONTENT, filename="a.txt", content_type="text/plain")
    receipt = client.mutation("uploadAvatar", file=file)
    assert receipt.filename == "a.txt"
    assert receipt.size == len(CONTENT)
    assert receipt.content_type == "text/plain"
    assert transport.sent[-1].variables["file"] is file


def test_a_file_in_a_nested_list_and_a_single_file_for_a_list_are_accepted(
    client: GraphQLClient, transport: FakeGraphQLTransport
) -> None:
    one, two = UploadFile(b"1", filename="1.txt"), UploadFile(b"22", filename="2.txt")
    receipts = client.mutation(
        "uploadAttachments", input={"note": "n", "files": [one, two]}
    )
    assert [receipt.filename for receipt in receipts] == ["1.txt", "2.txt"]
    single = client.mutation("uploadAttachments", input={"files": one})
    assert [receipt.size for receipt in single] == [1]
    assert transport.sent[-1].variables["input"]["files"] == [one]


@pytest.mark.parametrize(
    ("operation", "variables", "where", "found"),
    [
        ("updateUser", {"id": "FILE"}, "$id", "ID"),
        ("updateUser", {"id": "u1", "name": "FILE"}, "$name", "String"),
        ("createPost", {"input": "FILE"}, "$input", "CreatePostInput"),
        (
            "createPost",
            {"input": {"title": "FILE", "authorId": "u1"}},
            "$input.title",
            "String",
        ),
        ("pyKeywordEcho", {"value": "FILE"}, "$value", "PyKeyword"),
    ],
)
def test_a_file_anywhere_else_is_refused_with_the_path_and_the_scalars(
    client: GraphQLClient,
    transport: FakeGraphQLTransport,
    operation: str,
    variables: dict[str, Any],
    where: str,
    found: str,
) -> None:
    file = UploadFile(CONTENT, filename="secret-name.txt")

    def place(value: Any) -> Any:
        if value == "FILE":
            return file
        if isinstance(value, dict):
            return {key: place(item) for key, item in value.items()}
        return value

    call = client.query if operation == "pyKeywordEcho" else client.mutation
    with pytest.raises(ArgumentError) as caught:
        call(operation, variables=place(variables))
    message = str(caught.value)
    assert f"{where} holds an UploadFile, but the position of type '{found}'" in message
    assert "A file is accepted only where the type is 'Upload'." in message
    assert (
        "Fix: pass the file to an argument or input field of type 'Upload'" in message
    )
    assert CONTENT_TEXT not in message and "secret-name" not in message
    assert transport.sent == []


def test_a_file_inside_the_json_value_of_another_scalar_is_refused() -> None:
    schema = build_schema("scalar JSON scalar Upload type Query { a(v: JSON): Int }")
    rules = UploadRules.for_schema(schema, ("Upload",), None)
    argument = schema.query_type.fields["a"].args["v"]  # type: ignore[union-attr]
    with pytest.raises(ValueError, match=r"\$v\.k\[0\] holds an UploadFile"):
        serialize_variable(
            {"k": [UploadFile(b"x", filename="a")]}, argument.type, None, "v", rules
        )


def test_a_schema_without_an_upload_scalar_refuses_every_file() -> None:
    schema = build_schema(
        "scalar File type Query { a: Int } type Mutation { put(file: File!): Int }"
    )
    schema.mutation_type.fields["put"].resolve = (  # type: ignore[union-attr]
        lambda _root, _info, file: len(file.content)
    )
    transport = FakeGraphQLTransport(schema)
    file = UploadFile(b"abc", filename="a")
    with pytest.raises(ArgumentError) as caught:
        client_over(schema, transport).mutation("put", file=file)
    message = str(caught.value)
    assert "$file holds an UploadFile, but the schema has no upload scalar." in message
    assert "upload_scalars names 'Upload'" in message
    assert "ClientConfig.upload_scalars" in message
    assert transport.sent == []
    configured = client_over(schema, transport, upload_scalars=("File",))
    assert configured.mutation("put", file=file) == 3


def test_a_registered_serialize_is_not_called_on_a_file(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    def refuse(_value: Any) -> Any:
        raise AssertionError("serialize ran on a file")

    client = GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(url="http://127.0.0.1:9/graphql"),
        scalars=ScalarRegistry(
            [ScalarSpec(name="Upload", serialize=refuse, fake=lambda _rng: None)]
        ),
    )
    file = UploadFile(b"abc", filename="a")
    assert client.mutation("uploadAvatar", file=file).size == 3
    assert (
        client.execute(
            "mutation($f: Upload!) { uploadAvatar(file: $f) { size } }", {"f": file}
        ).data.upload_avatar.size
        == 3
    )


def test_execute_refuses_a_misplaced_file_too(
    client: GraphQLClient, transport: FakeGraphQLTransport
) -> None:
    with pytest.raises(ArgumentError, match=r"\$id holds an UploadFile"):
        client.execute(
            "query($id: ID!) { user(id: $id) { id } }",
            {"id": UploadFile(b"x", filename="a")},
        )
    assert transport.sent == []


# -- the size limit -----------------------------------------------------------


def test_the_default_limit_is_32_mib() -> None:
    assert ClientConfig().max_upload_bytes == DEFAULT_MAX_UPLOAD_BYTES == 32 * 2**20


def test_a_request_above_the_limit_is_refused_before_any_io(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    client = client_over(schema, transport, max_upload_bytes=10)
    with pytest.raises(ArgumentError) as caught:
        client.mutation("uploadAvatar", file=UploadFile(b"x" * 11, filename="a"))
    message = str(caught.value)
    assert "hold 11 bytes, more than max_upload_bytes=10." in message
    assert "Fix: send smaller files, or raise ClientConfig.max_upload_bytes" in message
    assert transport.sent == []
    assert len(client.recorder.calls) == 0


def test_one_file_at_two_positions_counts_once(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    client = client_over(schema, transport, max_upload_bytes=10)
    file = UploadFile(b"x" * 6, filename="a")
    receipts = client.mutation("uploadAttachments", input={"files": [file, file]})
    assert [receipt.size for receipt in receipts] == [6, 6]
    other = UploadFile(b"x" * 6, filename="b")
    with pytest.raises(ArgumentError, match="hold 12 bytes"):
        client.mutation("uploadAttachments", input={"files": [file, other]})
    assert len(transport.sent) == 1


def test_the_limit_holds_over_http_before_any_request(
    schema: GraphQLSchema,
) -> None:
    received: Received = []
    with local_server(graphql_responder(schema, received)) as url:
        transport = HttpxTransport()
        try:
            client = build_client(
                url=url, transport=transport, schema=schema, max_upload_bytes=4
            )
            with pytest.raises(ArgumentError):
                client.mutation("uploadAvatar", file=UploadFile(b"12345", filename="a"))
        finally:
            transport.close()
    assert received == []


# -- the wire -----------------------------------------------------------------


def test_the_wire_is_the_multipart_request_of_the_specification(
    http: tuple[GraphQLClient, Received],
) -> None:
    client, received = http
    file = UploadFile(CONTENT, filename="a.txt", content_type="text/plain")
    receipt = client.mutation("uploadAvatar", file=file, fields=["filename", "size"])
    assert (receipt.filename, receipt.size) == ("a.txt", len(CONTENT))

    _, headers = received[-1]
    assert headers["content-type"].startswith("multipart/form-data; boundary=")
    assert headers["accept"] == (
        "application/graphql-response+json, application/json;q=0.9"
    )
    parts = parts_of(received)
    assert [part.name for part in parts] == ["operations", "map", "0"]
    operations = json.loads(parts[0].content)
    assert operations == {
        "query": operations["query"],
        "operationName": "uploadAvatar",
        "variables": {"file": None},
    }
    assert "uploadAvatar(file: $file)" in operations["query"]
    assert json.loads(parts[1].content) == {"0": ["variables.file"]}
    assert parts[0].headers == (
        ("Content-Disposition", 'form-data; name="operations"'),
    )
    assert parts[2].headers == (
        ("Content-Disposition", 'form-data; name="0"; filename="a.txt"'),
        ("Content-Type", "text/plain"),
    )
    assert parts[2].content == CONTENT


def test_a_nested_list_maps_each_file_to_its_position(
    http: tuple[GraphQLClient, Received],
) -> None:
    client, received = http
    one, two = UploadFile(b"1", filename="1.txt"), UploadFile(b"22", filename="2.txt")
    receipts = client.mutation(
        "uploadAttachments", input={"note": "n", "files": [one, two]}
    )
    assert [receipt.size for receipt in receipts] == [1, 2]
    parts = parts_of(received)
    assert [part.name for part in parts] == ["operations", "map", "0", "1"]
    assert json.loads(parts[0].content)["variables"] == {
        "input": {"note": "n", "files": [None, None]}
    }
    assert json.loads(parts[1].content) == {
        "0": ["variables.input.files.0"],
        "1": ["variables.input.files.1"],
    }
    assert [part.content for part in parts[2:]] == [b"1", b"22"]


def test_one_file_at_two_positions_is_sent_once_and_mapped_to_both(
    http: tuple[GraphQLClient, Received],
) -> None:
    client, received = http
    file = UploadFile(b"same", filename="same.txt")
    receipts = client.mutation("uploadAttachments", input={"files": [file, file]})
    assert [receipt.filename for receipt in receipts] == ["same.txt", "same.txt"]
    parts = parts_of(received)
    assert [part.name for part in parts] == ["operations", "map", "0"]
    assert json.loads(parts[1].content) == {
        "0": ["variables.input.files.0", "variables.input.files.1"]
    }


def test_a_request_without_a_file_is_still_json(
    http: tuple[GraphQLClient, Received],
) -> None:
    client, received = http
    client.query("user", id="u1", fields=["id"])
    assert received[-1][1]["content-type"] == "application/json"


class _FailFirstConnect(httpx.BaseTransport):
    """Refuses the first connection, then answers. Records each body sent."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(request.read())
        if len(self.bodies) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(
            200,
            headers={"content-type": MEDIA_TYPE},
            content=json.dumps(
                {"data": {"uploadAvatar": {"size": len(CONTENT)}}}
            ).encode(),
        )


def test_a_connect_failure_retry_resends_the_same_bytes(
    schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "pytest_graphql._core.transport.httpx_transport._sleep", lambda _s: None
    )
    pool = _FailFirstConnect()
    transport = HttpxTransport(_shared_pool=pool)
    try:
        client = build_client(
            url="http://127.0.0.1:9/graphql", transport=transport, schema=schema
        )
        file = UploadFile(CONTENT, filename="a.txt")
        assert client.mutation(
            "uploadAvatar", file=file, fields=["size"], idempotent=True
        ).size == len(CONTENT)
    finally:
        transport.close()
    assert len(pool.bodies) == 2
    assert pool.bodies[0] == pool.bodies[1]
    assert CONTENT in pool.bodies[0]


# -- in process ---------------------------------------------------------------


def test_an_upload_through_the_in_process_asgi_transport() -> None:
    transport = HttpxTransport.for_app(AsgiApp())
    try:
        client = build_client(
            url="/graphql", transport=transport, schema=build_schema(APP_SDL)
        )
        file = UploadFile(b"hello", filename="a.txt", content_type="text/plain")
        assert client.mutation("upload", file=file) == "a.txt:5:text/plain"
    finally:
        transport.close()


# -- no content in any rendered view ------------------------------------------


def _request(variables: dict[str, Any], url: str = "http://h/graphql") -> RequestInfo:
    return RequestInfo(
        operation="uploadAvatar",
        kind="mutation",
        document="mutation uploadAvatar($file: Upload!) { uploadAvatar(file: $file) }",
        variables=variables,
        headers={},
        url=url,
    )


def test_every_view_of_a_request_shows_the_marker_and_never_the_content() -> None:
    file = UploadFile(CONTENT, filename="a.txt", content_type="text/plain")
    request = _request({"file": file, "more": [file, {"deep": file}]})
    marker = f'<upload "a.txt", {len(CONTENT)} bytes, text/plain>'
    snapshot = request.redacted()
    assert snapshot.variables == {
        "file": marker,
        "more": [marker, {"deep": marker}],
    }
    for text in (repr(request), str(request), repr(snapshot), request.as_curl()):
        assert CONTENT_TEXT not in text
    assert marker in repr(snapshot)


def test_the_filename_passes_the_scrub_and_the_escape() -> None:
    secret = "token-value-123456"
    file = UploadFile(b"x", filename=f"{secret}\x1b[31m.txt")
    request = dataclasses.replace(
        _request({"file": file, "token": secret}),
    )
    rendered = request.redacted().variables["file"]
    assert secret not in rendered
    assert "[redacted:token]" in rendered
    assert "\x1b" not in rendered and "\\x1b" in rendered
    assert secret not in request.as_curl()


def test_a_redacted_variable_that_holds_a_file_shows_the_marker_of_its_path() -> None:
    request = _request({"password": UploadFile(CONTENT, filename="key.pem")})
    assert request.redacted().variables == {"password": "[redacted:password]"}
    assert CONTENT_TEXT not in request.as_curl()


def test_the_exceptions_and_the_recorder_never_hold_the_content(
    schema: GraphQLSchema,
) -> None:
    file = UploadFile(CONTENT, filename="a.txt")

    class Failing:
        def send(self, _request: RequestInfo, *, timeout: float) -> RawResponse:  # noqa: ARG002
            return RawResponse(
                status_code=200,
                media_type=MEDIA_TYPE,
                data=None,
                errors=({"message": "rejected", "path": ["uploadAvatar"]},),
                extensions=None,
                headers={},
            )

        def close(self) -> None:
            return None

    client = client_over(schema, Failing())
    with pytest.raises(GraphQLExecutionError) as execution:
        client.mutation("uploadAvatar", file=file, fields=["size"])

    transport = HttpxTransport(max_attempts=1)
    try:
        closed = build_client(
            url="http://127.0.0.1:9/graphql", transport=transport, schema=schema
        )
        with pytest.raises(GraphQLConnectionError) as connection:
            closed.mutation("uploadAvatar", file=file, fields=["size"])
    finally:
        transport.close()

    texts = [
        str(execution.value),
        repr(execution.value),
        str(connection.value),
        repr(connection.value),
        client.recorder.dump(),
        closed.recorder.dump(),
        *(repr(call) for call in client.recorder.calls),
        *(call.curl for call in closed.recorder.calls),
    ]
    for text in texts:
        assert CONTENT_TEXT not in text
    marker = f'<upload "a.txt", {len(CONTENT)} bytes, application/octet-stream>'
    for call in (*client.recorder.calls, *closed.recorder.calls):
        assert call.request.variables == {"file": marker}
        assert (
            '-F \'0=@"a.txt";headers="Content-Type: application/octet-stream"\''
        ) in call.curl


LEAK_TEST = f"""
from pytest_graphql import UploadFile
from tests.unit import plugin_probe


def test_upload(gql, caplog):
    # Split, so the traceback of the source line does not show the content.
    file = UploadFile({CONTENT[:5]!r} + {CONTENT[5:]!r}, filename="a.txt")
    gql.mutation("uploadAvatar", file=file, fields=["size"])
    plugin_probe.EVENTS.append(
        ("records", [record.getMessage() for record in caplog.records])
    )
    assert False, "boom"
"""


def test_no_content_in_the_failure_section_the_log_or_the_json_report(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit import plugin_probe
    from tests.unit.plugin_inner import run_inner

    result = run_inner(
        pytester,
        monkeypatch,
        test=LEAK_TEST,
        args=["--gql-log", "--gql-log-level=full", "--gql-report=report.jsonl"],
    )
    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    assert 'variables: {"file": "<upload \\"a.txt\\", 26 bytes' in output
    report = (pytester.path / "report.jsonl").read_text(encoding="utf-8")
    assert (
        '<upload \\"a.txt\\", 26 bytes' in json.loads(report)["calls"][0]["variables"]
    )
    records = [event for event in plugin_probe.EVENTS if event[0] == "records"]
    assert records and records[0][1]
    for text in (output, report, *records[0][1]):
        assert CONTENT_TEXT not in text


# -- as_curl() and hostile filenames ------------------------------------------

HOSTILE_NAMES = [
    "a;type=x",
    "a;headers=X: 1",
    "a,b",
    'a"b',
    "a\\b",
    "a'b",
    "a b",
    "a\nb",
    "äø-文件.txt",
]


def test_as_curl_renders_operations_map_and_each_file_in_the_quoted_form() -> None:
    one, two = UploadFile(b"1", filename="a.txt"), UploadFile(b"2", filename="b.txt")
    command = _request({"file": one, "list": [two, one]}).as_curl()
    operations = (
        '-F \'operations="{\\"operationName\\": \\"uploadAvatar\\", '
        '\\"query\\": \\"mutation uploadAvatar($file: Upload!) '
        '{ uploadAvatar(file: $file) }\\", \\"variables\\": '
        '{\\"file\\": null, \\"list\\": [null, null]}}"\''
    )
    file_map = (
        '-F \'map="{\\"0\\": [\\"variables.file\\", \\"variables.list.1\\"], '
        '\\"1\\": [\\"variables.list.0\\"]}"\''
    )
    octet = 'headers="Content-Type: application/octet-stream"'
    assert command == (
        "curl -sS -X POST http://h/graphql "
        f"{operations} {file_map} -F '0=@\"a.txt\";{octet}' -F '1=@\"b.txt\";{octet}'"
    )
    assert "--data" not in command


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_a_hostile_filename_stays_one_quoted_curl_word(name: str) -> None:
    command = _request({"file": UploadFile(b"x", filename=name)}).as_curl()
    words = _shell_words(command)
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    assert words[-2:] == [
        "-F",
        f'0=@"{escaped}";headers="Content-Type: application/octet-stream"',
    ]
    assert sum(word == "-F" for word in words) == 3
    assert "-H" not in words


def _shell_words(command: str) -> list[str]:
    import shlex

    return shlex.split(command)


def _wire_filename(part: Part) -> str:
    raw = part.param("filename")
    assert raw is not None
    return raw


needs_curl = [
    pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed"),
    pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX shell"),
]


def _curl_and_httpx_file_parts(
    file: UploadFile, schema: GraphQLSchema, folder: Path
) -> tuple[Part, Part]:
    """The file part of the ``as_curl()`` command and of the client, as received.

    Both go to the project's loopback server. The curl request is checked for
    its three parts, its map and its operations, and for no extra header.
    """
    received: Received = []
    with local_server(graphql_responder(schema, received)) as url:
        (folder / file.filename).write_bytes(file.content)
        command = _request({"file": file}, url=url).as_curl()
        run = subprocess.run(
            ["sh", "-c", command],
            cwd=folder,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert run.returncode == 0, run.stderr
        transport = HttpxTransport()
        try:
            transport.send(_request({"file": file}, url=url), timeout=10)
        finally:
            transport.close()
    (curl_body, curl_headers), (httpx_body, httpx_headers) = received
    curl_parts = parse_parts(curl_body, curl_headers["content-type"])
    httpx_parts = parse_parts(httpx_body, httpx_headers["content-type"])
    assert [part.name for part in curl_parts] == ["operations", "map", "0"]
    assert json.loads(curl_parts[1].content) == {"0": ["variables.file"]}
    assert json.loads(curl_parts[0].content)["variables"] == {"file": None}
    assert not any(key.startswith("x") for key in curl_headers)
    curl_file, httpx_file = curl_parts[2], httpx_parts[2]
    for part in (curl_file, httpx_file):
        assert [key for key, _ in part.headers] == [
            "Content-Disposition",
            "Content-Type",
        ]
    assert curl_file.content == httpx_file.content == file.content
    return curl_file, httpx_file


def _windows_cannot_send(name: str) -> str | None:
    """Why curl on Windows cannot read a file of this name, or None.

    The curl test writes the file to disk, and Windows refuses ``"``, ``\\``
    and a line break in a filename. Windows curl reads its arguments in the
    ANSI code page, so it cannot open a non-ASCII name given through ``sh``.
    The shell words are checked on every platform by the test above.
    """
    if sys.platform != "win32":
        return None
    if any(char in name for char in '"\\\n'):
        return "Windows cannot store this filename"
    if not name.isascii():
        return "Windows curl cannot open a non-ASCII filename given through sh"
    return None


CURL_NAMES = [
    pytest.param(
        name,
        marks=pytest.mark.skipif(reason is not None, reason=reason or ""),
    )
    for name in HOSTILE_NAMES
    for reason in [_windows_cannot_send(name)]
]


@pytest.mark.parametrize("name", CURL_NAMES)
@needs_curl[0]
@needs_curl[1]
def test_curl_sends_the_filename_unchanged_and_no_extra_header(
    name: str, schema: GraphQLSchema, tmp_path: Path
) -> None:
    file = UploadFile(b"hello", filename=name)
    curl_file, httpx_file = _curl_and_httpx_file_parts(file, schema, tmp_path)
    assert decode_filename(_wire_filename(curl_file)) == name
    assert decode_filename(_wire_filename(httpx_file)) == name


HOSTILE_TYPES = [
    "text/csv",
    "text/plain; charset=utf-8",
    "a/b;headers=X-Evil: 1",
    'a/"b", c',
    "a\\b/c;type=x",
]


@pytest.mark.parametrize("content_type", HOSTILE_TYPES)
@needs_curl[0]
@needs_curl[1]
def test_curl_sends_the_content_type_of_the_file_unchanged(
    content_type: str, schema: GraphQLSchema, tmp_path: Path
) -> None:
    # report.bin: curl would guess application/octet-stream from the name.
    file = UploadFile(b"hello", filename="report.bin", content_type=content_type)
    curl_file, httpx_file = _curl_and_httpx_file_parts(file, schema, tmp_path)
    assert curl_file.header("Content-Type") == content_type
    assert httpx_file.header("Content-Type") == content_type


@pytest.mark.parametrize("content_type", HOSTILE_TYPES)
def test_a_hostile_content_type_stays_inside_the_quoted_curl_word(
    content_type: str,
) -> None:
    file = UploadFile(b"x", filename="a.bin", content_type=content_type)
    words = _shell_words(_request({"file": file}).as_curl())
    escaped = content_type.replace("\\", "\\\\").replace('"', '\\"')
    assert words[-1] == f'0=@"a.bin";headers="Content-Type: {escaped}"'
    assert sum(word == "-F" for word in words) == 3


# -- as_curl() follows the snapshot -------------------------------------------


def _curl_map(command: str) -> dict[str, list[str]]:
    words = _shell_words(command)
    value = next(word for word in words if word.startswith("map="))
    text = value.removeprefix("map=")
    decoded: dict[str, list[str]] = json.loads(json.loads(text))
    return decoded


def _curl_operations(command: str) -> dict[str, Any]:
    words = _shell_words(command)
    value = next(word for word in words if word.startswith("operations="))
    decoded: dict[str, Any] = json.loads(json.loads(value.removeprefix("operations=")))
    return decoded


def _file_words(command: str) -> list[str]:
    return [word for word in _shell_words(command) if "=@" in word]


def test_a_redacted_file_gets_no_curl_part_and_its_name_is_hidden() -> None:
    name = "private-filename.pem"
    file = UploadFile(b"x", filename=name)
    request = dataclasses.replace(_request({"file": file}), redact_variables=("file",))
    command = request.as_curl()
    assert name not in command
    assert _file_words(command) == []
    assert _curl_map(command) == {}
    assert _curl_operations(command)["variables"] == {"file": "[redacted:file]"}
    # The name alone is a secret too, as a server would echo it.
    assert request.scrub(f"bad file {name}") == "bad file [redacted:file]"


def test_files_under_a_redacted_input_get_no_curl_part() -> None:
    one = UploadFile(b"1", filename="first-private.txt")
    two = UploadFile(b"2", filename="second-private.txt")
    request = dataclasses.replace(
        _request({"input": {"note": "n", "files": [one, two]}}),
        redact_variables=("input",),
    )
    command = request.as_curl()
    for text in ("first-private", "second-private", '"note"'):
        assert text not in command
    assert _file_words(command) == []
    assert _curl_map(command) == {}


def test_a_file_at_a_redacted_and_a_visible_position_gets_no_curl_part() -> None:
    name = "private-filename.pem"
    file = UploadFile(b"x", filename=name)
    request = dataclasses.replace(
        _request({"secret": file, "file": file}), redact_variables=("secret",)
    )
    snapshot = request.redacted()
    command = request.as_curl()
    assert name not in repr(snapshot)
    assert name not in command
    assert snapshot.variables == {
        "secret": "[redacted:secret]",
        "file": "[redacted:secret]",
    }
    assert _curl_map(command) == {}
    assert _file_words(command) == []


def test_a_long_filename_is_bounded_in_the_curl_command() -> None:
    file = UploadFile(b"x", filename="a" * 100_000 + ".txt")
    request = dataclasses.replace(_request({"file": file}), max_diagnostic_bytes=128)
    command = request.as_curl()
    assert len(command) < 1_000
    assert _file_words(command) == []
    assert _curl_map(command) == {}
    assert "a" * 200 not in command


@pytest.mark.parametrize("shared", [False, True])
def test_many_files_and_many_positions_are_bounded_in_the_curl_command(
    shared: bool,
) -> None:
    one = UploadFile(b"", filename="e.txt")
    files = [one if shared else UploadFile(b"", filename="e.txt") for _ in range(2000)]
    request = dataclasses.replace(_request({"files": files}), max_diagnostic_bytes=1024)
    command = request.as_curl()
    assert _metadata_bytes(command) <= 1024
    mapped = [path for paths in _curl_map(command).values() for path in paths]
    assert 0 < len(mapped) < 100
    rendered = _curl_operations(command)["variables"]["files"]
    # A mapped position is null. A position past the budget keeps its marker.
    assert rendered[: len(mapped)] == [None] * len(mapped)
    assert all(item is not None for item in rendered[len(mapped) :])
    assert len(_file_words(command)) == (1 if shared else len(mapped))


def _metadata_bytes(command: str) -> int:
    """The UTF-8 size of the ``map`` part and the file parts, as emitted."""
    return len(command[command.index("-F 'map=") :].encode("utf-8"))


@pytest.mark.parametrize(
    "filename", ["a.txt", 'ä"文\\;,.txt'], ids=["ascii", "escaped-unicode"]
)
def test_the_map_and_the_file_parts_fit_max_diagnostic_bytes_exactly(
    filename: str,
) -> None:
    files = [UploadFile(b"", filename=filename) for _ in range(2000)]
    widths = set()
    for limit in range(16, 4097, 45):
        request = dataclasses.replace(
            _request({"files": files}), max_diagnostic_bytes=limit
        )
        command = request.as_curl()
        assert _metadata_bytes(command) <= limit, limit
        file_map = _curl_map(command)
        widths.update(len(key) for key in file_map)
        assert len(_file_words(command)) == len(file_map)
        # Admission is in order: no gap before the last mapped position.
        mapped = [path for key in sorted(file_map, key=int) for path in file_map[key]]
        assert mapped == [f"variables.files.{index}" for index in range(len(mapped))]
    # The limits cross the change from one-digit to two-digit part names.
    assert widths == {1, 2}


def test_the_empty_map_is_the_fixed_minimum_of_a_tiny_budget() -> None:
    file = UploadFile(b"", filename="a.txt")
    request = dataclasses.replace(_request({"file": file}), max_diagnostic_bytes=8)
    command = request.as_curl()
    assert command.endswith("-F 'map=\"{}\"'")
    assert _file_words(command) == []


def test_a_position_dropped_from_the_snapshot_is_not_mapped() -> None:
    files = [UploadFile(b"", filename=f"f{index}.txt") for index in range(50)]
    request = dataclasses.replace(_request({"files": files}), max_diagnostic_bytes=256)
    shown = request.redacted().variables["files"]
    assert len(shown) < 50
    command = request.as_curl()
    mapped = [path for paths in _curl_map(command).values() for path in paths]
    assert mapped == [f"variables.files.{index}" for index in range(len(mapped))]
    assert len(mapped) <= len(shown)
    assert "f49.txt" not in command


# -- the factory --------------------------------------------------------------


def test_fake_makes_a_seeded_small_text_file_for_an_upload_scalar(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    def fake(seed: int) -> dict[str, Any]:
        payload: dict[str, Any] = client_over(
            schema, transport, seed=seed
        ).fake.AttachmentInput()
        return payload

    first, again, other = fake(7), fake(7), fake(8)
    assert first == again
    assert first != other
    files = first["files"]
    assert files
    for file in files:
        assert isinstance(file, UploadFile)
        assert 16 <= len(file.content) <= 64
        assert set(file.content.decode("ascii")) <= set(string.printable[:95])
        assert file.content_type == "text/plain"
        prefix, number, suffix = (
            file.filename[:5],
            file.filename[5:-4],
            file.filename[-4:],
        )
        assert (prefix, suffix) == ("fake-", ".txt")
        assert number.isdigit() and int(number) < 10000
    receipts = client_over(schema, transport, seed=7).mutation(
        "uploadAttachments", input=first
    )
    assert [receipt.filename for receipt in receipts] == [f.filename for f in files]


def test_a_registered_spec_for_an_upload_scalar_makes_its_own_fake(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    client = GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(url="http://127.0.0.1:9/graphql"),
        scalars=ScalarRegistry(
            [ScalarSpec(name="Upload", serialize=dict, fake=lambda _rng: "custom")]
        ),
    )
    assert set(client.fake.AttachmentInput()["files"]) == {"custom"}


# -- the plugin options -------------------------------------------------------


RECORD = """
from tests.unit import plugin_probe


def test_record(gql):
    plugin_probe.EVENTS.append(("config", gql.config))
"""


def test_the_ini_options_and_environment_variables_set_the_fields(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.plugin_inner import recorded, run_inner

    result = run_inner(
        pytester,
        monkeypatch,
        test=RECORD,
        ini="gql_upload_scalars =\n    Upload\n    File\ngql_max_upload_bytes = 100",
        env={"PYTEST_GQL_MAX_UPLOAD_BYTES": "200"},
    )
    result.assert_outcomes(passed=1)
    config = recorded("config")[0][1]
    assert tuple(config.upload_scalars) == ("Upload", "File")
    assert config.max_upload_bytes == 200


@pytest.mark.parametrize(
    ("ini", "setting"),
    [
        ("gql_upload_scalars = not-a-name", "gql_upload_scalars"),
        ("gql_max_upload_bytes = -1", "gql_max_upload_bytes"),
    ],
)
def test_an_invalid_value_is_refused_without_repeating_it(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, ini: str, setting: str
) -> None:
    from tests.unit.plugin_inner import run_inner

    result = run_inner(pytester, monkeypatch, test=RECORD, ini=ini)
    assert result.ret != 0
    error = result.stderr.str()
    assert setting in error
    assert "not-a-name" not in error and "-1" not in error
