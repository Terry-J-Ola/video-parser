#!/usr/bin/env python3
"""Interactively write per-user video parser configuration without echoing secrets."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path


def default_config_path() -> Path:
    if os.name == "nt":
        return Path.home() / ".video-parser" / "config.env"
    root = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "video-parser" / "config.env"


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure summarize-video provider access")
    parser.add_argument("--config", type=Path, default=default_config_path())
    args = parser.parse_args()

    base_url = _prompt("API Base URL")
    api_key = getpass.getpass("API Key: ").strip()
    if not base_url or not api_key:
        print("Configuration not saved: API Base URL and API Key are required.")
        return 2

    asr_model = _prompt("ASR Model", "qwen3-asr-flash")
    vlm_model = _prompt("VLM Model", "qwen3-vl-flash")
    summary_model = _prompt("Summary Model", "qwen3.7-plus")

    target = args.config.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join([
        f"VIDEO_PARSER_API_KEY={api_key}",
        f"VIDEO_PARSER_BASE_URL={base_url}",
        f"VIDEO_PARSER_ASR_MODEL={asr_model}",
        f"VIDEO_PARSER_VLM_MODEL={vlm_model}",
        f"VIDEO_PARSER_SUMMARY_MODEL={summary_model}",
        "",
    ])
    target.write_text(content, encoding="utf-8")
    if os.name != "nt":
        target.chmod(0o600)
    print(f"Configuration saved to: {target.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
