"""In-process resolvers and fixture data for the hostile test schema.

``build_schema()`` reads ``sdl.graphql``, binds the resolvers below onto the
resulting :class:`~graphql.GraphQLSchema`, and returns it. Nothing here imports
``pytest``: this module is fixture data, not a test.
"""

from __future__ import annotations

import datetime
import uuid
from pathlib import Path
from typing import Any

from graphql import GraphQLError, GraphQLResolveInfo, GraphQLSchema
from graphql import build_schema as build_ast_schema

SDL_PATH = Path(__file__).resolve().parent / "sdl.graphql"

# ---------------------------------------------------------------------------
# Fixture data. Each record carries an internal "__typename" marker used only
# by resolve_type below; it is never exposed as a GraphQL field.
# ---------------------------------------------------------------------------

USERS: dict[str, dict[str, Any]] = {
    "u1": {
        "__typename": "User",
        "id": "u1",
        "name": "Ada Lovelace",
        "managerId": None,
        "teamId": "t1",
        "oldName": "Ada L.",
        "legacyManagerId": None,
        "userId": "u1",
        "user_id": "u1-snake",
        "joinedAt": datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
        "externalId": uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "preferences": {"theme": "dark"},
        "avatar": None,
        "balance": "100.00",
    },
    "u2": {
        "__typename": "User",
        "id": "u2",
        "name": "Grace Hopper",
        "managerId": "u1",
        "teamId": "t1",
        "oldName": "Grace H.",
        "legacyManagerId": "u1",
        "userId": "u2",
        "user_id": "u2-snake",
        "joinedAt": datetime.datetime(2020, 2, 1, tzinfo=datetime.timezone.utc),
        "externalId": uuid.UUID("22222222-2222-2222-2222-222222222222"),
        "preferences": {"theme": "light"},
        "avatar": {"filename": "u2.png", "contentType": "image/png"},
        "balance": "250.50",
    },
    "u3": {
        "__typename": "User",
        "id": "u3",
        "name": "Alan Turing",
        "managerId": "u1",
        "teamId": "t2",
        "oldName": "Alan T.",
        "legacyManagerId": "u1",
        "userId": "u3",
        "user_id": "u3-snake",
        "joinedAt": datetime.datetime(2020, 3, 1, tzinfo=datetime.timezone.utc),
        "externalId": uuid.UUID("33333333-3333-3333-3333-333333333333"),
        "preferences": None,
        "avatar": None,
        "balance": None,
    },
}

TEAMS: dict[str, dict[str, Any]] = {
    "t1": {"__typename": "Team", "id": "t1", "name": "Core", "captainId": "u2"},
    "t2": {"__typename": "Team", "id": "t2", "name": "Platform", "captainId": "u3"},
}

POSTS: dict[str, dict[str, Any]] = {
    "p1": {
        "__typename": "Post",
        "id": "p1",
        "title": "Hello World",
        "authorId": "u1",
        "editorId": "u2",
    },
    "p2": {
        "__typename": "Post",
        "id": "p2",
        "title": "Second Post",
        "authorId": "u2",
        "editorId": None,
    },
}

ATTACHMENTS: list[dict[str, Any]] = [
    {"__typename": "Attachment", "filename": "diagram.png", "sizeBytes": 2048},
]

SETTINGS: dict[str, Any] = {"theme": "dark", "locale": "en-US"}

RESERVED_WORD_FIELDS: dict[str, Any] = {
    "id": "rw-1",
    "from": "origin",
    "class": "vip",
    "type": "member",
    "keys": ["a", "b"],
    "items": ["x", "y"],
    "values": ["1", "2"],
    "to_dict": "ok",
}

WIDE_INSTANCE: dict[str, Any] = {f"field{i}": f"value{i}" for i in range(300)}

_NODES_BY_ID: dict[str, dict[str, Any]] = {**USERS, **TEAMS, **POSTS}


def _posts_by_author(user_id: str) -> list[dict[str, Any]]:
    return [post for post in POSTS.values() if post["authorId"] == user_id]


def _connection(
    posts: list[dict[str, Any]], *, first: int | None, after: str | None
) -> dict[str, Any]:
    start = 0
    if after is not None:
        for index, post in enumerate(posts):
            if post["id"] == after:
                start = index + 1
                break
    window = posts[start:]
    if first is not None:
        window = window[:first]
    edges = [{"cursor": post["id"], "node": post} for post in window]
    return {
        "edges": edges,
        "pageInfo": {
            "hasNextPage": start + len(window) < len(posts),
            "hasPreviousPage": start > 0,
            "startCursor": edges[0]["cursor"] if edges else None,
            "endCursor": edges[-1]["cursor"] if edges else None,
        },
    }


# ---------------------------------------------------------------------------
# Field resolvers. Argument names below match the GraphQL argument names in
# sdl.graphql exactly; graphql-core passes them as keyword arguments.
# ---------------------------------------------------------------------------


def resolve_user_manager(user: dict[str, Any], _info: GraphQLResolveInfo) -> Any:
    manager_id = user["managerId"]
    return USERS.get(manager_id) if manager_id else None


def resolve_user_legacy_manager(user: dict[str, Any], _info: GraphQLResolveInfo) -> Any:
    manager_id = user["legacyManagerId"]
    return USERS.get(manager_id) if manager_id else None


def resolve_user_team(user: dict[str, Any], _info: GraphQLResolveInfo) -> Any:
    return TEAMS.get(user["teamId"])


def resolve_user_posts(
    user: dict[str, Any], _info: GraphQLResolveInfo, *, first: int
) -> Any:
    return _posts_by_author(user["id"])[:first]


def resolve_user_friends(
    user: dict[str, Any], _info: GraphQLResolveInfo, *, limit: int = 10
) -> Any:
    peers = [
        other
        for other in USERS.values()
        if other["teamId"] == user["teamId"] and other["id"] != user["id"]
    ]
    return peers[:limit]


def resolve_user_loose_friends(user: dict[str, Any], _info: GraphQLResolveInfo) -> Any:
    # Deliberately includes a null entry: exercises [User] (nullable items).
    peers = resolve_user_friends(user, _info)
    return [*peers, None]


def resolve_user_strict_friends(user: dict[str, Any], _info: GraphQLResolveInfo) -> Any:
    return resolve_user_friends(user, _info)


def resolve_user_posts_connection(
    user: dict[str, Any],
    _info: GraphQLResolveInfo,
    *,
    first: int | None = None,
    after: str | None = None,
) -> Any:
    return _connection(_posts_by_author(user["id"]), first=first, after=after)


def resolve_team_captain(team: dict[str, Any], _info: GraphQLResolveInfo) -> Any:
    captain_id = team["captainId"]
    return USERS.get(captain_id) if captain_id else None


def resolve_team_members(team: dict[str, Any], _info: GraphQLResolveInfo) -> Any:
    return [user for user in USERS.values() if user["teamId"] == team["id"]]


def resolve_post_author(post: dict[str, Any], _info: GraphQLResolveInfo) -> Any:
    return USERS[post["authorId"]]


def resolve_post_editor(post: dict[str, Any], _info: GraphQLResolveInfo) -> Any:
    editor_id = post["editorId"]
    return USERS.get(editor_id) if editor_id else None


def resolve_query_node(_root: None, _info: GraphQLResolveInfo, **kwargs: Any) -> Any:
    return _NODES_BY_ID.get(kwargs["id"])


def resolve_query_user(_root: None, _info: GraphQLResolveInfo, **kwargs: Any) -> Any:
    return USERS.get(kwargs["id"])


def resolve_query_users(_root: None, _info: GraphQLResolveInfo) -> Any:
    return list(USERS.values())


def resolve_query_team(_root: None, _info: GraphQLResolveInfo, **kwargs: Any) -> Any:
    return TEAMS.get(kwargs["id"])


def resolve_query_teams(_root: None, _info: GraphQLResolveInfo) -> Any:
    return list(TEAMS.values())


def resolve_query_post(_root: None, _info: GraphQLResolveInfo, **kwargs: Any) -> Any:
    return POSTS.get(kwargs["id"])


def resolve_query_settings(_root: None, _info: GraphQLResolveInfo) -> Any:
    return SETTINGS


def resolve_query_reserved_word_fields(_root: None, _info: GraphQLResolveInfo) -> Any:
    return RESERVED_WORD_FIELDS


def resolve_query_search(_root: None, _info: GraphQLResolveInfo, *, term: str) -> Any:
    needle = term.lower()
    results: list[dict[str, Any]] = []
    results.extend(u for u in USERS.values() if needle in u["name"].lower())
    results.extend(t for t in TEAMS.values() if needle in t["name"].lower())
    results.extend(a for a in ATTACHMENTS if needle in a["filename"].lower())
    return results


def resolve_query_wide(_root: None, _info: GraphQLResolveInfo) -> Any:
    return WIDE_INSTANCE


def resolve_query_matrix(_root: None, _info: GraphQLResolveInfo) -> Any:
    return [["a", "b"], ["c"]]


def resolve_query_ping_scalar(_root: None, _info: GraphQLResolveInfo) -> Any:
    return True


def resolve_query_nullable_list(_root: None, _info: GraphQLResolveInfo) -> Any:
    return None


def resolve_query_non_null_list_of_nullables(
    _root: None, _info: GraphQLResolveInfo
) -> Any:
    return ["x", None, "y"]


def resolve_query_py_keyword(_root: None, _info: GraphQLResolveInfo) -> Any:
    return "class"


def resolve_query_py_keyword_echo(
    _root: None, _info: GraphQLResolveInfo, **kwargs: Any
) -> Any:
    return kwargs["value"]


def resolve_mutation_update_user(
    _root: None, _info: GraphQLResolveInfo, **kwargs: Any
) -> Any:
    user = USERS[kwargs["id"]]
    name = kwargs.get("name")
    if name is None:
        return user
    return {**user, "name": name}


def resolve_mutation_create_post(
    _root: None, _info: GraphQLResolveInfo, **kwargs: Any
) -> Any:
    post_input = kwargs["input"]
    return {
        "__typename": "Post",
        "id": "p-new",
        "title": post_input["title"],
        "authorId": post_input["authorId"],
        "editorId": None,
    }


def resolve_mutation_move_post(
    _root: None, _info: GraphQLResolveInfo, **kwargs: Any
) -> Any:
    # Post has no team relation modeled; the "data" argument only exercises
    # the "multiple arguments, one an input object" mutation shape.
    return POSTS[kwargs["id"]]


def _resolve_node_type(
    value: dict[str, Any], _info: GraphQLResolveInfo, _type: Any
) -> str:
    return str(value["__typename"])


FIELD_RESOLVERS: dict[str, dict[str, Any]] = {
    "Query": {
        "node": resolve_query_node,
        "user": resolve_query_user,
        "users": resolve_query_users,
        "team": resolve_query_team,
        "teams": resolve_query_teams,
        "post": resolve_query_post,
        "settings": resolve_query_settings,
        "reservedWordFields": resolve_query_reserved_word_fields,
        "search": resolve_query_search,
        "wide": resolve_query_wide,
        "matrix": resolve_query_matrix,
        "pingScalar": resolve_query_ping_scalar,
        "nullableList": resolve_query_nullable_list,
        "nonNullListOfNullables": resolve_query_non_null_list_of_nullables,
        "pyKeyword": resolve_query_py_keyword,
        "pyKeywordEcho": resolve_query_py_keyword_echo,
    },
    "Mutation": {
        "updateUser": resolve_mutation_update_user,
        "createPost": resolve_mutation_create_post,
        "movePost": resolve_mutation_move_post,
    },
    "User": {
        "manager": resolve_user_manager,
        "legacyManager": resolve_user_legacy_manager,
        "team": resolve_user_team,
        "posts": resolve_user_posts,
        "friends": resolve_user_friends,
        "looseFriends": resolve_user_loose_friends,
        "strictFriends": resolve_user_strict_friends,
        "postsConnection": resolve_user_posts_connection,
    },
    "Team": {
        "captain": resolve_team_captain,
        "members": resolve_team_members,
    },
    "Post": {
        "author": resolve_post_author,
        "editor": resolve_post_editor,
    },
}


def _serialize_upload(value: Any) -> Any:
    if not isinstance(value, dict) or "filename" not in value:
        raise GraphQLError(
            f"Upload must serialize a mapping with a 'filename' key, got {value!r}"
        )
    return dict(value)


def _parse_upload(value: Any) -> Any:
    return _serialize_upload(value)


# Scalars with a real, "registered" serializer/parser. Money is deliberately
# left out: it is the "one unregistered" custom scalar the checklist asks for,
# so it keeps graphql-core's identity default. JSON is registered but its
# correct behaviour is identity, since a JSON scalar means "accept any
# JSON-shaped value as-is"; Upload is registered with a validating serializer
# so it is observably different from an unregistered scalar, unlike identity.
SCALAR_SERIALIZERS: dict[str, Any] = {
    "DateTime": (
        lambda value: (
            value.isoformat() if isinstance(value, datetime.datetime) else value
        ),
        lambda value: datetime.datetime.fromisoformat(value),
    ),
    "UUID": (
        lambda value: str(value),
        lambda value: uuid.UUID(value),
    ),
    "JSON": (
        lambda value: value,
        lambda value: value,
    ),
    "Upload": (
        _serialize_upload,
        _parse_upload,
    ),
}


def build_schema() -> GraphQLSchema:
    """Build the hostile test schema with every resolver and scalar bound."""
    schema = build_ast_schema(SDL_PATH.read_text(encoding="utf-8"))

    for type_name, fields in FIELD_RESOLVERS.items():
        graphql_type = schema.type_map[type_name]
        for field_name, resolver in fields.items():
            graphql_type.fields[field_name].resolve = resolver  # type: ignore[attr-defined]

    node_interface = schema.type_map["Node"]
    node_interface.resolve_type = _resolve_node_type  # type: ignore[attr-defined]

    search_union = schema.type_map["SearchResult"]
    search_union.resolve_type = _resolve_node_type  # type: ignore[attr-defined]

    for scalar_name, (serialize, parse_value) in SCALAR_SERIALIZERS.items():
        scalar_type = schema.type_map[scalar_name]
        scalar_type.serialize = serialize  # type: ignore[attr-defined]
        scalar_type.parse_value = parse_value  # type: ignore[attr-defined]

    return schema
