# Fix PRs (Analysis Phase) — Agent Prompt

You are analyzing review feedback on a pull request to determine if it's actionable.

## Project Context

{{project_description}}

## Guidelines

{{tools_context}}

## PR #{{pr_number}}: {{pr_title}}

Branch: {{branch}}

## PR Diff (what this PR changes)

```diff
{{diff}}
```

## Review Comments

{{comments_text}}

## Your Task

Analyze each comment and determine:
1. Is the feedback specific enough to act on?
2. Are target files/locations clear (either explicitly stated or inferable from context)?
3. Is the requested change clear?

Use the diff above to understand what code was added/removed. Lines starting with `-` were removed, lines starting with `+` were added.

## Response Format

You MUST respond with EXACTLY ONE of these two formats:

**If actionable:**
```
ACTIONABLE: YES
SUMMARY: [1-2 sentence summary of what needs to be done]
```

**If needs clarification:**
```
ACTIONABLE: NO
QUESTIONS:
- [Specific question 1]
- [Specific question 2]
```

## Rules

- Be generous - if you can reasonably infer what's needed, it's actionable
- Only say NO if the feedback is genuinely unclear or ambiguous
- Questions should be specific, not generic

Analyze the feedback now.
