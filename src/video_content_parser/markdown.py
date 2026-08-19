"""技术证据稿与业务讲义的确定性 Markdown 渲染器。"""
from __future__ import annotations

from .models import VideoParseResult


def _format_time(ms: int | None) -> str:
    """毫秒时间戳格式化为 HH:MM:SS 形式；None 视为 0。"""
    if ms is None:
        return "00:00:00"
    total_seconds = ms // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _format_range(start_ms: int, end_ms: int) -> str:
    """把起止时间戳格式化为区间字符串，如 00:01:23–00:02:10。"""
    return f"{_format_time(start_ms)}–{_format_time(end_ms)}"


def _escape_markdown(text: str) -> str:
    """Markdown 转义占位函数，目前原样返回，预留扩展点。"""
    return text


def render_learner_markdown(result: VideoParseResult) -> str:
    """把已校验内容渲染为忠实、简洁且不暴露证据元数据的视频概要。"""
    document = result.learner_document
    if document is None:
        raise ValueError("learner_document is required")

    lines: list[str] = [
        f"# {document.title}",
        "",
        "## 视频概要",
        "",
        document.introduction,
        "",
        "## 主要内容",
        "",
    ]

    for section in document.sections:
        lines.extend([f"### {section.title}", ""])
        for paragraph in section.paragraphs:
            lines.extend([paragraph, ""])

    if document.key_takeaways:
        lines.extend(["## 核心要点", ""])
        lines.extend(f"- {item}" for item in document.key_takeaways)
        lines.append("")

    return "\n".join(lines)


def render_evidence_markdown(result: VideoParseResult) -> str:
    """逐项渲染可追溯证据，不总结或补写缺失内容。"""
    lines: list[str] = [
        f"# {result.title}｜技术证据稿",
        "",
        f"> 来源文件：`{result.source_name}`  ",
        f"> 视频时长：{_format_time(result.duration_ms)}  ",
        f"> 处理状态：`{result.status}`  ",
        f"> 主要语言：{', '.join(result.languages) if result.languages else '未识别'}",
        "",
        "> 本文按处理结果逐项呈现原始证据，不对缺失内容进行推断或补写。",
        "",
        "## 时间线证据",
        "",
    ]

    # 把转写片段和关键帧合并为统一事件列表，按时间戳排序后交错输出
    analysis_map = {item.keyframe_id: item for item in result.frame_analyses}
    events: list[tuple[int, str, object]] = []
    for seg in result.transcript_segments:
        events.append((seg.start_ms, "transcript", seg))
    for kf in result.keyframes:
        events.append((kf.timestamp_ms, "keyframe", kf))
    # 先按时间排序，同一时间点转写优先于关键帧
    events.sort(key=lambda e: (e[0], 0 if e[1] == "transcript" else 1))

    if not events:
        lines.extend(["未获得任何证据（转写和关键帧均为空）。", ""])
    else:
        for _, kind, item in events:
            if kind == "transcript":
                seg = item  # TranscriptSegment
                lines.extend([
                    f"### [{_format_range(seg.start_ms, seg.end_ms)}] 🔊 `{seg.id}`",
                    "",
                    seg.text,
                    "",
                    f"- 时间来源：`{seg.timing_source}`",
                ])
                if seg.language:
                    lines.append(f"- 语言：`{seg.language}`")
                if seg.speaker:
                    lines.append(f"- 说话人：`{seg.speaker}`")
                lines.append("")
            else:  # keyframe
                kf = item  # Keyframe
                lines.extend([
                    f"### [{_format_time(kf.timestamp_ms)}] 🖼️ `{kf.id}`",
                    "",
                    f"![{kf.id}]({kf.image_path})",
                    "",
                    f"- 选取原因：`{kf.selection_reason}`",
                    f"- 图片尺寸：{kf.width} × {kf.height}",
                ])
                analysis = analysis_map.get(kf.id)
                if analysis is None:
                    lines.extend(["- 画面分析：**缺失**", ""])
                    continue
                lines.append(f"- 内容类型：`{analysis.content_type}`")
                if analysis.visible_text:
                    lines.extend(["", "**画面文字：**", ""])
                    lines.extend(f"> {text}" for text in analysis.visible_text)
                if analysis.description:
                    lines.extend(["", "**画面描述：**", "", analysis.description])
                lines.append("")

    lines.extend(["## 处理告警", ""])
    if result.warnings:
        for warning in result.warnings:
            time_suffix = ""
            if warning.start_ms is not None:
                end_ms = warning.end_ms if warning.end_ms is not None else warning.start_ms
                time_suffix = f" [{_format_range(warning.start_ms, end_ms)}]"
            lines.extend([
                f"- `{warning.code}`{time_suffix}：{warning.message}",
            ])
    else:
        lines.append("无处理告警。")
    lines.append("")
    return "\n".join(lines)
