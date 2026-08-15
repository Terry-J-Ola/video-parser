# Output contract

The wrapper prints one UTF-8 JSON object to stdout. Progress and diagnostic text go to stderr.

## CLI schema

`schema_version` is `video_content_parser.cli.v1`. `mode` is `single`, `batch`, or `configuration`. Each item in `results` contains:

- `name`: source file name.
- `status`: `complete`, `partial`, `skipped`, or `failed`.
- `output_dir`: absolute artifact directory when available.
- `result_json`: absolute structured result path.
- `evidence_markdown`: absolute technical evidence document path.
- `summary_markdown`: absolute evidence-grounded video summary path, or `null` when summary generation failed.
- `warnings`: processing warning codes.
- `error`: fatal error text without credentials.

## Exit codes

- `0`: complete or already processed.
- `1`: invalid input or fatal processing failure.
- `2`: runtime or provider configuration missing.
- `4`: usable partial result; inspect warnings and do not claim full coverage.

The remote provider receives extracted audio and selected frames. Do not process sensitive video without user authorization.
