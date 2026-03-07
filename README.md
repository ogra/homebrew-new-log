# homebrew-new-log

A GitHub Actions–powered tracker that archives newly added Homebrew Formulae and Casks.  
This repository automatically collects new additions from the Homebrew project every day and stores them as:

- A cumulative JSON archive (`data/all_items.json`)
- Per-repository upstream state (`data/state.json`)
- Daily human‑readable Markdown logs (`logs/YYYY-MM-DD.md`)

This makes it possible to review Homebrew’s “New Formulae” and “New Casks” history at any time, even if you missed the one‑time output shown by `brew update`.

---

## How it works

### 1. Daily scheduled workflow

A GitHub Actions workflow runs once per day. It:

- Loads the last processed upstream SHA for:
  - `Homebrew/homebrew-core` (Formulae)
  - `Homebrew/homebrew-cask` (Casks)
- Fetches the latest upstream SHA for each repository
- Walks git history since the previous SHA
- Detects newly added `.rb` files only
- Retrieves metadata (version, description, homepage) from the official Homebrew API when available
- Updates the cumulative JSON archive
- Updates the persisted upstream state
- Generates a Markdown log for the day when additions were found
- Commits the results back to this repository

All operations use the built‑in `GITHUB_TOKEN`, so no personal access token is required.

### First run behavior

If `data/state.json` does not exist yet, the workflow records the current upstream SHAs as a baseline and exits without importing everything as new. The archive only grows after later runs detect newly added files in git history.

---

## Output files

### `data/all_items.json`

A single JSON file containing the full historical list of newly added Formulae and Casks.  
Each entry includes:

- `type`
- `name`
- `version`
- `desc`
- `homepage`
- `date`
- `source_repo`
- `path`
- `commit_sha`

This file grows over time as new items are discovered.

---

### `data/state.json`

This tracked file stores the last processed upstream SHA for each Homebrew repository:

```json
{
  "homebrew-core": {
    "last_seen_sha": "40-hex-sha"
  },
  "homebrew-cask": {
    "last_seen_sha": "40-hex-sha"
  }
}
```

The workflow only advances this state after archive and log generation succeed, which keeps reruns retryable if a step fails midway through.

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
- Uses Python plus the GitHub and Homebrew APIs to fetch and process data
- Commits changes automatically
- Requires no secrets or manual maintenance

You can inspect or modify the workflow in `.github/workflows/update.yml`.

---

## License

MIT License.  
Homebrew additions are detected from the official Homebrew git history, and metadata is enriched from the official Homebrew API when available.
