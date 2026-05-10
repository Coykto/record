---
description: Use when the user wants to file a GitHub issue against this project's repo (e.g. "/issue ...", "open an issue for X", "report a bug about Y"). Searches existing open + closed issues for duplicates before creating, and asks the user before filing a likely duplicate.
argument-hint: <issue description>
---

Create a new GitHub issue in this project's repository, but first check whether a similar issue (open or closed) already exists. If a clearly similar issue is found, ask the user whether to file the new one anyway.

The user-provided issue description is:

$ARGUMENTS

If `$ARGUMENTS` is empty, ask the user to provide an issue description and stop.

## Steps

### 1. Verify prerequisites

Run these checks. If any fail, report the exact remediation command and stop — do not try to work around missing auth.

- `gh --version` — `gh` CLI must be installed. If missing: tell the user to run `brew install gh`.
- `gh auth status` — must be authenticated. If not: tell the user to run `gh auth login`.
- `gh repo view --json nameWithOwner -q .nameWithOwner` — confirms the current directory's repo. Capture the result as `REPO` (e.g. `Coykto/record`). If this fails (not a repo, no remote), stop and report.

### 2. Derive a title and search keywords

From the user's description:

- **Title** — a concise one-line title (≤ 72 chars). Imperative voice for bugs/tasks ("Fix X", "Add Y"); descriptive for questions.
- **Search keywords** — 2–5 distinctive terms (nouns, error strings, component names) that capture what the issue is *about*. Strip stopwords and generic verbs. These go into the GitHub search query.

### 3. Search for similar issues (open AND closed)

Run:

```
gh issue list --repo "$REPO" --state all --search "<keywords>" --limit 10 --json number,title,state,url,body
```

If the search returns nothing, skip to step 5.

### 4. Judge similarity

For each returned issue, compare title + body against the user's description. Classify each as:

- **Very similar** — same underlying problem/request, even if worded differently or already closed.
- **Tangentially related** — overlaps on keywords but a different problem.
- **Unrelated** — keyword false positive.

If at least one issue is **very similar**, use the `AskUserQuestion` tool to ask the user how to proceed. Show the matched issue(s) clearly (number, state, title, URL) in the question text. Options:

1. **File anyway** — proceed to step 5 and create the new issue.
2. **Cancel** — do not create. Stop and report which existing issue to follow instead.
3. **Comment on existing instead** — if there is exactly one very similar issue, offer to add the user's description as a comment to it via `gh issue comment <number> --repo "$REPO" --body "<description>"`. Then stop.

If no issues are very similar (only tangential/unrelated matches), do **not** ask — proceed to step 5. Briefly mention in your final summary that tangential matches existed but were not duplicates.

### 5. Create the issue

```
gh issue create --repo "$REPO" --title "<title>" --body "<body>"
```

Pass the body via a heredoc to preserve formatting. The body should be the user's description verbatim, lightly cleaned up (preserve their wording — do not invent reproduction steps, severity, or context they did not provide).

Report the new issue's URL (printed by `gh`) back to the user.

## Notes

- Use `AskUserQuestion` for the duplicate-confirmation step — never plain prompts or numbered lists.
- Do not add labels, assignees, milestones, or templates unless the user's description explicitly asks for them.
- Do not edit the user's description for tone or "professionalism" — file what they said.
