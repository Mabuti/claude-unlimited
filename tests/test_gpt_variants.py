"""Cross-module regressions for the local gpt-6.1-sol model variants."""

import importlib.util
import json
from pathlib import Path

from claude_unlimited.config import Profile
from claude_unlimited.openai_bridge import run
from claude_unlimited.openai_credential import StoredOpenAICredential, encode
import claude_unlimited.openai_bridge as bridge_module


class _Response:
    status = 200

    def __init__(self):
        self.body = b'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{}}}\n\n'
        self.position = 0

    def getheaders(self):
        return []

    def read(self, size=None):
        if size is None:
            size = len(self.body) - self.position
        chunk = self.body[self.position:self.position + size]
        self.position += len(chunk)
        return chunk


class _Connection:
    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.requests = []
        self.response = _Response()

    def request(self, method, path, body=None, headers=None):
        self.requests.append({"method": method, "path": path, "body": body, "headers": headers})

    def getresponse(self):
        return self.response

    def close(self):
        pass


def test_272k_variant_is_translated_to_the_real_upstream_model(monkeypatch):
    connections = []

    def factory(host, port, timeout=None):
        connection = _Connection(host, port, timeout)
        connections.append(connection)
        return connection

    monkeypatch.setattr(bridge_module.http.client, "HTTPSConnection", factory)
    bridge_module._MODEL_SUBSTITUTIONS.clear()
    profile = Profile(id="p", name="Codex", kind="codex", auth_mode="chatgpt_subscription",
                      codex_model="gpt-6.1-sol-272k")
    credential = encode(StoredOpenAICredential(
        access_token="test-access-token", refresh_token=None, account_id="test-account",
        id_token=None))
    body = json.dumps({"model": "claude-fable-5", "messages": [{"role": "user", "content": "hi"}]}).encode()

    result = run(profile, credential, body)
    list(result.body_chunks)

    assert json.loads(connections[0].requests[0]["body"])["model"] == "gpt-6.1-sol"


def _checker_module():
    path = Path(__file__).parents[1] / "scripts" / "check_gpt_windows.py"
    spec = importlib.util.spec_from_file_location("check_gpt_windows_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_window_checker_rejects_a_variant_cap_above_the_cached_real_window(tmp_path, monkeypatch):
    checker = _checker_module()
    cache = tmp_path / "models_cache.json"
    cache.write_text(json.dumps({"fetched_at": "test", "models": [{
        "slug": "gpt-6.1-sol", "visibility": "list",
        "context_window": 500_000, "max_context_window": 872_000,
        "effective_context_window_percent": 95,
    }]}), encoding="utf-8")
    monkeypatch.setattr(checker, "CACHE", str(cache))
    monkeypatch.setattr(checker.gpt_windows, "CODEX_MODEL_VARIANTS",
                        {"gpt-6.1-sol-272k": ("gpt-6.1-sol", 500_001)})

    assert checker.main() == 1
