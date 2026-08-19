from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from PIL import Image, ImageChops

from . import ffmpeg as ffmpeg_module
from .logging_config import get_logger
from .models import Keyframe, SelectionReason
from .options import VideoParseOptions

logger = get_logger(__name__)


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


# dHash 判定相似后，像素差异占比低于此阈值才真正丢弃，防止误杀改字帧
_DEDUP_PIXEL_DIFF_RATIO = 0.03
# 亮度差超过此值才算"有变化的像素"
_PIXEL_DIFF_LUMINANCE = 30


def _pixel_diff_ratio(path_a: Path, path_b: Path) -> float:
    """计算两张图片的像素级差异占比（0.0~1.0）。

    转灰度后统一尺寸，逐像素比较亮度，差值超过 _PIXEL_DIFF_LUMINANCE 的像素占比。
    用于 dHash 判定相似后的二次确认：PPT 改几个字时 dHash 几乎不变，
    但改字区域的像素差异明显，能避免误杀。
    """
    with Image.open(path_a) as img_a, Image.open(path_b) as img_b:
        # 统一尺寸以保证逐像素可比
        if img_a.size != img_b.size:
            img_b = img_b.resize(img_a.size, Image.Resampling.LANCZOS)
        g_a = img_a.convert("L")
        g_b = img_b.convert("L")
        diff = ImageChops.difference(g_a, g_b)
        hist = diff.histogram()
        total = sum(hist)
        if total == 0:
            return 0.0
        changed = sum(hist[i] for i in range(_PIXEL_DIFF_LUMINANCE, 256))
        return changed / total


def _extract_scene_frames(
    source: Path,
    output_dir: Path,
    threshold: float,
) -> list[_CandidateFrame]:
    """利用 ffmpeg 场景检测筛选出画面发生显著变化的帧作为候选关键帧。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(output_dir / "scene-%04d.jpg")

    # select 过滤场景变化得分大于阈值的帧；showinfo 输出时间戳供后续解析
    # 注意：必须用 -loglevel info 覆盖 run_ffmpeg 默认的 -loglevel error，
    # 否则 showinfo 的输出被抑制，时间戳解析为空，导致场景检测被误判为失败
    result = ffmpeg_module.run_ffmpeg([
        "-loglevel", "info",
        "-i", str(source),
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-vsync", "0",
        "-q:v", "2",
        "-y", pattern,
    ])

    if result.returncode != 0:
        logger.warning("ffmpeg 场景检测执行失败 (returncode=%d)", result.returncode)
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

    logger.info(
        "场景检测: 阈值=%.2f, 生成图片 %d, 时间戳 %d, 配对候选 %d",
        threshold, len(scene_files), len(timestamps), len(candidates),
    )
    if len(scene_files) != len(timestamps):
        logger.warning(
            "场景检测图片数(%d)与时间戳数(%d)不匹配，可能丢失部分候选帧",
            len(scene_files), len(timestamps),
        )
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
        logger.info("场景检测无候选帧，回退到等间隔采样（间隔 %.1fs）", options.fallback_frame_interval_seconds)
        candidates = _extract_interval_frames(
            source, interval_dir, options.fallback_frame_interval_seconds,
            options.max_keyframes, duration_ms,
        )

    if not candidates:
        logger.warning("关键帧抽取失败: 场景检测和间隔采样均无结果")
        return []

    candidates.sort(key=lambda c: c.timestamp_ms)

    # 候选数超过上限时按等步长抽样，并强制保留首尾帧以保证时间轴覆盖完整
    if len(candidates) > options.max_keyframes:
        logger.info(
            "候选帧 %d 超过上限 %d，按等步长抽样",
            len(candidates), options.max_keyframes,
        )
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
    last_frame_path: Path | None = None
    # 去重统计：dHash 丢弃数、像素救回数
    dedup_skipped = 0
    dedup_rescued = 0

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

                # 候选帧可能被去重，因此文件名必须使用"已保留帧"序号。
                frame_number = len(kept) + 1
                final_path = keyframes_dir / f"frame-{frame_number:04d}.jpg"
                img.convert("RGB").save(final_path, "JPEG", quality=90)

                phash = _compute_dhash(final_path)

                # 与上一保留帧相似度过高时，再做像素级二次确认
                if last_hash is not None and len(phash) == len(last_hash):
                    dist = _hamming_distance(phash, last_hash)
                    if dist <= options.dedup_hamming_threshold:
                        # dHash 判定相似，但可能是"PPT 改几个字"的误判，
                        # 用像素级差异做二次确认：差异 < 3% 才真正丢弃
                        should_skip = True
                        if last_frame_path is not None:
                            try:
                                diff_ratio = _pixel_diff_ratio(final_path, last_frame_path)
                                if diff_ratio > _DEDUP_PIXEL_DIFF_RATIO:
                                    # 像素差异明显，保留这帧（dHash 误判）
                                    should_skip = False
                                    dedup_rescued += 1
                                    logger.debug(
                                        "帧 %s 被 dHash 判定相似(hamming=%d)但像素差异 %.1f%%，保留",
                                        final_path.name, dist, diff_ratio * 100,
                                    )
                            except Exception:
                                pass  # 像素比较失败时退回 dHash 判定
                        if should_skip:
                            dedup_skipped += 1
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
                last_frame_path = final_path

        except Exception:
            logger.debug("候选帧处理失败: %s", cand.path.name, exc_info=True)
            continue

    logger.info(
        "关键帧去重: 候选 %d → 保留 %d（dHash 丢弃 %d，像素救回 %d）",
        len(candidates), len(kept), dedup_skipped, dedup_rescued,
    )
    return kept
