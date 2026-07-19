from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any

try:  # pragma: no cover - Python 3.11+ provides tomllib
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


HOME_CONFIG_CANDIDATES = ("config.yml", "config.yaml", "config.toml")
PROJECT_CONFIG_CANDIDATES = (
    Path(".funding-bot/config.yml"),
    Path(".funding-bot/config.yaml"),
    Path(".funding-bot/config.toml"),
    Path("funding-bot.toml"),
)


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _strip_yaml_comment(line: str) -> str:
    result: list[str] = []
    in_single = False
    in_double = False
    for character in line:
        if character == "'" and not in_double:
            in_single = not in_single
        elif character == '"' and not in_single:
            in_double = not in_double
        elif character == "#" and not in_single and not in_double:
            break
        result.append(character)
    return "".join(result).rstrip()


def _parse_yaml_scalar(raw_value: str) -> Any:
    value = raw_value.strip()
    if value == "":
        return ""
    if value[0] in {'"', "'"} and value[-1] == value[0]:
        return ast.literal_eval(value)
    lowered = value.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "~"}:
        return None
    if value.startswith("[") or value.startswith("{"):
        return json.loads(value)
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _prepare_yaml_lines(text: str) -> list[tuple[int, str]]:
    prepared: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        line = _strip_yaml_comment(raw_line)
        if not line.strip():
            continue
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip(" \t"))]:
            raise ValueError("Tabs are not supported in YAML configuration files.")
        indent = len(line) - len(line.lstrip(" "))
        prepared.append((indent, line.strip()))
    return prepared


def _parse_yaml_list(
    lines: list[tuple[int, str]], index: int, indent: int
) -> tuple[list[Any], int]:
    items: list[Any] = []
    while index < len(lines):
        current_indent, stripped = lines[index]
        if current_indent < indent:
            break
        if current_indent != indent or not stripped.startswith("- "):
            raise ValueError("Invalid YAML list indentation.")
        item_text = stripped[2:].strip()
        if item_text:
            items.append(_parse_yaml_scalar(item_text))
            index += 1
            continue
        if index + 1 >= len(lines) or lines[index + 1][0] <= current_indent:
            items.append(None)
            index += 1
            continue
        next_indent, next_stripped = lines[index + 1]
        if next_stripped.startswith("- "):
            value, index = _parse_yaml_list(lines, index + 1, next_indent)
        else:
            value, index = _parse_yaml_dict(lines, index + 1, next_indent)
        items.append(value)
    return items, index


def _parse_yaml_dict(
    lines: list[tuple[int, str]], index: int, indent: int
) -> tuple[dict[str, Any], int]:
    data: dict[str, Any] = {}
    while index < len(lines):
        current_indent, stripped = lines[index]
        if current_indent < indent:
            break
        if current_indent != indent or stripped.startswith("- "):
            raise ValueError("Invalid YAML mapping indentation.")
        key, separator, raw_value = stripped.partition(":")
        if separator != ":":
            raise ValueError(f"Invalid YAML line: {stripped!r}")
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value:
            data[key] = _parse_yaml_scalar(raw_value)
            index += 1
            continue
        if index + 1 >= len(lines) or lines[index + 1][0] <= current_indent:
            data[key] = None
            index += 1
            continue
        next_indent, next_stripped = lines[index + 1]
        if next_stripped.startswith("- "):
            value, index = _parse_yaml_list(lines, index + 1, next_indent)
        else:
            value, index = _parse_yaml_dict(lines, index + 1, next_indent)
        data[key] = value
    return data, index


def parse_yaml_config(text: str) -> dict[str, Any]:
    lines = _prepare_yaml_lines(text)
    if not lines:
        return {}
    data, index = _parse_yaml_dict(lines, 0, lines[0][0])
    if index != len(lines):
        raise ValueError("Unexpected trailing YAML content.")
    return data


def load_config_file(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".toml":
        if tomllib is None:
            raise RuntimeError("TOML configuration requires Python 3.11 or newer.")
        return dict(tomllib.loads(text))
    if suffix in {".yml", ".yaml"}:
        return parse_yaml_config(text)
    raise ValueError(f"Unsupported config file format for {path}.")


def _extract_config_path(argv: list[str] | None, env: dict[str, str] | None = None) -> str | None:
    arguments = list(argv or [])
    environment = env or os.environ
    for index, value in enumerate(arguments):
        if value == "--config" and index + 1 < len(arguments):
            return arguments[index + 1]
        if value.startswith("--config="):
            return value.split("=", 1)[1]
    return environment.get("FUNDING_BOT_CONFIG")


def discover_config_paths(
    *,
    argv: list[str] | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> list[Path]:
    explicit = _extract_config_path(argv, env=env)
    if explicit:
        return [Path(explicit).expanduser()]

    base_cwd = cwd or Path.cwd()
    base_home = home or Path.home()
    paths: list[Path] = []
    for candidate in HOME_CONFIG_CANDIDATES:
        resolved = (base_home / ".funding-bot" / candidate).resolve()
        if resolved.exists():
            paths.append(resolved)
    for candidate in PROJECT_CONFIG_CANDIDATES:
        resolved = (base_cwd / candidate).resolve()
        if resolved.exists():
            paths.append(resolved)
    return paths


def load_cli_config(
    *,
    argv: list[str] | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    config: dict[str, Any] = {}
    loaded_from: list[str] = []
    for path in discover_config_paths(argv=argv, cwd=cwd, env=env, home=home):
        if not path.exists():
            raise FileNotFoundError(f"Configuration file {path} does not exist.")
        config = _merge_dicts(config, load_config_file(path))
        loaded_from.append(str(path))
    if loaded_from:
        config["_loaded_from"] = loaded_from
    return config
