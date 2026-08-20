# Video Content Parser（视频内容解析器）

`video-parser` 是一个 Codex Plugin，也可以作为独立 Python CLI 使用。它从本地视频提取音频和关键帧，调用远程模型完成 ASR、画面理解和内容整理，最终生成：

- 忠实的视频内容概要（业务讲义）
- 按时间戳交错排列的技术证据稿
- 结构化 `parse_result.json`
- 批量 XLSX 耗时与分模型 Token 统计
- DEBUG 级运行日志

输入视频保留在本地；提取后的音频和选定关键帧会发送给配置的远程模型服务。处理敏感视频前应确认已获得授权。

## 默认模型与配置

默认模型：

| 阶段 | 默认模型 |
|---|---|
| 音频转写 | `qwen3-asr-flash` |
| 关键帧理解 | `qwen3-vl-flash` |
| 章节与概要 | `qwen3.7-plus` |

推荐使用插件附带的配置脚本，它把 Key 写入用户私有配置文件，不会写入仓库或输出产物：

```powershell
python skills\summarize-video\scripts\configure.py
```

也可以通过环境变量或 `.env` 配置：

```dotenv
VIDEO_PARSER_API_KEY=your-key
VIDEO_PARSER_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VIDEO_PARSER_ASR_MODEL=qwen3-asr-flash
VIDEO_PARSER_VLM_MODEL=qwen3-vl-flash
VIDEO_PARSER_SUMMARY_MODEL=qwen3.7-plus
```

兼容旧变量名 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`ASR_MODEL`、`VLM_MODEL` 和 `SECTION_MODEL`。读取优先级为：系统环境变量、显式 `--env-file`、用户私有配置、当前目录 `.env`。

## 安装与运行

### Codex Plugin

仓库地址：[Terry-J-Ola/video-parser](https://github.com/Terry-J-Ola/video-parser)

安装插件后，Codex 会通过 `skills/summarize-video/scripts/` 中的包装脚本检查配置、安装隔离运行时并调用 CLI。Skill 的标准调用入口为：

```powershell
# 检查运行时和配置，不显示 Key
python skills\summarize-video\scripts\check_runtime.py

# 单视频
python skills\summarize-video\scripts\run_parser.py "D:\videos\demo.mp4" "D:\outputs\demo"

# 批量目录
python skills\summarize-video\scripts\run_parser.py --input-dir "D:\videos" --output-root "D:\outputs"
```

`run_parser.py` 保证 stdout 只输出一个 UTF-8 JSON 对象，进度与诊断写入 stderr。

### 独立 CLI

开发环境安装：

```powershell
python -m pip install -e .
video-content-parser --version
```

常用命令：

```powershell
# 单视频；默认输出到 ./output/<视频名>/
video-content-parser "D:\videos\demo.mp4"

# 指定单视频输出目录
video-content-parser "D:\videos\demo.mp4" "D:\outputs\demo"

# 批量处理目录第一层的视频
video-content-parser --input-dir "D:\videos" --output-root "D:\outputs"

# 强制重新解析已有 complete 结果
video-content-parser "D:\videos\demo.mp4" "D:\outputs\demo" --force
```

批量扫描不递归。支持 `.mp4`、`.mkv`、`.avi`、`.mov`、`.wmv`、`.flv`、`.webm` 和 `.m4v`。

## CLI 参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `source` | 无 | 单视频路径；不填时进入批量模式 |
| `output_dir` | `./output/<视频名>/` | 单视频输出目录 |
| `--input-dir` | `./input/` | 批量输入目录 |
| `--output-root` | `./output/` | 批量输出根目录 |
| `--env-file` | 无 | 显式 Provider 配置文件 |
| `--result-format` | `text` | `text` 或 `json` |
| `--force, -f` | 关闭 | 忽略已有 complete 结果并重跑 |
| `--language` | 自动 | ASR 语言提示，如 `zh`、`en` |
| `--audio-chunk-seconds` | `30` | ASR 音频分片秒数，范围 15～60 |
| `--audio-overlap-seconds` | `2.0` | 相邻 ASR 分片重叠秒数，减少句子截断 |
| `--scene-threshold` | `0.30` | 场景切换阈值，越低越敏感 |
| `--fallback-frame-interval` | `10.0` | 场景检测无结果时的固定抽帧间隔 |
| `--max-keyframes` | `60` | 单视频最多保留关键帧数 |
| `--keyframe-max-width` | `1280` | 关键帧最大宽度 |
| `--dedup-hamming-threshold` | `6` | 相邻帧 dHash 去重阈值；`0` 表示关闭去重 |
| `--vlm-concurrency` | `4` | VLM 批次最大并发数 |

## 处理流程

```text
本地视频
  ├─ FFmpeg 提取音频 → 重叠分片 → qwen3-asr-flash
  ├─ 场景检测/间隔回退 → dHash + 像素复核 → qwen3-vl-flash
  └─ 转写与画面证据 → qwen3.7-plus
       ├─ 技术证据稿
       ├─ 忠实内容概要
       └─ parse_result.json
```

处理特点：

- ASR 分片默认保留 2 秒重叠区，并对相邻结果去重。
- VLM 批次请求失败时降级为逐帧分析，尽量保留可用证据。
- 技术证据稿把转写和画面证据按时间戳交错排列。
- 内容概要只压缩已有证据，保留明确出现的名称、数字、条件、示例和顺序。
- 任一核心阶段不完整时结果标记为 `partial`，不会伪装成完整结果。

## 输出结构

无警告结果写入正常目录；带警告结果写入 `_warnings`：

```text
<output-root>/
├─ <视频名>/
│  ├─ parse_result.json
│  ├─ <视频名>_技术证据稿.md
│  ├─ <视频名>_业务讲义.md          # learner_document 成功时生成
│  └─ assets/
│     ├─ audio.mp3
│     ├─ _chunks/*.mp3
│     └─ keyframes/*.jpg
├─ _warnings/<视频名>/              # partial 或存在警告时
├─ logs/parse_YYYYMMDD_HHMMSS.log
└─ batch_summary_YYYYMMDD_HHMMSS.xlsx
```

批量 XLSX 在一个工作表中记录：

- 视频文件名和状态
- ASR、VLM、概要模型名称及各自 Token
- 单视频总 Token 和耗时
- 分阶段 Token、批次总 Token、视频数量和总耗时的公式汇总

Token 以服务端返回的 usage 为准；服务未返回、跳过或失败时可能为 `0`。

## JSON 契约

`parse_result.json` 顶层 schema 为 `video_parse_result.v3`：

```json
{
  "schema_version": "video_parse_result.v3",
  "app_version": "0.4.0",
  "runtime_package_path": "D:\\path\\to\\video_content_parser",
  "status": "complete",
  "source_name": "demo.mp4",
  "duration_ms": 146000,
  "title": "视频标题",
  "languages": ["zh"],
  "summary": "忠实内容概要",
  "artifacts": {
    "audio_path": "assets/audio.mp3",
    "evidence_markdown_path": "demo_技术证据稿.md",
    "learner_markdown_path": "demo_业务讲义.md"
  },
  "transcript_segments": [],
  "keyframes": [],
  "frame_analyses": [],
  "sections": [],
  "learner_document": {},
  "warnings": [],
  "token_usage": {
    "asr": {"model": "qwen3-asr-flash", "total_tokens": 0},
    "vlm": {"model": "qwen3-vl-flash", "total_tokens": 0},
    "sections": {"model": "qwen3.7-plus", "total_tokens": 0}
  }
}
```

`app_version` 与 `runtime_package_path` 用于确认本次任务实际加载了哪个版本、哪一份 Python 包；运行日志启动部分也会记录相同信息。

Skill 包装层 stdout 使用独立的 `video_content_parser.cli.v3` 契约，包含 `mode`、状态汇总、逐视频结果，以及批量模式的 `batch_summary_xlsx`。完整字段和退出码见 [output-contract.md](skills/summarize-video/references/output-contract.md)。

## 状态、跳过与退出码

- 已存在 `status=complete` 的结果时默认跳过；`--force` 可重跑。
- `partial` 结果不会被跳过，下次运行会重新处理。
- 有任何 warning 的结果会被分流到 `_warnings/<视频名>/`。
- 退出码 `0`：完成或跳过。
- 退出码 `1`：输入无效或致命失败。
- 退出码 `2`：运行时或 Provider 配置缺失。
- 退出码 `4`：存在可用但不完整的 partial 结果。

## 日志与排错

每次真实运行在 `<output-root>/logs/` 创建 `parse_*.log`：

| 通道 | 级别 | 内容 |
|---|---|---|
| 控制台 | INFO 及以上 | 当前视频、阶段进度和结果 |
| 文件 | DEBUG 及以上 | ASR 分片、VLM 批次与单帧降级、关键帧去重、重试和完整异常堆栈 |

批量日志带 `[视频名]` 前缀。日志不记录 API Key。结果为 `partial`、`failed` 或带警告时，应优先查看对应运行日志。

## 开发验证

```powershell
# 使用可用的 Python 运行测试
python -m unittest discover -s tests -v

# 验证源码 CLI
$env:PYTHONPATH = "$PWD\src"
python -m video_content_parser --version
python -m video_content_parser --help
```

发布版本需要同时更新：

- `.codex-plugin/plugin.json`
- `pyproject.toml`
- `src/video_content_parser/__init__.py`
- `skills/summarize-video/runtime-version.txt`

安装版插件和私有 CLI 只有在执行插件更新/重装后才会切换到新版本。
