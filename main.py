#!/usr/bin/env python3
"""
Точка входа сборщика карт Portal 2.
"""

from __future__ import annotations

import sys
from pathlib import Path

import config
import qt_ui

import os
os.environ["QT_LOGGING_RULES"] = "kf.kio.*=false"

SCRIPT_DIR = Path(__file__).resolve().parent


def _die(reason: str) -> None:
    print(f"[p2_build_tool] Остановка: {reason}", file=sys.stderr)


def main() -> None:
    print(f"[p2_build_tool] Запуск из {SCRIPT_DIR}", file=sys.stderr)

    try:
        import PyQt6  # noqa: F401
    except ImportError:
        _die("PyQt6 не установлен — выполните: pip install PyQt6")
        return

    cfg = config.load_or_setup(SCRIPT_DIR)
    if cfg is None:
        _die("настройка отменена или не завершена")
        return

    vmf_path = qt_ui.pick_vmf(cfg.maps_dir)
    if vmf_path is None:
        _die("выбор VMF отменён")
        return

    cfg_path = SCRIPT_DIR / config.CFG_FILENAME
    qt_ui.run_build_window(cfg, vmf_path, cfg_path)


if __name__ == "__main__":
    main()
