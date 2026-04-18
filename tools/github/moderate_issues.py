#!/usr/bin/env python3
"""
Issue Moderator for Robot Arena

Finds issues that need formatting, uses Claude to ask clarifying questions
or propose formatted versions, then labels them as llm-ready.

Usage:
    python moderate_issues.py              # Process all unformatted issues
    python moderate_issues.py --dry-run    # Show what would be done (no Claude, no GitHub)
    python moderate_issues.py --test       # Test mode (runs Claude but doesn't post to GitHub)
    python moderate_issues.py --issue 123  # Process specific issue

Requirements:
    - gh CLI installed and authenticated
    - claude CLI installed (npm install -g @anthropic-ai/claude-code)
"""

import subprocess
import json
import sys
import argparse
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _state

# Config - can override with environment variables
REPO = os.environ.get("ROBOT_ARENA_REPO", "KKallas/Robot-Arena")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
MAX_ISSUES = 10

# Global mode flags
DRY_RUN = False
TEST_MODE = False

# Paths for external prompt and context files
TOOLS_DIR = Path(__file__).parent
PROMPT_FILE = TOOLS_DIR / "moderate_issues.md"
PROJECT_DESC_FILE = TOOLS_DIR / "project_description.md"
TOOLS_CONTEXT_DIR = TOOLS_DIR / "tools"

# Load agent instructions (go up one level from 99-tools to find .github)
AGENT_INSTRUCTIONS_PATH = Path(__file__).parent.parent / ".github" / "AGENT_ISSUE_MANAGER.md"


def run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a command and return result."""
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def check_requirements() -> bool:
    """Check if required tools are installed."""
    missing = []

    # Check gh CLI
    try:
        subprocess.run(["gh", "--version"], capture_output=True, check=True)
    except FileNotFoundError:
        missing.append(("gh", "Install from: https://cli.github.com/"))
    except subprocess.CalledProcessError:
        missing.append(("gh", "Run: gh auth login"))

    # Check claude CLI (only needed for non-dry-run)
    try:
        subprocess.run(["claude", "--version"], capture_output=True, check=True)
    except FileNotFoundError:
        missing.append(("claude", "Install with: npm install -g @anthropic-ai/claude-code"))

    if missing:
        print("\n❌ Missing requirements:")
        for tool, fix in missing:
            print(f"  - {tool}: {fix}")
        print("")
        return False

    return True


def get_issues_needing_moderation() -> list[dict]:
    """Get all issues that need formatting (no llm-ready, no needs-human label)."""
    result = run_cmd([
        "gh", "issue", "list",
        "--repo", REPO,
        "--state", "open",
        "--limit", "100",
        "--json", "number,title,body,labels,comments"
    ], check=False)

    if result.returncode != 0:
        print(f"\n❌ Failed to fetch issues from GitHub:")
        print(f"  {result.stderr.strip()}")
        print(f"\nTry running: gh auth login")
        sys.exit(1)

    issues = json.loads(result.stdout)

    # Filter: no llm-ready, no needs-human, no in-progress
    filtered = []
    for issue in issues:
        label_names = [l["name"] for l in issue.get("labels", [])]
        if "llm-ready" not in label_names and \
           "needs-human" not in label_names and \
           "in-progress" not in label_names:
            filtered.append(issue)

    return filtered


def get_issue(issue_number: int) -> dict:
    """Get a specific issue."""
    result = run_cmd([
        "gh", "issue", "view", str(issue_number),
        "--repo", REPO,
        "--json", "number,title,body,labels,comments"
    ])
    return json.loads(result.stdout)


def load_file(path: Path, fallback: str = "") -> str:
    """Load a text file, return fallback if it doesn't exist."""
    if path.exists():
        return path.read_text()
    return fallback


def load_tools_context() -> str:
    """Load all .md files from the tools/ directory as combined context."""
    if not TOOLS_CONTEXT_DIR.exists():
        return ""
    parts = []
    for md_file in sorted(TOOLS_CONTEXT_DIR.glob("*.md")):
        parts.append(md_file.read_text())
    return "\n\n".join(parts)


def get_agent_instructions() -> str:
    """Load the agent instructions from file."""
    if AGENT_INSTRUCTIONS_PATH.exists():
        return AGENT_INSTRUCTIONS_PATH.read_text()
    else:
        return """
You are an Issue Manager agent. Your job is to turn messy human issues into
LLM-ready format by asking clarifying questions.

For each issue, you should:
1. Identify what's missing (target files, action, acceptance criteria)
2. Comment with questions or a proposed formatted version
3. Add appropriate labels

Use the gh CLI to interact with issues:
- gh issue comment NUMBER --body "..." --repo REPO
- gh issue edit NUMBER --add-label "llm-ready" --repo REPO
"""


def build_prompt(issue: dict, agent_instructions: str, test_mode: bool = False) -> str:
    """Build the prompt for Claude from external .md template."""
    comments = issue.get("comments", [])
    comments_text = ""
    if comments:
        comments_text = "\n\n## Existing Comments\n\n"
        for c in comments:
            comments_text += f"**{c.get('author', {}).get('login', 'unknown')}:**\n{c.get('body', '')}\n\n"

    labels = [l["name"] for l in issue.get("labels", [])]

    test_notice = ""
    if test_mode:
        test_notice = f"""
## ⚠️ TEST MODE ACTIVE

You are in TEST MODE. Instead of running actual gh commands, you should:
1. PRINT the exact command you WOULD run (prefixed with "[TEST] Would run:")
2. Explain what that command would do
3. DO NOT actually execute any gh commands

Example output:
[TEST] Would run: gh issue comment 123 --repo {REPO} --body "..."
This would post a comment asking for clarification about target files.

"""

    bot_signature = f"""

---
🤖 *Generated by Issue Manager Agent (Claude {CLAUDE_MODEL})*"""

    if test_mode:
        action_instructions = "[TEST MODE] Print the commands you would run, don't execute them."
    else:
        action_instructions = f"Use the gh CLI to take action. The repo is: {REPO}. Execute the commands directly - do not ask for permission."

    # Load the prompt template from the .md file
    project_description = load_file(PROJECT_DESC_FILE, "No project description available.")
    tools_context = load_tools_context()
    template = load_file(PROMPT_FILE)

    if template:
        return (template
            .replace("{{test_notice}}", test_notice)
            .replace("{{project_description}}", project_description)
            .replace("{{tools_context}}", tools_context)
            .replace("{{agent_instructions}}", agent_instructions)
            .replace("{{issue_number}}", str(issue['number']))
            .replace("{{issue_title}}", issue['title'])
            .replace("{{labels}}", ', '.join(labels) if labels else 'none')
            .replace("{{issue_body}}", issue['body'] or '(empty)')
            .replace("{{comments_text}}", comments_text)
            .replace("{{action_instructions}}", action_instructions)
            .replace("{{bot_signature}}", bot_signature)
            .replace("{{repo}}", REPO)
        )

    # Fallback: hardcoded prompt if .md file is missing
    return f"""You are the Issue Manager agent.
{test_notice}
{agent_instructions}

**Issue #{issue['number']}:** {issue['title']}
**Labels:** {', '.join(labels) if labels else 'none'}

{issue['body'] or '(empty)'}
{comments_text}

{action_instructions}
ALL comments MUST end with: {bot_signature}
"""


def process_issue(issue: dict, dry_run: bool = False, test_mode: bool = False) -> bool:
    """Process a single issue with Claude."""
    agent_instructions = get_agent_instructions()
    prompt = build_prompt(issue, agent_instructions, test_mode=test_mode)

    print(f"\n{'='*60}")
    print(f"Processing issue #{issue['number']}: {issue['title']}")
    if test_mode:
        print(f"⚠️  TEST MODE - Claude will run but won't post to GitHub")
    elif dry_run:
        print(f"⚠️  DRY RUN - Nothing will be executed")
    print(f"{'='*60}")

    # Show issue details
    labels = [l["name"] for l in issue.get("labels", [])]
    print(f"\nIssue details:")
    print(f"  Number: #{issue['number']}")
    print(f"  Title: {issue['title']}")
    print(f"  Labels: {', '.join(labels) if labels else 'none'}")
    print(f"  Body length: {len(issue.get('body', '') or '')} chars")
    print(f"  Comments: {len(issue.get('comments', []))}")

    if dry_run:
        print(f"\n[DRY RUN] Would send prompt to Claude ({len(prompt)} chars)")
        print(f"\n--- Prompt preview (first 500 chars) ---")
        print(prompt[:500])
        print("...")
        return True

    # Run Claude
    # In test mode, don't allow actual gh commands
    allowed_tools = "Bash(echo *)" if test_mode else "Bash(gh *)"

    print(f"\nRunning Claude...")
    print(f"  Allowed tools: {allowed_tools}")
    print(f"\n--- Claude output (streaming) ---")
    sys.stdout.flush()

    usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    try:
        process = subprocess.Popen(
            ["claude", "-p", prompt, "--allowedTools", allowed_tools, "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if event.get("type") == "assistant":
                    content = event.get("message", {}).get("content", [])
                    for block in content:
                        if block.get("type") == "text":
                            print(block.get("text", ""), end="", flush=True)
                elif event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        print(delta.get("text", ""), end="", flush=True)
                elif event.get("type") == "result":
                    result = event.get("result", "")
                    if result and isinstance(result, str):
                        print(result, flush=True)
                    usage["cost_usd"] = event.get("cost_usd", 0) or event.get("total_cost_usd", 0) or 0
                    result_usage = event.get("usage", {})
                    if result_usage:
                        usage["input_tokens"] = result_usage.get("input_tokens", 0)
                        usage["output_tokens"] = result_usage.get("output_tokens", 0)
            except json.JSONDecodeError:
                print(line, flush=True)

        process.wait()

        stderr_output = process.stderr.read()
        if stderr_output:
            print(f"\n[stderr]: {stderr_output}", flush=True)

        print(f"\n--- End output ---")

        success = process.returncode == 0
        _state.record_run("moderate_issues", f"#{issue['number']}",
                          usage["input_tokens"], usage["output_tokens"],
                          usage["cost_usd"], success)

        if not success:
            print(f"Claude returned non-zero: {process.returncode}")
            return False

        if test_mode:
            print(f"\n✅ Test completed. No changes made to GitHub.")

        return True

    except Exception as e:
        print(f"Error running Claude: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Moderate GitHub issues for Robot Arena",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  (default)   Run Claude and post to GitHub
  --test      Run Claude but DON'T post to GitHub (safe testing)
  --dry-run   Don't run Claude at all, just show what would happen

Examples:
  python moderate_issues.py --dry-run           # Preview issues to process
  python moderate_issues.py --test --issue 123  # Test Claude on issue #123
  python moderate_issues.py --issue 123         # Actually process issue #123
        """
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without running Claude or posting")
    parser.add_argument("--test", action="store_true",
                        help="Run Claude but don't post to GitHub (safe testing)")
    parser.add_argument("--issue", type=int,
                        help="Process a specific issue number")
    parser.add_argument("--max", type=int, default=MAX_ISSUES,
                        help=f"Max issues to process (default: {MAX_ISSUES})")
    parser.add_argument("--max-tokens", type=int, default=0,
                        help="Stop after using this many tokens total (reads from .state.json)")
    args = parser.parse_args()

    print("=" * 60)
    print("  Robot Arena Issue Moderator")
    print("  Turning messy issues into LLM-ready format")
    print("=" * 60)

    if args.dry_run:
        print("\n⚠️  DRY RUN MODE - No Claude, no GitHub changes")
    elif args.test:
        print("\n⚠️  TEST MODE - Claude runs but no GitHub changes")
    else:
        print("\n🚀 LIVE MODE - Will post to GitHub!")

    # Check requirements
    if not check_requirements():
        sys.exit(1)

    if args.issue:
        # Process specific issue
        issue = get_issue(args.issue)
        process_issue(issue, dry_run=args.dry_run, test_mode=args.test)
    else:
        # Process all issues needing moderation
        issues = get_issues_needing_moderation()

        if not issues:
            print("\n✅ No issues need moderation. All caught up!")
            return

        print(f"\nFound {len(issues)} issues needing moderation:")
        for i in issues[:args.max]:
            labels = [l["name"] for l in i.get("labels", [])]
            print(f"  #{i['number']}: {i['title']} [{', '.join(labels) if labels else 'no labels'}]")

        if len(issues) > args.max:
            print(f"  ... and {len(issues) - args.max} more (use --max to increase)")

        print("")

        processed = 0
        for issue in issues[:args.max]:
            if args.max_tokens and not _state.check_budget(args.max_tokens):
                print(f"\n⏸️  Token budget reached ({_state.get_tokens_used():,} / {args.max_tokens:,}). Stopping.")
                break
            if process_issue(issue, dry_run=args.dry_run, test_mode=args.test):
                processed += 1
            else:
                print(f"⚠️  Failed to process issue #{issue['number']}")

        print(f"\n{'='*60}")
        print(f"  Done! Processed {processed}/{min(len(issues), args.max)} issues")
        print(f"{'='*60}")


if __name__ == "__main__":
    main()
