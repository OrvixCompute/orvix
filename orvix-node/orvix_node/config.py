"""Configuration loading with a clear precedence:

    CLI args  >  env vars (ORVIX_NODE_*)  >  config file  >  defaults

Config file lives at ~/.orvix/config.yaml (Linux/Mac) or
%APPDATA%/orvix/config.yaml (Windows).
"""

import os
import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from orvix_node.exceptions import ConfigError

ENV_PREFIX = "ORVIX_NODE_"


def _orvix_dir() -> Path:
    """Platform-appropriate base directory for Orvix node files."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "orvix"
    return Path.home() / ".orvix"


def config_path() -> Path:
    return _orvix_dir() / "config.yaml"


def default_log_file() -> Path:
    return _orvix_dir() / "logs" / "node.log"


class NodeConfig(BaseModel):
    # Required for the orchestrator to identify and authenticate this node.
    provider_id: str
    node_secret: str

    orchestrator_url: str = "wss://api.orvix.network"
    model: str = "qwen-2.5-7b"
    inference_endpoint: str = "http://localhost:8000/v1"  # local vLLM, later
    heartbeat_interval: int = 15
    health_port: int = 9000
    log_level: str = "INFO"
    log_file: str = ""  # resolved to default_log_file() if empty
    max_concurrent_jobs: int = 4
    json_logs: bool = False
    # Inference backend: "mock" (default) or "vllm".
    backend: str = "mock"
    # Advertise image (Flux) capability at registration. Opt-in: only enable once
    # the ModelManager swap logic is deployed, else the node would advertise an
    # engine it cannot yet serve. Dual-mode (chat + image on one GPU) also needs
    # vllm_managed=true so the manager can free VRAM by stopping the vLLM server.
    enable_image_engine: bool = False
    # Let the node own the vLLM server as a subprocess (start on load, kill on
    # unload to free VRAM). Required for chat<->image swap; keep false when the
    # vLLM server is managed out of band.
    vllm_managed: bool = False
    # Unload the resident engine after this many idle minutes to free VRAM.
    idle_unload_minutes: int = 10
    # Where generated images are written before the orchestrator fetches them.
    image_tmp_dir: str = "/tmp/node-images"
    # Externally reachable base URL for this node's binary endpoint (the
    # orchestrator fetches images from here). Falls back to the local health
    # server when empty (dev only — not reachable from a remote orchestrator).
    binary_public_url: str = ""

    def masked(self) -> dict:
        """Config as a dict with secrets masked, for display."""
        data = self.model_dump()
        if data.get("node_secret"):
            data["node_secret"] = "****" + str(data["node_secret"])[-4:]
        return data

    def resolved_log_file(self) -> Path:
        return Path(self.log_file) if self.log_file else default_log_file()


def _env_overrides() -> dict:
    overrides: dict = {}
    for field in NodeConfig.model_fields:
        val = os.environ.get(ENV_PREFIX + field.upper())
        if val is not None:
            overrides[field] = val
    return overrides


def load_config(
    cli_overrides: dict | None = None, config_file: Path | None = None
) -> NodeConfig:
    """Merge defaults < file < env < CLI and validate.

    Raises ConfigError if required fields are missing or values are invalid.
    """
    data: dict = {}

    path = config_file or config_path()
    if path and Path(path).exists():
        try:
            loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Could not parse config file {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"Config file {path} must contain a YAML mapping")
        data.update(loaded)

    data.update(_env_overrides())

    if cli_overrides:
        data.update({k: v for k, v in cli_overrides.items() if v is not None})

    try:
        return NodeConfig(**data)
    except ValidationError as exc:
        missing = [
            ".".join(str(p) for p in err["loc"])
            for err in exc.errors()
            if err["type"] == "missing"
        ]
        if missing:
            raise ConfigError(
                "Missing required config: "
                + ", ".join(missing)
                + f". Run `orvix-node config init` and edit {config_path()}, "
                "or pass the values via flags / env vars."
            ) from exc
        raise ConfigError(f"Invalid configuration: {exc}") from exc


CONFIG_TEMPLATE = """\
# Orvix Node configuration
# Required:
provider_id: ""        # your provider id (from POST /v1/provider/register)
node_secret: ""        # your node secret (keep this private)

# Connection:
orchestrator_url: "wss://api.orvix.network"   # use ws://localhost:8000 for local dev
model: "qwen-2.5-7b"

# Runtime:
heartbeat_interval: 15
health_port: 9000
max_concurrent_jobs: 4
backend: "mock"        # "mock" or "vllm"

# Engines / VRAM (single-GPU swap):
enable_image_engine: false   # advertise + serve Flux images (needs vllm_managed for dual-mode)
vllm_managed: false          # node owns the vLLM server subprocess (kill on unload to free VRAM)
idle_unload_minutes: 10      # unload the resident engine after this many idle minutes

# Logging:
log_level: "INFO"
json_logs: false
# log_file: ""         # defaults to ~/.orvix/logs/node.log
"""


def init_config_file() -> Path:
    """Create the config file from the template if it does not exist."""
    path = config_path()
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return path
