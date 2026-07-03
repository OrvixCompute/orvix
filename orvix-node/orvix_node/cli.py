"""Click CLI for the Orvix node software."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import click

from orvix_node.config import config_path, init_config_file, load_config
from orvix_node.exceptions import AuthError, ConfigError
from orvix_node.version import __version__


def _fail(message: str, code: int = 1) -> None:
    click.secho(f"Error: {message}", fg="red", err=True)
    sys.exit(code)


@click.group()
@click.version_option(__version__, "--version", prog_name="orvix-node")
def cli() -> None:
    """Orvix Node — run GPU compute for the Orvix network."""


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------
@cli.command()
@click.option("--config", "config_file", type=click.Path(), help="Path to config.yaml")
@click.option("--provider-id", help="Override provider_id")
@click.option("--orchestrator-url", help="Override orchestrator_url")
@click.option("--model", help="Override model")
def start(config_file, provider_id, orchestrator_url, model) -> None:
    """Start the node agent (connects to the orchestrator)."""
    overrides = {
        "provider_id": provider_id,
        "orchestrator_url": orchestrator_url,
        "model": model,
    }
    try:
        cfg = load_config(
            cli_overrides=overrides,
            config_file=Path(config_file) if config_file else None,
        )
    except ConfigError as exc:
        _fail(str(exc))

    try:
        asyncio.run(_run_agent(cfg))
    except AuthError as exc:
        _fail(f"Authentication failed: {exc}", code=2)
    except KeyboardInterrupt:
        pass


async def _run_agent(cfg) -> None:
    # Imports are local so lightweight commands (config/gpu) don't pull in
    # websockets/uvicorn/etc.
    from orvix_node.client import OrchestratorClient
    from orvix_node.executor import JobExecutor
    from orvix_node.gpu import detector
    from orvix_node.health import HealthServer
    from orvix_node.inference.manager import ModelManager
    from orvix_node.logger import configure_logging, logger
    from orvix_node.state import state

    configure_logging(cfg.log_level, cfg.resolved_log_file(), cfg.json_logs)
    logger.info("Orvix Node v{} starting (provider={})", __version__, cfg.provider_id)

    # GPU gate.
    gpu = detector.detect()
    if gpu is None:
        _fail(
            "No GPU detected. Install with `pip install orvix-node[gpu]`, "
            "or set ORVIX_NODE_STUB_GPU=true for development."
        )
    await state.set_gpu_status("ok")
    logger.info("GPU: {} ({} MB VRAM)", gpu.model, gpu.vram_total_mb)

    # Chat engine selection.
    backend_name = os.environ.get("ORVIX_NODE_BACKEND", cfg.backend).lower()
    if backend_name == "vllm":
        from orvix_node.inference.vllm import VLLMBackend

        logger.warning("Using vLLM chat engine (managed={})", cfg.vllm_managed)
        chat_engine = VLLMBackend(model=cfg.model, managed=cfg.vllm_managed)
    else:
        from orvix_node.inference.mock import MockBackend

        logger.warning("Using MOCK inference engine — responses are fake.")
        chat_engine = MockBackend(provider_id=cfg.provider_id)

    engines = {"chat": chat_engine}
    if cfg.enable_image_engine:
        from orvix_node.inference.flux import FluxEngine

        engines["image"] = FluxEngine()
        logger.info("Image engine (Flux) enabled — chat<->image will swap on demand.")

    manager = ModelManager(
        engines, idle_timeout_seconds=cfg.idle_unload_minutes * 60
    )
    binary_base_url = cfg.binary_public_url or f"http://127.0.0.1:{cfg.health_port}"
    executor = JobExecutor(
        manager,
        max_concurrent=cfg.max_concurrent_jobs,
        image_tmp_dir=cfg.image_tmp_dir,
        binary_base_url=binary_base_url,
    )

    # Pre-warm the chat engine so the first request isn't slowed by a cold load.
    async with manager.serving(cfg.model):
        pass

    # Background task: unload the resident engine after it goes idle.
    async def _idle_loop() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await manager.idle_check()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Idle check failed: {}", exc)

    idle_task = asyncio.create_task(_idle_loop(), name="idle-check")

    # Background task: remove leftover image temp files (>1h old).
    async def _sweep_loop() -> None:
        from orvix_node.binary import sweep_temp_dir

        while True:
            await asyncio.sleep(600)
            try:
                sweep_temp_dir(cfg.image_tmp_dir)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Temp sweep failed: {}", exc)

    sweep_task = asyncio.create_task(_sweep_loop(), name="temp-sweep")

    health = HealthServer(cfg.health_port, manager=manager)
    await health.start()

    client = OrchestratorClient(cfg)

    async def job_handler(job) -> None:
        await executor.execute(
            job, send_chunk=client.send_message, send_result=client.send_message
        )

    async def image_handler(dispatch) -> None:
        await executor.execute_image(
            dispatch, send_complete=client.send_message, send_failed=client.send_message
        )

    client.set_job_handler(job_handler)
    client.set_image_handler(image_handler)

    # Graceful shutdown on SIGINT/SIGTERM.
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            pass  # Windows: handled via KeyboardInterrupt instead

    client_task = asyncio.create_task(client.start(), name="client")
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-wait")
    done, _ = await asyncio.wait(
        {client_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )

    # Drain & shut down.
    await client.stop()
    idle_task.cancel()
    sweep_task.cancel()
    await executor.shutdown()
    await health.stop()
    if client_task in done and client_task.exception():
        raise client_task.exception()
    client_task.cancel()
    logger.info("Node stopped cleanly")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
@cli.command()
@click.option("--config", "config_file", type=click.Path(), help="Path to config.yaml")
def status(config_file) -> None:
    """Ping the local health endpoint and print status."""
    import httpx

    try:
        cfg = load_config(config_file=Path(config_file) if config_file else None)
        port = cfg.health_port
    except ConfigError:
        port = 9000  # fall back to default if config is incomplete

    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=3.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        _fail(f"Node does not appear to be running on :{port} ({exc})")

    click.secho("Node status:", bold=True)
    for key in ("status", "version", "uptime", "current_jobs", "orchestrator_connected"):
        click.echo(f"  {key}: {data.get(key)}")
    gpu = data.get("gpu", {})
    click.echo(f"  gpu: {gpu.get('status')} ({gpu.get('primary_gpu')})")


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------
@cli.command()
@click.option("--tail", "tail_n", default=50, help="Number of lines to show")
@click.option("--follow", "-f", is_flag=True, help="Follow the log file")
@click.option("--config", "config_file", type=click.Path(), help="Path to config.yaml")
def logs(tail_n, follow, config_file) -> None:
    """Tail the node log file."""
    import time

    try:
        cfg = load_config(config_file=Path(config_file) if config_file else None)
        log_file = cfg.resolved_log_file()
    except ConfigError:
        from orvix_node.config import default_log_file

        log_file = default_log_file()

    if not log_file.exists():
        _fail(f"Log file not found: {log_file}")

    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-tail_n:]:
        click.echo(line)

    if follow:
        with log_file.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            try:
                while True:
                    line = f.readline()
                    if line:
                        click.echo(line.rstrip("\n"))
                    else:
                        time.sleep(0.3)
            except KeyboardInterrupt:
                pass


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
@cli.group()
def config() -> None:
    """Manage node configuration."""


@config.command("show")
@click.option("--config", "config_file", type=click.Path(), help="Path to config.yaml")
def config_show(config_file) -> None:
    """Print resolved config with secrets masked."""
    try:
        cfg = load_config(config_file=Path(config_file) if config_file else None)
    except ConfigError as exc:
        _fail(str(exc))
    import json

    click.echo(json.dumps(cfg.masked(), indent=2, default=str))


@config.command("init")
def config_init() -> None:
    """Create ~/.orvix/config.yaml from the template if it doesn't exist."""
    path = config_path()
    if path.exists():
        click.secho(f"Config already exists at {path}", fg="yellow")
        return
    created = init_config_file()
    click.secho(f"Created {created}", fg="green")
    click.echo("Edit it to set provider_id and node_secret, then run `orvix-node start`.")


# ---------------------------------------------------------------------------
# gpu
# ---------------------------------------------------------------------------
@cli.command()
@click.option("--watch", is_flag=True, help="Live-update metrics every second")
def gpu(watch) -> None:
    """Detect the GPU and print its info (and optionally live metrics)."""
    import time

    from orvix_node.gpu import detector

    info = detector.detect()
    if info is None:
        _fail(
            "No GPU detected. Set ORVIX_NODE_STUB_GPU=true to simulate one, "
            "or install with `pip install orvix-node[gpu]`."
        )

    def _print_info() -> None:
        click.secho("GPU Info", bold=True)
        for k, v in info.model_dump().items():
            click.echo(f"  {k:<20} {v}")

    if not watch:
        _print_info()
        return

    try:
        while True:
            click.clear()
            _print_info()
            m = detector.get_metrics()
            click.secho("\nMetrics", bold=True)
            for k, v in m.model_dump(mode="json").items():
                click.echo(f"  {k:<20} {v}")
            click.echo("\n(press Ctrl+C to stop)")
            time.sleep(1)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# test-inference
# ---------------------------------------------------------------------------
@cli.command("test-inference")
@click.option("--prompt", required=True, help="Prompt to send to the backend")
@click.option("--model", default="qwen-2.5-7b", help="Model name (label only for mock)")
@click.option("--stream", is_flag=True, help="Use the streaming path")
def test_inference(prompt, model, stream) -> None:
    """Run the inference backend locally without the orchestrator."""
    from orvix_node.inference.base import GenerateRequest
    from orvix_node.logger import configure_logging

    configure_logging("INFO", None, False)
    backend_name = os.environ.get("ORVIX_NODE_BACKEND", "mock").lower()

    async def _run() -> None:
        if backend_name == "vllm":
            from orvix_node.inference.vllm import VLLMBackend

            backend = VLLMBackend(model=model)
        else:
            from orvix_node.inference.mock import MockBackend

            backend = MockBackend(provider_id="local-test")

        await backend.load(model)
        req = GenerateRequest(messages=[{"role": "user", "content": prompt}])

        if stream:
            click.echo("Streaming response:")
            async for chunk in backend.generate_stream(req):
                if chunk.delta_content:
                    click.echo(chunk.delta_content, nl=False)
                if chunk.is_final and chunk.usage:
                    click.echo(
                        f"\n\n[usage] prompt={chunk.usage.prompt_tokens} "
                        f"completion={chunk.usage.completion_tokens}"
                    )
        else:
            resp = await backend.generate(req)
            click.echo(f"\n{resp.content}\n")
            click.echo(
                f"[usage] prompt={resp.prompt_tokens} completion={resp.completion_tokens} "
                f"finish={resp.finish_reason}"
            )
        await backend.unload()

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
