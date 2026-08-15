from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from PIL import Image

from . import ffmpeg as ffmpeg_module
from .models import Keyframe, SelectionReason
from .options import VideoParseOptions


@dataclass
class _CandidateFrame:
    """关键帧抽取过程中的候选帧，记录时间戳、文件路径与入选原因。"""

    timestamp_ms: int
    path: Path
    selection_reason: str
    scene_score: float | None


def _compute_dhash(image_path: Path, hash_size: int = 8) -> str:
    """计算图像的差异哈希（dHash），用于相邻关键帧去重。"""
    with Image.open(image_path) as img:
        # 转灰度并缩放，保留亮度分布信息
        img = img.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
        pixels = list(img.tobytes())
        width = hash_size + 1
        difference = []
        # 比较每行相邻像素的亮度，得到二值差分图
        for row in range(hash_size):
            for col in range(hash_size):
                left = pixels[row * width + col]
                right = pixels[row * width + col + 1]
                difference.append(left > right)
        # 每 4 位拼成一个十六进制字符，压缩存储
        hex_str = ""
        for i in range(0, len(difference), 4):
            nibble = difference[i:i + 4]
            val = sum(b << j for j, b in enumerate(nibble))
            hex_str += f"{val:x}"
        return hex_str


def _hamming_distance(h1: str, h2: str) -> int:
    """计算两个十六进制哈希串之间的汉明距离，衡量图像相似度。"""
    return sum(c1 != c2 for c1, c2 in zip(h1, h2))


def _extract_scene_frames(
    source: Path,
    output_dir: Path,
    threshold: float,
) -> list[_CandidateFrame]:
    """利用 ffmpeg 场景检测筛选出画面发生显著变化的帧作为候选关键帧。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(output_dir / "scene-%04d.jpg")

    # select 过滤场景变化得分大于阈值的帧；showinfo 输出时间戳供后续解析
    result = ffmpeg_module.run_ffmpeg([
        "-i", str(source),
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-vsync", "0",
        "-q:v", "2",
        "-y", pattern,
    ])

    if result.returncode != 0:
        return []

    # 从 stderr 中解析每帧的时间戳，与生成的图片按序对应
    timestamps = ffmpeg_module.parse_showinfo_stderr(result.stderr)
    candidates: list[_CandidateFrame] = []

    scene_files = sorted(output_dir.glob("scene-*.jpg"))
    for ts, fpath in zip(timestamps, scene_files):
        candidates.append(_CandidateFrame(
            timestamp_ms=int(ts * 1000),
            path=fpath,
            selection_reason="scene_change",
            scene_score=None,
        ))

    return candidates


def _extract_interval_frames(
    source: Path,
    output_dir: Path,
    interval_seconds: float,
    max_frames: int,
    duration_ms: int | None,
) -> list[_CandidateFrame]:
    """按固定时间间隔均匀采样帧，作为场景检测失败时的兜底策略。"""
    del max_frames
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(output_dir / "interval-%04d.jpg")

    # 通过低 fps 采样实现等间隔抽帧
    fps = 1.0 / interval_seconds
    result = ffmpeg_module.run_ffmpeg([
        "-i", str(source),
        "-vf", f"fps={fps}",
        "-q:v", "2",
        "-y", pattern,
    ])

    if result.returncode != 0:
        return []

    interval_files = sorted(output_dir.glob("interval-*.jpg"))
    candidates: list[_CandidateFrame] = []

    for i, fpath in enumerate(interval_files):
        ts_ms = int(i * interval_seconds * 1000)
        # 超过实际时长的尾部帧丢弃，避免时间戳越界
        if duration_ms is not None and ts_ms > duration_ms:
            break
        candidates.append(_CandidateFrame(
            timestamp_ms=ts_ms,
            path=fpath,
            selection_reason="interval_fallback",
            scene_score=None,
        ))

    return candidates


def extract_keyframes(
    source: Path,
    output_dir: Path,
    options: VideoParseOptions,
    duration_ms: int | None,
) -> list[Keyframe]:
    """从视频中抽取关键帧：优先场景检测，失败时降级为等间隔采样，再做去重与限数。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    keyframes_dir = output_dir / "keyframes"
    keyframes_dir.mkdir(parents=True, exist_ok=True)

    scene_dir = output_dir / "_scene_frames"
    interval_dir = output_dir / "_interval_frames"

    # 优先使用场景检测；无结果时回退到等间隔采样
    candidates = _extract_scene_frames(source, scene_dir, options.scene_threshold)

    if not candidates:
        candidates = _extract_interval_frames(
            source, interval_dir, options.fallback_frame_interval_seconds,
            options.max_keyframes, duration_ms,
        )

    if not candidates:
        return []

    candidates.sort(key=lambda c: c.timestamp_ms)

    # 候选数超过上限时按等步长抽样，并强制保留首尾帧以保证时间轴覆盖完整
    if len(candidates) > options.max_keyframes:
        step = len(candidates) / options.max_keyframes
        sampled: list[_CandidateFrame] = []
        for i in range(options.max_keyframes):
            idx = min(int(i * step), len(candidates) - 1)
            sampled.append(candidates[idx])
        if sampled[0] is not candidates[0]:
            sampled[0] = candidates[0]
        if sampled[-1] is not candidates[-1]:
            sampled[-1] = candidates[-1]
        candidates = sampled

    kept: list[Keyframe] = []
    last_hash: str | None = None

    for cand in candidates:
        try:
            with Image.open(cand.path) as img:
                width, height = img.size
                # 超过最大宽度时按比例缩放，控制输出体积
                if width > options.keyframe_max_width:
                    ratio = options.keyframe_max_width / width
                    new_size = (options.keyframe_max_width, int(height * ratio))
                    img = img.resize(new_size, Image.Resampling.LANCZOS)
                    width, height = new_size

                # 候选帧可能被去重，因此文件名必须使用“已保留帧”序号。
                frame_number = len(kept) + 1
                final_path = keyframes_dir / f"frame-{frame_number:04d}.jpg"
                img.convert("RGB").save(final_path, "JPEG", quality=90)

                phash = _compute_dhash(final_path)

                # 与上一保留帧相似度过高则跳过并删除文件，避免重复内容
                if last_hash is not None and len(phash) == len(last_hash):
                    dist = _hamming_distance(phash, last_hash)
                    if dist <= options.dedup_hamming_threshold:
                        final_path.unlink(missing_ok=True)
                        continue

                kept.append(Keyframe(
                    id=f"frame-{frame_number:04d}",
                    timestamp_ms=cand.timestamp_ms,
                    image_path=f"assets/keyframes/frame-{frame_number:04d}.jpg",
                    selection_reason=cast(SelectionReason, cand.selection_reason),
                    scene_score=cand.scene_score,
                    width=width,
                    height=height,
                    perceptual_hash=phash,
                ))
                last_hash = phash

        except Exception:
            continue

    return kept
