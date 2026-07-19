import json
import threading
import unittest
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scripts.mock_connector_server import create_server


class MockConnectorServerTests(unittest.TestCase):
    def setUp(self):
        self.server = create_server("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def _request_json(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_health_endpoint_reports_available_connectors(self):
        payload = self._request_json("GET", "/health")
        self.assertEqual("ok", payload["status"])
        self.assertIn("grants-portal", payload["connectors"])

    def test_grants_portal_endpoint_returns_expected_shape(self):
        payload = self._request_json("POST", "/grants-portal", {"keyword": "education learning"})
        self.assertEqual(1, payload["data"]["hitCount"])
        self.assertEqual("Mock Education Innovation Grant", payload["data"]["oppHits"][0]["title"])

    def test_csr_network_endpoint_returns_expected_shape(self):
        query = urlencode({"q": "csr digital learning"})
        payload = self._request_json("GET", f"/csr-network?{query}")
        self.assertEqual("Mock CSR Digital Learning Fund", payload["results"][0]["title"])


if __name__ == "__main__":
    unittest.main()
