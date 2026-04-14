#!/usr/bin/env python3
"""pipeline/project_bootstrap.py — stub awaiting KKallas/Imp#10.

Real implementation (P3.10): provision a GitHub Projects-v2 board with
the seven Imp custom fields (duration_days, start_date, end_date,
confidence, source, assignee_verified, depends_on), idempotent, and
persist the resulting project number to `.imp/config.json`.

Until that work lands, the Setup Agent's `create_imp_project` tool
calls this script and expects a non-zero exit with a clear message, so
the agent can tell the admin the step is deferred and move on.
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True, help="GitHub owner (user or org)")
    parser.add_argument(
        "--title",
        default="Imp",
        help="Project title (default: Imp)",
    )
    parser.parse_args()

    print(
        "pipeline/project_bootstrap.py is a stub — "
        "real implementation lands with KKallas/Imp#10.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
