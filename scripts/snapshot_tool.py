#!/usr/bin/env python3
"""Validate or update regression snapshots."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DEFAULT_TARGETS = ("tests/test_regression.py",)


def _build_pytest_command(command: str, targets: list[str]) -> list[str]:
    pytest_command = [sys.executable, "-m", "pytest", *targets]
    if command == "update":
        pytest_command.append("--snapshot-update")
    return pytest_command


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate or refresh committed regression snapshots."
    )
    parser.add_argument(
        "command",
        choices=("validate", "update"),
        help="Compare snapshots or regenerate them in place.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        default=list(DEFAULT_TARGETS),
        help="Optional pytest targets. Defaults to the regression suite.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    command = _build_pytest_command(args.command, args.targets)
    print(f"Running: {' '.join(command)}")
    completed = subprocess.run(command, cwd=repo_root)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
