"""Tests for configuration loading and precedence."""

import textwrap

import pytest

from orvix_node.config import NODE_ID_FILENAME, NodeConfig, load_config
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


# --- stable node identity ---------------------------------------------------
def test_node_id_is_generated_and_cached_beside_the_config(tmp_path):
    path = _write_config(
        tmp_path,
        """
        provider_id: prov-1
        node_secret: secret-1
        """,
    )
    cfg = load_config(config_file=path)

    assert cfg.node_id
    id_file = tmp_path / NODE_ID_FILENAME
    assert id_file.exists()
    assert id_file.read_text(encoding="utf-8").strip() == cfg.node_id
    # Beside the config, not in the home dir: on a container host ~ is the
    # ephemeral layer, so identity must live wherever the config lives.
    assert id_file.parent == path.parent


def test_node_id_is_stable_across_loads(tmp_path):
    path = _write_config(
        tmp_path,
        """
        provider_id: prov-1
        node_secret: secret-1
        """,
    )
    first = load_config(config_file=path).node_id
    second = load_config(config_file=path).node_id
    assert first == second


def test_explicit_node_id_wins_over_the_cache(tmp_path):
    path = _write_config(
        tmp_path,
        """
        provider_id: prov-1
        node_secret: secret-1
        node_id: pinned-id
        """,
    )
    (tmp_path / NODE_ID_FILENAME).write_text("cached-id\n", encoding="utf-8")
    assert load_config(config_file=path).node_id == "pinned-id"


def test_node_id_survives_an_unwritable_directory(tmp_path, monkeypatch):
    # Caching is best-effort: a read-only config dir must not stop the node
    # from starting, it just falls back to a fresh id each run.
    path = _write_config(
        tmp_path,
        """
        provider_id: prov-1
        node_secret: secret-1
        """,
    )

    def _boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr("pathlib.Path.write_text", _boom)
    cfg = load_config(config_file=path)
    assert cfg.node_id


# --- join ------------------------------------------------------------------
#
# `join` exists to remove the hand-editing step provider onboarding required.
# The two things worth pinning: it refuses to destroy an existing config, and a
# rejected credential leaves nothing behind — a half-written config is worse
# than none, because the next start reads it and fails somewhere less obvious.


def _invoke_join(tmp_path, monkeypatch, args, verify=None):
    from click.testing import CliRunner

    import orvix_node.cli as cli_mod

    cfg_path = tmp_path / "config.yaml"
    monkeypatch.setattr(cli_mod, "config_path", lambda: cfg_path)

    if verify is not None:
        class _Client:
            def __init__(self, cfg):
                self.cfg = cfg

            async def verify(self):
                return verify(self.cfg)

        import orvix_node.client as client_mod

        monkeypatch.setattr(client_mod, "OrchestratorClient", _Client)

    return CliRunner().invoke(cli_mod.join, args), cfg_path


def test_join_writes_a_config_after_verifying(tmp_path, monkeypatch):
    seen = {}

    def ok(cfg):
        seen["provider_id"] = cfg.provider_id
        seen["secret"] = cfg.node_secret
        return "node-abc"

    result, cfg_path = _invoke_join(
        tmp_path, monkeypatch,
        ["--provider-id", "prov-1", "--node-secret", "s3cret"],
        verify=ok,
    )

    assert result.exit_code == 0, result.output
    assert seen == {"provider_id": "prov-1", "secret": "s3cret"}
    assert "node-abc" in result.output
    body = cfg_path.read_text()
    assert 'provider_id: "prov-1"' in body
    assert 'node_secret: "s3cret"' in body


def test_join_refuses_to_clobber_an_existing_config(tmp_path, monkeypatch):
    result, cfg_path = _invoke_join(
        tmp_path, monkeypatch, ["--provider-id", "p", "--node-secret", "s"],
        verify=lambda c: "n",
    )
    assert result.exit_code == 0
    original = cfg_path.read_text()

    again, _ = _invoke_join(
        tmp_path, monkeypatch, ["--provider-id", "other", "--node-secret", "other"],
        verify=lambda c: "n",
    )
    assert again.exit_code != 0
    assert "--force" in again.output
    assert cfg_path.read_text() == original, "the existing config must be untouched"


def test_join_writes_nothing_when_the_orchestrator_rejects(tmp_path, monkeypatch):
    """A half-written config is worse than none: the next start would read it."""
    from orvix_node.exceptions import AuthError

    def rejected(cfg):
        raise AuthError("Registration rejected: unknown provider")

    result, cfg_path = _invoke_join(
        tmp_path, monkeypatch, ["--provider-id", "bad", "--node-secret", "bad"],
        verify=rejected,
    )

    assert result.exit_code != 0
    assert "rejected" in result.output.lower()
    assert not cfg_path.exists(), "no config may be left behind"


def test_join_keeps_the_template_comments(tmp_path, monkeypatch):
    """The file is something the provider edits later; a bare dump would strip
    every explanation of what they can tune."""
    result, cfg_path = _invoke_join(
        tmp_path, monkeypatch, ["--provider-id", "p", "--node-secret", "s"],
        verify=lambda c: "n",
    )
    assert result.exit_code == 0
    body = cfg_path.read_text()
    assert "#" in body
    assert "heartbeat_interval" in body


def test_join_config_is_not_world_readable(tmp_path, monkeypatch):
    """It holds the node secret."""
    import stat

    result, cfg_path = _invoke_join(
        tmp_path, monkeypatch, ["--provider-id", "p", "--node-secret", "s"],
        verify=lambda c: "n",
    )
    assert result.exit_code == 0
    mode = stat.S_IMODE(cfg_path.stat().st_mode)
    assert mode & 0o077 == 0, f"config mode {oct(mode)} exposes the secret"
