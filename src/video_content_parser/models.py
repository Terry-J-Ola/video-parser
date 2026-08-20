"""视频解析结果的数据模型定义。

使用 Pydantic 描述从音频转写到关键帧分析、内容分段的完整结构，
并在模型层完成字段校验与跨实体引用一致性检查。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from . import __version__


# ---------- TranscriptSegment ----------

# 时间戳来源：provider_segment 表示 Provider 直接给出的精确分段，
# audio_chunk 表示仅能定位到所在音频切片的粗粒度时间
TimingSource = Literal["provider_segment", "audio_chunk"]


class TranscriptSegment(BaseModel):
    """语音转写得到的单段文本及其时间信息。"""

    id: str
    start_ms: int
    end_ms: int
    text: str
    language: str | None = None
    speaker: str | None = None
    confidence: float | None = None
    timing_source: TimingSource

    @field_validator("text")
    @classmethod
    def _text_non_empty(cls, v: str) -> str:
        """去除首尾空白后校验文本非空。"""
        v = v.strip()
        if not v:
            raise ValueError("text must not be empty")
        return v

    @model_validator(mode="after")
    def _time_range(self) -> "TranscriptSegment":
        """校验时间区间合法：start 非负且严格小于 end。"""
        if not (0 <= self.start_ms < self.end_ms):
            raise ValueError(f"invalid time range: {self.start_ms} - {self.end_ms}")
        return self


# ---------- Keyframe ----------

# 关键帧选取原因：scene_change 表示由场景切换检出，interval_fallback 表示固定间隔兜底抽帧
SelectionReason = Literal["scene_change", "interval_fallback"]


class Keyframe(BaseModel):
    """从视频中抽取的关键帧及其元信息。"""

    id: str
    timestamp_ms: int
    image_path: str
    selection_reason: SelectionReason
    scene_score: float | None = None
    width: int
    height: int
    perceptual_hash: str   # 感知哈希，用于帧间去重

    @field_validator("timestamp_ms")
    @classmethod
    def _ts_non_negative(cls, v: int) -> int:
        """时间戳必须非负。"""
        if v < 0:
            raise ValueError("timestamp_ms must be >= 0")
        return v


# ---------- FrameAnalysis ----------

# 画面内容类型枚举
ContentType = Literal["slide", "screen", "person", "product", "document", "scene", "unknown"]


class FrameAnalysis(BaseModel):
    """VLM 对单个关键帧的分析结果。"""

    keyframe_id: str
    timestamp_ms: int
    visible_text: list[str] = Field(default_factory=list)
    description: str
    content_type: ContentType = "unknown"
    confidence: float | None = None


# ---------- Section ----------

class Quote(BaseModel):
    """章节中引用的原话片段。"""

    text: str
    transcript_segment_ids: list[str] = Field(default_factory=list)


class Section(BaseModel):
    """视频内容的逻辑分段，聚合相关的转写片段与关键帧。"""

    id: str
    title: str
    start_ms: int
    end_ms: int
    summary: str
    paragraphs: list[str] = Field(default_factory=list)
    quotes: list[Quote] = Field(default_factory=list)
    transcript_segment_ids: list[str] = Field(default_factory=list)
    keyframe_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> "Section":
        """校验时间区间合法，且至少引用一条转写或一个关键帧。"""
        if not (0 <= self.start_ms < self.end_ms):
            raise ValueError(f"invalid section time range: {self.start_ms} - {self.end_ms}")
        if not self.transcript_segment_ids and not self.keyframe_ids:
            raise ValueError("section must reference at least one transcript or keyframe")
        return self


# ---------- LearnerDocument ----------

class LearnerSection(BaseModel):
    """业务讲义中的主题章节；证据 ID 仅用于校验，不直接展示。"""

    title: str
    paragraphs: list[str] = Field(default_factory=list)
    transcript_segment_ids: list[str] = Field(default_factory=list)
    keyframe_ids: list[str] = Field(default_factory=list)
    illustration_keyframe_id: str | None = None

    @model_validator(mode="after")
    def _validate_content(self) -> "LearnerSection":
        """正文必须可读、有证据，且插图必须来自本章关键帧。"""
        self.title = self.title.strip()
        self.paragraphs = [p.strip() for p in self.paragraphs if p.strip()]
        if not self.title or not self.paragraphs:
            raise ValueError("learner section requires title and paragraphs")
        if not self.transcript_segment_ids and not self.keyframe_ids:
            raise ValueError("learner section requires evidence")
        if (
            self.illustration_keyframe_id is not None
            and self.illustration_keyframe_id not in self.keyframe_ids
        ):
            raise ValueError("illustration must be a referenced keyframe")
        return self


class LearnerDocument(BaseModel):
    """按业务主题组织、可独立阅读的讲义文章。"""

    title: str
    introduction: str
    sections: list[LearnerSection]
    key_takeaways: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_content(self) -> "LearnerDocument":
        """文章必须具备标题、导语和至少一个有效主题章节。"""
        self.title = self.title.strip()
        self.introduction = self.introduction.strip()
        self.key_takeaways = [item.strip() for item in self.key_takeaways if item.strip()]
        if not self.title or not self.introduction or not self.sections:
            raise ValueError("learner document requires title, introduction and sections")
        return self


# ---------- Warning ----------

# 警告码枚举：覆盖音频缺失、ASR 失败、关键帧缺失、分段降级等异常情形
WarningCode = Literal[
    "NO_AUDIO_TRACK",
    "ASR_FAILED",
    "ASR_COARSE_TIMING",
    "NO_KEYFRAMES",
    "FRAME_ANALYSIS_FAILED",
    "SECTION_BUILDER_FAILED",
    "SECTION_BUILDER_FALLBACK",
    "DURATION_UNKNOWN",
    "CONTENT_PARTIAL",
]


class Warning(BaseModel):
    """流程中产生的非致命警告，附可选的时间区间。"""

    code: WarningCode
    message: str
    start_ms: int | None = None
    end_ms: int | None = None


# ---------- TokenUsage ----------

class ModelTokenUsage(BaseModel):
    """单个模块（ASR / VLM / SectionBuilder）的 token 用量统计。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # 实际调用的模型名，从流式响应的 chunk.model 提取（含快照版本号）。
    # 未调用真实模型（如 FakeBackend）时为空字符串。
    model: str = ""
    # VLM 多模态细节（可选）
    image_tokens: int = 0
    audio_tokens: int = 0
    # ASR 细节（可选）
    input_audio_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, other: "ModelTokenUsage") -> "ModelTokenUsage":
        """把另一个用量累加到当前实例上，并返回 self。

        model 字段：多个 chunk 累加时模型名应一致，self.model 已有值则保留；
        否则取 other.model（首次填充）。这避免多 chunk 场景下 model 被覆盖为空。
        """
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        if not self.model and other.model:
            self.model = other.model
        self.image_tokens += other.image_tokens
        self.audio_tokens += other.audio_tokens
        self.input_audio_tokens += other.input_audio_tokens
        self.reasoning_tokens += other.reasoning_tokens
        return self

    def add_usage_dict(self, usage: object) -> None:
        """从 OpenAI 兼容接口返回的 usage 对象中提取字段并累加。

        usage 可能是 dict 或带属性的对象，字段可能在顶层也可能在 prompt_tokens_details 中。
        """
        def getval(obj: object, key: str) -> int:
            if obj is None:
                return 0
            if isinstance(obj, dict):
                v = obj.get(key)
            else:
                v = getattr(obj, key, None)
            if isinstance(v, int):
                return v
            return 0

        self.prompt_tokens += getval(usage, "prompt_tokens")
        self.completion_tokens += getval(usage, "completion_tokens")
        self.total_tokens += getval(usage, "total_tokens")

        # OpenAI 多模态接口：prompt_tokens_details 可能嵌套 audio_tokens / image_tokens / cached_tokens
        if isinstance(usage, dict):
            details = usage.get("prompt_tokens_details")
        else:
            details = getattr(usage, "prompt_tokens_details", None)
        self.image_tokens += getval(details, "image_tokens")
        self.audio_tokens += getval(details, "audio_tokens")
        self.input_audio_tokens += getval(details, "input_audio_tokens")
        self.reasoning_tokens += getval(details, "reasoning_tokens")


class TokenUsage(BaseModel):
    """解析流程各模块的总 token 用量。"""

    asr: ModelTokenUsage = Field(default_factory=ModelTokenUsage)
    vlm: ModelTokenUsage = Field(default_factory=ModelTokenUsage)
    sections: ModelTokenUsage = Field(default_factory=ModelTokenUsage)

    @property
    def total_prompt_tokens(self) -> int:
        return self.asr.prompt_tokens + self.vlm.prompt_tokens + self.sections.prompt_tokens

    @property
    def total_completion_tokens(self) -> int:
        return self.asr.completion_tokens + self.vlm.completion_tokens + self.sections.completion_tokens

    @property
    def total_tokens(self) -> int:
        return self.asr.total_tokens + self.vlm.total_tokens + self.sections.total_tokens


# ---------- Artifacts ----------

class Artifacts(BaseModel):
    """解析过程中落盘的产物路径。"""

    audio_path: str | None
    evidence_markdown_path: str
    learner_markdown_path: str | None = None


# ---------- VideoParseResult ----------

ResultStatus = Literal["complete", "partial"]


class VideoParseResult(BaseModel):
    """视频解析的最终聚合结果，作为对外暴露的顶层结构。"""

    schema_version: str = "video_parse_result.v3"
    app_version: str = __version__
    runtime_package_path: str = Field(
        default_factory=lambda: str(Path(__file__).resolve().parent),
    )
    status: ResultStatus
    source_name: str
    duration_ms: int | None
    title: str
    languages: list[str] = Field(default_factory=list)
    summary: str
    artifacts: Artifacts
    transcript_segments: list[TranscriptSegment] = Field(default_factory=list)
    keyframes: list[Keyframe] = Field(default_factory=list)
    frame_analyses: list[FrameAnalysis] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    learner_document: LearnerDocument | None = None
    warnings: list[Warning] = Field(default_factory=list)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)

    @model_validator(mode="after")
    def _sort_and_check(self) -> "VideoParseResult":
        """构造后处理：按时间排序各实体，并校验跨实体引用的一致性。"""
        # 按时间排序，保证输出稳定
        self.transcript_segments.sort(key=lambda s: (s.start_ms, s.end_ms, s.id))
        self.keyframes.sort(key=lambda k: k.timestamp_ms)
        self.sections.sort(key=lambda s: (s.start_ms, s.end_ms, s.id))

        # 建立主键索引以校验引用合法性
        kf_ids = {k.id for k in self.keyframes}
        ts_ids = {t.id for t in self.transcript_segments}

        # 帧分析引用的关键帧必须存在
        for fa in self.frame_analyses:
            if fa.keyframe_id not in kf_ids:
                raise ValueError(f"frame_analysis references unknown keyframe: {fa.keyframe_id}")

        # 逐个章节校验引用合法性，并检查章节时间范围覆盖其证据时间
        for sec in self.sections:
            for tid in sec.transcript_segment_ids:
                if tid not in ts_ids:
                    raise ValueError(f"section references unknown transcript: {tid}")
            for kid in sec.keyframe_ids:
                if kid not in kf_ids:
                    raise ValueError(f"section references unknown keyframe: {kid}")

            # 收集该章节引用的全部证据时间点（转写片段起止 + 关键帧时间戳）
            evidence_times: list[int] = []
            for tid in sec.transcript_segment_ids:
                t = next(t for t in self.transcript_segments if t.id == tid)
                evidence_times.extend([t.start_ms, t.end_ms])
            for kid in sec.keyframe_ids:
                k = next(k for k in self.keyframes if k.id == kid)
                evidence_times.append(k.timestamp_ms)

            # 章节时间范围必须包住所有证据时间，否则视为错误分段
            if evidence_times:
                if sec.start_ms > min(evidence_times):
                    raise ValueError(f"section {sec.id} start_ms after earliest evidence")
                if sec.end_ms < max(evidence_times):
                    raise ValueError(f"section {sec.id} end_ms before latest evidence")

        # 业务讲义的内部证据引用也必须能回溯到真实实体。
        if self.learner_document is not None:
            for sec in self.learner_document.sections:
                for tid in sec.transcript_segment_ids:
                    if tid not in ts_ids:
                        raise ValueError(
                            f"learner section references unknown transcript: {tid}"
                        )
                for kid in sec.keyframe_ids:
                    if kid not in kf_ids:
                        raise ValueError(
                            f"learner section references unknown keyframe: {kid}"
                        )

        return self
