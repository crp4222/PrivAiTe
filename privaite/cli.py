from __future__ import annotations

import click
import uvicorn

from privaite.config.loader import load_config


@click.command()
@click.option("--config", "config_path", default=None, help="Path to config YAML file")
@click.option("--host", default=None, help="Override server host")
@click.option("--port", default=None, type=int, help="Override server port")
@click.option("--reload", is_flag=True, help="Auto-reload on file changes (dev mode)")
def main(config_path: str | None, host: str | None, port: int | None, reload: bool) -> None:
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


if __name__ == "__main__":
    main()
