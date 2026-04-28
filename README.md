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
- Naming your project, picking a license, writing a README
- Creating a GitHub repo and pushing your files
- Setting up branch protection (require PR approval)

Other tabs (Queue, Workflows, Tools) are locked until setup completes. Once done, the full interface is available.

## How it works

Imp is deliberately simple. No MCP servers, no complex protocols, no middleware layers.

```
Browser (HTML + JS)
    ↕ WebSocket
FastAPI server (render_route.py)
    ↕ claude-agent-sdk
Claude (with native Bash, Read, Write tools)
    ↕ subprocess
gh CLI, git, python scripts in tools/
```

The agent talks to GitHub through `gh` commands. Tools are plain Python scripts with argparse. Workflows are folders of numbered step scripts. Everything the agent does is a shell command or a file read/write — the same things you'd do manually in a terminal.

### Why no MCP

MCP adds a protocol layer between the agent and the tools. Imp doesn't need it — the agent already has Bash access and can run any command directly. Keeping it simple means:

- Fewer moving parts to break
- Tools are just `.py` files you can run yourself
- No protocol versioning, no server lifecycle, no tool registration
- Easy to debug: if a tool works in your terminal, it works in Imp

## Usage

- **Queue** — work items awaiting your action
- **Chat** — talk to the AI agent (powered by Claude) to manage your project
- **Workflows** — multi-step automations (sync issues, triage, deploy)
- **Tools** — reusable scripts the AI agent can call

## License

MIT
