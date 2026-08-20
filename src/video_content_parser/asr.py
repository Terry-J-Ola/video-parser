from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path
from typing import Protocol

from .errors import ModelProviderError
from .logging_config import get_logger
from .models import ModelTokenUsage, TranscriptSegment
from .omni import collect_stream_text
from .options import VideoParseOptions

logger = get_logger(__name__)


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
        overlap_seconds: float = 2.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.chunk_seconds = chunk_seconds
        self.max_retries = max_retries
        self.overlap_seconds = overlap_seconds

    def transcribe(
        self,
        audio_path: Path,
        language: str | None,
    ) -> tuple[list[TranscriptSegment], ModelTokenUsage]:
        """将整段音频分片后逐段转写，并把每段文本还原到原始时间轴上。

        相邻分片间有 overlap_seconds 秒重叠区，转写后通过文本去重消除重叠区
        产生的重复文字，同时将每个分片的 start_ms 对齐到前一分片的 end_ms。
        """
        from . import audio as audio_module
        from . import ffmpeg as ffmpeg_module

        # 探测时长失败时回退到 60s，保证分片流程可继续
        duration = ffmpeg_module.probe_duration(audio_path) or 60000
        chunk_dir = audio_path.parent / "_chunks"
        chunks = audio_module.chunk_audio(
            audio_path, chunk_dir, self.chunk_seconds, duration,
            overlap_seconds=self.overlap_seconds,
        )
        logger.info(
            "ASR 分片完成: %d 个 chunk（每片 %ds，重叠 %.1fs）",
            len(chunks), self.chunk_seconds, self.overlap_seconds,
        )

        segments: list[TranscriptSegment] = []
        total_usage = ModelTokenUsage()
        failed_chunks: list[str] = []
        # 记录上一个成功 chunk 的文本，用于重叠区去重
        prev_text: str | None = None
        # 是否已向控制台公布过实际调用模型名（从 API 响应提取，非配置值）
        model_announced = False

        for idx, chunk in enumerate(chunks, start=1):
            try:
                text, chunk_usage = self._transcribe_chunk(chunk.path, language)
                logger.debug(
                    "chunk %d/%d 转写成功: %s（%d 字）",
                    idx, len(chunks), chunk.path.name, len(text),
                )
                # 第一个成功 chunk 拿到 API 实际返回的模型名后立即公布，
                # 让控制台尽早看到真实调用的模型（而非配置里的值）。
                if not model_announced and chunk_usage.model:
                    logger.info("ASR 实际调用模型: %s", chunk_usage.model)
                    model_announced = True
                # 每 10 个 chunk 或最后一个 chunk 打一条 INFO 级别进度日志，
                # 让控制台能实时看到 ASR 转写进度（DEBUG 级别只在文件中可见）。
                # 失败的 chunk 由下方 WARNING 日志覆盖（控制台可见）。
                if idx % 10 == 0 or idx == len(chunks):
                    logger.info(
                        "ASR 转写进度: %d/%d (%d%%)",
                        idx, len(chunks), idx * 100 // len(chunks),
                    )
            except Exception as exc:
                logger.warning(
                    "chunk %d/%d 转写失败: %s - %s",
                    idx, len(chunks), chunk.path.name, exc,
                )
                failed_chunks.append(f"{chunk.path.name}: {exc}")
                prev_text = None  # 中断去重链，下一段不做去重
                continue
            total_usage.add(chunk_usage)
            if not text:
                prev_text = None
                continue

            # 对重叠区做文本去重：去掉当前 chunk 开头与上一个 chunk 结尾重复的部分
            if prev_text is not None:
                text = _dedup_overlap(prev_text, text)

            # 第一个 chunk 的时间区间为自身；后续 chunk 的 start 对齐到前一个 chunk 的 end
            if segments:
                seg_start_ms = segments[-1].end_ms
            else:
                seg_start_ms = chunk.start_ms

            segments.append(TranscriptSegment(
                id=f"transcript-{len(segments) + 1:04d}",
                start_ms=seg_start_ms,
                end_ms=chunk.end_ms,
                text=text,
                language=language,
                confidence=None,
                timing_source="audio_chunk",
            ))
            prev_text = text

        # 单个 chunk 失败不影响其他 chunk；仅当全部 chunk 都失败时才抛出异常
        if not segments and failed_chunks:
            logger.error("ASR 全部 %d 个 chunk 转写失败", len(chunks))
            raise ModelProviderError(
                f"ASR 转写全部失败（共 {len(chunks)} 个 chunk）："
                + "; ".join(failed_chunks)
            )

        if failed_chunks:
            logger.warning(
                "ASR 部分失败: %d/%d 个 chunk 成功，%d 个失败",
                len(segments), len(chunks), len(failed_chunks),
            )
        logger.info("ASR 转写完成: %d 个片段", len(segments))
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
                    logger.error(
                        "chunk %s 重试 %d 次后仍失败: %s", chunk_path.name, self.max_retries, exc,
                    )
                    raise ModelProviderError(
                        f"Qwen ASR 转写失败（{chunk_path.name}）：{exc}"
                    ) from exc
                logger.debug(
                    "chunk %s 第 %d 次尝试失败，准备重试: %s",
                    chunk_path.name, attempt + 1, exc,
                )
        raise ModelProviderError(f"Qwen ASR 转写失败（{chunk_path.name}）")


# 保留旧类名，避免现有调用方在升级后立即中断。
OmniAsrBackend = QwenAsrBackend


# 去除标点和空白后的字符集合，用于"纯文本"匹配
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _strip_punctuation(text: str) -> str:
    """去掉标点、空格等非实质字符，只保留汉字/字母/数字。"""
    return _PUNCT_RE.sub("", text)


def _dedup_overlap(prev_text: str, curr_text: str, min_match: int = 3) -> str:
    """去掉 curr_text 开头与 prev_text 结尾重叠的部分。

    ASR 分片间有 2-3 秒重叠区，重叠区的文字会在相邻两个 chunk 中各出现一次。
    此函数找出最长的公共子串（prev 的后缀 vs curr 的前缀），从 curr 开头截掉重复部分。

    匹配策略：先去掉标点空格做"纯文本"精确匹配，再在原始文本中定位截断位置。
    """
    prev_clean = _strip_punctuation(prev_text)
    curr_clean = _strip_punctuation(curr_text)

    if len(prev_clean) < min_match or len(curr_clean) < min_match:
        return curr_text

    # 在 prev_clean 结尾和 curr_clean 开头找最长公共子串
    max_check = min(len(prev_clean), len(curr_clean), 80)
    best_match = 0
    for match_len in range(max_check, min_match - 1, -1):
        if prev_clean[-match_len:] == curr_clean[:match_len]:
            best_match = match_len
            break

    if best_match == 0:
        return curr_text

    # 在原始 curr_text 中定位截断位置：跳过 best_match 个实质字符（含标点）
    count = 0
    cut_pos = 0
    for i, ch in enumerate(curr_text):
        if _PUNCT_RE.fullmatch(ch):
            continue
        count += 1
        if count == best_match:
            cut_pos = i + 1
            break

    return curr_text[cut_pos:].lstrip()


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
            overlap_seconds=options.audio_overlap_seconds,
        )
    return FakeAsrBackend([])
