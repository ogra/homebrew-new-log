import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "fetch_homebrew_updates.py"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FetchHomebrewUpdatesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("fetch_homebrew_updates", SCRIPT_PATH)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.module.DATA_FILE = root / "data" / "all_items.json"
        self.module.STATE_FILE = root / "data" / "state.json"
        self.module.LOG_DIR = root / "logs"
        self.module.LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.added_files = {}

    def tearDown(self):
        self.tempdir.cleanup()

    def fake_get(self, url, params=None, headers=None, timeout=None):
        key = (url, tuple(sorted((params or {}).items())))
        try:
            payload = self.responses[key]
        except KeyError as exc:
            raise AssertionError(f"Unexpected request: {url} params={params}") from exc
        return FakeResponse(payload)

    def fake_added_files(self, repo_config, previous_sha, latest_sha):
        return self.added_files.get(repo_config["repo"], [])

    def set_responses(self, responses):
        self.responses = {(url, tuple(sorted((params or {}).items()))): payload for url, params, payload in responses}

    def test_fetch_json_retries_transient_http_status(self):
        responses = [FakeResponse({}, status_code=504), FakeResponse({"ok": True})]
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append((url, params, headers, timeout))
            return responses.pop(0)

        with (
            patch.object(self.module.requests, "get", side_effect=fake_get),
            patch.object(self.module.time, "sleep") as sleep,
        ):
            payload = self.module.fetch_json("https://api.github.com/example")

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(self.module.RETRY_BACKOFF_SECONDS)

    def test_first_run_initializes_state_without_log(self):
        self.set_responses(
            [
                (self.module.BREW_API_FORMULAE, None, []),
                (self.module.BREW_API_CASKS, None, []),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-core"], ""), None, {"default_branch": "master"}),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-core"], "branches/master"), None, {"commit": {"sha": "core-head"}}),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-cask"], ""), None, {"default_branch": "master"}),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-cask"], "branches/master"), None, {"commit": {"sha": "cask-head"}}),
            ]
        )

        with (
            patch.object(self.module.requests, "get", side_effect=self.fake_get),
            patch.object(self.module, "list_added_files", side_effect=self.fake_added_files),
        ):
            self.module.main()

        state = json.loads(self.module.STATE_FILE.read_text())
        self.assertEqual(
            state,
            {
                "homebrew-core": {"last_seen_sha": "core-head"},
                "homebrew-cask": {"last_seen_sha": "cask-head"},
            },
        )
        self.assertFalse(self.module.DATA_FILE.exists())
        self.assertEqual(list(self.module.LOG_DIR.iterdir()), [])

    def test_detects_only_added_rb_files_and_enriches_when_available(self):
        self.module.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.module.STATE_FILE.write_text(
            json.dumps(
                {
                    "homebrew-core": {"last_seen_sha": "old-core"},
                    "homebrew-cask": {"last_seen_sha": "old-cask"},
                }
            )
        )

        self.set_responses(
            [
                (
                    self.module.BREW_API_FORMULAE,
                    None,
                    [
                        {
                            "name": "fresh",
                            "versions": {"stable": "1.2.3"},
                            "desc": "Fresh formula",
                            "homepage": "https://example.com/fresh",
                        }
                    ],
                ),
                (self.module.BREW_API_CASKS, None, []),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-core"], ""), None, {"default_branch": "master"}),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-core"], "branches/master"), None, {"commit": {"sha": "new-core"}}),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-cask"], ""), None, {"default_branch": "master"}),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-cask"], "branches/master"), None, {"commit": {"sha": "new-cask"}}),
            ]
        )

        self.added_files = {
            "homebrew-core": [("commit-core-1", "2026-03-07T00:00:00Z", "Formula/fresh.rb")],
            "homebrew-cask": [("commit-cask-1", "2026-03-07T01:00:00Z", "Casks/fancy.rb")],
        }

        with (
            patch.object(self.module.requests, "get", side_effect=self.fake_get),
            patch.object(self.module, "list_added_files", side_effect=self.fake_added_files),
        ):
            self.module.main()

        items = json.loads(self.module.DATA_FILE.read_text())
        self.assertEqual(len(items), 2)

        formula = next(item for item in items if item["type"] == "formula")
        self.assertEqual(formula["name"], "fresh")
        self.assertEqual(formula["version"], "1.2.3")
        self.assertEqual(formula["source_repo"], "homebrew-core")
        self.assertEqual(formula["path"], "Formula/fresh.rb")
        self.assertEqual(formula["commit_sha"], "commit-core-1")

        cask = next(item for item in items if item["type"] == "cask")
        self.assertEqual(cask["name"], "fancy")
        self.assertEqual(cask["version"], "")
        self.assertEqual(cask["desc"], "")
        self.assertEqual(cask["homepage"], "")
        self.assertEqual(cask["source_repo"], "homebrew-cask")

        log_files = list(self.module.LOG_DIR.iterdir())
        self.assertEqual(len(log_files), 1)
        log_text = log_files[0].read_text()
        self.assertIn("## New Formulae", log_text)
        self.assertIn("## New Casks", log_text)
        self.assertIn("fresh", log_text)
        self.assertIn("fancy", log_text)

    def test_event_deduplication_prevents_duplicate_archive_entries(self):
        self.module.DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.module.DATA_FILE.write_text(
            json.dumps(
                [
                    {
                        "type": "formula",
                        "name": "duplicate",
                        "version": "",
                        "desc": "",
                        "homepage": "",
                        "date": "2026-03-06T00:00:00Z",
                        "source_repo": "homebrew-core",
                        "path": "Formula/duplicate.rb",
                        "commit_sha": "commit-core-1",
                    }
                ]
            )
        )
        self.module.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.module.STATE_FILE.write_text(
            json.dumps(
                {
                    "homebrew-core": {"last_seen_sha": "old-core"},
                    "homebrew-cask": {"last_seen_sha": "old-cask"},
                }
            )
        )

        self.set_responses(
            [
                (self.module.BREW_API_FORMULAE, None, []),
                (self.module.BREW_API_CASKS, None, []),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-core"], ""), None, {"default_branch": "master"}),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-core"], "branches/master"), None, {"commit": {"sha": "new-core"}}),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-cask"], ""), None, {"default_branch": "master"}),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-cask"], "branches/master"), None, {"commit": {"sha": "old-cask"}}),
            ]
        )

        self.added_files = {
            "homebrew-core": [("commit-core-1", "2026-03-07T00:00:00Z", "Formula/duplicate.rb")],
        }

        with (
            patch.object(self.module.requests, "get", side_effect=self.fake_get),
            patch.object(self.module, "list_added_files", side_effect=self.fake_added_files),
        ):
            self.module.main()

        items = json.loads(self.module.DATA_FILE.read_text())
        self.assertEqual(len(items), 1)
        self.assertEqual(list(self.module.LOG_DIR.iterdir()), [])
        state = json.loads(self.module.STATE_FILE.read_text())
        self.assertEqual(state["homebrew-core"]["last_seen_sha"], "new-core")

    def test_same_name_across_commits_deduplicates_to_one_entry(self):
        """A formula added in two separate commits should only appear once."""
        self.module.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.module.STATE_FILE.write_text(
            json.dumps(
                {
                    "homebrew-core": {"last_seen_sha": "old-core"},
                    "homebrew-cask": {"last_seen_sha": "old-cask"},
                }
            )
        )

        self.set_responses(
            [
                (
                    self.module.BREW_API_FORMULAE,
                    None,
                    [
                        {
                            "name": "myformula",
                            "versions": {"stable": "1.0.0"},
                            "desc": "A formula",
                            "homepage": "https://example.com/myformula",
                        }
                    ],
                ),
                (self.module.BREW_API_CASKS, None, []),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-core"], ""), None, {"default_branch": "master"}),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-core"], "branches/master"), None, {"commit": {"sha": "new-core"}}),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-cask"], ""), None, {"default_branch": "master"}),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-cask"], "branches/master"), None, {"commit": {"sha": "old-cask"}}),
            ]
        )

        self.added_files = {
            "homebrew-core": [
                ("commit-core-1", "2026-03-08T00:00:00Z", "Formula/myformula.rb"),
                ("commit-core-2", "2026-03-08T01:00:00Z", "Formula/myformula.rb"),
            ],
        }

        with (
            patch.object(self.module.requests, "get", side_effect=self.fake_get),
            patch.object(self.module, "list_added_files", side_effect=self.fake_added_files),
        ):
            self.module.main()

        items = json.loads(self.module.DATA_FILE.read_text())
        self.assertEqual(len(items), 1, "Same-name formula across commits must be deduplicated")
        self.assertEqual(items[0]["name"], "myformula")

        log_files = list(self.module.LOG_DIR.iterdir())
        self.assertEqual(len(log_files), 1)
        log_text = log_files[0].read_text()
        self.assertEqual(log_text.count("### myformula"), 1, "Formula must appear only once in log")

    def test_parse_added_files_keeps_only_added_rb_files_in_directory(self):
        repo_config = self.module.UPSTREAM_REPOS["homebrew-core"]
        log_output = "\n".join(
            [
                "@@commit abc 1772841600",
                "",
                "A\tFormula/fresh.rb",
                "A\tREADME.md",
                "A\tFormula/notes.txt",
                "@@commit def 1772845200",
                "",
                "A\tFormula/f/foo.rb",
            ]
        )

        added = self.module.parse_added_files(log_output, repo_config)

        self.assertEqual(
            added,
            [
                ("abc", "2026-03-07T00:00:00Z", "Formula/fresh.rb"),
                ("def", "2026-03-07T01:00:00Z", "Formula/f/foo.rb"),
            ],
        )

    def test_list_added_files_reads_real_git_history(self):
        upstream = Path(self.tempdir.name) / "upstream"
        upstream.mkdir()
        identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]

        def git(*args, env=None):
            return subprocess.run(
                ["git", "-C", str(upstream), *identity, *args],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            ).stdout.strip()

        def commit(message, date):
            git("add", "-A")
            stamp = f"{date}T00:00:00Z"
            git("commit", "-q", "-m", message, env={**os.environ, "GIT_COMMITTER_DATE": stamp})
            return git("rev-parse", "HEAD")

        git("init", "-q")
        git("config", "uploadpack.allowFilter", "true")
        git("config", "uploadpack.allowAnySHA1InWant", "true")
        (upstream / "Formula").mkdir()
        (upstream / "Formula" / "old.rb").write_text("old\n")
        base = commit("base", "2026-03-01")
        (upstream / "Formula" / "fresh.rb").write_text("fresh\n")
        (upstream / "README.md").write_text("readme\n")
        first = commit("add fresh", "2026-03-02")
        (upstream / "Formula" / "old.rb").write_text("changed\n")
        (upstream / "Formula" / "other.rb").write_text("other\n")
        second = commit("modify and add", "2026-03-03")

        repo_config = self.module.UPSTREAM_REPOS["homebrew-core"]
        base_date = self.module.datetime(2026, 3, 1, tzinfo=self.module.timezone.utc)
        with (
            patch.object(self.module, "git_remote_url", return_value=str(upstream)),
            patch.object(self.module, "fetch_commit_date", return_value=base_date),
        ):
            added = self.module.list_added_files(repo_config, base, second)

        self.assertEqual(
            added,
            [
                (first, "2026-03-02T00:00:00Z", "Formula/fresh.rb"),
                (second, "2026-03-03T00:00:00Z", "Formula/other.rb"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
