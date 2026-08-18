"""命令行入口模块。

解析命令行参数，确定输入视频与输出目录，构造 VideoParser 并执行解析，
最后打印解析结果的统计信息。

输出策略：
- 无警告结果 → output/<视频名>/
- 有警告结果 → output/_warnings/<视频名>/
解析前会检查这两个位置是否已有 status=complete 的结果，--force 可强制覆盖。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import load_provider_config
from .batch_xlsx import export_batch_summary_xlsx
from . import __version__
from .models import TokenUsage
from .options import VideoParseOptions
from .parser import VideoParser

# 有警告结果的子目录名
_WARNINGS_SUBDIR = "_warnings"


@dataclass
class VideoRecord:
    """单个视频的处理记录，用于批量汇总表。"""

    name: str
    status: str           # complete / partial / skipped / failed
    total_tokens: int
    elapsed_seconds: float
    asr_model: str = ""
    asr_tokens: int = 0
    vlm_model: str = ""
    vlm_tokens: int = 0
    summary_model: str = ""
    summary_tokens: int = 0
    output_dir: Path | None = None
    result_json: Path | None = None
    evidence_markdown: Path | None = None
    summary_markdown: Path | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        """转换为稳定的 CLI JSON 记录，路径统一为绝对路径。"""
        def absolute(path: Path | None) -> str | None:
            return str(path.resolve()) if path is not None else None

        return {
            "name": self.name,
            "status": self.status,
            "total_tokens": self.total_tokens,
            "token_usage": {
                "asr": {
                    "model": self.asr_model or None,
                    "total_tokens": self.asr_tokens,
                },
                "vlm": {
                    "model": self.vlm_model or None,
                    "total_tokens": self.vlm_tokens,
                },
                "summary": {
                    "model": self.summary_model or None,
                    "total_tokens": self.summary_tokens,
                },
            },
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "output_dir": absolute(self.output_dir),
            "result_json": absolute(self.result_json),
            "evidence_markdown": absolute(self.evidence_markdown),
            "summary_markdown": absolute(self.summary_markdown),
            "warnings": self.warnings,
            "error": self.error,
        }


def build_cli_payload(mode: str, records: list[VideoRecord]) -> dict[str, object]:
    """构造供 Skill 和自动化消费的版本化 JSON 契约。"""
    counts = {
        status: sum(record.status == status for record in records)
        for status in ("complete", "partial", "skipped", "failed")
    }
    return {
        "schema_version": "video_content_parser.cli.v3",
        "mode": mode,
        "summary": counts,
        "results": [record.to_dict() for record in records],
    }


def _display_width(text: str) -> int:
    """计算字符串在终端的显示宽度：CJK 全角字符算 2，其余算 1。"""
    width = 0
    for ch in text:
        # East_Asian_Width 属性为 W/F/A 的字符按全角处理（显示宽度 2）
        if unicodedata.east_asian_width(ch) in ("W", "F", "A"):
            width += 2
        else:
            width += 1
    return width


def _pad_to_width(text: str, target_width: int, align: str = "left") -> str:
    """按显示宽度对齐填充，避免中文导致的列错位。"""
    current = _display_width(text)
    pad = max(0, target_width - current)
    if align == "right":
        return " " * pad + text
    return text + " " * pad


def build_arg_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器，定义所有可调选项。"""
    p = argparse.ArgumentParser(
        prog="video-content-parser",
        description="Lightweight video content parser",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("source", type=Path, nargs="?", default=None,
                   help="Input video file path (omit for batch processing of ./input/)")
    p.add_argument("output_dir", type=Path, nargs="?", default=None,
                   help="Single-video output directory (default: ./output/<video_name>/)")
    p.add_argument("--input-dir", type=Path, default=None,
                   help="Batch input directory (default: ./input)")
    p.add_argument("--output-root", type=Path, default=None,
                   help="Batch output root (default: ./output)")
    p.add_argument("--env-file", type=Path, default=None,
                   help="Provider config file; environment variables take precedence")
    p.add_argument("--result-format", choices=("text", "json"), default="text",
                   help="CLI output contract (default: text)")
    p.add_argument("--force", "-f", action="store_true",
                   help="Force re-parse even if result already exists")
    p.add_argument("--language", type=str, default=None, help="Language hint (e.g. zh, en)")
    p.add_argument("--audio-chunk-seconds", type=int, default=30)
    p.add_argument("--scene-threshold", type=float, default=0.30)
    p.add_argument("--fallback-frame-interval", type=float, default=10.0)
    p.add_argument("--max-keyframes", type=int, default=60)
    p.add_argument("--keyframe-max-width", type=int, default=1280)
    p.add_argument("--dedup-hamming-threshold", type=int, default=6)
    p.add_argument("--vlm-concurrency", type=int, default=4,
                   help="Max concurrent VLM batch requests (default: 4)")
    return p


# 受支持的视频文件后缀（小写匹配）
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"}


def _resolve_paths(source: Path, output_dir: Path | None) -> tuple[Path, Path]:
    """解析单个视频的输入输出路径。

    未指定 output_dir 时，默认输出到 output/<视频名>/ 下。
    """
    source = Path(source).expanduser()
    if output_dir is None:
        output_dir = Path.cwd() / "output" / source.stem
    else:
        output_dir = Path(output_dir).expanduser()

    return source, output_dir


def _scan_input_videos(input_dir: Path | None = None) -> list[Path]:
    """扫描显式目录或当前工作目录 input/，按文件名排序返回。"""
    input_dir = Path(input_dir or (Path.cwd() / "input")).expanduser()
    if not input_dir.is_dir():
        return []
    videos = sorted(
        f for f in input_dir.iterdir()
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
    )
    return videos


def _get_final_output_dirs(base_output_dir: Path) -> tuple[Path, Path]:
    """给定基础输出目录，返回两个可能的最终输出路径。

    返回 (无警告路径, 有警告路径)。
    """
    return base_output_dir, base_output_dir.parent / _WARNINGS_SUBDIR / base_output_dir.name


def _has_complete_result(output_dir: Path) -> bool:
    """检查单个输出目录下是否已存在 status=complete 的解析结果。"""
    json_path = output_dir / "parse_result.json"
    if not json_path.is_file():
        return False
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        return data.get("status") == "complete"
    except (json.JSONDecodeError, KeyError):
        return False


def _find_existing_complete(base_output_dir: Path) -> Path | None:
    """在无警告和有警告两个候选目录中，找到已存在的 complete 结果。

    返回找到的目录路径，若都不存在则返回 None。
    """
    clean_dir, warnings_dir = _get_final_output_dirs(base_output_dir)
    if _has_complete_result(clean_dir):
        return clean_dir
    if _has_complete_result(warnings_dir):
        return warnings_dir
    return None


def _classify_and_move(staging_dir: Path, base_output_dir: Path, has_warnings: bool) -> Path:
    """根据是否有警告，把 staging 目录移动到最终输出路径。

    返回最终输出目录的路径。
    """
    clean_dir, warnings_dir = _get_final_output_dirs(base_output_dir)
    final_dir = warnings_dir if has_warnings else clean_dir

    # 若目标目录已存在（极少见，比如两次运行间隔很短），先清理
    if final_dir.exists():
        shutil.rmtree(final_dir)

    final_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging_dir), str(final_dir))
    return final_dir


def _process_one_video(
    source: Path,
    args: argparse.Namespace,
    options: VideoParseOptions,
) -> tuple[int, VideoRecord, TokenUsage | None]:
    """处理单个视频：跳过检查、解析、分类移动、打印统计。

    返回 (ret_code, record, token_usage)：
    - ret_code：0 完整成功/跳过，1 致命失败，4 产生可用但不完整的结果
    - record：包含文件名、状态、token、耗时四项的 VideoRecord（用于汇总表）
    - token_usage：成功处理时返回 result.token_usage（用于批量累加）；跳过/失败返回 None
    """
    emit_text = args.result_format == "text"
    model_fields = {
        "asr_model": args.provider_config.asr_model,
        "vlm_model": args.provider_config.vlm_model,
        "summary_model": args.provider_config.section_model,
    }
    if args.source is not None:
        requested_output = args.output_dir
    else:
        output_root = Path(args.output_root or (Path.cwd() / "output"))
        requested_output = output_root / source.stem
    base_output_dir = _resolve_paths(source, requested_output)[1]

    # 跳过已处理的完整结果（--force 可跳过此检查）
    if not args.force:
        existing = _find_existing_complete(base_output_dir)
        if existing is not None:
            if emit_text:
                print(
                    f"Skip: {source.name} already parsed "
                    f"(status=complete in {existing}). Use --force to re-parse."
                )
            data = json.loads((existing / "parse_result.json").read_text(encoding="utf-8"))
            artifacts = data.get("artifacts", {})
            evidence_name = artifacts.get("evidence_markdown_path")
            summary_name = artifacts.get("learner_markdown_path")
            return 0, VideoRecord(
                name=source.name, status="skipped",
                total_tokens=0, elapsed_seconds=0.0,
                **model_fields,
                output_dir=existing,
                result_json=existing / "parse_result.json",
                evidence_markdown=(existing / evidence_name) if evidence_name else None,
                summary_markdown=(existing / summary_name) if summary_name else None,
            ), None

    # 在最终输出目录的同级创建 staging 目录，解析完成后再移动到目标位置
    base_output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=".staging_", dir=str(base_output_dir.parent)))

    overall_start = time.perf_counter()
    try:
        parser = VideoParser(provider_config=args.provider_config)
        if emit_text:
            result = parser.parse(source, staging_dir, options)
        else:
            # JSON 模式下 stdout 只保留最终契约，进度报告改写到 stderr。
            with contextlib.redirect_stdout(sys.stderr):
                result = parser.parse(source, staging_dir, options)
    except Exception as e:
        # 解析失败时清理 staging 目录
        shutil.rmtree(staging_dir, ignore_errors=True)
        if emit_text:
            print(f"Error: {e}", file=sys.stderr)
        elapsed = time.perf_counter() - overall_start
        return 1, VideoRecord(
            name=source.name, status="failed",
            total_tokens=0, elapsed_seconds=elapsed,
            **model_fields,
            error=str(e),
        ), None

    # 根据 warnings 有无，将结果移动到最终目录
    has_warnings = len(result.warnings) > 0
    final_dir = _classify_and_move(staging_dir, base_output_dir, has_warnings)
    overall_elapsed = time.perf_counter() - overall_start

    # 打印解析结果统计信息
    if emit_text:
        warning_tag = " [HAS WARNINGS]" if has_warnings else ""
        print(f"Parse complete: status={result.status}{warning_tag}")
        print(f"  Total time: {overall_elapsed:.2f}s")
        print(f"  Final output: {final_dir}")
        print(f"  Transcript segments: {len(result.transcript_segments)}")
        print(f"  Keyframes: {len(result.keyframes)}")
        print(f"  Frame analyses: {len(result.frame_analyses)}")
        print(f"  Sections: {len(result.sections)}")
        print(f"  Warnings: {len(result.warnings)}")
        print(
            f"  Tokens: prompt={result.token_usage.total_prompt_tokens} "
            f"completion={result.token_usage.total_completion_tokens} "
            f"total={result.token_usage.total_tokens}"
        )
        print(
            "  Evidence Markdown: "
            f"{final_dir / result.artifacts.evidence_markdown_path}"
        )
        if result.artifacts.learner_markdown_path:
            print(
                "  Video Summary: "
                f"{final_dir / result.artifacts.learner_markdown_path}"
            )
        else:
            print("  Video Summary: not generated")

    evidence_path = final_dir / result.artifacts.evidence_markdown_path
    summary_path = (
        final_dir / result.artifacts.learner_markdown_path
        if result.artifacts.learner_markdown_path
        else None
    )
    return_code = 4 if result.status == "partial" else 0
    return return_code, VideoRecord(
        name=source.name, status=result.status,
        total_tokens=result.token_usage.total_tokens,
        elapsed_seconds=overall_elapsed,
        asr_tokens=result.token_usage.asr.total_tokens,
        vlm_tokens=result.token_usage.vlm.total_tokens,
        summary_tokens=result.token_usage.sections.total_tokens,
        **model_fields,
        output_dir=final_dir,
        result_json=final_dir / "parse_result.json",
        evidence_markdown=evidence_path,
        summary_markdown=summary_path,
        warnings=[warning.code for warning in result.warnings],
    ), result.token_usage


def main(argv: list[str] | None = None) -> int:
    """主入口：解析参数，批量或单视频处理，返回退出码。"""
    args = build_arg_parser().parse_args(argv)

    try:
        provider_config = load_provider_config(args.env_file)
    except (OSError, ValueError) as exc:
        record = VideoRecord(
            name=str(args.source or args.input_dir or "configuration"),
            status="failed", total_tokens=0, elapsed_seconds=0.0,
            error=str(exc),
        )
        if args.result_format == "json":
            print(json.dumps(build_cli_payload("configuration", [record]), ensure_ascii=False))
        else:
            print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    missing = []
    if not provider_config.openai_api_key:
        missing.append("VIDEO_PARSER_API_KEY")
    if not provider_config.openai_base_url:
        missing.append("VIDEO_PARSER_BASE_URL")
    if missing:
        message = "Missing provider configuration: " + ", ".join(missing)
        record = VideoRecord(
            name=str(args.source or args.input_dir or "configuration"),
            status="failed", total_tokens=0, elapsed_seconds=0.0,
            error=message,
        )
        if args.result_format == "json":
            print(json.dumps(build_cli_payload("configuration", [record]), ensure_ascii=False))
        else:
            print(f"Configuration error: {message}", file=sys.stderr)
        return 2
    args.provider_config = provider_config

    # 把命令行参数转换为解析选项对象
    options = VideoParseOptions(
        language=args.language,
        audio_chunk_seconds=args.audio_chunk_seconds,
        scene_threshold=args.scene_threshold,
        fallback_frame_interval_seconds=args.fallback_frame_interval,
        max_keyframes=args.max_keyframes,
        keyframe_max_width=args.keyframe_max_width,
        dedup_hamming_threshold=args.dedup_hamming_threshold,
        vlm_concurrency=args.vlm_concurrency,
    )

    # 指定了单个 source 文件 → 只处理这一个
    if args.source is not None:
        ret, record, _ = _process_one_video(args.source, args, options)
        if args.result_format == "json":
            print(json.dumps(build_cli_payload("single", [record]), ensure_ascii=False))
        return ret

    # 未指定 source → 批量处理显式 input-dir 或当前目录 input/。
    input_dir = Path(args.input_dir or (Path.cwd() / "input")).expanduser()
    output_root = Path(args.output_root or (Path.cwd() / "output")).expanduser()
    videos = _scan_input_videos(input_dir)
    if not videos:
        message = f"No video files found in {input_dir}"
        record = VideoRecord(
            name=str(input_dir), status="failed", total_tokens=0,
            elapsed_seconds=0.0, error=message,
        )
        if args.result_format == "json":
            print(json.dumps(build_cli_payload("batch", [record]), ensure_ascii=False))
        else:
            print(f"Error: {message}", file=sys.stderr)
        return 1

    total = len(videos)
    if args.result_format == "text":
        print(f"Found {total} video(s) to process.\n")

    success_count = 0
    skip_count = 0
    fail_count = 0
    batch_start = time.perf_counter()

    # 批量汇总 token 用量（按模块细分）
    batch_token_usage = TokenUsage()
    # 每个视频的处理记录，用于最后打印汇总表
    records: list[VideoRecord] = []

    for idx, source in enumerate(videos, start=1):
        if args.result_format == "text":
            print(f"{'='*50}")
            print(f"  [{idx}/{total}] {source.name}")
            print(f"{'='*50}")

        ret, record, usage = _process_one_video(source, args, options)
        records.append(record)

        # 仅对真正处理完成的视频累加 token（跳过/失败的 token=0）
        if usage is not None:
            batch_token_usage.asr.add(usage.asr)
            batch_token_usage.vlm.add(usage.vlm)
            batch_token_usage.sections.add(usage.sections)

        if record.status == "skipped":
            skip_count += 1
        elif record.status in ("complete", "partial"):
            success_count += 1
        else:
            fail_count += 1
        if args.result_format == "text":
            print()

    batch_elapsed = time.perf_counter() - batch_start

    # ── 打印批量汇总表 ──
    if args.result_format == "text":
        _print_batch_summary_table(records)

    # ── 导出 XLSX 汇总文件 ──
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    xlsx_path = output_root / f"batch_summary_{timestamp}.xlsx"
    export_batch_summary_xlsx(records, xlsx_path)

    if args.result_format == "json":
        payload = build_cli_payload("batch", records)
        payload["batch_summary_xlsx"] = str(xlsx_path.resolve())
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"{'='*50}")
        print("  批量处理完成")
        print(f"{'='*50}")
        print(f"  总耗时: {batch_elapsed:.2f}s")
        print(f"  成功: {success_count}  跳过: {skip_count}  失败: {fail_count}")
        print(
            f"  分模型 token: ASR={batch_token_usage.asr.total_tokens} "
            f"VLM={batch_token_usage.vlm.total_tokens} "
            f"概要={batch_token_usage.sections.total_tokens}"
        )
        print(
            f"  总 token: prompt={batch_token_usage.total_prompt_tokens} "
            f"completion={batch_token_usage.total_completion_tokens} "
            f"total={batch_token_usage.total_tokens}"
        )
        print(f"  XLSX 汇总: {xlsx_path}")
        print(f"{'='*50}")

    if fail_count > 0:
        return 1
    if any(record.status == "partial" for record in records):
        return 4
    return 0


def _print_batch_summary_table(records: list[VideoRecord]) -> None:
    """打印批量处理汇总表：序号、文件名、状态、分模型 Token、总 Token、耗时。

    使用 _display_width 计算中文显示宽度，保证列对齐。
    """
    # 列宽配置
    idx_w = 4       # "序号"
    name_w = 36     # "文件名"
    status_w = 10   # "状态"
    token_w = 12    # 分模型和总 Token
    time_w = 10     # "耗时"

    def fmt_tokens(n: int) -> str:
        """token 数值加千分位逗号。"""
        return f"{n:,}"

    def fmt_time(s: float) -> str:
        return f"{s:.2f}s"

    # 表头
    header = (
        f"  {_pad_to_width('序号', idx_w, 'right')}  "
        f"{_pad_to_width('文件名', name_w)}  "
        f"{_pad_to_width('状态', status_w)}  "
        f"{_pad_to_width('ASR Token', token_w, 'right')}  "
        f"{_pad_to_width('VLM Token', token_w, 'right')}  "
        f"{_pad_to_width('概要 Token', token_w, 'right')}  "
        f"{_pad_to_width('总 Token', token_w, 'right')}  "
        f"{_pad_to_width('耗时', time_w, 'right')}"
    )

    sep = "  " + "-" * (_display_width(header) - 2)

    print()
    print("=" * 70)
    print("  批量处理汇总表")
    print("=" * 70)
    print(header)
    print(sep)

    total_asr_tokens = 0
    total_vlm_tokens = 0
    total_summary_tokens = 0
    total_tokens = 0
    total_time = 0.0
    for i, r in enumerate(records, start=1):
        total_asr_tokens += r.asr_tokens
        total_vlm_tokens += r.vlm_tokens
        total_summary_tokens += r.summary_tokens
        total_tokens += r.total_tokens
        total_time += r.elapsed_seconds
        row = (
            f"  {_pad_to_width(str(i), idx_w, 'right')}  "
            f"{_pad_to_width(r.name, name_w)}  "
            f"{_pad_to_width(r.status, status_w)}  "
            f"{_pad_to_width(fmt_tokens(r.asr_tokens), token_w, 'right')}  "
            f"{_pad_to_width(fmt_tokens(r.vlm_tokens), token_w, 'right')}  "
            f"{_pad_to_width(fmt_tokens(r.summary_tokens), token_w, 'right')}  "
            f"{_pad_to_width(fmt_tokens(r.total_tokens), token_w, 'right')}  "
            f"{_pad_to_width(fmt_time(r.elapsed_seconds), time_w, 'right')}"
        )
        print(row)

    print(sep)
    # 合计行
    summary_row = (
        f"  {_pad_to_width('', idx_w, 'right')}  "
        f"{_pad_to_width('合计', name_w)}  "
        f"{_pad_to_width('', status_w)}  "
        f"{_pad_to_width(fmt_tokens(total_asr_tokens), token_w, 'right')}  "
        f"{_pad_to_width(fmt_tokens(total_vlm_tokens), token_w, 'right')}  "
        f"{_pad_to_width(fmt_tokens(total_summary_tokens), token_w, 'right')}  "
        f"{_pad_to_width(fmt_tokens(total_tokens), token_w, 'right')}  "
        f"{_pad_to_width(fmt_time(total_time), time_w, 'right')}"
    )
    print(summary_row)
    print("=" * 70)
if __name__ == "__main__":
    sys.exit(main())
