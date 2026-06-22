"""Pytest setup. Sets required env vars BEFORE app modules import config."""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.local")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")
os.environ.setdefault("JWT_SECRET", "test-secret-please-ignore")
os.environ.setdefault("ENVIRONMENT", "dev")
os.environ.setdefault("LOG_LEVEL", "WARNING")
