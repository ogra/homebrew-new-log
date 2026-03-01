#!/usr/bin/env python3

import requests
import json
from datetime import datetime
from pathlib import Path

REPOS = [("Homebrew/homebrew-core", "Formula/"), ("Homebrew/homebrew-cask", "Casks/")]

DATA_FILE = Path("data/all_items.json")
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)


def fetch_new_items(repo, path_prefix):
    url = f"https://api.github.com/repos/{repo}/commits"
    params = {"path": path_prefix}
    commits = requests.get(url, params=params).json()

    results = []

    for commit in commits:
        sha = commit["sha"]
        detail = requests.get(
            f"https://api.github.com/repos/{repo}/commits/{sha}"
        ).json()

        for file in detail.get("files", []):
            if file["status"] == "added" and file["filename"].startswith(path_prefix):
                name = file["filename"].split("/")[-1].replace(".rb", "")
                results.append(
                    {
                        "repo": repo,
                        "name": name,
                        "path": file["filename"],
                        "commit": sha,
                        "date": commit["commit"]["author"]["date"],
                    }
                )

    return results


def fetch_formula_metadata(name, is_cask=False):
    base = "cask" if is_cask else "formula"
    url = f"https://formulae.brew.sh/api/{base}/{name}.json"
    r = requests.get(url)
    if r.status_code != 200:
        return None
    data = r.json()

    return {
        "version": data.get("version") or "",
        "desc": data.get("desc") or "",
        "homepage": data.get("homepage") or "",
    }


def load_all_items():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return []


def save_all_items(items):
    DATA_FILE.write_text(json.dumps(items, indent=2))


def write_markdown_log(new_items):
    if not new_items:
        return None

    today = datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    log_path = LOG_DIR / f"{today}.md"

    formulae = []
    casks = []

    for item in new_items:
        is_cask = "cask" in item["repo"]
        meta = fetch_formula_metadata(item["name"], is_cask=is_cask)

        entry = {
            "name": item["name"],
            "version": meta["version"] if meta else "",
            "desc": meta["desc"] if meta else "",
            "homepage": meta["homepage"] if meta else "",
        }

        if is_cask:
            casks.append(entry)
        else:
            formulae.append(entry)

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


def main():
    all_items = load_all_items()
    existing_keys = {(i["repo"], i["name"], i["commit"]) for i in all_items}

    new_items = []

    for repo, prefix in REPOS:
        for item in fetch_new_items(repo, prefix):
            key = (item["repo"], item["name"], item["commit"])
            if key not in existing_keys:
                new_items.append(item)
                all_items.append(item)

    save_all_items(all_items)
    log_path = write_markdown_log(new_items)

    if log_path:
        print(f"New log written: {log_path}")
    else:
        print("No new items today.")


if __name__ == "__main__":
    main()
