# Output contract

The wrapper prints one UTF-8 JSON object to stdout. Progress and diagnostic text go to stderr.

## CLI schema

`schema_version` is `video_content_parser.cli.v3`. `mode` is `single`, `batch`, or `configuration`. Each item in `results` contains:

- `name`: source file name.
- `status`: `complete`, `partial`, `skipped`, or `failed`.
- `total_tokens`: total provider-reported tokens consumed by the video; `0` when usage is unavailable, skipped, or failed.
- `token_usage`: per-stage token totals and the configured model names:
  - `asr`: `model` and `total_tokens` for audio transcription.
  - `vlm`: `model` and `total_tokens` for keyframe analysis.
  - `summary`: `model` and `total_tokens` for structured sections and the video summary.
- `elapsed_seconds`: wall-clock processing time for the video in seconds.
- `output_dir`: absolute artifact directory when available.
- `result_json`: absolute structured result path.
- `evidence_markdown`: absolute technical evidence document path.
- `summary_markdown`: absolute evidence-grounded video summary path, or `null` when summary generation failed.
- `warnings`: processing warning codes.
- `error`: fatal error text without credentials.

Batch mode also includes `batch_summary_xlsx`, the absolute path to an XLSX workbook. The workbook contains one row per video with the ASR, VLM, and summary model names, their individual token totals, the video token total, and elapsed seconds. Formula-driven totals separately aggregate the three model stages, all tokens, video count, and elapsed seconds.

## Exit codes

- `0`: complete or already processed.
- `1`: invalid input or fatal processing failure.
- `2`: runtime or provider configuration missing.
- `4`: usable partial result; inspect warnings and do not claim full coverage.

The remote provider receives extracted audio and selected frames. Do not process sensitive video without user authorization.
