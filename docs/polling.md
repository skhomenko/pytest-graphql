# Polling

After this page you can wait until a query returns what you expect, set how long
and how often the call tries again, and tell when a poll is the wrong tool.

## Wait for a result

Some results are not ready when a call returns. A background job finishes later.
A search index catches up. A record appears after a message is handled. For these,
use `wait_until()`. It runs a query again and again until your function says the
result is good, and it returns that result.

```python {.exec}
import dataclasses

from pytest_graphql import build_client


class AppearsLater:
    # Answers that the user does not exist until the third call, as a server
    # does while a background job is still running.

    def __init__(self, inner, ready_on_call):
        self.inner = inner
        self.ready_on_call = ready_on_call
        self.calls = 0

    def send(self, request, *, timeout):
        raw = self.inner.send(request, timeout=timeout)
        self.calls += 1
        if self.calls < self.ready_on_call:
            return dataclasses.replace(raw, data={"user": None})
        return raw

    def close(self):
        pass


late = AppearsLater(gql.transport, ready_on_call=3)
client = build_client(
    url="http://localhost:8000/graphql", transport=late, schema=gql.schema
)

user = client.wait_until(
    "user",
    id="u1",
    fields=["id", "name"],
    until=lambda user: user is not None,
    timeout=5,
    interval=0.01,
)

assert user.name == "Ada Lovelace"
assert late.calls == 3
```

The example uses a small transport of its own to play the part of a slow server.
It answers "no such user" twice and then gives the real answer. See
[Extending](extending.md) for how a transport works. In your tests, the server is
the slow part, and you write only the `wait_until()` call. The interval is 0.01
seconds here so that the page runs fast. For a real server, use a longer
interval, such as 1 second.

The call takes the same name, arguments and options as `query()`. These are the
options that belong to the poll:

| Option | Default | Meaning |
|---|---|---|
| `until` | none, required | A function. It receives what `query()` returns. The poll ends when its result is true. |
| `timeout` | `30.0` | The deadline of the whole poll, in seconds. |
| `interval` | `1.0` | The first sleep between two attempts, in seconds. |
| `backoff` | `1.0` | The factor that makes the sleep longer after each attempt. |
| `ignore` | none | An exception class, or a tuple of classes, that means "not ready yet". |

`until` receives the same value that `query()` would return. With `raw=True` it
receives the whole response, and `wait_until()` returns the response too. A
variable that has the name of a poll option goes through `variables={...}`.

`timeout` here is not the time limit of one HTTP call. The limit of one call is the
`gql_timeout` setting. See [Configuration](configuration.md).

## Set the delay

After a failed attempt, the call sleeps for `interval * backoff ** (attempt - 1)`
seconds. It never sleeps longer than the time that is left, so the last attempt
runs at the deadline. The deadline is set once, before the first attempt. Nothing
else limits a sleep.

| `interval` | `backoff` | `timeout` | Sleeps between the attempts |
|---|---|---|---|
| `1` | `1` | `5` | 1, 1, 1, 1, 1 |
| `1` | `2` | `30` | 1, 2, 4, 8, 15 |
| `0.5` | `1.5` | `5` | 0.5, 0.75, 1.125, 1.6875, 0.9375 |
| `1` | `1` | `0` | none |

The second row ends with 15 and not 16, because 15 seconds are left. The last row
shows that one attempt always runs, even when the timeout is `0`.

A constant `interval` is right for most tests. Use `backoff` when the thing you
wait for can take a long time, and you do not want to call the server often while
you wait. `timeout`, `interval` and `backoff` must be finite numbers. `timeout` and
`interval` must be 0 or more, and `backoff` must be 1 or more. A value outside
that range raises `ValueError`, and a value that is not a number raises
`TypeError`, before the first call. An infinite timeout is refused, because a poll
that cannot end is not a bounded wait.

```python {.exec}
import time

from pytest_graphql import WaitTimeoutError

start = time.monotonic()
try:
    gql.wait_until(
        "user",
        id="u1",
        fields=["id", "name"],
        until=lambda user: user.name == "Grace Hopper",
        timeout=0.1,
        interval=0.01,
        backoff=2,
    )
except WaitTimeoutError as error:
    assert error.operation == "user"
    assert error.attempts >= 1
    assert error.timeout == 0.1
    assert time.monotonic() - start >= 0.1
else:
    raise AssertionError("the name never changes, so the wait must time out")
```

## Only queries

A poll repeats its call, so it must not change anything. `wait_until()` takes the
name of a query. The schema decides what a name is. A mutation raises
`ArgumentError` before the first call.

```python {.exec}
import pytest

from pytest_graphql import ArgumentError

with pytest.raises(ArgumentError, match="polls queries only"):
    gql.wait_until("updateUser", id="u1", until=lambda user: user)
```

To change something and then wait for the effect, call the mutation once. Then poll
a query that reads the effect.

## Keep waiting through an error

Some calls fail while the system is not ready. A gateway answers 503 while a
service starts. A query can fail with an execution error until the data exists.
List those exceptions in `ignore`. The poll counts such an attempt as "not ready
yet" and tries again.

`ignore` covers the whole attempt. It covers the call, the reading of the response,
and your `until` function. It accepts `Exception` subclasses only, so `Ctrl+C` and
`SystemExit` always stop the test. An exception that is not listed leaves the poll
at once, unchanged.

```python {.exec}
from pytest_graphql import GraphQLHTTPStatusError, build_client


class StartingUp:
    # Fails with 503 for the first calls, as a server does while it starts.

    def __init__(self, inner, failures):
        self.inner = inner
        self.failures = failures
        self.calls = 0

    def send(self, request, *, timeout):
        self.calls += 1
        if self.calls <= self.failures:
            raise GraphQLHTTPStatusError(
                "request failed with status 503: Service Unavailable",
                request=request.redacted(),
                status_code=503,
                body_excerpt="Service Unavailable",
            )
        return self.inner.send(request, timeout=timeout)

    def close(self):
        pass


starting = StartingUp(gql.transport, failures=2)
client = build_client(
    url="http://localhost:8000/graphql", transport=starting, schema=gql.schema
)

user = client.wait_until(
    "user",
    id="u1",
    fields=["id", "name"],
    until=lambda user: user.name,
    timeout=5,
    interval=0.01,
    ignore=GraphQLHTTPStatusError,
)

assert user.name == "Ada Lovelace"
assert starting.calls == 3
```

Be exact in `ignore`. A broad class, such as `Exception`, hides a real defect
until the time runs out. The report then says "timed out", and the cause is gone
from the first line.

## Stop early on a state that cannot recover

A job that has failed will not succeed if you wait longer. Do not wait for the
deadline. Raise from `until`. An exception that is not in `ignore` leaves the poll
at once, so the test fails with your message and not with a timeout.

```python {.exec}
import pytest


def done(user):
    if user.name == "Ada Lovelace":  # stands in for a status such as "FAILED"
        pytest.fail("the job failed, so waiting longer cannot help")
    return user.name == "Grace Hopper"


with pytest.raises(pytest.fail.Exception, match="the job failed"):
    gql.wait_until("user", id="u1", fields=["id", "name"], until=done, timeout=5)
```

## When the time runs out

The poll raises [`WaitTimeoutError`](api.md#pytest_graphql.WaitTimeoutError). It
has these attributes:

| Attribute | Holds |
|---|---|
| `operation` | The name of the query. |
| `attempts` | How many calls were made. |
| `elapsed` | The seconds from the first attempt until the poll stopped. |
| `timeout` | The limit the poll was given. |
| `last_response` | The latest response of any attempt, or `None`. |
| `last_exception` | The latest exception that `ignore` swallowed, or `None`. |

The message names the attempts and the time, and it says what to check:

```text
wait_until('user') timed out: 5 attempt(s) in 0.05 s (limit 0.05 s).
  Expected: until() to return true before the deadline.
  Last response: none, no attempt returned one.
  Last ignored exception: GraphQLHTTPStatusError: request failed with status 503: Service Unavailable
  Fix: raise timeout, check that until() can become true, or list the exception that hides the real failure in ignore.
```

The text of the last swallowed exception is shown only when it can be checked
against the request that caused it. If it cannot, the text is withheld, because it
could hold a secret. See [Diagnostics](diagnostics.md). The exception itself is
always on `last_exception`.

## When polling is the wrong answer

Polling is slow, and it can hide a defect. Use it only when the product is
supposed to need time. In these cases, something else is better.

- **The result is in the answer.** A mutation returns the changed record. Assert on
  that. Do not poll for a value that the call already gave you.
- **The test waits for time to pass.** A `time.sleep(5)` costs five seconds every
  run, and it is still too short on a slow day. `wait_until()` returns as soon as
  the result is ready.
- **The state can only get worse.** If a "failed" state is final, stop on it, as
  shown above. Waiting for the deadline hides the real answer.
- **You need to prove that something does not happen.** A poll cannot show a
  negative. Waiting for the whole timeout is slow and proves little. Check once,
  after the event that should have caused the effect.
- **You poll to hide a race in the product.** If a test passes only after many
  attempts, the product is slow or it has a defect. Keep the timeout close to what
  the product promises, so a slowdown fails the test.
- **The wait takes minutes.** A test that waits that long is hard to run. Test the
  job logic in a smaller test, and keep one slow test for the whole path.
- **You want to repeat a mutation.** A mutation can run its side effect twice, so a
  poll refuses it. If a mutation may fail and you need to repeat it, repeat it in
  your own code, where you can see what repeats.
