"""外部模型 Provider 配置加载模块。

从 .env 文件与系统环境变量中读取 API Key、Base URL 及各阶段使用的模型名称，
供 ASR / VLM / 分段等下游组件统一引用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


@dataclass
class ProviderConfig:
    """Provider 相关配置的不可变容器。"""

    openai_api_key: str
    openai_base_url: str
    asr_model: str       # 语音识别模型
    vlm_model: str       # 视觉语言模型，用于关键帧理解
    section_model: str   # 内容分段与摘要模型


def default_user_config_path() -> Path:
    """返回跨平台用户级配置文件路径，不依赖 Skill 或包的安装目录。"""
    if os.name == "nt":
        return Path.home() / ".video-parser" / "config.env"
    config_root = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_root / "video-parser" / "config.env"


def _read_config_file(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    return {
        key: str(value)
        for key, value in dotenv_values(path).items()
        if value is not None
    }


def load_provider_config(
    env_file: Path | None = None,
    *,
    include_default_files: bool = True,
) -> ProviderConfig:
    """按环境变量、显式文件、用户配置和兼容 .env 的顺序读取配置。"""
    if env_file is not None:
        env_file = Path(env_file).expanduser()
        if not env_file.is_file():
            raise FileNotFoundError(f"Config file does not exist: {env_file}")

    sources: list[dict[str, str]] = []
    if env_file is not None:
        sources.append(_read_config_file(env_file))
    if include_default_files:
        user_path = default_user_config_path()
        if env_file is None or user_path.resolve() != env_file.resolve():
            sources.append(_read_config_file(user_path))
        sources.append(_read_config_file(Path.cwd() / ".env"))

    def value(primary: str, legacy: str, default: str = "") -> str:
        environment_value = os.getenv(primary) or os.getenv(legacy)
        if environment_value:
            return environment_value
        for source in sources:
            configured = source.get(primary) or source.get(legacy)
            if configured:
                return configured
        return default

    return ProviderConfig(
        openai_api_key=value("VIDEO_PARSER_API_KEY", "OPENAI_API_KEY"),
        openai_base_url=value("VIDEO_PARSER_BASE_URL", "OPENAI_BASE_URL"),
        asr_model=value(
            "VIDEO_PARSER_ASR_MODEL", "ASR_MODEL", "qwen3-asr-flash",
        ),
        vlm_model=value(
            "VIDEO_PARSER_VLM_MODEL", "VLM_MODEL", "qwen3-vl-flash",
        ),
        section_model=value(
            "VIDEO_PARSER_SUMMARY_MODEL", "SECTION_MODEL", "qwen3.7-plus",
        ),
    )
