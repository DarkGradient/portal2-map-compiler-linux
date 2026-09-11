"""
Конфигурация сборщика карт Portal 2 + мастер первичной настройки.

Параметры сборщиков (VBSP, Postcompiler, VVIS, VRAD) вынесены в compile_opts.json.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import qt_ui

CFG_FILENAME = "compile_opts.json"

# Дефолтные параметры компиляции по умолчанию
DEFAULT_VBSP_ARGS: list[str] = []
DEFAULT_POSTCOMPILER_ARGS: list[str] = ["--propcombine"]
DEFAULT_VVIS_ARGS: list[str] = []
DEFAULT_VRAD_ARGS: list[str] = [
    "-both",
    "-final",
    "-textureshadows",
    "-StaticPropLighting",
    "-StaticPropPolys",
]


@dataclass
class BuildConfig:
    portal2_dir: Path
    maps_dir: Path
    postcompiler_bin: Path
    vbsp_args: list[str] = field(default_factory=lambda: list(DEFAULT_VBSP_ARGS))
    postcompiler_args: list[str] = field(default_factory=lambda: list(DEFAULT_POSTCOMPILER_ARGS))
    vvis_args: list[str] = field(default_factory=lambda: list(DEFAULT_VVIS_ARGS))
    vrad_args: list[str] = field(default_factory=lambda: list(DEFAULT_VRAD_ARGS))

    # --- сериализация ---------------------------------------------------
    def save(self, cfg_path: Path) -> None:
        data = asdict(self)
        for key in ("portal2_dir", "maps_dir", "postcompiler_bin"):
            data[key] = str(data[key])
        cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, cfg_path: Path) -> "BuildConfig":
        data = json.loads(cfg_path.read_text(encoding="utf-8"))

        def _parse_args(key: str, default: list[str]) -> list[str]:
            val = data.get(key, default)
            # Поддержка как списка ["-both", "-final"], так и строки "-both -final"
            if isinstance(val, str):
                return shlex.split(val)
            return list(val)

        return cls(
            portal2_dir=Path(data["portal2_dir"]),
            maps_dir=Path(data["maps_dir"]),
            postcompiler_bin=Path(data["postcompiler_bin"]),
            vbsp_args=_parse_args("vbsp_args", DEFAULT_VBSP_ARGS),
            postcompiler_args=_parse_args("postcompiler_args", DEFAULT_POSTCOMPILER_ARGS),
            vvis_args=_parse_args("vvis_args", DEFAULT_VVIS_ARGS),
            vrad_args=_parse_args("vrad_args", DEFAULT_VRAD_ARGS),
        )

    # --- производные пути для команд компиляции --------------------------
    @property
    def game_dir(self) -> Path:
        return self.portal2_dir / "portal2"

    @property
    def vbsp_cmd(self) -> list[str]:
        return [
            str(self.portal2_dir / "bin" / "vbsp.exe"),
            "-game", str(self.game_dir),
            *self.vbsp_args,
        ]

    @property
    def postcompiler_cmd(self) -> list[str]:
        return [
            str(self.postcompiler_bin),
            "-game", str(self.game_dir),
            *self.postcompiler_args,
        ]

    @property
    def vvis_cmd(self) -> list[str]:
        return [
            str(self.portal2_dir / "bin" / "vvis.exe"),
            "-game", str(self.game_dir),
            *self.vvis_args,
        ]

    @property
    def vrad_cmd(self) -> list[str]:
        return [
            str(self.portal2_dir / "bin" / "vrad.exe"),
            "-game", str(self.game_dir),
            *self.vrad_args,
        ]

    @property
    def dlc_maps_dir(self) -> Path:
        return self.portal2_dir / "portal2_dlc3" / "maps"


# --- логика патчинга gameinfo.txt -----------------------------------------

def patch_gameinfo(gameinfo_path: Path) -> str:
    if not gameinfo_path.is_file():
        return "missing"

    text = gameinfo_path.read_text(encoding="utf-8")
    block_match = re.search(r"SearchPaths\s*\{(.*?)\n\s*\}", text, re.DOTALL)
    if not block_match:
        return "missing"

    if "MAP_COMPILER/hammer" in block_match.group(1):
        return "already"

    new_text, count = re.subn(
        r"(Game\s*\|gameinfo_path\|\.\s*\n)",
        r"\1\t\t\tGame\t\t\t\tMAP_COMPILER/hammer\n",
        text,
        count=1,
    )
    if count == 0:
        return "missing"

    backup_file(gameinfo_path)
    gameinfo_path.write_text(new_text, encoding="utf-8")
    return "patched"


def find_postcompiler(script_dir: Path) -> Path | None:
    pc_dir = script_dir / "postcompiler"
    if not pc_dir.is_dir():
        return None
    for candidate in pc_dir.iterdir():
        if candidate.is_file() and candidate.name == "postcompiler":
            candidate.chmod(candidate.stat().st_mode | 0o111)
            return candidate
    return None


# --- безусловный бэкап перед модификацией ----------------------------------

def backup_file(path: Path) -> Path:
    backup_path = path.with_suffix(path.suffix + ".bak")
    n = 1
    while backup_path.exists():
        backup_path = path.with_suffix(f"{path.suffix}.bak{n}")
        n += 1
    backup_path.write_bytes(path.read_bytes())
    return backup_path


def run_setup_wizard(script_dir: Path) -> BuildConfig | None:
    picked = qt_ui.dir_select("Выбери папку Portal 2")
    if picked is None:
        print("[p2_build_tool] Диалог выбора папки Portal 2 отменён или не вернул путь", file=sys.stderr)
        return None
    portal2_dir = picked

    fgd_src = script_dir / "portal2.fgd"
    fgd_dest = portal2_dir / "bin" / "portal2.fgd"
    gameinfo_path = portal2_dir / "portal2" / "gameinfo.txt"
    postcompiler_bin = find_postcompiler(script_dir)

    lines = [
        f"Portal 2: {portal2_dir}",
        f"FGD будет заменён: {'да' if fgd_src.is_file() else 'нет (файл не найден)'}",
        f"gameinfo.txt будет обновлён: {gameinfo_path}",
        f"postcompiler: {postcompiler_bin if postcompiler_bin else 'НЕ НАЙДЕН'}",
    ]
    if not qt_ui.confirm("Подтверждение настройки", "\n".join(lines)):
        print("[p2_build_tool] Подтверждение настройки отклонено пользователем", file=sys.stderr)
        return None

    if postcompiler_bin is None:
        qt_ui.info("Ошибка", "Не найден postcompiler в папке /postcompiler/. Настройка прервана.")
        print(f"[p2_build_tool] postcompiler не найден в {script_dir / 'postcompiler'}", file=sys.stderr)
        return None

    if fgd_src.is_file():
        if fgd_dest.is_file():
            backup_file(fgd_dest)
        fgd_dest.write_bytes(fgd_src.read_bytes())

    patch_result = patch_gameinfo(gameinfo_path)
    if patch_result == "missing":
        qt_ui.info(
            "Внимание",
            f"Не удалось найти блок SearchPaths в {gameinfo_path}.\n"
            "Пути Hammer придётся добавить вручную.",
        )

    cfg = BuildConfig(
        portal2_dir=portal2_dir,
        maps_dir=portal2_dir / "sdk_content" / "maps",
        postcompiler_bin=postcompiler_bin,
    )
    cfg.save(script_dir / CFG_FILENAME)
    qt_ui.info("Готово", "Конфигурация сохранена.")
    return cfg


def load_or_setup(script_dir: Path) -> BuildConfig | None:
    cfg_path = script_dir / CFG_FILENAME
    if cfg_path.is_file():
        return BuildConfig.load(cfg_path)
    return run_setup_wizard(script_dir)
