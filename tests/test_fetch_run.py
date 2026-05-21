"""Unit tests for fetch_run — subprocess to `dbt-cloud` is mocked end-to-end."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from dbtcx import fetch_run as fr


# ---------- _is_valid_json_file ----------


class TestIsValidJsonFile:
    def test_returns_true_on_valid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "ok.json"
        p.write_text('{"a": 1}')
        assert fr._is_valid_json_file(p) is True

    def test_returns_false_on_text(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("Not found.")
        assert fr._is_valid_json_file(p) is False

    def test_returns_false_on_missing(self, tmp_path: Path) -> None:
        assert fr._is_valid_json_file(tmp_path / "nope.json") is False

    def test_returns_false_on_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.json"
        p.write_text("")
        assert fr._is_valid_json_file(p) is False


# ---------- _human_bytes ----------


class TestHumanBytes:
    @pytest.mark.parametrize(
        "n,expected",
        [
            (0, "0B"),
            (512, "512B"),
            (1024, "1.0K"),
            (2048, "2.0K"),
            (1024 * 1024, "1.0M"),
            (1024 * 1024 * 1024, "1.0G"),
        ],
    )
    def test_formats(self, n: int, expected: str) -> None:
        assert fr._human_bytes(n) == expected


# ---------- detect_materialization_step ----------


def _make_run_results(which: str, n_with_adapter: int = 1) -> dict:
    """Synthesize a minimal run_results.json shape for the probe to inspect."""
    results = []
    for i in range(n_with_adapter):
        results.append(
            {
                "unique_id": f"model.demo.m{i}",
                "adapter_response": {"_message": "OK", "job_id": f"bq-{i}"},
            }
        )
    return {"args": {"which": which}, "results": results}


class TestDetectMaterializationStep:
    def test_finds_run_step_after_skipping_404s(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Steps 1-3 are non-dbt (404), step 4 is the materialization."""
        materialization = _make_run_results("run", n_with_adapter=3)

        call_count = {"n": 0}

        def fake_call_dbtc(args, out_file=None):
            call_count["n"] += 1
            step_idx = args.index("--step")
            step = int(args[step_idx + 1])
            if step < 4:
                # 404 → nonzero exit, no file written (mimics dbtc behavior).
                return 1, ""
            if step == 4:
                Path(out_file).write_text(json.dumps(materialization))
                return 0, ""
            return 1, ""

        monkeypatch.setattr(fr, "_call_dbtc", fake_call_dbtc)
        result = fr.detect_materialization_step(run_id=12345, max_probe_steps=10)
        assert result is not None
        step, path = result
        assert step == 4
        assert path.exists()
        # Probed file is JSON of the materialization step.
        assert json.loads(path.read_text())["args"]["which"] == "run"
        path.unlink()  # cleanup

    def test_skips_docs_generate_step(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Step 1 = docs-generate (empty adapter_response), step 2 = build."""
        docs = {
            "args": {"which": "generate"},
            "results": [{"adapter_response": {}}],
        }
        build = _make_run_results("build", n_with_adapter=2)

        def fake_call_dbtc(args, out_file=None):
            step_idx = args.index("--step")
            step = int(args[step_idx + 1])
            if step == 1:
                Path(out_file).write_text(json.dumps(docs))
                return 0, ""
            if step == 2:
                Path(out_file).write_text(json.dumps(build))
                return 0, ""
            return 1, ""

        monkeypatch.setattr(fr, "_call_dbtc", fake_call_dbtc)
        result = fr.detect_materialization_step(run_id=12345, max_probe_steps=5)
        assert result is not None
        step, path = result
        assert step == 2  # skipped step 1 (empty adapter_response)
        path.unlink()

    def test_returns_none_on_full_miss(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All probed steps 404."""
        def fake_call_dbtc(args, out_file=None):
            return 1, ""

        monkeypatch.setattr(fr, "_call_dbtc", fake_call_dbtc)
        assert fr.detect_materialization_step(run_id=12345, max_probe_steps=3) is None

    def test_skips_non_json_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """dbtc returns exit 0 but writes 'Not found.' as the body (known quirk)."""
        good = _make_run_results("run")

        def fake_call_dbtc(args, out_file=None):
            step_idx = args.index("--step")
            step = int(args[step_idx + 1])
            if step == 1:
                Path(out_file).write_text("Not found.")
                return 0, ""  # exit 0 but garbage body
            if step == 2:
                Path(out_file).write_text(json.dumps(good))
                return 0, ""
            return 1, ""

        monkeypatch.setattr(fr, "_call_dbtc", fake_call_dbtc)
        result = fr.detect_materialization_step(run_id=12345, max_probe_steps=3)
        assert result is not None
        step, path = result
        assert step == 2
        path.unlink()


# ---------- fetch_run_command (end-to-end orchestration) ----------


class TestFetchRunCommand:
    def test_exits_when_dbtc_missing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(fr.shutil, "which", lambda _: None)
        rc = fr.fetch_run_command(run_id=12345)
        assert rc == 1
        err = capsys.readouterr().err
        assert "dbt-cloud" in err and "PATH" in err

    def test_step_override_skips_probe(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--step N path: no probe, fetch artifacts directly with --step 4."""
        monkeypatch.setattr(fr.shutil, "which", lambda _: "/usr/bin/dbt-cloud")

        recorded: list[list[str]] = []
        manifest_body = {
            "metadata": {"project_name": "demo"},
            "nodes": {
                "model.demo.m1": {
                    "unique_id": "model.demo.m1",
                    "name": "m1",
                    "resource_type": "model",
                    "config": {"materialized": "table"},
                    "depends_on": {"nodes": [], "macros": []},
                }
            },
            "sources": {},
            "exposures": {},
            "macros": {},
        }
        run_results_body = _make_run_results("run", n_with_adapter=1)

        def fake_call_dbtc(args, out_file=None):
            recorded.append(args)
            path_idx = args.index("--path")
            remote = args[path_idx + 1]
            if remote == "run_results.json":
                Path(out_file).write_text(json.dumps(run_results_body))
            elif remote == "manifest.json":
                Path(out_file).write_text(json.dumps(manifest_body))
            return 0, ""

        monkeypatch.setattr(fr, "_call_dbtc", fake_call_dbtc)

        rc = fr.fetch_run_command(
            run_id=999,
            step=4,
            out_dir=str(tmp_path),
        )
        assert rc == 0

        # Probe NOT invoked → no extra `dbt-cloud run get-artifact --step N
        # --path run_results.json` walks. Every call carries --step 4.
        for args in recorded:
            assert "--step" in args
            assert args[args.index("--step") + 1] == "4"

        # Artifacts landed.
        assert (tmp_path / "run_results.json").exists()
        assert (tmp_path / "manifest.json").exists()
        assert (tmp_path / "manifest.slim.json").exists()
        assert (tmp_path / ".step_used").read_text().strip() == "4"

        # Slim manifest is well-formed.
        slim = json.loads((tmp_path / "manifest.slim.json").read_text())
        assert slim["metadata"]["project_name"] == "demo"
        assert slim["nodes"][0]["materialized"] == "table"

    def test_step_change_clears_stale_artifacts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When .step_used flips, prior artifacts get wiped before re-fetch."""
        monkeypatch.setattr(fr.shutil, "which", lambda _: "/usr/bin/dbt-cloud")

        # Pre-seed: prior fetch was step 7, leftover files exist.
        (tmp_path / ".step_used").write_text("7")
        (tmp_path / "run_results.json").write_text(
            json.dumps({"stale": True})
        )
        (tmp_path / "manifest.json").write_text(json.dumps({"stale": True}))
        (tmp_path / "stale.compiled.sql").write_text("-- stale")

        manifest_body = {"nodes": {}, "sources": {}, "exposures": {}, "macros": {}}
        run_results_body = _make_run_results("run")

        def fake_call_dbtc(args, out_file=None):
            path_idx = args.index("--path")
            remote = args[path_idx + 1]
            if remote == "run_results.json":
                Path(out_file).write_text(json.dumps(run_results_body))
            elif remote == "manifest.json":
                Path(out_file).write_text(json.dumps(manifest_body))
            return 0, ""

        monkeypatch.setattr(fr, "_call_dbtc", fake_call_dbtc)

        rc = fr.fetch_run_command(
            run_id=999, step=4, out_dir=str(tmp_path), force=False
        )
        assert rc == 0

        # Step flipped → stale.compiled.sql nuked, run_results.json refreshed.
        assert not (tmp_path / "stale.compiled.sql").exists()
        rr = json.loads((tmp_path / "run_results.json").read_text())
        assert rr.get("stale") is None
        assert (tmp_path / ".step_used").read_text().strip() == "4"

    def test_compiled_sql_fallback_to_default_step(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Compiled SQL misses at --step 4, falls back to default-step success."""
        monkeypatch.setattr(fr.shutil, "which", lambda _: "/usr/bin/dbt-cloud")

        manifest_body = {"nodes": {}, "sources": {}, "exposures": {}, "macros": {}}
        rr_body = _make_run_results("run")

        def fake_call_dbtc(args, out_file=None):
            path_idx = args.index("--path")
            remote = args[path_idx + 1]
            if remote == "run_results.json":
                Path(out_file).write_text(json.dumps(rr_body))
                return 0, ""
            if remote == "manifest.json":
                Path(out_file).write_text(json.dumps(manifest_body))
                return 0, ""
            if remote.endswith(".sql"):
                # Miss at --step N, hit on default (no --step).
                if "--step" in args:
                    return 1, ""
                Path(out_file).write_text("-- compiled body")
                return 0, ""
            return 1, ""

        monkeypatch.setattr(fr, "_call_dbtc", fake_call_dbtc)

        rc = fr.fetch_run_command(
            run_id=999,
            step=4,
            model_path="compiled/demo/models/m.sql",
            out_dir=str(tmp_path),
        )
        assert rc == 0
        sql_dest = tmp_path / "m.compiled.sql"
        assert sql_dest.exists()
        assert sql_dest.read_text() == "-- compiled body"


# ---------- _call_dbtc ----------


class TestCallDbtc:
    def test_raises_on_missing_binary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args, **kwargs):
            raise FileNotFoundError("dbt-cloud")

        monkeypatch.setattr(fr.subprocess, "run", boom)
        with pytest.raises(RuntimeError, match="dbt-cloud"):
            fr._call_dbtc(["run", "list"])
