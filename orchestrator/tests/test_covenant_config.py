"""OpenCovenant config: defaults are opt-in and never disturb existing flows."""

from app.config import Settings


def _settings(monkeypatch, **overrides) -> Settings:
    for k in (
        "COVENANT_MCP_URL",
        "COVENANT_MCP_TIMEOUT_S",
        "COVENANT_MIN_REPUTATION",
        "COVENANT_ENABLE_ATTESTATION",
        "COVENANT_PROVIDER_WALLET_ADDRESS",
    ):
        monkeypatch.delenv(k, raising=False)
    return Settings(
        _env_file=None,
        SUPABASE_URL="https://x.supabase.co",
        SUPABASE_SERVICE_KEY="svc",
        JWT_SECRET="secret",
        **overrides,
    )


def test_covenant_attestation_off_by_default(monkeypatch):
    s = _settings(monkeypatch)
    assert s.COVENANT_ENABLE_ATTESTATION is False
    # The wallet is empty, so even if the flag were flipped the check would be
    # skipped — the existing registration flow is untouched out of the box.
    assert s.COVENANT_PROVIDER_WALLET_ADDRESS == ""
    assert s.COVENANT_MCP_URL == "https://mcp.opencovenant.org/mcp"
    assert s.COVENANT_MIN_REPUTATION == 0
    assert s.COVENANT_MCP_TIMEOUT_S == 10.0


def test_covenant_flags_are_configurable(monkeypatch):
    s = _settings(
        monkeypatch,
        COVENANT_ENABLE_ATTESTATION=True,
        COVENANT_PROVIDER_WALLET_ADDRESS="7G73PLhKvAPBGTzG5ESAE4coE7QrVeTTKfhTxQZbyGgC",
        COVENANT_MIN_REPUTATION=100,
        COVENANT_MCP_URL="https://other.example/mcp",
        COVENANT_MCP_TIMEOUT_S=5.0,
    )
    assert s.COVENANT_ENABLE_ATTESTATION is True
    assert s.COVENANT_PROVIDER_WALLET_ADDRESS == "7G73PLhKvAPBGTzG5ESAE4coE7QrVeTTKfhTxQZbyGgC"
    assert s.COVENANT_MIN_REPUTATION == 100
    assert s.COVENANT_MCP_URL == "https://other.example/mcp"
    assert s.COVENANT_MCP_TIMEOUT_S == 5.0
