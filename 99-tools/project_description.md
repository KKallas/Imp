# Project Description

This is a robot battle arena where AI pilots fight AI hackers. The project uses AI agents to manage development — LLMs handling GitHub issues, solving tasks, and creating PRs.

## What Robot Arena Does

Robot Arena runs autonomous bot battles. No human pilots. No real-time control. Uploaded scripts fight it out while we watch.

## Why AI-Managed Development

- **Issues are tasks** → LLMs can parse structured tasks
- **Code is text** → LLMs can read and write it
- **PRs are diffs** → LLMs can create them
- **Reviews are checklists** → LLMs can verify them

## Tech Stack

- Python backend
- GitHub for issue tracking and PRs
- Claude CLI for agent execution
- gh CLI for GitHub interaction

## Repository Structure

The codebase lives at the configured REPO (default: KKallas/Robot-Arena). The 99-tools/ directory contains the automation agents that operate on this repo.
