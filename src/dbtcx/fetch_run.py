"""Python port of fetch-run.sh — auto-detect materialization step + pull artifacts."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Tuple

MATERIALIZATION_WHICH = {"run", "build", "seed", "snapshot"}
DEFAULT_MAX_PROBE_STEPS = 15


# ---------- subprocess helpers ----------


def _call_dbtc(
    args: list[str], out_file: Optional[Path] = None
) -> Tuple[int, str]:
    """Call `dbt-cloud` with given args. Returns (returncode, stdout).

    `-f` is appended automatically when out_file is given so callers don't
    have to thread it through every call site.
    """
    cmd = ["dbt-cloud", *args]
    if out_file is not None:
        cmd.extend(["-f", str(out_file)])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise RuntimeError(
            "`dbt-cloud` not on PATH. Install with `pip install dbt-cloud-cli` "
            "(or reinstall dbtcx — dbt-cloud-cli ships transitively)."
        ) from e
    return proc.returncode, proc.stdout


def _is_valid_json_file(path: Path) -> bool:
    """True iff `path` exists, is non-empty, and parses as JSON."""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return False
        with open(path) as f:
            json.load(f)
        return True
    except (json.JSONDecodeError, OSError):
        return False


def _human_bytes(n: float) -> str:
    """Mimic `du -h`-ish output for log lines."""
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{int(n)}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


# ---------- step probing ----------


def detect_materialization_step(
    run_id: int, max_probe_steps: int = DEFAULT_MAX_PROBE_STEPS
) -> Optional[Tuple[int, Path]]:
    """Probe steps 1..max_probe_steps for the materialization step.

    Returns (step_number, path_to_run_results_json) on hit; None on miss.
    Caller is responsible for cleaning up the returned path if non-None.

    A step qualifies when BOTH:
      * `args.which` ∈ {run, build, seed, snapshot}
      * ≥1 result row has non-empty `adapter_response`

    Steps that 404 (non-dbt step) or write non-JSON text are skipped, not fatal.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="dbtcx-probe-"))
    try:
        print(f"== auto-detecting materialization step (probe 1..{max_probe_steps}) ==")
        for step in range(1, max_probe_steps + 1):
            probe = tmp_dir / f"probe_step{step}.json"
            code, _ = _call_dbtc(
                [
                    "run",
                    "get-artifact",
                    "--run-id",
                    str(run_id),
                    "--step",
                    str(step),
                    "--path",
                    "run_results.json",
                ],
                out_file=probe,
            )
            if code != 0:
                print(
                    f"  [step {step}] no run_results.json "
                    f"(404 or non-dbt step), skipping"
                )
                continue
            if not _is_valid_json_file(probe):
                print(
                    f"  [step {step}] run_results.json present but not "
                    f"valid JSON, skipping"
                )
                continue
            with open(probe) as f:
                data = json.load(f)
            which = data.get("args", {}).get("which", "?")
            has_adapter = sum(
                1
                for r in data.get("results", [])
                if r.get("adapter_response", {})
            )
            print(
                f"  [step {step}] which={which}, "
                f"results_with_adapter_response={has_adapter}"
            )
            if which in MATERIALIZATION_WHICH and has_adapter > 0:
                # Stash the probe outside tmp_dir so the finally block doesn't
                # nuke it — caller still owns lifetime via the returned path.
                kept = (
                    Path(tempfile.gettempdir())
                    / f"dbtcx-step{step}-{run_id}.json"
                )
                shutil.copy2(probe, kept)
                return step, kept
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------- artifact fetchers ----------


def _fetch_strict(
    run_id: int,
    remote_path: str,
    dest: Path,
    step_args: list[str],
    force: bool,
) -> int:
    """Pull `remote_path` to `dest`. No fallback. Returns exit code."""
    if dest.exists() and dest.stat().st_size > 0 and not force:
        print(f"  [skip] {dest.name} (exists, {_human_bytes(dest.stat().st_size)})")
        return 0

    print(f"  [pull] {dest.name} ... ", end="", flush=True)
    code, _ = _call_dbtc(
        [
            "run",
            "get-artifact",
            "--run-id",
            str(run_id),
            *step_args,
            "--path",
            remote_path,
        ],
        out_file=dest,
    )
    if code == 0:
        print(f"ok ({_human_bytes(dest.stat().st_size)})")
        return 0
    print("FAIL", file=sys.stderr)
    if dest.exists():
        dest.unlink()
    return code


def _fetch_with_fallback(
    run_id: int,
    remote_path: str,
    dest: Path,
    step_args: list[str],
    force: bool,
) -> int:
    """Pull with retry against default-step on miss.

    Used for compiled/*.sql — dbt Cloud bundles those into the docs-generate
    (last) step's artifacts, NOT the materialization step. Returns 0 on
    success (either attempt), nonzero only when both fail.
    """
    if dest.exists() and dest.stat().st_size > 0 and not force:
        print(f"  [skip] {dest.name} (exists, {_human_bytes(dest.stat().st_size)})")
        return 0

    print(f"  [pull] {dest.name} ... ", end="", flush=True)
    code, _ = _call_dbtc(
        [
            "run",
            "get-artifact",
            "--run-id",
            str(run_id),
            *step_args,
            "--path",
            remote_path,
        ],
        out_file=dest,
    )
    if code == 0:
        print(f"ok ({_human_bytes(dest.stat().st_size)})")
        return 0

    # Retry without --step → dbtc default (last step).
    if step_args:
        if dest.exists():
            dest.unlink()
        step_label = step_args[1] if len(step_args) > 1 else "?"
        print(
            f"miss at step {step_label}, retry default ... ",
            end="",
            flush=True,
        )
        code, _ = _call_dbtc(
            [
                "run",
                "get-artifact",
                "--run-id",
                str(run_id),
                "--path",
                remote_path,
            ],
            out_file=dest,
        )
        if code == 0:
            print(
                f"ok ({_human_bytes(dest.stat().st_size)}) "
                f"[fallback to default step]"
            )
            return 0

    print("FAIL", file=sys.stderr)
    if dest.exists():
        dest.unlink()
    return code


# ---------- manifest slim ----------


def _build_slim_manifest(manifest_path: Path, slim_path: Path) -> None:
    """Distill manifest.json → agent-friendly index. Drops compiled SQL bodies.

    Matches the jq filter shipped in the bash version 1:1 — keep field names
    aligned with downstream tooling that already consumes manifest.slim.json.
    """
    print("  [slim] manifest.slim.json ... ", end="", flush=True)
    with open(manifest_path) as f:
        m = json.load(f)
    slim = {
        "metadata": m.get("metadata"),
        "nodes": [
            {
                "unique_id": v.get("unique_id"),
                "name": v.get("name"),
                "resource_type": v.get("resource_type"),
                "package_name": v.get("package_name"),
                "database": v.get("database"),
                "schema": v.get("schema"),
                "alias": v.get("alias"),
                "original_file_path": v.get("original_file_path"),
                "materialized": (v.get("config") or {}).get("materialized"),
                "incremental_strategy": (v.get("config") or {}).get(
                    "incremental_strategy"
                ),
                "on_schema_change": (v.get("config") or {}).get("on_schema_change"),
                "tags": v.get("tags"),
                "depends_on_nodes": (v.get("depends_on") or {}).get("nodes"),
                "depends_on_macros": (v.get("depends_on") or {}).get("macros"),
            }
            for v in (m.get("nodes") or {}).values()
        ],
        "sources": [
            {
                "unique_id": v.get("unique_id"),
                "name": v.get("name"),
                "source_name": v.get("source_name"),
                "database": v.get("database"),
                "schema": v.get("schema"),
                "identifier": v.get("identifier"),
                "loaded_at_field": v.get("loaded_at_field"),
                "freshness": v.get("freshness"),
            }
            for v in (m.get("sources") or {}).values()
        ],
        "exposures_count": len(m.get("exposures") or {}),
        "macros_count": len(m.get("macros") or {}),
    }
    with open(slim_path, "w") as f:
        json.dump(slim, f, indent=2)
    print(f"ok ({_human_bytes(slim_path.stat().st_size)})")


# ---------- orchestrator ----------


def fetch_run_command(
    run_id: int,
    step: Optional[int] = None,
    model_path: Optional[str] = None,
    out_dir: Optional[str] = None,
    force: bool = False,
    max_probe_steps: int = DEFAULT_MAX_PROBE_STEPS,
) -> int:
    """End-to-end: resolve step → pull artifacts → slim manifest.

    Returns process exit code (0 on success, nonzero on hard failure).
    """
    if not shutil.which("dbt-cloud"):
        sys.stderr.write(
            "error: `dbt-cloud` CLI not on PATH. "
            "Install with `pip install dbt-cloud-cli` "
            "(or reinstall dbtcx — it ships transitively).\n"
        )
        return 1

    out_dir_path = Path(out_dir) if out_dir else Path(f"artifacts/run_{run_id}")
    out_dir_path.mkdir(parents=True, exist_ok=True)

    step_used: Optional[int] = None
    probed_path: Optional[Path] = None

    if step is not None:
        print(f"== using step override: {step} ==")
        step_used = step
    else:
        result = detect_materialization_step(run_id, max_probe_steps)
        if result is not None:
            step_used, probed_path = result
            print(f"== auto-detected step: {step_used} ==")
        else:
            print(
                f"== WARNING: no materialization step found in "
                f"1..{max_probe_steps}; falling back to dbtc default "
                f"(last step) ==",
                file=sys.stderr,
            )

    # Step-change detection: clear stale artifacts when step flips between runs.
    step_marker = out_dir_path / ".step_used"
    step_current = str(step_used) if step_used is not None else "default"
    if step_marker.exists():
        prev = step_marker.read_text().strip()
        if prev != step_current:
            print(
                f"== step changed: {prev} -> {step_current}; "
                f"clearing {out_dir_path} =="
            )
            for p in out_dir_path.glob("*"):
                if p.is_file() and p.name != ".step_used":
                    p.unlink()
    step_marker.write_text(step_current)

    step_args = ["--step", str(step_used)] if step_used is not None else []

    print(f"== fetch-run {run_id} -> {out_dir_path} ==")

    # run_results.json — reuse the probe instead of re-downloading when present.
    rr_path = out_dir_path / "run_results.json"
    if probed_path is not None and (not rr_path.exists() or force):
        shutil.copy2(probed_path, rr_path)
        print(
            f"  [pull] run_results.json ... "
            f"ok ({_human_bytes(rr_path.stat().st_size)}) [from probe]"
        )
    else:
        rc = _fetch_strict(run_id, "run_results.json", rr_path, step_args, force)
        if rc != 0:
            return rc

    # manifest.json — strict (no fallback; always present in any dbt step).
    mf_path = out_dir_path / "manifest.json"
    rc = _fetch_strict(run_id, "manifest.json", mf_path, step_args, force)
    if rc != 0:
        return rc

    # Optional compiled SQL — fallback to default-step because docs-generate
    # is where dbt Cloud actually bundles compiled/*.sql.
    if model_path:
        base = Path(model_path).stem
        sql_path = out_dir_path / f"{base}.compiled.sql"
        _fetch_with_fallback(run_id, model_path, sql_path, step_args, force)

    # Slim manifest — local-only post-processing, idempotent.
    slim_path = out_dir_path / "manifest.slim.json"
    if slim_path.exists() and slim_path.stat().st_size > 0 and not force:
        print(
            f"  [skip] manifest.slim.json (exists, "
            f"{_human_bytes(slim_path.stat().st_size)})"
        )
    else:
        _build_slim_manifest(mf_path, slim_path)

    print("== done ==")
    print(f"step_used={step_current}")

    # Cleanup the kept probe file (best-effort).
    if probed_path is not None:
        try:
            probed_path.unlink()
        except OSError:
            pass

    return 0
