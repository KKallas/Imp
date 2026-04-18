# heuristics

Calibrates project duration estimates from closed-issue history.

## Inputs
- `.imp/enriched.json` (from sync + heuristics pipeline)
- Closed issues with `createdAt` and `closedAt` timestamps

## Outputs
- `.imp/calibration.json` — S/M/L duration buckets (days)
- Duration estimates: `{"small": N, "medium": N, "large": N}`

## Algorithm
1. For each closed issue, compute `delta_days = closedAt - createdAt`
2. Bucket by complexity: body length + acceptance-criteria checkbox count
3. Per bucket, take the **median** delta_days (robust to outliers)
4. Cache result; recompute when stale (>24h) or closed-issue count changed

## Usage
```bash
python -m tools.heuristics.script
```

## Foreman integration
MCP tool: `get_duration_estimates()` — returns calibration + sample sizes
