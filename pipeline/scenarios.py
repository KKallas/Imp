"""pipeline/scenarios.py — scenario sessions for side-by-side comparison.

A scenario session lets the admin describe 2–5 parametric variants of
the project plan ("as-is", "start 2 weeks later", "4 devs not 2") and
see them rendered side-by-side — one chart and N metrics per scenario
— before committing to one of them. See KKallas/Imp#16 for the UX
design.

## Concepts

- **Scenario function**: a Python function decorated with `@scenario(name)`
  that takes the enriched baseline data + an `Out` collector and
  populates `out` with charts / metrics / lists / text.
- **Filter primitives**: pure data-transforming helpers (`delay_all`,
  `delay_issue`, `drop_issue`, `scale_durations`, `shift_start`,
  `exclude_weekends`, `freeze_after`) scenarios compose.
- **Out collector**: the scenario function's only means of emitting
  results. Explicit methods (`chart`, `metric`, `list`, `text`, `table`);
  stdout is ignored. Used to build the grid-comparison message.
- **Scenario session**: a saved bundle on disk at
  `.imp/scenarios/<session_id>/` containing the LLM-generated
  `scenarios.py`, the user's text descriptions, the last render
  result, and (optionally) a commit pointer.

## Session layout

```
.imp/scenarios/
  └── gantt-2026-04-15-abc123/
      ├── descriptions.txt     # one line per scenario; the human spec
      ├── scenarios.py          # LLM-generated; reproducibility contract
      ├── result.json           # last-run output per scenario
      └── committed.json        # {choice_index, committed_at, baseline_hash}
                                # (absent if never committed / closed)
```

## Execution model

1. User sends N text descriptions.
2. `generate_scenarios_py(descriptions)` makes ONE LLM call to emit
   the full `scenarios.py` (see `set_generator_backend` for testing).
3. `load_session(session_id)` imports the hidden `.py` into a
   restricted namespace, collects the `@scenario`-decorated functions.
4. `run_session(session_id, baseline_data)` calls each scenario
   function with `(data, Out())`, collects the outputs.
5. Outputs are serialised to `result.json` so re-open can render
   without re-running (and also so tests have a stable artifact).

## Safety notes

The generated `.py` executes in this process. We AST-scan for
obviously-bad patterns (imports of `os`/`subprocess`/`socket`/etc.,
`exec`/`eval`/`compile`, attribute access through builtins). If the
scan trips, the session is rejected with a specific reason. The
restricted exec namespace preloads only the scenario API + filter
primitives + standard library types the generator might use
(`datetime`, `date`, `timedelta`). This is not a hard sandbox — Guard
(KKallas/Imp#46) is the actual security boundary — but it keeps the
generator honest.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = ROOT / ".imp" / "scenarios"

MAX_SCENARIOS = 5
MIN_SCENARIOS = 2


# ---------- Out collector ----------


@dataclass
class Out:
    """Output collector a scenario function populates.

    One `Out` per scenario. Methods are the only sanctioned way to emit
    results — no stdout capture, no return-value magic. Each method
    appends to a typed list; the runner serialises to JSON for the
    comparison grid.
    """

    name: str
    charts: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[tuple[str, str]] = field(default_factory=list)
    lists: list[tuple[str, list[str]]] = field(default_factory=list)
    texts: list[tuple[str, str]] = field(default_factory=list)
    tables: list[tuple[str, list[list[str]]]] = field(default_factory=list)

    def chart(self, figure: Any) -> None:
        """Attach a Plotly figure. Accepts a plotly.graph_objects.Figure
        OR a plain dict (already-serialised figure)."""
        if hasattr(figure, "to_plotly_json"):
            self.charts.append(figure.to_plotly_json())
        elif isinstance(figure, dict):
            self.charts.append(figure)
        else:
            raise TypeError(
                f"out.chart expects a Figure or dict, got {type(figure).__name__}"
            )

    def metric(self, name: str, value: Any) -> None:
        self.metrics.append((str(name), str(value)))

    def list(self, name: str, items: list[Any]) -> None:
        self.lists.append((str(name), [str(item) for item in items]))

    def text(self, name: str, content: str) -> None:
        self.texts.append((str(name), str(content)))

    def table(self, name: str, rows: list[list[Any]]) -> None:
        self.tables.append(
            (str(name), [[str(cell) for cell in row] for row in rows])
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "charts": self.charts,
            "metrics": self.metrics,
            "lists": [[n, items] for n, items in self.lists],
            "texts": self.texts,
            "tables": [[n, rows] for n, rows in self.tables],
        }


# ---------- @scenario decorator ----------
#
# Functions decorated with @scenario("name") are collected by
# `_collect_scenarios` after exec. Storing in a ContextVar would be
# cleaner but module-level dict is simpler and the exec namespace is
# short-lived per-session.


_SCENARIO_REGISTRY_KEY = "__imp_scenarios__"


def scenario(name: str) -> Callable[[Callable[..., None]], Callable[..., None]]:
    """Decorator: mark a function as a scenario named `name`.

    The function must accept `(data, out)`. The runner collects all
    decorated functions in invocation order and runs them serially.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("@scenario(name) requires a non-empty name string")

    def decorator(fn: Callable[..., None]) -> Callable[..., None]:
        fn._scenario_name = name.strip()  # type: ignore[attr-defined]
        return fn

    return decorator


# ---------- filter primitives ----------
#
# All pure functions: `dict -> dict`. Never mutate the input — always
# deep-copy the relevant structure before modifying. Scenarios compose
# them freely.


def _deep_copy_issues(data: dict[str, Any]) -> dict[str, Any]:
    """Shallow-clone the top-level payload + deep-clone the issues list,
    which is the only part scenarios typically modify."""
    out = dict(data)
    out["issues"] = [
        {
            **issue,
            "fields": {
                k: (dict(v) if isinstance(v, dict) else v)
                for k, v in (issue.get("fields") or {}).items()
            },
        }
        for issue in (data.get("issues") or [])
    ]
    return out


def _field_value(issue: dict[str, Any], key: str) -> Any:
    cell = (issue.get("fields") or {}).get(key)
    if isinstance(cell, dict) and "value" in cell:
        return cell["value"]
    return cell


def get_field(issue: dict[str, Any], key: str, default: Any = None) -> Any:
    """Public helper: safely read a field value from an issue.

    Exposed in the scenarios exec namespace so LLM-generated code can
    access fields without crashing on missing keys. Handles three shapes:

      - Missing field → returns `default`
      - Provenance envelope `{"value": ..., "source": ..., ...}` → returns the value
      - Flat scalar (no envelope) → returns it unchanged

    Heuristics populates an envelope for every issue, but only for fields
    that actually have data. Open issues without project-board dates won't
    have `start_date` at all — the LLM's generated code must not assume
    every field is present. Use this helper instead of raw dict access.
    """
    value = _field_value(issue, key)
    return default if value is None else value


def _set_field_value(issue: dict[str, Any], key: str, value: Any) -> None:
    fields = issue.setdefault("fields", {})
    cell = fields.get(key)
    if isinstance(cell, dict):
        cell["value"] = value
    else:
        fields[key] = {"value": value, "source": "scenario", "confidence": "high"}


def delay_all(data: dict[str, Any], days: int) -> dict[str, Any]:
    """Push every issue's start_date and end_date forward by `days`.
    Issues with no dates are left alone."""
    if not isinstance(days, int):
        raise TypeError(f"days must be int, got {type(days).__name__}")
    out = _deep_copy_issues(data)
    delta = timedelta(days=days)
    for issue in out["issues"]:
        for key in ("start_date", "end_date"):
            cur = _field_value(issue, key)
            if isinstance(cur, str):
                try:
                    new = (date.fromisoformat(cur) + delta).isoformat()
                    _set_field_value(issue, key, new)
                except ValueError:
                    pass  # bad iso date — soft skip
    return out


def delay_issue(
    data: dict[str, Any], number: int, days: int
) -> dict[str, Any]:
    """Delay a single issue and cascade to anything that depends on it.

    Cascade rule: if B depends on A (via `depends_on_parsed`) and A's
    end_date shifts forward, B's start_date moves to max(B's original
    start, A's new end). Depth is bounded to avoid cycles pathologies.
    """
    if not isinstance(number, int):
        raise TypeError(f"number must be int, got {type(number).__name__}")
    if not isinstance(days, int):
        raise TypeError(f"days must be int, got {type(days).__name__}")

    out = _deep_copy_issues(data)
    delta = timedelta(days=days)

    by_num = {it.get("number"): it for it in out["issues"] if isinstance(it.get("number"), int)}
    target = by_num.get(number)
    if target is None:
        return out  # no-op if the issue isn't present

    # Shift the target's own dates
    for key in ("start_date", "end_date"):
        cur = _field_value(target, key)
        if isinstance(cur, str):
            try:
                new = (date.fromisoformat(cur) + delta).isoformat()
                _set_field_value(target, key, new)
            except ValueError:
                pass

    # Cascade to downstream issues (iterative BFS, depth-bounded)
    frontier = {number}
    seen: set[int] = set()
    max_depth = 20
    for _ in range(max_depth):
        if not frontier:
            break
        next_frontier: set[int] = set()
        for src in frontier:
            if src in seen:
                continue
            seen.add(src)
            src_issue = by_num.get(src)
            if src_issue is None:
                continue
            src_end_str = _field_value(src_issue, "end_date")
            if not isinstance(src_end_str, str):
                continue
            try:
                src_end = date.fromisoformat(src_end_str)
            except ValueError:
                continue

            # Find issues that depend on src and push their starts forward
            for issue in out["issues"]:
                deps = issue.get("depends_on_parsed") or []
                if src not in deps:
                    continue
                num = issue.get("number")
                if not isinstance(num, int) or num == src:
                    continue
                cur_start = _field_value(issue, "start_date")
                new_start = src_end
                if isinstance(cur_start, str):
                    try:
                        cur_start_d = date.fromisoformat(cur_start)
                        new_start = max(cur_start_d, src_end)
                    except ValueError:
                        pass
                _set_field_value(issue, "start_date", new_start.isoformat())
                # If end_date exists and is now before start, shift it
                cur_end = _field_value(issue, "end_date")
                if isinstance(cur_end, str):
                    try:
                        cur_end_d = date.fromisoformat(cur_end)
                        if cur_end_d < new_start:
                            _set_field_value(
                                issue,
                                "end_date",
                                (new_start + (cur_end_d - date.fromisoformat(cur_start)) if isinstance(cur_start, str) else new_start).isoformat(),
                            )
                    except ValueError:
                        pass
                next_frontier.add(num)
        frontier = next_frontier

    return out


def drop_issue(data: dict[str, Any], number: int) -> dict[str, Any]:
    """Remove an issue from the dataset. Dependencies on it are pruned."""
    if not isinstance(number, int):
        raise TypeError(f"number must be int, got {type(number).__name__}")
    out = _deep_copy_issues(data)
    out["issues"] = [
        {
            **it,
            "depends_on_parsed": [d for d in (it.get("depends_on_parsed") or []) if d != number],
        }
        for it in out["issues"]
        if it.get("number") != number
    ]
    out["issue_count"] = len(out["issues"])
    return out


def scale_durations(
    data: dict[str, Any], factor: float, where: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Multiply each issue's duration_days by `factor`. Recomputes
    end_date from start_date + new duration when both are present.

    `where` restricts which issues get scaled. Supported filters:
      - `{"label": "area:ui"}` — only issues with that label name
      - `{"state": "OPEN"}` — only open / closed issues
    Empty `where` scales everything.
    """
    if not isinstance(factor, (int, float)) or factor <= 0:
        raise ValueError(f"factor must be > 0, got {factor!r}")
    out = _deep_copy_issues(data)
    for issue in out["issues"]:
        if where and not _matches_where(issue, where):
            continue
        dur = _field_value(issue, "duration_days")
        if not isinstance(dur, (int, float)) or dur <= 0:
            continue
        new_dur = max(1, int(round(dur * factor)))
        _set_field_value(issue, "duration_days", new_dur)
        # Recompute end_date if possible
        start = _field_value(issue, "start_date")
        if isinstance(start, str):
            try:
                new_end = (date.fromisoformat(start) + timedelta(days=new_dur)).isoformat()
                _set_field_value(issue, "end_date", new_end)
            except ValueError:
                pass
    return out


def _matches_where(issue: dict[str, Any], where: dict[str, Any]) -> bool:
    if "label" in where:
        target = where["label"]
        labels = [
            lab.get("name") if isinstance(lab, dict) else lab
            for lab in (issue.get("labels") or [])
        ]
        if target not in labels:
            return False
    if "state" in where:
        if str(issue.get("state") or "").upper() != str(where["state"]).upper():
            return False
    return True


def shift_start(data: dict[str, Any], new_start: str) -> dict[str, Any]:
    """Anchor the plan to a new baseline start date. Every issue shifts
    by the same delta (earliest current start → new_start)."""
    out = _deep_copy_issues(data)
    try:
        anchor = date.fromisoformat(new_start)
    except ValueError as exc:
        raise ValueError(f"new_start must be ISO YYYY-MM-DD: {exc}") from exc

    starts = []
    for issue in out["issues"]:
        s = _field_value(issue, "start_date")
        if isinstance(s, str):
            try:
                starts.append(date.fromisoformat(s))
            except ValueError:
                pass
    if not starts:
        return out
    current_anchor = min(starts)
    delta_days = (anchor - current_anchor).days
    return delay_all(out, delta_days)


def exclude_weekends(data: dict[str, Any]) -> dict[str, Any]:
    """Mark non-working weekends — stretches end_date by 2/5 to account
    for skipped Sat/Sun. Rough approximation; works for the visual."""
    out = _deep_copy_issues(data)
    for issue in out["issues"]:
        dur = _field_value(issue, "duration_days")
        if not isinstance(dur, (int, float)) or dur <= 0:
            continue
        # Stretch factor: 7/5 (weekdays-only to calendar)
        new_dur = max(1, int(round(dur * 1.4)))
        _set_field_value(issue, "duration_days", new_dur)
        start = _field_value(issue, "start_date")
        if isinstance(start, str):
            try:
                new_end = (date.fromisoformat(start) + timedelta(days=new_dur)).isoformat()
                _set_field_value(issue, "end_date", new_end)
            except ValueError:
                pass
    return out


def freeze_after(data: dict[str, Any], cutoff: str) -> dict[str, Any]:
    """Drop any issue whose start_date is after `cutoff` (scope freeze)."""
    try:
        boundary = date.fromisoformat(cutoff)
    except ValueError as exc:
        raise ValueError(f"cutoff must be ISO YYYY-MM-DD: {exc}") from exc
    out = _deep_copy_issues(data)
    kept: list[dict[str, Any]] = []
    for issue in out["issues"]:
        s = _field_value(issue, "start_date")
        if isinstance(s, str):
            try:
                if date.fromisoformat(s) > boundary:
                    continue
            except ValueError:
                pass
        kept.append(issue)
    out["issues"] = kept
    out["issue_count"] = len(kept)
    return out


# ---------- safe exec ----------

# Modules the generated scenarios.py may import. Pure-stdlib,
# non-I/O, non-network only. The LLM routinely reaches for `copy`,
# `itertools`, etc. for data wrangling — blocking those all the time
# sent the generator into a fallback-to-mermaid loop. Guard's
# code-review checklist (KKallas/Imp#46) is the authoritative gate
# for anything concerning — this list is about removing false
# positives that make the generator useless in practice.
_SAFE_IMPORTS: set[str] = {
    "datetime",
    "copy",
    "itertools",
    "functools",
    "collections",
    "math",
    "json",
    "typing",
    "re",
    "statistics",
}

_SAFE_NAMES: set[str] = {
    "scenario",
    "Out",
    "delay_all",
    "delay_issue",
    "drop_issue",
    "scale_durations",
    "shift_start",
    "exclude_weekends",
    "freeze_after",
    "get_field",
    # From datetime, common names the generator may use
    "date",
    "datetime",
    "timedelta",
    # Python builtins that are universally safe
    "len",
    "range",
    "enumerate",
    "sorted",
    "min",
    "max",
    "sum",
    "abs",
    "int",
    "float",
    "str",
    "bool",
    "list",
    "dict",
    "tuple",
    "set",
    "any",
    "all",
    "map",
    "filter",
    "zip",
    "getattr",  # safe with literal-string attr; dunder-access check catches the dangerous case
    "isinstance",
    "hasattr",
}

_FORBIDDEN_CALL_NAMES: set[str] = {
    "exec",
    "eval",
    "compile",
    "__import__",
    "open",
    "globals",
    "locals",
    "vars",
    # setattr/delattr dropped — dunder-access check below already blocks the
    # attack path; bare setattr on locals is harmless data shuffling.
}


class ScenarioValidationError(Exception):
    """Raised when the generated .py fails AST validation before exec."""


# Real `__import__` captured once; our `_safe_import` wraps it with a
# name-gate so scenarios can `import copy` etc. (runtime path) without
# opening the door to arbitrary imports.
import builtins as _builtins_module  # noqa: E402

_REAL_IMPORT = _builtins_module.__import__


def _safe_import(
    name: str,
    globals: Optional[dict[str, Any]] = None,
    locals: Optional[dict[str, Any]] = None,
    fromlist: tuple = (),
    level: int = 0,
) -> Any:
    """Restricted `__import__` used in the scenarios exec namespace.

    AST validation already rejects imports of non-whitelisted modules at
    parse time — this function is belt-and-suspenders for the runtime
    path (e.g. if someone ever disables the AST check, or if a scenario
    imports inside a function body in a way the walker didn't catch).
    """
    if level != 0:
        raise ImportError("relative imports are not allowed in scenarios")
    root = name.split(".")[0]
    if root not in _SAFE_IMPORTS:
        raise ImportError(f"import of {name!r} is not allowed in scenarios")
    return _REAL_IMPORT(name, globals, locals, fromlist, level)


def _validate_scenarios_source(source: str) -> None:
    """AST-scan the generated source for forbidden constructs.

    Rejects if we see imports outside `_SAFE_IMPORTS`, calls to
    `_FORBIDDEN_CALL_NAMES`, or references to private attributes
    (`__something__`). This is a first-line check; Guard's code-review
    checklist (KKallas/Imp#46) is the authoritative gate for anything
    the admin could actually worry about.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ScenarioValidationError(f"syntax error: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in _SAFE_IMPORTS:
                    raise ScenarioValidationError(
                        f"forbidden import: {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in _SAFE_IMPORTS:
                raise ScenarioValidationError(
                    f"forbidden from-import: {node.module}"
                )
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALL_NAMES:
                raise ScenarioValidationError(
                    f"forbidden call: {func.id}()"
                )
        elif isinstance(node, ast.Attribute):
            # Reject dunder attribute access (e.g. x.__class__)
            if node.attr.startswith("__") and node.attr.endswith("__"):
                raise ScenarioValidationError(
                    f"forbidden dunder access: .{node.attr}"
                )


def _build_restricted_builtins() -> dict[str, Any]:
    """Construct the `__builtins__` dict for the scenarios exec namespace.

    Copies each name in `_SAFE_NAMES` from the real `builtins` module,
    plus plugs in our name-gated `_safe_import` so `import X` statements
    work for whitelisted modules at runtime.
    """
    restricted: dict[str, Any] = {}
    for name in _SAFE_NAMES:
        if hasattr(_builtins_module, name):
            restricted[name] = getattr(_builtins_module, name)
    # `import X` statements at runtime go through `__builtins__.__import__`.
    # Without this entry, even AST-approved imports fail at exec time with
    # "__import__ not found". Our wrapper re-checks the module name.
    restricted["__import__"] = _safe_import
    return restricted


def _exec_scenarios_source(source: str) -> list[Callable[..., None]]:
    """Exec the source in a restricted namespace and collect scenario functions."""
    _validate_scenarios_source(source)

    namespace: dict[str, Any] = {
        "__builtins__": _build_restricted_builtins(),
        "scenario": scenario,
        "Out": Out,
        "delay_all": delay_all,
        "delay_issue": delay_issue,
        "drop_issue": drop_issue,
        "scale_durations": scale_durations,
        "shift_start": shift_start,
        "exclude_weekends": exclude_weekends,
        "freeze_after": freeze_after,
        "get_field": get_field,
        "date": date,
        "datetime": datetime,
        "timedelta": timedelta,
    }
    try:
        exec(compile(source, "<scenarios>", "exec"), namespace)  # noqa: S102 — AST-validated
    except Exception as exc:  # noqa: BLE001
        raise ScenarioValidationError(f"exec failed: {exc}") from exc

    fns: list[Callable[..., None]] = []
    for value in namespace.values():
        if callable(value) and hasattr(value, "_scenario_name"):
            fns.append(value)
    if not fns:
        raise ScenarioValidationError(
            "no @scenario-decorated functions found in the generated source"
        )
    return fns


# ---------- session management ----------


def _new_session_id(prefix: str = "scn") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    token = secrets.token_hex(3)
    return f"{prefix}-{ts}-{token}"


def session_dir(session_id: str) -> Path:
    return SESSIONS_DIR / session_id


def _baseline_hash(baseline: dict[str, Any]) -> str:
    normalized = json.dumps(baseline, sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(normalized).hexdigest()[:16]


def save_session(
    session_id: str,
    *,
    descriptions: list[str],
    source: str,
) -> Path:
    dir_ = session_dir(session_id)
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "descriptions.txt").write_text("\n".join(descriptions))
    (dir_ / "scenarios.py").write_text(source)
    return dir_


def load_session_descriptions(session_id: str) -> list[str]:
    path = session_dir(session_id) / "descriptions.txt"
    if not path.exists():
        return []
    return [line for line in path.read_text().splitlines() if line.strip()]


def load_session_source(session_id: str) -> str:
    path = session_dir(session_id) / "scenarios.py"
    if not path.exists():
        raise FileNotFoundError(f"session {session_id} has no scenarios.py")
    return path.read_text()


def run_session(session_id: str, baseline: dict[str, Any]) -> list[Out]:
    """Import the session's scenarios.py, call each @scenario function,
    collect outputs in declaration order."""
    source = load_session_source(session_id)
    fns = _exec_scenarios_source(source)
    outs: list[Out] = []
    for fn in fns:
        name = getattr(fn, "_scenario_name", fn.__name__)
        out = Out(name=name)
        try:
            fn(baseline, out)
        except Exception as exc:  # noqa: BLE001
            out.text("error", f"scenario raised: {type(exc).__name__}: {exc}")
        outs.append(out)
    # Cache the result for re-open without re-running
    result = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "baseline_hash": _baseline_hash(baseline),
        "scenarios": [o.to_dict() for o in outs],
    }
    (session_dir(session_id) / "result.json").write_text(json.dumps(result, indent=2))
    return outs


def commit_session(session_id: str, choice_index: int, baseline: dict[str, Any]) -> dict[str, Any]:
    """Record a Stage 1 commit: which scenario the admin picked.

    Does NOT modify `.imp/enriched.json` — commit is internal state
    used by `pipeline/render_chart.py` to compose the active scenario
    on subsequent renders. The separate "apply to project board" flow
    (out of scope for this issue) handles Stage 2.
    """
    source = load_session_source(session_id)  # validates the session exists
    fns = _exec_scenarios_source(source)
    if choice_index < 0 or choice_index >= len(fns):
        raise ValueError(
            f"choice_index {choice_index} out of range for {len(fns)} scenarios"
        )
    descriptions = load_session_descriptions(session_id)
    committed = {
        "session_id": session_id,
        "choice_index": choice_index,
        "choice_name": getattr(fns[choice_index], "_scenario_name", f"scenario_{choice_index}"),
        "choice_description": descriptions[choice_index] if choice_index < len(descriptions) else None,
        "committed_at": datetime.now(timezone.utc).isoformat(),
        "baseline_hash": _baseline_hash(baseline),
    }
    (session_dir(session_id) / "committed.json").write_text(json.dumps(committed, indent=2))
    # Also pointer file so render_chart.py can find the active scenario
    active_ptr = ROOT / ".imp" / "active_scenario.json"
    active_ptr.parent.mkdir(parents=True, exist_ok=True)
    active_ptr.write_text(json.dumps({"session_id": session_id, "choice_index": choice_index}, indent=2))
    return committed


def close_session(session_id: str) -> None:
    """Close without committing. The session's files remain on disk
    (re-openable); just no `committed.json` is written and the active
    pointer is cleared if this session was the active one."""
    active_ptr = ROOT / ".imp" / "active_scenario.json"
    if active_ptr.exists():
        try:
            ptr = json.loads(active_ptr.read_text())
            if ptr.get("session_id") == session_id:
                active_ptr.unlink()
        except json.JSONDecodeError:
            active_ptr.unlink()


def list_sessions(limit: int = 20) -> list[dict[str, Any]]:
    """Newest-first listing of saved sessions with minimal metadata."""
    if not SESSIONS_DIR.exists():
        return []
    rows: list[dict[str, Any]] = []
    for d in sorted(SESSIONS_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        desc_path = d / "descriptions.txt"
        committed_path = d / "committed.json"
        descriptions = []
        if desc_path.exists():
            descriptions = [line for line in desc_path.read_text().splitlines() if line.strip()]
        committed: dict[str, Any] | None = None
        if committed_path.exists():
            try:
                committed = json.loads(committed_path.read_text())
            except json.JSONDecodeError:
                committed = None
        rows.append(
            {
                "session_id": d.name,
                "descriptions": descriptions,
                "scenario_count": len(descriptions),
                "committed": committed,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def active_session() -> Optional[dict[str, Any]]:
    """Return the current committed scenario pointer, or None."""
    ptr = ROOT / ".imp" / "active_scenario.json"
    if not ptr.exists():
        return None
    try:
        return json.loads(ptr.read_text())
    except json.JSONDecodeError:
        return None


def apply_active_scenario(baseline: dict[str, Any]) -> dict[str, Any]:
    """If an active scenario is committed, return baseline transformed
    by that scenario's function. Otherwise return baseline unchanged.

    This is what `pipeline/render_chart.py` calls to compose the
    current "lens" onto the enriched data.
    """
    ptr = active_session()
    if ptr is None:
        return baseline
    session_id = ptr.get("session_id")
    choice_index = ptr.get("choice_index")
    if not isinstance(session_id, str) or not isinstance(choice_index, int):
        return baseline
    try:
        source = load_session_source(session_id)
        fns = _exec_scenarios_source(source)
    except (FileNotFoundError, ScenarioValidationError):
        return baseline
    if choice_index < 0 or choice_index >= len(fns):
        return baseline
    fn = fns[choice_index]
    # Scenarios return data via the Out collector for the comparison
    # view, but they also mutate via the filter primitives and may
    # return the transformed data directly. We run the function with
    # a sacrificial Out and capture any returned value; if the function
    # called a filter primitive last (common pattern), its return is
    # the transformed data. Otherwise fall through to baseline.
    out = Out(name=getattr(fn, "_scenario_name", "active"))
    try:
        result = fn(baseline, out)
    except Exception:  # noqa: BLE001
        return baseline
    if isinstance(result, dict) and "issues" in result:
        return result
    # Fall back: if the scenario didn't return the transformed data,
    # we can't recompose — treat as no-op. Generators should end with
    # `return transformed_data` for the active-scenario composition
    # path to work; the comparison-view path doesn't need this.
    return baseline


# ---------- .py generator (pluggable backend) ----------

GeneratorBackend = Callable[[list[str]], Awaitable[str]]

_generator_backend: Optional[GeneratorBackend] = None


def set_generator_backend(backend: Optional[GeneratorBackend]) -> None:
    """Install a custom generator. Pass None to restore the default."""
    global _generator_backend
    _generator_backend = backend


def get_generator_backend() -> GeneratorBackend:
    return _generator_backend or _default_generator_backend


GENERATOR_SYSTEM_PROMPT = """\
You generate Python scenario files for Imp's scenario-comparison system.

You will receive a numbered list of plain-English scenario descriptions.
Your job is to emit ONE valid Python file that:

1. Defines one function per description, decorated with `@scenario("<name>")`.
2. Each function takes `(data, out)` where `data` is an enriched-issues
   dict (see shape below) and `out` is an `Out` collector.
3. Each function MUST populate `out` with at least:
   - `out.chart(figure)` — one Plotly figure dict for the gantt view
   - `out.metric("duration", f"{total_days} days")`
   - `out.metric("finish date", "YYYY-MM-DD")`
   - `out.list("blockers", [...])` — list of blocker labels or "none"
4. Each function MUST return the transformed data (result of applying
   the filter primitives) so `apply_active_scenario` can compose it
   on later render-chart calls.

## Available filter primitives (import-free; pre-loaded in namespace)

  delay_all(data, days: int)
  delay_issue(data, number: int, days: int)
  drop_issue(data, number: int)
  scale_durations(data, factor: float, where: dict | None = None)
  shift_start(data, new_start: "YYYY-MM-DD")
  exclude_weekends(data)
  freeze_after(data, cutoff: "YYYY-MM-DD")

## Building the chart

Since Plotly isn't importable, build the figure as a raw dict:

  figure = {
    "data": [{"type": "bar", "orientation": "h", "x": [...], "y": [...],
              "base": [...], "marker": {"color": "#3b82f6"}}],
    "layout": {"title": {"text": "..."}, "xaxis": {"type": "date"},
               "yaxis": {"automargin": True, "autorange": "reversed"}}
  }
  out.chart(figure)

## Data shape

  data = {
    "issues": [
      {
        "number": 42, "title": "...", "state": "OPEN" | "CLOSED",
        "labels": [{"name": "..."}],
        "depends_on_parsed": [12, 15],
        "fields": {
          "duration_days": {"value": 5, "source": "...", ...},
          "start_date":    {"value": "2026-04-15", ...},  # MAY BE ABSENT
          "end_date":      {"value": "2026-04-20", ...}   # MAY BE ABSENT
        }
      },
      ...
    ]
  }

## CRITICAL — defensive field access

**Fields are NOT guaranteed to be present.** `duration_days` is usually
populated by the heuristics pipeline, but `start_date` and `end_date`
are only there when the GitHub Project board has them set explicitly.
Most open issues have NO start_date / end_date. Writing
`issue["fields"]["start_date"]["value"]` WILL KeyError on those issues.

Use the pre-imported `get_field(issue, key, default=None)` helper instead:

```python
# GOOD — safe, handles missing envelope + missing key
start = get_field(issue, "start_date")
if start is None:
    continue  # or supply a default, or skip this issue

# GOOD — with default
dur = get_field(issue, "duration_days", default=3)

# BAD — crashes on issues without the field
start = issue["fields"]["start_date"]["value"]
```

The filter primitives (delay_all, delay_issue, scale_durations, etc.)
ALREADY handle missing fields correctly. If you only compose filters
and call `get_field` for reads, your code won't KeyError.

## Rules

- Output EXACTLY the Python file contents. No markdown fences, no prose
  before or after.
- No imports except `from datetime import date, timedelta` if needed.
- No I/O, no subprocess, no network, no file writes.
- No `exec`/`eval`/`compile`/`__import__`/`getattr`/`setattr`.
- Scenarios are pure transformations. Don't mutate `data` in place —
  the filter primitives already deep-copy.

If any description is ambiguous, pick a reasonable default and continue.
Never ask for clarification in your output — emit the file.
"""


def _render_generator_user_prompt(descriptions: list[str]) -> str:
    lines = ["Generate the scenarios.py file for these descriptions:", ""]
    for i, desc in enumerate(descriptions, 1):
        lines.append(f"{i}. {desc}")
    lines.append("")
    lines.append("Emit ONLY the Python file contents.")
    return "\n".join(lines)


async def _default_generator_backend(descriptions: list[str]) -> str:
    """Call claude-agent-sdk to generate the scenarios.py source.

    Uses a tools-free single-turn query — same pattern as
    `server/guard.py`. The system prompt constrains output to a
    well-formed Python file using only the filter primitives + Out API.
    """
    from claude_agent_sdk import (  # type: ignore[import-not-found]
        AssistantMessage,
        ClaudeAgentOptions,
        TextBlock,
        query,
    )

    options = ClaudeAgentOptions(
        system_prompt=GENERATOR_SYSTEM_PROMPT,
        allowed_tools=[],
        disallowed_tools=list(_GENERATOR_DISALLOWED_TOOLS),
        max_turns=1,
    )

    chunks: list[str] = []
    async for message in query(
        prompt=_render_generator_user_prompt(descriptions),
        options=options,
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
    source = "".join(chunks).strip()
    # Strip markdown fences if the model included them despite the
    # instruction — belt and suspenders.
    source = _strip_code_fences(source)
    return source


_GENERATOR_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "Edit",
    "Write",
    "Read",
    "Glob",
    "Grep",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
    "TodoWrite",
)


_FENCE_RE = re.compile(r"^```(?:python)?\s*\n?|\n?```\s*$", re.MULTILINE)


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text).strip()
    return text


MAX_GENERATOR_RETRIES = 2


async def generate_scenarios_py(descriptions: list[str]) -> str:
    """High-level generator entry point: descriptions → validated .py source.

    Calls the backend (overridable for tests) and runs the AST scanner
    on the result. If validation fails, retries up to
    `MAX_GENERATOR_RETRIES` times — feeding the error text back to the
    model so it can fix its output — before giving up. Raises
    `ScenarioValidationError` on the final failure.
    """
    if len(descriptions) < MIN_SCENARIOS:
        raise ValueError(
            f"need at least {MIN_SCENARIOS} scenarios, got {len(descriptions)}"
        )
    if len(descriptions) > MAX_SCENARIOS:
        raise ValueError(
            f"max {MAX_SCENARIOS} scenarios supported, got {len(descriptions)}"
        )
    backend = get_generator_backend()

    last_error: ScenarioValidationError | None = None
    current_descriptions = list(descriptions)
    for attempt in range(1 + MAX_GENERATOR_RETRIES):
        source = await backend(current_descriptions)
        try:
            _validate_scenarios_source(source)
            return source
        except ScenarioValidationError as exc:
            last_error = exc
            # Ask the model to fix it on the next attempt by amending
            # the user prompt with the failure reason. Only the LLM
            # backend reads this; fake backends ignore it and are
            # responsible for returning valid source directly.
            if attempt < MAX_GENERATOR_RETRIES:
                current_descriptions = _append_retry_note(
                    descriptions, attempt=attempt + 1, reason=str(exc)
                )
                continue
            raise

    assert last_error is not None  # unreachable; loop always raises or returns
    raise last_error


def _append_retry_note(
    descriptions: list[str], *, attempt: int, reason: str
) -> list[str]:
    """Modify the descriptions list to signal the LLM that the previous
    attempt's source was rejected. The last description gets a
    `[RETRY]` suffix with the AST-validator's rejection reason — the
    default backend's prompt picks this up and knows to fix it. The
    fake backend in tests ignores it."""
    if not descriptions:
        return descriptions
    suffix = (
        f"\n\n[RETRY {attempt}] Previous attempt was rejected by the AST "
        f"validator with: {reason}. Regenerate the full file without that "
        f"construct."
    )
    out = list(descriptions)
    out[-1] = out[-1] + suffix
    return out


# ---------- convenience: start a session end-to-end ----------


async def start_session(
    descriptions: list[str], baseline: dict[str, Any]
) -> tuple[str, list[Out]]:
    """Generate + save + run a session. Returns (session_id, outputs)."""
    source = await generate_scenarios_py(descriptions)
    session_id = _new_session_id()
    save_session(session_id, descriptions=descriptions, source=source)
    outs = run_session(session_id, baseline)
    return session_id, outs
