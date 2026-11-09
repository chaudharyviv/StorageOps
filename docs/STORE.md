# Ingest store

The DuckDB file written by `python -m ingest.run --store <db>`. It holds the validated metrics
that features and models read, what validation rejected, and a record of each run. It is never
the same file as the simulator's metrics file, and it holds no scenario labels.

The features component adds a fourth table, `features`, to the same file; it is documented in
`docs/FEATURES.md`.

A test (`tests/test_ingest_store_doc.py`) compares this page with the real schema, so a change
to a table must change this file too. Types are DuckDB's own names.

## clean_metrics

Rows that passed validation, in the `MetricRecord` layout. This is the table features read.

| Column | Type | Nullable | Notes |
| --- | --- | --- | --- |
| `ts` | TIMESTAMP WITH TIME ZONE | NO | Sample time, UTC |
| `cluster` | VARCHAR | NO | |
| `object_type` | VARCHAR | NO | One of the `ObjectType` values |
| `object_id` | VARCHAR | NO | |
| `metric` | VARCHAR | NO | A name listed in `docs/METRICS.md` |
| `value` | DOUBLE | NO | Finite, inside the bounds in `docs/metric_rules.yaml` |
| `unit` | VARCHAR | NO | Matches the catalogue unit for the metric |

**Primary key:** `cluster`, `object_type`, `object_id`, `metric`, `ts`

Loading uses insert-or-ignore on the key, so running the same ingest again adds nothing.

## quarantine

What validation rejected, with the reason. A row-level entry holds the rejected row as JSON in
`payload`. A `gap` entry has no row: `ts` is the first missing minute and `detail` says how many
minutes are missing. A `schema_batch` entry stands for a whole batch, which is kept as a Parquet
file in `<store>.rejected/`.

| Column | Type | Nullable | Notes |
| --- | --- | --- | --- |
| `quarantine_key` | VARCHAR | NO | Deterministic hash of the finding; the same finding always gets the same key |
| `run_id` | VARCHAR | NO | The run that first quarantined it |
| `batch_id` | BIGINT | YES | Queue sequence number; empty for end-of-stream gaps |
| `reason` | VARCHAR | NO | First fault code, in precedence order |
| `reasons` | VARCHAR | NO | All fault codes for the row, comma separated |
| `detail` | VARCHAR | NO | Human-readable explanation |
| `object_type` | VARCHAR | YES | |
| `object_id` | VARCHAR | YES | |
| `metric` | VARCHAR | YES | |
| `ts` | TIMESTAMP WITH TIME ZONE | YES | |
| `payload` | VARCHAR | YES | The rejected row as JSON; empty for gaps and rejected batches |

**Primary key:** `quarantine_key`

Fault codes, in precedence order: `schema_batch`, `schema_row`, `unknown_metric`,
`unit_mismatch`, `out_of_range`, `duplicate`, `out_of_order`, `gap`.

## validation_runs

One row per ingest run, written when the run starts and completed when it ends. A run that was
interrupted keeps `status` as `running`.

| Column | Type | Nullable | Notes |
| --- | --- | --- | --- |
| `run_id` | VARCHAR | NO | |
| `started_at` | TIMESTAMP WITH TIME ZONE | NO | |
| `finished_at` | TIMESTAMP WITH TIME ZONE | YES | Empty while running |
| `status` | VARCHAR | NO | `running`, `passed` or `failed` |
| `metrics_path` | VARCHAR | NO | The source metrics file, as given |
| `batch_minutes` | INTEGER | NO | |
| `batches` | INTEGER | YES | |
| `rows_read` | BIGINT | YES | |
| `rows_clean` | BIGINT | YES | Rows that passed validation |
| `rows_quarantined` | BIGINT | YES | Rows rejected (gaps are minutes, not rows, and are not counted here) |
| `rows_loaded` | BIGINT | YES | Clean rows that were new to `clean_metrics` |
| `rows_already_present` | BIGINT | YES | Clean rows that were already there |
| `fault_counts` | VARCHAR | YES | JSON object: fault code to count |
| `report` | VARCHAR | YES | The full `RunReport` as JSON (schema version 1) |

**Primary key:** `run_id`
