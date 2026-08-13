"""RPC endpoint resolution: SOLANA_RPC_URL (OOBE) vs legacy Helius fallback.

These tests pin the resolution rules in Settings.solana_rpc_endpoint /
solana_rpc_headers so the Solana JSON-RPC provider can be swapped without
touching any business logic — the payment listener, payouts and treasury
health all go through this one seam.
"""

from app.config import Settings

OOBE_MAINNET = "https://us-1-mainnet.oobeprotocol.ai"

# Any env vars that could leak into a Settings() instance from the host (e.g.
# a developer's .env file) must be cleared so these tests are deterministic.
_ENV_KEYS = (
    "SOLANA_RPC_URL",
    "SOLANA_RPC_API_KEY",
    "HELIUS_RPC_URL",
    "HELIUS_API_KEY",
)


def _settings(monkeypatch, **overrides) -> Settings:
    # Build Settings with .env reading disabled and ambient env vars cleared,
    # so these tests are hermetic regardless of a developer's .env file or
    # shell environment. Values not overridden fall back to Settings defaults.
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return Settings(
        _env_file=None,
        SUPABASE_URL="https://x.supabase.co",
        SUPABASE_SERVICE_KEY="svc",
        JWT_SECRET="secret",
        **overrides,
    )


def test_default_endpoint_is_oobe_gateway(monkeypatch):
    s = _settings(monkeypatch)
    assert s.solana_rpc_endpoint == OOBE_MAINNET
    # No API key configured -> no auth header (the gateway will 401 until the
    # operator sets SOLANA_RPC_API_KEY, which is the documented behaviour).
    assert s.solana_rpc_headers == {}


def test_oobe_key_sent_as_bearer_header(monkeypatch):
    s = _settings(monkeypatch, SOLANA_RPC_API_KEY="sk_live_abc123")
    assert s.solana_rpc_headers == {"Authorization": "Bearer sk_live_abc123"}
    # The key must NOT leak into the URL.
    assert "sk_live_abc123" not in s.solana_rpc_endpoint


def test_explicit_solana_rpc_url_wins_over_helius(monkeypatch):
    # Operator points SOLANA_RPC_URL at their own provider (e.g. Helius).
    s = _settings(
        monkeypatch,
        SOLANA_RPC_URL="https://mainnet.helius-rpc.com",
        HELIUS_RPC_URL="https://should-not-be-used.com",
        HELIUS_API_KEY="helius-key",
    )
    assert s.solana_rpc_endpoint == "https://mainnet.helius-rpc.com"
    # The Helius key is NOT appended because SOLANA_RPC_URL is explicit.
    assert "api-key" not in s.solana_rpc_endpoint


def test_legacy_helius_fallback_appends_api_key_query_param(monkeypatch):
    # Default SOLANA_RPC_URL + HELIUS_RPC_URL set -> legacy path, key in query.
    s = _settings(
        monkeypatch,
        HELIUS_RPC_URL="https://devnet.helius-rpc.com",
        HELIUS_API_KEY="abc123",
    )
    assert s.solana_rpc_endpoint == "https://devnet.helius-rpc.com?api-key=abc123"
    # Legacy path does not send a Bearer header.
    assert s.solana_rpc_headers == {}


def test_legacy_helius_fallback_keeps_existing_query_params(monkeypatch):
    s = _settings(
        monkeypatch,
        HELIUS_RPC_URL="https://helius-rpc.com?x=1",
        HELIUS_API_KEY="abc",
    )
    assert s.solana_rpc_endpoint == "https://helius-rpc.com?x=1&api-key=abc"


def test_no_key_no_query_param(monkeypatch):
    # Explicit non-default Helius URL with empty key -> no query param appended.
    s = _settings(monkeypatch, HELIUS_RPC_URL="https://devnet.helius-rpc.com", HELIUS_API_KEY="")
    assert s.solana_rpc_endpoint == "https://devnet.helius-rpc.com"


def test_default_helius_means_oobe(monkeypatch):
    # Both SOLANA_RPC_URL and HELIUS_RPC_URL at their defaults -> OOBE gateway,
    # not Helius. The legacy fallback only kicks in for an explicit Helius URL.
    s = _settings(monkeypatch)
    assert s.solana_rpc_endpoint == OOBE_MAINNET
    assert s.solana_rpc_headers == {}
