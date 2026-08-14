"""Configuration loading with a clear precedence:

    CLI args  >  env vars (ORVIX_NODE_*)  >  config file  >  defaults

Config file lives at ~/.orvix/config.yaml (Linux/Mac) or
%APPDATA%/orvix/config.yaml (Windows).
"""

import os
import sys
import uuid
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
    # This machine's stable identity, sent at registration so reconnects reuse
    # one `nodes` row instead of leaving an offline ghost behind each restart.
    # Left empty it is generated once and cached beside the config file; set it
    # explicitly to pin a node to a known id (e.g. when moving hosts).
    node_id: str = ""

    # The apex host, not an `api.` subdomain: that subdomain has never been
    # published, so the old default resolved to nothing and every node left on
    # it failed to connect. `/v1/node/connect` is appended by the client.
    orchestrator_url: str = "wss://orvix.network"
    model: str = "qwen-2.5-7b"
    inference_endpoint: str = "http://localhost:8000/v1"  # local vLLM, later
    heartbeat_interval: int = 15
    health_port: int = 9000
    log_level: str = "INFO"
    log_file: str = ""  # resolved to default_log_file() if empty
    max_concurrent_jobs: int = 4
    # Image jobs are limited separately from chat: a single diffusion pass needs
    # several GB of transient VRAM on top of the resident weights, so two at once
    # can OOM a card that serves one alongside a chat engine without trouble.
    # Only raise this if you have measured the headroom on your own GPU.
    max_concurrent_image_jobs: int = 1
    json_logs: bool = False
    # Inference backend: "mock" (default) or "vllm".
    backend: str = "mock"
    # Advertise image-generation capability at registration. Opt-in: only enable once
    # the ModelManager swap logic is deployed, else the node would advertise an
    # engine it cannot yet serve. Dual-mode (chat + image on one GPU) also needs
    # vllm_managed=true so the manager can free VRAM by stopping the vLLM server.
    enable_image_engine: bool = False
    # Advertise + serve text-to-video. Opt-in and off by default for a reason
    # beyond VRAM: a clip takes minutes, during which this node cannot take any
    # other job. Enable it on a machine dedicated to video, not on the one
    # carrying chat traffic.
    enable_video_engine: bool = False
    # Cap simultaneous video jobs independently. One is the sane default — two
    # concurrent clips on one card is how you OOM a GPU that handles one fine,
    # the same trap image already hit.
    max_concurrent_video_jobs: int = 1
    # Where generated clips are written before the orchestrator fetches them.
    video_tmp_dir: str = "/tmp/node-videos"
    # Advertise + serve text embeddings. Cheap enough to leave on: the default
    # engine runs on CPU in milliseconds, so unlike image and video it does not
    # compete for the GPU that chat is using.
    enable_embedding_engine: bool = False
    # Let the node own the vLLM server as a subprocess (start on load, kill on
    # unload to free VRAM). Required for chat<->image swap; keep false when the
    # vLLM server is managed out of band.
    vllm_managed: bool = False
    # Unload the resident engine after this many idle minutes to free VRAM.
    # 0 disables idle unload: the resident engine stays in VRAM until the node
    # stops. Use 0 on a single-purpose node (e.g. video) whose model load takes
    # minutes and should not be discarded while the node is up.
    idle_unload_minutes: int = 10
    # Keep chat + image both resident in VRAM instead of swapping between
    # them. Only enable this once you've confirmed both engines' combined
    # VRAM footprint fits the GPU (e.g. an AWQ/quantized chat model next to
    # an image engine) — the manager does not check this for you.
    concurrent_engines: bool = False
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


NODE_ID_FILENAME = "node-id"


def resolve_node_id(config_file: Path | None = None) -> str:
    """Return this machine's stable node id, generating it on first run.

    The id is stored next to the config file rather than in a fixed home
    directory, because the config is the thing an operator puts on durable
    storage. On a container host, ``~`` is usually the ephemeral layer — keeping
    identity beside the config means it survives exactly as long as the config
    does, and a restart reuses the same ``nodes`` row instead of orphaning one.

    A caller that wants full control can set ``node_id`` in the config; this is
    only the fallback.
    """
    directory = Path(config_file).parent if config_file else _orvix_dir()
    id_path = directory / NODE_ID_FILENAME

    try:
        existing = id_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass  # missing or unreadable — fall through and mint a new one

    node_id = str(uuid.uuid4())
    try:
        directory.mkdir(parents=True, exist_ok=True)
        id_path.write_text(node_id + "\n", encoding="utf-8")
    except OSError:
        # Non-fatal: the node still registers, it just gets a fresh id next
        # start (the pre-existing behaviour) instead of a stable one.
        pass
    return node_id


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

    if not data.get("node_id"):
        data["node_id"] = resolve_node_id(path)

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
orchestrator_url: "wss://orvix.network"   # use ws://localhost:8000 for local dev
model: "qwen-2.5-7b"

# Runtime:
heartbeat_interval: 15
health_port: 9000
max_concurrent_jobs: 4
backend: "mock"        # "mock" or "vllm"

# Engines / VRAM (single-GPU swap):
enable_image_engine: false   # advertise + serve image generation (needs vllm_managed for dual-mode)
enable_video_engine: false   # advertise + serve text-to-video (a clip holds the GPU for minutes)
max_concurrent_video_jobs: 1 # never raise this without measuring VRAM for two concurrent clips
enable_embedding_engine: false # advertise + serve embeddings (CPU by default, does not use the GPU)
vllm_managed: false          # node owns the vLLM server subprocess (kill on unload to free VRAM)
idle_unload_minutes: 10      # unload the resident engine after this many idle minutes (0 = keep resident)

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
