# File uploads

After this page you can send a file to a mutation, send several files in one
call, set where a file is accepted and how large the files may be, and read a
report of a call that sent a file.

## Send a file

Pass an [`UploadFile`](api.md#pytest_graphql.UploadFile) as the value of an
argument whose type is an upload scalar, such as `Upload`. The client sends the
call as a multipart request, by the
[GraphQL multipart request specification](https://github.com/jaydenseric/graphql-multipart-request-spec)
version 2.1.0. No extra package is needed.

```python {.exec}
from pytest_graphql import UploadFile

avatar = UploadFile(b"\x89PNG...", filename="avatar.png", content_type="image/png")
receipt = gql.mutation("uploadAvatar", file=avatar)

assert receipt.filename == "avatar.png"
assert receipt.size == 7
assert receipt.content_type == "image/png"
```

`content_type` defaults to `application/octet-stream`. It must be printable
ASCII, because it is written into a header line of the request. A line break or
another control character raises `ArgumentError`.

## Read a file from disk

`UploadFile.from_path()` reads the file once, when you call it, and keeps the
bytes in memory. The filename is the last part of the path only, so no local
directory reaches the server. The type is guessed from the filename, and you can
give it yourself.

```python {.exec}
import tempfile
from pathlib import Path

from pytest_graphql import UploadFile

with tempfile.TemporaryDirectory() as folder:
    path = Path(folder) / "fixtures" / "notes.txt"
    path.parent.mkdir()
    path.write_bytes(b"hello")
    notes = UploadFile.from_path(path)
    report = UploadFile.from_path(path, content_type="text/markdown")

assert notes.filename == "notes.txt"
assert notes.content_type == "text/plain"
assert report.content_type == "text/markdown"
assert gql.mutation("uploadAvatar", file=notes).size == 5
```

A file object is not accepted. The client may send a call again after a failed
connection, and it must send the same bytes each time.

## Send several files

A file can sit inside an input object and inside a list, at any depth. The same
`UploadFile` object at two places is sent once, and the server receives it at
both.

```python {.exec}
from pytest_graphql import UploadFile

cover = UploadFile(b"cover", filename="cover.txt")
page = UploadFile(b"page one", filename="page.txt")

receipts = gql.mutation(
    "uploadAttachments", input={"note": "draft", "files": [cover, page, cover]}
)

assert [receipt.filename for receipt in receipts] == [
    "cover.txt",
    "page.txt",
    "cover.txt",
]
```

## Where a file is accepted

A file is accepted only where the type is an upload scalar. The setting
`upload_scalars` names those scalars, and its default is `("Upload",)`. If your
schema calls the scalar something else, such as `File`, set
`upload_scalars=("File",)` in `ClientConfig`, or the ini option
`gql_upload_scalars`. Anywhere else, the call raises `ArgumentError` before
anything is sent. The message names the place and the scalars that accept a
file.

```python {.exec}
from pytest_graphql import ArgumentError, UploadFile

try:
    gql.mutation("updateUser", id="u1", name=UploadFile(b"x", filename="a.txt"))
except ArgumentError as error:
    message = str(error)

assert "$name holds an UploadFile, but the position of type 'String'" in message
assert "A file is accepted only where the type is 'Upload'." in message
```

A schema that has none of the named scalars refuses every file, and the message
tells you to set `upload_scalars`. A `ScalarSpec` that you register for an
upload scalar is not called on an `UploadFile`.

## The size limit

`max_upload_bytes` bounds the total size of the files of one call. The default
is 32 MiB. A file sent at several places counts once. A larger call raises
`ArgumentError` before anything is sent. The ini option is
`gql_max_upload_bytes`.

```python {.exec}
from pytest_graphql import ArgumentError, UploadFile, build_client

client = build_client(
    url="http://localhost:8000/graphql",
    transport=gql.transport,
    schema=gql.schema,
    max_upload_bytes=1024,
)
try:
    client.mutation("uploadAvatar", file=UploadFile(b"x" * 2000, filename="big.bin"))
except ArgumentError as error:
    assert "more than max_upload_bytes=1,024" in str(error)
```

## A server that needs a CSRF header

The client adds no CSRF header. Some servers refuse a multipart request unless it
carries a header that a plain HTML form cannot send. Apollo Server is one
example. Add the header that your server names with `with_headers()`.

```python {.exec}
from pytest_graphql import UploadFile

client = gql.with_headers({"Apollo-Require-Preflight": "true"})
assert client.mutation("uploadAvatar", file=UploadFile(b"x", filename="a")).size == 1
```

In a whole suite, put the header in the ini option `gql_headers` instead.

## Generated files

For an upload scalar with no registered `ScalarSpec`, `gql.fake` makes a small
`UploadFile`: 16 to 64 printable ASCII bytes, a name such as `fake-1234.txt`, and
the type `text/plain`. The same seed gives the same file.

```python {.exec}
from pytest_graphql import UploadFile

payload = gql.fake.AttachmentInput()
first = payload["files"][0]

assert isinstance(first, UploadFile)
assert first.content_type == "text/plain"
assert first.filename.startswith("fake-")
assert gql.fake.AttachmentInput() == payload
```

## Files in reports

A report never shows the content of a file. Everywhere a variable is shown, a
file is shown as its name, its size and its type:

```text
variables: {"file": "<upload \"avatar.png\", 7 bytes, image/png>"}
```

The filename goes through the same removal of known secrets as other text. The
`curl` command of a report sends the same parts as the client: `operations` with
`null` at each file, `map`, and one `-F` part per file that names the file by its
filename and gives its type:

```text
curl -sS -X POST http://localhost:8000/graphql -F 'operations="{...}"' -F 'map="{\"0\": [\"variables.file\"]}"' -F '0=@"avatar.png";headers="Content-Type: image/png"'
```

`curl` reads that file from the directory where you run the command, so you
supply it. Each part is in double quotes inside the `-F` value, so a `;` or a `,`
in a filename or a type stays part of it.

The command shows a file only where the report shows it. A file in a variable
that you list in `redact_variables` gets no part, and its filename is a known
secret, so it is removed from the rest of the text too. The `map` and the file
parts together are cut to `max_diagnostic_bytes`, as each field of a report is.
A position past the cut keeps its marker in `operations`.

## Custom transports

A custom transport receives each `UploadFile` at its place in
`request.variables`. The multipart encoding is done by the built-in transport,
also when it runs an app in process. A custom transport that cannot send files
raises its own error. See [Extending](extending.md#a-custom-transport).
