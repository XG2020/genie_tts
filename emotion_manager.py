import json
from pathlib import Path
from typing import Dict, Optional


class EmotionManager:
    def __init__(self, file_path: Path, config_emotions: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None):
        self.file_path = file_path
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_emotions = config_emotions or {}
        self.emotions_data: Dict[str, Dict[str, Dict[str, str]]] = self._load_emotions_from_file()
        # Merge configured emotions with file-based emotions (config takes precedence)
        self._merge_configured_emotions()

    def _load_emotions_from_file(self) -> Dict[str, Dict[str, Dict[str, str]]]:
        if not self.file_path.exists():
            self._save_emotions_to_file({})
            return {}
        try:
            raw = self.file_path.read_text(encoding="utf-8")
            if not raw.strip():
                return {}
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
            return {}
        except Exception:
            return {}

    def refresh_config_emotions(
        self,
        config_emotions: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None,
    ) -> None:
        """重新加载文件情绪并合并最新配置；配置面板数据优先。"""
        self.config_emotions = config_emotions or {}
        self.emotions_data = self._load_emotions_from_file()
        self._merge_configured_emotions()

    def _merge_configured_emotions(self):
        """合并文件和配置面板中的情绪；同名项以配置面板为准。"""
        for character_name, emotions in self.config_emotions.items():
            if character_name not in self.emotions_data:
                self.emotions_data[character_name] = {}
            # Config emotions override file-based emotions
            for emotion_name, emotion_data in emotions.items():
                self.emotions_data[character_name][emotion_name] = emotion_data

    def _save_emotions_to_file(self, data: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None) -> bool:
        target = self.emotions_data if data is None else data
        try:
            self.file_path.write_text(
                json.dumps(target, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return True
        except Exception:
            return False

    def get_character_name(self, character_name: str) -> Optional[str]:
        """Return the configured character key, tolerating case/whitespace differences."""
        requested = (character_name or "").strip()
        if requested in self.emotions_data:
            return requested
        requested_lower = requested.casefold()
        for configured_name in self.emotions_data:
            if configured_name.strip().casefold() == requested_lower:
                return configured_name
        return None

    def get_emotion_data(self, character_name: str, emotion_name: str) -> Optional[Dict[str, str]]:
        resolved_character = self.get_character_name(character_name)
        if resolved_character is None:
            return None
        emotions = self.emotions_data.get(resolved_character, {})
        requested_emotion = (emotion_name or "").strip()
        if requested_emotion in emotions:
            return emotions[requested_emotion]
        requested_lower = requested_emotion.casefold()
        for configured_name, emotion_data in emotions.items():
            if configured_name.strip().casefold() == requested_lower:
                return emotion_data
        return None

    def list_emotions(self, character_name: str) -> list[str]:
        resolved_character = self.get_character_name(character_name)
        if resolved_character is None:
            return []
        return list(self.emotions_data.get(resolved_character, {}).keys())

    def register_emotion(
        self,
        character_name: str,
        emotion_name: str,
        ref_audio_path: str,
        ref_audio_text: str,
        language: Optional[str] = None,
    ) -> bool:
        char = character_name.strip()
        emo = emotion_name.strip()
        if not char or not emo:
            return False
        if char not in self.emotions_data:
            self.emotions_data[char] = {}
        payload: Dict[str, str] = {
            "ref_audio_path": ref_audio_path.strip(),
            "ref_audio_text": ref_audio_text.strip(),
        }
        if language and language.strip():
            payload["language"] = language.strip()
        self.emotions_data[char][emo] = payload
        return self._save_emotions_to_file()

    def delete_emotion(self, character_name: str, emotion_name: str) -> bool:
        if character_name not in self.emotions_data:
            return False
        if emotion_name not in self.emotions_data[character_name]:
            return False
        del self.emotions_data[character_name][emotion_name]
        if not self.emotions_data[character_name]:
            del self.emotions_data[character_name]
        return self._save_emotions_to_file()
