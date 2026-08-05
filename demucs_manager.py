"""
demucs_manager.py — Quản lý cài đặt Demucs tự động (lazy install).

Cách dùng trong honggou_tab.py:
    from demucs_manager import get_demucs_python, ensure_demucs_installed_ui

1. Thay sys.executable trong cmd_demucs bằng get_demucs_python()
2. Gọi ensure_demucs_installed_ui(parent_widget, callback) trước khi tách nhạc
"""

import os
import sys
import shutil
import zipfile
import subprocess
import threading
import urllib.request

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QProgressBar, QWidget
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QFont

# ─── Cấu hình ────────────────────────────────────────────────────────────────

# Python embeddable Windows 64-bit (không cần cài, ~10MB)
PYTHON_ZIP_URL = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
# pip bootstrapper
GET_PIP_URL    = "https://bootstrap.pypa.io/get-pip.py"
# Demucs CPU-only (torch nhẹ hơn GPU rất nhiều)
TORCH_INDEX    = "https://download.pytorch.org/whl/cpu"

# Thư mục cài đặt: AppData\Roaming\AnhStudio (tồn tại vĩnh viễn, không bị Nuitka xóa)
def _app_dir() -> str:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(appdata, "AnhStudio")

def _portable_dir() -> str:
    return os.path.join(_app_dir(), "python_portable")

def _python_exe() -> str:
    return os.path.join(_portable_dir(), "python.exe")

def _pip_exe() -> str:
    return os.path.join(_portable_dir(), "Scripts", "pip.exe")

# ─── API công khai ────────────────────────────────────────────────────────────

def get_demucs_python() -> str:
    """
    Trả về đường dẫn python.exe dùng để chạy demucs.
    Ưu tiên: portable Python đã cài > sys.executable (fallback).
    """
    py = _python_exe()
    if os.path.exists(py):
        return py
    # Fallback: dùng Python hệ thống nếu có (máy dev)
    return sys.executable


def is_demucs_ready() -> bool:
    """True nếu portable Python đã cài và demucs đã có."""
    py = _python_exe()
    if not os.path.exists(py):
        return False
    try:
        si = _si()
        res = subprocess.run(
            [py, "-c", "import demucs; print('ok')"],
            capture_output=True, text=True, timeout=15,
            startupinfo=si
        )
        return res.stdout.strip() == "ok"
    except Exception:
        return False


def ensure_demucs_installed_ui(parent: QWidget, on_ready):
    """
    Kiểm tra demucs. Nếu chưa có → hiện dialog hỏi cài.
    on_ready() được gọi trên main thread sau khi cài xong (hoặc đã có sẵn).
    """
    if is_demucs_ready():
        on_ready()
        return
    dlg = _InstallDialog(parent)
    dlg.accepted_signal.connect(lambda: _start_install(parent, dlg, on_ready))
    dlg.exec()


# ─── Nội bộ ──────────────────────────────────────────────────────────────────

def _si():
    """STARTUPINFO ẩn console trên Windows."""
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return si


def _start_install(parent: QWidget, ask_dlg: "QDialog", on_ready):
    prog_dlg = _ProgressDialog(parent)
    prog_dlg.show()

    worker = _InstallWorker(_portable_dir(), _python_exe())
    worker.log.connect(prog_dlg.append_log)
    worker.progress.connect(prog_dlg.set_progress)
    worker.finished.connect(lambda ok, msg: _on_install_done(ok, msg, prog_dlg, on_ready))
    worker.start()
    prog_dlg._worker = worker  # giữ reference


def _on_install_done(ok: bool, msg: str, prog_dlg: "QDialog", on_ready):
    prog_dlg.close()
    if ok:
        on_ready()
    else:
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.critical(
            None, "Cài đặt thất bại",
            f"Không thể cài Demucs:\n{msg}\n\n"
            "Kiểm tra kết nối Internet rồi thử lại."
        )


# ─── Dialog hỏi cài ──────────────────────────────────────────────────────────

class _InstallDialog(QDialog):
    accepted_signal = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tính năng Tách Âm Thanh")
        self.setFixedWidth(420)
        self.setModal(True)
        self.setStyleSheet("""
            QDialog   { background:#1e293b; }
            QLabel    { color:#e2e8f0; }
            QPushButton { border-radius:8px; padding:8px 20px;
                          font-weight:bold; font-size:13px; }
        """)

        lay = QVBoxLayout(self)
        lay.setSpacing(14)
        lay.setContentsMargins(24, 24, 24, 20)

        icon = QLabel("🎵")
        icon.setFont(QFont("Segoe UI Emoji", 32))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(icon)

        title = QLabel("Cần cài thêm tính năng Tách Nhạc Nền")
        title.setFont(QFont("Arial", 13, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color:#6ee7b7;")
        lay.addWidget(title)

        info = QLabel(
            "Tính năng này dùng AI Demucs để tách nhạc nền ra\n"
            "khỏi video, giữ lại thoại + hiệu ứng âm thanh.\n\n"
            "⬇  Cần tải thêm ~500 MB (chỉ 1 lần duy nhất)\n"
            "📂  Lưu vào thư mục app, không ảnh hưởng hệ thống\n"
            "🔄  Lần sau dùng ngay, không cần tải lại"
        )
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info.setStyleSheet("color:#94a3b8; line-height:1.5;")
        lay.addWidget(info)

        btn_row = QHBoxLayout()
        btn_later = QPushButton("Để sau")
        btn_later.setStyleSheet(
            "QPushButton { background:#334155; color:#94a3b8; } "
            "QPushButton:hover { background:#475569; }"
        )
        btn_later.clicked.connect(self.reject)

        btn_install = QPushButton("✅  Cài ngay")
        btn_install.setStyleSheet(
            "QPushButton { background:#059669; color:white; } "
            "QPushButton:hover { background:#047857; }"
        )
        btn_install.clicked.connect(self._on_install)

        btn_row.addWidget(btn_later)
        btn_row.addWidget(btn_install)
        lay.addLayout(btn_row)

    def _on_install(self):
        self.accept()
        self.accepted_signal.emit()


# ─── Dialog tiến trình cài ───────────────────────────────────────────────────

class _ProgressDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Đang cài đặt Demucs...")
        self.setFixedSize(460, 260)
        self.setModal(True)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowCloseButtonHint)
        self.setStyleSheet("""
            QDialog  { background:#1e293b; }
            QLabel   { color:#e2e8f0; font-size:13px; }
            QTextEdit { background:#0f172a; color:#a3e635;
                        font-family:Consolas; font-size:9pt;
                        border:1px solid #334155; border-radius:6px; }
            QProgressBar {
                border:none; border-radius:6px;
                background:#334155; height:14px; text-align:center; color:white;
            }
            QProgressBar::chunk { background:#10b981; border-radius:6px; }
        """)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 16)
        lay.setSpacing(10)

        self.lbl = QLabel("⏳ Đang chuẩn bị...")
        lay.addWidget(self.lbl)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        lay.addWidget(self.bar)

        from PyQt6.QtWidgets import QTextEdit
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFixedHeight(140)
        lay.addWidget(self.log_box)

    def append_log(self, text: str):
        self.log_box.append(text)
        self.lbl.setText(text[:80])

    def set_progress(self, val: int):
        self.bar.setValue(val)


# ─── Worker cài đặt (chạy nền) ───────────────────────────────────────────────

class _InstallWorker(QThread):
    log      = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished = pyqtSignal(bool, str)  # (success, error_msg)

    def __init__(self, portable_dir: str, python_exe: str):
        super().__init__()
        self._dir = portable_dir
        self._py  = python_exe

    def run(self):
        try:
            # ── Bước 1: Tải & giải nén Python portable ──────────────────
            if not os.path.exists(self._py):
                self.log.emit("📥 Đang tải Python portable (~10 MB)...")
                self.progress.emit(5)

                os.makedirs(self._dir, exist_ok=True)
                zip_path = os.path.join(self._dir, "_python.zip")
                self._download(PYTHON_ZIP_URL, zip_path, 5, 20)

                self.log.emit("📦 Đang giải nén Python...")
                self.progress.emit(22)
                with zipfile.ZipFile(zip_path, "r") as z:
                    z.extractall(self._dir)
                os.remove(zip_path)

                # Bật import site-packages (mặc định bị tắt trong embedded)
                pth_files = [f for f in os.listdir(self._dir) if f.endswith("._pth")]
                for pth in pth_files:
                    pth_path = os.path.join(self._dir, pth)
                    with open(pth_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    # Bỏ comment dòng import site
                    content = content.replace("#import site", "import site")
                    with open(pth_path, "w", encoding="utf-8") as f:
                        f.write(content)

                self.log.emit("✅ Python portable đã sẵn sàng.")
                self.progress.emit(25)
            else:
                self.log.emit("✅ Python portable đã có sẵn.")
                self.progress.emit(25)

            # ── Bước 2: Cài pip ──────────────────────────────────────────
            pip = _pip_exe()
            if not os.path.exists(pip):
                self.log.emit("📥 Đang cài pip...")
                get_pip = os.path.join(self._dir, "get-pip.py")
                self._download(GET_PIP_URL, get_pip, 25, 30)
                self._run([self._py, get_pip, "--no-warn-script-location"], 30, 35)
                if os.path.exists(get_pip):
                    os.remove(get_pip)
                self.log.emit("✅ pip đã sẵn sàng.")
            else:
                self.log.emit("✅ pip đã có sẵn.")
            self.progress.emit(35)

            # ── Bước 3: Cài torch CPU-only (~200 MB) ────────────────────
            self.log.emit("📥 Đang cài torch CPU (~200 MB)... Vui lòng chờ.")
            self._run([
                pip, "install",
                "torch", "torchaudio",
                "--extra-index-url", TORCH_INDEX,
                "--no-warn-script-location", "-q"
            ], 35, 75)
            self.log.emit("✅ torch đã cài xong.")
            self.progress.emit(75)

            # ── Bước 4: Cài demucs (~50 MB) ─────────────────────────────
            self.log.emit("📥 Đang cài demucs...")
            self._run([
                pip, "install", "demucs",
                "--no-warn-script-location", "-q"
            ], 75, 95)
            self.log.emit("✅ demucs đã cài xong.")
            self.progress.emit(95)

            # ── Bước 5: Kiểm tra ─────────────────────────────────────────
            self.log.emit("🔍 Đang kiểm tra...")
            res = subprocess.run(
                [self._py, "-c", "import demucs; print('ok')"],
                capture_output=True, text=True, timeout=30,
                startupinfo=_si()
            )
            if res.stdout.strip() != "ok":
                raise RuntimeError("Kiểm tra demucs thất bại: " + res.stderr[:200])

            self.progress.emit(100)
            self.log.emit("🎉 Cài đặt hoàn tất! Sẵn sàng tách nhạc nền.")
            self.finished.emit(True, "")

        except Exception as e:
            self.finished.emit(False, str(e))

    def _download(self, url: str, dest: str, prog_start: int, prog_end: int):
        """Tải file với progress."""
        def _reporthook(count, block_size, total_size):
            if total_size > 0:
                pct = prog_start + int((prog_end - prog_start) * count * block_size / total_size)
                self.progress.emit(min(pct, prog_end))
        urllib.request.urlretrieve(url, dest, _reporthook)

    def _run(self, cmd: list, prog_start: int, prog_end: int):
        """Chạy lệnh và stream output vào log."""
        self.progress.emit(prog_start)
        si = _si()
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="ignore",
            startupinfo=si
        )
        for line in proc.stdout:
            line = line.strip()
            if line:
                self.log.emit(line[:120])
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"Lệnh thất bại (code {proc.returncode}): {' '.join(cmd[:3])}")
        self.progress.emit(prog_end)
