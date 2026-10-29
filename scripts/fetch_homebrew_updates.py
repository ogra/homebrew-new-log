#!/usr/bin/env python3

import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BREW_API_FORMULAE = "https://formulae.brew.sh/api/formula.json"
BREW_API_CASKS = "https://formulae.brew.sh/api/cask.json"
GITHUB_API_BASE = "https://api.github.com"
REQUEST_TIMEOUT = 30
REQUEST_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2
RETRY_STATUS_CODES = {500, 502, 503, 504}
COMMIT_MARKER = "@@commit "

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
    for attempt in range(REQUEST_RETRIES + 1):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            if response.status_code in RETRY_STATUS_CODES and attempt < REQUEST_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            status_code = getattr(exc.response, "status_code", None)
            if attempt >= REQUEST_RETRIES or (
                status_code is not None and status_code not in RETRY_STATUS_CODES
            ):
                raise
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))


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



def git_remote_url(repo_config):
    return f"https://github.com/{repo_config['owner']}/{repo_config['repo']}.git"


def run_git(git_dir, *args, retries=0):
    """Run a git command against a bare repository, retrying network failures."""
    command = ["git", "--git-dir", str(git_dir), *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    for attempt in range(retries + 1):
        try:
            return subprocess.run(
                command, check=True, capture_output=True, text=True, env=env
            ).stdout
        except subprocess.CalledProcessError as exc:
            if attempt >= retries:
                raise RuntimeError(
                    f"git {args[0]} failed: {exc.stderr.strip()}"
                ) from exc
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))


def fetch_commit_date(repo_config, commit_sha):
    commit_data = fetch_json(github_url(repo_config, f"commits/{commit_sha}"))
    date = ((commit_data.get("commit") or {}).get("committer") or {}).get("date")
    if not date:
        raise RuntimeError(
            f"GitHub API returned no commit date for {commit_sha} in "
            f"{repo_config['owner']}/{repo_config['repo']}"
        )
    return datetime.strptime(date, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def parse_added_files(log_output, repo_config):
    """Parse `git log --name-status` output into (sha, date, path) tuples."""
    added = []
    commit_sha = commit_date = None
    for line in log_output.splitlines():
        if line.startswith(COMMIT_MARKER):
            commit_sha, timestamp = line[len(COMMIT_MARKER):].split()
            commit_date = datetime.fromtimestamp(int(timestamp), timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        elif line and commit_sha:
            status, _, path = line.partition("\t")
            if status == "A" and is_relevant_added_file(repo_config, path):
                added.append((commit_sha, commit_date, path))
    return added


def list_files_at(git_dir, repo_config, commit_sha):
    output = run_git(
        git_dir, "ls-tree", "-r", "--name-only", commit_sha, "--", repo_config["directory"]
    )
    return {Path(line).stem for line in output.splitlines() if line.endswith(".rb")}


def list_added_files(repo_config, previous_sha, latest_sha):
    """List .rb files newly added after previous_sha, oldest commit first.

    History is read from a blobless, shallow clone so that no per-commit
    GitHub API requests (and no API rate limit) are involved. Names that
    already existed at previous_sha are skipped: upstream directory
    reorganizations show up as mass "added" files that are not new items.
    """
    since = fetch_commit_date(repo_config, previous_sha) - timedelta(days=1)
    with tempfile.TemporaryDirectory() as git_dir:
        run_git(git_dir, "init", "--bare", "--quiet")
        run_git(git_dir, "remote", "add", "origin", git_remote_url(repo_config))
        run_git(
            git_dir,
            "fetch",
            "--quiet",
            "--no-tags",
            "--filter=blob:none",
            f"--shallow-since={since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "origin",
            latest_sha,
            retries=REQUEST_RETRIES,
        )
        try:
            log_output = run_git(
                git_dir,
                "-c",
                "diff.renames=true",
                "log",
                "--reverse",
                "--diff-filter=A",
                "--name-status",
                f"--format={COMMIT_MARKER}%H %ct",
                f"{previous_sha}..{latest_sha}",
                "--",
                repo_config["directory"],
            )
        except RuntimeError as exc:
            raise RuntimeError(
                "Could not find previous SHA "
                f"{previous_sha} in {repo_config['owner']}/{repo_config['repo']} history; "
                "the upstream history may have been rewritten and the baseline may need "
                "to be reinitialized."
            ) from exc
        existing_names = list_files_at(git_dir, repo_config, previous_sha)
    return [
        added
        for added in parse_added_files(log_output, repo_config)
        if Path(added[2]).stem not in existing_names
    ]


def is_relevant_added_file(repo_config, filename):
    return filename.startswith(repo_config["directory"]) and filename.endswith(".rb")


def build_items_from_added_files(repo_name, repo_config, added_files):
    new_items = []
    seen_keys = set()

    for commit_sha, commit_date, path in added_files:
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

        added_files = list_added_files(repo_config, previous_sha, latest_sha)
        repo_items = build_items_from_added_files(repo_name, repo_config, added_files)

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
