# Imp

AI-powered project manager that lives inside your GitHub repo. Manage issues, run workflows, and chat with an AI agent — all from a single web interface.

## Requirements

- Python 3.11 or newer
- [GitHub CLI (`gh`)](https://cli.github.com/) installed and authenticated

## Setup

1. Copy the Imp folder into your project directory (or clone this repo)
2. Run:

```
python imp.py
```

3. Open http://127.0.0.1:8421 in your browser

That's it. On first run, Imp creates a virtual environment, installs dependencies, and starts the web server. No manual pip install needed.

## First run

When you open the browser for the first time, a setup wizard in the Chat tab guides you through:

- Checking if the folder is a git repository
- Connecting to GitHub via `gh` CLI
- Linking or creating a GitHub repo
- Setting up a project board

Other tabs (Queue, Workflows, Tools) are locked until setup completes. Once done, the full interface is available.

## Usage

- **Queue** — work items awaiting your action
- **Chat** — talk to the AI agent (powered by Claude) to manage your project
- **Workflows** — multi-step automations (sync issues, triage, deploy)
- **Tools** — reusable scripts the AI agent can call

## License

MIT
