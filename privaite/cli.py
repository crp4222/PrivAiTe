from __future__ import annotations

import os

import click
import uvicorn

from privaite.config.loader import load_config

# The documented quickstart is `python -m privaite --config privaite.yaml`, so
# the group runs the server when no subcommand is given. Turning this into a
# plain group would have broken every README, Dockerfile and compose file in
# the wild.


@click.group(invoke_without_command=True)
@click.option("--config", "config_path", default=None, help="Path to config YAML file")
@click.option("--host", default=None, help="Override server host")
@click.option("--port", default=None, type=int, help="Override server port")
@click.option("--reload", is_flag=True, help="Auto-reload on file changes (dev mode)")
@click.pass_context
def main(
    ctx: click.Context,
    config_path: str | None,
    host: str | None,
    port: int | None,
    reload: bool,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    serve(config_path, host, port, reload)


def serve(config_path: str | None, host: str | None, port: int | None, reload: bool) -> None:
    # uvicorn re-imports create_app() in the worker process, so the path has to
    # travel through the environment to reach the factory's load_config().
    if config_path:
        os.environ["PRIVAITE_CONFIG_PATH"] = config_path
    config = load_config(config_path)

    run_host = host or config.server.host
    run_port = port or config.server.port

    click.echo(f"Starting PrivAiTe on {run_host}:{run_port}")
    click.echo(f"PII processing: {'enabled' if config.pii.enabled else 'disabled'}")
    click.echo(f"Providers: {len(config.providers)} configured")
    if reload:
        click.echo("Auto-reload enabled (dev mode)")

    uvicorn.run(
        "privaite.app:create_app",
        host=run_host,
        port=run_port,
        workers=config.server.workers,
        log_level=config.server.log_level,
        factory=True,
        reload=reload,
        reload_dirs=["privaite", "config"] if reload else None,
    )


@main.command("verify")
@click.option(
    "--preset",
    default="onnx",
    help="Detection preset to exercise (default: onnx, the shipped default).",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def verify_command(preset: str, as_json: bool) -> None:
    """Send an agent-shaped request and print what the provider actually received.

    Runs entirely on 127.0.0.1 against a throwaway provider: no credential is
    used and nothing leaves the machine. Exits non-zero if any planted value
    reached the wire, so it works as a gate.
    """
    from privaite.verify import format_report, verify

    if preset == "onnx":
        click.echo("Using preset onnx; the detection model downloads once (~800 MB).", err=True)

    result = verify(preset=preset)

    if as_json:
        import dataclasses
        import json

        payload = dataclasses.asdict(result)
        payload["ok"] = result.ok
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(format_report(result))

    raise SystemExit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
