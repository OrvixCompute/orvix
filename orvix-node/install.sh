#!/usr/bin/env bash
# Orvix Node one-line installer (Linux providers; Ubuntu/Debian primary).
#   curl -sSL https://get.orvix.xyz/node | bash
set -euo pipefail

ORVIX_DIR="${HOME}/.orvix"
PKG="orvix-node"

info()  { printf '\033[0;36m[orvix]\033[0m %s\n' "$1"; }
warn()  { printf '\033[0;33m[orvix]\033[0m %s\n' "$1"; }
err()   { printf '\033[0;31m[orvix]\033[0m %s\n' "$1" >&2; }

detect_os() {
  if [ -f /etc/os-release ]; then
    . /etc/os-release
    echo "${ID:-unknown}"
  else
    echo "unknown"
  fi
}

ensure_python() {
  if command -v python3 >/dev/null 2>&1; then
    local ver
    ver="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
    info "Found Python ${ver}"
    return
  fi
  warn "Python 3 not found — installing..."
  local os; os="$(detect_os)"
  case "$os" in
    ubuntu|debian)
      sudo apt-get update -y
      sudo apt-get install -y python3 python3-pip python3-venv ;;
    *)
      err "Unsupported OS '$os'. Install Python 3.11+ manually and re-run."
      exit 1 ;;
  esac
}

main() {
  info "Installing Orvix Node Software..."
  ensure_python
  mkdir -p "${ORVIX_DIR}/logs"

  info "Installing ${PKG} via pip..."
  python3 -m pip install --user --upgrade "${PKG}"

  # Collect credentials.
  read -rp "Provider ID: " PROVIDER_ID
  read -rsp "Node secret: " NODE_SECRET; echo

  CONFIG="${ORVIX_DIR}/config.yaml"
  if [ ! -f "$CONFIG" ]; then
    cat > "$CONFIG" <<YAML
provider_id: "${PROVIDER_ID}"
node_secret: "${NODE_SECRET}"
orchestrator_url: "wss://api.orvix.xyz"
model: "qwen-2.5-7b"
heartbeat_interval: 15
health_port: 9000
max_concurrent_jobs: 4
backend: "mock"
log_level: "INFO"
YAML
    info "Wrote ${CONFIG}"
  else
    warn "Config already exists at ${CONFIG} — leaving it untouched."
  fi

  # Optional systemd service.
  read -rp "Install as a systemd service? [y/N] " INSTALL_SVC
  if [[ "${INSTALL_SVC:-N}" =~ ^[Yy]$ ]]; then
    SERVICE=/etc/systemd/system/orvix-node.service
    BIN="$(command -v orvix-node || echo "${HOME}/.local/bin/orvix-node")"
    sudo tee "$SERVICE" >/dev/null <<UNIT
[Unit]
Description=Orvix Node
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=${BIN} start
Restart=always
RestartSec=5
User=${USER}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable --now orvix-node
    info "Service installed and started (systemctl status orvix-node)."
  fi

  cat <<DONE

Orvix Node installed.

Next steps:
  orvix-node config show     # verify configuration
  orvix-node gpu             # check GPU detection
  orvix-node start           # start the node (or: systemctl start orvix-node)

For development without a GPU:
  ORVIX_NODE_STUB_GPU=true orvix-node start
DONE
}

main "$@"
