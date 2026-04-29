# Fix PRs (Fix Phase) — Agent Prompt

You are fixing a pull request based on review feedback.

## Project Context

{{project_description}}

## Guidelines

{{tools_context}}

## PR #{{pr_number}}: {{pr_title}}

Branch: {{branch}}

## PR Diff (what this PR currently changes)

```diff
{{diff}}
```

## Review Comments to Address

{{comments_text}}

## Instructions

1. Read each comment and understand what change is requested
2. Look at the diff to understand what was already changed in this PR
3. Make the requested changes to the files
4. Be precise - only change what's requested, don't refactor unrelated code
5. After making changes, summarize what you fixed

## Rules

- Address EACH comment - don't skip any
- If a comment is unclear, make a reasonable interpretation and note it
- Keep changes minimal and focused
- The reviewer may be referring to code that was removed (lines with `-`) or added (lines with `+`) in the diff

Begin by reading the files mentioned in the comments, then make the fixes.
