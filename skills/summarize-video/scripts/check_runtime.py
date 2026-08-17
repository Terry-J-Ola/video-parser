#!/usr/bin/env python3
"""Report CLI and provider configuration readiness without exposing secrets."""

from __future__ import annotations

import json
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def default_config_path() -> Path:
    if os.name == "nt":
        return Path.home() / ".video-parser" / "config.env"
    root = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "video-parser" / "config.env"


def private_cli_path() -> Path:
    root = Path.home() / ".video-parser" / "runtime"
    return root / ("Scripts/video-content-parser.exe" if os.name == "nt" else "bin/video-content-parser")


def required_runtime_version() -> str:
    return (Path(__file__).resolve().parent.parent / "runtime-version.txt").read_text(
        encoding="utf-8"
    ).strip()


def command_version(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            [*command, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    match = re.search(r"\b(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b", completed.stdout)
    return match.group(1) if match else None


def _configured(config_text: str, key: str) -> bool:
    for line in config_text.splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == key and value.strip():
            return True
    return False


def main() -> int:
    command: list[str] | None = None
    private_cli = private_cli_path()
    if private_cli.is_file():
        command = [str(private_cli)]
    else:
        cli_on_path = shutil.which("video-content-parser")
        if cli_on_path is not None:
            command = [cli_on_path]
        elif importlib.util.find_spec("video_content_parser") is not None:
            command = [str(Path(sys.executable)), "-m", "video_content_parser"]

    required_version = required_runtime_version()
    runtime_version = command_version(command) if command else None
    runtime_update_required = bool(command and runtime_version != required_version)
    cli = " ".join(command) if command else None

    config_file = default_config_path()
    config_text = config_file.read_text(encoding="utf-8") if config_file.is_file() else ""
    has_key = bool(
        os.getenv("VIDEO_PARSER_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or _configured(config_text, "VIDEO_PARSER_API_KEY")
    )
    has_base_url = bool(
        os.getenv("VIDEO_PARSER_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or _configured(config_text, "VIDEO_PARSER_BASE_URL")
    )
    payload = {
        "ready": bool(cli and not runtime_update_required and has_key and has_base_url),
        "cli": cli,
        "runtime_version": runtime_version,
        "required_runtime_version": required_version,
        "runtime_update_required": runtime_update_required,
        "config_file": str(config_file),
        "api_key_configured": has_key,
        "base_url_configured": has_base_url,
    }
    print(json.dumps(payload, ensure_ascii=True))
    return 0 if payload["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
