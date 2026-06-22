"""Application configuration loaded from environment variables via pydantic-settings."""

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

    # --- Auth --------------------------------------------------------------
    JWT_ALGORITHM: str = Field("HS256", description="JWT signing algorithm")
    JWT_EXPIRY_HOURS: int = Field(24, description="JWT lifetime in hours")

    # --- Solana / billing (optional until Prompt 6) ------------------------
    TREASURY_WALLET_ADDRESS: str = Field("", description="Treasury wallet that receives USDC deposits")
    USDC_MINT_ADDRESS: str = Field("", description="SPL mint address of the USDC token")
    HELIUS_API_KEY: str = Field("", description="Helius API key for Solana RPC")
    HELIUS_RPC_URL: str = Field(
        "https://mainnet.helius-rpc.com", description="Helius RPC base URL"
    )
    POLLING_INTERVAL_SECONDS: int = Field(15, description="Payment listener poll interval")
    ENABLE_PAYMENT_LISTENER: bool = Field(
        False, description="Start the Solana payment listener on app startup"
    )

    # --- Provider / payouts (Prompt 6) -------------------------------------
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
    TREASURY_KEYPAIR_PATH: str = Field(
        "", description="Path to the treasury keypair file (only if PAYOUT_STUB=false)"
    )
    ENABLE_PAYOUT_WORKER: bool = Field(
        False, description="Start the withdrawal payout worker on startup"
    )

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
    def helius_rpc_endpoint(self) -> str:
        """Full RPC URL including the API key query parameter when available."""
        if self.HELIUS_API_KEY:
            sep = "&" if "?" in self.HELIUS_RPC_URL else "?"
            return f"{self.HELIUS_RPC_URL}{sep}api-key={self.HELIUS_API_KEY}"
        return self.HELIUS_RPC_URL


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (singleton)."""
    return Settings()


# Convenient module-level handle. Import as: `from app.config import settings`.
settings = get_settings()
