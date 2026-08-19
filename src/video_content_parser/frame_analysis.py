from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Protocol

from .logging_config import get_logger
from .models import FrameAnalysis, Keyframe, ModelTokenUsage, Warning
from .omni import collect_stream_text_async, parse_json_object
from .options import VideoParseOptions

logger = get_logger(__name__)

# content_type 白名单，非法值统一归为 unknown
_VALID_CONTENT_TYPES = {
    "slide", "screen", "person", "product", "document", "scene", "unknown",
}


class VlmBackend(Protocol):
    """VLM（视觉语言模型）后端的统一接口约定，负责对关键帧做画面理解。"""

    def analyze(
        self,
        keyframes: list[Keyframe],
    ) -> tuple[list[FrameAnalysis], list[Warning], ModelTokenUsage]: ...


def _encode_frames(
    batch: list[Keyframe],
) -> tuple[list[tuple[Keyframe, str]], list[Warning]]:
    """读取并 base64 编码每帧图片，读取失败的帧转为告警而非中断。

    返回 (可读帧列表, 告警列表)。可读帧列表元素为 (Keyframe, base64字符串)。
    """
    warnings: list[Warning] = []
    readable: list[tuple[Keyframe, str]] = []
    for kf in batch:
        try:
            data = base64.b64encode(Path(kf.image_path).read_bytes()).decode("ascii")
            readable.append((kf, data))
        except Exception as exc:
            warnings.append(Warning(
                code="FRAME_ANALYSIS_FAILED",
                message=f"无法读取关键帧 {kf.id}：{exc}",
                start_ms=kf.timestamp_ms,
                end_ms=kf.timestamp_ms,
            ))
    return readable, warnings


def _build_content_parts(
    readable_frames: list[tuple[Keyframe, str]],
) -> list[dict[str, object]]:
    """根据可读帧构造模型输入的 content_parts（文本说明 + 图片交替排列）。"""
    valid_batch = [kf for kf, _ in readable_frames]
    prompt_lines = [
        "请逐一分析以下关键帧，并且只返回一个 JSON 对象。",
        '对象结构必须为：{"frames": [...]}。',
        "每一帧必须包含：",
        "- keyframe_id：严格照抄给定的帧 ID",
        "- visible_text：清晰可读的文字数组；逐字保留原文，忽略装饰文字和水印",
        "- description：使用中文客观描述画面中可见的事实，不推测未出现的信息",
        "- content_type：只能是 slide|screen|person|product|document|scene|unknown",
        "",
        "待分析帧：",
    ]
    for kf in valid_batch:
        prompt_lines.append(f"- {kf.id}，时间点 {kf.timestamp_ms / 1000:.1f} 秒")

    # 文本在前给出整体任务，随后交替插入“帧 ID 说明 + 图片”以对齐顺序
    content_parts: list[dict[str, object]] = [{"type": "text", "text": "\n".join(prompt_lines)}]
    for kf, data in readable_frames:
        content_parts.append({
            "type": "text",
            "text": f"下一张图片的准确 keyframe_id 是：{kf.id}",
        })
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{data}"},
        })
    return content_parts


def _parse_batch_response(
    raw: str,
    valid_batch: list[Keyframe],
) -> list[FrameAnalysis]:
    """解析模型返回的 JSON 文本，映射为 FrameAnalysis 列表。"""
    parsed = parse_json_object(raw)
    frames_data = parsed.get("frames", [])
    kf_map = {k.id: k for k in valid_batch}

    analyses: list[FrameAnalysis] = []
    for fd in frames_data:
        kid = fd.get("keyframe_id", "")
        # 模型可能返回未在本次请求中的 ID，直接忽略
        if kid not in kf_map:
            continue
        kf = kf_map[kid]
        content_type = fd.get("content_type", "unknown")
        if content_type not in _VALID_CONTENT_TYPES:
            content_type = "unknown"
        analyses.append(FrameAnalysis(
            keyframe_id=kid,
            timestamp_ms=kf.timestamp_ms,
            visible_text=[t for t in fd.get("visible_text", []) if t and t.strip()],
            description=fd.get("description", "").strip(),
            content_type=content_type,
        ))
    return analyses


class ChatVlmBackend:
    """基于 OpenAI 兼容协议的 VLM 后端实现，分批并发调用模型分析关键帧。

    对外保持同步接口 analyze()，内部使用 asyncio + AsyncOpenAI 并发处理多个批次，
    用 Semaphore 控制最大并发数以避免触发 API 限流。遇到失败时采用指数退避重试。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        frames_per_request: int = 6,
        max_retries: int = 3,
        concurrency: int = 4,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.frames_per_request = frames_per_request
        self.max_retries = max_retries
        self.concurrency = max(1, concurrency)

    def analyze(
        self,
        keyframes: list[Keyframe],
    ) -> tuple[list[FrameAnalysis], list[Warning], ModelTokenUsage]:
        """将关键帧按 frames_per_request 分批，并发送入模型分析，聚合结果与告警。"""
        if not keyframes:
            return [], [], ModelTokenUsage()

        batch_count = (len(keyframes) + self.frames_per_request - 1) // self.frames_per_request
        logger.info(
            "VLM 分析启动: %d 帧, %d 批（每批 %d 帧, 并发 %d）",
            len(keyframes), batch_count, self.frames_per_request, self.concurrency,
        )

        # 只有单批时无需启动事件循环，直接同步调用避免开销
        if len(keyframes) <= self.frames_per_request:
            return asyncio.run(self._analyze_batch_async(keyframes))

        return asyncio.run(self._analyze_all(keyframes))

    async def _analyze_all(
        self,
        keyframes: list[Keyframe],
    ) -> tuple[list[FrameAnalysis], list[Warning], ModelTokenUsage]:
        """并发处理所有批次，用 Semaphore 限制并发数。"""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        sem = asyncio.Semaphore(self.concurrency)

        # 切分批次
        batches = [
            keyframes[i:i + self.frames_per_request]
            for i in range(0, len(keyframes), self.frames_per_request)
        ]

        async def run_one(batch: list[Keyframe]):
            async with sem:
                return await self._analyze_batch_async(batch, client)

        # 所有批次并发执行，gather 保证返回顺序与提交顺序一致
        results = await asyncio.gather(*(run_one(b) for b in batches))

        all_analyses: list[FrameAnalysis] = []
        all_warnings: list[Warning] = []
        total_usage = ModelTokenUsage()
        for analyses, warnings, u in results:
            all_analyses.extend(analyses)
            all_warnings.extend(warnings)
            total_usage.add(u)
        return all_analyses, all_warnings, total_usage

    async def _analyze_batch_async(
        self,
        batch: list[Keyframe],
        client=None,
    ) -> tuple[list[FrameAnalysis], list[Warning], ModelTokenUsage]:
        """异步分析单个批次：编码图片、构造提示词、调用模型、解析返回。

        client 为 None 时在内部创建临时 AsyncOpenAI（单批场景）。
        遇到失败时按指数退避等待后重试，应对 429 限流。
        批次重试全部失败后，降级为单帧逐个调用，尽可能救回更多帧。
        """
        from openai import AsyncOpenAI

        warnings: list[Warning] = []

        # 编码图片，读取失败的帧转为告警
        readable_frames, encode_warnings = _encode_frames(batch)
        warnings.extend(encode_warnings)
        if not readable_frames:
            return [], warnings, ModelTokenUsage()

        valid_batch = [kf for kf, _ in readable_frames]
        content_parts = _build_content_parts(readable_frames)

        # 复用传入的 client，或为单批场景创建临时 client
        owns_client = client is None
        if owns_client:
            client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)

        try:
            for attempt in range(self.max_retries):
                try:
                    stream = await client.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "user", "content": content_parts}],  # type: ignore[list-item]
                        response_format={"type": "json_object"},
                        extra_body={"enable_thinking": False},
                        stream=True,
                        stream_options={"include_usage": True},
                    )

                    raw, usage = await collect_stream_text_async(stream)
                    analyses = _parse_batch_response(raw, valid_batch)
                    logger.debug(
                        "批次分析成功: %d 帧 → %d 分析结果", len(valid_batch), len(analyses),
                    )
                    return analyses, warnings, usage

                except Exception as exc:
                    # 最后一次重试仍失败时，降级为单帧逐个调用
                    if attempt == self.max_retries - 1:
                        logger.warning(
                            "批次（%d 帧）重试 %d 次后仍失败，降级为单帧逐个调用: %s",
                            len(valid_batch), self.max_retries, exc,
                        )
                        single_analyses, single_warnings, single_usage = (
                            await self._fallback_to_single_frames(valid_batch, client)
                        )
                        warnings.extend(single_warnings)
                        return single_analyses, warnings, single_usage
                    else:
                        # 指数退避：1s, 2s, 4s...（应对 429 限流）
                        logger.debug(
                            "批次（%d 帧）第 %d 次失败，%.1fs 后重试: %s",
                            len(valid_batch), attempt + 1, 2 ** attempt, exc,
                        )
                        await asyncio.sleep(2 ** attempt)
        finally:
            # 单批场景创建的 client 需要显式关闭，避免资源泄漏警告
            if owns_client and client is not None:
                await client.close()

        return [], warnings, ModelTokenUsage()

    async def _fallback_to_single_frames(
        self,
        frames: list[Keyframe],
        client,
    ) -> tuple[list[FrameAnalysis], list[Warning], ModelTokenUsage]:
        """批次失败后降级为单帧逐个调用，尽可能救回更多帧。

        批次失败通常是因为多帧 payload 过大触发限流或超时，
        拆成单帧后 payload 小很多，成功率显著提高。
        单帧调用也做指数退避重试，最终失败的帧才标记为告警。
        """
        warnings: list[Warning] = []
        all_analyses: list[FrameAnalysis] = []
        total_usage = ModelTokenUsage()

        for kf in frames:
            # 单帧编码
            readable, encode_warnings = _encode_frames([kf])
            warnings.extend(encode_warnings)
            if not readable:
                continue

            content_parts = _build_content_parts(readable)

            for attempt in range(self.max_retries):
                try:
                    stream = await client.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "user", "content": content_parts}],  # type: ignore[list-item]
                        response_format={"type": "json_object"},
                        extra_body={"enable_thinking": False},
                        stream=True,
                        stream_options={"include_usage": True},
                    )
                    raw, usage = await collect_stream_text_async(stream)
                    analyses = _parse_batch_response(raw, [kf])
                    all_analyses.extend(analyses)
                    total_usage.add(usage)
                    break
                except Exception as exc:
                    if attempt == self.max_retries - 1:
                        logger.warning("单帧 %s 分析失败: %s", kf.id, exc)
                        warnings.append(Warning(
                            code="FRAME_ANALYSIS_FAILED",
                            message=f"关键帧 {kf.id} 单帧分析失败：{exc}",
                            start_ms=kf.timestamp_ms,
                            end_ms=kf.timestamp_ms,
                        ))
                    else:
                        logger.debug(
                            "单帧 %s 第 %d 次失败，%.1fs 后重试: %s",
                            kf.id, attempt + 1, 2 ** attempt, exc,
                        )
                        await asyncio.sleep(2 ** attempt)

        logger.info(
            "单帧降级完成: 救回 %d/%d 帧", len(all_analyses), len(frames),
        )
        return all_analyses, warnings, total_usage


class FakeVlmBackend:
    """用于测试与无网络环境下的 VLM 桩实现，返回固定的模拟画面分析结果。"""

    def __init__(self, analyses: list[FrameAnalysis] | None = None):
        self._analyses = analyses

    def analyze(
        self,
        keyframes: list[Keyframe],
    ) -> tuple[list[FrameAnalysis], list[Warning], ModelTokenUsage]:
        # 传入预置结果时按帧 ID 过滤后复制返回，否则为每帧生成默认模拟描述
        if self._analyses is not None:
            kf_ids = {k.id for k in keyframes}
            matched = [a for a in self._analyses if a.keyframe_id in kf_ids]
            return [a.model_copy() for a in matched], [], ModelTokenUsage()
        return [
            FrameAnalysis(
                keyframe_id=kf.id,
                timestamp_ms=kf.timestamp_ms,
                description=f"模拟画面分析：第 {i + 1} 帧的场景描述",
                visible_text=[f"文字内容 {i + 1}"],
            )
            for i, kf in enumerate(keyframes)
        ], [], ModelTokenUsage()


def create_vlm_backend(
    options: VideoParseOptions,
    *,
    openai_api_key: str = "",
    openai_base_url: str = "",
    vlm_model: str = "",
) -> VlmBackend:
    """根据是否提供完整的 OpenAI 兼容配置，选择真实或桩 VLM 后端。"""
    # 三项配置齐全才启用真实后端，否则降级到 FakeVlmBackend
    has_chat = bool(openai_api_key and openai_base_url and vlm_model)
    if has_chat:
        return ChatVlmBackend(
            base_url=openai_base_url,
            api_key=openai_api_key,
            model=vlm_model,
            frames_per_request=options.frames_per_analysis_request,
            max_retries=options.max_model_retries,
            concurrency=options.vlm_concurrency,
        )
    return FakeVlmBackend([])
