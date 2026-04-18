# github

GitHub operations — from atomic git/gh commands to multi-step AI workflows.

Each `.py` file is an executable. Each `.md` file is the prompt/config
for the matching executable (where applicable).

## Executables

### Atomic operations
| Script | Purpose |
|--------|---------|
| `push.py` | Push local commits to remote |
| `pull.py` | Pull latest changes from remote |
| `fork.py` | Fork a GitHub repository |
| `open_issue.py` | Open a new issue |
| `close_issue.py` | Close an issue |
| `list_issues.py` | List issues (filterable) |
| `open_pr.py` | Open a pull request |
| `merge_pr.py` | Merge a pull request |
| `list_prs.py` | List pull requests |

### AI-powered workflows
| Script | Config | Purpose |
|--------|--------|---------|
| `moderate_issues.py` | `moderate_issues.md` | Format messy issues into structured tasks |
| `solve_issues.py` | `solve_issues.md` | Read an issue, write code, open a PR |
| `fix_prs.py` | `fix_prs_analysis.md`, `fix_prs_fix.md` | Read PR reviews, push fixes |

## Usage

```bash
# Atomic
python tools/github/push.py
python tools/github/open_issue.py --title "Bug report" --body "Details..."
python tools/github/list_issues.py --state open --limit 10

# AI workflows
python tools/github/moderate_issues.py --issue 42
python tools/github/solve_issues.py --issue 42
python tools/github/fix_prs.py --pr 42
```
