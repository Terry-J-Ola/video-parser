from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg as ffmpeg_module
from .errors import MediaProcessingError


@dataclass
class AudioChunk:
    """音频分片信息，记录单个分片的文件路径及其在原始音频中的时间区间。"""

    path: Path
    start_ms: int
    end_ms: int


@dataclass
class AudioArtifact:
    """从视频中抽取出的完整音频产物，包含文件路径与总时长。"""

    path: Path
    duration_ms: int


def extract_audio(source: Path, output_path: Path) -> AudioArtifact | None:
    """从视频源中抽取音轨，转码为单声道 16kHz MP3；无音轨时返回 None。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # -vn 丢弃视频流；统一为单声道、16kHz 采样率、64k 码率，适配 ASR 输入要求
    result = ffmpeg_module.run_ffmpeg([
        "-i", str(source),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-b:a", "64k",
        "-y", str(output_path),
    ])

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        # 当源文件不含音轨时静默返回 None，其它错误才视为处理失败
        if "no sound" in stderr.lower() or "audio" not in stderr.lower():
            return None
        raise MediaProcessingError(f"audio extraction failed: {stderr[:500]}")

    # 部分场景下 ffmpeg 返回成功但未产出有效文件，需要二次校验
    if not output_path.exists() or output_path.stat().st_size == 0:
        return None

    duration = ffmpeg_module.probe_duration(output_path)
    if duration is None:
        duration = 0

    return AudioArtifact(path=output_path, duration_ms=duration)


def chunk_audio(
    audio_path: Path,
    output_dir: Path,
    chunk_seconds: int,
    total_duration_ms: int,
    overlap_seconds: float = 0.0,
) -> list[AudioChunk]:
    """按固定时长将整段音频切分为多个 MP3 分片，供 ASR 逐段转写。

    相邻分片间可叠加 overlap_seconds 秒的重叠区，避免句子在分片边界处被截断。
    例如 chunk_seconds=30、overlap_seconds=2 时：
      chunk 0: [0,    30s)
      chunk 1: [28s,  58s)   ← 前 2 秒与 chunk 0 重叠
      chunk 2: [56s,  86s)   ← 前 2 秒与 chunk 1 重叠
    步进 = chunk_seconds - overlap_seconds，每个分片实际时长仍为 chunk_seconds。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[AudioChunk] = []

    chunk_ms = chunk_seconds * 1000
    overlap_ms = int(overlap_seconds * 1000)
    # 步进 = 分片时长 - 重叠时长；重叠为 0 时退化为原始无重叠行为
    step_ms = chunk_ms - overlap_ms if overlap_ms > 0 else chunk_ms
    start_ms = 0
    idx = 0

    while start_ms < total_duration_ms:
        end_ms = min(start_ms + chunk_ms, total_duration_ms)
        chunk_path = output_dir / f"chunk-{idx:04d}.mp3"

        # -ss 指定起始时间，-t 指定持续时长，实现精确切片
        result = ffmpeg_module.run_ffmpeg([
            "-i", str(audio_path),
            "-ss", f"{start_ms / 1000:.3f}",
            "-t", f"{(end_ms - start_ms) / 1000:.3f}",
            "-ac", "1",
            "-ar", "16000",
            "-b:a", "64k",
            "-y", str(chunk_path),
        ])

        # 仅当切片成功且文件非空时才纳入结果，避免空分片干扰后续处理
        if result.returncode == 0 and chunk_path.exists() and chunk_path.stat().st_size > 0:
            chunks.append(AudioChunk(path=chunk_path, start_ms=start_ms, end_ms=end_ms))

        # 最后一个分片已覆盖到末尾，停止切分
        if end_ms >= total_duration_ms:
            break
        start_ms = end_ms - overlap_ms if overlap_ms > 0 else end_ms
        idx += 1

    return chunks
