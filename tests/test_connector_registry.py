import json
import os
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from funding_bot import (
    CONNECTOR_CONFIG_ENV_VAR,
    ConnectorConfigError,
    ConnectorRegistry,
    FundingBot,
    GrantsPortalConnector,
)


class SandboxConnector(GrantsPortalConnector):
    connector_slug = "sandbox-portal"
    source_name = "Sandbox Registry Connector"


class _SandboxHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "payload": payload,
                "headers": {
                    "api_key": self.headers.get("X-Connector-Api-Key"),
                    "tenant": self.headers.get("X-Connector-Tenant"),
                },
            }
        )
        if (
            self.headers.get("X-Connector-Api-Key") != "sandbox-key"
            or self.headers.get("X-Connector-Tenant") != "tenant-42"
        ):
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"opportunities": []}')
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        body = {
            "schema_version": 2,
            "opportunities": [
                {
                    "source": "Sandbox Registry Connector",
                    "donor_name": "Sandbox Foundation",
                    "title": "Sandbox Education Grant",
                    "portal_url": "https://sandbox.example/opportunities/education",
                    "summary": "Education funding served from a mock connector endpoint.",
                    "category": "Education",
                    "tags": ["education", "sandbox"],
                }
            ],
        }
        self.wfile.write(json.dumps(body).encode("utf-8"))

    def log_message(self, format, *args):
        return


class _SandboxServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _SandboxHandler)
        self.httpd.requests = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/sandbox"

    @property
    def requests(self):
        return list(self.httpd.requests)  # type: ignore[attr-defined]

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()


class ConnectorRegistryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.registry = ConnectorRegistry()
        self.registry.register(
            SandboxConnector.connector_slug,
            SandboxConnector,
            credential_schema={
                "type": "object",
                "properties": {
                    "api_key": {"type": "string", "minLength": 1},
                    "tenant": {"type": "string", "minLength": 1},
                },
                "required": ["api_key", "tenant"],
                "additionalProperties": True,
            },
        )
        self.db_path = Path(".test_connector_registry.db")
        if self.db_path.exists():
            self.db_path.unlink()
        self.server = _SandboxServer()
        self.server.start()

    def tearDown(self):
        self.server.stop()
        if self.db_path.exists():
            self.db_path.unlink()
        os.environ.pop(CONNECTOR_CONFIG_ENV_VAR, None)
        os.environ.pop("SANDBOX_SECRET", None)

    def test_registry_discovery_lists_custom_connector(self):
        self.assertEqual(["sandbox-portal"], self.registry.discover())

    def test_startup_validation_reports_invalid_connector_credentials(self):
        os.environ[CONNECTOR_CONFIG_ENV_VAR] = json.dumps(
            {
                "connectors": [
                    {
                        "type": "sandbox-portal",
                        "transport": "http",
                        "base_url": self.server.url,
                        "credentials": {"api_key": "sandbox-key"},
                    }
                ]
            }
        )

        with unittest.mock.patch("funding_bot._require_https_url", side_effect=lambda url, **_: url):
            with self.assertRaises(ConnectorConfigError) as exc:
                FundingBot(connector_registry=self.registry)

        self.assertIn("Invalid credentials for connector 'sandbox-portal'", str(exc.exception))
        self.assertIn("'tenant' is a required property", str(exc.exception))

    def test_registered_connector_uses_alias_credentials_against_mock_endpoint(self):
        bootstrap = FundingBot(db_path=self.db_path)
        bootstrap.register_credential("sandbox", "SANDBOX_SECRET")
        bootstrap.close()
        os.environ["SANDBOX_SECRET"] = json.dumps(
            {"api_key": "sandbox-key", "tenant": "tenant-42"}
        )

        with unittest.mock.patch("funding_bot._require_https_url", side_effect=lambda url, **_: url):
            bot = FundingBot(
                db_path=self.db_path,
                trusted_sources={"Sandbox Registry Connector"},
                connector_registry=self.registry,
                connector_configs={
                    "connectors": [
                        {
                            "type": "sandbox-portal",
                            "transport": "http",
                            "base_url": self.server.url,
                            "credential_alias": "sandbox",
                        }
                    ]
                },
            )
        try:
            found = bot.run_discovery(keywords=["education"])
        finally:
            bot.close()

        self.assertEqual(1, len(found))
        self.assertEqual("Sandbox Education Grant", found[0]["title"])
        self.assertEqual("Sandbox Registry Connector", found[0]["source"])
        self.assertEqual(["education"], self.server.requests[0]["payload"]["keywords"])
        self.assertEqual("sandbox-key", self.server.requests[0]["headers"]["api_key"])
        self.assertEqual("tenant-42", self.server.requests[0]["headers"]["tenant"])


if __name__ == "__main__":
    unittest.main()
