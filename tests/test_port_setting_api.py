import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

import claude_unlimited.config as config
import claude_unlimited.daemon as daemon
import claude_unlimited.daemon_installer as daemon_installer
import claude_unlimited.profiles as profile_repo


class FakeSecretStore:
    def __init__(self):
        self.tokens = {}

    def set_token(self, profile_id, token):
        self.tokens[profile_id] = token

    def get_token(self, profile_id):
        return self.tokens[profile_id]

    def delete_token(self, profile_id):
        self.tokens.pop(profile_id, None)

    def has_token(self, profile_id):
        return profile_id in self.tokens


@pytest.fixture
def running_server(monkeypatch, tmp_path):
    monkeypatch.setattr(profile_repo, "secret_store", FakeSecretStore())
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")

    server = daemon.make_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", daemon._CSRF_TOKEN, tmp_path, port
    finally:
        server.shutdown()
        t.join(timeout=2)
        server.server_close()


def _request(url, method="GET", body=None, headers=None):
    headers = headers or {}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# GET /api/settings — launcher_kinds / running_port siblings
# ---------------------------------------------------------------------------


def test_get_settings_includes_launcher_kinds_and_running_port(running_server):
    base, _, _, port = running_server
    status, body = _request(f"{base}/api/settings")
    assert status == 200
    assert body["running_port"] == port
    kinds = {row["kind"]: row for row in body["launcher_kinds"]}
    assert kinds["claude"] == {
        "kind": "claude",
        "label_key": "settings.launchers.claude",
        "default_command": "claude",
        "used_by": "claude-unlimited code",
    }
    assert kinds["codex"] == {
        "kind": "codex",
        "label_key": "settings.launchers.codex",
        "default_command": "codex",
        "used_by": "claude-unlimited add-codex-account",
    }
    # existing settings keys are untouched siblings
    assert "launchers" in body["settings"]
    assert "port" in body["settings"]
    assert body["settings"]["update_mode"] == "auto_download"


def test_get_settings_launcher_kinds_reflects_a_registry_entry_added_at_runtime(running_server, monkeypatch):
    # Proves launcher_kinds is derived from config.LAUNCHER_KINDS by
    # iteration (daemon.py holds no literal list of its own) — adding a
    # future CLI is one registry tuple entry with no daemon.py edit.
    extra = config.LauncherKind(
        kind="futurecli", label_key="settings.launchers.futurecli",
        default_command="futurecli", used_by="claude-unlimited future-thing",
    )
    monkeypatch.setattr(config, "LAUNCHER_KINDS", config.LAUNCHER_KINDS + (extra,))

    base, _, _, _ = running_server
    status, body = _request(f"{base}/api/settings")
    assert status == 200
    kinds = {row["kind"] for row in body["launcher_kinds"]}
    assert "futurecli" in kinds


# ---------------------------------------------------------------------------
# PATCH /api/settings — launchers full replacement, port still rejected
# ---------------------------------------------------------------------------


def test_patch_settings_accepts_launchers_full_replacement(running_server):
    base, token, _, _ = running_server
    status, body = _request(f"{base}/api/settings", "PATCH", {"launchers": {"claude": "claude --verbose"}},
                             headers={"X-CSRF-Token": token})
    assert status == 200
    assert body["settings"]["launchers"] == {"claude": "claude --verbose"}

    # full replacement, not merge: patching again with a different mapping
    # drops the previous key entirely
    status, body = _request(f"{base}/api/settings", "PATCH", {"launchers": {"codex": "codex --yolo"}},
                             headers={"X-CSRF-Token": token})
    assert status == 200
    assert body["settings"]["launchers"] == {"codex": "codex --yolo"}


def test_patch_settings_still_rejects_port(running_server):
    base, token, _, _ = running_server
    status, body = _request(f"{base}/api/settings", "PATCH", {"port": 5555},
                             headers={"X-CSRF-Token": token})
    assert status == 400
    assert "POST /api/settings/port" in body["message"]


# ---------------------------------------------------------------------------
# POST /api/settings/port
# ---------------------------------------------------------------------------


def test_port_change_requires_csrf(running_server):
    base, _, _, _ = running_server
    status, body = _request(f"{base}/api/settings/port", "POST", {"port": 5555})
    assert status == 403


def test_port_change_rejects_below_1024(running_server):
    base, token, _, _ = running_server
    status, body = _request(f"{base}/api/settings/port", "POST", {"port": 80},
                             headers={"X-CSRF-Token": token})
    assert status == 400
    assert body["error"] == "invalid_port"
    assert "1024" in body["message"]


def test_port_change_rejects_above_65535(running_server):
    base, token, _, _ = running_server
    status, body = _request(f"{base}/api/settings/port", "POST", {"port": 70000},
                             headers={"X-CSRF-Token": token})
    assert status == 400
    assert body["error"] == "invalid_port"


@pytest.mark.parametrize("bad", [0, 80, 1023, 65536, 99999, -1])
def test_port_change_rejects_everything_outside_the_shared_range(bad, running_server):
    # The endpoint and config.resolve_port() now share one definition of the
    # range (config.MIN_PORT/MAX_PORT), so this asserts against the constants
    # rather than repeating 1024/65535 a third time.
    base, token, _, _ = running_server
    assert not config.port_in_range(bad)
    status, body = _request(f"{base}/api/settings/port", "POST", {"port": bad},
                             headers={"X-CSRF-Token": token})
    assert status == 400
    assert body["error"] == "invalid_port"
    assert str(config.MIN_PORT) in body["message"]
    assert str(config.MAX_PORT) in body["message"]


def test_port_change_rejects_a_non_integer_naming_the_shared_range(running_server):
    base, token, _, _ = running_server
    status, body = _request(f"{base}/api/settings/port", "POST", {"port": "not-a-port"},
                             headers={"X-CSRF-Token": token})
    assert status == 400
    assert body["error"] == "invalid_port"
    assert str(config.MIN_PORT) in body["message"]
    assert str(config.MAX_PORT) in body["message"]


def test_port_change_noop_when_equal_to_running_port(running_server, monkeypatch):
    base, token, tmp_path, port = running_server
    install_calls = []
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": False, "running": False, "pid": None})
    monkeypatch.setattr(daemon_installer, "install", lambda p: install_calls.append(p))
    status, body = _request(f"{base}/api/settings/port", "POST", {"port": port},
                             headers={"X-CSRF-Token": token})
    assert status == 200
    assert body == {"changed": False, "port": port}
    assert install_calls == []
    # nothing persisted either
    assert not (tmp_path / "config.json").exists()


def test_port_change_409_when_port_in_use_and_persists_nothing(running_server, monkeypatch):
    base, token, tmp_path, _ = running_server
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": False, "running": False, "pid": None})

    busy_port = _free_port()
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", busy_port))
    holder.listen(1)
    try:
        status, body = _request(f"{base}/api/settings/port", "POST", {"port": busy_port},
                                 headers={"X-CSRF-Token": token})
        assert status == 409
        assert body["error"] == "port_in_use"
    finally:
        holder.close()

    # the whole reason for the preflight: nothing was written to disk
    assert not (tmp_path / "config.json").exists()
    status, body = _request(f"{base}/api/settings")
    assert body["settings"]["port"] != busy_port


def test_port_change_not_installed_persists_but_does_not_restart(running_server, monkeypatch):
    base, token, tmp_path, _ = running_server
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": False, "running": False, "pid": None})
    install_calls = []
    start_calls = []
    monkeypatch.setattr(daemon_installer, "install", lambda p: install_calls.append(p))
    monkeypatch.setattr(daemon_installer, "start", lambda: start_calls.append(True))

    new_port = _free_port()
    status, body = _request(f"{base}/api/settings/port", "POST", {"port": new_port},
                             headers={"X-CSRF-Token": token})
    assert status == 200
    assert body["changed"] is True
    assert body["port"] == new_port
    assert body["service_updated"] is False
    assert body["restarting"] is False
    assert "next" in body["message"].lower() or "start" in body["message"].lower()

    # persisted despite not being an installed service
    assert install_calls == []
    time.sleep(0.05)
    assert start_calls == []
    status, body = _request(f"{base}/api/settings")
    assert body["settings"]["port"] == new_port


def test_port_change_installed_regenerates_unit_and_restarts(running_server, monkeypatch):
    base, token, tmp_path, _ = running_server
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": True, "running": True, "pid": 123})
    install_calls = []
    start_calls = []
    monkeypatch.setattr(daemon_installer, "install", lambda p: install_calls.append(p))
    monkeypatch.setattr(daemon_installer, "start", lambda: start_calls.append(True))

    new_port = _free_port()
    status, body = _request(f"{base}/api/settings/port", "POST", {"port": new_port},
                             headers={"X-CSRF-Token": token})
    assert status == 200
    assert body == {"changed": True, "port": new_port, "service_updated": True, "restarting": True}

    # install() — which itself restarts the service on every platform — runs
    # in a background thread AFTER the response above. See
    # test_port_change_response_reaches_client_before_install_restarts_the_service
    # for a test that actually proves that ordering rather than just this
    # call-order list, which a response-then-install AND an
    # install-then-response implementation would both satisfy.
    deadline = time.time() + 2
    while not install_calls and time.time() < deadline:
        time.sleep(0.02)
    assert install_calls == [new_port]

    # install() is the only restart call needed: daemon_installer.start()
    # would be a second, redundant restart of the service.
    assert start_calls == []

    # and the setting was actually persisted before the restart thread ran
    pool = config.load_pool()
    assert pool.settings.port == new_port


def test_port_change_response_reaches_client_before_install_restarts_the_service(running_server, monkeypatch):
    """Proves the 200 response is fully in the client's hands BEFORE
    daemon_installer.install() is invoked — install() is what actually
    restarts the service (synchronously, on every platform), so calling it
    first would tear down this process's listener while the request is
    still in flight and the client would see a connection reset instead of
    the success response it was promised.

    A recorded call-order list (as in the test above) cannot catch this: it
    looks identical whether install() runs before or after the response is
    written, since both happen inside the same test process either way.
    Here the stubbed install() instead BLOCKS on a threading.Event that this
    test only sets once it has read the response back in full — a real
    synchronization primitive, not a timing guess. A correct (respond-then-
    install) implementation has that event already set (or sees it get set
    within a few milliseconds) by the time install() runs, so the wait
    below returns almost instantly with True. A broken (install-then-
    respond) implementation calls install() from inside the still-blocked
    request handler, before the response has even been written — the event
    cannot become set until that call returns, so the wait exhausts its
    whole timeout and returns False. Using wait()'s own return value (rather
    than snapshotting is_set() the instant install() is entered) is what
    keeps this deterministic instead of racing normal thread scheduling.
    """
    base, token, _, _ = running_server
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": True, "running": True, "pid": 123})

    client_done = threading.Event()
    install_invoked = threading.Event()
    install_recorded = threading.Event()
    install_saw_client_done = []

    def fake_install(port):
        install_invoked.set()
        install_saw_client_done.append(client_done.wait(timeout=2.0))
        # Signals that the append above has actually executed. Without this,
        # the main thread can observe install_invoked as set and race ahead
        # to read install_saw_client_done in the gap between wait()
        # returning and the append completing — flakily seeing [] even
        # against a correct implementation.
        install_recorded.set()

    monkeypatch.setattr(daemon_installer, "install", fake_install)

    new_port = _free_port()
    req = urllib.request.Request(
        f"{base}/api/settings/port", data=json.dumps({"port": new_port}).encode(),
        method="POST", headers={"X-CSRF-Token": token},
    )
    # A generous client-side timeout: a broken implementation still delivers
    # the response eventually (once fake_install's own 2s wait above times
    # out and the handler falls through to send_json), it just delivers it
    # too late — this must not itself be what fails the test.
    with urllib.request.urlopen(req, timeout=5) as resp:
        status = resp.status
        body = json.loads(resp.read())
    client_done.set()

    assert status == 200
    assert body == {"changed": True, "port": new_port, "service_updated": True, "restarting": True}

    assert install_invoked.wait(timeout=3), "daemon_installer.install() was never called"
    # Bounded wait for fake_install's own append to have actually run, rather
    # than reading install_saw_client_done the instant install_invoked and
    # client_done.wait() return — that gap is exactly what made this test
    # flaky (~1 run in several) against CORRECT code: it could observe []
    # before the background thread finished appending. A broken
    # (install-then-respond) implementation still reaches this point — it
    # just gets here with install_saw_client_done == [False] — so this wait
    # does not weaken the test's ability to catch that bug.
    assert install_recorded.wait(timeout=3), "fake_install never finished recording its observation"
    assert install_saw_client_done == [True], (
        "daemon_installer.install() ran before the client had the response in hand — "
        "it timed out waiting for client_done instead of finding it already on its way"
    )


def test_port_change_persists_via_config_set_port(running_server, monkeypatch, tmp_path):
    # POST /api/settings/port must write through config.set_port (a PATCH
    # can't, since validated_settings_changes rejects "port" outright).
    calls = []
    real_set_port = config.set_port

    def spy(port):
        calls.append(port)
        return real_set_port(port)

    monkeypatch.setattr(daemon, "set_port", spy)
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": False, "running": False, "pid": None})

    base, token, _, _ = running_server
    new_port = _free_port()
    status, body = _request(f"{base}/api/settings/port", "POST", {"port": new_port},
                             headers={"X-CSRF-Token": token})
    assert status == 200
    assert calls == [new_port]


# ---------------------------------------------------------------------------
# Startup bind failure names the recovery (plan §3.6)
# ---------------------------------------------------------------------------


def test_run_foreground_bind_failure_names_config_path_and_port_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")

    busy_port = _free_port()
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", busy_port))
    holder.listen(1)
    try:
        with pytest.raises(OSError):
            daemon.run_foreground(host="127.0.0.1", port=busy_port)
    finally:
        holder.close()

    err = capsys.readouterr().err
    assert str(tmp_path / "config.json") in err
    assert "port" in err
    assert '"settings"' in err
    assert "claude-unlimited install" in err
