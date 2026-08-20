"""日志系统配置模块。

提供双通道日志（控制台 + 文件）、视频名前缀注入、错误堆栈完整记录。
控制台输出简洁格式（适合实时查看），文件输出详细格式（含模块名和时间戳，适合回查）。
"""
from __future__ import annotations

import contextvars
import logging
from datetime import datetime
from pathlib import Path

# 当前处理的视频名，用于在日志中标注来源
_current_video: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_video", default="",
)

# 仅跟踪本模块创建的 Handler，避免覆盖或关闭宿主程序自己的日志配置。
_managed_handlers: list[logging.Handler] = []


class VideoNameFilter(logging.Filter):
    """给每条日志记录注入当前处理的视频名前缀。"""

    def filter(self, record: logging.LogRecord) -> bool:
        video = _current_video.get("")
        record.video_name = f"[{video}] " if video else ""
        return True


def set_current_video(name: str) -> None:
    """设置当前处理的视频名，后续日志自动带上前缀。

    批量处理时在每个视频开始前调用，单个视频处理完后可重置为空。
    使用 ContextVar 保证异步/多线程场景下视频名不会串。
    """
    _current_video.set(name)


def get_current_video() -> str:
    """获取当前正在处理的视频名。"""
    return _current_video.get("")


def shutdown_logging() -> None:
    """刷新并关闭本模块创建的日志 Handler。

    CLI 可以在同一 Python 进程中被测试或重复调用。显式关闭 FileHandler
    能避免 Windows 上日志文件持续占用，也不会影响宿主程序安装的 Handler。
    """
    root = logging.getLogger()
    while _managed_handlers:
        handler = _managed_handlers.pop()
        root.removeHandler(handler)
        try:
            handler.flush()
        finally:
            handler.close()
    set_current_video("")


def setup_logging(log_dir: Path, *, console_level: int = logging.INFO) -> Path:
    """配置全局日志系统，同时输出到控制台和文件。

    Args:
        log_dir: 日志文件存放目录（如 output/logs/）
        console_level: 控制台最低日志级别（文件始终为 DEBUG）

    Returns:
        日志文件的完整路径
    """
    # 重复调用时先释放上一次创建的文件句柄。
    shutdown_logging()
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"parse_{timestamp}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # 控制台格式：简洁，只有时间 + 级别 + 视频名 + 消息
    console_fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(video_name)s%(message)s",
        datefmt="%H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(console_fmt)
    console.addFilter(VideoNameFilter())
    root.addHandler(console)
    _managed_handlers.append(console)

    # 文件格式：详细，含日期时间 + 模块名 + 视频名 + 消息
    # 异常堆栈由 logging 模块在 exc_info 设置时自动追加，无需写入 format 字符串
    file_fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] %(video_name)s%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_fmt)
    file_handler.addFilter(VideoNameFilter())
    root.addHandler(file_handler)
    _managed_handlers.append(file_handler)

    # 降低第三方库的日志噪音
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    return log_file


def get_logger(name: str) -> logging.Logger:
    """获取指定名称的 logger，自动继承全局配置。"""
    return logging.getLogger(name)
