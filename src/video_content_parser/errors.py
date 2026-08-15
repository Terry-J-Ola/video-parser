"""视频解析流程的自定义异常层次。

所有业务异常均继承自 VideoParserError，便于上层统一捕获与处理。
"""


class VideoParserError(Exception):
    """所有视频解析相关异常的基类。"""
    pass


class InvalidVideoInputError(VideoParserError):
    """输入视频路径或格式不合法时抛出。"""
    pass


class MediaProcessingError(VideoParserError):
    """FFmpeg 等媒体处理工具执行失败时抛出。"""
    pass


class NoUsableContentError(VideoParserError):
    """视频未产出可用内容（无音频、无关键帧等）时抛出。"""
    pass


class ModelProviderError(VideoParserError):
    """调用外部模型 Provider（ASR / VLM 等）失败时抛出。"""
    pass


class ResultWriteError(VideoParserError):
    """解析结果写入磁盘失败时抛出。"""
    pass
