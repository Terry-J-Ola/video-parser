from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree
from zipfile import ZipFile

from video_content_parser.batch_xlsx import export_batch_summary_xlsx
from video_content_parser.__main__ import VideoRecord, main


@dataclass
class Record:
    name: str
    status: str
    total_tokens: int
    elapsed_seconds: float


class BatchSummaryXlsxTests(unittest.TestCase):
    def test_exports_rows_and_formula_totals(self) -> None:
        records = [
            Record("课程一.mp4", "complete", 1200, 12.25),
            Record("课程二.mp4", "partial", 800, 7.75),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "batch_summary.xlsx"
            export_batch_summary_xlsx(
                records,
                target,
                generated_at=datetime(2026, 8, 17, 10, 30, tzinfo=timezone.utc),
            )

            self.assertTrue(target.is_file())
            with ZipFile(target) as archive:
                self.assertIn("xl/workbook.xml", archive.namelist())
                self.assertIn("xl/worksheets/sheet1.xml", archive.namelist())
                sheet_xml = archive.read("xl/worksheets/sheet1.xml")

            root = ElementTree.fromstring(sheet_xml)
            namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

            def cell(reference: str) -> ElementTree.Element:
                found = root.find(f".//s:c[@r='{reference}']", namespace)
                self.assertIsNotNone(found)
                return found  # type: ignore[return-value]

            self.assertEqual(cell("D10").findtext("s:f", namespaces=namespace), "SUM(D8:D9)")
            self.assertEqual(cell("D10").findtext("s:v", namespaces=namespace), "2000")
            self.assertEqual(cell("E10").findtext("s:f", namespaces=namespace), "SUM(E8:E9)")
            self.assertEqual(cell("E10").findtext("s:v", namespaces=namespace), "20.0")
            self.assertEqual(cell("B5").findtext("s:v", namespaces=namespace), "2")
            self.assertEqual(cell("E5").findtext("s:v", namespaces=namespace), "2000")

            text_nodes = root.findall(".//s:t", namespace)
            text = "\n".join(node.text or "" for node in text_nodes)
            self.assertIn("课程一.mp4", text)
            self.assertIn("课程二.mp4", text)
            self.assertIn("部分完成", text)

    def test_batch_json_returns_xlsx_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_root = root / "output"
            input_dir.mkdir()
            (input_dir / "课程.mp4").write_bytes(b"test")
            record = VideoRecord(
                name="课程.mp4",
                status="complete",
                total_tokens=3456,
                elapsed_seconds=12.5,
            )
            provider = SimpleNamespace(openai_api_key="configured", openai_base_url="configured")
            stdout = StringIO()

            with (
                patch("video_content_parser.__main__.load_provider_config", return_value=provider),
                patch("video_content_parser.__main__._process_one_video", return_value=(0, record, None)),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "--input-dir",
                        str(input_dir),
                        "--output-root",
                        str(output_root),
                        "--result-format",
                        "json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            report_path = Path(payload["batch_summary_xlsx"])
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["schema_version"], "video_content_parser.cli.v2")
            self.assertEqual(payload["results"][0]["total_tokens"], 3456)
            self.assertEqual(payload["results"][0]["elapsed_seconds"], 12.5)
            self.assertTrue(report_path.is_file())
            self.assertEqual(report_path.suffix, ".xlsx")


if __name__ == "__main__":
    unittest.main()
