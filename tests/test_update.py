"""grovylib/update.py: the passive "is grovy up to date?" check against GitHub's tagged releases."""
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from grovylib import core, update  # noqa: E402


class TestVersionParsing(unittest.TestCase):
    def test_semver_accepts_v_prefix_and_orders_numerically(self):
        self.assertEqual(update._semver("v1.9.2"), (1, 9, 2))
        self.assertEqual(update._semver("1.9.2"), (1, 9, 2))
        # numeric, not lexical: v1.10.0 is newer than v1.9.2 (where string compare would say the opposite)
        self.assertGreater(update._semver("v1.10.0"), update._semver("v1.9.2"))

    def test_semver_rejects_non_versions(self):
        for bad in ("", None, "latest", "nightly", "v1", "v1.2"):
            self.assertIsNone(update._semver(bad))

    def test_check_repo_validates_owner_slash_repo(self):
        with mock.patch.dict(os.environ, {"GROVY_UPDATE_REPO": "me/fork"}):
            self.assertEqual(update.check_repo(), "me/fork")
        for bad in ("../../etc/passwd?x=1", "../evil", "owner/repo/extra", "me/repo?x=1", "has space/repo"):
            with mock.patch.dict(os.environ, {"GROVY_UPDATE_REPO": bad}):
                self.assertIsNone(update.check_repo(), bad)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROVY_UPDATE_REPO", None)
            self.assertEqual(update.check_repo(), update.DEFAULT_REPO)

    def test_check_enabled_opt_out(self):
        for off in ("off", "0", "no", "false", "OFF"):
            with mock.patch.dict(os.environ, {"GROVY_UPDATE_CHECK": off}):
                self.assertFalse(update.check_enabled())
        with mock.patch.dict(os.environ, {"GROVY_UPDATE_CHECK": "on"}):
            self.assertTrue(update.check_enabled())


class TestCheckAndStatus(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="grovy-upd-", dir="/tmp")
        self.cache = os.path.join(self.dir, "update.json")
        self._cp = mock.patch.object(update, "_cache_path", lambda: self.cache)
        self._cp.start()

    def tearDown(self):
        self._cp.stop()

    def test_check_writes_latest_and_status_reports_behind(self):
        with mock.patch.object(update, "fetch_latest", return_value="v2.0.0") as fetch, \
             mock.patch.object(update, "current_version", return_value="v1.9.2"), \
             mock.patch.dict(os.environ, {"GROVY_UPDATE_CHECK": "on", "GROVY_UPDATE_REPO": "Garfield1002/grovy"}):
            update.check_for_update()
            fetch.assert_called_once_with("Garfield1002/grovy")
            st = update.update_status()
        self.assertTrue(st["behind"])
        self.assertEqual(st["latest"], "v2.0.0")
        self.assertEqual(st["current"], "v1.9.2")
        self.assertEqual(st["url"], "https://github.com/Garfield1002/grovy/releases/tag/v2.0.0")

    def test_not_behind_when_current_is_newest(self):
        with mock.patch.object(update, "fetch_latest", return_value="v1.9.2"), \
             mock.patch.object(update, "current_version", return_value="v1.9.2"), \
             mock.patch.dict(os.environ, {"GROVY_UPDATE_CHECK": "on"}):
            update.check_for_update()
            st = update.update_status()
        self.assertFalse(st["behind"])

    def test_daily_cache_means_only_one_fetch(self):
        with mock.patch.object(update, "fetch_latest", return_value="v2.0.0") as fetch, \
             mock.patch.dict(os.environ, {"GROVY_UPDATE_CHECK": "on"}):
            update.check_for_update()
            update.check_for_update()           # within CHECK_EVERY: no second request
            self.assertEqual(fetch.call_count, 1)
            update.check_for_update(force=True)  # "Check now" always asks
            self.assertEqual(fetch.call_count, 2)

    def test_network_error_keeps_last_latest_and_records_why(self):
        with mock.patch.object(update, "fetch_latest", return_value="v2.0.0"), \
             mock.patch.dict(os.environ, {"GROVY_UPDATE_CHECK": "on"}):
            update.check_for_update()
        with mock.patch.object(update, "fetch_latest", side_effect=urllib.error.URLError("down")), \
             mock.patch.object(update, "current_version", return_value="v1.9.2"), \
             mock.patch.dict(os.environ, {"GROVY_UPDATE_CHECK": "on"}):
            update.check_for_update(force=True)
            st = update.update_status()
        self.assertEqual(st["latest"], "v2.0.0")        # the good value survives the failed retry
        self.assertTrue(st["behind"])

    def test_non_oserror_fetch_failure_is_caught_not_raised(self):
        import http.client
        # http.client.HTTPException is NOT an OSError: a truncated GitHub response must still not escape the check.
        with mock.patch.object(update, "fetch_latest", side_effect=http.client.IncompleteRead(b"")), \
             mock.patch.dict(os.environ, {"GROVY_UPDATE_CHECK": "on"}):
            update.check_for_update(force=True)   # must not raise
            st = update.update_status()
        self.assertTrue(st["error"])

    def test_corrupt_checked_in_cache_does_not_crash(self):
        core.atomic_write_json(self.cache, {"repo": "Garfield1002/grovy", "checked": "not-a-number", "latest": "v1.0.0"})
        with mock.patch.object(update, "fetch_latest", return_value="v1.0.0") as fetch, \
             mock.patch.dict(os.environ, {"GROVY_UPDATE_CHECK": "on"}):
            update.check_for_update()   # corrupt 'checked' must not raise; it just re-fetches
            fetch.assert_called_once()

    def test_opt_out_writes_nothing_and_status_is_disabled(self):
        with mock.patch.object(update, "fetch_latest") as fetch, \
             mock.patch.dict(os.environ, {"GROVY_UPDATE_CHECK": "off"}):
            update.check_for_update(force=True)
            fetch.assert_not_called()
            self.assertEqual(update.update_status(), {"enabled": False})
        self.assertFalse(os.path.exists(self.cache))

    def test_bad_repo_reports_error_without_fetching(self):
        with mock.patch.object(update, "fetch_latest") as fetch, \
             mock.patch.dict(os.environ, {"GROVY_UPDATE_CHECK": "on", "GROVY_UPDATE_REPO": "not a repo"}):
            update.check_for_update(force=True)
            fetch.assert_not_called()
            st = update.update_status()
        self.assertIsNone(st["repo"])
        self.assertTrue(st["error"])


if __name__ == "__main__":
    unittest.main()
