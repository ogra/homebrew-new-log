#!/usr/bin/env python3

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

BREW_API_FORMULAE = "https://formulae.brew.sh/api/formula.json"
BREW_API_CASKS = "https://formulae.brew.sh/api/cask.json"
GITHUB_API_BASE = "https://api.github.com"
REQUEST_TIMEOUT = 30

DATA_FILE = Path("data/all_items.json")
STATE_FILE = Path("data/state.json")
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

UPSTREAM_REPOS = {
    "homebrew-core": {
        "owner": "Homebrew",
        "repo": "homebrew-core",
        "type": "formula",
        "directory": "Formula/",
    },
    "homebrew-cask": {
        "owner": "Homebrew",
        "repo": "homebrew-cask",
        "type": "cask",
        "directory": "Casks/",
    },
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def fetch_json(url, *, params=None):
    headers = {"User-Agent": "homebrew-new-log"}
    if url.startswith(GITHUB_API_BASE):
        headers["Accept"] = "application/vnd.github+json"
        github_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if github_token:
            headers["Authorization"] = f"Bearer {github_token}"
    response = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Homebrew JSON API helpers
# ---------------------------------------------------------------------------


def fetch_all_formulae():
    """Fetch the full list of formulae from the Homebrew JSON API."""
    try:
        return fetch_json(BREW_API_FORMULAE)
    except requests.RequestException as exc:
        print(f"Failed to fetch formulae list: {exc}")
        return []



def fetch_all_casks():
    """Fetch the full list of casks from the Homebrew JSON API."""
    try:
        return fetch_json(BREW_API_CASKS)
    except requests.RequestException as exc:
        print(f"Failed to fetch casks list: {exc}")
        return []



def build_metadata_lookup(items, item_type):
    lookup = {}
    if item_type == "formula":
        for item in items:
            name = item.get("name") or item.get("full_name")
            if name:
                lookup[name] = {
                    "version": (item.get("versions") or {}).get("stable", ""),
                    "desc": item.get("desc") or "",
                    "homepage": item.get("homepage") or "",
                }
    else:
        for item in items:
            token = item.get("token")
            if token:
                lookup[token] = {
                    "version": item.get("version") or "",
                    "desc": item.get("desc") or "",
                    "homepage": item.get("homepage") or "",
                }
    return lookup


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------


def github_url(repo_config, path):
    base_url = f"{GITHUB_API_BASE}/repos/{repo_config['owner']}/{repo_config['repo']}"
    if not path:
        return base_url
    return f"{base_url}/{path}"



def fetch_default_branch(repo_config):
    repo_data = fetch_json(github_url(repo_config, ""))
    default_branch = repo_data.get("default_branch")
    if not default_branch:
        raise RuntimeError(
            "GitHub API returned no default branch for "
            f"{repo_config['owner']}/{repo_config['repo']}"
        )
    return default_branch



def fetch_latest_sha(repo_config):
    default_branch = fetch_default_branch(repo_config)
    branch_data = fetch_json(github_url(repo_config, f"branches/{default_branch}"))
    latest_sha = (branch_data.get("commit") or {}).get("sha")
    if not latest_sha:
        raise RuntimeError(
            "GitHub API returned no commit SHA for branch "
            f"{default_branch} in {repo_config['owner']}/{repo_config['repo']}"
        )
    return latest_sha, default_branch



def list_commits_since(repo_config, previous_sha, default_branch):
    commits = []
    page = 1

    while True:
        batch = fetch_json(
            github_url(repo_config, "commits"),
            params={"sha": default_branch, "per_page": 100, "page": page},
        )
        if not batch:
            break

        for commit in batch:
            if commit.get("sha") == previous_sha:
                commits.reverse()
                return commits
            commits.append(commit)

        page += 1

    raise RuntimeError(
        "Could not find previous SHA "
        f"{previous_sha} in {repo_config['owner']}/{repo_config['repo']} history; "
        "the upstream history may have been rewritten and the baseline may need "
        "to be reinitialized."
    )



def fetch_commit_details(repo_config, commit_sha):
    return fetch_json(github_url(repo_config, f"commits/{commit_sha}"))



def is_relevant_added_file(repo_config, file_info):
    filename = file_info.get("filename", "")
    return (
        file_info.get("status") == "added"
        and filename.startswith(repo_config["directory"])
        and filename.endswith(".rb")
    )



def build_items_from_commits(repo_name, repo_config, commits):
    new_items = []
    seen_keys = set()

    for commit in commits:
        commit_sha = commit.get("sha", "")
        if not commit_sha:
            continue

        commit_details = fetch_commit_details(repo_config, commit_sha)
        commit_date = (
            ((commit_details.get("commit") or {}).get("committer") or {}).get("date")
            or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )

        for file_info in commit_details.get("files") or []:
            if not is_relevant_added_file(repo_config, file_info):
                continue

            path = file_info["filename"]
            event_key = (repo_config["type"], path, commit_sha)
            if event_key in seen_keys:
                continue
            seen_keys.add(event_key)

            new_items.append(
                {
                    "type": repo_config["type"],
                    "name": Path(path).stem,
                    "version": "",
                    "desc": "",
                    "homepage": "",
                    "date": commit_date,
                    "source_repo": repo_name,
                    "path": path,
                    "commit_sha": commit_sha,
                }
            )

    return new_items


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def load_json_file(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Malformed JSON in {path}: {exc}") from exc



def load_all_items():
    items = load_json_file(DATA_FILE, [])
    if not isinstance(items, list):
        raise SystemExit(f"Malformed archive file: {DATA_FILE}")

    migrated = False
    for item in items:
        if not isinstance(item, dict):
            raise SystemExit(f"Malformed archive entry in {DATA_FILE}")
        if "type" not in item:
            migrated = True
            if "cask" in item.get("repo", ""):
                item["type"] = "cask"
            else:
                item["type"] = "formula"
        item.setdefault("version", "")
        item.setdefault("desc", "")
        item.setdefault("homepage", "")

    return items, migrated



def save_all_items(items):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(items, indent=2) + "\n")



def load_state():
    state = load_json_file(STATE_FILE, {})
    if not isinstance(state, dict):
        raise SystemExit(f"Malformed state file: {STATE_FILE}")

    validated_state = {}
    for repo_name, repo_state in state.items():
        if not isinstance(repo_state, dict):
            raise SystemExit(f"Malformed state entry for {repo_name}")
        last_seen_sha = repo_state.get("last_seen_sha")
        if last_seen_sha is not None and not isinstance(last_seen_sha, str):
            raise SystemExit(f"Malformed last_seen_sha for {repo_name}")
        validated_state[repo_name] = {}
        if last_seen_sha:
            validated_state[repo_name]["last_seen_sha"] = last_seen_sha

    return validated_state



def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")



def build_existing_event_keys(items):
    keys = set()
    for item in items:
        commit_sha = item.get("commit_sha")
        path = item.get("path")
        item_type = item.get("type")
        if commit_sha and path and item_type:
            keys.add((item_type, path, commit_sha))
    return keys


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
        for formula in formulae:
            lines.append(f"### {formula['name']}\n")
            lines.append(formula.get("version", ""))
            lines.append(formula.get("desc", ""))
            if formula.get("homepage"):
                lines.append(f"[Project Home]({formula['homepage']})\n")

    if casks:
        lines.append("## New Casks\n")
        for cask in casks:
            lines.append(f"### {cask['name']}\n")
            lines.append(cask.get("version", ""))
            lines.append(cask.get("desc", ""))
            if cask.get("homepage"):
                lines.append(f"[Project Home]({cask['homepage']})\n")

    log_path.write_text("\n".join(lines))
    return log_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    all_items, migrated = load_all_items()
    state = load_state()
    pending_state = dict(state)
    existing_event_keys = build_existing_event_keys(all_items)

    baseline_initialized = []
    detected_items = []

    for repo_name, repo_config in UPSTREAM_REPOS.items():
        latest_sha, default_branch = fetch_latest_sha(repo_config)
        repo_state = dict(state.get(repo_name, {}))
        previous_sha = repo_state.get("last_seen_sha")

        if not previous_sha:
            baseline_initialized.append(repo_name)
            pending_state[repo_name] = {"last_seen_sha": latest_sha}
            print(f"Initialized {repo_name} baseline at {latest_sha}")
            continue

        if previous_sha == latest_sha:
            pending_state[repo_name] = {"last_seen_sha": latest_sha}
            print(f"No upstream changes for {repo_name}")
            continue

        commits = list_commits_since(repo_config, previous_sha, default_branch)
        repo_items = build_items_from_commits(repo_name, repo_config, commits)

        new_repo_items = []
        for item in repo_items:
            event_key = (item["type"], item["path"], item["commit_sha"])
            if event_key in existing_event_keys:
                continue
            existing_event_keys.add(event_key)
            new_repo_items.append(item)

        detected_items.extend(new_repo_items)
        pending_state[repo_name] = {"last_seen_sha": latest_sha}
        print(f"Detected {len(new_repo_items)} new {repo_config['type']}(s) in {repo_name}")

    # Deduplicate by (type, name): if the same formula/cask appears in multiple
    # commits (e.g. an initial add followed by a fix commit), keep only the first.
    seen_names: set = set()
    deduped_items = []
    for item in detected_items:
        name_key = (item["type"], item["name"])
        if name_key not in seen_names:
            seen_names.add(name_key)
            deduped_items.append(item)
    detected_items = deduped_items

    if detected_items:
        metadata = {}
        if any(item["type"] == "formula" for item in detected_items):
            metadata["formula"] = build_metadata_lookup(fetch_all_formulae(), "formula")
        if any(item["type"] == "cask" for item in detected_items):
            metadata["cask"] = build_metadata_lookup(fetch_all_casks(), "cask")

        for item in detected_items:
            item.update(metadata.get(item["type"], {}).get(item["name"], {}))
            all_items.append(item)

    if migrated or detected_items:
        save_all_items(all_items)

    log_path = write_markdown_log(detected_items)
    save_state(pending_state)

    if baseline_initialized and not detected_items:
        print("Baseline initialization completed; no daily log generated.")
    elif log_path:
        print(f"New log written: {log_path}")
    else:
        print("No new items today.")


if __name__ == "__main__":
    main()
