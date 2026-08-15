"""FFmpeg 命令执行的封装工具模块。

提供可执行文件定位、子进程调用、可用性检查、时长探测
以及 showinfo 滤镜输出解析等底层能力，供上层媒体处理流程复用。
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
from typing import Sequence

import imageio_ffmpeg

from .errors import MediaProcessingError


def get_ffmpeg_exe() -> str:
    """返回内置的 FFmpeg 可执行文件绝对路径。"""
    return imageio_ffmpeg.get_ffmpeg_exe()


def run_ffmpeg(args: Sequence[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    """以子进程方式运行 FFmpeg，统一注入静默参数并捕获异常。"""
    exe = get_ffmpeg_exe()
    # -hide_banner 隐藏版权横幅，-loglevel error 只输出错误，便于解析失败原因
    cmd = [exe, "-hide_banner", "-loglevel", "error", *args]
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise MediaProcessingError(f"FFmpeg not found: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise MediaProcessingError(f"FFmpeg timed out after {timeout}s") from e


def check_ffmpeg_available() -> None:
    """探活 FFmpeg，不可用时抛出 MediaProcessingError。"""
    try:
        result = run_ffmpeg(["-version"])
        if result.returncode != 0:
            raise MediaProcessingError(f"FFmpeg failed: {result.stderr.decode(errors='replace')}")
    except MediaProcessingError:
        raise
    except Exception as e:
        raise MediaProcessingError(f"FFmpeg not available: {e}") from e


def probe_duration(source: Path) -> int | None:
    """通过解析 FFmpeg 输入信息获取视频时长（毫秒），失败返回 None。"""
    exe = get_ffmpeg_exe()
    # 仅传入 -i 不带输出，FFmpeg 会因缺少输出而退出，但时长信息会打印到 stderr
    cmd = [exe, "-i", str(source)]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except Exception:
        return None

    text = result.stderr.decode(errors="replace") + result.stdout.decode(errors="replace")
    import re
    # 匹配形如 "Duration: 00:01:23.45" 的时长字段
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", text)
    if m:
        hours = int(m.group(1))
        minutes = int(m.group(2))
        seconds = float(m.group(3))
        total_ms = int((hours * 3600 + minutes * 60 + seconds) * 1000)
        return total_ms
    return None


# 匹配 showinfo 滤镜输出中的 pts_time 字段，用于定位每一帧的精确时间戳
_SHOWINFO_PTS = re.compile(r"pts_time:([\d.]+)")


def parse_showinfo_stderr(stderr: bytes) -> list[float]:
    """从 showinfo 滤镜的 stderr 输出中提取全部帧时间戳（秒）。"""
    text = stderr.decode(errors="replace")
    return [float(m) for m in _SHOWINFO_PTS.findall(text)]
