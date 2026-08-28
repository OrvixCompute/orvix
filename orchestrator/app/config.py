"""Application configuration loaded from environment variables via pydantic-settings."""

import json
from decimal import Decimal
from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central settings object. Values are read from the environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Core (required) ---------------------------------------------------
    SUPABASE_URL: str = Field(..., description="Supabase project URL")
    SUPABASE_SERVICE_KEY: str = Field(..., description="Supabase service_role key (server-side only)")
    JWT_SECRET: str = Field(..., description="Secret used to sign HS256 JWTs")
    ENVIRONMENT: str = Field("dev", description="Runtime environment: dev or prod")

    # --- Core (optional) ---------------------------------------------------
    LOG_LEVEL: str = Field("INFO", description="Loguru log level")
    CORS_ORIGINS: str = Field("*", description="Comma-separated list of allowed CORS origins")
    REDIS_URL: str = Field(
        "",
        description=(
            "Redis connection URL (e.g. redis://localhost:6379/0). When set, "
            "rate limiting and challenge storage use Redis instead of in-memory "
            "dicts. Empty falls back to in-memory (single-worker only)."
        ),
    )

    # --- Observability (optional) ------------------------------------------
    SENTRY_DSN: str = Field(
        "", description="Sentry DSN for error tracking; disabled when empty"
    )
    SENTRY_TRACES_SAMPLE_RATE: float = Field(
        0.1, description="Fraction of transactions traced by Sentry (0.0–1.0)"
    )

    # --- Auth --------------------------------------------------------------
    JWT_ALGORITHM: str = Field("HS256", description="JWT signing algorithm")
    JWT_EXPIRY_HOURS: int = Field(24, description="JWT lifetime in hours")

    # --- Solana / billing (optional until the payment listener is on) ------
    TREASURY_WALLET_ADDRESS: str = Field("", description="Treasury wallet that receives USDC deposits")
    USDC_MINT_ADDRESS: str = Field("", description="SPL mint address of the USDC token")
    ORVX_MINT_ADDRESS: str = Field("", description="SPL mint address of the ORVX token (staking deposits)")
    # Solana JSON-RPC provider. Defaults to the OOBE Protocol Synapse gateway
    # (mainnet US) — set SOLANA_RPC_URL to any compliant JSON-RPC endpoint
    # (e.g. Helius) and it is used instead. SOLANA_RPC_API_KEY is sent as a
    # Bearer token, matching the Synapse gateway's auth scheme.
    SOLANA_RPC_URL: str = Field(
        "https://us-1-mainnet.oobeprotocol.ai", description="Solana JSON-RPC endpoint (Synapse gateway by default)"
    )
    SOLANA_RPC_API_KEY: str = Field(
        "", description="API key for the Solana RPC endpoint (sent as Bearer token; blank for public endpoints)"
    )
    # Backwards compatibility: legacy Helius configuration. HELIUS_RPC_URL /
    # HELIUS_API_KEY are honored only when SOLANA_RPC_URL is left at its default.
    HELIUS_API_KEY: str = Field("", description="Legacy Helius API key (used when SOLANA_RPC_URL is default)")
    HELIUS_RPC_URL: str = Field(
        "https://mainnet.helius-rpc.com", description="Legacy Helius RPC base URL (used when SOLANA_RPC_URL is default)"
    )
    POLLING_INTERVAL_SECONDS: int = Field(15, description="Payment listener poll interval")
    ENABLE_PAYMENT_LISTENER: bool = Field(
        False, description="Start the Solana payment listener on app startup"
    )

    # --- Provider / payouts ------------------------------------------------
    PROVIDER_REWARD_PERCENTAGE: int = Field(
        70, description="Percentage of a job's cost paid to the provider"
    )
    MIN_WITHDRAW_AMOUNT_USDC: float = Field(100.0, description="Minimum withdrawal amount")
    AUTO_APPROVE_MAX_USDC: float = Field(
        10000.0, description="Withdrawals above this require manual approval"
    )
    MAX_WITHDRAWALS_PER_DAY: int = Field(5, description="Per-user daily withdrawal cap")
    PAYOUT_STUB: bool = Field(
        True, description="Simulate on-chain payouts instead of sending real transactions"
    )
    PAYOUT_INTERVAL_SECONDS: int = Field(300, description="Payout worker interval")
    PAYOUT_CONFIRM_MAX_ATTEMPTS: int = Field(
        30, description="Times to poll a payout signature for confirmation (2s apart) before flagging for review"
    )
    TREASURY_KEYPAIR_PATH: str = Field(
        "", description="Path to the treasury keypair file (only if PAYOUT_STUB=false)"
    )
    ENABLE_PAYOUT_WORKER: bool = Field(
        False, description="Start the withdrawal payout worker on startup"
    )

    # --- OpenCovenant trust (optional; opt-in node attestation) ------------
    # OpenCovenant exposes on-chain trust facts (wallet reputation, agent
    # identity, attestation verify) over a remote MCP. All tools are read-only
    # and need no credentials. This is strictly opt-in: when the URL is left at
    # its default and the feature flag is off, node registration behaves
    # exactly as before — the attestation never runs and nothing blocks.
    COVENANT_MCP_URL: str = Field(
        "https://mcp.opencovenant.org/mcp",
        description="Remote MCP endpoint for OpenCovenant trust checks",
    )
    COVENANT_MCP_TIMEOUT_S: float = Field(
        10.0, description="Timeout for each OpenCovenant MCP call"
    )
    COVENANT_MIN_REPUTATION: int = Field(
        0,
        description=(
            "Minimum covenant_reputation score a node needs to register when "
            "COVENANT_ENABLE_ATTESTATION is true (0 disables the gate entirely; "
            "the check then runs but never rejects). A score of 0 means the "
            "wallet has no on-chain settlement history yet."
        ),
    )
    COVENANT_ENABLE_ATTESTATION: bool = Field(
        False,
        description=(
            "When true, node registration runs an OpenCovenant reputation "
            "check against the provider's wallet. Defaults to false so the "
            "existing registration flow is untouched. The check is fail-soft: "
            "a network error or timeout records 'no attestation' but never "
            "blocks registration. Requires COVENANT_PROVIDER_WALLET_ADDRESS to "
            "resolve the provider's wallet."
        ),
    )
    COVENANT_PROVIDER_WALLET_ADDRESS: str = Field(
        "",
        description=(
            "Solana wallet used for covenant_reputation checks on node "
            "registration (a provider's wallet, e.g. their treasury address). "
            "Empty disables the check even when COVENANT_ENABLE_ATTESTATION is "
            "true."
        ),
    )

    # --- Inference receipts (signed verdicts) -------------------------------
    # Base64 32-byte Ed25519 seed used to sign per-request receipts
    # (X-Orvix-Receipt). Empty disables receipt signing; the public key is
    # served at GET /v1/verify/public-key for offline verification.
    RECEIPT_SIGNING_KEY: str = Field(
        "",
        description=(
            "Base64 32-byte Ed25519 seed for signing inference receipts. "
            "Empty disables signing. Generate with: "
            "python -c \"import base64,os;print(base64.b64encode(os.urandom(32)).decode())\""
        ),
    )

    # --- Staking / tokenomics (whitepaper alignment) -----------------------
    REQUIRE_STAKE_FOR_PROVIDER: bool = Field(
        False,
        description="When false (alpha), provider register skips the staked_orvx minimum check",
    )
    # 0.2% of the fixed 1,000,000,000 supply. Operator's number, set while the
    # token is early and the stake is cheap in dollar terms; the intent is to
    # revisit it as market cap grows. Worth knowing what it implies: a per-provider
    # minimum caps how many providers the supply can ever seat — 0.2% means 500 at
    # 100% of supply staked, and realistically a few hundred. Lowering it later
    # cannot claw back stake already locked by early providers, so a reduction is
    # the easy direction and a rise is the one that strands people.
    PROVIDER_MIN_STAKE_ORVX: int = Field(
        2_000_000, description="Minimum ORVX a user must stake to register as a provider"
    )
    STAKE_INTENT_TTL_MINUTES: int = Field(
        30, description="How long a staking intent (and its memo) stays valid"
    )
    ADMIN_API_KEY: str = Field(
        "", description="Shared secret for admin buyback/burn endpoints (X-Admin-Key)"
    )

    # --- Buyback & burn (admin tooling) ------------------------------------
    ORVX_DECIMALS: int = Field(6, description="On-chain decimals of the ORVX SPL token")
    USDC_DECIMALS: int = Field(6, description="On-chain decimals of the USDC SPL token")
    BUYBACK_STUB: bool = Field(
        True, description="Simulate the USDC->ORVX swap instead of sending a real transaction"
    )
    BURN_STUB: bool = Field(
        True, description="Simulate the ORVX burn transfer instead of sending a real transaction"
    )
    BUYBACK_MAX_SLIPPAGE_BPS: int = Field(
        100, description="Abort a buyback if Jupiter price impact exceeds this (basis points)"
    )
    BUYBACK_MIN_INTERVAL_SECONDS: int = Field(
        300, description="Minimum seconds between buyback executions (drain guard)"
    )
    JUPITER_QUOTE_API: str = Field(
        "https://quote-api.jup.ag/v6", description="Jupiter v6 quote/swap API base URL"
    )
    INCINERATOR_ADDRESS: str = Field(
        "1nc1nerator11111111111111111111111111111111",
        description="Solana incinerator address that burns SPL tokens sent to it",
    )
    AUDIT_LOG_DIR: str = Field(
        "", description="If set, buyback/burn executions are appended to dated files here"
    )

    # --- Quotas / holder gating --------------------------------------------
    ORVX_HOLDER_THRESHOLD: int = Field(
        10000, description="Minimum ORVX balance to count as a holder"
    )
    # These three defaults are what a caller gets for free before anything bills
    # them. They were written when nothing charged, so small numbers were merely
    # stingy. Chat and images both bill now, which turns a low default into a
    # deploy that starts taking money after one or two requests.
    #
    # They therefore track the operator's actual policy rather than sitting well
    # below it: a missing .env line should fail generous, not fail charging.
    CHAT_LIFETIME_FREE_LIMIT: int = Field(
        1000, description="Free lifetime chat requests for non-holders"
    )
    IMAGE_DAILY_LIMIT_HOLDER: int = Field(
        50, description="Daily image generations for holders (resets 00:00 UTC)"
    )
    IMAGE_DAILY_LIMIT_FALLBACK: int = Field(
        50, description="Daily image generations for everyone when ORVX_MINT_ADDRESS is unset"
    )
    # Priced per 1024x1024 image and scaled by pixel count, because that is what
    # drives the cost: a measured 1024x1024 generation takes ~2.3 s of GPU and
    # peaks near 19.6 GiB of VRAM, and both scale with area.
    #
    # 0.05 was set deliberately by the operator on 2026-08-07, replacing the 0.01
    # placeholder this shipped with. It is a pricing decision, not a derived one:
    # the GPU time is comparable to a chat completion, so the ratio to chat is a
    # judgement about what the two are worth rather than what they cost to serve.
    IMAGE_PRICE_USDC_PER_MEGAPIXEL: Decimal = Field(
        Decimal("0.05"),
        description=(
            "USDC charged per 1024x1024-equivalent image (1.048576 MP). Larger "
            "sizes cost proportionally more, smaller ones less. Applies only "
            "once a caller is past their free daily allowance."
        ),
    )
    HOLDER_CACHE_TTL_MINUTES: int = Field(
        15, description="How long a holder-status lookup is cached"
    )
    UPGRADE_URL: str = Field(
        "https://orvix.network/pricing", description="Shown when a quota is exhausted"
    )
    TOKENOMICS_URL: str = Field(
        "https://orvix.network/tokenomics", description="Shown when image access needs ORVX"
    )

    # --- Chat generation -----------------------------------------------
    CHAT_JOB_TIMEOUT: int = Field(
        60,
        description=(
            "Seconds to wait for a node to complete a chat job. Raise this if "
            "nodes swap between chat/image engines and the swap-back (e.g. a "
            "managed vLLM cold start) can exceed the default."
        ),
    )

    # --- Image generation --------------------------------------------------
    IMAGE_JOB_TIMEOUT: int = Field(
        90, description="Seconds to wait for a node to complete an image job"
    )
    IMAGE_STORAGE_DIR: str = Field(
        "/var/orvix/images", description="Local dir where generated images are saved"
    )
    PUBLIC_IMAGE_URL_BASE: str = Field(
        "https://orvix.network/images",
        description="Public base URL that maps to IMAGE_STORAGE_DIR (served by nginx)",
    )
    MAX_IMAGE_STORAGE_MB: int = Field(
        5000, description="Refuse new image jobs when IMAGE_STORAGE_DIR exceeds this size"
    )

    # --- Video generation --------------------------------------------------
    VIDEO_JOB_TIMEOUT: int = Field(
        600, description="Seconds to wait for a node to complete a video job (clips take minutes)"
    )
    VIDEO_STORAGE_DIR: str = Field(
        "/var/orvix/videos", description="Local dir where generated videos are saved"
    )
    PUBLIC_VIDEO_URL_BASE: str = Field(
        "https://orvix.network/videos",
        description="Public base URL that maps to VIDEO_STORAGE_DIR (served by nginx)",
    )
    MAX_VIDEO_STORAGE_MB: int = Field(
        20000, description="Refuse new video jobs when VIDEO_STORAGE_DIR exceeds this size"
    )
    VIDEO_DAILY_LIMIT_HOLDER: int = Field(
        5, description="Daily video generations for holders (resets 00:00 UTC)"
    )
    VIDEO_DAILY_LIMIT_FALLBACK: int = Field(
        5, description="Daily video generations for everyone when ORVX_MINT_ADDRESS is unset"
    )

    # --- Treasury architecture (cold/hot/payout) ---------------------------
    # TREASURY_WALLET_ADDRESS (above) is the HOT wallet: it receives incoming
    # USDC deposits and the payment listener subscribes to it. Do NOT reroute it.
    # TREASURY_KEYPAIR_PATH (above) is the HOT wallet's keypair file.
    TREASURY_MAIN_PUBLIC: str = Field(
        "", description="Cold-storage (main) wallet public key; private key stays OFFLINE"
    )
    PAYOUT_WALLET_PUBLIC: str = Field(
        "", description="Payout signer wallet public key"
    )
    PAYOUT_KEYPAIR_PATH: str = Field(
        "", description="Path to the payout signer keypair file (chmod 600); used by Session 3 payouts"
    )
    HOT_SWEEP_THRESHOLD_USDC: float = Field(
        100.0, description="Sweep hot->main once the hot USDC balance exceeds this"
    )
    HOT_SWEEP_MIN_KEEP_USDC: float = Field(
        10.0, description="Always leave at least this much USDC in the hot wallet as an operational buffer"
    )
    TREASURY_SWEEP_STUB: bool = Field(
        True, description="Simulate the hot->main sweep instead of sending a real transfer"
    )
    ENABLE_HOT_SWEEPER: bool = Field(
        False, description="Start the daily hot-wallet sweeper (usually run via systemd timer instead)"
    )

    # --- Treasury health thresholds ----------------------------------------
    # Defaults come from a measured mainnet payout, not estimates: paying a
    # provider for the first time cost 0.002044 SOL (destination ATA rent plus
    # fee), and a repeat payout costs only the ~0.000005 fee.
    TREASURY_MIN_PAYOUT_SOL: float = Field(
        0.02,
        description=(
            "Warn when the payout wallet's SOL drops below this. 0.02 covers "
            "roughly 10 first-time provider payouts at 0.002 SOL of ATA rent each."
        ),
    )
    TREASURY_MIN_PAYOUT_USDC: float = Field(
        5.0,
        description="Warn when the payout wallet's USDC float drops below this",
    )
    TREASURY_MIN_HOT_SOL: float = Field(
        0.005,
        description=(
            "Warn when the hot wallet's SOL drops below this. Hot only signs "
            "hot->main sweeps, so it needs fees but no ATA rent."
        ),
    )

    # --- Public network stats ----------------------------------------------
    NETWORK_STATS_CACHE_SECONDS: int = Field(
        30,
        description=(
            "How long GET /v1/network/stats is served from cache. The endpoint is "
            "public and unauthenticated, so the cache is what keeps the aggregation "
            "off the database on every page load. Set to 0 to disable."
        ),
    )
    NETWORK_STATS_WINDOW_HOURS: int = Field(
        24, description="Rolling window for the *_window counters in /v1/network/stats"
    )

    # --- Non-custodial user staking (Anchor program) ------------------------
    # Empty disables the /v1/staking/user/* endpoints (404 "not configured").
    USER_STAKING_PROGRAM_ID: str = Field(
        "", description="Anchor program ID for non-custodial user staking (base58)"
    )
    USER_STAKING_LOCK_DAYS: int = Field(
        7, description="Default lock period (days) offered when staking (3/7/14 allowed)"
    )
    USER_STAKING_VAULT_SEED: str = Field(
        "vault", description="PDA seed for the program-owned ORVX vault"
    )
    USER_STAKING_VAULT_AUTHORITY_SEED: str = Field(
        "vault_authority", description="PDA seed for the vault's token authority (signs CPI)"
    )
    USER_STAKING_STAKE_SEED: str = Field(
        "stake", description="PDA seed for per-user StakeAccount"
    )

    # --- Governance (Snapshot.org) -----------------------------------------
    GOVERNANCE_SNAPSHOT_SPACE: str = Field(
        "orvix", description="Snapshot space slug for ORVX governance"
    )
    GOVERNANCE_SNAPSHOT_URL: str = Field(
        "https://snapshot.box/#/orvix", description="Public Snapshot space URL"
    )

    # --- Token intelligence (scans, accumulation, monitors, webhooks) ------
    # The token-intel layer works with plain Solana JSON-RPC only. Mint
    # addresses do not appear in ordinary transfer transactions and plain RPC
    # cannot enumerate a mint's holders, so holder/whale analysis is anchored on
    # explicit watchlists rather than chain-wide enumeration.
    TOKEN_METADATA_PROGRAM_ID: str = Field(
        "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",
        description="Metaplex token-metadata program ID (metadata PDA derivation)",
    )
    TOKEN_POOLS_JSON: str = Field(
        "",
        description=(
            "JSON mapping mint -> list of liquidity-pool token-account addresses, "
            "e.g. {\"mint...\": [\"poolAta...\"]}. Used to estimate a token's "
            "liquidity from on-chain balances. Empty disables the liquidity estimate."
        ),
    )
    TOKEN_WHALE_WATCHLIST_JSON: str = Field(
        "",
        description=(
            "JSON array of wallet addresses tracked for accumulation / large-transfer "
            "analysis, e.g. [\"wallet...\"]. Empty disables whale inflow metrics."
        ),
    )
    INTEL_SCAN_CACHE_TTL_SECONDS: int = Field(
        600, description="How long token/wallet/accumulation scan results are cached"
    )
    INTEL_HOLDER_SNAPSHOT_TTL_SECONDS: int = Field(
        3600,
        description=(
            "How often the monitor worker refreshes holder snapshots for monitored "
            "tokens (0 disables automatic refresh; POST /v1/admin/intel/holder-snapshot "
            "always works)"
        ),
    )
    MAX_TOKEN_ACCOUNTS_PER_WALLET: int = Field(
        25, description="Cap on token accounts listed in a wallet analysis"
    )
    MAX_WALLET_TX_PARSE: int = Field(
        5, description="Cap on parsed transactions in a wallet's recent activity"
    )

    # --- Monitor agents + webhook alerts -----------------------------------
    ENABLE_MONITOR_WORKER: bool = Field(
        False, description="Start the monitor evaluation + webhook delivery worker"
    )
    MONITOR_WORKER_INTERVAL_SECONDS: int = Field(
        120, description="Monitor worker loop cadence"
    )
    MONITOR_MAX_PER_CYCLE: int = Field(
        50, description="Monitors evaluated per worker cycle"
    )
    MONITOR_DEFAULT_INTERVAL_MINUTES: int = Field(
        30, description="Default evaluation interval for monitors that don't set one"
    )
    WEBHOOK_MAX_ATTEMPTS: int = Field(
        5, description="Webhook delivery attempts before the delivery is marked failed"
    )
    WEBHOOK_RETRY_BASE_SECONDS: int = Field(
        30, description="Webhook backoff base: attempt n waits 2^(n-1) * base seconds"
    )
    WEBHOOK_TIMEOUT_SECONDS: int = Field(
        10, description="Timeout per webhook delivery request"
    )
    WEBHOOK_SIGNING_SECRET: str = Field(
        "",
        description=(
            "HMAC-SHA256 signing secret for webhook deliveries. When set, every "
            "delivery includes an X-Orvix-Signature header (hex HMAC of the raw "
            "JSON body); receivers verify it to authenticate the sender. Empty "
            "disables signing."
        ),
    )
    RESOLVE_HOLDING_METADATA: bool = Field(
        False,
        description=(
            "When true, wallet analysis resolves each holding's on-chain name/symbol "
            "via the Metaplex metadata program (one extra RPC per holding, capped). "
            "Off by default because it multiplies RPC cost on every wallet scan."
        ),
    )
    INTEL_AI_MODEL: str = Field(
        "qwen-2.5-7b",
        description=(
            "Chat model used by the token-intelligence analysis. The analysis is "
            "dispatched to a GPU node over the normal job path; when no node serves "
            "the model the analysis degrades to null instead of failing."
        ),
    )
    INTEL_AI_MAX_TOKENS: int = Field(
        500, description="Max completion tokens for a token-intelligence analysis"
    )
    INTEL_AI_TEMPERATURE: float = Field(
        0.3, description="Sampling temperature for token-intelligence analysis"
    )

    # --- Social intelligence (X/Twitter, DexScreener) -----------------------
    SOCIAL_CACHE_TTL_SECONDS: int = Field(
        300, description="How long social analysis results are cached"
    )
    X_BEARER_TOKEN: str = Field(
        "",
        description=(
            "Twitter/X API v2 bearer token. When set, social analysis includes "
            "tweet volume, follower count, and engagement metrics. Empty disables "
            "X/Twitter data (DexScreener data still works without a token)."
        ),
    )
    DEXSCREENER_API_URL: str = Field(
        "https://api.dexscreener.com/latest/dex",
        description="DexScreener API base URL for token social/volume data",
    )

    # --- Parsed helpers ----------------------------------------------------
    @staticmethod
    def _parse_json_list(raw: str, name: str) -> list[str]:
        """Parse a JSON-array config value into a list. Invalid JSON -> []."""
        if not raw.strip():
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(x) for x in parsed if x]

    @staticmethod
    def _parse_json_dict(raw: str, name: str) -> dict[str, list[str]]:
        """Parse a JSON-object config value into {key: [values]}. Invalid -> {}."""
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        out: dict[str, list[str]] = {}
        for key, val in parsed.items():
            if isinstance(val, list):
                out[str(key)] = [str(x) for x in val if x]
            else:
                out[str(key)] = [str(val)]
        return out

    @property
    def token_whale_watchlist(self) -> list[str]:
        """Parsed TOKEN_WHALE_WATCHLIST_JSON wallet list."""
        return self._parse_json_list(self.TOKEN_WHALE_WATCHLIST_JSON, "TOKEN_WHALE_WATCHLIST_JSON")

    @property
    def token_pools(self) -> dict[str, list[str]]:
        """Parsed TOKEN_POOLS_JSON mapping mint -> pool token-account addresses."""
        return self._parse_json_dict(self.TOKEN_POOLS_JSON, "TOKEN_POOLS_JSON")

    @field_validator("ENVIRONMENT")
    @classmethod
    def _validate_environment(cls, v: str) -> str:
        v = v.lower()
        if v not in ("dev", "prod"):
            raise ValueError("ENVIRONMENT must be 'dev' or 'prod'")
        return v

    @property
    def is_prod(self) -> bool:
        return self.ENVIRONMENT == "prod"

    @property
    def cors_origins_list(self) -> List[str]:
        """Parse the comma-separated CORS_ORIGINS string into a list."""
        if self.CORS_ORIGINS.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def solana_rpc_endpoint(self) -> str:
        """The Solana JSON-RPC endpoint to use.

        SOLANA_RPC_URL wins. When it is left at the default (Synapse gateway),
        legacy HELIUS_RPC_URL / HELIUS_API_KEY are honored so existing deploys
        keep working unchanged; the Helius key is appended as an `api-key`
        query param, which is how Helius authenticates. The Synapse gateway
        instead authenticates with a Bearer header (see solana_rpc_headers).
        """
        url = self.SOLANA_RPC_URL
        # Legacy deploy: SOLANA_RPC_URL left at the default but HELIUS_RPC_URL
        # explicitly set -> keep using Helius, appending the key as a query
        # param (Helius' auth scheme). When both are at their defaults, use
        # the OOBE gateway.
        if (
            url == "https://us-1-mainnet.oobeprotocol.ai"
            and self.HELIUS_RPC_URL != "https://mainnet.helius-rpc.com"
        ):
            url = self.HELIUS_RPC_URL
            if self.HELIUS_API_KEY:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}api-key={self.HELIUS_API_KEY}"
        return url

    @property
    def solana_rpc_headers(self) -> dict[str, str]:
        """HTTP headers for the Solana RPC endpoint (Bearer auth when keyed)."""
        if self.SOLANA_RPC_API_KEY:
            return {"Authorization": f"Bearer {self.SOLANA_RPC_API_KEY}"}
        return {}


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (singleton)."""
    return Settings()


# Convenient module-level handle. Import as: `from app.config import settings`.
settings = get_settings()
