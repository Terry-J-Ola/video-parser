"""结构化结果、技术证据稿和业务讲义的原子写入模块。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .errors import ResultWriteError
from .markdown import render_evidence_markdown, render_learner_markdown
from .models import VideoParseResult


def _atomic_write(target: Path, content: bytes) -> None:
    """原子写入：先写临时文件，再通过 os.replace 原子替换目标文件。

    任何异常都会清理临时文件并向上抛出，确保不会残留半成品。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件与目标文件同目录，保证 os.replace 是同分区原子操作
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=".tmp_",
        suffix=target.suffix,
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp_path, target)
    except Exception:
        # 写入失败时清理临时文件
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_outputs(result: VideoParseResult, output_dir: Path) -> None:
    """写入 JSON、技术证据稿和可选业务讲义，并清理旧输出文件。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        learner_path_value = result.artifacts.learner_markdown_path
        if (result.learner_document is None) != (learner_path_value is None):
            raise ResultWriteError(
                "learner document and output path must either both exist or both be absent"
            )

        # 先写 JSON（完整结构化数据，保留 None 字段便于下游消费）
        json_path = output_dir / "parse_result.json"
        json_bytes = result.model_dump_json(indent=2, exclude_none=False).encode("utf-8")
        _atomic_write(json_path, json_bytes)

        evidence_path = output_dir / result.artifacts.evidence_markdown_path
        evidence_content = render_evidence_markdown(result)
        _atomic_write(evidence_path, evidence_content.encode("utf-8"))

        expected_learner_path = output_dir / f"{Path(result.source_name).stem}_业务讲义.md"
        if learner_path_value:
            learner_path = output_dir / learner_path_value
            learner_content = render_learner_markdown(result)
            _atomic_write(learner_path, learner_content.encode("utf-8"))
        elif expected_learner_path.exists():
            expected_learner_path.unlink()

        legacy_content_path = output_dir / "content.md"
        if legacy_content_path.exists():
            legacy_content_path.unlink()

    except (OSError, IOError) as e:
        raise ResultWriteError(f"Failed to write outputs: {e}") from e
