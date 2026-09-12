"""
Запуск отдельных шагов компиляции (VBSP/Postcompiler/VVIS/VRAD).

Намеренно НЕ знает про yad и вообще про UI. Отдаёт наружу объект StepHandle,
за которым UI-слой (или консоль, или что угодно) может наблюдать: опрашивать
poll(), читать лог по мере роста файла. Это тот же принцип, что и в
оригинальном run_step() из баша (фоновый процесс + цикл рендера), только
без жёсткой связки с конкретным способом отображения.

Все инструменты (vbsp++/vvis++/vrad++ из toolsplusplus и postcompiler) —
нативные Linux-бинарники, запускаются напрямую, без wine и без конвертации
путей в формат Z:\\...
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
import os


def fmt_time(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}с"
    return f"{seconds // 60}м {seconds % 60}с"


@dataclass
class StepHandle:
    """Живой хендл запущенного шага — опрашивается снаружи, пока идёт сборка."""
    name: str
    log_file: Path
    process: subprocess.Popen
    start_time: float = field(default_factory=time.monotonic)
    _log_fh: object = field(default=None, repr=False)

    def is_running(self) -> bool:
        return self.process.poll() is None

    def elapsed(self) -> int:
        return int(time.monotonic() - self.start_time)

    def tail(self, n: int = 6) -> list[str]:
        """Последние n строк лога — для live-отображения в UI."""
        if not self.log_file.is_file():
            return []
        # Для больших логов построчное чтение с конца было бы эффективнее,
        # но здесь логи компиляции редко превышают пару МБ — простота важнее.
        lines = self.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:]

    def wait(self) -> tuple[bool, int]:
        """
        Блокирующее ожидание завершения. Возвращает (успех, время_в_секундах).
        Использовать, когда live-опрос не нужен — например, в CLI-режиме.
        """
        self.process.wait()
        if self._log_fh is not None:
            self._log_fh.close()
        elapsed = self.elapsed()
        return self.process.returncode == 0, elapsed

    def finish(self) -> tuple[bool, int]:
        """
        Вызывать после того, как is_running() вернул False — закрывает файл
        лога и возвращает (успех, время_в_секундах). Не блокирует.
        """
        if self._log_fh is not None:
            self._log_fh.close()
        return self.process.returncode == 0, self.elapsed()


def run_step(name: str, cmd: list[str], log_file: Path) -> StepHandle:
    """
    Запускает нативный бинарник в фоне (аналог `cmd >> log_file 2>&1 &` из баша).
    """
    real_cmd = list(cmd)

    env = os.environ.copy()
    if real_cmd:
        bin_dir = str(Path(real_cmd[0]).resolve().parent)
        # Некоторые сборки toolsplusplus поставляются с собственными .so
        # рядом с бинарником — на всякий случай добавляем их каталог в поиск,
        # чтобы не ловить "error while loading shared libraries" на чужой машине.
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{bin_dir}:{existing}" if existing else bin_dir

    log_fh = open(log_file, "ab")  # append-режим, как >> в баше
    process = subprocess.Popen(
        real_cmd,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        env=env,
    )
    handle = StepHandle(name=name, log_file=log_file, process=process)
    handle._log_fh = log_fh
    return handle


def run_step_blocking(name: str, cmd: list[str], log_file: Path,
                       poll_interval: float = 0.5,
                       on_tick=None) -> tuple[bool, int]:
    handle = run_step(name, cmd, log_file)
    while handle.is_running():
        if on_tick is not None:
            on_tick(handle)
        time.sleep(poll_interval)

    ok, elapsed = handle.finish()
    # Вызываем ещё раз после завершения процесса,
    # чтобы гарантированно отрисовать финальные строки лога
    if on_tick is not None:
        on_tick(handle)

    return ok, elapsed
