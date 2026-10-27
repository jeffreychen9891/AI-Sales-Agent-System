"""T82: the session owns its channel sink, on a port the OS handed out.

The address used to be the literal `127.0.0.1:58090` in three unconnected
places -- `tests/conftest.py`, `scripts/ci.sh` and `scripts/ci.ps1` -- and
`FASTAGENT_CHANNEL_SEND_URL` did not reach the one in `conftest` at all. Two
things followed, both of which cost real runs: two checkouts could not gate at
the same time, and when they did line up, one run was graded against a sink the
other had started.

What is asserted here is the property that makes both impossible: **the address
the code under test is given and the address the sink is listening on are the
same fact**, and that fact is not a constant anyone can write down.
"""

from __future__ import annotations

import os
import socket

import httpx


def test_the_send_url_points_at_this_sessions_own_sink(channel_sink: str) -> None:
    """One fact, not two literals that have to agree.

    `FASTAGENT_CHANNEL_SEND_URL` is what `Settings` reads and therefore what
    every `HttpChannelAdapter` in the suite talks to. If it can drift from the
    sink this session started, the suite can be graded against someone else's
    dependency -- which is the failure this fixture exists to remove, so it is
    the failure worth a test.
    """

    assert os.environ["FASTAGENT_CHANNEL_SEND_URL"] == f"{channel_sink}/send"


def test_the_sink_is_serving_and_resettable(channel_sink: str) -> None:
    """A live socket, not just a well-formed URL.

    `seeded_database` calls `/test/reset` on this address before every case. When
    that call went to a hard-coded port, a run whose sink was elsewhere reset
    *nothing* and still went green.
    """

    with httpx.Client(trust_env=False) as client:
        assert client.get(f"{channel_sink}/healthz", timeout=5).json() == {"status": "ok"}
        assert client.post(f"{channel_sink}/test/reset", timeout=5).status_code == 200


def test_the_port_was_assigned_rather_than_chosen(channel_sink: str) -> None:
    """Two runs on one machine must not be able to land on the same port.

    Asserting "not 58090" alone would pass for any other constant, so the real
    check is the second one: asking the OS for another loopback port while this
    sink holds its own never returns the sink's port. That is the whole reason
    the fixture takes its port from `bind(("127.0.0.1", 0))` and hands that same
    socket to uvicorn instead of picking a number.
    """

    port = int(channel_sink.rsplit(":", 1)[1])
    assert port != 58090, "the fixed port is what T82 removed"

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other:
        other.bind(("127.0.0.1", 0))
        assert other.getsockname()[1] != port
