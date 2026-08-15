from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Protocol

from .errors import ModelProviderError
from .models import ModelTokenUsage, TranscriptSegment
from .omni import collect_stream_text
from .options import VideoParseOptions


class AsrBackend(Protocol):
    """ASR 后端的统一接口约定，不同实现需提供 transcribe 方法。"""

    def transcribe(
        self,
        audio_path: Path,
        language: str | None,
    ) -> tuple[list[TranscriptSegment], ModelTokenUsage]: ...


class QwenAsrBackend:
    """基于 Qwen 系列模型（OpenAI 兼容协议）的 ASR 后端实现。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        chunk_seconds: int = 30,
        max_retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.chunk_seconds = chunk_seconds
        self.max_retries = max_retries

    def transcribe(
        self,
        audio_path: Path,
        language: str | None,
    ) -> tuple[list[TranscriptSegment], ModelTokenUsage]:
        """将整段音频分片后逐段转写，并把每段文本还原到原始时间轴上。"""
        from . import audio as audio_module
        from . import ffmpeg as ffmpeg_module

        # 探测时长失败时回退到 60s，保证分片流程可继续
        duration = ffmpeg_module.probe_duration(audio_path) or 60000
        chunk_dir = audio_path.parent / "_chunks"
        chunks = audio_module.chunk_audio(audio_path, chunk_dir, self.chunk_seconds, duration)

        segments: list[TranscriptSegment] = []
        total_usage = ModelTokenUsage()

        for chunk in chunks:
            text, chunk_usage = self._transcribe_chunk(chunk.path, language)
            total_usage.add(chunk_usage)
            if text:
                # 用累计长度生成自增 ID，确保全局唯一
                segments.append(TranscriptSegment(
                    id=f"transcript-{len(segments) + 1:04d}",
                    start_ms=chunk.start_ms,
                    end_ms=chunk.end_ms,
                    text=text,
                    language=language,
                    confidence=None,
                    timing_source="audio_chunk",
                ))

        return segments, total_usage

    def _transcribe_chunk(self, chunk_path: Path, language: str | None) -> tuple[str, ModelTokenUsage]:
        """调用模型转写单个音频分片，失败时按 max_retries 重试。"""
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        for attempt in range(self.max_retries):
            try:
                # 将音频编码为 data URI，便于通过 chat 协议传入
                audio_bytes = chunk_path.read_bytes()
                encoded = base64.b64encode(audio_bytes).decode("ascii")
                mime_type = mimetypes.guess_type(chunk_path.name)[0] or "audio/mpeg"
                message = {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": f"data:{mime_type};base64,{encoded}",
                            },
                        },
                    ],
                }
                # enable_itn 关闭逆文本归一化，保留原始口语形态
                asr_options: dict[str, str | bool] = {"enable_itn": False}
                if language:
                    asr_options["language"] = language
                stream = client.chat.completions.create(
                    model=self.model,
                    messages=[message],  # type: ignore[list-item]
                    extra_body={"asr_options": asr_options},
                    stream=True,
                    stream_options={"include_usage": True},
                )
                content, usage = collect_stream_text(stream)
                if not content:
                    raise ValueError("服务商返回了空转写结果")
                return content, usage
            except Exception as exc:
                # 仅在最后一次重试仍失败时抛出，避免中间错误打断整段转写
                if attempt == self.max_retries - 1:
                    raise ModelProviderError(
                        f"Qwen ASR 转写失败（{chunk_path.name}）：{exc}"
                    ) from exc
        raise ModelProviderError(f"Qwen ASR 转写失败（{chunk_path.name}）")


# 保留旧类名，避免现有调用方在升级后立即中断。
OmniAsrBackend = QwenAsrBackend


class FakeAsrBackend:
    """用于测试与无网络环境下的 ASR 桩实现，返回固定的模拟转写片段。"""

    def __init__(self, segments: list[TranscriptSegment] | None = None):
        self._segments = segments

    def transcribe(
        self,
        audio_path: Path,
        language: str | None,
    ) -> tuple[list[TranscriptSegment], ModelTokenUsage]:
        _ = audio_path
        # 传入预置片段时直接复制返回，否则生成 12 段每 10s 的默认模拟数据
        if self._segments is not None:
            return [s.model_copy() for s in self._segments], ModelTokenUsage()
        lang = language or "zh"
        return [
            TranscriptSegment(
                id=f"seg-{i + 1:04d}",
                start_ms=i * 10000,
                end_ms=(i + 1) * 10000,
                text=f"模拟语音转写片段 {i + 1}",
                language=lang,
                timing_source="audio_chunk",
            )
            for i in range(12)
        ], ModelTokenUsage()


def create_asr_backend(
    options: VideoParseOptions,
    *,
    openai_api_key: str = "",
    openai_base_url: str = "",
    asr_model: str = "",
) -> AsrBackend:
    """根据是否提供完整的 OpenAI 兼容配置，选择真实或桩 ASR 后端。"""
    # 三项配置齐全才启用真实后端，否则降级到 FakeAsrBackend
    if openai_api_key and openai_base_url and asr_model:
        return QwenAsrBackend(
            base_url=openai_base_url,
            api_key=openai_api_key,
            model=asr_model,
            chunk_seconds=options.audio_chunk_seconds,
            max_retries=options.max_model_retries,
        )
    return FakeAsrBackend([])
