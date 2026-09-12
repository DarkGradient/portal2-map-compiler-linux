"""
Конфигурация сборщика карт Portal 2 + мастер первичной настройки.
"""

from __future__ import annotations

import json
import re
import shlex
import stat
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import i18n

CFG_FILENAME = "compile_opts.json"

COMPILER_BIN_NAMES = {
    "vbsp": "vbsp++",
    "vvis": "vvis++",
    "vrad": "vrad++",
}

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
    compilers_dir: Path
    language: str = "ru"
    vbsp_args: list[str] = field(default_factory=lambda: list(DEFAULT_VBSP_ARGS))
    postcompiler_args: list[str] = field(default_factory=lambda: list(DEFAULT_POSTCOMPILER_ARGS))
    vvis_args: list[str] = field(default_factory=lambda: list(DEFAULT_VVIS_ARGS))
    vrad_args: list[str] = field(default_factory=lambda: list(DEFAULT_VRAD_ARGS))
    vbsp_enabled: bool = True
    postcompiler_enabled: bool = True
    vvis_enabled: bool = True
    vrad_enabled: bool = True

    def save(self, cfg_path: Path) -> None:
        data = asdict(self)
        for key in ("portal2_dir", "maps_dir", "postcompiler_bin", "compilers_dir"):
            data[key] = str(data[key])
        cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, cfg_path: Path) -> "BuildConfig":
        data = json.loads(cfg_path.read_text(encoding="utf-8"))

        def _parse_args(key: str, default: list[str]) -> list[str]:
            val = data.get(key, default)
            if isinstance(val, str):
                return shlex.split(val)
            return list(val)

        lang = data.get("language", "ru")
        i18n.load_locale(lang)

        return cls(
            portal2_dir=Path(data["portal2_dir"]),
            maps_dir=Path(data["maps_dir"]),
            postcompiler_bin=Path(data["postcompiler_bin"]),
            compilers_dir=Path(data["compilers_dir"]),
            language=lang,
            vbsp_args=_parse_args("vbsp_args", DEFAULT_VBSP_ARGS),
            postcompiler_args=_parse_args("postcompiler_args", DEFAULT_POSTCOMPILER_ARGS),
            vvis_args=_parse_args("vvis_args", DEFAULT_VVIS_ARGS),
            vrad_args=_parse_args("vrad_args", DEFAULT_VRAD_ARGS),
            vbsp_enabled=data.get("vbsp_enabled", True),
            postcompiler_enabled=data.get("postcompiler_enabled", True),
            vvis_enabled=data.get("vvis_enabled", True),
            vrad_enabled=data.get("vrad_enabled", True),
        )

    @property
    def game_dir(self) -> Path:
        return self.portal2_dir / "portal2"

    @property
    def vbsp_cmd(self) -> list[str]:
        return [
            str(self.compilers_dir / COMPILER_BIN_NAMES["vbsp"]),
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
            str(self.compilers_dir / COMPILER_BIN_NAMES["vvis"]),
            "-game", str(self.game_dir),
            *self.vvis_args,
        ]

    @property
    def vrad_cmd(self) -> list[str]:
        return [
            str(self.compilers_dir / COMPILER_BIN_NAMES["vrad"]),
            "-game", str(self.game_dir),
            *self.vrad_args,
        ]

    @property
    def dlc_maps_dir(self) -> Path:
        return self.portal2_dir / "portal2_dlc3" / "maps"


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


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def find_postcompiler(script_dir: Path) -> Path | None:
    pc_dir = script_dir / "postcompiler"
    if not pc_dir.is_dir():
        return None
    for candidate in pc_dir.iterdir():
        if candidate.is_file() and candidate.name == "postcompiler":
            _make_executable(candidate)
            return candidate
    return None


def find_compilers_dir(portal2_dir: Path) -> Path | None:
    candidate = portal2_dir / "MAP_COMPILER" / "tools" / "compilers"
    if not candidate.is_dir():
        return None
    required = COMPILER_BIN_NAMES.values()
    if not all((candidate / name).is_file() for name in required):
        return None
    for name in required:
        _make_executable(candidate / name)
    return candidate


HAMMER_LINK_NAMES: list[str] = [
    "hammerplusplus",
    "hammerplusplus.exe",
    "hlmvplusplus.dll",
    "hlmvplusplus.exe",
]


def find_hammer_tools_dir(portal2_dir: Path) -> Path | None:
    candidate = portal2_dir / "MAP_COMPILER" / "tools" / "hammer"
    if not candidate.is_dir():
        return None
    if not all((candidate / name).exists() for name in HAMMER_LINK_NAMES):
        return None
    return candidate


def link_hammer_tools(hammer_tools_dir: Path, bin_dir: Path) -> dict[str, str]:
    bin_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, str] = {}
    for name in HAMMER_LINK_NAMES:
        src = hammer_tools_dir / name
        dest = bin_dir / name
        if not src.exists():
            results[name] = "missing_src"
            continue
        if dest.is_symlink():
            results[name] = "already" if dest.resolve() == src.resolve() else "skipped_wrong_symlink"
            continue
        if dest.exists():
            results[name] = "skipped_exists"
            continue
        dest.symlink_to(src, target_is_directory=src.is_dir())
        results[name] = "linked"
    return results


def backup_file(path: Path) -> Path:
    backup_path = path.with_suffix(path.suffix + ".bak")
    n = 1
    while backup_path.exists():
        backup_path = path.with_suffix(f"{path.suffix}.bak{n}")
        n += 1
    backup_path.write_bytes(path.read_bytes())
    return backup_path


def run_setup_wizard(script_dir: Path) -> BuildConfig | None:
    import qt_ui

    picked = qt_ui.dir_select(i18n.t("wizard.select_portal2_dir"))
    if picked is None:
        print("[p2_build_tool] Setup canceled", file=sys.stderr)
        return None
    portal2_dir = picked

    fgd_src = script_dir / "portal2.fgd"
    fgd_dest = portal2_dir / "bin" / "portal2.fgd"
    gameinfo_path = portal2_dir / "portal2" / "gameinfo.txt"
    postcompiler_bin = find_postcompiler(script_dir)
    compilers_dir = find_compilers_dir(portal2_dir)
    hammer_tools_dir = find_hammer_tools_dir(portal2_dir)

    lines = [
        i18n.t("wizard.info_portal2", dir=portal2_dir),
        i18n.t("wizard.info_fgd", status=i18n.t("wizard.yes") if fgd_src.is_file() else i18n.t("wizard.no_file_missing")),
        i18n.t("wizard.info_gameinfo", path=gameinfo_path),
        i18n.t("wizard.info_postcompiler", status=postcompiler_bin if postcompiler_bin else i18n.t("wizard.not_found")),
        i18n.t("wizard.info_compilers", status=compilers_dir if compilers_dir else i18n.t("wizard.not_found_compilers")),
        i18n.t("wizard.info_hammer", status=i18n.t("wizard.yes") if hammer_tools_dir else i18n.t("wizard.no_file_missing")),
    ]
    if not qt_ui.confirm(i18n.t("wizard.confirm_title"), "\n".join(lines)):
        return None

    if postcompiler_bin is None:
        qt_ui.info(i18n.t("wizard.confirm_title"), i18n.t("wizard.error_no_postcompiler"))
        return None

    if compilers_dir is None:
        qt_ui.info(i18n.t("wizard.confirm_title"), i18n.t("wizard.error_no_compilers", path=portal2_dir / "MAP_COMPILER" / "tools" / "compilers"))
        return None

    if fgd_src.is_file():
        if fgd_dest.is_file():
            backup_file(fgd_dest)
        fgd_dest.write_bytes(fgd_src.read_bytes())

    if hammer_tools_dir is not None:
        link_results = link_hammer_tools(hammer_tools_dir, portal2_dir / "bin")
        for name, status in link_results.items():
            if status not in ("linked", "already"):
                print(f"[p2_build_tool] Hammer++ symlink '{name}': {status}", file=sys.stderr)
        if any(status == "skipped_exists" for status in link_results.values()):
            qt_ui.info(i18n.t("wizard.confirm_title"), i18n.t("wizard.warn_existing_symlink"))

    patch_result = patch_gameinfo(gameinfo_path)
    if patch_result == "missing":
        qt_ui.info(i18n.t("wizard.confirm_title"), i18n.t("wizard.warn_searchpaths_missing", path=gameinfo_path))

    cfg = BuildConfig(
        portal2_dir=portal2_dir,
        maps_dir=portal2_dir / "sdk_content" / "maps",
        postcompiler_bin=postcompiler_bin,
        compilers_dir=compilers_dir,
    )
    cfg.save(script_dir / CFG_FILENAME)
    qt_ui.info(i18n.t("wizard.done_title"), i18n.t("wizard.done_text"))
    return cfg


def load_or_setup(script_dir: Path) -> BuildConfig | None:
    cfg_path = script_dir / CFG_FILENAME
    if cfg_path.is_file():
        return BuildConfig.load(cfg_path)
    return run_setup_wizard(script_dir)
