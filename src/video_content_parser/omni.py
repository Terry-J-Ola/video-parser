from __future__ import annotations

import json
from collections.abc import AsyncIterable, Iterable
from typing import Any

from .models import ModelTokenUsage


def _extract_content(chunk: Any) -> list[str]:
    """从单个流式 chunk 中提取所有文本片段，兼容字符串和列表两种 content 格式。"""
    results: list[str] = []
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return results
    delta = getattr(choices[0], "delta", None)
    content = getattr(delta, "content", None)
    if isinstance(content, str):
        results.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                results.append(item)
            elif isinstance(item, dict):
                text_val = item.get("text")
                if isinstance(text_val, str):
                    results.append(text_val)
            elif hasattr(item, "text"):
                text_val = getattr(item, "text")
                if isinstance(text_val, str):
                    results.append(text_val)
    return results


def collect_stream_text(stream: Iterable[Any]) -> tuple[str, ModelTokenUsage]:
    """从同步流式响应中收集所有文本增量，以及最后一个 chunk 中携带的 usage。"""
    parts: list[str] = []
    usage = ModelTokenUsage()
    for chunk in stream:
        parts.extend(_extract_content(chunk))
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            usage.add_usage_dict(chunk_usage)
    return "".join(parts).strip(), usage


async def collect_stream_text_async(stream: AsyncIterable[Any]) -> tuple[str, ModelTokenUsage]:
    """从异步流式响应中收集所有文本增量，以及最后一个 chunk 中携带的 usage。"""
    parts: list[str] = []
    usage = ModelTokenUsage()
    async for chunk in stream:
        parts.extend(_extract_content(chunk))
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            usage.add_usage_dict(chunk_usage)
    return "".join(parts).strip(), usage


def parse_json_object(text: str) -> dict[str, Any]:
    """从可能含散文或 Markdown 代码块的文本中提取第一个 JSON 对象。"""
    decoder = json.JSONDecoder()
    # 逐字符扫描首个 '{'，再用 raw_decode 尝试解析，兼容模型输出前后多余文字
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("Model response did not contain a valid JSON object")
