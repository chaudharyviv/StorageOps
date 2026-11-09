# Features

Features computed from `clean_metrics` for training and serving. One implementation serves both: `compute_features_range` (training) and `compute_features_at` (serving) run the same code, so they give identical values.

This file is generated from `features/features.yaml` by `python -m features.catalogue`; do not edit it by hand. A test fails if it drifts from the code.

## Rules

- **No future data.** A feature at time `t` uses only samples at or before `t`. A series has no row at `t` until it has a sample.
- **Windows.** A window of `w` minutes ending at `t` covers the samples at minutes `t-w+1` to `t`. `t` is floored to the whole minute.
- **Missing data.** A window needs at least 50% of its minutes present, or its feature is null (it never raises). `std` and `slope` also need two samples. The first hours of a series therefore have null long-window features.
- **Null, not NaN.** A feature that cannot be computed is stored as SQL NULL.
- **One cluster.** The store must hold a single cluster; mixed clusters are an error.
- **Reads `clean_metrics` only.** Not the rows validation rejected, and never the labels.

## Windows

| Window | Minutes |
| --- | --- |
| `15m` | 15 |
| `1h` | 60 |
| `6h` | 360 |
| `24h` | 1440 |

## Functions

| Function | What it computes |
| --- | --- |
| `last` | Latest present sample in the window (the current level); null if none. It carries a value forward for at most the window length. |
| `mean` | Mean of the present samples. |
| `min` | Smallest present sample. |
| `max` | Largest present sample. |
| `std` | Population standard deviation of the present samples; needs two. |
| `slope` | Least-squares slope of value against time, in metric units per hour; needs two samples. |
| `time_to_full` | Minutes until the metric reaches `full` if it keeps rising at the window's slope. 0 once the current level is at or above `full`; `cap_minutes` when the slope is zero or negative, or the estimate is longer than the cap; null without a level or a slope. |

## The features table

Written to the ingest store by `python -m features.run --store <db> --step 5`. Computing the same range again replaces the same rows, so the table is unchanged. After editing `features.yaml`, run with `--rebuild` to drop features that no longer exist.

| Column | Type | Nullable | Notes |
| --- | --- | --- | --- |
| `ts` | TIMESTAMP WITH TIME ZONE | NO | Evaluation time (UTC); uses samples at or before it |
| `object_type` | VARCHAR | NO | One of the `ObjectType` values |
| `object_id` | VARCHAR | NO |  |
| `feature_name` | VARCHAR | NO | From features.yaml, e.g. `space.used_percent__slope_6h` |
| `value` | DOUBLE | YES | Null means not computable here (too little data in the window) |

**Primary key:** `object_type`, `object_id`, `feature_name`, `ts`

`read_features(store, object_type)` returns a stored object type as one wide Polars frame (a row per `ts` and `object_id`, a column per feature); `pivot_features` does the same for a frame from the compute functions.

## What answers what

| Need | Function | Metrics | Features |
| --- | --- | --- | --- |
| current_level | `last` | 17 metrics | 17 |
| rolling_statistics | `mean`, `min`, `max`, `std` | 16 metrics | 256 |
| growth_rate | `slope` | 16 metrics | 64 |
| time_to_full | `time_to_full` | `snapshot.reserve_used_percent`, `space.used_percent` | 6 |
| snapshot_to_active_ratio_trend | `slope` | `snapshot.to_active_ratio` | 4 |
| efficiency_ratio_trend | `slope` | `efficiency.ratio` | 4 |
| latency_p95_trend | `slope` | `latency.read_p95_ms`, `latency.write_p95_ms` | 8 |
| snapmirror_lag_trend | `slope` | `lag_seconds` | 4 |

## Templates

| Name | Metrics | Excluding | Function | Windows | Parameters |
| --- | --- | --- | --- | --- | --- |
| `{metric}__level` | all |  | `last` | 15m |  |
| `{metric}__mean_{window}` | all | `space.autogrow_max_bytes` | `mean` | all |  |
| `{metric}__min_{window}` | all | `space.autogrow_max_bytes` | `min` | all |  |
| `{metric}__max_{window}` | all | `space.autogrow_max_bytes` | `max` | all |  |
| `{metric}__std_{window}` | all | `space.autogrow_max_bytes` | `std` | all |  |
| `{metric}__slope_{window}` | all | `space.autogrow_max_bytes` | `slope` | all |  |
| `{metric}__ttf_{window}` | `space.used_percent`, `snapshot.reserve_used_percent` |  | `time_to_full` | 1h, 6h, 24h | full=100, cap_minutes=43200 |

## Features by object type

### aggregate (66 features)

| Feature | Source metric | Function | Window | Parameters |
| --- | --- | --- | --- | --- |
| `space.size_bytes__level` | `space.size_bytes` | `last` | 15m |  |
| `space.size_bytes__max_15m` | `space.size_bytes` | `max` | 15m |  |
| `space.size_bytes__max_1h` | `space.size_bytes` | `max` | 1h |  |
| `space.size_bytes__max_24h` | `space.size_bytes` | `max` | 24h |  |
| `space.size_bytes__max_6h` | `space.size_bytes` | `max` | 6h |  |
| `space.size_bytes__mean_15m` | `space.size_bytes` | `mean` | 15m |  |
| `space.size_bytes__mean_1h` | `space.size_bytes` | `mean` | 1h |  |
| `space.size_bytes__mean_24h` | `space.size_bytes` | `mean` | 24h |  |
| `space.size_bytes__mean_6h` | `space.size_bytes` | `mean` | 6h |  |
| `space.size_bytes__min_15m` | `space.size_bytes` | `min` | 15m |  |
| `space.size_bytes__min_1h` | `space.size_bytes` | `min` | 1h |  |
| `space.size_bytes__min_24h` | `space.size_bytes` | `min` | 24h |  |
| `space.size_bytes__min_6h` | `space.size_bytes` | `min` | 6h |  |
| `space.size_bytes__slope_15m` | `space.size_bytes` | `slope` | 15m |  |
| `space.size_bytes__slope_1h` | `space.size_bytes` | `slope` | 1h |  |
| `space.size_bytes__slope_24h` | `space.size_bytes` | `slope` | 24h |  |
| `space.size_bytes__slope_6h` | `space.size_bytes` | `slope` | 6h |  |
| `space.size_bytes__std_15m` | `space.size_bytes` | `std` | 15m |  |
| `space.size_bytes__std_1h` | `space.size_bytes` | `std` | 1h |  |
| `space.size_bytes__std_24h` | `space.size_bytes` | `std` | 24h |  |
| `space.size_bytes__std_6h` | `space.size_bytes` | `std` | 6h |  |
| `space.used_bytes__level` | `space.used_bytes` | `last` | 15m |  |
| `space.used_bytes__max_15m` | `space.used_bytes` | `max` | 15m |  |
| `space.used_bytes__max_1h` | `space.used_bytes` | `max` | 1h |  |
| `space.used_bytes__max_24h` | `space.used_bytes` | `max` | 24h |  |
| `space.used_bytes__max_6h` | `space.used_bytes` | `max` | 6h |  |
| `space.used_bytes__mean_15m` | `space.used_bytes` | `mean` | 15m |  |
| `space.used_bytes__mean_1h` | `space.used_bytes` | `mean` | 1h |  |
| `space.used_bytes__mean_24h` | `space.used_bytes` | `mean` | 24h |  |
| `space.used_bytes__mean_6h` | `space.used_bytes` | `mean` | 6h |  |
| `space.used_bytes__min_15m` | `space.used_bytes` | `min` | 15m |  |
| `space.used_bytes__min_1h` | `space.used_bytes` | `min` | 1h |  |
| `space.used_bytes__min_24h` | `space.used_bytes` | `min` | 24h |  |
| `space.used_bytes__min_6h` | `space.used_bytes` | `min` | 6h |  |
| `space.used_bytes__slope_15m` | `space.used_bytes` | `slope` | 15m |  |
| `space.used_bytes__slope_1h` | `space.used_bytes` | `slope` | 1h |  |
| `space.used_bytes__slope_24h` | `space.used_bytes` | `slope` | 24h |  |
| `space.used_bytes__slope_6h` | `space.used_bytes` | `slope` | 6h |  |
| `space.used_bytes__std_15m` | `space.used_bytes` | `std` | 15m |  |
| `space.used_bytes__std_1h` | `space.used_bytes` | `std` | 1h |  |
| `space.used_bytes__std_24h` | `space.used_bytes` | `std` | 24h |  |
| `space.used_bytes__std_6h` | `space.used_bytes` | `std` | 6h |  |
| `space.used_percent__level` | `space.used_percent` | `last` | 15m |  |
| `space.used_percent__max_15m` | `space.used_percent` | `max` | 15m |  |
| `space.used_percent__max_1h` | `space.used_percent` | `max` | 1h |  |
| `space.used_percent__max_24h` | `space.used_percent` | `max` | 24h |  |
| `space.used_percent__max_6h` | `space.used_percent` | `max` | 6h |  |
| `space.used_percent__mean_15m` | `space.used_percent` | `mean` | 15m |  |
| `space.used_percent__mean_1h` | `space.used_percent` | `mean` | 1h |  |
| `space.used_percent__mean_24h` | `space.used_percent` | `mean` | 24h |  |
| `space.used_percent__mean_6h` | `space.used_percent` | `mean` | 6h |  |
| `space.used_percent__min_15m` | `space.used_percent` | `min` | 15m |  |
| `space.used_percent__min_1h` | `space.used_percent` | `min` | 1h |  |
| `space.used_percent__min_24h` | `space.used_percent` | `min` | 24h |  |
| `space.used_percent__min_6h` | `space.used_percent` | `min` | 6h |  |
| `space.used_percent__slope_15m` | `space.used_percent` | `slope` | 15m |  |
| `space.used_percent__slope_1h` | `space.used_percent` | `slope` | 1h |  |
| `space.used_percent__slope_24h` | `space.used_percent` | `slope` | 24h |  |
| `space.used_percent__slope_6h` | `space.used_percent` | `slope` | 6h |  |
| `space.used_percent__std_15m` | `space.used_percent` | `std` | 15m |  |
| `space.used_percent__std_1h` | `space.used_percent` | `std` | 1h |  |
| `space.used_percent__std_24h` | `space.used_percent` | `std` | 24h |  |
| `space.used_percent__std_6h` | `space.used_percent` | `std` | 6h |  |
| `space.used_percent__ttf_1h` | `space.used_percent` | `time_to_full` | 1h | cap_minutes=43200, full=100 |
| `space.used_percent__ttf_24h` | `space.used_percent` | `time_to_full` | 24h | cap_minutes=43200, full=100 |
| `space.used_percent__ttf_6h` | `space.used_percent` | `time_to_full` | 6h | cap_minutes=43200, full=100 |

### qos (84 features)

| Feature | Source metric | Function | Window | Parameters |
| --- | --- | --- | --- | --- |
| `iops__level` | `iops` | `last` | 15m |  |
| `iops__max_15m` | `iops` | `max` | 15m |  |
| `iops__max_1h` | `iops` | `max` | 1h |  |
| `iops__max_24h` | `iops` | `max` | 24h |  |
| `iops__max_6h` | `iops` | `max` | 6h |  |
| `iops__mean_15m` | `iops` | `mean` | 15m |  |
| `iops__mean_1h` | `iops` | `mean` | 1h |  |
| `iops__mean_24h` | `iops` | `mean` | 24h |  |
| `iops__mean_6h` | `iops` | `mean` | 6h |  |
| `iops__min_15m` | `iops` | `min` | 15m |  |
| `iops__min_1h` | `iops` | `min` | 1h |  |
| `iops__min_24h` | `iops` | `min` | 24h |  |
| `iops__min_6h` | `iops` | `min` | 6h |  |
| `iops__slope_15m` | `iops` | `slope` | 15m |  |
| `iops__slope_1h` | `iops` | `slope` | 1h |  |
| `iops__slope_24h` | `iops` | `slope` | 24h |  |
| `iops__slope_6h` | `iops` | `slope` | 6h |  |
| `iops__std_15m` | `iops` | `std` | 15m |  |
| `iops__std_1h` | `iops` | `std` | 1h |  |
| `iops__std_24h` | `iops` | `std` | 24h |  |
| `iops__std_6h` | `iops` | `std` | 6h |  |
| `latency.read_p95_ms__level` | `latency.read_p95_ms` | `last` | 15m |  |
| `latency.read_p95_ms__max_15m` | `latency.read_p95_ms` | `max` | 15m |  |
| `latency.read_p95_ms__max_1h` | `latency.read_p95_ms` | `max` | 1h |  |
| `latency.read_p95_ms__max_24h` | `latency.read_p95_ms` | `max` | 24h |  |
| `latency.read_p95_ms__max_6h` | `latency.read_p95_ms` | `max` | 6h |  |
| `latency.read_p95_ms__mean_15m` | `latency.read_p95_ms` | `mean` | 15m |  |
| `latency.read_p95_ms__mean_1h` | `latency.read_p95_ms` | `mean` | 1h |  |
| `latency.read_p95_ms__mean_24h` | `latency.read_p95_ms` | `mean` | 24h |  |
| `latency.read_p95_ms__mean_6h` | `latency.read_p95_ms` | `mean` | 6h |  |
| `latency.read_p95_ms__min_15m` | `latency.read_p95_ms` | `min` | 15m |  |
| `latency.read_p95_ms__min_1h` | `latency.read_p95_ms` | `min` | 1h |  |
| `latency.read_p95_ms__min_24h` | `latency.read_p95_ms` | `min` | 24h |  |
| `latency.read_p95_ms__min_6h` | `latency.read_p95_ms` | `min` | 6h |  |
| `latency.read_p95_ms__slope_15m` | `latency.read_p95_ms` | `slope` | 15m |  |
| `latency.read_p95_ms__slope_1h` | `latency.read_p95_ms` | `slope` | 1h |  |
| `latency.read_p95_ms__slope_24h` | `latency.read_p95_ms` | `slope` | 24h |  |
| `latency.read_p95_ms__slope_6h` | `latency.read_p95_ms` | `slope` | 6h |  |
| `latency.read_p95_ms__std_15m` | `latency.read_p95_ms` | `std` | 15m |  |
| `latency.read_p95_ms__std_1h` | `latency.read_p95_ms` | `std` | 1h |  |
| `latency.read_p95_ms__std_24h` | `latency.read_p95_ms` | `std` | 24h |  |
| `latency.read_p95_ms__std_6h` | `latency.read_p95_ms` | `std` | 6h |  |
| `latency.write_p95_ms__level` | `latency.write_p95_ms` | `last` | 15m |  |
| `latency.write_p95_ms__max_15m` | `latency.write_p95_ms` | `max` | 15m |  |
| `latency.write_p95_ms__max_1h` | `latency.write_p95_ms` | `max` | 1h |  |
| `latency.write_p95_ms__max_24h` | `latency.write_p95_ms` | `max` | 24h |  |
| `latency.write_p95_ms__max_6h` | `latency.write_p95_ms` | `max` | 6h |  |
| `latency.write_p95_ms__mean_15m` | `latency.write_p95_ms` | `mean` | 15m |  |
| `latency.write_p95_ms__mean_1h` | `latency.write_p95_ms` | `mean` | 1h |  |
| `latency.write_p95_ms__mean_24h` | `latency.write_p95_ms` | `mean` | 24h |  |
| `latency.write_p95_ms__mean_6h` | `latency.write_p95_ms` | `mean` | 6h |  |
| `latency.write_p95_ms__min_15m` | `latency.write_p95_ms` | `min` | 15m |  |
| `latency.write_p95_ms__min_1h` | `latency.write_p95_ms` | `min` | 1h |  |
| `latency.write_p95_ms__min_24h` | `latency.write_p95_ms` | `min` | 24h |  |
| `latency.write_p95_ms__min_6h` | `latency.write_p95_ms` | `min` | 6h |  |
| `latency.write_p95_ms__slope_15m` | `latency.write_p95_ms` | `slope` | 15m |  |
| `latency.write_p95_ms__slope_1h` | `latency.write_p95_ms` | `slope` | 1h |  |
| `latency.write_p95_ms__slope_24h` | `latency.write_p95_ms` | `slope` | 24h |  |
| `latency.write_p95_ms__slope_6h` | `latency.write_p95_ms` | `slope` | 6h |  |
| `latency.write_p95_ms__std_15m` | `latency.write_p95_ms` | `std` | 15m |  |
| `latency.write_p95_ms__std_1h` | `latency.write_p95_ms` | `std` | 1h |  |
| `latency.write_p95_ms__std_24h` | `latency.write_p95_ms` | `std` | 24h |  |
| `latency.write_p95_ms__std_6h` | `latency.write_p95_ms` | `std` | 6h |  |
| `throughput_mbps__level` | `throughput_mbps` | `last` | 15m |  |
| `throughput_mbps__max_15m` | `throughput_mbps` | `max` | 15m |  |
| `throughput_mbps__max_1h` | `throughput_mbps` | `max` | 1h |  |
| `throughput_mbps__max_24h` | `throughput_mbps` | `max` | 24h |  |
| `throughput_mbps__max_6h` | `throughput_mbps` | `max` | 6h |  |
| `throughput_mbps__mean_15m` | `throughput_mbps` | `mean` | 15m |  |
| `throughput_mbps__mean_1h` | `throughput_mbps` | `mean` | 1h |  |
| `throughput_mbps__mean_24h` | `throughput_mbps` | `mean` | 24h |  |
| `throughput_mbps__mean_6h` | `throughput_mbps` | `mean` | 6h |  |
| `throughput_mbps__min_15m` | `throughput_mbps` | `min` | 15m |  |
| `throughput_mbps__min_1h` | `throughput_mbps` | `min` | 1h |  |
| `throughput_mbps__min_24h` | `throughput_mbps` | `min` | 24h |  |
| `throughput_mbps__min_6h` | `throughput_mbps` | `min` | 6h |  |
| `throughput_mbps__slope_15m` | `throughput_mbps` | `slope` | 15m |  |
| `throughput_mbps__slope_1h` | `throughput_mbps` | `slope` | 1h |  |
| `throughput_mbps__slope_24h` | `throughput_mbps` | `slope` | 24h |  |
| `throughput_mbps__slope_6h` | `throughput_mbps` | `slope` | 6h |  |
| `throughput_mbps__std_15m` | `throughput_mbps` | `std` | 15m |  |
| `throughput_mbps__std_1h` | `throughput_mbps` | `std` | 1h |  |
| `throughput_mbps__std_24h` | `throughput_mbps` | `std` | 24h |  |
| `throughput_mbps__std_6h` | `throughput_mbps` | `std` | 6h |  |

### snapmirror (42 features)

| Feature | Source metric | Function | Window | Parameters |
| --- | --- | --- | --- | --- |
| `lag_seconds__level` | `lag_seconds` | `last` | 15m |  |
| `lag_seconds__max_15m` | `lag_seconds` | `max` | 15m |  |
| `lag_seconds__max_1h` | `lag_seconds` | `max` | 1h |  |
| `lag_seconds__max_24h` | `lag_seconds` | `max` | 24h |  |
| `lag_seconds__max_6h` | `lag_seconds` | `max` | 6h |  |
| `lag_seconds__mean_15m` | `lag_seconds` | `mean` | 15m |  |
| `lag_seconds__mean_1h` | `lag_seconds` | `mean` | 1h |  |
| `lag_seconds__mean_24h` | `lag_seconds` | `mean` | 24h |  |
| `lag_seconds__mean_6h` | `lag_seconds` | `mean` | 6h |  |
| `lag_seconds__min_15m` | `lag_seconds` | `min` | 15m |  |
| `lag_seconds__min_1h` | `lag_seconds` | `min` | 1h |  |
| `lag_seconds__min_24h` | `lag_seconds` | `min` | 24h |  |
| `lag_seconds__min_6h` | `lag_seconds` | `min` | 6h |  |
| `lag_seconds__slope_15m` | `lag_seconds` | `slope` | 15m |  |
| `lag_seconds__slope_1h` | `lag_seconds` | `slope` | 1h |  |
| `lag_seconds__slope_24h` | `lag_seconds` | `slope` | 24h |  |
| `lag_seconds__slope_6h` | `lag_seconds` | `slope` | 6h |  |
| `lag_seconds__std_15m` | `lag_seconds` | `std` | 15m |  |
| `lag_seconds__std_1h` | `lag_seconds` | `std` | 1h |  |
| `lag_seconds__std_24h` | `lag_seconds` | `std` | 24h |  |
| `lag_seconds__std_6h` | `lag_seconds` | `std` | 6h |  |
| `transfer_errors__level` | `transfer_errors` | `last` | 15m |  |
| `transfer_errors__max_15m` | `transfer_errors` | `max` | 15m |  |
| `transfer_errors__max_1h` | `transfer_errors` | `max` | 1h |  |
| `transfer_errors__max_24h` | `transfer_errors` | `max` | 24h |  |
| `transfer_errors__max_6h` | `transfer_errors` | `max` | 6h |  |
| `transfer_errors__mean_15m` | `transfer_errors` | `mean` | 15m |  |
| `transfer_errors__mean_1h` | `transfer_errors` | `mean` | 1h |  |
| `transfer_errors__mean_24h` | `transfer_errors` | `mean` | 24h |  |
| `transfer_errors__mean_6h` | `transfer_errors` | `mean` | 6h |  |
| `transfer_errors__min_15m` | `transfer_errors` | `min` | 15m |  |
| `transfer_errors__min_1h` | `transfer_errors` | `min` | 1h |  |
| `transfer_errors__min_24h` | `transfer_errors` | `min` | 24h |  |
| `transfer_errors__min_6h` | `transfer_errors` | `min` | 6h |  |
| `transfer_errors__slope_15m` | `transfer_errors` | `slope` | 15m |  |
| `transfer_errors__slope_1h` | `transfer_errors` | `slope` | 1h |  |
| `transfer_errors__slope_24h` | `transfer_errors` | `slope` | 24h |  |
| `transfer_errors__slope_6h` | `transfer_errors` | `slope` | 6h |  |
| `transfer_errors__std_15m` | `transfer_errors` | `std` | 15m |  |
| `transfer_errors__std_1h` | `transfer_errors` | `std` | 1h |  |
| `transfer_errors__std_24h` | `transfer_errors` | `std` | 24h |  |
| `transfer_errors__std_6h` | `transfer_errors` | `std` | 6h |  |

### volume (238 features)

| Feature | Source metric | Function | Window | Parameters |
| --- | --- | --- | --- | --- |
| `data.change_rate_percent__level` | `data.change_rate_percent` | `last` | 15m |  |
| `data.change_rate_percent__max_15m` | `data.change_rate_percent` | `max` | 15m |  |
| `data.change_rate_percent__max_1h` | `data.change_rate_percent` | `max` | 1h |  |
| `data.change_rate_percent__max_24h` | `data.change_rate_percent` | `max` | 24h |  |
| `data.change_rate_percent__max_6h` | `data.change_rate_percent` | `max` | 6h |  |
| `data.change_rate_percent__mean_15m` | `data.change_rate_percent` | `mean` | 15m |  |
| `data.change_rate_percent__mean_1h` | `data.change_rate_percent` | `mean` | 1h |  |
| `data.change_rate_percent__mean_24h` | `data.change_rate_percent` | `mean` | 24h |  |
| `data.change_rate_percent__mean_6h` | `data.change_rate_percent` | `mean` | 6h |  |
| `data.change_rate_percent__min_15m` | `data.change_rate_percent` | `min` | 15m |  |
| `data.change_rate_percent__min_1h` | `data.change_rate_percent` | `min` | 1h |  |
| `data.change_rate_percent__min_24h` | `data.change_rate_percent` | `min` | 24h |  |
| `data.change_rate_percent__min_6h` | `data.change_rate_percent` | `min` | 6h |  |
| `data.change_rate_percent__slope_15m` | `data.change_rate_percent` | `slope` | 15m |  |
| `data.change_rate_percent__slope_1h` | `data.change_rate_percent` | `slope` | 1h |  |
| `data.change_rate_percent__slope_24h` | `data.change_rate_percent` | `slope` | 24h |  |
| `data.change_rate_percent__slope_6h` | `data.change_rate_percent` | `slope` | 6h |  |
| `data.change_rate_percent__std_15m` | `data.change_rate_percent` | `std` | 15m |  |
| `data.change_rate_percent__std_1h` | `data.change_rate_percent` | `std` | 1h |  |
| `data.change_rate_percent__std_24h` | `data.change_rate_percent` | `std` | 24h |  |
| `data.change_rate_percent__std_6h` | `data.change_rate_percent` | `std` | 6h |  |
| `data.entropy_proxy__level` | `data.entropy_proxy` | `last` | 15m |  |
| `data.entropy_proxy__max_15m` | `data.entropy_proxy` | `max` | 15m |  |
| `data.entropy_proxy__max_1h` | `data.entropy_proxy` | `max` | 1h |  |
| `data.entropy_proxy__max_24h` | `data.entropy_proxy` | `max` | 24h |  |
| `data.entropy_proxy__max_6h` | `data.entropy_proxy` | `max` | 6h |  |
| `data.entropy_proxy__mean_15m` | `data.entropy_proxy` | `mean` | 15m |  |
| `data.entropy_proxy__mean_1h` | `data.entropy_proxy` | `mean` | 1h |  |
| `data.entropy_proxy__mean_24h` | `data.entropy_proxy` | `mean` | 24h |  |
| `data.entropy_proxy__mean_6h` | `data.entropy_proxy` | `mean` | 6h |  |
| `data.entropy_proxy__min_15m` | `data.entropy_proxy` | `min` | 15m |  |
| `data.entropy_proxy__min_1h` | `data.entropy_proxy` | `min` | 1h |  |
| `data.entropy_proxy__min_24h` | `data.entropy_proxy` | `min` | 24h |  |
| `data.entropy_proxy__min_6h` | `data.entropy_proxy` | `min` | 6h |  |
| `data.entropy_proxy__slope_15m` | `data.entropy_proxy` | `slope` | 15m |  |
| `data.entropy_proxy__slope_1h` | `data.entropy_proxy` | `slope` | 1h |  |
| `data.entropy_proxy__slope_24h` | `data.entropy_proxy` | `slope` | 24h |  |
| `data.entropy_proxy__slope_6h` | `data.entropy_proxy` | `slope` | 6h |  |
| `data.entropy_proxy__std_15m` | `data.entropy_proxy` | `std` | 15m |  |
| `data.entropy_proxy__std_1h` | `data.entropy_proxy` | `std` | 1h |  |
| `data.entropy_proxy__std_24h` | `data.entropy_proxy` | `std` | 24h |  |
| `data.entropy_proxy__std_6h` | `data.entropy_proxy` | `std` | 6h |  |
| `efficiency.ratio__level` | `efficiency.ratio` | `last` | 15m |  |
| `efficiency.ratio__max_15m` | `efficiency.ratio` | `max` | 15m |  |
| `efficiency.ratio__max_1h` | `efficiency.ratio` | `max` | 1h |  |
| `efficiency.ratio__max_24h` | `efficiency.ratio` | `max` | 24h |  |
| `efficiency.ratio__max_6h` | `efficiency.ratio` | `max` | 6h |  |
| `efficiency.ratio__mean_15m` | `efficiency.ratio` | `mean` | 15m |  |
| `efficiency.ratio__mean_1h` | `efficiency.ratio` | `mean` | 1h |  |
| `efficiency.ratio__mean_24h` | `efficiency.ratio` | `mean` | 24h |  |
| `efficiency.ratio__mean_6h` | `efficiency.ratio` | `mean` | 6h |  |
| `efficiency.ratio__min_15m` | `efficiency.ratio` | `min` | 15m |  |
| `efficiency.ratio__min_1h` | `efficiency.ratio` | `min` | 1h |  |
| `efficiency.ratio__min_24h` | `efficiency.ratio` | `min` | 24h |  |
| `efficiency.ratio__min_6h` | `efficiency.ratio` | `min` | 6h |  |
| `efficiency.ratio__slope_15m` | `efficiency.ratio` | `slope` | 15m |  |
| `efficiency.ratio__slope_1h` | `efficiency.ratio` | `slope` | 1h |  |
| `efficiency.ratio__slope_24h` | `efficiency.ratio` | `slope` | 24h |  |
| `efficiency.ratio__slope_6h` | `efficiency.ratio` | `slope` | 6h |  |
| `efficiency.ratio__std_15m` | `efficiency.ratio` | `std` | 15m |  |
| `efficiency.ratio__std_1h` | `efficiency.ratio` | `std` | 1h |  |
| `efficiency.ratio__std_24h` | `efficiency.ratio` | `std` | 24h |  |
| `efficiency.ratio__std_6h` | `efficiency.ratio` | `std` | 6h |  |
| `iops__level` | `iops` | `last` | 15m |  |
| `iops__max_15m` | `iops` | `max` | 15m |  |
| `iops__max_1h` | `iops` | `max` | 1h |  |
| `iops__max_24h` | `iops` | `max` | 24h |  |
| `iops__max_6h` | `iops` | `max` | 6h |  |
| `iops__mean_15m` | `iops` | `mean` | 15m |  |
| `iops__mean_1h` | `iops` | `mean` | 1h |  |
| `iops__mean_24h` | `iops` | `mean` | 24h |  |
| `iops__mean_6h` | `iops` | `mean` | 6h |  |
| `iops__min_15m` | `iops` | `min` | 15m |  |
| `iops__min_1h` | `iops` | `min` | 1h |  |
| `iops__min_24h` | `iops` | `min` | 24h |  |
| `iops__min_6h` | `iops` | `min` | 6h |  |
| `iops__slope_15m` | `iops` | `slope` | 15m |  |
| `iops__slope_1h` | `iops` | `slope` | 1h |  |
| `iops__slope_24h` | `iops` | `slope` | 24h |  |
| `iops__slope_6h` | `iops` | `slope` | 6h |  |
| `iops__std_15m` | `iops` | `std` | 15m |  |
| `iops__std_1h` | `iops` | `std` | 1h |  |
| `iops__std_24h` | `iops` | `std` | 24h |  |
| `iops__std_6h` | `iops` | `std` | 6h |  |
| `snapshot.reserve_used_percent__level` | `snapshot.reserve_used_percent` | `last` | 15m |  |
| `snapshot.reserve_used_percent__max_15m` | `snapshot.reserve_used_percent` | `max` | 15m |  |
| `snapshot.reserve_used_percent__max_1h` | `snapshot.reserve_used_percent` | `max` | 1h |  |
| `snapshot.reserve_used_percent__max_24h` | `snapshot.reserve_used_percent` | `max` | 24h |  |
| `snapshot.reserve_used_percent__max_6h` | `snapshot.reserve_used_percent` | `max` | 6h |  |
| `snapshot.reserve_used_percent__mean_15m` | `snapshot.reserve_used_percent` | `mean` | 15m |  |
| `snapshot.reserve_used_percent__mean_1h` | `snapshot.reserve_used_percent` | `mean` | 1h |  |
| `snapshot.reserve_used_percent__mean_24h` | `snapshot.reserve_used_percent` | `mean` | 24h |  |
| `snapshot.reserve_used_percent__mean_6h` | `snapshot.reserve_used_percent` | `mean` | 6h |  |
| `snapshot.reserve_used_percent__min_15m` | `snapshot.reserve_used_percent` | `min` | 15m |  |
| `snapshot.reserve_used_percent__min_1h` | `snapshot.reserve_used_percent` | `min` | 1h |  |
| `snapshot.reserve_used_percent__min_24h` | `snapshot.reserve_used_percent` | `min` | 24h |  |
| `snapshot.reserve_used_percent__min_6h` | `snapshot.reserve_used_percent` | `min` | 6h |  |
| `snapshot.reserve_used_percent__slope_15m` | `snapshot.reserve_used_percent` | `slope` | 15m |  |
| `snapshot.reserve_used_percent__slope_1h` | `snapshot.reserve_used_percent` | `slope` | 1h |  |
| `snapshot.reserve_used_percent__slope_24h` | `snapshot.reserve_used_percent` | `slope` | 24h |  |
| `snapshot.reserve_used_percent__slope_6h` | `snapshot.reserve_used_percent` | `slope` | 6h |  |
| `snapshot.reserve_used_percent__std_15m` | `snapshot.reserve_used_percent` | `std` | 15m |  |
| `snapshot.reserve_used_percent__std_1h` | `snapshot.reserve_used_percent` | `std` | 1h |  |
| `snapshot.reserve_used_percent__std_24h` | `snapshot.reserve_used_percent` | `std` | 24h |  |
| `snapshot.reserve_used_percent__std_6h` | `snapshot.reserve_used_percent` | `std` | 6h |  |
| `snapshot.reserve_used_percent__ttf_1h` | `snapshot.reserve_used_percent` | `time_to_full` | 1h | cap_minutes=43200, full=100 |
| `snapshot.reserve_used_percent__ttf_24h` | `snapshot.reserve_used_percent` | `time_to_full` | 24h | cap_minutes=43200, full=100 |
| `snapshot.reserve_used_percent__ttf_6h` | `snapshot.reserve_used_percent` | `time_to_full` | 6h | cap_minutes=43200, full=100 |
| `snapshot.to_active_ratio__level` | `snapshot.to_active_ratio` | `last` | 15m |  |
| `snapshot.to_active_ratio__max_15m` | `snapshot.to_active_ratio` | `max` | 15m |  |
| `snapshot.to_active_ratio__max_1h` | `snapshot.to_active_ratio` | `max` | 1h |  |
| `snapshot.to_active_ratio__max_24h` | `snapshot.to_active_ratio` | `max` | 24h |  |
| `snapshot.to_active_ratio__max_6h` | `snapshot.to_active_ratio` | `max` | 6h |  |
| `snapshot.to_active_ratio__mean_15m` | `snapshot.to_active_ratio` | `mean` | 15m |  |
| `snapshot.to_active_ratio__mean_1h` | `snapshot.to_active_ratio` | `mean` | 1h |  |
| `snapshot.to_active_ratio__mean_24h` | `snapshot.to_active_ratio` | `mean` | 24h |  |
| `snapshot.to_active_ratio__mean_6h` | `snapshot.to_active_ratio` | `mean` | 6h |  |
| `snapshot.to_active_ratio__min_15m` | `snapshot.to_active_ratio` | `min` | 15m |  |
| `snapshot.to_active_ratio__min_1h` | `snapshot.to_active_ratio` | `min` | 1h |  |
| `snapshot.to_active_ratio__min_24h` | `snapshot.to_active_ratio` | `min` | 24h |  |
| `snapshot.to_active_ratio__min_6h` | `snapshot.to_active_ratio` | `min` | 6h |  |
| `snapshot.to_active_ratio__slope_15m` | `snapshot.to_active_ratio` | `slope` | 15m |  |
| `snapshot.to_active_ratio__slope_1h` | `snapshot.to_active_ratio` | `slope` | 1h |  |
| `snapshot.to_active_ratio__slope_24h` | `snapshot.to_active_ratio` | `slope` | 24h |  |
| `snapshot.to_active_ratio__slope_6h` | `snapshot.to_active_ratio` | `slope` | 6h |  |
| `snapshot.to_active_ratio__std_15m` | `snapshot.to_active_ratio` | `std` | 15m |  |
| `snapshot.to_active_ratio__std_1h` | `snapshot.to_active_ratio` | `std` | 1h |  |
| `snapshot.to_active_ratio__std_24h` | `snapshot.to_active_ratio` | `std` | 24h |  |
| `snapshot.to_active_ratio__std_6h` | `snapshot.to_active_ratio` | `std` | 6h |  |
| `snapshot.used_bytes__level` | `snapshot.used_bytes` | `last` | 15m |  |
| `snapshot.used_bytes__max_15m` | `snapshot.used_bytes` | `max` | 15m |  |
| `snapshot.used_bytes__max_1h` | `snapshot.used_bytes` | `max` | 1h |  |
| `snapshot.used_bytes__max_24h` | `snapshot.used_bytes` | `max` | 24h |  |
| `snapshot.used_bytes__max_6h` | `snapshot.used_bytes` | `max` | 6h |  |
| `snapshot.used_bytes__mean_15m` | `snapshot.used_bytes` | `mean` | 15m |  |
| `snapshot.used_bytes__mean_1h` | `snapshot.used_bytes` | `mean` | 1h |  |
| `snapshot.used_bytes__mean_24h` | `snapshot.used_bytes` | `mean` | 24h |  |
| `snapshot.used_bytes__mean_6h` | `snapshot.used_bytes` | `mean` | 6h |  |
| `snapshot.used_bytes__min_15m` | `snapshot.used_bytes` | `min` | 15m |  |
| `snapshot.used_bytes__min_1h` | `snapshot.used_bytes` | `min` | 1h |  |
| `snapshot.used_bytes__min_24h` | `snapshot.used_bytes` | `min` | 24h |  |
| `snapshot.used_bytes__min_6h` | `snapshot.used_bytes` | `min` | 6h |  |
| `snapshot.used_bytes__slope_15m` | `snapshot.used_bytes` | `slope` | 15m |  |
| `snapshot.used_bytes__slope_1h` | `snapshot.used_bytes` | `slope` | 1h |  |
| `snapshot.used_bytes__slope_24h` | `snapshot.used_bytes` | `slope` | 24h |  |
| `snapshot.used_bytes__slope_6h` | `snapshot.used_bytes` | `slope` | 6h |  |
| `snapshot.used_bytes__std_15m` | `snapshot.used_bytes` | `std` | 15m |  |
| `snapshot.used_bytes__std_1h` | `snapshot.used_bytes` | `std` | 1h |  |
| `snapshot.used_bytes__std_24h` | `snapshot.used_bytes` | `std` | 24h |  |
| `snapshot.used_bytes__std_6h` | `snapshot.used_bytes` | `std` | 6h |  |
| `space.autogrow_max_bytes__level` | `space.autogrow_max_bytes` | `last` | 15m |  |
| `space.logical_used_bytes__level` | `space.logical_used_bytes` | `last` | 15m |  |
| `space.logical_used_bytes__max_15m` | `space.logical_used_bytes` | `max` | 15m |  |
| `space.logical_used_bytes__max_1h` | `space.logical_used_bytes` | `max` | 1h |  |
| `space.logical_used_bytes__max_24h` | `space.logical_used_bytes` | `max` | 24h |  |
| `space.logical_used_bytes__max_6h` | `space.logical_used_bytes` | `max` | 6h |  |
| `space.logical_used_bytes__mean_15m` | `space.logical_used_bytes` | `mean` | 15m |  |
| `space.logical_used_bytes__mean_1h` | `space.logical_used_bytes` | `mean` | 1h |  |
| `space.logical_used_bytes__mean_24h` | `space.logical_used_bytes` | `mean` | 24h |  |
| `space.logical_used_bytes__mean_6h` | `space.logical_used_bytes` | `mean` | 6h |  |
| `space.logical_used_bytes__min_15m` | `space.logical_used_bytes` | `min` | 15m |  |
| `space.logical_used_bytes__min_1h` | `space.logical_used_bytes` | `min` | 1h |  |
| `space.logical_used_bytes__min_24h` | `space.logical_used_bytes` | `min` | 24h |  |
| `space.logical_used_bytes__min_6h` | `space.logical_used_bytes` | `min` | 6h |  |
| `space.logical_used_bytes__slope_15m` | `space.logical_used_bytes` | `slope` | 15m |  |
| `space.logical_used_bytes__slope_1h` | `space.logical_used_bytes` | `slope` | 1h |  |
| `space.logical_used_bytes__slope_24h` | `space.logical_used_bytes` | `slope` | 24h |  |
| `space.logical_used_bytes__slope_6h` | `space.logical_used_bytes` | `slope` | 6h |  |
| `space.logical_used_bytes__std_15m` | `space.logical_used_bytes` | `std` | 15m |  |
| `space.logical_used_bytes__std_1h` | `space.logical_used_bytes` | `std` | 1h |  |
| `space.logical_used_bytes__std_24h` | `space.logical_used_bytes` | `std` | 24h |  |
| `space.logical_used_bytes__std_6h` | `space.logical_used_bytes` | `std` | 6h |  |
| `space.size_bytes__level` | `space.size_bytes` | `last` | 15m |  |
| `space.size_bytes__max_15m` | `space.size_bytes` | `max` | 15m |  |
| `space.size_bytes__max_1h` | `space.size_bytes` | `max` | 1h |  |
| `space.size_bytes__max_24h` | `space.size_bytes` | `max` | 24h |  |
| `space.size_bytes__max_6h` | `space.size_bytes` | `max` | 6h |  |
| `space.size_bytes__mean_15m` | `space.size_bytes` | `mean` | 15m |  |
| `space.size_bytes__mean_1h` | `space.size_bytes` | `mean` | 1h |  |
| `space.size_bytes__mean_24h` | `space.size_bytes` | `mean` | 24h |  |
| `space.size_bytes__mean_6h` | `space.size_bytes` | `mean` | 6h |  |
| `space.size_bytes__min_15m` | `space.size_bytes` | `min` | 15m |  |
| `space.size_bytes__min_1h` | `space.size_bytes` | `min` | 1h |  |
| `space.size_bytes__min_24h` | `space.size_bytes` | `min` | 24h |  |
| `space.size_bytes__min_6h` | `space.size_bytes` | `min` | 6h |  |
| `space.size_bytes__slope_15m` | `space.size_bytes` | `slope` | 15m |  |
| `space.size_bytes__slope_1h` | `space.size_bytes` | `slope` | 1h |  |
| `space.size_bytes__slope_24h` | `space.size_bytes` | `slope` | 24h |  |
| `space.size_bytes__slope_6h` | `space.size_bytes` | `slope` | 6h |  |
| `space.size_bytes__std_15m` | `space.size_bytes` | `std` | 15m |  |
| `space.size_bytes__std_1h` | `space.size_bytes` | `std` | 1h |  |
| `space.size_bytes__std_24h` | `space.size_bytes` | `std` | 24h |  |
| `space.size_bytes__std_6h` | `space.size_bytes` | `std` | 6h |  |
| `space.used_bytes__level` | `space.used_bytes` | `last` | 15m |  |
| `space.used_bytes__max_15m` | `space.used_bytes` | `max` | 15m |  |
| `space.used_bytes__max_1h` | `space.used_bytes` | `max` | 1h |  |
| `space.used_bytes__max_24h` | `space.used_bytes` | `max` | 24h |  |
| `space.used_bytes__max_6h` | `space.used_bytes` | `max` | 6h |  |
| `space.used_bytes__mean_15m` | `space.used_bytes` | `mean` | 15m |  |
| `space.used_bytes__mean_1h` | `space.used_bytes` | `mean` | 1h |  |
| `space.used_bytes__mean_24h` | `space.used_bytes` | `mean` | 24h |  |
| `space.used_bytes__mean_6h` | `space.used_bytes` | `mean` | 6h |  |
| `space.used_bytes__min_15m` | `space.used_bytes` | `min` | 15m |  |
| `space.used_bytes__min_1h` | `space.used_bytes` | `min` | 1h |  |
| `space.used_bytes__min_24h` | `space.used_bytes` | `min` | 24h |  |
| `space.used_bytes__min_6h` | `space.used_bytes` | `min` | 6h |  |
| `space.used_bytes__slope_15m` | `space.used_bytes` | `slope` | 15m |  |
| `space.used_bytes__slope_1h` | `space.used_bytes` | `slope` | 1h |  |
| `space.used_bytes__slope_24h` | `space.used_bytes` | `slope` | 24h |  |
| `space.used_bytes__slope_6h` | `space.used_bytes` | `slope` | 6h |  |
| `space.used_bytes__std_15m` | `space.used_bytes` | `std` | 15m |  |
| `space.used_bytes__std_1h` | `space.used_bytes` | `std` | 1h |  |
| `space.used_bytes__std_24h` | `space.used_bytes` | `std` | 24h |  |
| `space.used_bytes__std_6h` | `space.used_bytes` | `std` | 6h |  |
| `space.used_percent__level` | `space.used_percent` | `last` | 15m |  |
| `space.used_percent__max_15m` | `space.used_percent` | `max` | 15m |  |
| `space.used_percent__max_1h` | `space.used_percent` | `max` | 1h |  |
| `space.used_percent__max_24h` | `space.used_percent` | `max` | 24h |  |
| `space.used_percent__max_6h` | `space.used_percent` | `max` | 6h |  |
| `space.used_percent__mean_15m` | `space.used_percent` | `mean` | 15m |  |
| `space.used_percent__mean_1h` | `space.used_percent` | `mean` | 1h |  |
| `space.used_percent__mean_24h` | `space.used_percent` | `mean` | 24h |  |
| `space.used_percent__mean_6h` | `space.used_percent` | `mean` | 6h |  |
| `space.used_percent__min_15m` | `space.used_percent` | `min` | 15m |  |
| `space.used_percent__min_1h` | `space.used_percent` | `min` | 1h |  |
| `space.used_percent__min_24h` | `space.used_percent` | `min` | 24h |  |
| `space.used_percent__min_6h` | `space.used_percent` | `min` | 6h |  |
| `space.used_percent__slope_15m` | `space.used_percent` | `slope` | 15m |  |
| `space.used_percent__slope_1h` | `space.used_percent` | `slope` | 1h |  |
| `space.used_percent__slope_24h` | `space.used_percent` | `slope` | 24h |  |
| `space.used_percent__slope_6h` | `space.used_percent` | `slope` | 6h |  |
| `space.used_percent__std_15m` | `space.used_percent` | `std` | 15m |  |
| `space.used_percent__std_1h` | `space.used_percent` | `std` | 1h |  |
| `space.used_percent__std_24h` | `space.used_percent` | `std` | 24h |  |
| `space.used_percent__std_6h` | `space.used_percent` | `std` | 6h |  |
| `space.used_percent__ttf_1h` | `space.used_percent` | `time_to_full` | 1h | cap_minutes=43200, full=100 |
| `space.used_percent__ttf_24h` | `space.used_percent` | `time_to_full` | 24h | cap_minutes=43200, full=100 |
| `space.used_percent__ttf_6h` | `space.used_percent` | `time_to_full` | 6h | cap_minutes=43200, full=100 |
