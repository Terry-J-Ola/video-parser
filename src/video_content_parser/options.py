"""视频解析流程的可调运行参数定义。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class VideoParseOptions(BaseModel):
    """视频解析的可配置参数集合。

    通过 Pydantic 的 Field 约束对每个参数限定取值范围，
    在构造时即完成校验，避免错误参数进入下游处理。
    """

    language: str | None = None                              # 转写语言提示，None 表示自动检测
    audio_chunk_seconds: int = Field(default=30, ge=15, le=60)                # 单段音频切片时长（秒）
    audio_overlap_seconds: float = Field(default=2.0, ge=0.0, le=10.0)        # 相邻 chunk 间的重叠时长（秒），防止截断句子
    scene_threshold: float = Field(default=0.30, ge=0.0, le=1.0)              # 场景切换判定阈值，越小越敏感
    fallback_frame_interval_seconds: float = Field(default=10.0, gt=0)        # 场景检测失败时的固定抽帧间隔（秒）
    max_keyframes: int = Field(default=60, ge=1, le=200)                      # 单视频最大保留关键帧数量
    keyframe_max_width: int = Field(default=1280, ge=320, le=1920)            # 关键帧缩放的最大宽度（像素）
    dedup_hamming_threshold: int = Field(default=6, ge=0, le=64)              # 感知哈希汉明距离去重阈值
    frames_per_analysis_request: int = Field(default=6, ge=1, le=10)          # 单次 VLM 分析请求携带的帧数
    vlm_concurrency: int = Field(default=4, ge=1, le=16)                      # VLM 批次最大并发请求数
    max_model_retries: int = Field(default=3, ge=1, le=5)                     # 模型调用失败最大重试次数
