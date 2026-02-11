# Imp

Ultra simple AI agent workflows for Claude Code to automate your GitHub repo. Create issues, run the scripts, get PRs. Make an LLM do the housekeeping and developemnt when you are busy.

## Quick Start

### 1. Get the tools

```bash
cd your-project
git clone https://github.com/KKallas/Imp.git /tmp/imp
cp -r /tmp/imp/99-tools ./99-tools
rm -rf /tmp/imp
```

### 2. Install requirements

```bash
brew install gh && gh auth login       # GitHub CLI
npm install -g @anthropic-ai/claude-code  # Claude CLI
```

### 3. Describe your project

Open `99-tools/project_description.md` and describe what your project does, the tech stack, folder structure, how to build/test. The agents use this as context.

Tip: paste your codebase into any LLM and ask "write a project description for an AI agent that will work on this code."

### 4. Point to your repo

```bash
export ROBOT_ARENA_REPO="your-org/your-repo"
```

### 5. Create issues on GitHub

Just write issues like you normally would -- messy is fine:

> "the login page is slow"
>
> "add dark mode"
>
> "fix the bug where users can submit empty forms"

### 6. Run

```bash
./99-tools/run_all.sh --max-tasks 3
```

That's it. The agents will:
1. Read your issues, ask clarifying questions, format them into structured tasks
2. Pick up ready tasks, write code, create PRs
3. If you leave review comments on PRs, fix them on the next run

### What it looks like

```bash
# First run: agents format your messy issues into structured tasks
./99-tools/run_all.sh --max-tasks 5

# You review the formatted issues on GitHub, maybe answer a question
# Then run again: agents solve the ready issues and create PRs
./99-tools/run_all.sh --max-tasks 5

# You review PRs, leave comments like "use a different variable name here"
# Run again: agents read your comments and push fixes
./99-tools/run_all.sh --max-tasks 5
```

### Control spending

```bash
./99-tools/run_all.sh --max-tokens 100000  # token budget
./99-tools/run_all.sh --max-tasks 3        # task limit
./99-tools/run_all.sh --status             # check usage
./99-tools/run_all.sh --reset              # reset counters
```

### Test safely first

```bash
./99-tools/run_all.sh --dry-run  # preview, no Claude, no GitHub
./99-tools/run_all.sh --test     # runs Claude but doesn't touch GitHub
```

See [99-tools/README.md](99-tools/README.md) for full docs.
