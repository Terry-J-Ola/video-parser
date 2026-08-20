from __future__ import annotations

import io
import json
import logging
import re
import tempfile
import unittest
from pathlib import Path

from video_content_parser import __version__
from video_content_parser.logging_config import setup_logging, shutdown_logging
from video_content_parser.models import Artifacts, VideoParseResult


class ReleaseIntegrityTests(unittest.TestCase):
    def test_release_versions_are_consistent(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest_version = json.loads(
            (root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )["version"]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        package_match = re.search(
            r"(?ms)^\[project\].*?^version\s*=\s*\"([^\"]+)\"",
            pyproject,
        )
        self.assertIsNotNone(package_match)
        runtime_version = (
            root / "skills" / "summarize-video" / "runtime-version.txt"
        ).read_text(encoding="utf-8").strip()

        self.assertEqual(manifest_version, "0.4.0")
        self.assertEqual(package_match.group(1), manifest_version)  # type: ignore[union-attr]
        self.assertEqual(__version__, manifest_version)
        self.assertEqual(runtime_version, manifest_version)

    def test_parse_result_records_loaded_version_and_package_path(self) -> None:
        result = VideoParseResult(
            status="partial",
            source_name="demo.mp4",
            duration_ms=None,
            title="demo",
            summary="",
            artifacts=Artifacts(
                audio_path=None,
                evidence_markdown_path="demo_技术证据稿.md",
            ),
        )
        package_path = Path(result.runtime_package_path)

        self.assertEqual(result.app_version, "0.4.0")
        self.assertTrue(package_path.is_dir())
        self.assertEqual(package_path.name, "video_content_parser")
        self.assertEqual(package_path, Path(__file__).resolve().parents[1] / "src" / "video_content_parser")

    def test_logging_shutdown_releases_file_and_preserves_host_handler(self) -> None:
        root_logger = logging.getLogger()
        host_stream = io.StringIO()
        host_handler = logging.StreamHandler(host_stream)
        root_logger.addHandler(host_handler)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = setup_logging(Path(temp_dir))
                logging.getLogger("release-test").info("log lifecycle test")
                shutdown_logging()

                self.assertIn(host_handler, root_logger.handlers)
                self.assertIn("log lifecycle test", log_path.read_text(encoding="utf-8"))
                log_path.unlink()
                self.assertFalse(log_path.exists())
        finally:
            shutdown_logging()
            root_logger.removeHandler(host_handler)
            host_handler.close()

    def test_reinitializing_logging_releases_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = setup_logging(root / "first")
            second = setup_logging(root / "second")
            first.unlink()
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())
            shutdown_logging()


if __name__ == "__main__":
    unittest.main()
