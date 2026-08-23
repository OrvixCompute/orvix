"""Pytest setup. Sets required env vars BEFORE app modules import config."""

import os
import sys

# Add shared packages to Python path so `orvix_protocol` is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "protocol"))

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.local")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")
os.environ.setdefault("JWT_SECRET", "test-secret-please-ignore")
os.environ.setdefault("ENVIRONMENT", "dev")
# The suite has no registered nodes, so the chat tests need the dev mock path to
# reach the billing/quota logic they actually cover. Tests about the refusal
# itself flip this off explicitly.
os.environ.setdefault("ALLOW_MOCK_INFERENCE", "true")
os.environ.setdefault("LOG_LEVEL", "WARNING")
