from pydantic import Field
from typing import Dict, Any, Optional

from nekro_agent.api.plugin import ExtraField
from nekro_agent.core.config import ModelConfigGroup
from nekro_agent.core.config import config as core_config
from nekro_agent.services.plugin.base import ConfigBase, NekroPlugin

plugin = NekroPlugin(
    name="语音合成插件",
    module_name="genie_tts",
    description="提供文本到语音合成功能",
    version="1.0.0",
    author="XGGM",
    url="https://github.com/XG2020/genie_tts",
)


@plugin.mount_config()
class TTSConfig(ConfigBase):
    API_URL: str = Field(
        default="https://your-name-your-space.hf.space",
        title="TTS API URL",
        description="单服务模式下的Genie TTS服务基础URL。",
    )
    TTS_SERVERS: list[str] = Field(
        default_factory=list,
        title="TTS服务列表",
        description="多服务地址列表，启用后优先使用并支持故障切换。",
    )
    ENABLE_PROXY_ACCESS: bool = Field(
        default=False,
        title="启用代理访问",
        description="启用后通过系统默认代理访问 Genie TTS 服务 API。",
    )
    ENABLE_SPACE_KEEPALIVE: bool = Field(
        default=False,
        title="启用自动保活",
        description="启用后会定期访问TTS服务地址，降低Hugging Face Space休眠概率。",
    )
    SPACE_KEEPALIVE_URL: str = Field(
        default="",
        title="保活目标地址",
        description="可选，额外保活地址。为空时仅保活 TTS_SERVERS/API_URL。",
    )
    SPACE_KEEPALIVE_INTERVAL_MINUTES: int = Field(
        default=25,
        title="保活间隔分钟",
        description="两次保活请求之间的间隔分钟数，最小为1。",
    )
    token: str = Field(
        default="None",
        title="token",
        description="可选的 Bearer Token，不需要可留空或None。",
    )
    DEFAULT_MODEL: str = Field(
        default="feibi",
        title="角色名",
        description="Genie服务中的角色名，可通过 /genie_tts_set 动态修改。",
    )
    DEFAULT_EMOTION_NAME: str = Field(
        default="",
        title="默认情感名",
        description="可选。若已注册该角色情感，将优先使用该情感的参考音频。",
    )
    ENABLE_AUTO_EMOTION_RECOGNITION: bool = Field(
        default=False,
        title="启用自动情感识别",
        description="启用后会根据文本内容自动匹配当前角色下最合适的已注册情感。",
    )
    AUTO_EMOTION_MODEL: str = Field(
        default="default",
        title="自动情感识别模型",
        description="用于识别文本情感的模型组，请从模型配置中选择聊天模型组。",
        json_schema_extra=ExtraField(
            ref_model_groups=True,
            required=True,
            model_type="chat",
        ).model_dump(),
    )
    AUTO_EMOTION_PROMPT: str = Field(
        default="你是情感分类助手。请从候选情感中仅选择一个最匹配当前文本的情感名并原样返回，不要解释。\n候选情感：{emotion_list}\n文本：{text}",
        title="自动情感识别提示词",
        description="用于自动情感识别的系统提示词模板，支持 {emotion_list} 和 {text} 占位符。",
    )
    AUTO_EMOTION_TIMEOUT: int = Field(
        default=30,
        title="自动情感识别超时秒数",
        description="自动情感识别请求超时时间。",
    )
    AUTO_EMOTION_FALLBACK_TO_DEFAULT: bool = Field(
        default=True,
        title="自动情感识别失败回退默认情感",
        description="自动情感识别失败时是否回退到默认情感/默认参考音频。",
    )
    CONFIGURED_EMOTIONS: Dict[str, Dict[str, Dict[str, str]]] = Field(
        default={},
        title="配置的情感数据",
        description="通过配置面板注册的情感数据，格式为 {角色名: {情感名: {ref_audio_path: 路径, ref_audio_text: 文本, language: 语言}}}",
    )
    REFERENCE_AUDIO_PATH: str = Field(
        default="reference_audio/feibi_happy.ogg",
        title="参考音频路径",
        description="参考音频相对路径（相对于服务端模型仓库）。",
    )
    REFERENCE_AUDIO_TEXT: str = Field(
        default="晚上好，漂泊者",
        title="参考音频文本",
        description="参考音频对应文本。",
    )
    LANGUAGE: str = Field(
        default="zh",
        title="语言",
        description="语言代码，可选 jp/zh/en。",
    )
    ENABLE_TRANSLATION: bool = Field(
        default=False,
        title="启用翻译",
        description="启用后在语音合成前先调用翻译API处理文本。",
    )
    TRANSLATION_MODEL: str = Field(
        default="default",
        title="翻译模型",
        description="用于翻译文本的模型组，请从模型配置中选择聊天模型组。",
        json_schema_extra=ExtraField(
            ref_model_groups=True,
            required=True,
            model_type="chat",
        ).model_dump(),
    )
    TRANSLATION_PROMPT: str = Field(
        default="你是专业翻译助手。请将用户输入翻译为自然流畅的日语，只返回翻译结果，不要解释。",
        title="翻译提示词",
        description="翻译请求使用的系统提示词。",
    )
    TRANSLATION_TIMEOUT: int = Field(
        default=60,
        title="翻译超时秒数",
        description="翻译接口请求超时时间。",
    )
    TRANSLATION_FALLBACK_TO_ORIGINAL: bool = Field(
        default=True,
        title="翻译失败使用原文",
        description="翻译失败时是否自动回退为原始文本。",
    )
    ENABLE_SENTENCE_SPLITTING: bool = Field(
        default=False,
        title="启用句子切分",
        description="开启后将长文本按句子切分并并发合成。",
    )
    SENTENCES_PER_CHUNK: int = Field(
        default=2,
        title="每段句子数",
        description="句子切分模式下每个分段包含的句子数量。",
    )
    SENTENCE_SPLIT_REGEX: str = Field(
        default=r"([。、，！？,.!?])",
        title="句子切分正则",
        description="用于识别句子边界的正则表达式。",
    )
    ENABLE_TTS_TEXT_CLEANING: bool = Field(
        default=False,
        title="启用文本清洗",
        description="启用后先按清洗正则去除文本中不需要合成的内容。",
    )
    TTS_TEXT_CLEAN_REGEX: str = Field(
        default=r"\([^()]*\)|（[^（）]*）|\[[^\[\]]*\]|【[^【】]*】|\{[^{}]*\}|｛[^｛｝]*｝|<[^<>]*>|《[^《》]*》",
        title="文本清洗正则",
        description="用于文本清洗的正则表达式。",
    )
    TTS_MAX_RETRIES: int = Field(
        default=2,
        title="失败重试次数",
        description="单个文本块在所有服务失败后的最大重试次数。",
    )
    TTS_MAX_CONCURRENCY: int = Field(
        default=2,
        title="最大并发块数",
        description="句子切分模式下的最大并发请求数量。",
    )
    TTS_TIMEOUT: int = Field(
        default=120,
        title="超时秒数",
        description="设置参考音频和语音合成接口超时秒数。",
    )


config = plugin.get_config(TTSConfig)


def get_model_group_info(model_name: str) -> ModelConfigGroup:
    try:
        return core_config.MODEL_GROUPS[model_name]
    except KeyError as e:
        raise ValueError(f"模型组 '{model_name}' 不存在，请确认配置正确") from e


def reload_emotion_manager():
    from .handlers import reload_emotion_manager as handlers_reload_emotion_manager
    handlers_reload_emotion_manager()
