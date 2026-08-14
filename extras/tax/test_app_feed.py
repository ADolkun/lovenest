"""The estimator's gain feed carries Reportable Gain only (issue #78)."""
import io
import json
import unittest
import urllib.error
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
