#!/bin/bash
#
# Run all 99-tools agents in sequence with a shared token budget.
# Stops when the budget is reached. Resume by running again.
#
# Usage:
#   ./99-tools/run_all.sh                    # Run with default 200k token budget
#   ./99-tools/run_all.sh --max-tokens 50000 # Run with 50k budget
#   ./99-tools/run_all.sh --max-tasks 5      # Stop after 5 total tasks across all steps
#   ./99-tools/run_all.sh --dry-run          # Preview what would happen
#   ./99-tools/run_all.sh --test             # Run Claude but don't touch GitHub
#   ./99-tools/run_all.sh --reset            # Reset token counter and run
#   ./99-tools/run_all.sh --status           # Just show current token usage

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MAX_TOKENS=200000
MAX_TASKS=0  # 0 = unlimited
MODE=""
RESET=false
STATUS_ONLY=false

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --max-tokens)
            MAX_TOKENS="$2"
            shift 2
            ;;
        --max-tasks)
            MAX_TASKS="$2"
            shift 2
            ;;
        --dry-run)
            MODE="--dry-run"
            shift
            ;;
        --test)
            MODE="--test"
            shift
            ;;
        --reset)
            RESET=true
            shift
            ;;
        --status)
            STATUS_ONLY=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--max-tokens N] [--max-tasks N] [--dry-run|--test] [--reset] [--status]"
            exit 1
            ;;
    esac
done

# Status only
if $STATUS_ONLY; then
    python3 "$SCRIPT_DIR/_state.py" status
    exit 0
fi

# Reset if requested
if $RESET; then
    python3 "$SCRIPT_DIR/_state.py" reset
fi

# Helper: get current run count from state
get_run_count() {
    python3 "$SCRIPT_DIR/_state.py" run-count
}

# Helper: get tokens used
get_tokens_used() {
    python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import _state; print(_state.get_tokens_used())
"
}

echo "============================================================"
echo "  99-tools Agent Runner"
echo "  Token budget: $MAX_TOKENS"
if [ "$MAX_TASKS" -gt 0 ] 2>/dev/null; then
    echo "  Task limit:   $MAX_TASKS"
fi
echo "============================================================"

# Show current usage
echo ""
python3 "$SCRIPT_DIR/_state.py" status
echo ""

# Check budget before starting
USED=$(get_tokens_used)
if [ "$USED" -ge "$MAX_TOKENS" ]; then
    echo "Token budget already exhausted ($USED / $MAX_TOKENS)."
    echo "Run with --reset to clear the counter, or increase --max-tokens."
    exit 0
fi

# Snapshot run count at start
RUNS_AT_START=$(get_run_count)

# Step 1: Moderate issues
echo ""
echo "--- Step 1: Moderating issues ---"
REMAINING_TASKS=""
if [ "$MAX_TASKS" -gt 0 ] 2>/dev/null; then
    TASKS_DONE=$(( $(get_run_count) - RUNS_AT_START ))
    REMAINING_TASKS=$(( MAX_TASKS - TASKS_DONE ))
    if [ "$REMAINING_TASKS" -le 0 ]; then
        echo "Task limit reached. Skipping."
    else
        python3 "$SCRIPT_DIR/moderate_issues.py" $MODE --max-tokens "$MAX_TOKENS" --max "$REMAINING_TASKS" || true
    fi
else
    python3 "$SCRIPT_DIR/moderate_issues.py" $MODE --max-tokens "$MAX_TOKENS" || true
fi

# Check budget
USED=$(get_tokens_used)
if [ "$USED" -ge "$MAX_TOKENS" ]; then
    echo ""
    echo "Token budget reached after moderation. Stopping."
    python3 "$SCRIPT_DIR/_state.py" status
    exit 0
fi

# Step 2: Solve issues
echo ""
echo "--- Step 2: Solving issues ---"
if [ "$MAX_TASKS" -gt 0 ] 2>/dev/null; then
    TASKS_DONE=$(( $(get_run_count) - RUNS_AT_START ))
    REMAINING_TASKS=$(( MAX_TASKS - TASKS_DONE ))
    if [ "$REMAINING_TASKS" -le 0 ]; then
        echo "Task limit reached. Skipping."
    else
        python3 "$SCRIPT_DIR/solve_issues.py" $MODE --max-tokens "$MAX_TOKENS" --max "$REMAINING_TASKS" || true
    fi
else
    python3 "$SCRIPT_DIR/solve_issues.py" $MODE --max-tokens "$MAX_TOKENS" || true
fi

# Check budget
USED=$(get_tokens_used)
if [ "$USED" -ge "$MAX_TOKENS" ]; then
    echo ""
    echo "Token budget reached after solving. Stopping."
    python3 "$SCRIPT_DIR/_state.py" status
    exit 0
fi

# Step 3: Fix PRs
echo ""
echo "--- Step 3: Fixing PRs ---"
if [ "$MAX_TASKS" -gt 0 ] 2>/dev/null; then
    TASKS_DONE=$(( $(get_run_count) - RUNS_AT_START ))
    REMAINING_TASKS=$(( MAX_TASKS - TASKS_DONE ))
    if [ "$REMAINING_TASKS" -le 0 ]; then
        echo "Task limit reached. Skipping."
    else
        python3 "$SCRIPT_DIR/fix_prs.py" $MODE --max-tokens "$MAX_TOKENS" --max "$REMAINING_TASKS" || true
    fi
else
    python3 "$SCRIPT_DIR/fix_prs.py" $MODE --max-tokens "$MAX_TOKENS" || true
fi

# Summary
TOTAL_TASKS=$(( $(get_run_count) - RUNS_AT_START ))
echo ""
echo "============================================================"
echo "  Run complete! ($TOTAL_TASKS tasks processed)"
echo "============================================================"
python3 "$SCRIPT_DIR/_state.py" status
