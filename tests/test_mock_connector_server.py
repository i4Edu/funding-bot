from __future__ import annotations

import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from funding_bot import CSRNetworkConnector, GrantsPortalConnector


def _request_json(method: str, url: str, payload: dict | None = None) -> dict:
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_health_endpoint_reports_available_connectors(
    mock_connector_server: dict[str, str],
) -> None:
    payload = _request_json("GET", mock_connector_server["health_url"])
    assert payload["status"] == "ok"
    assert "grants-portal" in payload["connectors"]


def test_grants_portal_endpoint_returns_expected_shape(
    mock_connector_server: dict[str, str],
) -> None:
    payload = _request_json(
        "POST",
        mock_connector_server["grants_portal_url"],
        {"keyword": "education learning"},
    )
    assert payload["data"]["hitCount"] == 1
    assert payload["data"]["oppHits"][0]["title"] == "Mock Education Innovation Grant"


def test_csr_network_endpoint_returns_expected_shape(mock_connector_server: dict[str, str]) -> None:
    query = urlencode({"q": "csr digital learning"})
    payload = _request_json("GET", f"{mock_connector_server['csr_network_url']}?{query}")
    assert payload["results"][0]["title"] == "Mock CSR Digital Learning Fund"


def test_connectors_can_use_mock_server_endpoints(mock_connector_server: dict[str, str]) -> None:
    grants_connector = GrantsPortalConnector(
        base_url=mock_connector_server["grants_portal_url"],
        transport="http",
    )
    csr_connector = CSRNetworkConnector(
        base_url=mock_connector_server["csr_network_url"],
        credentials={"subscription_key": "test-subscription-key"},
        transport="http",
    )

    grants_result = grants_connector.fetch_result(["education"])
    csr_result = csr_connector.fetch_result(["csr", "digital learning"])

    assert grants_result["metadata"]["source_status"] == "remote"
    assert grants_result["opportunities"][0]["title"] == "Mock Education Innovation Grant"
    assert csr_result["metadata"]["source_status"] == "remote"
    assert csr_result["opportunities"][0]["title"] == "Mock CSR Digital Learning Fund"
