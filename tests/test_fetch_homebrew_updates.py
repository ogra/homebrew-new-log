import importlib.util
import json
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

    def tearDown(self):
        self.tempdir.cleanup()

    def fake_get(self, url, params=None, headers=None, timeout=None):
        key = (url, tuple(sorted((params or {}).items())))
        try:
            payload = self.responses[key]
        except KeyError as exc:
            raise AssertionError(f"Unexpected request: {url} params={params}") from exc
        return FakeResponse(payload)

    def set_responses(self, responses):
        self.responses = {(url, tuple(sorted((params or {}).items()))): payload for url, params, payload in responses}

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

        with patch.object(self.module.requests, "get", side_effect=self.fake_get):
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
                (
                    self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-core"], "commits"),
                    {"sha": "master", "per_page": 100, "page": 1},
                    [{"sha": "commit-core-1"}, {"sha": "old-core"}],
                ),
                (
                    self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-core"], "commits/commit-core-1"),
                    None,
                    {
                        "commit": {"committer": {"date": "2026-03-07T00:00:00Z"}},
                        "files": [
                            {"filename": "Formula/fresh.rb", "status": "added"},
                            {"filename": "Formula/ignored.rb", "status": "modified"},
                            {"filename": "README.md", "status": "added"},
                        ],
                    },
                ),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-cask"], ""), None, {"default_branch": "master"}),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-cask"], "branches/master"), None, {"commit": {"sha": "new-cask"}}),
                (
                    self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-cask"], "commits"),
                    {"sha": "master", "per_page": 100, "page": 1},
                    [{"sha": "commit-cask-1"}, {"sha": "old-cask"}],
                ),
                (
                    self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-cask"], "commits/commit-cask-1"),
                    None,
                    {
                        "commit": {"committer": {"date": "2026-03-07T01:00:00Z"}},
                        "files": [
                            {"filename": "Casks/fancy.rb", "status": "added"},
                            {"filename": "Casks/skip.rb", "status": "renamed"},
                        ],
                    },
                ),
            ]
        )

        with patch.object(self.module.requests, "get", side_effect=self.fake_get):
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
                (
                    self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-core"], "commits"),
                    {"sha": "master", "per_page": 100, "page": 1},
                    [{"sha": "commit-core-1"}, {"sha": "old-core"}],
                ),
                (
                    self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-core"], "commits/commit-core-1"),
                    None,
                    {
                        "commit": {"committer": {"date": "2026-03-07T00:00:00Z"}},
                        "files": [{"filename": "Formula/duplicate.rb", "status": "added"}],
                    },
                ),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-cask"], ""), None, {"default_branch": "master"}),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-cask"], "branches/master"), None, {"commit": {"sha": "old-cask"}}),
            ]
        )

        with patch.object(self.module.requests, "get", side_effect=self.fake_get):
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
                (
                    self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-core"], "commits"),
                    {"sha": "master", "per_page": 100, "page": 1},
                    [{"sha": "commit-core-2"}, {"sha": "commit-core-1"}, {"sha": "old-core"}],
                ),
                (
                    self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-core"], "commits/commit-core-1"),
                    None,
                    {
                        "commit": {"committer": {"date": "2026-03-08T00:00:00Z"}},
                        "files": [{"filename": "Formula/myformula.rb", "status": "added"}],
                    },
                ),
                (
                    self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-core"], "commits/commit-core-2"),
                    None,
                    {
                        "commit": {"committer": {"date": "2026-03-08T01:00:00Z"}},
                        "files": [{"filename": "Formula/myformula.rb", "status": "added"}],
                    },
                ),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-cask"], ""), None, {"default_branch": "master"}),
                (self.module.github_url(self.module.UPSTREAM_REPOS["homebrew-cask"], "branches/master"), None, {"commit": {"sha": "old-cask"}}),
            ]
        )

        with patch.object(self.module.requests, "get", side_effect=self.fake_get):
            self.module.main()

        items = json.loads(self.module.DATA_FILE.read_text())
        self.assertEqual(len(items), 1, "Same-name formula across commits must be deduplicated")
        self.assertEqual(items[0]["name"], "myformula")

        log_files = list(self.module.LOG_DIR.iterdir())
        self.assertEqual(len(log_files), 1)
        log_text = log_files[0].read_text()
        self.assertEqual(log_text.count("### myformula"), 1, "Formula must appear only once in log")


if __name__ == "__main__":
    unittest.main()
