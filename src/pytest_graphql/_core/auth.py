"""The ``Auth`` protocol and the two implementations that ship (B5).

``Auth.apply`` is called once per request, so a token that has to be refreshed
is refreshed inside ``apply`` and needs no extra machinery. It sits fourth in
the C4 header precedence chain: above ``ClientConfig.headers`` and the
``gql_headers`` fixture, below ``with_headers()`` and a per-call ``headers=``.

``apply`` receives the live ``RequestInfo`` (C2), because an implementation
genuinely needs the real values to sign or refresh a request. The leak
boundary is rendering, not the object, so nothing here may render one: this
module produces a new ``RequestInfo`` and never text.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from pytest_graphql._core.diagnostics import RequestInfo
from pytest_graphql._core.headers import merge_headers


@runtime_checkable
class Auth(Protocol):
    """One request identity: something that signs each request before it is sent.

    Any object with an `apply` method of this shape is an `Auth`. The client
    calls `apply` once for every request, so a token that expires can be
    refreshed inside `apply` and needs no other machinery. Use `BearerAuth` or
    `HeaderAuth` for the common cases and write your own class for anything else.

    Headers reach a request from several places. In order, lowest to highest:
    `ClientConfig.headers`, the `Auth` object, `GraphQLClient.with_headers()`,
    and a `headers=` option on a single call. A later source replaces an earlier
    one for the same header name. Names compare without regard to case.

    Examples:
        A custom `Auth` that adds an API key header:

        ```python {.exec}
        from dataclasses import replace

        from pytest_graphql import Auth, RequestInfo


        class ApiKeyAuth:
            def __init__(self, key: str) -> None:
                self.key = key

            def apply(self, request: RequestInfo) -> RequestInfo:
                headers = {**request.headers, "X-Api-Key": self.key}
                return replace(request, headers=headers)


        auth = ApiKeyAuth("key-123")
        assert isinstance(auth, Auth)
        ada = gql.with_auth(auth).query("user", id="u1")
        assert ada.name == "Ada Lovelace"
        ```
    """

    def apply(self, request: RequestInfo) -> RequestInfo:
        """Return the request to send, with this identity applied.

        `request` is the live request and carries real header and variable
        values, because signing needs them. It is immutable, so build a new one
        with `dataclasses.replace()` and return that. Do not print or log the
        request text yourself. Its `repr()` is already redacted, and
        `redacted()` is the safe view if you need one.

        Args:
            request: The request about to be sent.

        Returns:
            The request to send in its place.

        Examples:
            ```python {.exec}
            from pytest_graphql import BearerAuth, RequestInfo

            request = RequestInfo(
                operation=None,
                kind="query",
                document="{ __typename }",
                variables={},
                headers={},
                url="http://localhost:8000/graphql",
            )
            signed = BearerAuth("tok").apply(request)
            assert signed.headers["Authorization"] == "Bearer tok"
            ```
        """
        ...


def with_headers(request: RequestInfo, headers: Mapping[str, str]) -> RequestInfo:
    """``request`` with ``headers`` merged over its own, under the C4 rule.

    The one place an ``Auth`` implementation in this package rewrites a
    request, so the case-insensitive replacement rule is applied once rather
    than at each implementation.
    """
    return dataclasses.replace(request, headers=merge_headers(request.headers, headers))


class BearerAuth:
    """Authenticate with an `Authorization: Bearer <token>` header.

    `repr()` of this object never shows the token. In a request, the
    `Authorization` header is shown as a placeholder in `repr()`, in a snapshot
    and in a failure report, because that header name is in
    `ClientConfig.redact_headers` by default.

    The token is also removed from free text, such as a server's error message or
    the query text, because it is the value of a header in `redact_headers`. That
    removal has three conditions. `authorization` must stay in that list.
    `ClientConfig.redact_values` must be on. The token must be at least
    `ClientConfig.min_redacted_value_length` characters long, which is 8 by
    default. A token that fails one of them can stay in such text. Give a test
    account a token that is long enough.

    Args:
        token: The bearer token. Do not include the `Bearer ` prefix.

    Examples:
        ```python {.exec}
        from pytest_graphql import BearerAuth

        auth = BearerAuth("s3cret-token")
        assert repr(auth) == "BearerAuth(token=<redacted>)"
        ada = gql.with_auth(auth).query("user", id="u1")
        assert ada.name == "Ada Lovelace"

        # Free text: a token of 8 characters or more is removed, a shorter one is not.
        from pytest_graphql import RequestInfo

        request = RequestInfo(None, "query", "{ a }", {}, {}, "http://localhost/graphql")
        assert "[redacted:" in BearerAuth("long-enough-token").apply(request).scrub(
            "echo long-enough-token"
        )
        assert BearerAuth("short").apply(request).scrub("echo short") == "echo short"

        # The header name must be in redact_headers, as `authorization` is by default.
        from dataclasses import replace

        unlisted = replace(request, redact_headers=frozenset())
        token = BearerAuth("long-enough-token")
        assert token.apply(unlisted).scrub("echo long-enough-token") == (
            "echo long-enough-token"
        )
        ```
    """

    __slots__ = ("token",)

    def __init__(self, token: str) -> None:
        self.token = token
        """The token as given. Treat it as a secret.

        Examples:
            ```python {.exec}
            from pytest_graphql import BearerAuth

            assert BearerAuth("s3cret-token").token == "s3cret-token"
            ```
        """

    def apply(self, request: RequestInfo) -> RequestInfo:
        """Return `request` with `Authorization: Bearer <token>` set.

        An `Authorization` header already on the request, in any capitalization,
        is replaced.

        Args:
            request: The request about to be sent.

        Returns:
            A copy of `request` that carries the header.

        Examples:
            ```python {.exec}
            from pytest_graphql import BearerAuth, RequestInfo

            request = RequestInfo(
                operation=None,
                kind="query",
                document="{ __typename }",
                variables={},
                headers={"authorization": "Bearer old"},
                url="http://localhost:8000/graphql",
            )
            signed = BearerAuth("new").apply(request)
            assert dict(signed.headers) == {"Authorization": "Bearer new"}
            ```
        """
        return with_headers(request, {"Authorization": f"Bearer {self.token}"})

    def __repr__(self) -> str:
        return "BearerAuth(token=<redacted>)"


class HeaderAuth:
    """Authenticate with a fixed set of headers, such as an API key.

    Header names that contain a hyphen cannot be written as keyword arguments,
    so the constructor also takes a mapping as its first positional argument.
    Both forms can be mixed in one call. A name given twice, in any
    capitalization, keeps the keyword value.

    `repr()` of this object shows the header names and never the values.

    In a request, a header value is shown as a placeholder only when its name is
    in `ClientConfig.redact_headers`. The default list is `authorization`,
    `cookie`, `proxy-authorization` and `x-api-key`. A header with another name
    is shown with its value in `repr()` of the request, in a snapshot and in a
    failure report, so add its name to that list.

    The same list decides what is removed from free text, such as a server's
    error message or the query text. The value of a header whose name is in
    `redact_headers` is a known secret. The value of a header with another name
    is not removed because of that header, however long it is. It is removed
    only if the same text is also a known secret from another source, such as
    the URL. For a listed name, the removal needs `ClientConfig.redact_values`
    on and a value of at least `ClientConfig.min_redacted_value_length`
    characters, which is 8 by default.

    Args:
        headers: An optional mapping of header names to values. It is
            positional only.
        **named: More headers, written as keyword arguments.

    Examples:
        ```python {.exec}
        from pytest_graphql import HeaderAuth

        auth = HeaderAuth({"X-Api-Key": "key-123"}, Authorization="Token abc")
        assert repr(auth).endswith("values=<redacted>)")
        ada = gql.with_auth(auth).query("user", id="u1")
        assert ada.name == "Ada Lovelace"

        # In a request, only a name in redact_headers is hidden.
        from pytest_graphql import RequestInfo

        request = RequestInfo(None, "query", "{ a }", {}, {}, "http://localhost/graphql")
        shown = HeaderAuth({"X-Trace": "t-1"}).apply(request).redacted().headers
        assert shown["X-Trace"] == "t-1"
        hidden = HeaderAuth({"X-Api-Key": "k-1"}).apply(request).redacted().headers
        assert hidden["X-Api-Key"] == "[redacted:X-Api-Key]"

        # Free text: an unlisted name does not make its value a secret.
        from dataclasses import replace

        echo = "server echoed long-secret-value"
        custom = HeaderAuth({"X-Custom-Token": "long-secret-value"})
        assert custom.apply(request).scrub(echo) == echo
        names = frozenset({"x-custom-token"})
        listed = replace(request, redact_headers=names)
        assert "long-secret-value" not in custom.apply(listed).scrub(echo)
        ```
    """

    __slots__ = ("headers",)

    def __init__(
        self, headers: Mapping[str, str] | None = None, /, **named: str
    ) -> None:
        self.headers = merge_headers(headers, named)
        """The headers this object sets, as a plain dict. The values are secrets.

        Examples:
            ```python {.exec}
            from pytest_graphql import HeaderAuth

            auth = HeaderAuth({"X-Api-Key": "key-123"}, Accept="application/json")
            expected = {"X-Api-Key": "key-123", "Accept": "application/json"}
            assert auth.headers == expected
            ```
        """

    def apply(self, request: RequestInfo) -> RequestInfo:
        """Return `request` with these headers set.

        A header already on the request with the same name, in any
        capitalization, is replaced by this one.

        Args:
            request: The request about to be sent.

        Returns:
            A copy of `request` that carries the headers.

        Examples:
            ```python {.exec}
            from pytest_graphql import HeaderAuth, RequestInfo

            request = RequestInfo(
                operation=None,
                kind="query",
                document="{ __typename }",
                variables={},
                headers={"x-api-key": "old"},
                url="http://localhost:8000/graphql",
            )
            signed = HeaderAuth({"X-Api-Key": "new"}).apply(request)
            assert dict(signed.headers) == {"X-Api-Key": "new"}
            ```
        """
        return with_headers(request, self.headers)

    def __repr__(self) -> str:
        names = ", ".join(sorted(self.headers))
        return f"HeaderAuth(names=[{names}], values=<redacted>)"


def as_(auth: Auth | str) -> Auth:
    """Resolve the documented shorthand: a bare string is a bearer token."""
    return BearerAuth(auth) if isinstance(auth, str) else auth
