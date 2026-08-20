---
name: summarize-video
description: Convert local video files into a faithful Chinese content summary, a timestamp-interleaved technical evidence document, structured JSON, a batch XLSX statistics workbook, and DEBUG-level error-tracing logs by using the video-content-parser CLI. Use when Codex needs to summarize one or more local training, presentation, demonstration, interview, or business videos; extract what a video actually says; generate auditable Markdown evidence without inventing unsupported content; or diagnose a failed/partial parse via the run log.
---

# Summarize Video

Use the bundled scripts instead of reconstructing shell commands. Keep every output grounded in the parser's ASR and frame evidence.

## Workflow

1. Resolve every input video to an absolute local path. Do not upload or copy the video elsewhere yourself.
2. Run `python scripts/check_runtime.py`.
3. If configuration is missing, ask the user to run `python scripts/configure.py`. Never request that the user paste an API key into chat.
4. If the CLI runtime is missing or `runtime_update_required` is true, run `python scripts/install_runtime.py`. When the Skill is installed outside this repository, supply `--source` with the approved Python package or Git URL.
5. Run one video with:

   ```text
   python scripts/run_parser.py <absolute-video-path> <absolute-output-directory>
   ```

6. For a directory, run:

   ```text
   python scripts/run_parser.py --input-dir <absolute-input-directory> --output-root <absolute-output-root>
   ```

7. Parse the JSON printed to stdout. Read `references/output-contract.md` when interpreting exit codes or artifact fields.
8. Return clickable paths for the video summary, technical evidence Markdown, and `parse_result.json`. For batch runs, also return the `batch_summary_*.xlsx` workbook containing per-video ASR, VLM, summary-model and total token usage, plus per-video and total elapsed time. Report warnings without presenting them as video content.
9. When the result is `partial`, `failed`, or carries warnings, point the user to the run log at `<output-root>/logs/parse_*.log`. The log carries per-video prefixes (`[视频名]`), per-chunk / per-batch DEBUG detail, and full exception tracebacks (`logger.exception`). Use it to localize ASR chunk failures, VLM batch fallbacks, and keyframe deduplication decisions before re-running with `--force`.

## Content Rules

- Treat the generated summary as a compression of input evidence, not an independent article.
- Preserve explicitly stated names, terms, numbers, conditions, examples, and sequence. The parser enforces a fidelity mode that retains transcript information density; do not paraphrase away specifics the parser preserved.
- Do not add explanations, recommendations, background, value claims, or conclusions absent from the evidence.
- Keep timestamps, evidence IDs, processing diagnostics, and model metadata out of the summary; they belong in the technical evidence document, which interleaves transcript segments and keyframes by timestamp with 🔊 / 🖼️ markers.
- Do not add images to the summary. Keyframes remain available in the technical evidence document and assets directory.

## Security and Privacy

- Keep API keys in environment variables or the user-level config created by `configure.py`.
- Never print, echo, log, or place keys in command arguments, Markdown, JSON results, or the Skill directory. The parser's own log file respects this rule: keys never appear in `output/logs/parse_*.log`.
- Explain that the parser sends extracted audio and selected video frames to the configured remote model provider. Ask before processing privacy-sensitive material when the user has not already authorized that transfer.
- Do not publish or copy input videos, audio, frames, or output artifacts.
