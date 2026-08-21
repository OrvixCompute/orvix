"""Pydantic models for the token intelligence layer.

Covers token/CA scans, wallet analysis, accumulation scoring, monitoring
agents, alert events and webhook deliveries. Optional fields are `None` when
the underlying data source is unavailable (e.g. no liquidity pools configured)
rather than erroring — the endpoints degrade gracefully.
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# --- token scan ------------------------------------------------------------

class TokenMetadata(BaseModel):
    name: Optional[str] = None
    symbol: Optional[str] = None
    uri: Optional[str] = None
    update_authority: Optional[str] = None


class TokenSupply(BaseModel):
    amount: str = Field(..., description="Raw base-unit supply")
    decimals: int
    ui_amount_string: Optional[str] = Field(None, description="Human supply, e.g. 1,000,000.0")


class TokenLiquidity(BaseModel):
    estimated_usdc: Optional[float] = Field(
        None, description="Sum of configured pool token-account balances; null when no pools configured"
    )
    pool_count: int = 0


class TokenHolderSnapshot(BaseModel):
    total_holders: Optional[int] = None
    top_holders: list[dict[str, Any]] = Field(
        default_factory=list, description="[{\"wallet\": str, \"balance\": float}] sorted desc"
    )
    top10_share: Optional[float] = Field(
        None, description="Fraction of supply held by the top 10 holders (0-1)"
    )
    as_of: Optional[datetime] = None


class EarlyBuyerEntry(BaseModel):
    wallet: str
    amount: float = Field(..., description="UI amount of the first detected buy")
    signature: str = Field(..., description="Transaction of the first detected buy")
    block_time: Optional[int] = Field(None, description="Unix timestamp of the first buy")


class TokenRisk(BaseModel):
    warnings: list[str] = Field(
        default_factory=list, description="Human-readable risk flags, e.g. no metadata / high concentration"
    )


class TokenIntelligenceAnalysis(BaseModel):
    narrative: str = Field(..., description="AI-written market picture / emerging narrative")
    risk_flags: list[str] = Field(default_factory=list)
    watch_next: str = Field("", description="What to watch next, per the model")
    holder_count: Optional[int] = Field(None, description="Number of top holders analyzed")
    top10_share: Optional[float] = Field(None, description="Top-10 holder concentration (0-1)")
    risk_score: Optional[int] = Field(None, ge=0, le=100, description="0=safe, 100=extreme risk")
    verdict: Optional[str] = Field(None, description="buy | hold | avoid | scam_risk")
    reasons: list[str] = Field(default_factory=list, description="Why this verdict — concise bullet points")


class TokenIntelligenceResponse(BaseModel):
    mint: str
    model: str = Field(..., description="Chat model that produced the analysis")
    analysis: TokenIntelligenceAnalysis
    generated_at: datetime
    latency_ms: Optional[int] = Field(None, description="Node round-trip time")
    node_id: Optional[str] = Field(None, description="GPU node that served the analysis")


class TokenScanResponse(BaseModel):
    mint: str
    metadata: Optional[TokenMetadata] = None
    supply: Optional[TokenSupply] = None
    price_usdc: Optional[float] = Field(None, description="Jupiter-derived price in USDC; null when unquoteable")
    liquidity: TokenLiquidity = Field(default_factory=TokenLiquidity)
    holders: Optional[TokenHolderSnapshot] = None
    risk: TokenRisk = Field(default_factory=TokenRisk)
    scanned_at: datetime


# --- wallet analysis -------------------------------------------------------

class WalletHolding(BaseModel):
    mint: str
    ui_amount: float = Field(..., description="UI balance held by the wallet")
    symbol: Optional[str] = None
    name: Optional[str] = None


class WalletActivityEntry(BaseModel):
    signature: str
    slot: Optional[int] = None
    timestamp: Optional[datetime] = None
    memo: Optional[str] = None
    transfers: list[dict[str, Any]] = Field(
        default_factory=list, description="Parsed SPL transfers the wallet was a party to"
    )


class WalletBuyEntry(BaseModel):
    signature: str
    amount: float = Field(..., description="UI amount of `mint` bought/inflowed to the wallet")
    timestamp: Optional[datetime] = None
    counterparty: Optional[str] = Field(None, description="Source token account or wallet, when known")


class WalletAnalysisResponse(BaseModel):
    wallet: str
    holdings: list[WalletHolding] = Field(
        default_factory=list, description="Token accounts owned; capped at MAX_TOKEN_ACCOUNTS_PER_WALLET"
    )
    recent_activity: list[WalletActivityEntry] = Field(
        default_factory=list, description="Parsed recent transactions; capped at MAX_WALLET_TX_PARSE"
    )
    buy_history: list[WalletBuyEntry] = Field(
        default_factory=list, description="Per-mint buy/inflow history when `mint` is given"
    )
    analyzed_at: datetime


# --- accumulation ----------------------------------------------------------

class AccumulationMetrics(BaseModel):
    watchlist_wallets: int = Field(..., description="Number of tracked wallets analyzed")
    inflow_7d: float = Field(..., description="Net UI inflow of the token across watchlist wallets, 7d window")
    inflow_ratio: Optional[float] = Field(None, description="inflow_7d / supply; null when supply unknown")
    buy_tx_7d: int = Field(..., description="Distinct buy transfers by watchlist wallets in 7d")
    top10_share: Optional[float] = Field(None, description="From cached holder snapshot; null when absent")
    distribution_score: float = Field(..., description="0-100; 50 when no holder snapshot")
    inflow_score: float = Field(..., description="0-100; +100 at >=2% of supply")
    activity_score: float = Field(..., description="0-100; +100 at >=20 buy txs")


class AccumulationResponse(BaseModel):
    mint: str
    score: int = Field(..., ge=0, le=100, description="0-100 accumulation score")
    label: str = Field(..., description="distribution | weak | moderate | strong")
    metrics: AccumulationMetrics
    computed_at: datetime


# --- monitors --------------------------------------------------------------

class MonitorCondition(BaseModel):
    """One condition on a monitor.

    Token target types:
      {"type": "accumulation_score", "gte": 70}
      {"type": "price_drop_pct", "gte": 10}
      {"type": "large_transfer", "min_ui_amount": 1000.0}
    Wallet target types:
      {"type": "new_activity"}
      {"type": "large_inflow", "min_ui_amount": 5000.0}
    """

    type: str
    gte: Optional[float] = None
    min_ui_amount: Optional[float] = None


class MonitorCreateRequest(BaseModel):
    name: str = Field("", max_length=120)
    target_type: str = Field(..., description="token | wallet")
    target_address: str = Field(..., description="Solana mint CA or wallet address")
    conditions: list[MonitorCondition] = Field(..., min_length=1)
    webhook_url: Optional[str] = Field(None, max_length=2048)
    is_active: bool = True
    interval_minutes: int = Field(30, ge=5, le=1440)


class MonitorUpdateRequest(BaseModel):
    """All fields optional — only the provided ones are updated."""

    name: Optional[str] = Field(None, max_length=120)
    conditions: Optional[list[MonitorCondition]] = Field(None, min_length=1)
    webhook_url: Optional[str] = Field(None, max_length=2048)
    is_active: Optional[bool] = None
    interval_minutes: Optional[int] = Field(None, ge=5, le=1440)
    reset_baseline: Optional[bool] = Field(
        None,
        description="When true and the monitor is a token with a price condition, re-snapshot the baseline price",
    )


class MonitorResponse(BaseModel):
    id: str
    name: str
    target_type: str
    target_address: str
    conditions: list[dict[str, Any]]
    webhook_url: Optional[str] = None
    is_active: bool
    interval_minutes: int
    baseline_price_usdc: Optional[float] = None
    last_checked_at: Optional[datetime] = None
    created_at: datetime


class AlertEventResponse(BaseModel):
    id: str
    monitor_id: str
    condition_type: str
    message: str
    payload: dict[str, Any]
    occurred_at: datetime


class WebhookTestResponse(BaseModel):
    ok: bool
    status_code: Optional[int] = None
    error: Optional[str] = None


# --- social intelligence ---------------------------------------------------

class SocialMetrics(BaseModel):
    dex_trending: bool = False
    dex_volume_24h: Optional[float] = Field(None, description="24h trading volume in USD from DexScreener")
    dex_price_change_24h: Optional[float] = Field(None, description="24h price change percentage")
    twitter_followers: Optional[int] = None
    twitter_statuses_7d: Optional[int] = Field(None, description="Tweets in the last 7 days")
    social_sentiment: Optional[str] = Field(None, description="positive | neutral | negative")


class SocialAnalysisResponse(BaseModel):
    mint: str
    social_links: dict[str, Optional[str]] = Field(
        default_factory=dict, description="{twitter, website, telegram, discord}"
    )
    social_score: int = Field(0, ge=0, le=100, description="Composite social activity score")
    metrics: SocialMetrics = Field(default_factory=SocialMetrics)
    as_of: datetime


# --- wallet clustering -----------------------------------------------------

class WalletCluster(BaseModel):
    id: str = Field(..., description="Cluster identifier")
    wallets: list[str] = Field(..., description="Wallets in this cluster")
    signals: list[str] = Field(
        default_factory=list,
        description="Clustering signals: shared_funding, coordinated_timing, overlapping_holdings",
    )
    confidence: float = Field(..., ge=0, le=1, description="Cluster confidence based on signal count")


class WalletClusterResponse(BaseModel):
    mint: str
    clusters: list[WalletCluster] = Field(default_factory=list)
    total_wallets_analyzed: int
    as_of: datetime
