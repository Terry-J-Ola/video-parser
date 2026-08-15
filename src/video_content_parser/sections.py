"""章节构建模块。

将 ASR 转写片段与关键帧分析结果聚合为语义章节(Section)。
支持基于大模型的智能分章与确定性的时间窗口回退分章两种策略。
"""
from __future__ import annotations

import json
import re
from typing import Protocol

from .models import (
    FrameAnalysis,
    LearnerDocument,
    LearnerSection,
    ModelTokenUsage,
    Quote,
    Section,
    TranscriptSegment,
    Warning,
)
from .omni import collect_stream_text, parse_json_object


_INTERNAL_TEXT_MARKERS = (
    "处理状态",
    "模型名称",
    "告警",
    "讲者原意",
    "画面信息",
    "技术证据",
    "证据 ID",
    "证据ID",
    "transcript_segment_id",
    "keyframe_id",
)
_TIMESTAMP_PATTERN = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)")


def _contains_internal_text(text: str, evidence_ids: set[str]) -> bool:
    """判断模型可见文本是否泄露时间轴或内部证据信息。"""
    return (
        bool(_TIMESTAMP_PATTERN.search(text))
        or any(marker in text for marker in _INTERNAL_TEXT_MARKERS)
        or any(evidence_id in text for evidence_id in evidence_ids)
    )


class SectionBuilder(Protocol):
    """章节构建器协议，定义统一的章节生成接口。"""

    def build(
        self,
        transcripts: list[TranscriptSegment],
        frame_analyses: list[FrameAnalysis],
    ) -> tuple[list[Section], LearnerDocument | None, list[Warning], ModelTokenUsage]: ...


class ChatSectionBuilder:
    """基于大语言模型的章节构建器。

    通过对话模型把转写片段和关键帧分析整理成语义章节；
    当模型调用失败或返回无效结果时，回退到确定性时间窗口分章。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        max_retries: int = 3,
        fallback_window_seconds: int = 60,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_retries = max_retries
        self.fallback_window_seconds = fallback_window_seconds

    def build(
        self,
        transcripts: list[TranscriptSegment],
        frame_analyses: list[FrameAnalysis],
    ) -> tuple[list[Section], LearnerDocument | None, list[Warning], ModelTokenUsage]:
        """一次生成兼容章节和业务文章，失败时仅回退兼容章节。"""
        from openai import OpenAI

        total_usage = ModelTokenUsage()

        # 无任何输入证据时直接返回空结果
        if not transcripts and not frame_analyses:
            return [], None, [], total_usage

        warnings: list[Warning] = []
        last_error: Exception | None = None

        try:
            client = OpenAI(api_key=self.api_key, base_url=self.base_url)

            # 构造模型输入：转写片段与关键帧分析的精简字段
            input_data = {
                "transcripts": [
                    {
                        "id": t.id,
                        "start_ms": t.start_ms,
                        "end_ms": t.end_ms,
                        "text": t.text,
                    }
                    for t in transcripts
                ],
                "frames": [
                    {
                        "keyframe_id": f.keyframe_id,
                        "timestamp_ms": f.timestamp_ms,
                        "visible_text": f.visible_text,
                        "description": f.description,
                    }
                    for f in frame_analyses
                ],
            }

            # 同一次模型调用既保留结构化章节，也生成忠实的视频概要。
            prompt = (
                "请只基于下面提供的转写和画面分析，同时生成兼容语义章节 sections "
                "和视频内容概要 learner_document。"
                "sections 的每章必须包含 title、summary、paragraphs、quotes、"
                "transcript_segment_ids、keyframe_ids；quotes.text 必须保留转写原文。"
                "learner_document 必须包含 title、introduction、sections、key_takeaways。"
                "其中概要的每章必须包含 title、paragraphs、transcript_segment_ids、"
                "keyframe_ids、illustration_keyframe_id。"
                "概要只回答视频讲了什么，并按视频实际内容归纳主题、观点、事实、"
                "示例和明确讲到的步骤；短视频可以只有一个主题，篇幅随证据量自适应。"
                "不得补充输入证据没有表达的原因、价值、方法、建议、背景或结论，"
                "不得把常识、行业知识或模型推测写成视频内容。"
                "如果证据没有讲为什么或如何执行，就不要补写这些内容。"
                "保留证据中明确出现的人名、术语、数字、条件和先后关系，不夸大含义。"
                "不得在概要文本中输出时间戳、证据 ID、处理状态、模型信息、告警、"
                "讲者原意、画面信息或技术证据标签。"
                "所有章节必须引用输入中真实存在的证据 ID，不得编造事实或 ID。"
                "概要中的证据 ID 只供程序校验，不会渲染。"
                "业务讲义只输出文字概要，因此 illustration_keyframe_id 必须为 null。"
                "标题、概要、主要内容和核心要点均使用中文。只返回 JSON 对象。\n\n"
                f"输入证据：{json.dumps(input_data, ensure_ascii=False)}"
            )

            # 重试机制：模型可能偶发返回空内容或非法 JSON
            for attempt in range(self.max_retries):
                try:
                    stream = client.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "user", "content": prompt}],
                        response_format={"type": "json_object"},
                        extra_body={"enable_thinking": False},
                        stream=True,
                        stream_options={"include_usage": True},
                    )
                    raw, usage = collect_stream_text(stream)
                    total_usage.add(usage)
                    # 空响应直接进入下一次重试
                    if not raw:
                        continue
                    parsed = parse_json_object(raw)
                    sections_data = parsed.get("sections", [])
                    learner_data = parsed.get("learner_document")

                    sections = self._validate_and_build(
                        sections_data, transcripts, frame_analyses,
                    )
                    learner_document = self._validate_learner_document(
                        learner_data, transcripts, frame_analyses,
                    )
                    if sections and learner_document is not None:
                        return sections, learner_document, warnings, total_usage
                    last_error = ValueError("模型结果中没有可用章节或业务文章")

                except Exception as exc:
                    last_error = exc
                    if attempt == self.max_retries - 1:
                        break

        except Exception as exc:
            last_error = exc

        # 所有重试均失败，记录警告并走确定性回退
        warnings.append(Warning(
            code="SECTION_BUILDER_FALLBACK",
            message=(
                "章节模型生成失败，已使用确定性回退。"
                + (f"原因：{last_error}" if last_error else "")
            ),
        ))
        fallback_sections, fallback_warnings, _ = self._fallback_build(transcripts, frame_analyses)
        return fallback_sections, None, warnings + fallback_warnings, total_usage

    def _validate_learner_document(
        self,
        document_data: object,
        transcripts: list[TranscriptSegment],
        frame_analyses: list[FrameAnalysis],
    ) -> LearnerDocument | None:
        """过滤模型编造的证据 ID，并构造通过本地校验的业务文章。"""
        if not isinstance(document_data, dict):
            return None

        ts_ids = {item.id for item in transcripts}
        frame_ids = {item.keyframe_id for item in frame_analyses}
        evidence_ids = ts_ids | frame_ids
        learner_sections: list[LearnerSection] = []

        title = str(document_data.get("title", "")).strip()
        introduction = str(document_data.get("introduction", "")).strip()
        raw_takeaways = document_data.get("key_takeaways", [])
        if not isinstance(raw_takeaways, list):
            raw_takeaways = []
        takeaways = [str(item).strip() for item in raw_takeaways if str(item).strip()]
        if any(
            _contains_internal_text(text, evidence_ids)
            for text in [title, introduction, *takeaways]
        ):
            return None

        raw_sections = document_data.get("sections", [])
        if not isinstance(raw_sections, list):
            return None

        for raw_section in raw_sections:
            if not isinstance(raw_section, dict):
                continue
            raw_ts_ids = raw_section.get("transcript_segment_ids", [])
            raw_frame_ids = raw_section.get("keyframe_ids", [])
            if not isinstance(raw_ts_ids, list):
                raw_ts_ids = []
            if not isinstance(raw_frame_ids, list):
                raw_frame_ids = []
            section_title = str(raw_section.get("title", "")).strip()
            raw_paragraphs = raw_section.get("paragraphs", [])
            if not isinstance(raw_paragraphs, list):
                raw_paragraphs = []
            paragraphs = [str(item).strip() for item in raw_paragraphs if str(item).strip()]
            if any(
                _contains_internal_text(text, evidence_ids)
                for text in [section_title, *paragraphs]
            ):
                continue
            valid_ts = [item for item in raw_ts_ids if item in ts_ids]
            valid_frames = [item for item in raw_frame_ids if item in frame_ids]
            if not valid_ts and not valid_frames:
                continue

            illustration = raw_section.get("illustration_keyframe_id")
            if illustration not in valid_frames:
                illustration = None

            try:
                learner_sections.append(LearnerSection(
                    title=section_title,
                    paragraphs=paragraphs,
                    transcript_segment_ids=valid_ts,
                    keyframe_ids=valid_frames,
                    illustration_keyframe_id=illustration,
                ))
            except Exception:
                continue

        if not learner_sections:
            return None

        try:
            return LearnerDocument(
                title=title,
                introduction=introduction,
                sections=learner_sections,
                key_takeaways=takeaways,
            )
        except Exception:
            return None

    def _validate_and_build(
        self,
        sections_data: list[dict],
        transcripts: list[TranscriptSegment],
        frame_analyses: list[FrameAnalysis],
    ) -> list[Section]:
        """校验模型返回的章节并构造 Section 对象。

        主要做四件事：过滤引用了不存在 ID 的章节、用真实证据的时间戳
        重算章节起止时间、补齐缺失的摘要/正文、规范化 quotes 结构。
        """
        # 收集真实存在的证据 ID，用于过滤模型可能编造的引用
        ts_ids = {t.id for t in transcripts}
        fa_ids = {f.keyframe_id for f in frame_analyses}
        sections: list[Section] = []

        for i, sd in enumerate(sections_data):
            sec_ids = sd.get("transcript_segment_ids", [])
            kf_ids = sd.get("keyframe_ids", [])

            # 仅保留引用了真实证据的 ID
            valid_ts = [tid for tid in sec_ids if tid in ts_ids]
            valid_kf = [kid for kid in kf_ids if kid in fa_ids]

            # 没有任何有效证据的章节直接丢弃
            if not valid_ts and not valid_kf:
                continue

            # 用所有引用证据的时间戳推导章节起止时间
            all_times: list[int] = []
            for tid in valid_ts:
                t = next(t for t in transcripts if t.id == tid)
                all_times.extend([t.start_ms, t.end_ms])
            for kid in valid_kf:
                f = next(f for f in frame_analyses if f.keyframe_id == kid)
                all_times.append(f.timestamp_ms)

            if not all_times:
                continue

            start_ms = min(all_times)
            end_ms = max(all_times)

            # 模型若未给出段落，则用转写原文拼出兜底段落
            paragraphs = [p.strip() for p in sd.get("paragraphs", []) if p and p.strip()]
            if not paragraphs:
                combined = " ".join(
                    t.text for t in transcripts if t.id in valid_ts
                )
                paragraphs = [combined[:500]] if combined else ["(no text)"]

            # 兼容模型返回 quotes 为字符串或对象两种形式
            quotes_data = sd.get("quotes", [])
            quotes: list[Quote] = []
            for qd in quotes_data:
                if isinstance(qd, str):
                    qtext = qd.strip()
                    quote_ids = valid_ts
                elif isinstance(qd, dict):
                    qtext = str(qd.get("text", "")).strip()
                    quote_ids = [
                        tid for tid in qd.get("transcript_segment_ids", [])
                        if tid in ts_ids
                    ]
                else:
                    continue
                if qtext:
                    quotes.append(Quote(
                        text=qtext,
                        transcript_segment_ids=quote_ids,
                    ))

            # 标题/摘要缺失时用序号或正文前缀兜底
            title = sd.get("title", "").strip() or f"章节 {i + 1}"
            summary = sd.get("summary", "").strip() or paragraphs[0][:200]

            try:
                sections.append(Section(
                    id=f"section-{len(sections) + 1:04d}",
                    title=title,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    summary=summary,
                    paragraphs=paragraphs,
                    quotes=quotes,
                    transcript_segment_ids=valid_ts,
                    keyframe_ids=valid_kf,
                ))
            except Exception:
                # 单个章节构造失败不影响其他章节
                continue

        return sections

    def _fallback_build(
        self,
        transcripts: list[TranscriptSegment],
        frame_analyses: list[FrameAnalysis],
    ) -> tuple[list[Section], list[Warning], ModelTokenUsage]:
        """确定性回退分章：无转写时按关键帧分章，否则按固定时间窗口分章。"""
        # 没有转写文本：每个关键帧单独成一个章节
        if not transcripts:
            sections: list[Section] = []
            for fa in frame_analyses:
                sections.append(Section(
                    id=f"section-{len(sections) + 1:04d}",
                    title=fa.description[:50] or "视觉章节",
                    start_ms=fa.timestamp_ms,
                    end_ms=fa.timestamp_ms + 1000,
                    summary=fa.description,
                    paragraphs=[fa.description],
                    quotes=[],
                    transcript_segment_ids=[],
                    keyframe_ids=[fa.keyframe_id],
                ))
            return sections, [], ModelTokenUsage()

        # 按固定时间窗口对转写片段进行分组
        window_ms = self.fallback_window_seconds * 1000
        sections: list[Section] = []
        current_ts: list[TranscriptSegment] = []
        window_end = 0

        for t in transcripts:
            # 当前片段已超出窗口范围，则把已收集的片段封一个章节
            if not current_ts or t.start_ms >= window_end:
                if current_ts:
                    sections.append(self._window_to_section(current_ts, frame_analyses, sections))
                current_ts = [t]
                window_end = t.start_ms + window_ms
            else:
                # 仍在窗口内，追加并适当延展窗口边界
                current_ts.append(t)
                window_end = max(window_end, t.end_ms + window_ms)

        # 处理最后一组残留片段
        if current_ts:
            sections.append(self._window_to_section(current_ts, frame_analyses, sections))

        return sections, [], ModelTokenUsage()

    def _window_to_section(
        self,
        ts_list: list[TranscriptSegment],
        frame_analyses: list[FrameAnalysis],
        existing: list[Section],
    ) -> Section:
        """把一个时间窗口内的转写片段聚合为单个 Section。"""
        start_ms = ts_list[0].start_ms
        end_ms = ts_list[-1].end_ms

        # 落在窗口时间范围内的关键帧归入本章节
        matched_frames = [
            fa for fa in frame_analyses
            if start_ms <= fa.timestamp_ms <= end_ms
        ]

        # 标题取首句转写文本，过长则截断加省略号
        title = ts_list[0].text[:60]
        if len(title) >= 60:
            title += "..."

        paragraphs = [" ".join(t.text for t in ts_list)]

        return Section(
            id=f"section-{len(existing) + 1:04d}",
            title=title,
            start_ms=start_ms,
            end_ms=end_ms,
            summary=paragraphs[0][:200],
            paragraphs=paragraphs,
            # 仅取前两条片段作为代表性引用
            quotes=[Quote(text=t.text, transcript_segment_ids=[t.id]) for t in ts_list[:2]],
            transcript_segment_ids=[t.id for t in ts_list],
            keyframe_ids=[f.keyframe_id for f in matched_frames],
        )


class FakeSectionBuilder:
    """用于测试或无模型环境下的假章节构建器。

    可注入预设章节，或在没有真实模型时按数量均分源数据生成占位章节。
    """

    def __init__(
        self,
        sections: list[Section] | None = None,
        learner_document: LearnerDocument | None = None,
    ):
        self._sections = sections
        self._learner_document = learner_document

    def build(
        self,
        transcripts: list[TranscriptSegment],
        frame_analyses: list[FrameAnalysis],
    ) -> tuple[list[Section], LearnerDocument | None, list[Warning], ModelTokenUsage]:
        """优先返回注入的章节；否则把源数据均分成 4 份生成占位章节。"""
        # 已注入预设章节时直接深拷贝返回，避免外部修改污染内部状态
        if self._sections is not None:
            document = (
                self._learner_document.model_copy(deep=True)
                if self._learner_document is not None
                else None
            )
            return [s.model_copy() for s in self._sections], document, [], ModelTokenUsage()

        sections: list[Section] = []
        sources = transcripts or frame_analyses
        if not sources:
            return [], None, [], ModelTokenUsage()

        # 将源数据大致均分为 4 个章节
        chunk_size = max(1, len(sources) // 4)
        for idx in range(0, len(sources), chunk_size):
            chunk = sources[idx:idx + chunk_size]
            if not chunk:
                break
            # 通过 isinstance 区分转写片段与关键帧分析
            start_ms = chunk[0].start_ms if isinstance(chunk[0], TranscriptSegment) else chunk[0].timestamp_ms
            end_ms = (
                chunk[-1].end_ms if isinstance(chunk[-1], TranscriptSegment)
                else chunk[-1].timestamp_ms + 1000
            )
            seg_ids = [t.id for t in chunk if isinstance(t, TranscriptSegment)]
            kf_ids = [f.keyframe_id for f in chunk if isinstance(f, FrameAnalysis)]
            if not seg_ids and not kf_ids:
                continue
            title = f"章节 {len(sections) + 1}"
            sections.append(Section(
                id=f"section-{len(sections) + 1:04d}",
                title=title,
                start_ms=start_ms,
                end_ms=max(end_ms, start_ms + 1),
                summary=f"模拟章节总结：{title}",
                paragraphs=[f"这是 {title} 的内容段落。"],
                quotes=[],
                transcript_segment_ids=seg_ids,
                keyframe_ids=kf_ids,
            ))
        document = (
            self._learner_document.model_copy(deep=True)
            if self._learner_document is not None
            else None
        )
        return sections, document, [], ModelTokenUsage()


def create_section_builder(
    *,
    openai_api_key: str = "",
    openai_base_url: str = "",
    section_model: str = "",
    max_retries: int = 3,
) -> SectionBuilder:
    """工厂函数：配置齐全时返回 ChatSectionBuilder，否则返回 FakeSectionBuilder。"""
    has_chat = bool(openai_api_key and openai_base_url and section_model)
    if has_chat:
        return ChatSectionBuilder(
            base_url=openai_base_url,
            api_key=openai_api_key,
            model=section_model,
            max_retries=max_retries,
        )
    return FakeSectionBuilder([])
