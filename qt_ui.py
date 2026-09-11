"""
UI-слой на PyQt6.

Прогресс-бары заменены на интерактивные поля аргументов каждого инструмента
(VBSP, Postcompiler, VVIS, VRAD) с мгновенным сохранением в compile_opts.json.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QFontDatabase, QTextCursor
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout,
)

import config
import runner

STEP_NAMES = ["VBSP", "Postcompiler", "VVIS", "VRAD"]
STEP_CONFIG_MAP = {
    "VBSP": "vbsp_args",
    "Postcompiler": "postcompiler_args",
    "VVIS": "vvis_args",
    "VRAD": "vrad_args",
}

_app: QApplication | None = None


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
        None, "Выбери VMF", str(maps_dir), "VMF files (*.vmf)",
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


# --- Фоновый воркер компиляции с потоковым чтением лога --------------------

class CompileWorker(QThread):
    step_started = pyqtSignal(str)
    step_finished = pyqtSignal(str, bool)
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

        w_vmf = runner.to_wine_path(self.vmf_path)
        w_bsp = runner.to_wine_path(bsp_file)

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

            # 1. VBSP
            if not _run_step("VBSP", [*self.cfg.vbsp_cmd, w_vmf]):
                self.build_finished.emit(False, "VBSP", None, "")
                return

            # 2. Postcompiler
            if not _run_step("Postcompiler", [*self.cfg.postcompiler_cmd, str(bsp_file)]):
                if _try_fix_hammeraddons_vdf(self.cfg.portal2_dir):
                    if not _run_step("Postcompiler", [*self.cfg.postcompiler_cmd, str(bsp_file)]):
                        self.build_finished.emit(False, "Postcompiler", None, "")
                        return
                else:
                    self.build_finished.emit(False, "Postcompiler", None, "")
                    return

            # 3. VVIS
            if not _run_step("VVIS", [*self.cfg.vvis_cmd, w_bsp]):
                self.build_finished.emit(False, "VVIS", None, "")
                return

            # 4. VRAD
            if not _run_step("VRAD", [*self.cfg.vrad_cmd, w_bsp]):
                self.build_finished.emit(False, "VRAD", None, "")
                return

            self.cfg.dlc_maps_dir.mkdir(parents=True, exist_ok=True)
            dest = self.cfg.dlc_maps_dir / bsp_file.name
            shutil.copy2(bsp_file, dest)

            total_time = runner.fmt_time(int(time.monotonic() - build_start))
            self.build_finished.emit(True, "", dest, total_time)


# --- Главное окно сборки ---------------------------------------------------

class BuildDashboard(QDialog):
    _STATUS_STYLES = {
        "wait": ("ожидание", "color: #757575;"),
        "running": ("сборка...", "color: #1976d2; font-weight: bold;"),
        "done": ("готово ✓", "color: #2e7d32; font-weight: bold;"),
        "failed": ("ОШИБКА ✗", "color: #d32f2f; font-weight: bold;"),
    }

    def __init__(self, cfg: config.BuildConfig, vmf_path: Path, cfg_path: Path):
        _ensure_app()
        super().__init__()
        self.cfg = cfg
        self.vmf_path = vmf_path
        self.cfg_path = cfg_path
        self.worker: CompileWorker | None = None
        self.log_file = vmf_path.parent / f"{vmf_path.stem}_build.log"

        self.setWindowTitle("Сборка карты Portal 2")
        self.resize(860, 640)
        layout = QVBoxLayout(self)

        # Шапка
        self.map_label = QLabel(f"<b>Карта:</b> {self.vmf_path.name}")
        layout.addWidget(self.map_label)

        # Блок аргументов инструментов
        self._inputs: dict[str, QLineEdit] = {}
        self._status_labels: dict[str, QLabel] = {}

        for name in STEP_NAMES:
            row = QHBoxLayout()

            name_lbl = QLabel(f"<b>{name}:</b>")
            name_lbl.setFixedWidth(100)

            attr_name = STEP_CONFIG_MAP[name]
            current_args: list[str] = getattr(self.cfg, attr_name)

            edit = QLineEdit(shlex.join(current_args))
            edit.setPlaceholderText("без аргументов")
            # Мгновенное сохранение при изменении
            edit.textChanged.connect(
                lambda text, a=attr_name, e=edit: self._on_arg_changed(a, e)
            )

            status_lbl = QLabel("ожидание")
            status_lbl.setFixedWidth(85)
            status_lbl.setStyleSheet("color: #757575;")

            row.addWidget(name_lbl)
            row.addWidget(edit, 1)
            row.addWidget(status_lbl)
            layout.addLayout(row)

            self._inputs[name] = edit
            self._status_labels[name] = status_lbl

        # Окно лога
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        mono_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono_font.setPointSize(9)
        self.log_view.setFont(mono_font)
        layout.addWidget(self.log_view)

        # Статус сборки
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # Кнопки
        btn_layout = QHBoxLayout()
        self.btn_rebuild = QPushButton("Пересобрать")
        self.btn_pick_other = QPushButton("Выбрать другую карту")
        self.btn_open_log = QPushButton("Открыть лог в редакторе")
        self.btn_exit = QPushButton("Выход")

        self.btn_rebuild.clicked.connect(self._on_rebuild_clicked)
        self.btn_pick_other.clicked.connect(self._on_pick_other_clicked)
        self.btn_open_log.clicked.connect(self._on_open_log_clicked)
        self.btn_exit.clicked.connect(self.close)

        btn_layout.addWidget(self.btn_rebuild)
        btn_layout.addWidget(self.btn_pick_other)
        btn_layout.addWidget(self.btn_open_log)
        btn_layout.addWidget(self.btn_exit)
        layout.addLayout(btn_layout)

        # Запуск первичной сборки
        self._start_build()

    def _on_arg_changed(self, attr_name: str, edit: QLineEdit) -> None:
        """Мгновенно обновляет BuildConfig и сохраняет compile_opts.json."""
        text = edit.text().strip()
        try:
            parsed = shlex.split(text)
        except ValueError:
            # На случай, если пользователь прямо сейчас вводит незакрытую кавычку
            parsed = text.split()

        setattr(self.cfg, attr_name, parsed)
        try:
            self.cfg.save(self.cfg_path)
        except Exception as e:
            print(f"[p2_build_tool] Ошибка сохранения конфига: {e}", file=sys.stderr)

    def _set_inputs_readonly(self, readonly: bool) -> None:
        """Блокирует редактирование полей на время активной сборки."""
        for edit in self._inputs.values():
            edit.setReadOnly(readonly)

    def _start_build(self) -> None:
        self.log_file = self.vmf_path.parent / f"{self.vmf_path.stem}_build.log"
        self.map_label.setText(f"<b>Карта:</b> {self.vmf_path.name}")
        self.status_label.setText("<i>Сборка запущена...</i>")
        self.log_view.clear()

        self.btn_rebuild.setEnabled(False)
        self.btn_pick_other.setEnabled(False)
        self._set_inputs_readonly(True)

        for name in STEP_NAMES:
            self._set_status(name, "wait")

        self.worker = CompileWorker(self.cfg, self.vmf_path)
        self.worker.step_started.connect(lambda name: self._set_status(name, "running"))
        self.worker.step_finished.connect(
            lambda name, ok: self._set_status(name, "done" if ok else "failed")
        )
        self.worker.log_chunk_ready.connect(self._on_log_chunk)
        self.worker.build_finished.connect(self._on_build_finished)
        self.worker.start()

    def _set_status(self, step: str, state: str) -> None:
        text, style = self._STATUS_STYLES.get(state, (state, ""))
        lbl = self._status_labels.get(step)
        if lbl:
            lbl.setText(text)
            lbl.setStyleSheet(style)

    def _on_log_chunk(self, chunk: str) -> None:
        scrollbar = self.log_view.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 25

        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(chunk)

        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def _on_build_finished(self, ok: bool, err_step: str, dest_bsp: object, total_time: str) -> None:
        self.btn_rebuild.setEnabled(True)
        self.btn_pick_other.setEnabled(True)
        self._set_inputs_readonly(False)

        if ok:
            self.status_label.setText(
                f"<b style='color: green;'>✓ Сборка успешно завершена за {total_time}!</b><br>"
                f"BSP скопирован в: {dest_bsp}"
            )
        else:
            self.status_label.setText(
                f"<b style='color: red;'>✗ Сборка прервана на этапе: {err_step}.</b> "
                "Подробности смотрите в логе выше."
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
