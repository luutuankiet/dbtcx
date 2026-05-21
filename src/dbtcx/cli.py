"""dbtcx CLI entrypoint — click group exposing fetch-run + proxy."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

from . import __version__
from .fetch_run import fetch_run_command


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="dbtcx")
@click.option(
    "--env-file",
    default=".env",
    show_default=True,
    metavar="PATH",
    help="Path to .env file with DBT_CLOUD_API_TOKEN, DBT_CLOUD_ACCOUNT_ID, DBT_CLOUD_HOST.",
)
@click.pass_context
def main(ctx: click.Context, env_file: str) -> None:
    """dbtcx — agent-friendly companion utilities for dbt-cloud-cli."""
    env_path = Path(env_file)
    if env_path.exists():
        load_dotenv(env_path)
    ctx.ensure_object(dict)
    ctx.obj["env_file"] = str(env_path)


@main.command("fetch-run")
@click.argument("run_id", type=int)
@click.option(
    "--step",
    type=int,
    default=None,
    help="Manual step override (default: auto-detect materialization step).",
)
@click.option(
    "--model-path",
    default=None,
    metavar="PATH",
    help="Optional compiled model SQL path inside the run artifacts "
    "(e.g. 'compiled/<project>/models/marts/my_model.sql').",
)
@click.option(
    "--out-dir",
    default=None,
    metavar="DIR",
    help="Output directory. Default: ./artifacts/run_<id>/",
)
@click.option(
    "--force",
    is_flag=True,
    help="Re-download even if files exist.",
)
@click.option(
    "--max-probe-steps",
    default=15,
    show_default=True,
    type=int,
    help="Upper bound for step probing during auto-detect.",
)
def fetch_run(
    run_id: int,
    step: int | None,
    model_path: str | None,
    out_dir: str | None,
    force: bool,
    max_probe_steps: int,
) -> None:
    """Pull diagnostic artifacts for a dbt Cloud run.

    Auto-detects the materialization step so adapter_response is populated
    (warehouse job ID, slot_ms, bytes_processed). Pass --step N to override.
    """
    rc = fetch_run_command(
        run_id=run_id,
        step=step,
        model_path=model_path,
        out_dir=out_dir,
        force=force,
        max_probe_steps=max_probe_steps,
    )
    sys.exit(rc)


@main.command(
    context_settings={"ignore_unknown_options": True, "help_option_names": []}
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def proxy(args: tuple[str, ...]) -> None:
    """Pass-through to dbt-cloud-cli with .env already loaded.

    Example:

        dbtcx proxy run list --job-id 12345 --order-by '-id' --limit 5
    """
    if not args:
        click.echo("usage: dbtcx proxy <dbt-cloud-cli command and args>", err=True)
        sys.exit(2)
    try:
        sys.exit(subprocess.call(["dbt-cloud", *args]))
    except FileNotFoundError:
        click.echo(
            "error: `dbt-cloud` CLI not on PATH. "
            "It ships transitively with `dbtcx`; reinstall or check your venv.",
            err=True,
        )
        sys.exit(127)


if __name__ == "__main__":  # pragma: no cover
    main()
