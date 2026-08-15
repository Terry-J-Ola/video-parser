#!/usr/bin/env python3
"""Report CLI and provider configuration readiness without exposing secrets."""

from __future__ import annotations

import json
import importlib.util
import os
import shutil
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


def _configured(config_text: str, key: str) -> bool:
    for line in config_text.splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == key and value.strip():
            return True
    return False


def main() -> int:
    cli = shutil.which("video-content-parser")
    if cli is None and private_cli_path().is_file():
        cli = str(private_cli_path())
    if cli is None and importlib.util.find_spec("video_content_parser") is not None:
        cli = f"{Path(sys.executable)} -m video_content_parser"

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
        "ready": bool(cli and has_key and has_base_url),
        "cli": cli,
        "config_file": str(config_file),
        "api_key_configured": has_key,
        "base_url_configured": has_base_url,
    }
    print(json.dumps(payload, ensure_ascii=True))
    return 0 if payload["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
