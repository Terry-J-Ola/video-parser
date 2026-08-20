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
from video_content_parser.__main__ import VideoRecord, _process_one_video, main
from video_content_parser.models import ModelTokenUsage, TokenUsage
from video_content_parser.options import VideoParseOptions


@dataclass
class Record:
    name: str
    status: str
    asr_model: str
    asr_tokens: int
    vlm_model: str
    vlm_tokens: int
    summary_model: str
    summary_tokens: int
    total_tokens: int
    elapsed_seconds: float


class BatchSummaryXlsxTests(unittest.TestCase):
    def test_process_record_keeps_each_model_token_total(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "课程.mp4"
            source.write_bytes(b"test")
            output_dir = root / "result"
            provider = SimpleNamespace(
                asr_model="qwen3-asr-flash",
                vlm_model="qwen3-vl-flash",
                section_model="qwen3.7-plus",
            )
            usage = TokenUsage(
                asr=ModelTokenUsage(total_tokens=100),
                vlm=ModelTokenUsage(total_tokens=200),
                sections=ModelTokenUsage(total_tokens=300),
            )
            result = SimpleNamespace(
                status="complete",
                warnings=[],
                token_usage=usage,
                artifacts=SimpleNamespace(
                    evidence_markdown_path="课程_技术证据稿.md",
                    learner_markdown_path="课程_业务讲义.md",
                ),
                transcript_segments=[],
                keyframes=[],
                frame_analyses=[],
                sections=[],
            )
            parser = SimpleNamespace(parse=lambda *_args, **_kwargs: result)
            args = SimpleNamespace(
                result_format="json",
                source=source,
                output_dir=output_dir,
                output_root=None,
                force=True,
                provider_config=provider,
            )

            with patch("video_content_parser.__main__.VideoParser", return_value=parser):
                exit_code, record, returned_usage = _process_one_video(
                    source, args, VideoParseOptions(),
                )

            self.assertEqual(exit_code, 0)
            self.assertIs(returned_usage, usage)
            self.assertEqual(record.asr_tokens, 100)
            self.assertEqual(record.vlm_tokens, 200)
            self.assertEqual(record.summary_tokens, 300)
            self.assertEqual(record.total_tokens, 600)
            self.assertEqual(record.asr_model, "qwen3-asr-flash")
            self.assertEqual(record.vlm_model, "qwen3-vl-flash")
            self.assertEqual(record.summary_model, "qwen3.7-plus")

    def test_exports_rows_and_formula_totals(self) -> None:
        records = [
            Record(
                "课程一.mp4", "complete",
                "qwen3-asr-flash", 300,
                "qwen3-vl-flash", 400,
                "qwen3.7-plus", 500,
                1200, 12.25,
            ),
            Record(
                "课程二.mp4", "partial",
                "qwen3-asr-flash", 200,
                "qwen3-vl-flash", 250,
                "qwen3.7-plus", 350,
                800, 7.75,
            ),
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

            self.assertEqual(cell("E10").findtext("s:f", namespaces=namespace), "SUM(E8:E9)")
            self.assertEqual(cell("E10").findtext("s:v", namespaces=namespace), "500")
            self.assertEqual(cell("G10").findtext("s:f", namespaces=namespace), "SUM(G8:G9)")
            self.assertEqual(cell("G10").findtext("s:v", namespaces=namespace), "650")
            self.assertEqual(cell("I10").findtext("s:f", namespaces=namespace), "SUM(I8:I9)")
            self.assertEqual(cell("I10").findtext("s:v", namespaces=namespace), "850")
            self.assertEqual(cell("J8").findtext("s:f", namespaces=namespace), "E8+G8+I8")
            self.assertEqual(cell("J8").findtext("s:v", namespaces=namespace), "1200")
            self.assertEqual(cell("J10").findtext("s:f", namespaces=namespace), "SUM(J8:J9)")
            self.assertEqual(cell("J10").findtext("s:v", namespaces=namespace), "2000")
            self.assertEqual(cell("K10").findtext("s:f", namespaces=namespace), "SUM(K8:K9)")
            self.assertEqual(cell("K10").findtext("s:v", namespaces=namespace), "20.0")
            self.assertEqual(cell("B5").findtext("s:v", namespaces=namespace), "2")
            self.assertEqual(cell("E5").findtext("s:v", namespaces=namespace), "500")
            self.assertEqual(cell("G5").findtext("s:v", namespaces=namespace), "650")
            self.assertEqual(cell("I5").findtext("s:v", namespaces=namespace), "850")
            self.assertEqual(cell("K5").findtext("s:v", namespaces=namespace), "2000")

            text_nodes = root.findall(".//s:t", namespace)
            text = "\n".join(node.text or "" for node in text_nodes)
            self.assertIn("课程一.mp4", text)
            self.assertIn("课程二.mp4", text)
            self.assertIn("部分完成", text)
            self.assertIn("qwen3-asr-flash", text)
            self.assertIn("qwen3-vl-flash", text)
            self.assertIn("qwen3.7-plus", text)

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
                asr_model="qwen3-asr-flash",
                asr_tokens=1000,
                vlm_model="qwen3-vl-flash",
                vlm_tokens=1200,
                summary_model="qwen3.7-plus",
                summary_tokens=1256,
            )
            provider = SimpleNamespace(
                openai_api_key="configured",
                openai_base_url="configured",
                asr_model="qwen3-asr-flash",
                vlm_model="qwen3-vl-flash",
                section_model="qwen3.7-plus",
            )
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
            self.assertEqual(payload["schema_version"], "video_content_parser.cli.v3")
            self.assertEqual(payload["results"][0]["total_tokens"], 3456)
            self.assertEqual(payload["results"][0]["token_usage"]["asr"]["total_tokens"], 1000)
            self.assertEqual(payload["results"][0]["token_usage"]["vlm"]["total_tokens"], 1200)
            self.assertEqual(payload["results"][0]["token_usage"]["summary"]["total_tokens"], 1256)
            self.assertEqual(payload["results"][0]["token_usage"]["summary"]["model"], "qwen3.7-plus")
            self.assertEqual(payload["results"][0]["elapsed_seconds"], 12.5)
            self.assertTrue(report_path.is_file())
            self.assertEqual(report_path.suffix, ".xlsx")
            log_files = list((output_root / "logs").glob("parse_*.log"))
            self.assertEqual(len(log_files), 1)
            log_text = log_files[0].read_text(encoding="utf-8")
            self.assertIn("程序版本: 0.4.0", log_text)
            self.assertIn("运行包路径:", log_text)


if __name__ == "__main__":
    unittest.main()
