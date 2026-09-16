from datetime import datetime, timezone

from claude_unlimited.observation import (
    AuthInvalid,
    ProviderUnavailable,
    QuotaExhausted,
    ShortRateLimit,
    Unknown,
    UsageSnapshot,
    classify,
)

NOW = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)


def test_success_with_usage_headers_yields_usage_snapshot():
    # The header shape Anthropic sends: a 0-1 utilization float and a Unix
    # epoch reset, not remaining/limit.
    headers = {
        "anthropic-ratelimit-unified-5h-utilization": "0.61",
        "anthropic-ratelimit-unified-5h-reset": "1787191800",
    }
    obs = classify(200, headers, NOW)
    assert isinstance(obs, UsageSnapshot)
    assert obs.percent == 61.0
    assert obs.confidence == "measured"
    assert obs.resets_at is not None
    assert obs.resets_at.year == 2026


def test_reset_header_parsed_as_unix_epoch_not_iso8601():
    headers = {"anthropic-ratelimit-unified-5h-utilization": "0.5",
               "anthropic-ratelimit-unified-5h-reset": "1787191800"}
    obs = classify(200, headers, NOW)
    assert obs.resets_at == datetime.fromtimestamp(1787191800, tz=timezone.utc)


def test_reset_header_falls_back_to_iso8601_if_ever_sent_that_way():
    headers = {"anthropic-ratelimit-unified-5h-utilization": "0.5",
               "anthropic-ratelimit-unified-5h-reset": "2026-08-20T04:00:00Z"}
    obs = classify(200, headers, NOW)
    assert obs.resets_at == datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc)


def test_success_without_usage_headers_yields_unknown():
    obs = classify(200, {}, NOW)
    assert isinstance(obs, Unknown)
    assert obs.status_code == 200


def test_429_with_rejected_status_is_quota_exhausted_not_rate_limit():
    headers = {"anthropic-ratelimit-unified-5h-status": "rejected"}
    obs = classify(429, headers, NOW)
    assert isinstance(obs, QuotaExhausted)


def test_429_without_rejected_status_is_short_rate_limit():
    headers = {"retry-after": "12"}
    obs = classify(429, headers, NOW)
    assert isinstance(obs, ShortRateLimit)
    assert obs.retry_after_seconds == 12.0


def test_429_with_healthy_5h_status_and_no_retry_after_is_unknown_not_short_rate_limit():
    # Regression for the "healthy account gets benched" bug: Anthropic sent
    # a 429 with NO Retry-After but WITH a 5h-status of "allowed" (measured
    # at 5% utilization) — a real quota/rate-limit 429 always carries either
    # a rejected window or a Retry-After to back off for. Getting neither,
    # with an affirmatively-healthy window present, means this 429 is not
    # about this Profile's quota at all. Falling through to
    # ShortRateLimit(None) here is what put a 5%-utilized Team account into
    # an escalating COOLDOWN (up to 30 minutes) for no reason the headers
    # support.
    headers = {"anthropic-ratelimit-unified-5h-status": "allowed"}
    obs = classify(429, headers, NOW)
    assert isinstance(obs, Unknown)
    assert obs.status_code == 429


def test_429_with_no_ratelimit_headers_at_all_is_still_short_rate_limit():
    # The opposite of the case above: headers genuinely absent (no unified
    # status of any kind, no Retry-After) must keep behaving exactly as
    # before — ShortRateLimit(None), so the Router's existing escalating
    # cooldown still applies to a plain, headerless 429.
    obs = classify(429, {}, NOW)
    assert isinstance(obs, ShortRateLimit)
    assert obs.retry_after_seconds is None


def test_429_7d_rejected_with_5h_allowed_is_still_quota_exhausted():
    # A Profile can have 5h headroom left while its weekly cap is spent.
    # `status_5h or status_7d` would pick the truthy "allowed" and never look
    # at 7d, misclassifying this as a rate-limit blip the Router would
    # cooldown-and-retry forever instead of rotating away from.
    headers = {
        "anthropic-ratelimit-unified-5h-status": "allowed",
        "anthropic-ratelimit-unified-7d-status": "rejected",
        "anthropic-ratelimit-unified-7d-reset": "1787191800",
    }
    obs = classify(429, headers, NOW)
    assert isinstance(obs, QuotaExhausted)
    # The 7d window's own reset time, not the (here-absent) 5h one.
    assert obs.resets_at == datetime.fromtimestamp(1787191800, tz=timezone.utc)


def test_429_5h_rejected_uses_5h_reset_not_7d():
    headers = {
        "anthropic-ratelimit-unified-5h-status": "rejected",
        "anthropic-ratelimit-unified-5h-reset": "1787191800",
        "anthropic-ratelimit-unified-7d-status": "allowed",
        "anthropic-ratelimit-unified-7d-reset": "1787999999",
    }
    obs = classify(429, headers, NOW)
    assert isinstance(obs, QuotaExhausted)
    assert obs.resets_at == datetime.fromtimestamp(1787191800, tz=timezone.utc)


def test_529_is_provider_unavailable_not_quota_exhausted():
    obs = classify(529, {"retry-after": "5"}, NOW)
    assert isinstance(obs, ProviderUnavailable)


def test_401_is_auth_invalid():
    assert isinstance(classify(401, {}, NOW), AuthInvalid)


def test_403_is_not_auth_invalid():
    # 403 (permission_error) means the credential is valid but not scoped for
    # this request, most commonly a model the key cannot access. Treating it
    # like a 401 would mark a healthy Profile "needs re-authentication" just
    # because `/model` picked an unavailable model.
    obs = classify(403, {}, NOW)
    assert not isinstance(obs, AuthInvalid)
    assert isinstance(obs, Unknown)
    assert obs.status_code == 403


def test_unrecognized_status_is_unknown_not_a_crash():
    obs = classify(418, {}, NOW)
    assert isinstance(obs, Unknown)
    assert obs.status_code == 418


def test_malformed_header_values_degrade_to_unknown_not_a_crash():
    headers = {"anthropic-ratelimit-unified-5h-utilization": "not-a-number"}
    obs = classify(200, headers, NOW)
    assert isinstance(obs, Unknown)


def test_429_headerless_with_should_retry_hint_is_unknown_not_cooldown():
    """The shape measured 2026-09-15 against a healthy OAuth account: 429,
    no unified-ratelimit window at all, no Retry-After, but
    x-should-retry: true. Must NOT bench the Profile -- the old behaviour
    escalated this into a 30-minute COOLDOWN and emptied the pool."""
    obs = classify(429, {"x-should-retry": "true"}, NOW)
    assert isinstance(obs, Unknown)
    assert obs.status_code == 429


def test_429_rejected_window_beats_should_retry_hint():
    """A rejected window is authoritative even if the retry hint is set."""
    obs = classify(429, {"x-should-retry": "true",
                         "anthropic-ratelimit-unified-5h-status": "rejected",
                         "anthropic-ratelimit-unified-5h-reset": "1787191800"}, NOW)
    assert isinstance(obs, QuotaExhausted)


def test_429_retry_after_beats_should_retry_hint():
    """An explicit Retry-After is a real backoff instruction; honour it."""
    obs = classify(429, {"x-should-retry": "true", "retry-after": "42"}, NOW)
    assert isinstance(obs, ShortRateLimit)
    assert obs.retry_after_seconds == 42


def test_should_retry_header_survives_the_response_header_filter():
    """classify() only ever sees headers that proxy.filter_response_headers
    let through. The fix is inert if x-should-retry is filtered out before
    it arrives -- which is exactly what happened before it was added to
    ALLOWED_HEADERS, so this guards the wiring, not just the logic."""
    from claude_unlimited.proxy import filter_response_headers

    filtered = filter_response_headers({"X-Should-Retry": "true", "x-irrelevant": "drop me"})
    assert filtered.get("x-should-retry") == "true"
    assert "x-irrelevant" not in filtered
    assert isinstance(classify(429, filtered, NOW), Unknown)
