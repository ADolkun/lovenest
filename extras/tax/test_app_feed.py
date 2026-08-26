"""The estimator's gain feed carries Reportable Gain only (issue #78)."""
import io
import json
import unittest
import urllib.error
from unittest import mock

from fastapi import HTTPException

import app

AUTH = {"Authorization": "Bearer t"}


def _urlopen_returning(payload):
    return mock.Mock(return_value=io.BytesIO(json.dumps(payload).encode()))


class ReportableGainFeedTest(unittest.TestCase):
    def test_asks_the_gated_backend_endpoint_for_the_ytd_window(self):
        """The gate lives in the backend, so this app must not compute gains
        itself — it may only read the endpoint that applies the allowlist."""
        urlopen = _urlopen_returning({"reportable_gain": 1.0, "non_reportable_gain": 0.0})
        with mock.patch("urllib.request.urlopen", urlopen):
            app._reportable_gain(AUTH)
        url = urlopen.call_args.args[0].full_url
        self.assertIn("/api/assets/reportable-gain", url)
        self.assertIn(f"start={app.YTD_START}", url)

    def test_forwards_reportable_and_non_reportable_gain_separately(self):
        with mock.patch(
            "urllib.request.urlopen",
            _urlopen_returning({"reportable_gain": 1200.5, "non_reportable_gain": 900.0}),
        ):
            self.assertEqual(
                app._reportable_gain(AUTH),
                {"reportable_gain": 1200.5, "non_reportable_gain": 900.0},
            )

    def test_backend_failure_reports_instead_of_guessing_a_gain(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            result = app._reportable_gain(AUTH)
        self.assertNotIn("reportable_gain", result)
        self.assertIn("gain_error", result)


FEED = {
    "as_of": "2026-08-25", "tax_year": 2026,
    "trad_401k": 10.0, "roth_ira": 20.0, "hsa": 30.0, "taxable": 40.0,
    "roth_basis": 15.0,
    "annual_trad_401k": 1.0, "annual_roth": 2.0, "annual_hsa": 3.0, "annual_taxable": 4.0,
    "annual_is_year_to_date": True,
    "excluded": {"other": 5.0, "ungrouped": 6.0},
    "live": ["trad_401k", "roth_ira", "hsa", "taxable", "roth_basis"],
}


def _urlopen_status(status):
    """urlopen used as a context manager, as require_auth does."""
    response = mock.MagicMock()
    response.__enter__.return_value = mock.Mock(status=status)
    return mock.Mock(return_value=response)


class ProjectionFeedTest(unittest.TestCase):
    def test_asks_the_backend_projection_endpoint(self):
        urlopen = _urlopen_returning(FEED)
        with mock.patch("urllib.request.urlopen", urlopen):
            app._projection_feed(AUTH)
        self.assertIn("/api/assets/projection-feed", urlopen.call_args.args[0].full_url)

    def test_forwards_the_engine_key_fields_unchanged(self):
        """The keys are the engine's input names, so any renaming here would
        silently drop a balance from the projection."""
        with mock.patch("urllib.request.urlopen", _urlopen_returning(FEED)):
            result = app._projection_feed(AUTH)
        for key in ("trad_401k", "roth_ira", "hsa", "taxable", "roth_basis",
                    "annual_trad_401k", "annual_roth", "annual_hsa", "annual_taxable"):
            self.assertEqual(result[key], FEED[key])
        self.assertTrue(result["annual_is_year_to_date"])
        self.assertEqual(result["excluded"], {"other": 5.0, "ungrouped": 6.0})

    def test_backend_failure_omits_the_balances_instead_of_zeroing_them(self):
        """A zero balance is an answer the projection would act on; absence is
        the only honest report of a feed that never arrived."""
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            result = app._projection_feed(AUTH)
        self.assertIn("projection_error", result)
        for key in ("trad_401k", "roth_ira", "hsa", "taxable", "roth_basis",
                    "annual_trad_401k", "annual_roth", "annual_hsa", "annual_taxable"):
            self.assertNotIn(key, result)


class WorkspaceGateTest(unittest.TestCase):
    """Config constants are read at import time, so patch the module attrs."""

    def _auth(self, workspace_id):
        with mock.patch("urllib.request.urlopen", _urlopen_status(200)):
            return app.require_auth(authorization="Bearer t", workspace_id=workspace_id)

    def test_accepts_either_configured_workspace(self):
        with mock.patch.object(app, "HUB_WORKSPACE_ID", "ws-hub"), \
             mock.patch.object(app, "INVESTMENT_WORKSPACE_ID", "ws-invest"):
            self.assertEqual(self._auth("ws-hub")["X-Workspace-Id"], "ws-hub")
            self.assertEqual(self._auth("ws-invest")["X-Workspace-Id"], "ws-invest")

    def test_rejects_a_workspace_that_is_configured_for_neither(self):
        with mock.patch.object(app, "HUB_WORKSPACE_ID", "ws-hub"), \
             mock.patch.object(app, "INVESTMENT_WORKSPACE_ID", "ws-invest"):
            with self.assertRaises(HTTPException) as caught:
                self._auth("ws-other")
        self.assertEqual(caught.exception.status_code, 403)

    def test_unconfigured_sidecar_skips_the_check_and_probes_the_user(self):
        urlopen = _urlopen_status(200)
        with mock.patch.object(app, "HUB_WORKSPACE_ID", ""), \
             mock.patch.object(app, "INVESTMENT_WORKSPACE_ID", ""), \
             mock.patch("urllib.request.urlopen", urlopen):
            app.require_auth(authorization="Bearer t", workspace_id="anything")
        self.assertIn("/api/users/me", urlopen.call_args.args[0].full_url)


class WorkspaceClauseTest(unittest.TestCase):
    def test_scopes_to_the_caller_workspace_not_the_configured_one(self):
        """Membership was only verified for the workspace in the request, so
        the ledger queries must not read from any other."""
        with mock.patch.object(app, "HUB_WORKSPACE_ID", "ws-hub"), \
             mock.patch.object(app, "INVESTMENT_WORKSPACE_ID", "ws-invest"):
            sql, params = app._workspace_clause("t", "ws-invest")
        self.assertIn("t.workspace_id = %(ws)s", sql)
        self.assertEqual(params, {"ws": "ws-invest"})

    def test_a_missing_workspace_matches_nothing(self):
        sql, params = app._workspace_clause("t", "")
        self.assertIn("t.workspace_id = %(ws)s", sql)
        self.assertEqual(params, {"ws": ""})


if __name__ == "__main__":
    unittest.main()
