"""视频解析主流程模块。

VideoParser 串联音频抽取、ASR 转写、关键帧抽取、画面分析、章节构建
等子步骤，并将最终结果写入输出目录。任何子步骤失败都会被收集为
Warning，最终根据是否出现关键失败码判定结果为 complete 或 partial。
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

from . import ffmpeg as ffmpeg_module
from .asr import (
    AsrBackend,
    create_asr_backend,
)
from .audio import extract_audio
from .config import ProviderConfig, load_provider_config
from .errors import (
    InvalidVideoInputError,
    NoUsableContentError,
)
from .frame_analysis import (
    VlmBackend,
    create_vlm_backend,
)
from .keyframes import extract_keyframes
from .logging_config import get_logger
from .models import (
    Artifacts,
    FrameAnalysis,
    Keyframe,
    LearnerDocument,
    ModelTokenUsage,
    Section,
    TokenUsage,
    TranscriptSegment,
    VideoParseResult,
    Warning,
)
from .options import VideoParseOptions
from .sections import (
    SectionBuilder,
    create_section_builder,
)
from .writer import write_outputs

logger = get_logger(__name__)


# 这些警告码一旦出现，说明核心内容采集环节失败，结果只能算 partial
_INCOMPLETE_WARNING_CODES = {
    "NO_AUDIO_TRACK",
    "ASR_FAILED",
    "NO_KEYFRAMES",
    "FRAME_ANALYSIS_FAILED",
    "SECTION_BUILDER_FAILED",
    "SECTION_BUILDER_FALLBACK",
}


def _is_complete_result(
    transcripts: list[TranscriptSegment],
    keyframes: list[Keyframe],
    frame_analyses: list[FrameAnalysis],
    sections: list[Section],
    learner_document: LearnerDocument | None,
    warnings: list[Warning],
) -> bool:
    """核心证据、兼容章节和业务文章齐全时才视为完整结果。"""
    if not transcripts or not keyframes or not sections or learner_document is None:
        return False

    # 所有抽取出的关键帧都必须有对应的画面分析
    expected_frame_ids = {frame.id for frame in keyframes}
    analyzed_frame_ids = {analysis.keyframe_id for analysis in frame_analyses}
    if analyzed_frame_ids != expected_frame_ids:
        return False

    return not any(warning.code in _INCOMPLETE_WARNING_CODES for warning in warnings)


class VideoParser:
    """视频解析编排器，串联各子模块完成端到端解析。"""

    def __init__(
        self,
        *,
        provider_config: ProviderConfig | None = None,
        asr_backend: AsrBackend | None = None,
        vlm_backend: VlmBackend | None = None,
        section_builder: SectionBuilder | None = None,
    ):
        self._provider_config = provider_config or load_provider_config()
        self._asr_backend = asr_backend
        self._vlm_backend = vlm_backend
        self._section_builder = section_builder

    def parse(
        self,
        source: Path,
        output_dir: Path,
        options: VideoParseOptions | None = None,
    ) -> VideoParseResult:
        """解析入口：校验输入、创建临时目录并执行主流程，结束后清理临时文件。"""
        options = options or VideoParseOptions()
        source = Path(source)
        output_dir = Path(output_dir)

        self._validate_input(source)
        output_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg_module.check_ffmpeg_available()

        # 临时目录放在输出目录下，便于解析失败后一并清理
        tmp_dir = Path(tempfile.mkdtemp(prefix="vp_", dir=str(output_dir)))
        try:
            return self._do_parse(source, output_dir, tmp_dir, options)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _validate_input(self, source: Path) -> None:
        """校验输入路径是否存在且为文件。"""
        if not source.exists():
            raise InvalidVideoInputError(f"Input path does not exist: {source}")
        if not source.is_file():
            raise InvalidVideoInputError(f"Input is not a file: {source}")

    def _do_parse(
        self,
        source: Path,
        output_dir: Path,
        tmp_dir: Path,
        options: VideoParseOptions,
    ) -> VideoParseResult:
        """实际执行解析的内部方法，按序完成各子步骤并汇总结果。"""
        warnings: list[Warning] = []
        source_name = source.name
        # 各步骤耗时记录，格式为 (步骤名, 秒)
        timings: list[tuple[str, float]] = []

        # 探测视频时长，失败时仅记录警告，不阻断流程
        t0 = time.perf_counter()
        duration_ms = ffmpeg_module.probe_duration(source)
        timings.append(("探测时长", time.perf_counter() - t0))
        if duration_ms is None:
            warnings.append(Warning(
                code="DURATION_UNKNOWN",
                message="Unable to determine video duration reliably.",
            ))

        # 准备产物目录：assets 存放音频与关键帧图片
        assets_dir = output_dir / "assets"
        keyframes_dir = assets_dir / "keyframes"
        assets_dir.mkdir(parents=True, exist_ok=True)
        keyframes_dir.mkdir(parents=True, exist_ok=True)

        # 1. 抽取音频
        t0 = time.perf_counter()
        audio_path = assets_dir / "audio.mp3"
        audio_artifact = extract_audio(source, audio_path)
        timings.append(("抽取音频", time.perf_counter() - t0))
        logger.info("音频抽取完成: %s", audio_path.name)
        if audio_artifact is None:
            logger.warning("视频无可用音频轨道")
            warnings.append(Warning(
                code="NO_AUDIO_TRACK",
                message="No usable audio track found.",
            ))

        # 2. ASR 转写（仅当存在音频时执行）
        transcript_segments: list[TranscriptSegment] = []
        asr_usage = ModelTokenUsage()
        t0 = time.perf_counter()
        if audio_artifact is not None:
            asr = self._get_asr_backend(options)
            logger.info("开始 ASR 转写: %s", audio_path.name)
            try:
                transcript_segments, asr_usage = asr.transcribe(audio_path, options.language)
                logger.info("ASR 转写完成: %d 段", len(transcript_segments))
            except Exception as e:
                logger.exception("ASR 转写整体失败")
                warnings.append(Warning(
                    code="ASR_FAILED",
                    message=f"ASR transcription failed: {e}",
                ))
        timings.append(("ASR 转写", time.perf_counter() - t0))

        # 3. 抽取关键帧
        keyframes: list[Keyframe] = []
        t0 = time.perf_counter()
        logger.info("开始抽取关键帧")
        try:
            keyframes = extract_keyframes(source, tmp_dir, options, duration_ms)
            logger.info("关键帧抽取完成: %d 帧", len(keyframes))
        except Exception as e:
            logger.exception("关键帧抽取失败")
            warnings.append(Warning(
                code="NO_KEYFRAMES",
                message=f"Keyframe extraction failed: {e}",
            ))
        timings.append(("抽取关键帧", time.perf_counter() - t0))

        if not keyframes:
            warnings.append(Warning(
                code="NO_KEYFRAMES",
                message="No keyframes could be extracted.",
            ))

        # 把关键帧从临时目录复制到正式输出目录
        t0 = time.perf_counter()
        for kf in keyframes:
            src = tmp_dir / "keyframes" / Path(kf.image_path).name
            if src.exists():
                dst = output_dir / kf.image_path
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        timings.append(("复制关键帧", time.perf_counter() - t0))

        # 4. 关键帧画面分析（VLM）
        frame_analyses = []
        vlm_usage = ModelTokenUsage()
        t0 = time.perf_counter()
        if keyframes:
            vlm = self._get_vlm_backend(options)
            logger.info("开始 VLM 画面分析: %d 帧", len(keyframes))
            try:
                # 把图片路径指向输出目录中的正式副本
                analysis_keyframes = [
                    kf.model_copy(update={
                        "image_path": str((output_dir / kf.image_path).resolve()),
                    })
                    for kf in keyframes
                ]
                frame_analyses, fa_warnings, vlm_usage = vlm.analyze(analysis_keyframes)
                warnings.extend(fa_warnings)
                logger.info("VLM 画面分析完成: %d/%d", len(frame_analyses), len(keyframes))
            except Exception as e:
                logger.exception("VLM 画面分析整体失败")
                warnings.append(Warning(
                    code="FRAME_ANALYSIS_FAILED",
                    message=f"Frame analysis failed: {e}",
                ))
        timings.append(("VLM 画面分析", time.perf_counter() - t0))

        # 5. 章节构建
        sections = []
        learner_document = None
        sec_usage = ModelTokenUsage()
        sec_builder = self._get_section_builder(options)
        t0 = time.perf_counter()
        logger.info("开始章节构建")
        try:
            sections, learner_document, sec_warnings, sec_usage = sec_builder.build(
                transcript_segments, frame_analyses,
            )
            warnings.extend(sec_warnings)
            logger.info("章节构建完成: %d 章", len(sections))
        except Exception as e:
            # 章节构建器整体异常时，强制走确定性回退保证有章节产出
            logger.exception("章节构建失败，降级为确定性回退")
            from .sections import ChatSectionBuilder
            warnings.append(Warning(
                code="SECTION_BUILDER_FAILED",
                message=f"Section builder failed: {e}",
            ))
            fallback = ChatSectionBuilder(
                base_url="", api_key="", model="",
                max_retries=options.max_model_retries,
            )
            sections, sec_warnings, _ = fallback._fallback_build(
                transcript_segments, frame_analyses,
            )
            warnings.extend(sec_warnings)
        timings.append(("章节构建", time.perf_counter() - t0))

        # 既没有转写也没有关键帧，说明视频无可解析内容
        if not transcript_segments and not keyframes:
            raise NoUsableContentError(
                "No usable audio or visual content could be extracted from the video."
            )

        # 根据产物完整度判定状态
        status = "complete" if _is_complete_result(
            transcript_segments,
            keyframes,
            frame_analyses,
            sections,
            learner_document,
            warnings,
        ) else "partial"

        if status == "partial":
            warnings.append(Warning(
                code="CONTENT_PARTIAL",
                message="Parse result is partial due to some processing failures.",
            ))

        # 从产物中派生标题、语言、摘要等元信息
        title = self._derive_title(source_name, transcript_segments, frame_analyses)
        languages = self._derive_languages(transcript_segments)
        summary = self._derive_summary(transcript_segments, frame_analyses)

        source_stem = Path(source_name).stem
        evidence_markdown_path = f"{source_stem}_技术证据稿.md"
        learner_markdown_path = (
            f"{source_stem}_业务讲义.md"
            if learner_document is not None
            else None
        )

        # 汇总各模块 token 用量
        total_usage = TokenUsage()
        total_usage.asr.add(asr_usage)
        total_usage.vlm.add(vlm_usage)
        total_usage.sections.add(sec_usage)

        result = VideoParseResult(
            status=status,
            source_name=source_name,
            duration_ms=duration_ms,
            title=title,
            languages=languages,
            summary=summary,
            artifacts=Artifacts(
                audio_path="assets/audio.mp3" if audio_artifact else None,
                evidence_markdown_path=evidence_markdown_path,
                learner_markdown_path=learner_markdown_path,
            ),
            transcript_segments=transcript_segments,
            keyframes=keyframes,
            frame_analyses=frame_analyses,
            sections=sections,
            learner_document=learner_document,
            warnings=warnings,
            token_usage=total_usage,
        )

        t0 = time.perf_counter()
        write_outputs(result, output_dir)
        timings.append(("写入文件", time.perf_counter() - t0))
        logger.info("结果已写入: %s", output_dir)

        # 打印各步骤耗时报告
        total = sum(sec for _, sec in timings)
        print(f"\n{'='*50}")
        print(f"  耗时报告：{source_name}")
        print(f"{'='*50}")
        for name, sec in timings:
            pct = (sec / total * 100) if total > 0 else 0
            print(f"  {name:<12s}  {sec:>7.2f}s  ({pct:>5.1f}%)")
        print(f"  {'─'*36}")
        print(f"  {'合计':<12s}  {total:>7.2f}s")
        print(f"{'='*50}")

        # 打印 Token 用量报告
        print(f"  Token 用量：{source_name}")
        print(f"{'='*50}")

        def _fmt_fake(tot: int) -> str:
            return "" if tot > 0 else "  (n/a / fake)"

        asr = total_usage.asr
        print(
            f"  {'ASR':<10s}  prompt={asr.prompt_tokens:>6d}  "
            f"completion={asr.completion_tokens:>6d}  total={asr.total_tokens:>6d}"
            + _fmt_fake(asr.total_tokens)
        )

        vlm = total_usage.vlm
        vlm_extra = f"  (image={vlm.image_tokens} audio={vlm.audio_tokens})"
        print(
            f"  {'VLM':<10s}  prompt={vlm.prompt_tokens:>6d}  "
            f"completion={vlm.completion_tokens:>6d}  total={vlm.total_tokens:>6d}"
            + vlm_extra
            + _fmt_fake(vlm.total_tokens)
        )

        sec = total_usage.sections
        print(
            f"  {'Sections':<10s}  prompt={sec.prompt_tokens:>6d}  "
            f"completion={sec.completion_tokens:>6d}  total={sec.total_tokens:>6d}"
            + _fmt_fake(sec.total_tokens)
        )

        print(f"  {'─'*36}")
        print(
            f"  {'合计':<10s}              prompt={total_usage.total_prompt_tokens:>6d}  "
            f"completion={total_usage.total_completion_tokens:>6d}  "
            f"total={total_usage.total_tokens:>6d}"
        )
        print(f"{'='*50}\n")

        return result

    def _get_asr_backend(self, options: VideoParseOptions) -> AsrBackend:
        """获取 ASR 后端：优先用注入实例，否则按配置创建。"""
        if self._asr_backend is not None:
            return self._asr_backend
        pc = self._provider_config
        return create_asr_backend(
            options,
            openai_api_key=pc.openai_api_key,
            openai_base_url=pc.openai_base_url,
            asr_model=pc.asr_model,
        )

    def _get_vlm_backend(self, options: VideoParseOptions) -> VlmBackend:
        """获取画面分析(VLM)后端：优先用注入实例，否则按配置创建。"""
        if self._vlm_backend is not None:
            return self._vlm_backend
        pc = self._provider_config
        return create_vlm_backend(
            options,
            openai_api_key=pc.openai_api_key,
            openai_base_url=pc.openai_base_url,
            vlm_model=pc.vlm_model,
        )

    def _get_section_builder(self, options: VideoParseOptions) -> SectionBuilder:
        """获取章节构建器：优先用注入实例，否则按配置创建。"""
        if self._section_builder is not None:
            return self._section_builder
        pc = self._provider_config
        return create_section_builder(
            openai_api_key=pc.openai_api_key,
            openai_base_url=pc.openai_base_url,
            section_model=pc.section_model,
            max_retries=options.max_model_retries,
        )

    @staticmethod
    def _derive_title(
        source_name: str,
        transcripts: list[TranscriptSegment],
        frame_analyses,
    ) -> str:
        """派生标题：优先取首句转写，否则用文件名。"""
        _ = frame_analyses
        if transcripts:
            first_text = transcripts[0].text
            if len(first_text) > 80:
                return first_text[:80] + "..."
            if first_text:
                return first_text
        stem = Path(source_name).stem
        return stem

    @staticmethod
    def _derive_languages(transcripts: list[TranscriptSegment]) -> list[str]:
        """从转写片段中收集去重后的语言列表。"""
        langs: list[str] = []
        seen: set[str] = set()
        for t in transcripts:
            if t.language and t.language not in seen:
                langs.append(t.language)
                seen.add(t.language)
        return langs

    @staticmethod
    def _derive_summary(transcripts, frame_analyses) -> str:
        """派生摘要：拼接前几句转写文本与首个画面描述。"""
        parts: list[str] = []
        if transcripts:
            all_text = " ".join(t.text for t in transcripts[:3])
            if len(all_text) > 200:
                all_text = all_text[:200] + "..."
            parts.append(all_text)
        if frame_analyses:
            desc = frame_analyses[0].description
            if desc:
                parts.append(desc[:100])
        if parts:
            return " | ".join(parts)
        return "No summary available."
