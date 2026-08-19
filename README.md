# Video Content Parser（视频内容解析器）

从本地视频提取语音转写（ASR）、关键帧与画面分析（VLM）、语义章节（LLM），输出三类结果：
- **结构化 JSON**：可编程二次消费
- **技术证据稿**：按时间线交错呈现转写与画面，逐条可追溯
- **业务讲义**：面向学习者的整理稿，不含内部证据信息

---

## 一、环境准备

### 1. 依赖

- Python 3.10+（建议使用 conda 环境）
- FFmpeg（抽音频 / 抽帧；imageio-ffmpeg 会自动下载，也可自行装系统级）
- 已开通对应模型权限的 API Key（详见下节）

### 2. 安装

```powershell
# 进入项目根目录
cd video-parser

# 安装到当前环境
python -m pip install -e .
```

---

## 二、配置 `.env`

将 `.env.example`（如果存在）复制为 `.env`，或直接编辑项目根目录的 `.env`：

```dotenv
# ========== OpenAI 兼容模式 ==========
# VLM 画面分析、章节构建走这个通道
# 阿里云百炼兼容模式：https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_API_KEY=sk-ws-your-proxy-key-or-official-key
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

# ========== DashScope 原生 API ==========
# paraformer-v2 / FunASR 等专用模型需走原生通道，用阿里云官方 sk- 开头的 key
# 注意：代理 key（sk-ws- 开头）不支持原生 API，会报 403 AccessDenied
DASHSCOPE_API_KEY=sk-your-official-dashscope-key

# ========== 模型选择 ==========
# ASR：
#   - qwen3-asr-flash       走 OpenAI 兼容模式（base64 分片，代理 key 可用）
#   - paraformer-v2         走 DashScope 原生 API（需官方 key，整文件上传+轮询）
ASR_MODEL=paraformer-v2

# VLM（视觉多模态）：qwen3-vl-flash / qwen-vl-plus 等
VLM_MODEL=qwen3-vl-flash

# 章节构建（文本 LLM）：qwen-plus / qwen3.7-plus 等
SECTION_MODEL=qwen-plus
```

⚠️ **paraformer-v2 注意**：必须用**阿里云百炼控制台**生成的官方 key（`sk-` 开头）。第三方代理 key（`sk-ws-` 开头）只支持 OpenAI 兼容模式，调用原生 API 会返回 `AccessDenied: current user api does not support synchronous calls`。

---

## 三、快速使用

### 方式 A：一键脚本（推荐，最方便）

将视频放入项目根目录的 `input/` 文件夹，然后：

```cmd
run.bat
```

可选参数：
```cmd
run.bat --force                                :: 强制重新解析（跳过缓存判断）
run.bat --scene-threshold 0.05                 :: 更敏感的场景切换检测（PPT视频推荐）
run.bat --fallback-frame-interval 5            :: 固定抽帧间隔改为 5 秒
run.bat --vlm-concurrency 8                    :: VLM 并发调至 8 路
```

### 方式 B：单个视频（指定路径）

```powershell
# 输出到默认目录 output/<视频名>/
video-content-parser D:\videos\demo.mp4

# 自定义输出目录
video-content-parser D:\videos\demo.mp4 D:\my-output\demo

# 强制重跑
video-content-parser D:\videos\demo.mp4 --force
```

### 方式 C：批量处理 `input/` 目录

不传路径参数即自动批量模式：

```powershell
video-content-parser               # 处理 input/ 下所有视频，自动跳过已 complete 的
video-content-parser --force       # 全部强制重跑
video-content-parser --input-dir D:\videos --output-root D:\out    # 自定义批量路径
```

批量模式运行结束后会：
1. 在控制台打印**汇总表**（每个视频的文件名 / 状态 / Token 总量 / 耗时）
2. 在 `output/` 目录下导出 **CSV 汇总文件**：`batch_summary_YYYYMMDD_HHMMSS.csv`

---

## 四、全部命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `source` | 无 | 单个视频路径；不填则进入批量模式扫描 `input/` |
| `output_dir` | 无 | 单个视频的自定义输出目录；批量模式用 `--output-root` |
| `--input-dir` | `./input/` | 批量模式的扫描目录 |
| `--output-root` | `./output/` | 批量模式的输出根目录 |
| `--force, -f` | 关 | 强制重新解析（忽略 `status=complete` 缓存） |
| `--language` | 自动 | 语言提示：`zh` / `en` 等 |
| `--scene-threshold` | `0.30` | 场景切换阈值（0~1，越小越敏感）；PPT 课件推荐 `0.05` |
| `--fallback-frame-interval` | `10.0` | 场景检测失败时的**固定抽帧间隔（秒）** |
| `--max-keyframes` | `60` | 单视频最多保留关键帧数（1~200） |
| `--dedup-hamming-threshold` | `6` | 感知哈希去重阈值；`0` = 完全不去重 |
| `--vlm-concurrency` | `4` | VLM 批次最大并发数（1~16）；API 限流时可减小 |
| `--keyframe-max-width` | `1280` | 关键帧最大宽度（超出则按比例缩放） |
| `--audio-chunk-seconds` | `30` | ASR 音频分片时长（秒），qwen3-asr-flash 适用 |
| `--result-format` | `text` | `text` 人类可读 / `json` 纯结构化输出（便于自动化） |
| `--env-file` | 无 | 显式指定 `.env` 文件路径 |
| `--help, -h` | — | 查看完整帮助 |

---

## 五、输出目录结构

### 单个视频输出

```
output/<视频名>/                        :: 无警告时放这里
output/_warnings/<视频名>/              :: 有警告时自动分流到此处
├── parse_result.json                   :: 完整结构化数据（含 token 用量）
├── <视频名>_技术证据稿.md              :: 时间线交错：转写 🔊 + 关键帧 🖼️ + 画面分析 + 告警
├── <视频名>_业务讲义.md                :: 章节化业务文章（learner_document 非空时才生成）
└── assets/
    ├── audio.mp3                       :: 抽取的整段音频
    └── keyframes/
        ├── frame-0001.jpg
        ├── frame-0002.jpg
        └── ...
```

### 批量模式额外产物

```
output/batch_summary_20260818_153022.csv      :: 汇总表（UTF-8 BOM）
output/logs/parse_20260818_225648.log         :: 本次运行的完整日志
```

CSV 列：`序号、文件名、状态、Token总量、耗时(秒)`，最后一行合计，带 UTF-8 BOM，Excel/WPS 双击可直接打开。

### parse_result.json 顶层字段

```json
{
  "schema_version": "1.0",
  "status": "complete | partial",
  "source_name": "xxx.mp4",
  "duration_ms": 146000,
  "title": "自动生成的标题",
  "languages": ["zh"],
  "summary": "...",
  "artifacts": { ... },
  "token_usage": {                    // ← 新增：三模块 token 用量分别统计
    "asr":      { "prompt_tokens":0, "completion_tokens":0, "total_tokens":0, ... },
    "vlm":      { "prompt_tokens":0, "completion_tokens":0, "total_tokens":0, "image_tokens":0, "audio_tokens":0 },
    "sections": { "prompt_tokens":0, "completion_tokens":0, "total_tokens":0, ... },
    "total_prompt_tokens": 0,
    "total_completion_tokens": 0,
    "total_tokens": 0
  },
  "transcript_segments": [ { "id":"transcript-0001", "start_ms":0, "end_ms":30000, "text":"...", ... } ],
  "keyframes":         [ { "id":"frame-0001", "timestamp_ms":0, "image_path":"assets/keyframes/frame-0001.jpg",
                           "selection_reason":"scene_change | interval_fallback", "perceptual_hash":"...", ... } ],
  "frame_analyses":    [ { "keyframe_id":"frame-0001", "content_type":"...", "description":"...", ... } ],
  "sections":          [ ... ],
  "learner_document":  { ... },      // 业务讲义数据，null 时不生成 *_业务讲义.md
  "warnings":          [ { "code":"ASR_FAILED", "message":"..." }, ... ]
}
```

---

## 六、跳过缓存 & 强制重跑机制

解析前会检查输出目录下 `parse_result.json` 的顶层 `status` 字段：

- `status = "complete"` 且**未加 `--force`** → 跳过（提示：已解析，加 `--force` 可强制重跑）
- `status = "partial"` 或文件不存在 → 直接重跑，**不需要 `--force`**
- 任何情况下加了 `--force` → 一定重跑

⚠️ 警告分流目录 `output/_warnings/<视频名>/` 和正常目录 `output/<视频名>/` 都会被检查；只要其中一个存在 complete 结果即视为已处理。

---

## 七、耗时 & Token 监控

每个视频跑完会在控制台打印：

```
==================================================
  耗时报告：demo.mp4
==================================================
  探测时长        0.52s  (  0.5%)
  抽取音频        8.31s  (  8.3%)
  ASR 转写       12.05s  ( 12.1%)
  抽取关键帧      5.73s  (  5.7%)
  复制关键帧      0.03s  (  0.0%)
  VLM 画面分析   62.18s  (62.3%)
  章节构建       10.44s  (10.5%)
  写入文件        0.12s  (  0.1%)
  ────────────────────────────
  合计          99.88s
==================================================
  Token 用量：demo.mp4
==================================================
  ASR         prompt=xxx  completion=xxx  total=xxx
  VLM         prompt=xxx  completion=xxx  total=xxx  (image=xxxx audio=xxxx)
  Sections    prompt=xxx  completion=xxx  total=xxx
  ────────────────────────────
  合计                  prompt=xxx  completion=xxx  total=xxx
==================================================
```

通过「VLM 画面分析」占比可以直观判断 `--vlm-concurrency` 是否生效；通过 Token 用量可以估算每月 API 账单。

---

## 八、日志系统与错误追踪

每次运行会在 `output/logs/` 下生成一个带时间戳的日志文件：`parse_YYYYMMDD_HHMMSS.log`。

### 双通道输出

| 通道 | 级别 | 格式 | 用途 |
|------|------|------|------|
| 控制台 | INFO 及以上 | `时间 [级别] [视频名] 消息` | 实时查看进度 |
| 文件 | DEBUG 及以上 | `日期 时间 [级别] [模块] [视频名] 消息 + 堆栈` | 事后回查、排查错误 |

### 日志分级

| 级别 | 含义 | 控制台可见 | 典型场景 |
|------|------|:---:|------|
| `DEBUG` | 逐 chunk / 逐批次的详细过程 | ✗ | ASR 每个 chunk 转写结果、VLM 每批成功帧数、去重判断细节 |
| `INFO` | 处理里程碑 | ✓ | 音频抽取完成、ASR 转写完成、关键帧抽取完成、VLM 分析完成 |
| `WARNING` | 非致命问题 | ✓ | 单个 chunk 转写失败、部分失败统计、关键帧去重丢弃 |
| `ERROR` | 重试耗尽 | ✓ | chunk 重试 3 次后仍失败 |
| `exception` | 异常 + 完整堆栈 | ✓ | ASR/VLM/章节构建整体失败、视频解析失败 |

### 视频名前缀

批量处理时每条日志自动带 `[视频名]` 前缀，方便快速定位某个视频的全部日志：

```
grep "FABE" output/logs/parse_*.log
```

### 错误追踪示例

当 ASR 某个 chunk 失败时，日志会完整记录重试→失败→降级全过程：

```
23:00:00 [DEBUG]  chunk chunk-0010.mp3 第 1 次尝试失败，准备重试: 服务商返回了空转写结果
23:00:01 [DEBUG]  chunk chunk-0010.mp3 第 2 次尝试失败，准备重试: 服务商返回了空转写结果
23:00:02 [ERROR]  chunk chunk-0010.mp3 重试 3 次后仍失败: 服务商返回了空转写结果
23:00:02 [WARNING] chunk 11/16 转写失败: chunk-0010.mp3 - Qwen ASR 转写失败...
23:00:07 [WARNING] ASR 部分失败: 15/16 个 chunk 成功，1 个失败
```

从这条链可以一眼看出：哪个 chunk 失败了、重试了几次、失败原因是什么、对整体的影响多大。

### 各模块日志覆盖

| 模块 | 记录的关键信息 |
|------|----------|
| ASR (`asr.py`) | 分片数量、每个 chunk 成功/失败/重试、部分失败统计 |
| 关键帧 (`keyframes.py`) | 场景检测统计（图片数/时间戳数/配对数）、去重统计（dHash 丢弃/像素救回） |
| VLM (`frame_analysis.py`) | 批次数/并发数、批次重试退避、单帧降级、单帧失败 |
| 章节构建 (`sections.py`) | 构建启动（输入证据数）、重试尝试、空响应、确定性回退 |
| 主流程 (`parser.py`) | 各步骤里程碑、异常 `logger.exception` 堆栈 |
| CLI (`__main__.py`) | 批量进度 `N/M`、跳过、完成统计、解析失败堆栈 |

> **提示**：控制台的耗时报告和 Token 报告（`print` 输出）是用户可见的 CLI 产物，不属于日志系统；日志系统（`logger` 输出）记录的是内部处理过程和错误堆栈，用于事后排查。

---

## 九、常见问题 FAQ

**Q1. 为什么 ASR 转写一段都没有（transcript_segments=0）？**

A：看 `parse_result.json` → `warnings[0].message`。常见原因：
- `ASR_FAILED`（chunk-xxx 返回空）：ASR 模型偶发返回空，重新加 `--force` 跑一次通常能恢复。**修复后单 chunk 失败不再清零其他 chunk 的成功结果**。
- 降级到 FakeAsrBackend：`.env` 中 `ASR_MODEL` / `OPENAI_API_KEY` / `OPENAI_BASE_URL` 有缺失。查看 `output/logs/parse_*.log` 日志文件中的 WARNING 级别记录。
- paraformer-v2 返回 403：用了代理 key 走原生通道，必须换成官方 key。

**Q2. 为什么关键帧的 `selection_reason` 全是 `interval_fallback`？**

A：场景切换检测（`select='gt(scene,threshold)'`）默认阈值 `0.30` 太严，PPT 课件的画面切换是淡入淡出，`scene` 分数不跳变。改用：
```powershell
video-content-parser xxx.mp4 --scene-threshold 0.05 --force
```

**Q3. 为什么抽到的关键帧比预想的少？**

A：存在**感知哈希去重**（`--dedup-hamming-threshold`，默认 6）。相邻两帧太像会被丢弃。如果想完全保留：
```powershell
video-content-parser xxx.mp4 --dedup-hamming-threshold 0 --force
```

**Q4. 技术证据稿里转写和关键帧是分开的吗？**

A：**不是**。已改为**时间线交错**格式：转写（🔊）和关键帧（🖼️）合并后按时间戳统一排序，阅读体验更像带画面的字幕稿。证据稿按以下顺序组织：
- 头部元信息
- 「时间线证据」区（🔊 转写 + 🖼️ 关键帧+画面分析，按时间戳交错）
- 处理告警区

**Q5. 为什么 `*_业务讲义.md` 显示 "Business Handout: not generated"？**

A：`learner_document` 为 `null` 时不生成业务讲义。通常由 `SECTIONS_FAILED` 或 `CONTENT_PARTIAL`（章节构建步骤失败）导致。检查 warnings 中具体错误码。

---

## 十、与 Trae Skill 集成

`.trae/skills/video-parser/SKILL.md` 定义了项目级 Skill。
- **用法**：在 Trae 对话中说「帮我处理一下 `xxx.mp4`」「把 `input/` 里的视频都转成讲义」，Trae 会自动加载 Skill 并按步骤执行。
- **分享**：将完整项目（含 `.trae/` 目录）通过 Git/压缩包分享给同事，同事用 Trae 打开项目即自动加载 Skill。
- **全局安装**：将 `.trae/skills/video-parser` 文件夹拷贝到 `C:\Users\<用户名>\.trae-cn\skills\`，重启 Trae 即可在任何项目中调用。

Skill 只是调用此 CLI 的快捷指令；**接收方机器仍需装好项目运行环境（Python、Conda、FFmpeg、API Key 配置）**。

---

## 十一、项目目录结构速览

```
video-parser/
├── run.bat                              :: 一键启动脚本（推荐）
├── .env                                 :: API Key 与模型配置
├── input/                               :: 视频放入这里（批量模式扫描此目录）
├── output/                              :: 解析结果 + batch_summary CSV + 日志
│   ├── <视频名>/                        :: 无警告的视频
│   ├── _warnings/<视频名>/              :: 有警告的视频
│   ├── logs/parse_*.log                 :: 每次运行的完整日志（DEBUG 级，含堆栈）
│   └── batch_summary_*.csv              :: 批量模式汇总表（UTF-8 BOM）
├── .trae/skills/video-parser/           :: Trae 对话式封装（Skill）
├── src/video_content_parser/
│   ├── __main__.py                      :: CLI 入口：参数解析、批量循环、汇总表、CSV 导出
│   ├── parser.py                        :: 主流程串联 + 步骤耗时监控
│   ├── logging_config.py                :: 日志系统：双通道输出 + 视频名前缀 + 错误堆栈
│   ├── config.py                        :: Provider 配置读取
│   ├── asr.py                           :: ASR 后端（QwenAsrBackend + FakeAsrBackend）
│   ├── frame_analysis.py                :: VLM 后端（AsyncOpenAI + Semaphore 并发）
│   ├── sections.py                      :: 章节构建（Chat LLM + 回退到时间窗口）
│   ├── keyframes.py                     :: 关键帧抽取（scene 检测 + interval 兜底 + dHash 去重）
│   ├── markdown.py                      :: 技术证据稿 & 业务讲义 Markdown 渲染
│   ├── writer.py                        :: 原子写入（先写临时文件再 os.replace）
│   ├── omni.py                          :: 流式文本收集（同步 + 异步版本）
│   ├── models.py                        :: Pydantic 数据模型 + TokenUsage
│   ├── options.py                       :: CLI 参数默认值与约束
│   ├── ffmpeg.py                        :: FFmpeg 封装
│   ├── audio.py                         :: 音频抽取、分片
│   └── errors.py                        :: 自定义异常（ModelProviderError 等）
└── README.md
```
