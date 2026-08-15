#!/usr/bin/env python3
"""Invoke video-content-parser with a JSON-only stdout contract."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def private_cli_path() -> Path:
    root = Path.home() / ".video-parser" / "runtime"
    return root / ("Scripts/video-content-parser.exe" if os.name == "nt" else "bin/video-content-parser")


def find_command() -> list[str] | None:
    command = shutil.which("video-content-parser")
    if command:
        return [command]
    private = private_cli_path()
    if private.is_file():
        return [str(private)]
    if importlib.util.find_spec("video_content_parser") is not None:
        return [sys.executable, "-m", "video_content_parser"]
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a video summary and evidence package")
    parser.add_argument("source", type=Path, nargs="?")
    parser.add_argument("output_dir", type=Path, nargs="?")
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--force", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.source is None and args.input_dir is None:
        print("Specify a source video or --input-dir.", file=sys.stderr)
        return 2
    if args.source is not None and args.input_dir is not None:
        print("Use either a source video or --input-dir, not both.", file=sys.stderr)
        return 2

    command = find_command()
    if command is None:
        print("video-content-parser is not installed. Run install_runtime.py first.", file=sys.stderr)
        return 2

    if args.source is not None:
        command.append(str(args.source.expanduser().resolve()))
        if args.output_dir is not None:
            command.append(str(args.output_dir.expanduser().resolve()))
    else:
        command.extend(["--input-dir", str(args.input_dir.expanduser().resolve())])
        if args.output_root is not None:
            command.extend(["--output-root", str(args.output_root.expanduser().resolve())])
    if args.env_file is not None:
        command.extend(["--env-file", str(args.env_file.expanduser().resolve())])
    if args.force:
        command.append("--force")
    command.extend(["--result-format", "json"])

    child_env = os.environ.copy()
    child_env.setdefault("PYTHONUTF8", "1")
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        encoding="utf-8",
        env=child_env,
    )
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        print("Parser did not return valid JSON.", file=sys.stderr)
        if completed.stdout:
            print(completed.stdout, file=sys.stderr, end="")
        return completed.returncode or 1
    print(json.dumps(payload, ensure_ascii=False))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
