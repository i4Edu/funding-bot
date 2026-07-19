from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


def _keyword_matches(raw_keywords: list[str], *expected_terms: str) -> bool:
    haystack = " ".join(keyword.strip().lower() for keyword in raw_keywords if keyword).strip()
    if not haystack:
        return True
    return any(term.lower() in haystack for term in expected_terms)


def _grants_payload(keywords: list[str]) -> dict[str, Any]:
    opportunities = []
    if _keyword_matches(keywords, "education", "learning", "school", "innovation"):
        opportunities.append(
            {
                "id": "334326",
                "number": "MOCK-EDU-001",
                "title": "Mock Education Innovation Grant",
                "agency": "Mock Grants Agency",
                "agencyCode": "MGA",
                "docType": "Education",
                "oppStatus": "posted",
                "openDate": "2026-07-01",
                "closeDate": "2026-09-30",
                "cfdaList": ["84.001"],
            }
        )
    return {
        "data": {
            "hitCount": len(opportunities),
            "oppHits": opportunities,
        }
    }


def _csr_payload(keywords: list[str]) -> dict[str, Any]:
    opportunities = []
    if _keyword_matches(keywords, "csr", "digital learning", "corporate", "education"):
        opportunities.append(
            {
                "title": "Mock CSR Digital Learning Fund",
                "url": "https://mock.example.org/csr/digital-learning",
                "summary": "Mock CSR funding for digital learning and workforce readiness.",
                "category": "Corporate Partnerships",
                "program_areas": ["Digital Learning"],
                "eligibility": ["Nonprofit"],
                "tags": ["csr", "mock", "education"],
                "funder": {
                    "name": "Mock Corporate Giving",
                    "url": "https://mock.example.org/funders/mock-corporate-giving",
                },
            }
        )
    return {"results": opportunities}


def _sandbox_payload(keywords: list[str]) -> dict[str, Any]:
    opportunities = []
    if _keyword_matches(keywords, "sandbox", "education", "connector"):
        opportunities.append(
            {
                "source": "Mock Connector Sandbox",
                "donor_name": "Mock Connector Foundation",
                "title": "Sandbox Connector Opportunity",
                "portal_url": "https://mock.example.org/sandbox/opportunity",
                "summary": "Schema v2 payload for custom connector testing.",
                "category": "Education",
                "tags": ["sandbox", "connector", "mock"],
            }
        )
    return {"schema_version": 2, "opportunities": opportunities}


class MockConnectorHandler(BaseHTTPRequestHandler):
    server_version = "FundingBotMockConnector/1.0"

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw_body = self.rfile.read(length).decode("utf-8")
        if not raw_body.strip():
            return {}
        return json.loads(raw_body)

    def _write_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._write_json(
                200,
                {
                    "status": "ok",
                    "connectors": ["grants-portal", "csr-network", "sandbox"],
                },
            )
            return
        if parsed.path == "/csr-network":
            keywords = parse_qs(parsed.query).get("q", [""])
            self._write_json(200, _csr_payload(keywords))
            return
        self._write_json(404, {"error": f"Unknown endpoint: {parsed.path}"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        payload = self._read_json_body()
        keywords = []
        raw_keyword = payload.get("keyword")
        if isinstance(raw_keyword, str) and raw_keyword.strip():
            keywords.extend(raw_keyword.split())
        raw_keywords = payload.get("keywords")
        if isinstance(raw_keywords, list):
            keywords.extend(str(item) for item in raw_keywords)

        if parsed.path == "/grants-portal":
            self._write_json(200, _grants_payload(keywords))
            return
        if parsed.path == "/sandbox":
            self._write_json(200, _sandbox_payload(keywords))
            return
        self._write_json(404, {"error": f"Unknown endpoint: {parsed.path}"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), MockConnectorHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the funding-bot mock connector server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = create_server(args.host, args.port)
    try:
        print(f"Mock connector server listening on http://{args.host}:{args.port}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
