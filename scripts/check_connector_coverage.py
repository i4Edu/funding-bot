from __future__ import annotations

import ast
import json
import sys
from pathlib import Path


TARGET_SYMBOLS = (
    "GrantsPortalConnector",
    "CSRNetworkConnector",
    "NGODirectoryConnector",
    "FoundationDirectoryConnector",
    "CrowdfundingConnector",
    "GlobalGivingConnector",
    "KickstarterForGoodConnector",
    "ConnectorRegistry",
    "default_connectors",
    "connector_registry",
    "create_connector",
)


def _ranges_for_symbols(source_path: Path) -> list[tuple[str, int, int]]:
    module = ast.parse(source_path.read_text(encoding="utf-8"))
    ranges: list[tuple[str, int, int]] = []
    for node in module.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in TARGET_SYMBOLS:
            ranges.append((node.name, node.lineno, node.end_lineno))
    missing = sorted(set(TARGET_SYMBOLS) - {name for name, _, _ in ranges})
    if missing:
        raise SystemExit(f"Unable to locate connector symbols: {', '.join(missing)}")
    return ranges


def main() -> int:
    coverage_path = Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json")
    threshold = float(sys.argv[2] if len(sys.argv) > 2 else "90")
    report = json.loads(coverage_path.read_text(encoding="utf-8"))
    file_report = report["files"].get("funding_bot.py")
    if file_report is None:
        raise SystemExit("coverage.json does not contain funding_bot.py results.")

    executed = set(file_report.get("executed_lines", []))
    missing = set(file_report.get("missing_lines", []))
    executable = executed | missing

    covered_total = 0
    executable_total = 0
    for name, start, end in _ranges_for_symbols(Path("funding_bot.py")):
        symbol_lines = {line for line in executable if start <= line <= end}
        covered = len(symbol_lines & executed)
        total = len(symbol_lines)
        percent = 100.0 if total == 0 else (covered / total) * 100
        covered_total += covered
        executable_total += total
        print(f"{name}: {covered}/{total} executable lines covered ({percent:.1f}%)")

    overall = 100.0 if executable_total == 0 else (covered_total / executable_total) * 100
    print(f"Connector coverage: {covered_total}/{executable_total} executable lines ({overall:.1f}%)")
    if overall < threshold:
        print(f"Connector coverage {overall:.1f}% is below the required {threshold:.1f}% threshold.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
