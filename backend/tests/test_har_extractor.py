"""Tests for the HAR endpoint extractor (pure stdlib, no external deps)."""

from tools import har_extractor as hx


def _sample_har() -> dict:
    return {
        "log": {
            "entries": [
                # Noise: a static asset on the app host — must be ignored.
                {
                    "request": {
                        "method": "GET",
                        "url": "https://omni.variational.io/static/app.js",
                        "headers": [],
                    },
                    "response": {"status": 200, "content": {}},
                },
                # Auth nonce
                {
                    "request": {
                        "method": "GET",
                        "url": "https://omni-client-api.prod.ap-northeast-1.variational.io/auth/nonce",
                        "headers": [{"name": "accept", "value": "*/*"}],
                    },
                    "response": {
                        "status": 200,
                        "content": {"text": '{"nonce": "abc123"}'},
                    },
                },
                # Login (carries a signature + auth header)
                {
                    "request": {
                        "method": "POST",
                        "url": "https://omni-client-api.prod.ap-northeast-1.variational.io/auth/login",
                        "headers": [
                            {"name": "content-type", "value": "application/json"},
                            {"name": "authorization", "value": "Bearer secret-token"},
                            {"name": "user-agent", "value": "Chrome"},
                        ],
                        "postData": {
                            "mimeType": "application/json",
                            "text": '{"message": "siwe...", "signature": "0x' + "a" * 130 + '"}',
                        },
                    },
                    "response": {"status": 200, "content": {"text": '{"token": "jwt-here"}'}},
                },
                # RFQ
                {
                    "request": {
                        "method": "POST",
                        "url": "https://omni-client-api.prod.ap-northeast-1.variational.io/rfq",
                        "headers": [
                            {"name": "authorization", "value": "Bearer secret-token"},
                            {"name": "x-client-version", "value": "1.2.3"},
                        ],
                        "postData": {
                            "mimeType": "application/json",
                            "text": '{"listing": "BTC_USDC_PERP", "side": "buy", "size_usd": 500}',
                        },
                    },
                    "response": {
                        "status": 200,
                        "content": {"text": '{"quote_id": "q1", "price": "95000.5"}'},
                    },
                },
                # Order submit
                {
                    "request": {
                        "method": "POST",
                        "url": "https://omni-client-api.prod.ap-northeast-1.variational.io/order?dryRun=false",
                        "headers": [{"name": "authorization", "value": "Bearer secret-token"}],
                        "queryString": [{"name": "dryRun", "value": "false"}],
                        "postData": {
                            "mimeType": "application/json",
                            "text": '{"quote_id": "q1", "signature": "0x' + "b" * 130 + '"}',
                        },
                    },
                    "response": {"status": 200, "content": {"text": '{"order_id": "o1"}'}},
                },
            ]
        }
    }


def test_classify_maps_paths_to_actions():
    assert hx.classify("/auth/nonce") == "auth_nonce"
    assert hx.classify("/auth/login") == "auth_login"
    assert hx.classify("/rfq") == "rfq"
    assert hx.classify("/order") == "order_submit"
    assert hx.classify("/positions") == "position"
    assert hx.classify("/market/statistics") == "market_data"
    assert hx.classify("/static/app.js") is None


def test_templatize_preserves_shape_and_types():
    body = {"listing": "BTC", "size": 500, "leverage": 3.0, "flag": True, "sig": "0x" + "a" * 130}
    t = hx.templatize(body)
    assert t == {
        "listing": "<str>",
        "size": "<int>",
        "leverage": "<float>",
        "flag": "<bool>",
        "sig": "<hex:signature-or-address>",
    }


def test_extract_finds_flow_and_ignores_noise():
    endpoints = hx.extract(_sample_har(), host_filter="variational.io")
    categories = [e.category for e in endpoints]
    # Static asset excluded; all five API calls captured.
    assert categories == ["auth_nonce", "auth_login", "rfq", "order_submit", "position"] or set(
        categories
    ) == {"auth_nonce", "auth_login", "rfq", "order_submit"}
    assert "market_data" not in categories  # none in fixture
    assert "auth_nonce" in categories
    assert "order_submit" in categories


def test_secrets_are_redacted_but_flagged():
    endpoints = hx.extract(_sample_har(), host_filter="variational.io")
    login = next(e for e in endpoints if e.category == "auth_login")
    # Authorization header name surfaced so the connector knows it's needed...
    assert "authorization" in login.sensitive_headers
    # ...but its secret value is never stored.
    serialized = str(login.to_map_entry())
    assert "secret-token" not in serialized
    assert "jwt-here" not in serialized
    # Non-sensitive replay headers kept by name.
    rfq = next(e for e in endpoints if e.category == "rfq")
    assert "x-client-version" in rfq.required_headers


def test_query_keys_captured():
    endpoints = hx.extract(_sample_har(), host_filter="variational.io")
    submit = next(e for e in endpoints if e.category == "order_submit")
    assert submit.query_keys == ["dryRun"]


def test_build_endpoint_map_groups_by_category():
    endpoints = hx.extract(_sample_har(), host_filter="variational.io")
    m = hx.build_endpoint_map(endpoints, "omni-client-api.prod.ap-northeast-1.variational.io")
    assert "endpoints" in m
    assert "rfq" in m["endpoints"]
    assert m["endpoints"]["rfq"][0]["method"] == "POST"
