"""FastAPI application entrypoint for the Orvix Orchestrator."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.config import settings
from app.database import test_connection
from app.exceptions import register_exception_handlers
from app.logger import configure_logging, logger
from app.middleware import register_middleware
from app.routes import (
    account,
    admin,
    api_keys,
    auth,
    billing,
    governance,
    health,
    inference,
    node,
    provider,
    staking,
)
from app.services.node_manager import node_manager
from app.services.payment_listener import payment_listener
from app.services.payout_service import payout_service
from app.services.solana_service import get_solana_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown hooks."""
    configure_logging()
    logger.info("Orvix Orchestrator starting... (env={})", settings.ENVIRONMENT)

    if not test_connection():
        logger.error("Database connection failed at startup — check your .env values")

    if settings.ENABLE_PAYMENT_LISTENER:
        await payment_listener.start()
    else:
        logger.info("Payment listener disabled (set ENABLE_PAYMENT_LISTENER=true to enable)")

    # Node registry health monitor (drops stale node connections).
    await node_manager.start_health_check()

    if settings.ENABLE_PAYOUT_WORKER:
        await payout_service.start()
    else:
        logger.info("Payout worker disabled (set ENABLE_PAYOUT_WORKER=true to enable)")

    yield

    logger.info("Shutting down...")
    if settings.ENABLE_PAYMENT_LISTENER:
        await payment_listener.stop()
    if settings.ENABLE_PAYOUT_WORKER:
        await payout_service.stop()
    await node_manager.shutdown()
    await get_solana_service().close()


app = FastAPI(
    title="Orvix Orchestrator",
    version=__version__,
    description="Decentralized AI compute network on Solana — OpenAI-compatible API.",
    lifespan=lifespan,
)

# Order matters: middleware, then handlers, then routers.
register_middleware(app)
register_exception_handlers(app)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(api_keys.router)
app.include_router(inference.router)
app.include_router(billing.router)
app.include_router(node.router)
app.include_router(provider.router)
app.include_router(staking.router)
app.include_router(account.router)
app.include_router(admin.router)
app.include_router(governance.router)
