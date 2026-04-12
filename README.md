# Genie TTS 插件

基于 Genie TTS 的文本转语音插件。  
插件通过 Genie TTS 服务将文本合成为语音，并支持情感参考音频、翻译预处理、文本清洗、句子切分并发合成、多服务故障切换。

## 功能概览

- 将文本合成为语音，并自动发送到当前会话（群聊/私聊）
- 支持会话级情感切换与默认情感配置
- 支持通过配置面板预置多角色情感库
- 支持翻译链路（调用平台聊天模型组）
- 支持文本清洗（正则移除括号内容等）
- 支持句子切分 + 并发合成 + WAV 自动拼接
- 支持多 TTS 服务地址轮询与失败重试

## 工作流程

1. 读取角色、参考音频、语言与会话情感配置
2. 可选执行翻译（`ENABLE_TRANSLATION`）
3. 可选执行文本清洗（`ENABLE_TTS_TEXT_CLEANING`）
4. 可选进行句子切分与并发任务分块（`ENABLE_SENTENCE_SPLITTING`）
5. 调用 TTS 服务：
   - `POST /set_reference_audio` 设置参考音频
   - `POST /tts` 获取流式音频
6. 合并分块音频（WAV）并发送到会话

## 前置：部署语音服务

本插件**自身不进行语音合成**，它依赖一个后端的 **Genie TTS 服务**。您必须先拥有一个可访问的该服务，插件才能正常工作。

> **Genie TTS** 是一个强大的语音合成项目，您需要将其部署为一个Web服务。
> - **官方仓库**: [https://github.com/High-Logic/Genie](https://github.com/High-Logic/Genie)

### 方案一：使用 Hugging Face 一键部署

算力免费而且无需本地机器配置，但是合成速度比较慢。

1.  **复制Space**:
    -   服务仓库: [https://huggingface.co/spaces/XG2020/na_genie_tts/tree/main](https://huggingface.co/spaces/XG2020/na_genie_tts/tree/main)
    -   点击页面右上角的 **"Duplicate this Space"** 即可一键复制，拥有一个完全属于您自己的、免费的TTS服务。
    -   api接口url格式为 https://your-name-your-space.hf.space
       - tips: 你可以将你复制好的仓库地址发给ai询问，让ai帮你转换成.hf.space的空间地址

2.  **使用自定义模型**:
    -   默认服务会从我的模型仓库 [XG2020/genie_tts_models](https://huggingface.co/XG2020/genie_tts_models/tree/main) 下载模型。该模型仓库已包含多个预置角色，
       - Mika (聖園ミカ) — 蔚蓝档案 (Blue Archive) (日语)
       - ThirtySeven (37) — 重返未来：1999 (Reverse: 1999) (英语)
       - Feibi (菲比) — 鸣潮 (Wuthering Waves) (中文)
   您可以直接使用。现在默认注册了一个角色是feibi。您可以去app.py按照说明修改。
    -   若要使用您自己的模型，请将您训练和转换好的模型上传到您自己的 Hugging Face 模型仓库，然后在 Space 的 `app.py` 文件中修改 `REPO_ID` 和 `CHARACTERS` 字典。
    -   支持的模型版本： GPT-SoVITS V2, V2ProPlus 可去GPT-SoVITS官方的模型分享社区下载[点击访问](https://www.ai-hobbyist.com/forum.php?mod=forumdisplay&fid=138&filter=typeid&typeid=97&sortid=1)
    -   **【关键步骤】** 在您的空间中，**您必须创建一个名为 `reference_audio` 的文件夹**，并将所有用于注册情感的参考音频文件（如 `.wav`, `.ogg`）放入其中。
    -   **注意：** Genie 服务目前有加载3个模型的上限，请确保 `CHARACTERS` 字典中启用的角色不超过3个。
3.  **开启自动保活（可选）**：Hugging Face Space 超过 24 小时无人访问会休眠。插件新增了“自动保活空间”的开关，开启后会定时访问空间主页防止休眠， **注意：此功能需要在插件管理开启定时器插件**。
  配置项：
    -   **启用**：在插件配置中打开“是否自动定期访问 Hugging Face Space 以防止休眠”。
    -   **保活地址**：默认使用 TTS 服务器列表的第一个地址，若您想单独设置，请填写“保活请求的目标地址”。
    -   **间隔**：可通过“两次保活之间的间隔分钟数”调整访问频率，建议 15-30 分钟。

### 方案二：本地或 Windows 部署

- 如果您想在本地运行，请参照 Genie 官方仓库的文档进行部署。
- 也可以使用我提供的docker版本构建镜像部署 **[genie_tts_docker](https://github.com/XG2020/genie_tts_docker)**
   - 拉取镜像 docker pull xggm/genie-tts-docker:latest
   - 在宿主机创建目录${HOME}/srv/genie_tts/models/feibi与${HOME}/srv/genie_tts/reference_audio将模型和参考音频文件分别上传到这两个文件夹
   - 然后执行下面的命令
     ```
      sudo docker run -d --name genie-tts -p 7860:7860 -v "${HOME}/srv/genie_tts/models:/code/models:ro" -v "${HOME}/srv/genie_tts/reference_audio:/code/reference_audio" -e GENIE_PRELOAD_CHARACTERS=feibi xggm/genie-tts-docker:latest
      ```
- 作者还提供了 **Windows 一键整合包**，极大简化了部署流程，详情请访问其 GitHub。

**部署完成后，请记下您的服务 URL (例如 `https://your-name-your-space.hf.space`)，后续配置插件时需要用到。**

## 配置说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `API_URL` | `https://your-name-your-space.hf.space` | 单服务模式下的 Genie TTS 服务地址 |
| `TTS_SERVERS` | `[]` | 多服务地址列表；非空时优先使用并支持故障切换 |
| `ENABLE_SPACE_KEEPALIVE` | `False` | 是否启用自动保活任务（基于 timer 统一调度） |
| `SPACE_KEEPALIVE_URL` | `""` | 额外保活地址（可选），会与 TTS 服务地址一起保活 |
| `SPACE_KEEPALIVE_INTERVAL_MINUTES` | `25` | 两次保活任务之间的间隔分钟数（最小1） |
| `token` | `None` | Bearer Token；为空或 `None` 时不附带鉴权头 |
| `DEFAULT_MODEL` | `feibi` | 默认角色名（全局配置，可通过 `genie_tts_set` 修改） |
| `DEFAULT_EMOTION_NAME` | `""` | 默认情感名；命中时覆盖默认参考音频配置 |
| `ENABLE_AUTO_EMOTION_RECOGNITION` | `False` | 全局默认自动情感识别开关；仅作为会话初始值，运行中可被会话命令覆盖 |
| `AUTO_EMOTION_MODEL` | `default` | 自动情感识别使用的模型组（必须是 chat 类型） |
| `AUTO_EMOTION_PROMPT` | 见插件默认值 | 自动情感识别提示词模板，支持 `{emotion_list}` 和 `{text}` |
| `AUTO_EMOTION_TIMEOUT` | `30` | 自动情感识别超时（秒） |
| `AUTO_EMOTION_FALLBACK_TO_DEFAULT` | `True` | 自动情感识别失败时是否回退默认情感 |
| `CONFIGURED_EMOTIONS` | `{}` | 配置面板预置情感，格式：`{角色: {情感: {ref_audio_path, ref_audio_text, language}}}` |
| `REFERENCE_AUDIO_PATH` | `reference_audio/feibi_happy.ogg` | 默认参考音频相对路径（服务端模型仓库内） |
| `REFERENCE_AUDIO_TEXT` | `晚上好，漂泊者` | 默认参考音频文本 |
| `LANGUAGE` | `zh` | 默认语言代码（`jp/zh/en`） |
| `ENABLE_TRANSLATION` | `False` | 合成前是否先进行翻译 |
| `TRANSLATION_MODEL` | `default` | 翻译使用的模型组（必须是 chat 类型） |
| `TRANSLATION_PROMPT` | 见插件默认值 | 翻译系统提示词 |
| `TRANSLATION_TIMEOUT` | `60` | 翻译超时（秒） |
| `TRANSLATION_FALLBACK_TO_ORIGINAL` | `True` | 翻译失败时是否回退原文 |
| `ENABLE_SENTENCE_SPLITTING` | `False` | 是否启用句子切分并发 |
| `SENTENCES_PER_CHUNK` | `2` | 每个分块包含句子数 |
| `SENTENCE_SPLIT_REGEX` | `([。、，！？,.!?])` | 句子切分正则 |
| `ENABLE_TTS_TEXT_CLEANING` | `False` | 是否启用文本清洗 |
| `TTS_TEXT_CLEAN_REGEX` | 见插件默认值 | 文本清洗正则 |
| `TTS_MAX_RETRIES` | `2` | 每个分块最大重试次数 |
| `TTS_MAX_CONCURRENCY` | `2` | 分块并发上限 |
| `TTS_TIMEOUT` | `120` | TTS 请求超时（秒） |

## 命令

### 基础命令

- `/genie_tts_set [角色名]`：设置默认角色
- `/genie_tts_help`：查看帮助

### 自动情感识别命令

- `/genie_tts_auto_emotion_on [角色名]`：为当前会话开启自动情感识别，可选指定角色
- `/genie_tts_auto_emotion_off`：为当前会话关闭自动情感识别
- `/genie_tts_auto_emotion_status`：查看当前会话自动情感识别状态

### 情感命令

- `/genie_tts_emotion_add 情感名|参考音频路径|参考文本|[语言]`
- `/genie_tts_emotion_add 角色名|情感名|参考音频路径|参考文本|[语言]`
- `/genie_tts_emotion_del 情感名`
- `/genie_tts_emotion_del 角色名|情感名`
- `/genie_tts_emotion_list [角色名]`
- `/genie_tts_emotion_set 情感名`
- `/genie_tts_emotion_set 角色名|情感名`
- `/genie_tts_emotion_clear`

说明：
- 省略角色名时使用 `DEFAULT_MODEL`
- `参考音频路径` 必须是相对路径，且不能包含 `..`
- `emotion_set` 仅在当前会话生效
- `auto_emotion_on/off` 仅在当前会话生效，不会修改全局配置 `ENABLE_AUTO_EMOTION_RECOGNITION`
- 自动情感识别会在当前角色已注册情感中选择最匹配项
- `auto_emotion_status` 会优先显示当前会话状态；若当前会话未覆盖，则继承全局配置开关

## 情感配置示例

`CONFIGURED_EMOTIONS` 示例：

```json
{
  "lita": {
    "happy": {
      "ref_audio_path": "reference_audio/lita_happy.ogg",
      "ref_audio_text": "晚安，舰长大人",
      "language": "zh"
    },
    "sad": {
      "ref_audio_path": "reference_audio/lita_sad.ogg",
      "ref_audio_text": "舰长受伤了吗？",
      "language": "zh"
    }
  }
}
```

## 注意事项

- 若配置了 `TTS_SERVERS`，将忽略 `API_URL` 的单服务优先级
- 启用自动保活后，会通过 timer 统一调度并定期访问 `TTS_SERVERS/API_URL` 及 `SPACE_KEEPALIVE_URL`
- 翻译模型必须是 chat 模型组，否则会报错
- 文本清洗后若为空，合成会直接失败
- 句子切分并发仅影响客户端分块；服务端仍可通过 `split_sentence` 控制内部切分
