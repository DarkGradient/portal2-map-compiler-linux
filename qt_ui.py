"""
UI-слой на PyQt6.

Все тексты вынесены в файлы локалей (locales/*.json) через модуль i18n.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import (
    QColor, QFont, QFontDatabase, QTextCharFormat, QTextCursor,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QFileDialog,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

import config
import i18n
import runner

STEP_NAMES = ["VBSP", "Postcompiler", "VVIS", "VRAD"]
STEP_CONFIG_MAP = {
    "VBSP": ("vbsp_args", "vbsp_enabled"),
    "Postcompiler": ("postcompiler_args", "postcompiler_enabled"),
    "VVIS": ("vvis_args", "vvis_enabled"),
    "VRAD": ("vrad_args", "vrad_enabled"),
}
DEFAULT_ARGS_MAP = {
    "VBSP": config.DEFAULT_VBSP_ARGS,
    "Postcompiler": config.DEFAULT_POSTCOMPILER_ARGS,
    "VVIS": config.DEFAULT_VVIS_ARGS,
    "VRAD": config.DEFAULT_VRAD_ARGS,
}

_app: QApplication | None = None

ANSI_COLORS = {
    30: QColor("#2e3436"),
    31: QColor("#f44747"),
    32: QColor("#6a9955"),
    33: QColor("#dcdcaa"),
    34: QColor("#569cd6"),
    35: QColor("#c586c0"),
    36: QColor("#4ec9b0"),
    37: QColor("#d4d4d4"),
    90: QColor("#808080"),
    91: QColor("#f44747"),
    92: QColor("#b5cea8"),
    93: QColor("#fce94f"),
    94: QColor("#9cdcfe"),
    95: QColor("#ce9178"),
    96: QColor("#4fc1ff"),
    97: QColor("#ffffff"),
}

_ANSI_SPLIT_RE = re.compile(r"(\x1b\[[0-9;]*[a-zA-Z]|\x1b)")
_ANSI_STRIP_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b")


def strip_ansi(text: str) -> str:
    return _ANSI_STRIP_RE.sub("", text)


class AnsiColorParser:
    def __init__(self, default_color: QColor = QColor("#d4d4d4")):
        self.default_color = default_color
        self.current_format = QTextCharFormat()
        self.current_format.setForeground(self.default_color)

    def reset(self) -> None:
        self.current_format = QTextCharFormat()
        self.current_format.setForeground(self.default_color)

    def append_to_cursor(self, text: str, cursor: QTextCursor) -> None:
        parts = _ANSI_SPLIT_RE.split(text)
        for part in parts:
            if not part:
                continue
            if part.startswith("\x1b["):
                cmd = part[-1]
                params = part[2:-1]
                if cmd == "m":
                    codes = [int(p) for p in params.split(";") if p.isdigit()] if params else [0]
                    for code in codes:
                        if code == 0:
                            self.current_format = QTextCharFormat()
                            self.current_format.setForeground(self.default_color)
                        elif code == 1:
                            self.current_format.setFontWeight(QFont.Weight.Bold.value)
                        elif code in (21, 22):
                            self.current_format.setFontWeight(QFont.Weight.Normal.value)
                        elif code in ANSI_COLORS:
                            self.current_format.setForeground(ANSI_COLORS[code])
                        elif code == 39:
                            self.current_format.setForeground(self.default_color)
            elif part == "\x1b":
                continue
            else:
                cursor.insertText(part, self.current_format)


def _ensure_app() -> QApplication:
    global _app
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _app = app
    return app


def confirm(title: str, text: str) -> bool:
    _ensure_app()
    box = QMessageBox()
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Icon.Question)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


def info(title: str, text: str) -> None:
    _ensure_app()
    box = QMessageBox()
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Icon.Information)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()


def dir_select(title: str) -> Path | None:
    _ensure_app()
    path = QFileDialog.getExistingDirectory(None, title)
    return Path(path) if path else None


def pick_vmf(maps_dir: Path) -> Path | None:
    _ensure_app()
    path, _filter = QFileDialog.getOpenFileName(
        None, i18n.t("wizard.pick_vmf_title"), str(maps_dir), "VMF files (*.vmf)",
    )
    return Path(path) if path else None


def _try_fix_hammeraddons_vdf(portal2_dir: Path) -> bool:
    vdf_path = portal2_dir / "hammeraddons.vdf"
    if not vdf_path.is_file():
        return False
    text = vdf_path.read_text(encoding="utf-8")
    if not re.search(r'"game"\s*""', text):
        return False
    config.backup_file(vdf_path)
    new_text = re.sub(r'"game"\s*""', '"game" "p2"', text)
    vdf_path.write_text(new_text, encoding="utf-8")
    return True


# --- Парсер параметров компиляторов ---------------------------------------

@dataclass
class CompilerOption:
    name: str
    default: str
    description: str
    takes_arg: bool


def get_postcompiler_fallback_options() -> list[CompilerOption]:
    return [
        CompilerOption("--propcombine", "", i18n.t("postcompiler.propcombine_desc"), False),
        CompilerOption("--no-propcombine", "", i18n.t("postcompiler.no_propcombine_desc"), False),
        CompilerOption("--dump-instances", "", i18n.t("postcompiler.dump_instances_desc"), False),
        CompilerOption("--debug", "", i18n.t("postcompiler.debug_desc"), False),
    ]


def parse_help_table(text: str) -> list[CompilerOption]:
    clean_text = strip_ansi(text)
    options: list[CompilerOption] = []
    lines = clean_text.splitlines()
    sep_found = False

    for line in lines:
        if "====" in line:
            sep_found = True
            continue
        if not sep_found:
            continue

        stripped = line.strip()
        if not stripped:
            continue
        if "no map name specified" in stripped.lower():
            break

        if "|" in line:
            parts = line.split("|")
            if len(parts) >= 3:
                name = parts[0].strip()
                default = parts[1].strip()
                desc = "|".join(parts[2:]).strip()
                if name.startswith("-"):
                    if name == "-game":
                        continue
                    takes_arg = bool(default) or ("<" in desc and ">" in desc)
                    options.append(
                        CompilerOption(
                            name=name,
                            default=default,
                            description=desc,
                            takes_arg=takes_arg,
                        )
                    )
        else:
            if options:
                options[-1].description += " " + stripped

    return options


def parse_postcompiler_help(exe_path: Path) -> list[CompilerOption]:
    try:
        env = os.environ.copy()
        bin_dir = str(exe_path.resolve().parent)
        cur_ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{bin_dir}:{cur_ld}" if cur_ld else bin_dir

        res = subprocess.run(
            [str(exe_path), "--help"],
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
            timeout=5,
        )
        clean_text = strip_ansi(res.stdout + "\n" + res.stderr)
        options = []
        for line in clean_text.splitlines():
            line_str = line.strip()
            if line_str.startswith("-"):
                m = re.match(r"^(--?[\w-]+)(?:\s+(.*))?$", line_str)
                if m:
                    opt_name = m.group(1)
                    if opt_name in ("-h", "--help", "-game"):
                        continue
                    rest = (m.group(2) or "").strip()
                    options.append(CompilerOption(name=opt_name, default="", description=rest, takes_arg=False))
        return options if options else get_postcompiler_fallback_options()
    except Exception:
        return get_postcompiler_fallback_options()


def get_compiler_options(tool_name: str, cfg: config.BuildConfig) -> list[CompilerOption]:
    if tool_name == "VBSP":
        exe = cfg.compilers_dir / config.COMPILER_BIN_NAMES["vbsp"]
    elif tool_name == "VVIS":
        exe = cfg.compilers_dir / config.COMPILER_BIN_NAMES["vvis"]
    elif tool_name == "VRAD":
        exe = cfg.compilers_dir / config.COMPILER_BIN_NAMES["vrad"]
    elif tool_name == "Postcompiler":
        if cfg.postcompiler_bin and cfg.postcompiler_bin.is_file():
            return parse_postcompiler_help(cfg.postcompiler_bin)
        return get_postcompiler_fallback_options()
    else:
        return []

    if not exe.is_file():
        return []

    try:
        env = os.environ.copy()
        bin_dir = str(exe.resolve().parent)
        cur_ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{bin_dir}:{cur_ld}" if cur_ld else bin_dir

        res = subprocess.run(
            [str(exe), "-help"],
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
            timeout=5,
        )
        text = res.stdout + "\n" + res.stderr
        return parse_help_table(text)
    except Exception as e:
        print(f"[p2_build_tool] Ошибка получения справки от {exe}: {e}", file=sys.stderr)
        return []


# --- Диалог выбора всех опций компилятора ---------------------------------

class CompilerOptionsDialog(QDialog):
    def __init__(
        self,
        tool_name: str,
        options: list[CompilerOption],
        current_args: list[str],
        default_args: list[str],
        parent=None,
    ):
        super().__init__(parent)
        self.tool_name = tool_name
        self.options = options
        self.default_args = default_args
        self.result_args: list[str] = list(current_args)

        self.setWindowTitle(i18n.t("options_dialog.title", tool=tool_name))
        self.resize(920, 580)
        layout = QVBoxLayout(self)

        search_layout = QHBoxLayout()
        search_lbl = QLabel(i18n.t("options_dialog.search_label"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(i18n.t("options_dialog.search_placeholder"))
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        search_layout.addWidget(search_lbl)
        search_layout.addWidget(self.search_edit, 1)
        layout.addLayout(search_layout)

        self.table = QTableWidget(len(options), 4)
        self.table.setHorizontalHeaderLabels([
            i18n.t("options_dialog.col_option"),
            i18n.t("options_dialog.col_value"),
            i18n.t("options_dialog.col_default"),
            i18n.t("options_dialog.col_desc"),
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 120)

        parsed_map, custom_tokens = self._parse_active_args(current_args, options)

        for row, opt in enumerate(options):
            name_item = QTableWidgetItem(opt.name)
            name_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            is_active = opt.name in parsed_map
            name_item.setCheckState(Qt.CheckState.Checked if is_active else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, name_item)

            val_text = parsed_map.get(opt.name, "")
            val_item = QTableWidgetItem(val_text)
            if not opt.takes_arg:
                val_item.setText("")
            self.table.setItem(row, 1, val_item)

            def_item = QTableWidgetItem(opt.default if opt.default else "—")
            def_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 2, def_item)

            desc_item = QTableWidgetItem(opt.description)
            desc_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 3, desc_item)

        self.table.cellChanged.connect(self._on_cell_changed)
        layout.addWidget(self.table)

        custom_layout = QHBoxLayout()
        custom_lbl = QLabel(i18n.t("options_dialog.custom_args_label"))
        self.custom_args_edit = QLineEdit(shlex.join(custom_tokens))
        self.custom_args_edit.setPlaceholderText(i18n.t("options_dialog.custom_args_placeholder"))
        custom_layout.addWidget(custom_lbl)
        custom_layout.addWidget(self.custom_args_edit, 1)
        layout.addLayout(custom_layout)

        btn_layout = QHBoxLayout()
        btn_reset = QPushButton(i18n.t("options_dialog.btn_reset"))
        btn_reset.clicked.connect(self._on_reset_clicked)

        btn_cancel = QPushButton(i18n.t("options_dialog.btn_cancel"))
        btn_cancel.clicked.connect(self.reject)

        btn_apply = QPushButton(i18n.t("options_dialog.btn_apply"))
        btn_apply.setDefault(True)
        btn_apply.clicked.connect(self._on_apply_clicked)

        btn_layout.addWidget(btn_reset)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(btn_apply)
        layout.addLayout(btn_layout)

    @staticmethod
    def _parse_active_args(current_args: list[str], options: list[CompilerOption]) -> tuple[dict[str, str], list[str]]:
        known_names = {opt.name for opt in options}
        parsed_map: dict[str, str] = {}
        custom_tokens: list[str] = []

        i = 0
        while i < len(current_args):
            token = current_args[i]
            if token in known_names:
                vals = []
                while i + 1 < len(current_args) and not current_args[i + 1].startswith("-"):
                    vals.append(current_args[i + 1])
                    i += 1
                parsed_map[token] = " ".join(vals)
                i += 1
            else:
                custom_tokens.append(token)
                i += 1

        return parsed_map, custom_tokens

    def _on_search_text_changed(self, query: str) -> None:
        q = query.strip().lower()
        for row in range(self.table.rowCount()):
            name_text = self.table.item(row, 0).text().lower()
            def_text = self.table.item(row, 2).text().lower()
            desc_text = self.table.item(row, 3).text().lower()
            matches = (not q) or (q in name_text) or (q in def_text) or (q in desc_text)
            self.table.setRowHidden(row, not matches)

    def _on_cell_changed(self, row: int, column: int) -> None:
        if column == 1:
            val_item = self.table.item(row, 1)
            name_item = self.table.item(row, 0)
            if val_item and name_item and val_item.text().strip():
                name_item.setCheckState(Qt.CheckState.Checked)

    def _on_reset_clicked(self) -> None:
        parsed_map, custom = self._parse_active_args(self.default_args, self.options)
        self.table.blockSignals(True)
        for row, opt in enumerate(self.options):
            is_active = opt.name in parsed_map
            self.table.item(row, 0).setCheckState(Qt.CheckState.Checked if is_active else Qt.CheckState.Unchecked)
            self.table.item(row, 1).setText(parsed_map.get(opt.name, ""))
        self.custom_args_edit.setText(shlex.join(custom))
        self.table.blockSignals(False)

    def _on_apply_clicked(self) -> None:
        res: list[str] = []
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            if name_item and name_item.checkState() == Qt.CheckState.Checked:
                opt_name = name_item.text()
                val_item = self.table.item(row, 1)
                val = val_item.text().strip() if val_item else ""
                res.append(opt_name)
                if val:
                    try:
                        res.extend(shlex.split(val))
                    except ValueError:
                        res.extend(val.split())

        custom_text = self.custom_args_edit.text().strip()
        if custom_text:
            try:
                res.extend(shlex.split(custom_text))
            except ValueError:
                res.extend(custom_text.split())

        self.result_args = res
        self.accept()


# --- Фоновый воркер компиляции --------------------------------------------

class CompileWorker(QThread):
    step_started = pyqtSignal(str)
    step_finished = pyqtSignal(str, bool)
    step_skipped = pyqtSignal(str)
    log_chunk_ready = pyqtSignal(str)
    build_finished = pyqtSignal(bool, str, object, str)

    def __init__(self, cfg: config.BuildConfig, vmf_path: Path):
        super().__init__()
        self.cfg = cfg
        self.vmf_path = vmf_path
        self._cancel_requested = False
        self._current_handle: runner.StepHandle | None = None

    def cancel(self) -> None:
        self._cancel_requested = True
        if self._current_handle and self._current_handle.is_running():
            try:
                self._current_handle.process.terminate()
            except Exception:
                pass

    def run(self) -> None:
        vmf_dir = self.vmf_path.parent
        vmf_name = self.vmf_path.stem
        bsp_file = vmf_dir / f"{vmf_name}.bsp"
        log_file = vmf_dir / f"{vmf_name}_build.log"
        log_file.write_text("", encoding="utf-8")

        vmf_arg = str(self.vmf_path)
        bsp_arg = str(bsp_file)

        build_start = time.monotonic()

        with open(log_file, "r", encoding="utf-8", errors="replace") as reader:

            def _run_step(name: str, cmd: list[str]) -> bool:
                if self._cancel_requested:
                    return False

                header = f"\n{'=' * 20} [{name}] {'=' * 20}\n"
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(header)

                self.step_started.emit(name)
                handle = runner.run_step(name, cmd, log_file)
                self._current_handle = handle

                while handle.is_running():
                    if self._cancel_requested:
                        try:
                            handle.process.terminate()
                        except Exception:
                            pass
                        break

                    chunk = reader.read()
                    if chunk:
                        self.log_chunk_ready.emit(chunk)
                    time.sleep(0.05)

                ok, _ = handle.finish()

                chunk = reader.read()
                if chunk:
                    self.log_chunk_ready.emit(chunk)

                success = ok and not self._cancel_requested
                self.step_finished.emit(name, success)
                return success

            def _skip_step(name: str) -> None:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"\n{'=' * 20} [{name}] {'=' * 20}{i18n.t('worker.step_skipped_header')}")
                self.step_skipped.emit(name)

            # 1. VBSP
            if self.cfg.vbsp_enabled:
                if not _run_step("VBSP", [*self.cfg.vbsp_cmd, vmf_arg]):
                    self.build_finished.emit(False, "VBSP", None, "")
                    return
            else:
                _skip_step("VBSP")
                if (self.cfg.vvis_enabled or self.cfg.vrad_enabled) and not bsp_file.is_file():
                    with open(log_file, "a", encoding="utf-8") as f:
                        f.write(i18n.t("worker.bsp_missing_error", file=bsp_file.name))
                    self.build_finished.emit(False, "VBSP", None, "")
                    return

            # 2. Postcompiler
            if self.cfg.postcompiler_enabled and self.cfg.postcompiler_bin and self.cfg.postcompiler_bin.is_file():
                if not _run_step("Postcompiler", [*self.cfg.postcompiler_cmd, bsp_arg]):
                    if _try_fix_hammeraddons_vdf(self.cfg.portal2_dir):
                        if not _run_step("Postcompiler", [*self.cfg.postcompiler_cmd, bsp_arg]):
                            self.build_finished.emit(False, "Postcompiler", None, "")
                            return
                    else:
                        self.build_finished.emit(False, "Postcompiler", None, "")
                        return
            else:
                _skip_step("Postcompiler")

            # 3. VVIS
            if self.cfg.vvis_enabled:
                if not _run_step("VVIS", [*self.cfg.vvis_cmd, bsp_arg]):
                    self.build_finished.emit(False, "VVIS", None, "")
                    return
            else:
                _skip_step("VVIS")

            # 4. VRAD
            if self.cfg.vrad_enabled:
                if not _run_step("VRAD", [*self.cfg.vrad_cmd, bsp_arg]):
                    self.build_finished.emit(False, "VRAD", None, "")
                    return
            else:
                _skip_step("VRAD")

            dest = None
            if bsp_file.is_file():
                self.cfg.dlc_maps_dir.mkdir(parents=True, exist_ok=True)
                dest = self.cfg.dlc_maps_dir / bsp_file.name
                shutil.copy2(bsp_file, dest)

            total_time = runner.fmt_time(int(time.monotonic() - build_start))
            self.build_finished.emit(True, "", dest, total_time)


# --- Главное окно сборки ---------------------------------------------------

class BuildDashboard(QDialog):
    def __init__(self, cfg: config.BuildConfig, vmf_path: Path, cfg_path: Path):
        _ensure_app()
        super().__init__()
        self.cfg = cfg
        self.vmf_path = vmf_path
        self.cfg_path = cfg_path
        self.worker: CompileWorker | None = None
        self.log_file = vmf_path.parent / f"{vmf_path.stem}_build.log"
        self._options_cache: dict[str, list[CompilerOption]] = {}
        self.ansi_parser = AnsiColorParser(default_color=QColor("#d4d4d4"))

        self.setWindowTitle(i18n.t("dashboard.title"))
        self.resize(940, 680)
        layout = QVBoxLayout(self)

        self.map_label = QLabel(i18n.t("dashboard.map_label", name=self.vmf_path.name))
        layout.addWidget(self.map_label)

        self._step_checkboxes: dict[str, QCheckBox] = {}
        self._inputs: dict[str, QLineEdit] = {}
        self._opt_buttons: dict[str, QPushButton] = {}
        self._status_labels: dict[str, QLabel] = {}

        for name in STEP_NAMES:
            row = QHBoxLayout()

            args_attr, enabled_attr = STEP_CONFIG_MAP[name]
            is_enabled: bool = getattr(self.cfg, enabled_attr)

            cb = QCheckBox()
            cb.setChecked(is_enabled)
            cb.setToolTip(i18n.t("dashboard.step_cb_tooltip"))
            cb.toggled.connect(
                lambda checked, a=enabled_attr, n=name: self._on_step_toggled(a, n, checked)
            )

            name_lbl = QLabel(f"<b>{name}:</b>")
            name_lbl.setFixedWidth(95)

            current_args: list[str] = getattr(self.cfg, args_attr)
            edit = QLineEdit(shlex.join(current_args))
            edit.setPlaceholderText(i18n.t("dashboard.args_placeholder"))
            edit.textChanged.connect(
                lambda text, a=args_attr, e=edit: self._on_arg_changed(a, e)
            )

            btn_opts = QPushButton(i18n.t("dashboard.btn_options"))
            btn_opts.setFixedWidth(105)
            btn_opts.setToolTip(i18n.t("dashboard.btn_options_tooltip", name=name))
            btn_opts.clicked.connect(lambda _, n=name: self._open_options_dialog(n))

            status_lbl = QLabel(i18n.t("status.wait"))
            status_lbl.setFixedWidth(85)
            status_lbl.setStyleSheet("color: #757575;")

            row.addWidget(cb)
            row.addWidget(name_lbl)
            row.addWidget(edit, 1)
            row.addWidget(btn_opts)
            row.addWidget(status_lbl)
            layout.addLayout(row)

            self._step_checkboxes[name] = cb
            self._inputs[name] = edit
            self._opt_buttons[name] = btn_opts
            self._status_labels[name] = status_lbl

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        mono_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono_font.setPointSize(9)
        self.log_view.setFont(mono_font)
        self.log_view.setStyleSheet("""
            QPlainTextEdit {
                background-color: #181818;
                color: #d4d4d4;
                border: 1px solid #333333;
                border-radius: 4px;
                padding: 6px;
            }
        """)
        layout.addWidget(self.log_view)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        btn_layout = QHBoxLayout()
        self.btn_rebuild = QPushButton(i18n.t("dashboard.btn_rebuild"))
        self.btn_pick_other = QPushButton(i18n.t("dashboard.btn_pick_other"))
        self.btn_open_log = QPushButton(i18n.t("dashboard.btn_open_log"))
        self.btn_exit = QPushButton(i18n.t("dashboard.btn_exit"))

        self.btn_rebuild.clicked.connect(self._on_rebuild_clicked)
        self.btn_pick_other.clicked.connect(self._on_pick_other_clicked)
        self.btn_open_log.clicked.connect(self._on_open_log_clicked)
        self.btn_exit.clicked.connect(self.close)

        btn_layout.addWidget(self.btn_rebuild)
        btn_layout.addWidget(self.btn_pick_other)
        btn_layout.addWidget(self.btn_open_log)
        btn_layout.addWidget(self.btn_exit)
        layout.addLayout(btn_layout)

        self._start_build()

    def _on_step_toggled(self, enabled_attr: str, name: str, checked: bool) -> None:
        setattr(self.cfg, enabled_attr, checked)
        self._inputs[name].setEnabled(checked)
        self._opt_buttons[name].setEnabled(checked)
        try:
            self.cfg.save(self.cfg_path)
        except Exception as e:
            print(f"[p2_build_tool] Ошибка сохранения конфига: {e}", file=sys.stderr)

    def _on_arg_changed(self, attr_name: str, edit: QLineEdit) -> None:
        text = edit.text().strip()
        try:
            parsed = shlex.split(text)
        except ValueError:
            parsed = text.split()

        setattr(self.cfg, attr_name, parsed)
        try:
            self.cfg.save(self.cfg_path)
        except Exception as e:
            print(f"[p2_build_tool] Ошибка сохранения конфига: {e}", file=sys.stderr)

    def _open_options_dialog(self, tool_name: str) -> None:
        if tool_name not in self._options_cache:
            self._options_cache[tool_name] = get_compiler_options(tool_name, self.cfg)

        opts = self._options_cache[tool_name]
        if not opts:
            info(i18n.t("wizard.warn_existing_symlink"), i18n.t("dashboard.no_options_warning", tool=tool_name))
            return

        args_attr = STEP_CONFIG_MAP[tool_name][0]
        current_args: list[str] = getattr(self.cfg, args_attr)
        default_args = DEFAULT_ARGS_MAP.get(tool_name, [])

        dlg = CompilerOptionsDialog(tool_name, opts, current_args, default_args, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_args = dlg.result_args
            setattr(self.cfg, args_attr, new_args)
            self._inputs[tool_name].setText(shlex.join(new_args))
            try:
                self.cfg.save(self.cfg_path)
            except Exception as e:
                print(f"[p2_build_tool] Ошибка сохранения конфига: {e}", file=sys.stderr)

    def _set_inputs_readonly(self, readonly: bool) -> None:
        for edit in self._inputs.values():
            edit.setReadOnly(readonly)
        for cb in self._step_checkboxes.values():
            cb.setEnabled(not readonly)
        for btn in self._opt_buttons.values():
            btn.setEnabled(not readonly)

    def _start_build(self) -> None:
        self.log_file = self.vmf_path.parent / f"{self.vmf_path.stem}_build.log"
        self.map_label.setText(i18n.t("dashboard.map_label", name=self.vmf_path.name))
        self.status_label.setText(i18n.t("dashboard.build_starting"))
        self.log_view.clear()
        self.ansi_parser.reset()

        self.btn_rebuild.setEnabled(False)
        self.btn_pick_other.setEnabled(False)
        self._set_inputs_readonly(True)

        for name in STEP_NAMES:
            enabled_attr = STEP_CONFIG_MAP[name][1]
            if getattr(self.cfg, enabled_attr):
                self._set_status(name, "wait")
            else:
                self._set_status(name, "skipped")

        self.worker = CompileWorker(self.cfg, self.vmf_path)
        self.worker.step_started.connect(lambda name: self._set_status(name, "running"))
        self.worker.step_finished.connect(
            lambda name, ok: self._set_status(name, "done" if ok else "failed")
        )
        self.worker.step_skipped.connect(lambda name: self._set_status(name, "skipped"))
        self.worker.log_chunk_ready.connect(self._on_log_chunk)
        self.worker.build_finished.connect(self._on_build_finished)
        self.worker.start()

    def _set_status(self, step: str, state: str) -> None:
        style_map = {
            "wait": "color: #757575;",
            "running": "color: #1976d2; font-weight: bold;",
            "done": "color: #2e7d32; font-weight: bold;",
            "skipped": "color: #888888; font-style: italic;",
            "failed": "color: #d32f2f; font-weight: bold;",
        }
        text = i18n.t(f"status.{state}")
        lbl = self._status_labels.get(step)
        if lbl:
            lbl.setText(text)
            lbl.setStyleSheet(style_map.get(state, ""))

    def _on_log_chunk(self, chunk: str) -> None:
        scrollbar = self.log_view.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 25

        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.ansi_parser.append_to_cursor(chunk, cursor)

        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def _on_build_finished(self, ok: bool, err_step: str, dest_bsp: object, total_time: str) -> None:
        self.btn_rebuild.setEnabled(True)
        self.btn_pick_other.setEnabled(True)
        self._set_inputs_readonly(False)

        for name in STEP_NAMES:
            enabled = getattr(self.cfg, STEP_CONFIG_MAP[name][1])
            self._inputs[name].setEnabled(enabled)
            self._opt_buttons[name].setEnabled(enabled)

        if ok:
            bsp_info = (
                i18n.t("dashboard.bsp_copied", dest=dest_bsp)
                if dest_bsp
                else i18n.t("dashboard.bsp_not_moved")
            )
            self.status_label.setText(
                i18n.t("dashboard.build_finished_success", time=total_time, bsp_info=bsp_info)
            )
        else:
            self.status_label.setText(
                i18n.t("dashboard.build_finished_error", step=err_step)
            )

    def _on_rebuild_clicked(self) -> None:
        self._start_build()

    def _on_pick_other_clicked(self) -> None:
        new_vmf = pick_vmf(self.cfg.maps_dir)
        if new_vmf is not None:
            self.vmf_path = new_vmf
            self._start_build()

    def _on_open_log_clicked(self) -> None:
        if self.log_file.is_file():
            subprocess.run(["xdg-open", str(self.log_file)])

    def closeEvent(self, event) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(1500)
        event.accept()


def run_build_window(cfg: config.BuildConfig, vmf_path: Path, cfg_path: Path) -> None:
    app = _ensure_app()
    dlg = BuildDashboard(cfg, vmf_path, cfg_path)
    dlg.show()
    app.exec()
