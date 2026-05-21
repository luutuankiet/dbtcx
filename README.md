# dbtcx

> Agent-friendly companion to [`dbt-cloud-cli`](https://github.com/data-mie/dbt-cloud-cli) — solves the multi-step-job artifact trap.

## The problem this solves

`dbt-cloud-cli` defaults to **last-step** artifacts when you call `dbt-cloud run get-artifact`. For any dbt Cloud job that ends with `dbt docs generate` (so: virtually every production job), the resulting `run_results.json` has empty `adapter_response` on every row. The BigQuery / Snowflake / Redshift job ID, `slot_ms`, `bytes_processed` — all live in `adapter_response` for the actual `dbt run` / `dbt build` step. So post-run diagnostic pulls are useless without manual step discovery.

`dbtcx fetch-run` probes the run's steps until it finds the materialization step (`args.which ∈ {run, build, seed, snapshot}` AND ≥1 non-empty `adapter_response`), and pulls artifacts from THAT step.

## Quickstart

```bash
pip install dbtcx
# or
uv pip install dbtcx
```

Configure (token from dbt Cloud → Settings → API Tokens):

```bash
cat > .env <<'EOF'
DBT_CLOUD_API_TOKEN=dbtu_...
DBT_CLOUD_ACCOUNT_ID=12345
DBT_CLOUD_HOST=cloud.getdbt.com    # bare hostname, no scheme
EOF
```

Pull diagnostic artifacts for a run (auto-detect materialization step):

```bash
dbtcx fetch-run 12345678
```

Bundle a model's compiled SQL too:

```bash
dbtcx fetch-run 12345678 --model-path 'compiled/<project>/models/marts/my_model.sql'
```

Output → `./artifacts/run_<run_id>/`:

- `run_results.json` — pulled from the materialization step (`adapter_response` populated)
- `manifest.json` — full project state
- `manifest.slim.json` — agent-friendly index (no compiled SQL bodies; node deps + materialization + schema only)
- `<model>.compiled.sql` — if `--model-path` was given (falls back to default-step if not in materialization bundle)
- `.step_used` — marker file noting which step the artifacts came from

## CLI

### `dbtcx fetch-run`

```
dbtcx fetch-run <run_id> [--step N] [--model-path PATH] [--out-dir DIR] [--force] [--max-probe-steps N]
```

- `--step N` — manual override (skip auto-detect)
- `--force` — re-download even if files exist
- `--out-dir` — default `./artifacts/run_<id>/`
- `--max-probe-steps` — default 15

Idempotent: re-runs skip existing files unless `--force`. If the resolved step changes between runs (e.g. you pass `--step 4` after a default auto-detect), the previous artifacts are cleared to avoid mixing step-specific files.

### `dbtcx proxy`

```
dbtcx proxy <dbt-cloud-cli args>
```

Pass-through to `dbt-cloud` with `.env` already loaded. Examples:

```bash
dbtcx proxy run list --job-id 12345 --order-by '-id' --limit 5
dbtcx proxy account get
dbtcx proxy job list --account-id 12345
```

`dbt-cloud-cli` reads `DBT_CLOUD_API_TOKEN` / `DBT_CLOUD_ACCOUNT_ID` / `DBT_CLOUD_HOST` natively — no flag plumbing.

## Why agents love this

When a coding agent (Claude Code, Cursor, Aider, etc.) is asked to "diagnose why this dbt model is slow", the workflow is:

1. Find the latest production run.
2. Pull `run_results.json` to get `adapter_response.job_id` for the model.
3. Dump the warehouse query plan.
4. Rank hot stages.

Step 2 is where `dbt-cloud-cli` quietly fails on multi-step prod jobs — the agent gets `adapter_response: {}` and burns N round-trips figuring out which step it actually needed. `dbtcx fetch-run` collapses all that to one call, with progress logs that tell the agent exactly which step won.

## Configuration reference

| Var | Required | Notes |
|---|---|---|
| `DBT_CLOUD_API_TOKEN` | yes | Service token (`dbtu_...`) from dbt Cloud → Settings → API Tokens |
| `DBT_CLOUD_ACCOUNT_ID` | yes | Numeric account ID (visible in the URL after `/accounts/`) |
| `DBT_CLOUD_HOST` | yes | **Bare hostname only** — no `https://`. Multi-tenant US: `cloud.getdbt.com`; single-tenant US: `<prefix>.us1.dbt.com`; EMEA: `emea.dbt.com`; AU: `au.dbt.com` |
| `DBT_CLOUD_READONLY` | no | When `true`, suppresses destructive subcommands |

`.env` is loaded from the current working directory by default. Use `--env-file PATH` to point elsewhere:

```bash
dbtcx --env-file ~/.config/dbtcx/prod.env fetch-run 12345678
```

## Development

```bash
git clone https://github.com/luutuankiet/dbtcx.git
cd dbtcx
uv venv
uv pip install -e ".[dev]"
pytest
```

## Acknowledgements

Wraps [`dbt-cloud-cli`](https://github.com/data-mie/dbt-cloud-cli) by data-mie. This package adds the multi-step-aware fetcher + a thin env-loader pass-through; everything else delegates to the upstream CLI.

## License

MIT — see [LICENSE](./LICENSE).
