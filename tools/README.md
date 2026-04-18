# 99-tools

AI agents that manage your GitHub issues and PRs. Drop it into any project, describe your project in a markdown file, create issues, run the scripts, get PRs. All agent behavior lives in editable markdown files -- no Python knowledge needed to customize.

## Setup

### 1. Install requirements

```bash
# GitHub CLI
brew install gh
gh auth login

# Claude CLI
npm install -g @anthropic-ai/claude-code

# Python 3 and git (you probably already have these)
```

### 2. Add to your project

Copy the `99-tools/` folder into the root of your GitHub repo:

```bash
cp -r 99-tools/ your-project/99-tools/
cd your-project
```

### 3. Fill out project_description.md

Open `99-tools/project_description.md` and describe your project. This is the context every agent receives, so the more you put here the better the results. Include things like:

- What the project does
- Tech stack and key dependencies
- Folder structure overview
- How to build and test
- Any conventions or patterns you follow

Tip: paste your current codebase into an LLM and ask it to write the project description for you.

### 4. Customize the agent prompts (optional)

Each agent has a `.md` file that controls how it behaves:

| File | What it controls |
|---|---|
| `project_description.md` | Project context all agents receive |
| `moderate_issues.md` | How the issue moderator asks questions and formats issues |
| `solve_issues.md` | How the issue solver writes code |
| `fix_prs_analysis.md` | How PR feedback gets analyzed |
| `fix_prs_fix.md` | How PR fixes get made |

The defaults work fine out of the box. When you want to tweak behavior (e.g. "always write tests" or "use our logging pattern"), edit the relevant `.md` file. You can ask an LLM to help write these too.

### 5. Set your repo

```bash
export ROBOT_ARENA_REPO="your-org/your-repo"
```

### 6. Test it

```bash
# See what the agents would do (no Claude, no GitHub changes)
./99-tools/run_all.sh --dry-run
```

## Daily Usage

The workflow is simple: you create issues, run the tools, review PRs.

```
You create issues (messy is fine)
        |
        v
moderate_issues.py -- asks questions, formats into LLM-ready tasks
        |                adds "llm-ready" label when done
        v
solve_issues.py ----- picks up llm-ready issues, writes code, creates PRs
        |
        v
You review PRs, leave comments
        |
        v
fix_prs.py ---------- reads your comments, pushes fixes
```

### Run everything

```bash
# Run all agents with defaults (200k token budget)
./99-tools/run_all.sh

# Set a token budget
./99-tools/run_all.sh --max-tokens 100000

# Limit how many tasks get processed total (across all steps)
./99-tools/run_all.sh --max-tasks 5

# Both limits -- whichever hits first stops execution
./99-tools/run_all.sh --max-tokens 100000 --max-tasks 5
```

`--max-tasks` counts across all three steps. If moderate handles 2 issues and the limit is 5, solve gets up to 3, and so on. If there's nothing to moderate, the full budget goes to solve and fix.

### Run individually

```bash
# Moderate issues
python3 99-tools/moderate_issues.py --dry-run          # Preview
python3 99-tools/moderate_issues.py --test --issue 123 # Test (no GitHub changes)
python3 99-tools/moderate_issues.py --issue 123        # Live

# Solve issues
python3 99-tools/solve_issues.py --dry-run             # Preview
python3 99-tools/solve_issues.py --test --issue 123    # Test (no push/PR)
python3 99-tools/solve_issues.py --issue 123           # Live

# Fix PRs
python3 99-tools/fix_prs.py --dry-run                  # Preview
python3 99-tools/fix_prs.py --test --pr 123            # Test (no push)
python3 99-tools/fix_prs.py --pr 123                   # Live
```

### Testing modes

| Mode | Claude runs? | GitHub changes? | Use case |
|------|-------------|-----------------|----------|
| `--dry-run` | No | No | Preview what would happen |
| `--test` | Yes | No | Test Claude's behavior safely |
| (none) | Yes | Yes | Live execution |

## Token Budget & Resume

All token usage is tracked in `.state.json`. Set a budget, the tools stop when it's reached. When you get more tokens, just run again -- it picks up where it left off (GitHub labels track what's been processed).

```bash
# Check usage
./99-tools/run_all.sh --status

# Reset counters (new billing cycle, bought more tokens, etc.)
./99-tools/run_all.sh --reset

# Individual scripts also support --max-tokens
python3 99-tools/solve_issues.py --max-tokens 50000
```

## Configuration

```bash
# Set your repository (required)
export ROBOT_ARENA_REPO="your-org/your-repo"

# Override Claude model (default: claude-sonnet-4-20250514)
export CLAUDE_MODEL="claude-sonnet-4-20250514"
```

## Template Placeholders

The prompt `.md` files use `{{placeholder}}` syntax that gets replaced at runtime:

| Placeholder | Available in | Description |
|---|---|---|
| `{{project_description}}` | All | Contents of project_description.md |
| `{{issue_number}}` | moderate, solve | GitHub issue number |
| `{{issue_title}}` | moderate, solve | Issue title |
| `{{issue_body}}` | moderate, solve | Issue body text |
| `{{comments_text}}` | moderate, fix_prs | Formatted comments |
| `{{pr_number}}` | fix_prs | PR number |
| `{{pr_title}}` | fix_prs | PR title |
| `{{branch}}` | fix_prs | PR branch name |
| `{{diff}}` | fix_prs | PR diff |
| `{{repo}}` | moderate | Repository name |
| `{{labels}}` | moderate | Current issue labels |
| `{{bot_signature}}` | moderate | Bot attribution line |
| `{{test_notice}}` | moderate | Test mode warning |
| `{{agent_instructions}}` | moderate | From .github/AGENT_ISSUE_MANAGER.md |
| `{{action_instructions}}` | moderate | Test vs live mode instructions |

## File Structure

```
99-tools/
├── README.md                # This file
├── run_all.sh               # Run all agents with token/task budget
├── _state.py                # Token tracking and resume
├── .state.json              # Token usage data (gitignore this)
├── project_description.md   # Your project context
├── moderate_issues.md       # Prompt: issue moderator
├── moderate_issues.py       # Script: issue moderator
├── solve_issues.md          # Prompt: issue solver
├── solve_issues.py          # Script: issue solver
├── fix_prs_analysis.md      # Prompt: PR analysis phase
├── fix_prs_fix.md           # Prompt: PR fix phase
└── fix_prs.py               # Script: PR fixer
```
