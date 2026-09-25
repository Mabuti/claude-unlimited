"""In-flight accounting on the Anthropic ingress (/v1/messages): a Profile's
slot is a reference count, released exactly once per request, including
when the response is discarded before a byte of it was read.

The gateway runs for real; only the upstream transport is faked."""

import pytest

import claude_unlimited.daemon as daemon
import claude_unlimited.gateway as gateway_module
from claude_unlimited.config import Pool, Profile, save_pool
from claude_unlimited.gateway import Gateway
from claude_unlimited.upstream import UpstreamResponse

USAGE_HEADERS = {"anthropic-ratelimit-unified-5h-utilization": "0.1",
                 "anthropic-ratelimit-unified-5h-reset": "1787191800",
                 "content-type": "application/json"}


class RecordingConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeSecretStore:
    def get_token(self, profile_id):
        return "tok-" + profile_id


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore())
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))
    connections = []

    def transport(req):
        conn = RecordingConnection()
        connections.append(conn)

        def chunks():
            yield b'{"ok":'
            yield b"true}"

        return UpstreamResponse(status=200, headers=dict(USAGE_HEADERS), body_chunks=chunks(), connection=conn)

    gw = Gateway(transport=transport)
    gw.connections = connections
    return gw


def test_a_messages_result_discarded_unread_releases_its_slot_and_connection(gateway):
    result = gateway.handle("POST", "/v1/messages", {}, b"{}")
    assert gateway.serving_now_ids() == {"a"}

    daemon._discard_result(result)       # e.g. the client left before the headers went out

    assert gateway.serving_now_ids() == set()
    assert gateway.connections[0].closed


def test_two_open_messages_responses_keep_the_profile_in_flight_until_both_finish(gateway):
    first = gateway.handle("POST", "/v1/messages", {}, b"{}")
    second = gateway.handle("POST", "/v1/messages", {}, b"{}")

    assert b"".join(first.body_chunks) == b'{"ok":true}'
    assert gateway.serving_now_ids() == {"a"}      # the second one is still open
    assert gateway.seconds_since_last_activity() == 0.0

    assert b"".join(second.body_chunks) == b'{"ok":true}'
    assert gateway.serving_now_ids() == set()
    assert all(conn.closed for conn in gateway.connections)


def test_a_drained_body_is_released_once_even_if_closed_again(gateway):
    first = gateway.handle("POST", "/v1/messages", {}, b"{}")
    second = gateway.handle("POST", "/v1/messages", {}, b"{}")

    b"".join(first.body_chunks)
    first.body_chunks.close()            # already released on exhaustion: must not release again
    daemon._discard_result(first)

    assert gateway.serving_now_ids() == {"a"}
    daemon._discard_result(second)
    assert gateway.serving_now_ids() == set()


class _GoneClient:
    """A request handler whose client disconnected before the headers."""

    def send_response(self, status):
        raise BrokenPipeError("client gone")

    def send_header(self, key, value):
        raise AssertionError("unreachable")

    def end_headers(self):
        raise AssertionError("unreachable")


def test_write_proxy_result_releases_a_body_it_never_started(gateway):
    result = gateway.handle("POST", "/v1/messages", {}, b"{}")

    with pytest.raises(BrokenPipeError):
        daemon._DashboardHandler._write_proxy_result(_GoneClient(), result)

    assert gateway.serving_now_ids() == set()
    assert gateway.connections[0].closed
