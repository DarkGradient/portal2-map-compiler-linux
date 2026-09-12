"""
Модуль интернационализации (i18n) на основе JSON-словарей.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_LOCALES_DIR = Path(__file__).resolve().parent / "locales"
_current_lang = "ru"
_translations: dict[str, str] = {}
_fallback_translations: dict[str, str] = {}


def load_locale(lang: str = "ru") -> None:
    """Загружает файл локали из locales/<lang>.json."""
    global _current_lang, _translations, _fallback_translations
    _current_lang = lang

    ru_file = _LOCALES_DIR / "ru.json"
    if ru_file.is_file():
        try:
            _fallback_translations = json.loads(ru_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[i18n] Ошибка загрузки fallback-локали ru.json: {e}", file=sys.stderr)

    target_file = _LOCALES_DIR / f"{lang}.json"
    if target_file.is_file():
        try:
            _translations = json.loads(target_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[i18n] Ошибка загрузки локали {lang}.json: {e}", file=sys.stderr)
            _translations = _fallback_translations
    else:
        _translations = _fallback_translations


def t(key: str, **kwargs) -> str:
    """
    Возвращает перевод по ключу с подстановкой параметров.
    Пример: t("dashboard.build_finished_success", time="1м 20с")
    """
    text = _translations.get(key)
    if text is None:
        text = _fallback_translations.get(key, key)

    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, ValueError):
            return text
    return text


# Автоматическая первичная загрузка при старте модуля
load_locale("ru")