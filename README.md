# homebrew-new-log

A GitHub Actions–powered tracker that archives newly added Homebrew Formulae and Casks.  
This repository automatically collects new additions from the Homebrew project every day and stores them in two formats:

- A cumulative JSON archive (`data/all_items.json`)
- Daily human‑readable Markdown logs (`logs/YYYY-MM-DD.md`)

This makes it possible to review Homebrew’s “New Formulae” and “New Casks” history at any time, even if you missed the one‑time output shown by `brew update`.

---

## How it works

### 1. Daily scheduled workflow

A GitHub Actions workflow runs once per day. It:

- Fetches recent commits from:
  - `Homebrew/homebrew-core` (Formulae)
  - `Homebrew/homebrew-cask` (Casks)
- Detects newly added `.rb` files
- Retrieves metadata (version, description, homepage) from the official Homebrew API
- Updates the cumulative JSON archive
- Generates a Markdown log for the day
- Commits the results back to this repository

All operations use the built‑in `GITHUB_TOKEN`, so no personal access token is required.

---

## Output files

### `data/all_items.json`

A single JSON file containing the full historical list of newly added Formulae and Casks.  
Each entry includes:

- Name
- Repository (`homebrew-core` or `homebrew-cask`)
- Commit SHA
- Added date
- File path

This file grows over time as new items are discovered.

---

### `logs/YYYY-MM-DD.md`

A daily Markdown report formatted like:

```markdown
# Homebrew updates 2026-03-01

## New Formulae

### foo

1.2.3
A fast and lightweight tool for X.
[Project Home](https://example.com/foo)

## New Casks

### bar

2.4.0
GUI application for Y.
[Project Home](https://example.com/bar)
```

Only items added on that specific day are included.

---

## Why this project exists

Homebrew shows “New Formulae” and “New Casks” only once during `brew update`, and the information disappears afterward.  
This repository preserves that data permanently, making it easy to:

- Track new software entering the Homebrew ecosystem
- Analyze trends over time
- Discover new tools you might have missed
- Build downstream tools or dashboards using the JSON archive

---

## Automation details

The workflow:

- Runs daily via cron
- Uses Python to fetch and process data
- Commits changes automatically
- Requires no secrets or manual maintenance

You can inspect or modify the workflow in `.github/workflows/update.yml`.

---

## License

MIT License.  
Homebrew metadata is sourced from the official Homebrew repositories and API.
