# github

CLI tools for managing GitHub issues, pull requests, and repositories via the `gh` and `git` CLIs.

## Scripts

| Script | Purpose | Key Args |
|---|---|---|
| `list_issues.py` | List issues with optional filters | `--state`, `--limit`, `--label`, `--repo` |
| `open_issue.py` | Create a new issue | `--title` (required), `--body`, `--label`, `--repo` |
| `close_issue.py` | Close an issue, optionally with a comment | `issue`, `--reason`, `--comment`, `--repo` |
| `moderate_issues.py` | Send issues to Claude for LLM-ready formatting | `--dry-run`, `--test`, `--issue`, `--max` |
| `solve_issues.py` | Auto-solve `llm-ready` issues with Claude and open PRs | `--dry-run`, `--test`, `--issue`, `--max` |
| `list_prs.py` | List pull requests | `--state`, `--limit`, `--repo` |
| `open_pr.py` | Create a pull request | `--title` (required), `--body`, `--base`, `--head`, `--repo` |
| `merge_pr.py` | Merge a pull request | `pr`, `--method`, `--repo` |
| `fork.py` | Fork a repo without cloning | `owner/repo` |
| `pull.py` | Pull latest changes | `branch` (optional) |
| `push.py` | Push commits to remote | `branch` (optional) |

## Usage

```bash
# List open issues
python github/list_issues.py

# Create an issue with labels
python github/open_issue.py --title "Fix login bug" --body "Details here" --label bug

# Close an issue as completed with a comment
python github/close_issue.py 42 --comment "Fixed in #43"

# Open a PR
python github/open_pr.py --title "Fix login bug" --base main --head fix/login

# Merge a PR with squash
python github/merge_pr.py 43 --method squash

# Moderate issues — dry-run first
python github/moderate_issues.py --dry-run

# Solve LLM-ready issues — dry-run first
python github/solve_issues.py --dry-run
```
