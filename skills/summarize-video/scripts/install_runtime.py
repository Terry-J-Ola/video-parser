#!/usr/bin/env python3
"""Install video-content-parser into a private per-user virtual environment."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _repository_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[3]
    return candidate if (candidate / "pyproject.toml").is_file() else None


def _install_from_built_wheel(python: Path, source: str) -> None:
    """Build with the host Python, then install the wheel into the private runtime."""

    with tempfile.TemporaryDirectory(prefix="video-parser-wheel-") as temp_dir:
        wheel_dir = Path(temp_dir)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(wheel_dir),
                source,
            ],
            check=True,
        )
        wheels = sorted(wheel_dir.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"Expected one built wheel, found {len(wheels)}")
        subprocess.run(
            [str(python), "-m", "pip", "install", "--upgrade", str(wheels[0])],
            check=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Install summarize-video CLI runtime")
    parser.add_argument(
        "--source",
        help="Python package source, local repository, wheel, or git+https URL",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=Path.home() / ".video-parser" / "runtime",
    )
    args = parser.parse_args()

    source = args.source or os.getenv("VIDEO_PARSER_PACKAGE_SOURCE")
    if source is None:
        repository_root = _repository_root()
        if repository_root is None:
            print(
                "No package source is available. Pass --source with the approved "
                "wheel, package name, local repository, or git+https URL.",
                file=sys.stderr,
            )
            return 2
        source = str(repository_root)

    runtime_dir = args.runtime_dir.expanduser()
    if not (runtime_dir / "pyvenv.cfg").is_file():
        subprocess.run([sys.executable, "-m", "venv", str(runtime_dir)], check=True)

    python = runtime_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    install_command = [str(python), "-m", "pip", "install", "--upgrade", source]
    try:
        subprocess.run(install_command, check=True)
    except subprocess.CalledProcessError:
        print(
            "Direct install failed; building a wheel with the host Python and retrying.",
            file=sys.stderr,
        )
        _install_from_built_wheel(python, source)
    cli = runtime_dir / (
        "Scripts/video-content-parser.exe" if os.name == "nt" else "bin/video-content-parser"
    )
    subprocess.run([str(cli), "--version"], check=True)
    print(f"Runtime installed: {cli.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
