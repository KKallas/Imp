#!/usr/bin/env python3
"""Create a GitHub repo from the current folder and push all files.

Inputs:
  --name (str): Repository name (default: current folder name).
  --private (flag): Create as private repo (default: public).
  --description (str, optional): Repo description.

Process:
  1. git init (if not already a repo)
  2. gh repo create
  3. Add remote, commit all files, push

Output: Prints the repo URL or error details."""

import argparse
import os
import subprocess
import sys


def run(cmd, **kwargs):
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def is_git_repo():
    rc, _, _ = run(["git", "rev-parse", "--is-inside-work-tree"])
    return rc == 0


def has_remote():
    rc, out, _ = run(["git", "remote"])
    return rc == 0 and bool(out.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a GitHub repo from this folder")
    parser.add_argument("--name", default=os.path.basename(os.getcwd()))
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--description", default="")
    args = parser.parse_args()

    # 1. git init
    if not is_git_repo():
        print("Initializing git repo...")
        rc, out, err = run(["git", "init"])
        if rc != 0:
            print(f"git init failed: {err}")
            return 1
        print(out)

    # 2. Check if remote already exists
    if has_remote():
        rc, url, _ = run(["git", "remote", "get-url", "origin"])
        print(f"Remote already exists: {url}")
        print("Use push.py instead.")
        return 1

    # 3. Create repo on GitHub
    visibility = "--private" if args.private else "--public"
    cmd = ["gh", "repo", "create", args.name, visibility, "--source=.", "--remote=origin"]
    if args.description:
        cmd.extend(["--description", args.description])

    print(f"Creating GitHub repo '{args.name}'...")
    rc, out, err = run(cmd)
    if rc != 0:
        print(f"gh repo create failed: {err or out}")
        return 1
    print(out)

    # 4. Add all files and commit
    rc, _, _ = run(["git", "rev-parse", "HEAD"])
    if rc != 0:
        # No commits yet
        print("Adding files and creating initial commit...")
        run(["git", "add", "-A"])
        rc, out, err = run(["git", "commit", "-m", "Initial commit"])
        if rc != 0:
            print(f"git commit failed: {err}")
            return 1
        print(out)

    # 5. Push
    print("Pushing to GitHub...")
    rc, out, err = run(["git", "push", "-u", "origin", "HEAD"])
    if rc != 0:
        print(f"git push failed: {err}")
        return 1
    print(out or "Pushed successfully.")

    # Print final URL
    rc, url, _ = run(["gh", "repo", "view", "--json", "url", "-q", ".url"])
    if rc == 0:
        print(f"\nRepo: {url}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
