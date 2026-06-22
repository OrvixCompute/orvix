"""Tests for configuration loading and precedence."""

import textwrap

import pytest

from orvix_node.config import NodeConfig, load_config
from orvix_node.exceptions import ConfigError


def _write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_load_from_file(tmp_path):
    path = _write_config(
        tmp_path,
        """
        provider_id: prov-1
        node_secret: secret-1
        model: mistral-7b
        max_concurrent_jobs: 8
        """,
    )
    cfg = load_config(config_file=path)
    assert cfg.provider_id == "prov-1"
    assert cfg.model == "mistral-7b"
    assert cfg.max_concurrent_jobs == 8
    # defaults still apply
    assert cfg.heartbeat_interval == 15


def test_env_overrides_file(tmp_path, monkeypatch):
    path = _write_config(
        tmp_path,
        """
        provider_id: prov-1
        node_secret: secret-1
        model: mistral-7b
        """,
    )
    monkeypatch.setenv("ORVIX_NODE_MODEL", "llama-3.1-8b-quantized")
    monkeypatch.setenv("ORVIX_NODE_MAX_CONCURRENT_JOBS", "2")
    cfg = load_config(config_file=path)
    assert cfg.model == "llama-3.1-8b-quantized"
    assert cfg.max_concurrent_jobs == 2  # coerced from str


def test_cli_overrides_env_and_file(tmp_path, monkeypatch):
    path = _write_config(
        tmp_path,
        """
        provider_id: prov-1
        node_secret: secret-1
        model: mistral-7b
        """,
    )
    monkeypatch.setenv("ORVIX_NODE_MODEL", "from-env")
    cfg = load_config(cli_overrides={"model": "from-cli"}, config_file=path)
    assert cfg.model == "from-cli"
    # None CLI values are ignored (don't clobber).
    cfg2 = load_config(cli_overrides={"model": None}, config_file=path)
    assert cfg2.model == "from-env"


def test_missing_required_raises(tmp_path):
    path = _write_config(tmp_path, "model: mistral-7b\n")
    with pytest.raises(ConfigError) as exc:
        load_config(config_file=path)
    assert "provider_id" in str(exc.value)
    assert "node_secret" in str(exc.value)


def test_masked_hides_secret():
    cfg = NodeConfig(provider_id="p", node_secret="supersecretvalue")
    masked = cfg.masked()
    assert masked["node_secret"].startswith("****")
    assert "supersecret" not in masked["node_secret"]
