# Quickstart

The GraphQL test client for Python whose calls are built from the schema and checked against it before they are sent.

Install the package with its pytest extra:

```bash
pip install "pytest-graphql[pytest]"
```

Tell pytest where your GraphQL endpoint is:

```ini
# pytest.ini
[pytest]
gql_url = http://localhost:8000/graphql
```

Write a test. The `gql` fixture is a client that has already read your schema:

```python {.exec}
def test_user_has_a_name(gql):
    user = gql.query("user", id="u1")
    assert user.name == "Ada Lovelace"
```

Run `pytest`. The client chooses the fields to ask for, so the test contains no query text.

To learn what the package changes in your tests, read [Why pytest-graphql?](why.md).
