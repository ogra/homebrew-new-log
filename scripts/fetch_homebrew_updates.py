#!/usr/bin/env python3

import requests
import json
from datetime import datetime, timezone
from pathlib import Path

BREW_API_FORMULAE = "https://formulae.brew.sh/api/formula.json"
BREW_API_CASKS = "https://formulae.brew.sh/api/cask.json"

DATA_FILE = Path("data/all_items.json")
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Homebrew JSON API helpers
# ---------------------------------------------------------------------------

def fetch_all_formulae():
    """Fetch the full list of formulae from the Homebrew JSON API."""
    resp = requests.get(BREW_API_FORMULAE)
    if resp.status_code != 200:
        print(f"Failed to fetch formulae list: {resp.status_code}")
        return []
    return resp.json()


def fetch_all_casks():
    """Fetch the full list of casks from the Homebrew JSON API."""
    resp = requests.get(BREW_API_CASKS)
    if resp.status_code != 200:
        print(f"Failed to fetch casks list: {resp.status_code}")
        return []
    return resp.json()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_all_items():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return []


def save_all_items(items):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(items, indent=2))


# ---------------------------------------------------------------------------
# Markdown log
# ---------------------------------------------------------------------------

def write_markdown_log(new_items):
    if not new_items:
        return None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = LOG_DIR / f"{today}.md"

    formulae = [i for i in new_items if i["type"] == "formula"]
    casks = [i for i in new_items if i["type"] == "cask"]

    lines = [f"# Homebrew updates {today}\n"]

    if formulae:
        lines.append("## New Formulae\n")
        for f in formulae:
            lines.append(f"### {f['name']}\n")
            lines.append(f"{f['version']}")
            lines.append(f"{f['desc']}")
            if f["homepage"]:
                lines.append(f"[Project Home]({f['homepage']})\n")

    if casks:
        lines.append("## New Casks\n")
        for c in casks:
            lines.append(f"### {c['name']}\n")
            lines.append(f"{c['version']}")
            lines.append(f"{c['desc']}")
            if c["homepage"]:
                lines.append(f"[Project Home]({c['homepage']})\n")

    log_path.write_text("\n".join(lines))
    return log_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    all_items = load_all_items()
    initial_run = len(all_items) == 0

    # Build a set of known names per type for fast lookup
    existing_formulae = {i["name"] for i in all_items if i.get("type") == "formula"}
    existing_casks = {i["name"] for i in all_items if i.get("type") == "cask"}

    # --- Migrate legacy entries that lack a "type" field -----------------------
    # Older data used "repo" instead of "type". Normalise on first encounter so
    # that the existing_* sets are populated correctly.
    migrated = False
    for item in all_items:
        if "type" not in item:
            migrated = True
            if "cask" in item.get("repo", ""):
                item["type"] = "cask"
            else:
                item["type"] = "formula"
            # Ensure the new required fields exist (best-effort from old data)
            item.setdefault("version", "")
            item.setdefault("desc", "")
            item.setdefault("homepage", "")

    if migrated:
        # Rebuild lookup sets after migration
        existing_formulae = {i["name"] for i in all_items if i.get("type") == "formula"}
        existing_casks = {i["name"] for i in all_items if i.get("type") == "cask"}
    # ---------------------------------------------------------------------------

    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_items = []

    # --- Formulae --------------------------------------------------------------
    all_formulae = fetch_all_formulae()

    if initial_run and all_formulae:
        # On the very first run, just record current names without treating
        # everything as "new".  This avoids a massive initial log.
        for f in all_formulae:
            name = f.get("name") or f.get("full_name", "")
            if not name:
                continue
            versions = f.get("versions") or {}
            entry = {
                "type": "formula",
                "name": name,
                "version": versions.get("stable", ""),
                "desc": f.get("desc") or "",
                "homepage": f.get("homepage") or "",
                "date": today,
            }
            all_items.append(entry)
        existing_formulae = {i["name"] for i in all_items if i["type"] == "formula"}
    else:
        for f in all_formulae:
            name = f.get("name") or f.get("full_name", "")
            if not name:
                continue
            if name in existing_formulae:
                continue
            versions = f.get("versions") or {}
            entry = {
                "type": "formula",
                "name": name,
                "version": versions.get("stable", ""),
                "desc": f.get("desc") or "",
                "homepage": f.get("homepage") or "",
                "date": today,
            }
            new_items.append(entry)
            all_items.append(entry)

    # --- Casks -----------------------------------------------------------------
    all_casks = fetch_all_casks()

    if initial_run and all_casks:
        for c in all_casks:
            name = c.get("token", "")
            if not name:
                continue
            entry = {
                "type": "cask",
                "name": name,
                "version": c.get("version") or "",
                "desc": c.get("desc") or "",
                "homepage": c.get("homepage") or "",
                "date": today,
            }
            all_items.append(entry)
        existing_casks = {i["name"] for i in all_items if i["type"] == "cask"}
    else:
        for c in all_casks:
            name = c.get("token", "")
            if not name:
                continue
            if name in existing_casks:
                continue
            entry = {
                "type": "cask",
                "name": name,
                "version": c.get("version") or "",
                "desc": c.get("desc") or "",
                "homepage": c.get("homepage") or "",
                "date": today,
            }
            new_items.append(entry)
            all_items.append(entry)

    # --- Save & Log ------------------------------------------------------------
    save_all_items(all_items)
    log_path = write_markdown_log(new_items)

    if log_path:
        print(f"New log written: {log_path}")
    else:
        print("No new items today.")


if __name__ == "__main__":
    main()
