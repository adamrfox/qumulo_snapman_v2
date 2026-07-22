"""Tests for QumuloClient.request's retry behavior -- the network-error retry
budget was widened (and given a friendlier failure message) after a goal run
against a real cluster died on a DNS blip that outlasted the old ~3.5s
window. These pin the new behavior: more/slower network retries than 5xx
retries, and a readable message once the budget is actually exhausted.
"""

import unittest
from unittest.mock import MagicMock, patch

import httpx

from app.qumulo.client import MAX_RETRIES, ApiTimeout, QumuloClient


class RequestRetryTest(unittest.TestCase):
    def setUp(self):
        sleep_patcher = patch("app.qumulo.client.time.sleep", return_value=None)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def _client(self) -> QumuloClient:
        return QumuloClient("cluster.example.com", 8000, "token")

    def test_network_error_retries_then_succeeds(self):
        client = self._client()
        ok_response = MagicMock(status_code=200)
        ok_response.json.return_value = {"ok": True}
        side_effects = [httpx.ConnectError("dns fail")] * 3 + [ok_response]
        with patch.object(client._client, "request", side_effect=side_effects):
            result = client.request("GET", "/v1/foo")
        self.assertEqual(result, {"ok": True})

    def test_network_error_exhausts_retries_raises_friendly_connection_error(self):
        client = self._client()
        err = httpx.ConnectError("[Errno -3] Temporary failure in name resolution")
        with patch.object(client._client, "request", side_effect=err):
            with self.assertRaises(ConnectionError) as ctx:
                client.request("GET", "/v1/foo")
        self.assertIn("Lost connection to the cluster", str(ctx.exception))
        self.assertIn("Temporary failure in name resolution", str(ctx.exception))

    def test_timeout_raises_friendly_api_timeout(self):
        client = self._client()
        with patch.object(client._client, "request", side_effect=httpx.ReadTimeout("slow")):
            with self.assertRaises(ApiTimeout) as ctx:
                client.request("GET", "/v1/foo")
        self.assertIn("did not respond in time", str(ctx.exception))

    def test_5xx_retries_are_bounded_by_the_shorter_http_retry_budget(self):
        client = self._client()
        resp = MagicMock(status_code=503)
        resp.json.return_value = {"error_class": "internal_error", "description": "boom"}
        with patch.object(client._client, "request", return_value=resp) as mock_request:
            with self.assertRaises(Exception):
                client.request("GET", "/v1/foo")
        self.assertEqual(mock_request.call_count, MAX_RETRIES + 1)


if __name__ == "__main__":
    unittest.main()
