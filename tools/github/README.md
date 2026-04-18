# github

GitHub operations — issue moderation, issue solving, PR fixing.

Each `.py` file is an executable. Each `.md` file is the prompt/config
for the matching executable.

## Executables

| Script | Config | Purpose |
|--------|--------|---------|
| `moderate_issues.py` | `moderate_issues.md` | Format messy issues into structured tasks |
| `solve_issues.py` | `solve_issues.md` | Read an issue, write code, open a PR |
| `fix_prs.py` | `fix_prs_analysis.md`, `fix_prs_fix.md` | Read PR reviews, push fixes |

## Usage

```bash
python tools/github/moderate_issues.py --issue 42
python tools/github/solve_issues.py --issue 42
python tools/github/fix_prs.py --pr 42
```

## Foreman integration

MCP tools: `run_moderate_issues`, `run_solve_issues`, `run_fix_prs`
