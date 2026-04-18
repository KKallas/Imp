"""tools/heuristics/calibrate.py — S/M/L duration calibration from closed issues.

Derives small/medium/large duration buckets from the project's own
closed-issue history.  For each closed issue, computes
``delta_days = closedAt - createdAt`` and buckets by a complexity signal
(body length + acceptance-criteria checkbox count).  The median delta
per bucket becomes the calibrated duration.

Results are cached to ``.imp/calibration.json`` and recomputed when
stale (>24h) or when the closed-issue count changes.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
CALIBRATION_FILE = ROOT / ".imp" / "calibration.json"
ENRICHED_FILE = ROOT / ".imp" / "enriched.json"

# Defaults when no closed-issue data is available.
# Baselines from the Imp project itself (Apr 11-17, 2026):
#   small ~0.3 days (sub-day tasks), medium ~1 day, large ~3 days.
# Gantt charts need integer days, so small clamps to 1 day minimum.
DEFAULT_DURATIONS = {"small": 1, "medium": 1, "large": 3}

# Cache staleness threshold (seconds).
_STALE_SECS = 86400  # 24 hours

# Complexity thresholds.
_LARGE_BODY_CHARS = 1500
_MEDIUM_BODY_CHARS = 500
_LARGE_AC_COUNT = 5
_MEDIUM_AC_COUNT = 2

_CHECKBOX_RE = re.compile(r"^- \[[ xX]\]", re.MULTILINE)


# ── complexity bucketing ────────────────────────────────────────────

def complexity_bucket(issue: dict[str, Any]) -> str:
    """Classify an issue as ``small``, ``medium``, or ``large``.

    Uses body length and acceptance-criteria checkbox count as the
    complexity signal.
    """
    body = str(issue.get("body") or "")
    body_len = len(body)
    ac_count = len(_CHECKBOX_RE.findall(body))

    if ac_count > _LARGE_AC_COUNT or body_len > _LARGE_BODY_CHARS:
        return "large"
    if ac_count >= _MEDIUM_AC_COUNT or body_len > _MEDIUM_BODY_CHARS:
        return "medium"
    return "small"


# ── calibration ─────────────────────────────────────────────────────

def _parse_date(raw: Any) -> date | None:
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def calibrate(
    closed_issues: list[dict[str, Any]],
    defaults: dict[str, int] | None = None,
) -> dict[str, int]:
    """Compute median ``delta_days`` per complexity bucket.

    Returns ``{"small": N, "medium": N, "large": N}`` — always all
    three keys, falling back to *defaults* for empty buckets.
    """
    defaults = defaults or dict(DEFAULT_DURATIONS)
    buckets: dict[str, list[int]] = {"small": [], "medium": [], "large": []}

    for issue in closed_issues:
        created = _parse_date(issue.get("createdAt"))
        closed = _parse_date(issue.get("closedAt"))
        if created is None or closed is None:
            continue
        delta = (closed - created).days
        if delta < 0:
            continue
        bucket = complexity_bucket(issue)
        buckets[bucket].append(max(1, delta))

    result: dict[str, int] = {}
    for bucket_name in ("small", "medium", "large"):
        values = buckets[bucket_name]
        if values:
            result[bucket_name] = int(statistics.median(values))
        else:
            result[bucket_name] = defaults.get(bucket_name, 1)

    return result


# ── cache ───────────────────────────────────────────────────────────

def _load_cache() -> dict[str, Any] | None:
    if not CALIBRATION_FILE.exists():
        return None
    try:
        return json.loads(CALIBRATION_FILE.read_text())
    except (json.JSONDecodeError, KeyError):
        return None


def _save_cache(
    durations: dict[str, int],
    sample_sizes: dict[str, int],
    closed_count: int,
) -> None:
    CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "durations": durations,
        "sample_sizes": sample_sizes,
        "closed_count": closed_count,
        "calibrated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    CALIBRATION_FILE.write_text(json.dumps(data, indent=2))


def _is_stale(cache: dict[str, Any], current_closed_count: int) -> bool:
    if cache.get("closed_count") != current_closed_count:
        return True
    cal_at = cache.get("calibrated_at")
    if not cal_at:
        return True
    try:
        ts = datetime.fromisoformat(cal_at)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age > _STALE_SECS
    except ValueError:
        return True


# ── public API ──────────────────────────────────────────────────────

def calibrate_from_enriched(
    enriched_path: Path | None = None,
) -> dict[str, Any]:
    """Load enriched issues, calibrate, cache, and return the result.

    Returns ``{"durations": {...}, "sample_sizes": {...},
    "closed_count": N, "calibrated_at": "..."}``
    """
    path = enriched_path or ENRICHED_FILE
    if not path.exists():
        # No enriched data — return defaults
        return {
            "durations": dict(DEFAULT_DURATIONS),
            "sample_sizes": {"small": 0, "medium": 0, "large": 0},
            "closed_count": 0,
            "calibrated_at": None,
        }

    enriched = json.loads(path.read_text())
    issues = enriched.get("issues") or []
    closed = [
        i for i in issues
        if str(i.get("state") or "").upper() == "CLOSED"
    ]

    # Check cache
    cache = _load_cache()
    if cache is not None and not _is_stale(cache, len(closed)):
        return cache

    # Calibrate
    durations = calibrate(closed)

    # Count samples per bucket
    sample_sizes: dict[str, int] = {"small": 0, "medium": 0, "large": 0}
    for issue in closed:
        created = _parse_date(issue.get("createdAt"))
        closed_at = _parse_date(issue.get("closedAt"))
        if created is not None and closed_at is not None:
            bucket = complexity_bucket(issue)
            sample_sizes[bucket] += 1

    _save_cache(durations, sample_sizes, len(closed))

    result = _load_cache()
    assert result is not None
    return result


def get_duration_estimates() -> dict[str, Any]:
    """Foreman-facing API: returns current calibration + metadata."""
    cache = _load_cache()
    if cache is not None:
        return cache
    return calibrate_from_enriched()
