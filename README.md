# Video Content Parser CLI

从本地视频提取语音文本、关键帧和语义章节，输出忠实的视频内容概要、技术证据稿和结构化 JSON。

## 安装

```powershell
python -m pip install .
```

## 配置

复制 `.env.example` 为 `.env`，配置以下字段：

```text
VIDEO_PARSER_API_KEY=your-api-key
VIDEO_PARSER_BASE_URL=https://your-base-url/v1
VIDEO_PARSER_ASR_MODEL=qwen3-asr-flash
VIDEO_PARSER_VLM_MODEL=qwen3-vl-flash
VIDEO_PARSER_SUMMARY_MODEL=qwen3.7-plus
```

API Key 必须已开通对应模型权限。

环境变量优先级最高，也可以通过 `--env-file <配置文件>` 显式指定配置。插件内的 Skill 使用用户目录下的配置文件，不会把 Key 写进插件。

## 启动

指定输入和输出路径：

```powershell
video-content-parser <input_video> <output_dir>
python -m video_content_parser <input_video> <output_dir>
```

不传路径时，CLI 批量处理当前工作目录 `input/` 中的全部支持视频，并写入 `output/<视频名>/`。运行以下命令查看全部参数：

```powershell
video-content-parser --help
```

安装后，默认的 `input/` 和 `output/` 均相对于当前工作目录。批量运行可以显式指定：

```powershell
video-content-parser --input-dir <视频目录> --output-root <输出根目录>
```

供 Plugin、Skill 或自动化调用时，使用纯 JSON 标准输出：

```powershell
video-content-parser <视频路径> <输出目录> --result-format json
```

## 输出

- `parse_result.json`
- `<视频名>_技术证据稿.md`：按时间展示转写、关键帧、画面分析和告警
- `<视频名>_业务讲义.md`：根据输入证据生成的视频内容概要；不扩写证据未表达的内容，不展示时间轴、证据 ID 或关键帧图片
- `assets/audio.mp3`
- `assets/keyframes/*.jpg`
- 批量模式额外生成 `batch_summary_YYYYMMDD_HHMMSS.xlsx`：逐视频记录 ASR、VLM、概要模型名称及各自 Token、Token 总量和耗时，并通过公式分别汇总三类模型 Token、总 Token、视频数量和总耗时

## Codex Plugin

仓库根目录是 `video-parser` 插件，正式清单位于 `.codex-plugin/plugin.json`。插件包含 `skills/summarize-video/` 工作流以及同版本的 Python CLI 源码。

首次使用时，Codex 会检查运行环境；如果 CLI 尚未安装，Skill 会在用户目录 `~/.video-parser/runtime` 创建私有虚拟环境并从当前插件根目录安装。如果尚未配置模型服务，Skill 会引导用户通过隐藏输入将 Key 写入用户级配置文件。

插件当前采用最小的 Skills-only 形态，不包含 MCP 服务或自定义 UI。发布到公开仓库后，可以加入仓库 Marketplace 或提交到 ChatGPT 与 Codex 共用的公共 Plugins Directory。

### 从 GitHub 安装插件

其他用户安装 Codex/ChatGPT 桌面应用后，可以在终端添加本仓库 Marketplace：

```powershell
codex plugin marketplace add Terry-J-Ola/video-parser --ref main
```

然后在桌面应用的 Plugins Directory 中选择 `Terry-J-Ola Plugins`，安装 `Video Content Parser`，并新建一个任务。例如告诉 Codex：

```text
使用 summarize-video，把 D:\videos 中的视频处理到 D:\video-output。
```

首次运行时，Skill 会检查 CLI 和模型服务配置。用户需要 Python 3.10+，并能访问 Python 依赖源和所配置的模型服务；Skill 会把 Python 依赖安装到用户目录下的私有运行时。媒体处理使用 `imageio-ffmpeg` 提供的 FFmpeg，不要求用户另行配置系统 `ffprobe`。API Key 只保存在用户本机，不会上传到 GitHub 或写入视频产物。
