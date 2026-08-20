from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from video_content_parser.asr import _dedup_overlap
from video_content_parser.audio import chunk_audio
from video_content_parser.frame_analysis import ChatVlmBackend
from video_content_parser.keyframes import _CandidateFrame, extract_keyframes
from video_content_parser.markdown import render_evidence_markdown
from video_content_parser.models import (
    Artifacts,
    FrameAnalysis,
    Keyframe,
    ModelTokenUsage,
    TranscriptSegment,
    VideoParseResult,
)
from video_content_parser.omni import collect_stream_text
from video_content_parser.options import VideoParseOptions
from video_content_parser.sections import ChatSectionBuilder


def _stream_chunk(
    content: str,
    *,
    model: str = "qwen-snapshot",
    total_tokens: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=total_tokens,
            completion_tokens=0,
            total_tokens=total_tokens,
            prompt_tokens_details=None,
        ),
    )


class _AsyncStream:
    def __init__(self, chunks: list[SimpleNamespace]):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("batch payload rejected")
        frame_id = f"frame-{self.calls - 1:04d}"
        payload = (
            '{"frames":[{"keyframe_id":"'
            + frame_id
            + '","visible_text":[],"description":"recovered",'
            '"content_type":"slide"}]}'
        )
        return _AsyncStream([_stream_chunk(payload, total_tokens=5)])


class FidelityFeatureTests(unittest.TestCase):
    def test_stream_usage_captures_actual_model_and_preserves_it_when_added(self) -> None:
        text, usage = collect_stream_text([
            _stream_chunk("hello", model="qwen3-asr-flash-2026-01-01", total_tokens=7),
        ])
        total = ModelTokenUsage()
        total.add(usage)

        self.assertEqual(text, "hello")
        self.assertEqual(usage.model, "qwen3-asr-flash-2026-01-01")
        self.assertEqual(total.model, usage.model)
        self.assertEqual(total.total_tokens, 7)

    def test_asr_overlap_text_is_removed_without_dropping_new_content(self) -> None:
        result = _dedup_overlap(
            "先选择商品，再点击确认提交。",
            "点击确认提交，然后检查页面状态。",
        )
        self.assertEqual(result, "，然后检查页面状态。")

    def test_audio_chunks_use_requested_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio_path = root / "audio.mp3"
            audio_path.write_bytes(b"audio")

            def fake_ffmpeg(args: list[str]):
                Path(args[-1]).write_bytes(b"chunk")
                return SimpleNamespace(returncode=0, stderr=b"")

            with patch(
                "video_content_parser.audio.ffmpeg_module.run_ffmpeg",
                side_effect=fake_ffmpeg,
            ):
                chunks = chunk_audio(
                    audio_path,
                    root / "chunks",
                    chunk_seconds=30,
                    total_duration_ms=65000,
                    overlap_seconds=2.0,
                )

            self.assertEqual(
                [(item.start_ms, item.end_ms) for item in chunks],
                [(0, 30000), (28000, 58000), (56000, 65000)],
            )

    def test_vlm_batch_failure_falls_back_to_single_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            keyframes: list[Keyframe] = []
            for index in range(1, 3):
                image_path = root / f"frame-{index:04d}.jpg"
                image_path.write_bytes(b"jpeg-bytes")
                keyframes.append(Keyframe(
                    id=f"frame-{index:04d}",
                    timestamp_ms=index * 1000,
                    image_path=str(image_path),
                    selection_reason="scene_change",
                    width=10,
                    height=10,
                    perceptual_hash="0" * 16,
                ))

            backend = ChatVlmBackend(
                base_url="https://example.invalid/v1",
                api_key="test",
                model="qwen3-vl-flash",
                max_retries=1,
            )
            completions = _FakeCompletions()
            client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
            analyses, warnings, usage = asyncio.run(
                backend._analyze_batch_async(keyframes, client)
            )

            self.assertEqual([item.keyframe_id for item in analyses], [
                "frame-0001", "frame-0002",
            ])
            self.assertEqual(warnings, [])
            self.assertEqual(usage.total_tokens, 10)
            self.assertEqual(usage.model, "qwen-snapshot")

    def test_pixel_check_rescues_dhash_collision_but_removes_true_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            black_a = root / "black-a.png"
            black_b = root / "black-b.png"
            white = root / "white.png"
            Image.new("RGB", (40, 40), "black").save(black_a)
            Image.new("RGB", (40, 40), "black").save(black_b)
            Image.new("RGB", (40, 40), "white").save(white)
            options = VideoParseOptions(dedup_hamming_threshold=6)

            rescued_candidates = [
                _CandidateFrame(0, black_a, "scene_change", None),
                _CandidateFrame(1000, white, "scene_change", None),
            ]
            with patch(
                "video_content_parser.keyframes._extract_scene_frames",
                return_value=rescued_candidates,
            ):
                rescued = extract_keyframes(
                    root / "video.mp4", root / "rescued", options, 2000,
                )

            duplicate_candidates = [
                _CandidateFrame(0, black_a, "scene_change", None),
                _CandidateFrame(1000, black_b, "scene_change", None),
            ]
            with patch(
                "video_content_parser.keyframes._extract_scene_frames",
                return_value=duplicate_candidates,
            ):
                deduplicated = extract_keyframes(
                    root / "video.mp4", root / "duplicate", options, 2000,
                )

            self.assertEqual(len(rescued), 2)
            self.assertEqual(len(deduplicated), 1)

    def test_evidence_markdown_interleaves_events_by_timestamp(self) -> None:
        transcript = TranscriptSegment(
            id="transcript-0001",
            start_ms=10000,
            end_ms=20000,
            text="later transcript",
            timing_source="audio_chunk",
        )
        keyframe = Keyframe(
            id="frame-0001",
            timestamp_ms=5000,
            image_path="assets/keyframes/frame-0001.jpg",
            selection_reason="scene_change",
            width=10,
            height=10,
            perceptual_hash="0" * 16,
        )
        result = VideoParseResult(
            status="partial",
            source_name="demo.mp4",
            duration_ms=20000,
            title="demo",
            summary="",
            artifacts=Artifacts(
                audio_path=None,
                evidence_markdown_path="demo_技术证据稿.md",
            ),
            transcript_segments=[transcript],
            keyframes=[keyframe],
        )

        markdown = render_evidence_markdown(result)
        self.assertLess(markdown.index("`frame-0001`"), markdown.index("`transcript-0001`"))

    def test_fallback_sections_preserve_all_transcript_details(self) -> None:
        transcripts = [
            TranscriptSegment(
                id="transcript-0001",
                start_ms=0,
                end_ms=10000,
                text="产品型号为 A17，陈列在左侧第二层。",
                timing_source="audio_chunk",
            ),
            TranscriptSegment(
                id="transcript-0002",
                start_ms=10000,
                end_ms=20000,
                text="有条件的门店需要先扫码，再点击蓝色确认按钮。",
                timing_source="audio_chunk",
            ),
        ]
        backend = ChatSectionBuilder(
            base_url="https://example.invalid/v1",
            api_key="test",
            model="qwen3.7-plus",
        )
        sections, warnings, _usage = backend._fallback_build(transcripts, [])

        combined = "\n".join(paragraph for section in sections for paragraph in section.paragraphs)
        self.assertEqual(warnings, [])
        for transcript in transcripts:
            self.assertIn(transcript.text, combined)


if __name__ == "__main__":
    unittest.main()
