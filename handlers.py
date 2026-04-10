import asyncio
import base64
import io
import os
import re
import tempfile
import time
import wave
from pathlib import Path

import httpx
from nonebot import get_bot, on_command
from nonebot.adapters import Bot, Message
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from nekro_agent.adapters.onebot_v11.matchers.command import command_guard
from nekro_agent.api import core
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.core import logger
from nekro_agent.services.agent.openai import gen_openai_chat_response
from nekro_agent.services.plugin.base import SandboxMethodType
from nekro_agent.services.plugin.manager import save_plugin_config
from nekro_agent.services.timer.timer_service import timer_service

from .emotion_manager import EmotionManager
from .plugin import config, get_model_group_info, plugin

_server_locks: dict[str, asyncio.Lock] = {}
_session_emotions: dict[str, dict[str, str]] = {}
_session_auto_emotion_enabled: dict[str, bool] = {}
_session_auto_emotion_character: dict[str, str] = {}
_KEEPALIVE_CHAT_KEY = "system_genie_tts_keepalive"


def _create_emotion_manager():
    """Create emotion manager with configured emotions from plugin config"""
    from .plugin import config
    return EmotionManager(
        Path(__file__).with_name("emotions.json"),
        config_emotions=config.CONFIGURED_EMOTIONS
    )


_emotion_manager = _create_emotion_manager()


def reload_emotion_manager():
    global _emotion_manager
    _emotion_manager = _create_emotion_manager()


def _get_server_lock(server_url: str) -> asyncio.Lock:
    lock = _server_locks.get(server_url)
    if lock is None:
        lock = asyncio.Lock()
        _server_locks[server_url] = lock
    return lock


def _clean_text_for_tts(text: str) -> str:
    cleaned = text
    pattern = (config.TTS_TEXT_CLEAN_REGEX or "").strip()
    if pattern:
        for _ in range(10):
            updated = re.sub(pattern, "", cleaned)
            if updated == cleaned:
                break
            cleaned = updated
    return re.sub(r"\s+", " ", cleaned).strip()


def _split_text_into_chunks(text: str) -> list[str]:
    sentence_split_regex = (config.SENTENCE_SPLIT_REGEX or r"([。、，！？,.!?])").strip()
    parts = re.split(sentence_split_regex, text)
    if not parts:
        return [text]
    full_sentences: list[str] = []
    for i in range(0, len(parts) - 1, 2):
        sentence = parts[i]
        delimiter = parts[i + 1] if i + 1 < len(parts) else ""
        if sentence:
            full_sentences.append(sentence + delimiter)
    if len(parts) % 2 == 1 and parts[-1]:
        full_sentences.append(parts[-1])
    size = max(int(config.SENTENCES_PER_CHUNK), 1)
    return ["".join(full_sentences[i : i + size]) for i in range(0, len(full_sentences), size)]


def _resolve_servers() -> list[str]:
    servers = [s.strip().rstrip("/") for s in (config.TTS_SERVERS or []) if isinstance(s, str) and s.strip()]
    if servers:
        return servers
    fallback = (config.API_URL or "").strip().rstrip("/")
    return [fallback] if fallback else []


def _get_proxy() -> str | None:
    if not bool(config.ENABLE_PROXY_ACCESS):
        return None
    proxy = core.config.DEFAULT_PROXY
    if not proxy:
        return None
    if isinstance(proxy, str) and proxy.startswith(("http://", "https://")):
        return proxy
    return f"http://{proxy}"


def _get_keepalive_urls() -> list[str]:
    urls = set(_resolve_servers())
    custom_url = (config.SPACE_KEEPALIVE_URL or "").strip().rstrip("/")
    if custom_url:
        urls.add(custom_url)
    return list(urls)


async def _run_keepalive_once():
    target_urls = _get_keepalive_urls()
    if not target_urls:
        logger.warning("未找到可用于保活的地址，已跳过本次保活任务。")
        return
    async with httpx.AsyncClient(timeout=30, proxy=_get_proxy()) as client:
        async def ping(url: str):
            try:
                response = await client.get(url)
                logger.info(f"保活请求已发送到 {url}，状态码: {response.status_code}")
            except Exception as e:
                logger.warning(f"向 {url} 发送保活请求失败: {e}")
        await asyncio.gather(*[ping(url) for url in target_urls])


async def _schedule_next_keepalive():
    if not bool(config.ENABLE_SPACE_KEEPALIVE):
        return
    interval_minutes = max(int(config.SPACE_KEEPALIVE_INTERVAL_MINUTES), 1)
    trigger_time = int(time.time()) + interval_minutes * 60
    await timer_service.set_timer(
        chat_key=_KEEPALIVE_CHAT_KEY,
        trigger_time=trigger_time,
        event_desc="Genie TTS 自动保活任务",
        temporary=True,
        silent=True,
        callback=_keepalive_callback,
    )


async def _keepalive_callback():
    if not bool(config.ENABLE_SPACE_KEEPALIVE):
        return
    await _run_keepalive_once()
    await _schedule_next_keepalive()


def _build_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = (config.token or "").strip()
    if token and token.lower() != "none":
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _translate_text(text: str) -> str:
    if not bool(config.ENABLE_TRANSLATION):
        return text
    model_name = (config.TRANSLATION_MODEL or "").strip()
    prompt = (config.TRANSLATION_PROMPT or "").strip() or "你是翻译助手。"
    if not model_name:
        if bool(config.TRANSLATION_FALLBACK_TO_ORIGINAL):
            logger.warning("翻译已启用但未选择翻译模型，已回退为原文。")
            return text
        raise RuntimeError("翻译已启用，但未选择翻译模型。")
    try:
        model_group = get_model_group_info(model_name)
        if model_group.MODEL_TYPE != "chat":
            raise ValueError("翻译模型必须是聊天模型组。")
        response = await asyncio.wait_for(
            gen_openai_chat_response(
                model=model_group.CHAT_MODEL,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text},
                ],
                base_url=model_group.BASE_URL,
                api_key=model_group.API_KEY,
            ),
            timeout=max(int(config.TRANSLATION_TIMEOUT), 10),
        )
        translated_text = (response.response_content or "").strip()
        if not translated_text:
            raise RuntimeError("翻译结果为空。")
        return translated_text
    except Exception as e:
        if bool(config.TRANSLATION_FALLBACK_TO_ORIGINAL):
            logger.warning(f"翻译失败，已回退为原文: {e}")
            return text
        raise RuntimeError(f"翻译失败: {e}")


def _normalize_detected_emotion(raw_text: str, valid_emotions: list[str]) -> str | None:
    text = (raw_text or "").strip()
    if not text:
        return None
    candidates = [text, text.splitlines()[0].strip()]
    for candidate in candidates:
        cleaned = candidate.strip().strip("`").strip('"').strip("'").strip("[]")
        if cleaned in valid_emotions:
            return cleaned
    lower_map = {name.lower(): name for name in valid_emotions}
    for candidate in candidates:
        lowered = candidate.strip().strip("`").strip('"').strip("'").strip("[]").lower()
        if lowered in lower_map:
            return lower_map[lowered]
    for name in valid_emotions:
        if name in text:
            return name
    return None


async def _detect_emotion_name(text: str, character_name: str) -> str | None:
    model_name = (config.AUTO_EMOTION_MODEL or "").strip()
    if not model_name:
        if bool(config.AUTO_EMOTION_FALLBACK_TO_DEFAULT):
            logger.warning("自动情感识别已启用但未选择模型，已回退默认情感。")
            return None
        raise RuntimeError("自动情感识别已启用，但未选择模型。")
    emotions = _emotion_manager.list_emotions(character_name)
    if not emotions:
        return None
    prompt_template = (
        (config.AUTO_EMOTION_PROMPT or "").strip()
        or "请从候选情感中选择一个并原样返回：{emotion_list}\n文本：{text}"
    )
    prompt = prompt_template.format(emotion_list=", ".join(emotions), text=text)
    try:
        model_group = get_model_group_info(model_name)
        if model_group.MODEL_TYPE != "chat":
            raise ValueError("自动情感识别模型必须是聊天模型组。")
        response = await asyncio.wait_for(
            gen_openai_chat_response(
                model=model_group.CHAT_MODEL,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text},
                ],
                base_url=model_group.BASE_URL,
                api_key=model_group.API_KEY,
            ),
            timeout=max(int(config.AUTO_EMOTION_TIMEOUT), 10),
        )
        detected_text = (response.response_content or "").strip()
        emotion_name = _normalize_detected_emotion(detected_text, emotions)
        if emotion_name:
            return emotion_name
        if bool(config.AUTO_EMOTION_FALLBACK_TO_DEFAULT):
            logger.warning(f"自动情感识别结果无效，已回退默认情感: {detected_text}")
            return None
        raise RuntimeError(f"自动情感识别结果无效: {detected_text}")
    except Exception as e:
        if bool(config.AUTO_EMOTION_FALLBACK_TO_DEFAULT):
            logger.warning(f"自动情感识别失败，已回退默认情感: {e}")
            return None
        raise RuntimeError(f"自动情感识别失败: {e}")


async def _request_tts_from_server(
    client: httpx.AsyncClient,
    server_url: str,
    character_name: str,
    ref_audio_path: str,
    ref_audio_text: str,
    language: str,
    text: str,
    headers: dict[str, str],
    use_internal_split: bool,
) -> bytes:
    ref_payload = {
        "character_name": character_name,
        "audio_path": ref_audio_path,
        "audio_text": ref_audio_text,
        "language": language,
    }
    tts_payload = {
        "character_name": character_name,
        "text": text,
        "split_sentence": use_internal_split,
    }
    server_lock = _get_server_lock(server_url)
    async with server_lock:
        set_ref_response = await client.post(
            f"{server_url}/set_reference_audio",
            headers=headers,
            json=ref_payload,
        )
        set_ref_response.raise_for_status()
        audio_bytes = bytearray()
        content_type = ""
        async with client.stream(
            "POST",
            f"{server_url}/tts",
            headers=headers,
            json=tts_payload,
        ) as tts_response:
            tts_response.raise_for_status()
            content_type = (tts_response.headers.get("content-type") or "").lower()
            async for chunk in tts_response.aiter_bytes():
                if chunk:
                    audio_bytes.extend(chunk)
    if not audio_bytes:
        raise RuntimeError("语音合成失败：服务未返回音频数据。")
    data = bytes(audio_bytes)
    head = data[:16]
    if head.startswith(b"RIFF"):
        return data
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return data
    if head.startswith(b"OggS") or head.startswith(b"fLaC"):
        return data
    if "application/json" in content_type or head.lstrip().startswith((b"{", b"[")):
        snippet = data[:200].decode("utf-8", errors="ignore")
        raise RuntimeError(f"TTS服务返回JSON而非音频，服务: {server_url}，响应片段: {snippet}")
    if "text/html" in content_type or head.lstrip().startswith((b"<",)):
        snippet = data[:200].decode("utf-8", errors="ignore")
        raise RuntimeError(f"TTS服务返回HTML而非音频，服务: {server_url}，响应片段: {snippet}")
    if content_type.startswith("audio/") or content_type in {"application/octet-stream", ""}:
        logger.warning(
            f"TTS返回非标准音频头，按PCM16LE封装为WAV，服务: {server_url}，content-type: {content_type}，头部: {head.hex(' ')}"
        )
        return _pcm16le_to_wav_bytes(data)
    logger.warning(
        f"TTS返回未知音频格式，服务: {server_url}，content-type: {content_type}，头部: {head.hex(' ')}"
    )
    return data


def _merge_wav_bytes(chunks: list[bytes]) -> bytes:
    if not chunks:
        raise RuntimeError("没有可用于融合的音频数据。")
    if len(chunks) == 1:
        return chunks[0]
    params = None
    frames = []
    for chunk in chunks:
        with wave.open(io.BytesIO(chunk), "rb") as wf:
            if params is None:
                params = wf.getparams()
            frames.append(wf.readframes(wf.getnframes()))
    output = io.BytesIO()
    with wave.open(output, "wb") as wf_out:
        wf_out.setparams(params)
        for frame in frames:
            wf_out.writeframes(frame)
    return output.getvalue()


def _normalize_wav_bytes_for_send(audio_bytes: bytes) -> bytes:
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            params = wf.getparams()
            frames = wf.readframes(wf.getnframes())
        output = io.BytesIO()
        with wave.open(output, "wb") as wf_out:
            wf_out.setparams(params)
            wf_out.writeframes(frames)
        return output.getvalue()
    except wave.Error:
        return audio_bytes


def _pcm16le_to_wav_bytes(audio_bytes: bytes, sample_rate: int = 32000, channels: int = 1) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wf_out:
        wf_out.setnchannels(channels)
        wf_out.setsampwidth(2)
        wf_out.setframerate(sample_rate)
        wf_out.writeframes(audio_bytes)
    return output.getvalue()


async def _resolve_emotion_reference(chat_key: str, text: str) -> tuple[str, str, str, str]:
    character_name = (config.DEFAULT_MODEL or "").strip()
    ref_audio_path = (config.REFERENCE_AUDIO_PATH or "").strip()
    ref_audio_text = (config.REFERENCE_AUDIO_TEXT or "").strip()
    language = (config.LANGUAGE or "jp").strip()
    if not character_name or not ref_audio_path or not ref_audio_text:
        raise ValueError("请先配置角色、参考音频路径和参考音频文本。")

    selected = _session_emotions.get(chat_key)
    if selected:
        emo_data = _emotion_manager.get_emotion_data(
            selected["character"],
            selected["emotion"],
        )
        if emo_data:
            return (
                selected["character"],
                emo_data["ref_audio_path"],
                emo_data["ref_audio_text"],
                emo_data.get("language", language),
            )

    auto_emotion_enabled = _session_auto_emotion_enabled.get(
        chat_key,
        bool(config.ENABLE_AUTO_EMOTION_RECOGNITION),
    )
    auto_emotion_character = (
        _session_auto_emotion_character.get(chat_key, "").strip() or character_name
    )
    if auto_emotion_enabled and auto_emotion_character:
        detected_emotion = await _detect_emotion_name(text=text, character_name=auto_emotion_character)
        if detected_emotion:
            emo_data = _emotion_manager.get_emotion_data(auto_emotion_character, detected_emotion)
            if emo_data:
                return (
                    auto_emotion_character,
                    emo_data["ref_audio_path"],
                    emo_data["ref_audio_text"],
                    emo_data.get("language", language),
                )

    default_emotion = (config.DEFAULT_EMOTION_NAME or "").strip()
    if default_emotion:
        emo_data = _emotion_manager.get_emotion_data(character_name, default_emotion)
        if emo_data:
            return (
                character_name,
                emo_data["ref_audio_path"],
                emo_data["ref_audio_text"],
                emo_data.get("language", language),
            )
    return character_name, ref_audio_path, ref_audio_text, language


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="生成Genie语音",
    description="将文本转换为语音并返回音频字节数据",
)
async def genie_tts(
    _ctx: AgentCtx,
    content: str,
) -> bytes:
    """将输入文本合成为语音并发送到当前会话。

    AI 调用示例：
    - 用户说：请把“こんにちは”读出来
      AI 工具调用：生成Genie语音(content="こんにちは")
    - 用户说：用当前角色把这句话读出来：今天天气真好
      AI 工具调用：生成Genie语音(content="今天天气真好")
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("文本内容不能为空。")

    servers = _resolve_servers()
    if not servers:
        raise ValueError("请先配置可用的 Genie TTS 服务地址。")

    headers = _build_headers()
    character_name, ref_audio_path, ref_audio_text, language = await _resolve_emotion_reference(
        _ctx.from_chat_key,
        text,
    )

    timeout = max(int(config.TTS_TIMEOUT), 10)
    try:
        async with httpx.AsyncClient(timeout=timeout, proxy=_get_proxy()) as client:
            text = await _translate_text(text)
            if bool(config.ENABLE_TTS_TEXT_CLEANING):
                text = _clean_text_for_tts(text)
                if not text:
                    raise ValueError("文本清洗后为空。")
            if bool(config.ENABLE_SENTENCE_SPLITTING):
                chunks = _split_text_into_chunks(text)
            else:
                chunks = [text]
            if not chunks:
                raise RuntimeError("未生成可合成文本块。")

            results: list[bytes | None] = [None] * len(chunks)
            semaphore = asyncio.Semaphore(
                max(1, min(int(config.TTS_MAX_CONCURRENCY), len(chunks), len(servers)))
            )

            async def process_chunk(chunk_index: int, chunk_text: str):
                max_retries = max(int(config.TTS_MAX_RETRIES), 0)
                last_error: Exception | None = None
                last_server_url = ""
                for retry in range(max_retries + 1):
                    for offset in range(len(servers)):
                        server_url = servers[(chunk_index + retry + offset) % len(servers)]
                        try:
                            async with semaphore:
                                audio_data = await _request_tts_from_server(
                                    client=client,
                                    server_url=server_url,
                                    character_name=character_name,
                                    ref_audio_path=ref_audio_path,
                                    ref_audio_text=ref_audio_text,
                                    language=language,
                                    text=chunk_text,
                                    headers=headers,
                                    use_internal_split=not bool(config.ENABLE_SENTENCE_SPLITTING),
                                )
                            results[chunk_index] = audio_data
                            return
                        except Exception as e:
                            last_error = e
                            last_server_url = server_url
                            logger.warning(
                                f"Genie TTS 请求失败，文本块={chunk_index + 1}，服务={server_url}，重试={retry + 1}/{max_retries + 1}，错误={e}"
                            )
                            continue
                if last_error:
                    raise RuntimeError(
                        f"第 {chunk_index + 1} 个文本块合成失败，最后失败服务: {last_server_url}，错误: {last_error}"
                    ) from last_error
                raise RuntimeError(f"第 {chunk_index + 1} 个文本块合成失败。")

            await asyncio.gather(*[process_chunk(i, chunk) for i, chunk in enumerate(chunks)])
            audio_parts = [item for item in results if item is not None]
            if bool(config.ENABLE_SENTENCE_SPLITTING):
                try:
                    merged_audio = _merge_wav_bytes(audio_parts)
                except wave.Error as e:
                    logger.warning(f"句子拆分融合失败，回退整段合成: {e}")
                    merged_audio = None
                    max_retries = max(int(config.TTS_MAX_RETRIES), 0)
                    last_error: Exception | None = None
                    for retry in range(max_retries + 1):
                        for offset in range(len(servers)):
                            server_url = servers[(retry + offset) % len(servers)]
                            try:
                                merged_audio = await _request_tts_from_server(
                                    client=client,
                                    server_url=server_url,
                                    character_name=character_name,
                                    ref_audio_path=ref_audio_path,
                                    ref_audio_text=ref_audio_text,
                                    language=language,
                                    text=text,
                                    headers=headers,
                                    use_internal_split=True,
                                )
                                break
                            except Exception as request_error:
                                last_error = request_error
                                continue
                        if merged_audio is not None:
                            break
                    if merged_audio is None:
                        raise RuntimeError(f"句子拆分融合失败，且整段回退合成失败: {last_error}") from last_error
            else:
                merged_audio = _merge_wav_bytes(audio_parts)
        await send_audio(_ctx.from_chat_key, merged_audio)
        return merged_audio
    except RuntimeError:
        raise
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"TTS服务返回错误状态码: {e.response.status_code}")
    except httpx.RequestError as e:
        raise RuntimeError(f"请求TTS服务失败: {e}")
    except Exception as e:
        raise RuntimeError(f"出现未知问题: {e}")


@plugin.mount_init_method()
async def init():
    await timer_service.set_timer(
        chat_key=_KEEPALIVE_CHAT_KEY,
        trigger_time=-1,
        event_desc="",
        temporary=True,
        silent=True,
    )
    if bool(config.ENABLE_SPACE_KEEPALIVE):
        await _run_keepalive_once()
        await _schedule_next_keepalive()
        logger.info("Genie TTS 自动保活任务已启动（timer统一调度）")


@plugin.mount_cleanup_method()
async def clean_up():
    await timer_service.set_timer(
        chat_key=_KEEPALIVE_CHAT_KEY,
        trigger_time=-1,
        event_desc="",
        temporary=True,
        silent=True,
    )
    logger.info("TTS Plugin Resources Cleaned Up")


async def send_audio(chat_key, file):
    pairs = chat_key.split("_")
    chat_type = pairs[0]
    chat_id = pairs[2]
    bot = get_bot()
    temp_file_path = None
    if isinstance(file, bytes):
        normalized_audio = _normalize_wav_bytes_for_send(file)
        encoded_audio = base64.b64encode(normalized_audio).decode("utf-8")
        audio = MessageSegment.record(file=f"base64://{encoded_audio}")
    else:
        audio = MessageSegment.record(file=file)
    try:
        if chat_type == "onebot_v11-group":
            await bot.send_group_msg(group_id=chat_id, message=audio)
        else:
            await bot.send_private_msg(user_id=chat_id, message=audio)
    except Exception as e:
        if isinstance(file, bytes):
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_file:
                    tmp_file.write(normalized_audio)
                    temp_file_path = tmp_file.name
                fallback_audio = MessageSegment.record(file=Path(temp_file_path).as_uri())
                if chat_type == "onebot_v11-group":
                    await bot.send_group_msg(group_id=chat_id, message=fallback_audio)
                else:
                    await bot.send_private_msg(user_id=chat_id, message=fallback_audio)
                return
            except Exception:
                pass
        raise RuntimeError(f"出现未知问题: {e}")
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except Exception:
                pass


@on_command("genie_tts_set").handle()
async def genie_tts_set(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    username, cmd_content, chat_key, chat_type = await command_guard(event, bot, arg, matcher)
    model_name = cmd_content.strip()
    if not model_name:
        await matcher.finish("用法: /genie_tts_set [角色名]")
        return
    try:
        await save_plugin_config("XGGM.genie_tts", {"DEFAULT_MODEL": model_name})
        await matcher.finish(f"默认角色已设置为: {model_name}")
    except Exception as e:
        await matcher.finish(f"设置默认角色失败: {e}")


@on_command("genie_tts_emotion_add").handle()
async def genie_tts_emotion_add(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    username, cmd_content, chat_key, chat_type = await command_guard(event, bot, arg, matcher)
    parts = [x.strip() for x in cmd_content.split("|") if x.strip()]
    if len(parts) not in {3, 4, 5}:
        await matcher.finish("用法: /genie_tts_emotion_add 情感名|参考音频路径|参考文本|[语言]\n或: /genie_tts_emotion_add 角色名|情感名|参考音频路径|参考文本|[语言]")
        return
    if len(parts) in {3, 4}:
        character_name = (config.DEFAULT_MODEL or "").strip()
        emotion_name = parts[0]
        ref_audio_path = parts[1]
        ref_audio_text = parts[2]
        language = parts[3] if len(parts) == 4 else None
    else:
        character_name = parts[0]
        emotion_name = parts[1]
        ref_audio_path = parts[2]
        ref_audio_text = parts[3]
        language = parts[4] if len(parts) == 5 else None
    if not character_name:
        await matcher.finish("未配置默认角色，请先设置角色或在命令中显式传入角色名。")
        return
    if ".." in ref_audio_path or os.path.isabs(ref_audio_path):
        await matcher.finish("参考音频路径必须是相对路径，且不能包含 '..'。")
        return
    ok = _emotion_manager.register_emotion(
        character_name=character_name,
        emotion_name=emotion_name,
        ref_audio_path=ref_audio_path,
        ref_audio_text=ref_audio_text,
        language=language,
    )
    if not ok:
        await matcher.finish("注册情感失败，请检查参数。")
        return
    await matcher.finish(f"已注册情感: {character_name} - {emotion_name}")


@on_command("genie_tts_emotion_del").handle()
async def genie_tts_emotion_del(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    username, cmd_content, chat_key, chat_type = await command_guard(event, bot, arg, matcher)
    parts = [x.strip() for x in cmd_content.split("|") if x.strip()]
    if len(parts) not in {1, 2}:
        await matcher.finish("用法: /genie_tts_emotion_del 情感名\n或: /genie_tts_emotion_del 角色名|情感名")
        return
    if len(parts) == 1:
        character_name = (config.DEFAULT_MODEL or "").strip()
        emotion_name = parts[0]
    else:
        character_name = parts[0]
        emotion_name = parts[1]
    if not character_name:
        await matcher.finish("未配置默认角色，请先设置角色或在命令中显式传入角色名。")
        return
    ok = _emotion_manager.delete_emotion(character_name, emotion_name)
    if not ok:
        await matcher.finish(f"未找到情感: {character_name} - {emotion_name}")
        return
    await matcher.finish(f"已删除情感: {character_name} - {emotion_name}")


@on_command("genie_tts_emotion_list").handle()
async def genie_tts_emotion_list(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    username, cmd_content, chat_key, chat_type = await command_guard(event, bot, arg, matcher)
    character_name = cmd_content.strip() or (config.DEFAULT_MODEL or "").strip()
    if not character_name:
        await matcher.finish("未配置默认角色，请先设置角色或在命令中传入角色名。")
        return
    emotions = _emotion_manager.list_emotions(character_name)
    if not emotions:
        await matcher.finish(f"角色 {character_name} 暂无已注册情感。")
        return
    await matcher.finish(f"角色 {character_name} 的情感：\n- " + "\n- ".join(emotions))


@on_command("genie_tts_emotion_set").handle()
async def genie_tts_emotion_set(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    username, cmd_content, chat_key, chat_type = await command_guard(event, bot, arg, matcher)
    parts = [x.strip() for x in cmd_content.split("|") if x.strip()]
    if len(parts) not in {1, 2}:
        await matcher.finish("用法: /genie_tts_emotion_set 情感名\n或: /genie_tts_emotion_set 角色名|情感名")
        return
    if len(parts) == 1:
        character_name = (config.DEFAULT_MODEL or "").strip()
        emotion_name = parts[0]
    else:
        character_name = parts[0]
        emotion_name = parts[1]
    if not character_name:
        await matcher.finish("未配置默认角色，请先设置角色或在命令中显式传入角色名。")
        return
    if not _emotion_manager.get_emotion_data(character_name, emotion_name):
        await matcher.finish(f"未找到情感: {character_name} - {emotion_name}")
        return
    _session_emotions[chat_key] = {"character": character_name, "emotion": emotion_name}
    await matcher.finish(f"当前会话已切换情感: {character_name} - {emotion_name}")


@on_command("genie_tts_emotion_clear").handle()
async def genie_tts_emotion_clear(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    username, cmd_content, chat_key, chat_type = await command_guard(event, bot, arg, matcher)
    _session_emotions.pop(chat_key, None)
    await matcher.finish("当前会话情感覆盖已清除，将使用默认角色/默认情感配置。")


@on_command("genie_tts_auto_emotion_on").handle()
async def genie_tts_auto_emotion_on(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    username, cmd_content, chat_key, chat_type = await command_guard(event, bot, arg, matcher)
    character_name = cmd_content.strip()
    _session_auto_emotion_enabled[chat_key] = True
    if character_name:
        _session_auto_emotion_character[chat_key] = character_name
        await matcher.finish(f"当前会话已开启自动情感识别，角色: {character_name}")
        return
    _session_auto_emotion_character.pop(chat_key, None)
    await matcher.finish("当前会话已开启自动情感识别，角色将使用默认角色。")


@on_command("genie_tts_auto_emotion_off").handle()
async def genie_tts_auto_emotion_off(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    username, cmd_content, chat_key, chat_type = await command_guard(event, bot, arg, matcher)
    _session_auto_emotion_enabled[chat_key] = False
    _session_auto_emotion_character.pop(chat_key, None)
    await matcher.finish("当前会话已关闭自动情感识别。")


@on_command("genie_tts_auto_emotion_status").handle()
async def genie_tts_auto_emotion_status(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    username, cmd_content, chat_key, chat_type = await command_guard(event, bot, arg, matcher)
    enabled = _session_auto_emotion_enabled.get(chat_key, bool(config.ENABLE_AUTO_EMOTION_RECOGNITION))
    character_name = _session_auto_emotion_character.get(chat_key, "").strip() or (config.DEFAULT_MODEL or "").strip()
    status_text = "开启" if enabled else "关闭"
    await matcher.finish(f"当前会话自动情感识别: {status_text}\n当前角色: {character_name}")


@on_command("genie_tts_help").handle()
async def genie_tts_help(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    await matcher.finish(message="使用 /genie_tts_set 来设置角色名\n具体用法:\n/genie_tts_set [角色名]\n/genie_tts_set feibi\n\n\
情感命令:\n/genie_tts_emotion_add 情感名|参考音频路径|参考文本|[语言]\n/genie_tts_emotion_del 情感名\n/genie_tts_emotion_list [角色名]\n/genie_tts_emotion_set 情感名\n/genie_tts_emotion_clear\n\n\
自动情感识别命令:\n/genie_tts_auto_emotion_on [角色名]\n/genie_tts_auto_emotion_off\n/genie_tts_auto_emotion_status\n\n\
本插件已支持翻译、自动情感识别、多服务故障切换、文本清洗、句子切分并发合成、自动保活。\n翻译模型请在插件配置中通过模型组选择器设置（TRANSLATION_MODEL），自动情感识别模型请设置（AUTO_EMOTION_MODEL）。")
