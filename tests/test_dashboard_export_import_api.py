import json
import threading
import urllib.error
import urllib.request

import pytest

import claude_unlimited.daemon as daemon
import claude_unlimited.export_import as export_import
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
    store = FakeSecretStore()
    monkeypatch.setattr(profile_repo, "secret_store", store)
    monkeypatch.setattr(export_import, "secret_store", store)
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(export_import, "activity_module", __import__("claude_unlimited.activity", fromlist=["x"]))

    server = daemon.make_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", daemon._CSRF_TOKEN
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


def test_export_settings_only_no_passphrase(running_server):
    base, csrf = running_server
    status, payload = _request(f"{base}/api/export", "POST",
                                {"include_profiles": False, "include_settings": True, "include_activity": False},
                                {"X-CSRF-Token": csrf})
    assert status == 200
    assert payload["encrypted"] is False


def test_export_profiles_without_passphrase_rejected(running_server):
    base, csrf = running_server
    status, _ = _request(f"{base}/api/profiles", "POST",
                          {"name": "X", "kind": "oauth", "credential": "tok-long-enough", "account_uuid": "u1"},
                          {"X-CSRF-Token": csrf})
    assert status == 201

    status, payload = _request(f"{base}/api/export", "POST",
                                {"include_profiles": True, "include_settings": False, "include_activity": False},
                                {"X-CSRF-Token": csrf})
    assert status == 400
    assert payload["error"] == "export_failed"


def test_export_requires_csrf(running_server):
    base, _csrf = running_server
    status, payload = _request(f"{base}/api/export", "POST",
                                {"include_profiles": False, "include_settings": True, "include_activity": False})
    assert status == 403


def test_full_export_import_roundtrip_via_api(running_server):
    base, csrf = running_server
    headers = {"X-CSRF-Token": csrf}

    status, _ = _request(f"{base}/api/profiles", "POST",
                          {"name": "Personal Max", "kind": "oauth", "credential": "tok-real-long", "account_uuid": "u1"},
                          headers)
    assert status == 201

    status, bundle = _request(f"{base}/api/export", "POST",
                               {"include_profiles": True, "include_settings": True, "include_activity": False,
                                "passphrase": "correct horse battery staple"},
                               headers)
    assert status == 200
    assert bundle["encrypted"] is True

    status, preview = _request(f"{base}/api/import/preview", "POST",
                                {"bundle": json.dumps(bundle), "passphrase": "correct horse battery staple"},
                                headers)
    assert status == 200
    assert len(preview["profiles"]) == 1
    assert preview["profiles"][0]["name"] == "Personal Max"
    assert "credential" not in json.dumps(preview)  # never leaks a secret into the preview response

    # Reset so apply produces a fresh add, not a dedup skip.
    _request(f"{base}/api/reset", "POST", {}, headers)

    status, result = _request(f"{base}/api/import/apply", "POST",
                               {"bundle": json.dumps(bundle), "passphrase": "correct horse battery staple",
                                "import_profiles": True, "import_settings": True},
                               headers)
    assert status == 200
    assert result["result"]["profiles_added"] == 1

    status, profiles = _request(f"{base}/api/profiles")
    assert status == 200
    assert len(profiles["profiles"]) == 1
    assert profiles["profiles"][0]["name"] == "Personal Max"


def test_import_preview_wrong_passphrase(running_server):
    base, csrf = running_server
    headers = {"X-CSRF-Token": csrf}
    _request(f"{base}/api/profiles", "POST",
             {"name": "X", "kind": "oauth", "credential": "tok-long-enough", "account_uuid": "u1"}, headers)
    status, bundle = _request(f"{base}/api/export", "POST",
                               {"include_profiles": True, "include_settings": False, "include_activity": False,
                                "passphrase": "right-pass"}, headers)
    status, payload = _request(f"{base}/api/import/preview", "POST",
                                {"bundle": json.dumps(bundle), "passphrase": "wrong-pass"}, headers)
    assert status == 401
    assert payload["error"] == "wrong_passphrase"


@pytest.mark.parametrize('change', ['changed', 'same', 'omitted', 'disabled', 'invalid'])
def test_import_invalidates_both_substitution_caches_only_when_parity_changes(running_server, monkeypatch, change):
    from claude_unlimited import config, openai_bridge as bridge
    base, csrf = running_server
    initial = [{'claude_model': 'claude-fable-5-1', 'model': 'gpt-5.6-terra', 'effort': 'high'}]
    desired = [{'claude_model': 'claude-fable-5-1', 'model': 'gpt-5.6-sol', 'effort': 'max'}]
    config.update_settings(model_parity=initial)
    model_cache = {'gpt-5.6-sol': 'gpt-5.6-terra'}
    effort_cache = {('fixture.invalid', 'gpt-5.6-sol', 'max'): 'high'}
    monkeypatch.setattr(bridge, '_MODEL_SUBSTITUTIONS', model_cache.copy())
    monkeypatch.setattr(bridge, '_EFFORT_SUBSTITUTIONS', effort_cache.copy())
    settings = {'model_parity': desired}
    if change == 'same':
        settings['model_parity'] = initial
    elif change == 'omitted':
        settings = {'language': 'de'}
    elif change == 'invalid':
        settings['model_parity'] = [{'claude_model': 'claude-fable-5-1', 'source_effort': 'max'}]
    bundle = json.dumps({'bundle_version': export_import.BUNDLE_VERSION, 'encrypted': False,
                         'data': {'settings': settings}})
    status, _ = _request(f'{base}/api/import/apply', 'POST',
                         {'bundle': bundle, 'import_settings': change != 'disabled'},
                         {'X-CSRF-Token': csrf})
    assert status == (400 if change == 'invalid' else 200)
    assert config.load_pool().settings.model_parity == (desired if change == 'changed' else initial)
    assert bridge._MODEL_SUBSTITUTIONS == ({} if change == 'changed' else model_cache)
    assert bridge._EFFORT_SUBSTITUTIONS == ({} if change == 'changed' else effort_cache)


def test_model_map_serves_curated_reference_even_with_custom_saved_rows(running_server):
    from claude_unlimited import config
    base, _ = running_server
    config.update_settings(model_parity=[{'claude_model': 'claude-custom', 'model': 'custom', 'effort': 'max'}])
    status, data = _request(f'{base}/api/codex/model-map')
    assert status == 200
    assert [(r['claude_model'], r['model'], r['effort']) for r in data['curated_defaults']] == [
        ('claude-fable-5-1', 'gpt-5.6-sol', 'medium'),
        ('claude-opus-5-5', 'gpt-5.6-terra', 'high'),
        ('claude-sonnet-5', 'gpt-5.6-terra', 'medium'),
        ('claude-haiku-4-5', 'gpt-5.6-luna', 'low'),
    ]
