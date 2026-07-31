"""
CapCut TTS/SRT Widget — PyQt6 version
Được tích hợp vào Honggou Downloader Pro
"""

import sys, os, json, threading, queue, time, tempfile, subprocess, shutil
from pathlib import Path
from datetime import datetime

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QTabWidget,
    QPushButton, QLabel, QLineEdit, QTextEdit, QComboBox,
    QDoubleSpinBox, QCheckBox, QFileDialog, QMessageBox,
    QProgressBar, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QScrollArea, QFrame, QSizePolicy, QListWidget, QListWidgetItem,
    QSpinBox, QAbstractItemView
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QSize
from PyQt6.QtGui import QFont, QColor, QTextCursor, QTextCharFormat

ROOT_DIR = Path(__file__).parent

# Trỏ pydub tới ffmpeg.exe nằm CẠNH app (nếu có), tránh lỗi WinError 2
# "cannot find the file" khi pydub không thấy ffmpeg trong PATH hệ thống.
def _setup_ffmpeg_for_pydub():
    import shutil as _sh
    candidates = [
        str(ROOT_DIR / "ffmpeg.exe"),
        str(ROOT_DIR / "ffmpeg"),
        str(ROOT_DIR / "bin" / "ffmpeg.exe"),
    ]
    ffmpeg_path = next((p for p in candidates if os.path.exists(p)), None)
    if not ffmpeg_path:
        ffmpeg_path = _sh.which("ffmpeg")  # thử PATH hệ thống
    try:
        from pydub import AudioSegment as _AS
        if ffmpeg_path:
            _AS.converter = ffmpeg_path
            _AS.ffmpeg = ffmpeg_path
            probe = os.path.join(os.path.dirname(ffmpeg_path), "ffprobe.exe")
            if os.path.exists(probe):
                _AS.ffprobe = probe
            os.environ["PATH"] = os.path.dirname(ffmpeg_path) + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass
    return ffmpeg_path

FFMPEG_PATH = _setup_ffmpeg_for_pydub()

try:
    from capcut_tts_api import CapCutClient
    SDK_OK = True
    SDK_ERROR = ""
except ImportError as e:
    SDK_OK = False
    SDK_ERROR = str(e)

# ══════════════════════════════════════════════════════════════════════
#  COLORS
# ══════════════════════════════════════════════════════════════════════
BG      = "#0f1117"
PANEL   = "#1a1d27"
CARD    = "#252836"
INPUT   = "#2d3142"
ACCENT  = "#5865f2"
GREEN   = "#57f287"
YELLOW  = "#fee75c"
RED     = "#ed4245"
CYAN    = "#00c8ff"
FG      = "#ffffff"
FG2     = "#b9bbbe"
FG3     = "#72767d"

# ══════════════════════════════════════════════════════════════════════
#  LOG QUEUE TOÀN CỤC
# ══════════════════════════════════════════════════════════════════════
_log_q: queue.Queue = queue.Queue()

def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(msg, level="INFO"):
    _log_q.put((level, f"[{ts()}] [{level}] {msg}"))


# ══════════════════════════════════════════════════════════════════════
#  STYLE HELPERS
# ══════════════════════════════════════════════════════════════════════
def _btn_style(bg, fg, bg_hover):
    return (f"QPushButton {{ background: {bg}; color: {fg}; border: none; border-radius: 5px; "
            f"padding: 7px 16px; font-weight: bold; cursor: pointer; }}"
            f"QPushButton:hover {{ background: {bg_hover}; }}"
            f"QPushButton:disabled {{ background: #333; color: #555; }}")

def _entry_style():
    return (f"QLineEdit {{ background: {INPUT}; color: {FG}; border: 1px solid #3d4260; "
            f"border-radius: 4px; padding: 5px 8px; }}"
            f"QLineEdit:focus {{ border-color: {ACCENT}; }}")

def _combo_style():
    return (f"QComboBox {{ background: {INPUT}; color: {FG}; border: 1px solid #3d4260; "
            f"border-radius: 4px; padding: 4px 8px; }}"
            f"QComboBox::drop-down {{ border: none; width: 20px; }}"
            f"QComboBox QAbstractItemView {{ background: {CARD}; color: {FG}; "
            f"selection-background-color: {ACCENT}; }}")

def _textedit_style(fg=None):
    c = fg or FG2
    return (f"QTextEdit {{ background: {INPUT}; color: {c}; border: none; "
            f"border-radius: 4px; padding: 6px; font-family: Consolas; font-size: 9pt; }}")


# ══════════════════════════════════════════════════════════════════════
#  MAIN WIDGET
# ══════════════════════════════════════════════════════════════════════
class CapCutTTSWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {PANEL}; color: {FG};")

        # ── state ────────────────────────────────────────────────────
        self.client = None
        self.voices = []
        self._fvoices = []
        self.busy = False
        self.is_running = False
        self.current_task_type = None
        self.stop_flag = False
        self.task_queue: queue.Queue = queue.Queue()
        self._last_tts_file = None

        self.srt_files = []       # list of [path, QCheckBox]
        self.srt_loaded = set()

        self._lang_filter = "Tất cả"
        self._rate = 1.0
        self._out_dir = str(ROOT_DIR / "output_tts")
        self._out_stt = str(ROOT_DIR / "output_stt")

        # UI update queue (for cross-thread safe UI calls)
        self._ui_q: queue.Queue = queue.Queue()

        self._build_ui()
        self._init_client()

        # timers
        self._log_timer = QTimer(self)
        self._log_timer.timeout.connect(self._poll_log)
        self._log_timer.start(100)

        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._process_ui_queue)
        self._ui_timer.start(50)

    def _schedule_ui(self, fn):
        """Đưa hàm fn vào queue để chạy trên main thread."""
        self._ui_q.put(fn)

    def _process_ui_queue(self):
        try:
            while True:
                fn = self._ui_q.get_nowait()
                fn()
        except queue.Empty:
            pass

    # ─────────────────────────────────────────────────────────────────
    #  BUILD UI
    # ─────────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStyleSheet("QSplitter::handle { background: #2e3147; width: 2px; }")

        # ── LEFT: notebook ──────────────────────────────────────────
        self.nb = QTabWidget()
        self.nb.setStyleSheet(f"""
            QTabWidget::pane {{ border: none; background: {PANEL}; }}
            QTabBar::tab {{ background: {CARD}; color: {FG2}; padding: 8px 18px;
                           font-weight: bold; font-size: 9pt; border: none; }}
            QTabBar::tab:selected {{ background: {ACCENT}; color: white; }}
            QTabBar::tab:hover {{ background: #3d4260; }}
        """)
        self._build_tts_tab()
        self._build_stt_tab()
        self._build_device_tab()
        self._build_voices_tab()
        self._build_srt_tab()

        splitter.addWidget(self.nb)

        # ── RIGHT: log ──────────────────────────────────────────────
        log_wrap = QWidget()
        log_wrap.setStyleSheet(f"background: #0a0c14;")
        lw_layout = QVBoxLayout(log_wrap)
        lw_layout.setContentsMargins(0, 0, 0, 0)
        lw_layout.setSpacing(0)

        log_hdr = QWidget()
        log_hdr.setFixedHeight(34)
        log_hdr.setStyleSheet(f"background: {CARD}; border-bottom: 1px solid #2e3147;")
        lh = QHBoxLayout(log_hdr)
        lh.setContentsMargins(10, 0, 8, 0)
        lbl_log = QLabel("📋  Log theo dõi")
        lbl_log.setStyleSheet(f"color: {FG}; font-weight: bold;")
        btn_clr = QPushButton("✕ Xóa")
        btn_clr.setFixedHeight(24)
        btn_clr.setStyleSheet(_btn_style("#3d2030", "#ff7a8a", "#5d1040"))
        btn_clr.clicked.connect(self._clear_log)
        lh.addWidget(lbl_log)
        lh.addStretch()
        lh.addWidget(btn_clr)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setStyleSheet(
            "QTextEdit { background: #0a0c14; color: #c8ffd4; "
            "font-family: Consolas; font-size: 9pt; border: none; padding: 4px; }")

        lw_layout.addWidget(log_hdr)
        lw_layout.addWidget(self.log_box)

        # status bar
        self.lbl_status = QLabel("● Đang khởi động…")
        self.lbl_status.setFixedHeight(22)
        self.lbl_status.setStyleSheet(f"background: {CARD}; color: {YELLOW}; "
                                       "font-weight: bold; padding: 0 10px;")
        lw_layout.addWidget(self.lbl_status)

        splitter.addWidget(log_wrap)
        splitter.setSizes([680, 320])
        root.addWidget(splitter)

    # ─────────────────────────────────────────────────────────────────
    #  TAB: TTS
    # ─────────────────────────────────────────────────────────────────
    def _build_tts_tab(self):
        tab = QWidget(); tab.setStyleSheet(f"background: {PANEL};")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)
        self.nb.addTab(tab, "🔊  TTS")

        # Text input
        layout.addWidget(self._sec_label("✏  Văn bản cần đọc"))
        self.txt_input = QTextEdit()
        self.txt_input.setPlaceholderText("Nhập văn bản cần đọc…")
        self.txt_input.setText("Xin chào! Đây là công cụ chuyển văn bản sang giọng nói từ CapCut.")
        self.txt_input.setFixedHeight(110)
        self.txt_input.setStyleSheet(_textedit_style(FG))
        layout.addWidget(self.txt_input)

        layout.addWidget(self._sep())
        layout.addWidget(self._sec_label("🎛  Giọng đọc"))

        # Voice + Lang row
        row1 = QHBoxLayout(); row1.setSpacing(8)
        row1.addWidget(QLabel("Giọng:"))
        self.cmb_voice = QComboBox(); self.cmb_voice.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.cmb_voice.setStyleSheet(_combo_style())
        row1.addWidget(self.cmb_voice)

        row1.addWidget(QLabel("  Ngôn ngữ:"))
        self.cmb_lang = QComboBox(); self.cmb_lang.setFixedWidth(110)
        self.cmb_lang.setStyleSheet(_combo_style())
        self.cmb_lang.currentTextChanged.connect(self._filter_voices)
        row1.addWidget(self.cmb_lang)
        layout.addLayout(row1)

        # Rate row
        row2 = QHBoxLayout(); row2.setSpacing(8)
        row2.addWidget(QLabel("Tốc độ:"))
        self.spn_rate = QDoubleSpinBox()
        self.spn_rate.setRange(0.5, 2.0); self.spn_rate.setSingleStep(0.1); self.spn_rate.setValue(1.0)
        self.spn_rate.setDecimals(1); self.spn_rate.setFixedWidth(75)
        self.spn_rate.setStyleSheet(f"QDoubleSpinBox {{ background: {INPUT}; color: {FG}; "
                                     "border: 1px solid #3d4260; border-radius: 4px; padding: 4px; }}")
        row2.addWidget(self.spn_rate)
        lbl_r = QLabel("×  (0.5 – 2.0)"); lbl_r.setStyleSheet(f"color: {FG3};")
        row2.addWidget(lbl_r); row2.addStretch()
        layout.addLayout(row2)

        # Output dir row
        row3 = QHBoxLayout(); row3.setSpacing(6)
        row3.addWidget(QLabel("Lưu vào:"))
        self.inp_out_tts = QLineEdit(self._out_dir)
        self.inp_out_tts.setStyleSheet(_entry_style())
        row3.addWidget(self.inp_out_tts)
        btn_pick = QPushButton("📂")
        btn_pick.setFixedWidth(34)
        btn_pick.setStyleSheet(_btn_style(CARD, FG2, "#3d4260"))
        btn_pick.clicked.connect(lambda: self._pick_dir(self.inp_out_tts))
        row3.addWidget(btn_pick)
        layout.addLayout(row3)

        layout.addWidget(self._sep())
        layout.addWidget(self._sec_label("▶  Thực hiện"))

        # Action buttons
        act = QHBoxLayout(); act.setSpacing(8)
        self.btn_tts = QPushButton("▶  Tạo giọng nói")
        self.btn_tts.setStyleSheet(_btn_style(ACCENT, "white", "#4752c4"))
        self.btn_tts.clicked.connect(self._run_tts)
        act.addWidget(self.btn_tts)

        self.btn_stop_tts = QPushButton("⏹ Dừng")
        self.btn_stop_tts.setStyleSheet(_btn_style("#6a1a1a", "#ff6b6b", "#3a0a0a"))
        self.btn_stop_tts.setEnabled(False)
        self.btn_stop_tts.clicked.connect(self._cancel_task)
        act.addWidget(self.btn_stop_tts)

        self.btn_play = QPushButton("▶️ Nghe lại")
        self.btn_play.setStyleSheet(_btn_style("#1a2a3a", CYAN, "#1e3850"))
        self.btn_play.setEnabled(False)
        self.btn_play.clicked.connect(self._play_last)
        act.addWidget(self.btn_play)

        btn_open = QPushButton("📂 Mở output")
        btn_open.setStyleSheet(_btn_style(CARD, FG2, "#3d4260"))
        btn_open.clicked.connect(lambda: self._open_folder(self.inp_out_tts.text()))
        act.addWidget(btn_open)
        act.addStretch()
        layout.addLayout(act)

        self.pb_tts = QProgressBar()
        self.pb_tts.setTextVisible(False); self.pb_tts.setFixedHeight(6)
        self.pb_tts.setStyleSheet(f"QProgressBar {{ background: {CARD}; border: none; border-radius: 3px; }}"
                                   f"QProgressBar::chunk {{ background: {GREEN}; border-radius: 3px; }}")
        self.pb_tts.setRange(0, 0); self.pb_tts.hide()
        layout.addWidget(self.pb_tts)

        layout.addWidget(self._sec_label("📄  Kết quả"))
        self.txt_tts_result = QTextEdit(); self.txt_tts_result.setReadOnly(True)
        self.txt_tts_result.setFixedHeight(80)
        self.txt_tts_result.setStyleSheet(_textedit_style(GREEN))
        layout.addWidget(self.txt_tts_result)
        layout.addStretch()

    # ─────────────────────────────────────────────────────────────────
    #  TAB: STT
    # ─────────────────────────────────────────────────────────────────
    def _build_stt_tab(self):
        tab = QWidget(); tab.setStyleSheet(f"background: {PANEL};")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)
        self.nb.addTab(tab, "📝  STT")

        layout.addWidget(self._sec_label("📁  File âm thanh / video"))
        row = QHBoxLayout(); row.setSpacing(6)
        self.inp_stt_file = QLineEdit(); self.inp_stt_file.setStyleSheet(_entry_style())
        self.inp_stt_file.setPlaceholderText("Chọn file audio/video…")
        row.addWidget(self.inp_stt_file)
        btn_pick = QPushButton("📂 Chọn file")
        btn_pick.setStyleSheet(_btn_style(CARD, FG2, "#3d4260"))
        btn_pick.clicked.connect(self._pick_stt_file)
        row.addWidget(btn_pick)
        layout.addLayout(row)

        layout.addWidget(self._sep())
        layout.addWidget(self._sec_label("🔧  Cài đặt"))

        r1 = QHBoxLayout(); r1.setSpacing(8)
        r1.addWidget(QLabel("Ngôn ngữ nguồn:"))
        self.cmb_stt_lang = QComboBox()
        self.cmb_stt_lang.addItems(["vi-VN","zh-CN","en-US","ja-JP","ko-KR","fr-FR","de-DE","es-ES"])
        self.cmb_stt_lang.setStyleSheet(_combo_style())
        r1.addWidget(self.cmb_stt_lang); r1.addStretch()
        layout.addLayout(r1)

        r2 = QHBoxLayout(); r2.setSpacing(8)
        r2.addWidget(QLabel("Dịch sang:"))
        self.cmb_trans_lang = QComboBox()
        self.cmb_trans_lang.addItems(["vi-VN","zh-CN","en-US","ja-JP","ko-KR","fr-FR"])
        self.cmb_trans_lang.setStyleSheet(_combo_style())
        r2.addWidget(self.cmb_trans_lang)
        self.chk_trans = QCheckBox("Bật dịch phụ đề")
        self.chk_trans.setStyleSheet(f"color: {FG2};")
        r2.addWidget(self.chk_trans); r2.addStretch()
        layout.addLayout(r2)

        r3 = QHBoxLayout(); r3.setSpacing(6)
        r3.addWidget(QLabel("Lưu vào:"))
        self.inp_out_stt = QLineEdit(self._out_stt); self.inp_out_stt.setStyleSheet(_entry_style())
        r3.addWidget(self.inp_out_stt)
        btn_p = QPushButton("📂"); btn_p.setFixedWidth(34)
        btn_p.setStyleSheet(_btn_style(CARD, FG2, "#3d4260"))
        btn_p.clicked.connect(lambda: self._pick_dir(self.inp_out_stt))
        r3.addWidget(btn_p)
        layout.addLayout(r3)

        layout.addWidget(self._sep())
        act = QHBoxLayout(); act.setSpacing(8)
        self.btn_stt = QPushButton("▶  Nhận dạng giọng nói")
        self.btn_stt.setStyleSheet(_btn_style(ACCENT, "white", "#4752c4"))
        self.btn_stt.clicked.connect(self._run_stt)
        act.addWidget(self.btn_stt)
        btn_open2 = QPushButton("📂 Mở output")
        btn_open2.setStyleSheet(_btn_style(CARD, FG2, "#3d4260"))
        btn_open2.clicked.connect(lambda: self._open_folder(self.inp_out_stt.text()))
        act.addWidget(btn_open2); act.addStretch()
        layout.addLayout(act)

        self.pb_stt = QProgressBar()
        self.pb_stt.setTextVisible(False); self.pb_stt.setFixedHeight(6)
        self.pb_stt.setStyleSheet(f"QProgressBar {{ background: {CARD}; border: none; border-radius: 3px; }}"
                                   f"QProgressBar::chunk {{ background: {GREEN}; border-radius: 3px; }}")
        self.pb_stt.setRange(0, 0); self.pb_stt.hide()
        layout.addWidget(self.pb_stt)

        layout.addWidget(self._sec_label("📄  Kết quả phụ đề"))
        self.txt_stt_result = QTextEdit(); self.txt_stt_result.setReadOnly(True)
        self.txt_stt_result.setStyleSheet(_textedit_style(GREEN))
        layout.addWidget(self.txt_stt_result)

    # ─────────────────────────────────────────────────────────────────
    #  TAB: DEVICE
    # ─────────────────────────────────────────────────────────────────
    def _build_device_tab(self):
        tab = QWidget(); tab.setStyleSheet(f"background: {PANEL};")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)
        self.nb.addTab(tab, "⚙  Thiết bị")

        layout.addWidget(self._sec_label("⚙  Cấu hình Device (tuỳ chọn)"))
        hint = QLabel("Để trống → dùng giá trị mặc định. Hoặc load từ device.json.")
        hint.setStyleSheet(f"color: {FG3}; font-size: 9pt;")
        layout.addWidget(hint)

        self._dev_inputs = {}
        fields = [("Device ID:", "device_id"), ("IID:", "iid"),
                  ("App Version:", "appvr"), ("Region:", "region"), ("Language:", "lan")]
        for lbl, key in fields:
            r = QHBoxLayout(); r.setSpacing(8)
            r.addWidget(QLabel(lbl))
            inp = QLineEdit(); inp.setStyleSheet(_entry_style())
            self._dev_inputs[key] = inp
            r.addWidget(inp)
            layout.addLayout(r)

        layout.addWidget(self._sep())
        layout.addWidget(self._sec_label("📂  Load từ file JSON"))
        r2 = QHBoxLayout(); r2.setSpacing(6)
        self.inp_dev_path = QLineEdit(); self.inp_dev_path.setStyleSheet(_entry_style())
        r2.addWidget(self.inp_dev_path)
        btn_dev = QPushButton("📂 Chọn device.json")
        btn_dev.setStyleSheet(_btn_style(CARD, FG2, "#3d4260"))
        btn_dev.clicked.connect(self._pick_dev_file)
        r2.addWidget(btn_dev)
        layout.addLayout(r2)

        act = QHBoxLayout(); act.setSpacing(8)
        btn_apply = QPushButton("✔  Áp dụng")
        btn_apply.setStyleSheet(_btn_style(ACCENT, "white", "#4752c4"))
        btn_apply.clicked.connect(self._apply_device)
        act.addWidget(btn_apply)
        btn_reset = QPushButton("↺ Reset")
        btn_reset.setStyleSheet(_btn_style(CARD, FG2, "#3d4260"))
        btn_reset.clicked.connect(self._reset_device)
        act.addWidget(btn_reset); act.addStretch()
        layout.addLayout(act)

        self.lbl_dev_info = QLabel("")
        self.lbl_dev_info.setStyleSheet(f"background: {CARD}; color: {CYAN}; "
                                         "font-family: Consolas; font-size: 9pt; padding: 8px; border-radius: 4px;")
        self.lbl_dev_info.setWordWrap(True)
        layout.addWidget(self.lbl_dev_info)
        layout.addStretch()

    # ─────────────────────────────────────────────────────────────────
    #  TAB: VOICES
    # ─────────────────────────────────────────────────────────────────
    def _build_voices_tab(self):
        tab = QWidget(); tab.setStyleSheet(f"background: {PANEL};")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        self.nb.addTab(tab, "🎤  Giọng đọc")

        # toolbar
        bar = QHBoxLayout(); bar.setSpacing(8)
        self.inp_search = QLineEdit(); self.inp_search.setPlaceholderText("🔍 Tìm kiếm…")
        self.inp_search.setFixedWidth(180); self.inp_search.setStyleSheet(_entry_style())
        self.inp_search.textChanged.connect(self._update_tree)
        bar.addWidget(self.inp_search)

        btn_reload = QPushButton("↺ Tải lại")
        btn_reload.setStyleSheet(_btn_style(CARD, FG2, "#3d4260"))
        btn_reload.clicked.connect(self._load_voices)
        bar.addWidget(btn_reload)

        self.btn_preview = QPushButton("🔊 Nghe thử")
        self.btn_preview.setStyleSheet(_btn_style("#1a3a2a", GREEN, "#254030"))
        self.btn_preview.clicked.connect(self._preview_voice)
        bar.addWidget(self.btn_preview)

        self.lbl_count = QLabel("")
        self.lbl_count.setStyleSheet(f"color: {CYAN}; font-weight: bold;")
        bar.addStretch(); bar.addWidget(self.lbl_count)
        layout.addLayout(bar)

        self.lbl_preview_status = QLabel("")
        self.lbl_preview_status.setStyleSheet(f"color: {FG3}; font-size: 9pt;")
        layout.addWidget(self.lbl_preview_status)

        # tree
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["  Tên giọng đọc", "  Voice Type", "Lang"])
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(2, 70)
        self.tree.setStyleSheet(f"""
            QTreeWidget {{ background: {INPUT}; color: {FG2}; border: none;
                          alternate-background-color: {CARD}; }}
            QTreeWidget::item {{ padding: 4px; }}
            QTreeWidget::item:selected {{ background: {ACCENT}; color: white; }}
            QHeaderView::section {{ background: {CARD}; color: {FG}; font-weight: bold;
                                    border: none; padding: 4px; }}
        """)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.itemDoubleClicked.connect(self._use_voice)
        layout.addWidget(self.tree)

        # bottom
        btm = QLabel("💡 Double-click để chọn giọng vào tab TTS")
        btm.setStyleSheet(f"background: {CARD}; color: {FG3}; font-size: 8pt; padding: 4px 8px;")
        layout.addWidget(btm)

    # ─────────────────────────────────────────────────────────────────
    #  TAB: SRT BATCH
    # ─────────────────────────────────────────────────────────────────
    def _build_srt_tab(self):
        tab = QWidget(); tab.setStyleSheet(f"background: {PANEL};")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)
        self.nb.addTab(tab, "📂  SRT (Batch)")

        hint = QLabel("📌 Chọn nhiều file SRT, tick chọn các file cần xử lý, sau đó bấm Tạo Voice.")
        hint.setStyleSheet(f"color: {FG2}; font-size: 9pt;")
        layout.addWidget(hint)

        btn_add = QPushButton("➕ Thêm file SRT")
        btn_add.setStyleSheet(_btn_style(CARD, FG2, "#3d4260"))
        btn_add.clicked.connect(self._add_srt_files)
        layout.addWidget(btn_add)

        # SRT list (QListWidget với checkboxes)
        self.srt_list = QListWidget()
        self.srt_list.setStyleSheet(f"""
            QListWidget {{ background: {INPUT}; color: {FG}; border: none; border-radius: 4px; }}
            QListWidget::item {{ padding: 4px 8px; }}
            QListWidget::item:hover {{ background: {CARD}; }}
        """)
        self.srt_list.setMinimumHeight(120)
        layout.addWidget(self.srt_list)

        # Ctrl buttons
        ctrl = QHBoxLayout(); ctrl.setSpacing(6)
        btn_all = QPushButton("✅ Chọn tất cả")
        btn_all.setStyleSheet(_btn_style(CARD, FG2, "#3d4260"))
        btn_all.clicked.connect(self._select_all_srt)
        ctrl.addWidget(btn_all)
        btn_none = QPushButton("⬜ Bỏ chọn")
        btn_none.setStyleSheet(_btn_style(CARD, FG2, "#3d4260"))
        btn_none.clicked.connect(self._deselect_all_srt)
        ctrl.addWidget(btn_none)
        btn_del = QPushButton("🗑 Xóa đã chọn")
        btn_del.setStyleSheet(_btn_style(CARD, FG2, "#3d4260"))
        btn_del.clicked.connect(self._remove_selected_srt)
        ctrl.addWidget(btn_del); ctrl.addStretch()
        layout.addLayout(ctrl)

        layout.addWidget(self._sep())
        layout.addWidget(self._sec_label("🎛  Cài đặt giọng nói"))

        r1 = QHBoxLayout(); r1.setSpacing(8)
        r1.addWidget(QLabel("Giọng:"))
        self.srt_cmb_voice = QComboBox()
        self.srt_cmb_voice.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.srt_cmb_voice.setStyleSheet(_combo_style())
        r1.addWidget(self.srt_cmb_voice)
        r1.addWidget(QLabel("  Ngôn ngữ:"))
        self.srt_cmb_lang = QComboBox(); self.srt_cmb_lang.setFixedWidth(110)
        self.srt_cmb_lang.setStyleSheet(_combo_style())
        self.srt_cmb_lang.currentTextChanged.connect(self._filter_voices)
        r1.addWidget(self.srt_cmb_lang)
        layout.addLayout(r1)

        r2 = QHBoxLayout(); r2.setSpacing(8)
        r2.addWidget(QLabel("Tốc độ:"))
        self.srt_spn_rate = QDoubleSpinBox()
        self.srt_spn_rate.setRange(0.5, 2.0); self.srt_spn_rate.setSingleStep(0.1)
        self.srt_spn_rate.setValue(1.0); self.srt_spn_rate.setDecimals(1)
        self.srt_spn_rate.setFixedWidth(75)
        self.srt_spn_rate.setStyleSheet(f"QDoubleSpinBox {{ background: {INPUT}; color: {FG}; "
                                         "border: 1px solid #3d4260; border-radius: 4px; padding: 4px; }}")
        r2.addWidget(self.srt_spn_rate)
        lbl_r2 = QLabel("× (0.5 – 2.0)"); lbl_r2.setStyleSheet(f"color: {FG3};")
        r2.addWidget(lbl_r2); r2.addStretch()
        layout.addLayout(r2)

        r3 = QHBoxLayout(); r3.setSpacing(6)
        r3.addWidget(QLabel("Lưu vào:"))
        self.srt_inp_out = QLineEdit(self._out_dir); self.srt_inp_out.setStyleSheet(_entry_style())
        r3.addWidget(self.srt_inp_out)
        btn_p3 = QPushButton("📂"); btn_p3.setFixedWidth(34)
        btn_p3.setStyleSheet(_btn_style(CARD, FG2, "#3d4260"))
        btn_p3.clicked.connect(lambda: self._pick_dir(self.srt_inp_out))
        r3.addWidget(btn_p3)
        layout.addLayout(r3)

        layout.addWidget(self._sep())
        exp_row = QHBoxLayout(); exp_row.setSpacing(12)
        self.chk_split = QCheckBox("Xuất từng file rời")
        self.chk_split.setChecked(True); self.chk_split.setStyleSheet(f"color: {FG2};")
        self.chk_merged = QCheckBox("Gộp thành 1 file dài")
        self.chk_merged.setChecked(True); self.chk_merged.setStyleSheet(f"color: {FG2};")
        exp_row.addWidget(self.chk_split); exp_row.addWidget(self.chk_merged); exp_row.addStretch()
        layout.addLayout(exp_row)

        act = QHBoxLayout(); act.setSpacing(8)
        self.btn_srt = QPushButton("▶  Tạo voice từ SRT (Batch)")
        self.btn_srt.setStyleSheet(_btn_style(ACCENT, "white", "#4752c4"))
        self.btn_srt.clicked.connect(self._run_srt_batch)
        act.addWidget(self.btn_srt)
        self.btn_stop_srt = QPushButton("⏹ Dừng")
        self.btn_stop_srt.setStyleSheet(_btn_style("#6a1a1a", "#ff6b6b", "#3a0a0a"))
        self.btn_stop_srt.setEnabled(False)
        self.btn_stop_srt.clicked.connect(self._cancel_task)
        act.addWidget(self.btn_stop_srt)
        btn_open3 = QPushButton("📂 Mở output")
        btn_open3.setStyleSheet(_btn_style(CARD, FG2, "#3d4260"))
        btn_open3.clicked.connect(lambda: self._open_folder(self.srt_inp_out.text()))
        act.addWidget(btn_open3); act.addStretch()
        layout.addLayout(act)

        self.pb_srt = QProgressBar()
        self.pb_srt.setTextVisible(False); self.pb_srt.setFixedHeight(6)
        self.pb_srt.setStyleSheet(f"QProgressBar {{ background: {CARD}; border: none; border-radius: 3px; }}"
                                   f"QProgressBar::chunk {{ background: {GREEN}; border-radius: 3px; }}")
        self.pb_srt.setRange(0, 0); self.pb_srt.hide()
        layout.addWidget(self.pb_srt)

        layout.addWidget(self._sec_label("📄  Kết quả batch"))
        self.txt_srt_result = QTextEdit(); self.txt_srt_result.setReadOnly(True)
        self.txt_srt_result.setFixedHeight(90)
        self.txt_srt_result.setStyleSheet(_textedit_style(GREEN))
        layout.addWidget(self.txt_srt_result)

    # ─────────────────────────────────────────────────────────────────
    #  WIDGET HELPERS
    # ─────────────────────────────────────────────────────────────────
    def _sec_label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {FG2}; font-weight: bold; font-size: 9pt; "
                           f"border-bottom: 1px solid #3d4260; padding-bottom: 3px;")
        return lbl

    def _sep(self):
        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: #2e3147;"); line.setFixedHeight(1)
        return line

    # ─────────────────────────────────────────────────────────────────
    #  SRT FILE LIST
    # ─────────────────────────────────────────────────────────────────
    def _add_srt_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Chọn file SRT", "", "SRT Files (*.srt)")
        for path in paths:
            if path not in self.srt_loaded:
                self.srt_loaded.add(path)
                item = QListWidgetItem(os.path.basename(path))
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
                self.srt_list.addItem(item)

    def _select_all_srt(self):
        for i in range(self.srt_list.count()):
            self.srt_list.item(i).setCheckState(Qt.CheckState.Checked)

    def _deselect_all_srt(self):
        for i in range(self.srt_list.count()):
            self.srt_list.item(i).setCheckState(Qt.CheckState.Unchecked)

    def _remove_selected_srt(self):
        for i in reversed(range(self.srt_list.count())):
            item = self.srt_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                self.srt_loaded.discard(item.data(Qt.ItemDataRole.UserRole))
                self.srt_list.takeItem(i)

    def _get_checked_srt_paths(self):
        result = []
        for i in range(self.srt_list.count()):
            item = self.srt_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                result.append(item.data(Qt.ItemDataRole.UserRole))
        return result

    # ─────────────────────────────────────────────────────────────────
    #  INIT CLIENT & VOICES
    # ─────────────────────────────────────────────────────────────────
    def _init_client(self):
        if not SDK_OK:
            self._set_status(f"● LỖI SDK: {SDK_ERROR}", RED); return
        try:
            self.client = CapCutClient()
            log("CapCutClient khởi tạo thành công", "OK")
            self._set_status("● Sẵn sàng", GREEN)
            self._load_voices()
        except Exception as ex:
            log(f"Lỗi khởi tạo: {ex}", "ERROR")
            self._set_status("● Lỗi khởi tạo", RED)

    def _load_voices(self):
        if not self.client: return
        try:
            self.voices = self.client.list_voices()
            langs = sorted({v.lang for v in self.voices if v.lang})
            lang_items = ["Tất cả"] + langs

            self.cmb_lang.blockSignals(True)
            self.cmb_lang.clear(); self.cmb_lang.addItems(lang_items)
            self.cmb_lang.blockSignals(False)

            self.srt_cmb_lang.blockSignals(True)
            self.srt_cmb_lang.clear(); self.srt_cmb_lang.addItems(lang_items)
            self.srt_cmb_lang.blockSignals(False)

            self._update_tree()
            self._update_voice_combos()
            log(f"Tải {len(self.voices)} giọng đọc thành công", "OK")
        except Exception as ex:
            log(f"Lỗi tải giọng: {ex}", "ERROR")

    def _filter_voices(self):
        # Sync the two lang filters
        sender = self.sender()
        if sender == self.cmb_lang:
            self.srt_cmb_lang.blockSignals(True)
            self.srt_cmb_lang.setCurrentText(self.cmb_lang.currentText())
            self.srt_cmb_lang.blockSignals(False)
        elif sender == self.srt_cmb_lang:
            self.cmb_lang.blockSignals(True)
            self.cmb_lang.setCurrentText(self.srt_cmb_lang.currentText())
            self.cmb_lang.blockSignals(False)
        self._update_tree()
        self._update_voice_combos()

    @staticmethod
    def _is_neural(voice_type: str) -> bool:
        return "Neural" in voice_type

    def _update_tree(self):
        kw = self.inp_search.text().lower()
        lng = self.cmb_lang.currentText()
        self.tree.clear()
        self._fvoices = []
        for v in self.voices:
            if lng and lng != "Tất cả" and v.lang.lower() != lng.lower(): continue
            if kw and kw not in v.display_name.lower() and kw not in v.voice_type.lower(): continue
            self._fvoices.append(v)
        for v in self._fvoices:
            is_neural = self._is_neural(v.voice_type)
            name_show = f"⚡ {v.display_name} [edge-tts]" if is_neural else v.display_name
            item = QTreeWidgetItem([name_show, v.voice_type, v.lang])
            if is_neural:
                for col in range(3):
                    item.setForeground(col, QColor("#40c8f0"))
            self.tree.addTopLevelItem(item)
        self.lbl_count.setText(f"{len(self._fvoices)} giọng")

    def _update_voice_combos(self):
        lng = self.cmb_lang.currentText()
        seen, items = set(), []
        for v in self.voices:
            if lng and lng != "Tất cả" and v.lang.lower() != lng.lower(): continue
            if v.voice_type not in seen:
                seen.add(v.voice_type)
                items.append(f"{v.display_name}  [{v.voice_type}]")
        cur = self.cmb_voice.currentText()
        self.cmb_voice.blockSignals(True)
        self.cmb_voice.clear(); self.cmb_voice.addItems(items)
        if cur in items: self.cmb_voice.setCurrentText(cur)
        self.cmb_voice.blockSignals(False)

        self.srt_cmb_voice.blockSignals(True)
        self.srt_cmb_voice.clear(); self.srt_cmb_voice.addItems(items)
        self.srt_cmb_voice.blockSignals(False)

    def _use_voice(self, item, _col=None):
        idx = self.tree.indexOfTopLevelItem(item)
        if 0 <= idx < len(self._fvoices):
            v = self._fvoices[idx]
            target = f"{v.display_name}  [{v.voice_type}]"
            self.cmb_voice.setCurrentText(target)
            log(f"Đã chọn giọng → TTS: {v.display_name} ({v.voice_type})", "OK")

    # ─────────────────────────────────────────────────────────────────
    #  PREVIEW VOICE
    # ─────────────────────────────────────────────────────────────────
    def _preview_voice(self):
        if not self.client:
            QMessageBox.critical(self, "Lỗi", "Client chưa khởi tạo!"); return
        sel = self.tree.selectedItems()
        if not sel:
            QMessageBox.information(self, "Thông báo", "Hãy chọn một giọng đọc trước!"); return
        idx = self.tree.indexOfTopLevelItem(sel[0])
        if not (0 <= idx < len(self._fvoices)): return
        v = self._fvoices[idx]
        self.lbl_preview_status.setText(f"⏳ Đang tạo mẫu: {v.display_name}…")
        self.lbl_preview_status.setStyleSheet(f"color: {YELLOW}; font-size: 9pt;")
        self.btn_preview.setEnabled(False); self.btn_preview.setText("⏳ Đang xử lý…")
        threading.Thread(target=self._preview_worker, args=(v.voice_type, v.display_name), daemon=True).start()

    def _preview_worker(self, voice_type, display_name):
        import urllib.request
        sample = f"Xin chao, day la giong {display_name}. Chuc ban mot ngay tot lanh!"
        try:
            if self._is_neural(voice_type):
                tmp_path = self._edge_tts_generate(sample, voice_type, "1.0")
            else:
                url = self._tts_get_url(sample, voice_type, "1.0", "Preview")
                tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, prefix="capcut_preview_")
                tmp.close()
                urllib.request.urlretrieve(url, tmp.name)
                tmp_path = tmp.name
            os.startfile(tmp_path)
            self._schedule_ui(lambda: (
                self.lbl_preview_status.setText(f"✔ Đang phát: {display_name}"),
                self.lbl_preview_status.setStyleSheet(f"color: {GREEN}; font-size: 9pt;"),
            ))
        except Exception as ex:
            log(f"Lỗi preview: {ex}", "ERROR")
            self._schedule_ui(lambda: self.lbl_preview_status.setText(f"✖ Lỗi: {ex}"))
        finally:
            self._schedule_ui(lambda: (
                self.btn_preview.setEnabled(True),
                self.btn_preview.setText("🔊 Nghe thử"),
            ))

    # ─────────────────────────────────────────────────────────────────
    #  CANCEL & BUSY STATE
    # ─────────────────────────────────────────────────────────────────
    def _cancel_task(self):
        if not self.is_running:
            log("Không có tiến trình nào đang chạy.", "WARN"); return
        self.stop_flag = True
        log("🛑 Đang cố gắng dừng tác vụ…", "WARN")
        self._schedule_ui(lambda: (
            self.btn_stop_tts.setEnabled(False),
            self.btn_stop_srt.setEnabled(False),
        ))

    def _set_busy(self, busy, mode="tts"):
        self.busy = busy
        self.is_running = busy
        self.current_task_type = mode if busy else None

        def _update():
            if mode == "tts":
                self.btn_tts.setEnabled(not busy)
                self.pb_tts.setVisible(busy)
            elif mode == "srt":
                self.btn_srt.setEnabled(not busy)
                self.pb_srt.setVisible(busy)
            elif mode == "stt":
                self.btn_stt.setEnabled(not busy)
                self.pb_stt.setVisible(busy)

            self.btn_stop_tts.setEnabled(busy and mode in ("tts", "srt"))
            self.btn_stop_srt.setEnabled(busy and mode in ("tts", "srt"))

            if busy:
                self.btn_stop_tts.setText("⏹ Dừng")
                self.btn_stop_srt.setText("⏹ Dừng")
                self._set_status(f"● Đang xử lý {mode.upper()}…", YELLOW)
            else:
                self.btn_stop_tts.setEnabled(False)
                self.btn_stop_srt.setEnabled(False)
                self._set_status("● Sẵn sàng", GREEN)
                self._check_queue()

        self._schedule_ui(_update)

    def _check_queue(self):
        if not self.task_queue.empty():
            task_type, args = self.task_queue.get()
            log(f"⏳ Bắt đầu lệnh {task_type.upper()} từ hàng đợi.", "STEP")
            if task_type == "tts":
                self._do_run_tts(*args)
            elif task_type == "srt":
                self._do_run_srt_batch(*args)

    # ─────────────────────────────────────────────────────────────────
    #  TTS WORKER
    # ─────────────────────────────────────────────────────────────────
    def _get_voice_type(self, combo: QComboBox):
        sel = combo.currentText()
        if "[" in sel and sel.endswith("]"):
            return sel[sel.rfind("[")+1:-1]
        return "BV074_streaming"

    def _get_rate(self, spinbox: QDoubleSpinBox):
        v = spinbox.value()
        v = max(0.5, min(2.0, v))
        return f"{v:.1f}"

    def _run_tts(self):
        if self.busy:
            if self.current_task_type == "srt":
                text = self.txt_input.toPlainText().strip()
                voice_type = self._get_voice_type(self.cmb_voice)
                rate = self._get_rate(self.spn_rate)
                outd = self.inp_out_tts.text()
                self.task_queue.put(("tts", (text, voice_type, rate, outd)))
                log("⏳ Đã thêm TTS vào hàng đợi.", "WARN")
            else:
                QMessageBox.warning(self, "Đang bận", f"Đang xử lý {self.current_task_type}!")
            return

        text = self.txt_input.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Thiếu văn bản", "Vui lòng nhập văn bản cần đọc!"); return
        if not self.client:
            QMessageBox.critical(self, "Lỗi", "Client chưa khởi tạo!"); return

        voice_type = self._get_voice_type(self.cmb_voice)
        rate = self._get_rate(self.spn_rate)
        outd = self.inp_out_tts.text()
        self._do_run_tts(text, voice_type, rate, outd)

    def _do_run_tts(self, text, voice_type, rate, out_dir):
        self.stop_flag = False
        self.current_task_type = "tts"
        self._set_busy(True, "tts")
        threading.Thread(target=self._tts_worker, args=(text, voice_type, rate, out_dir), daemon=True).start()

    def _tts_worker(self, text, voice_type, rate, out_dir):
        import urllib.request
        try:
            use_edge = self._is_neural(voice_type)
            engine = "edge-tts" if use_edge else "CapCut"
            log(f"=== TTS bắt đầu | giọng: {voice_type} | engine: {engine} | rate: {rate}x ===", "STEP")
            segments = [s.strip() for s in text.split("\n") if s.strip()] or [text]
            os.makedirs(out_dir, exist_ok=True)
            saved = []
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            for i, seg in enumerate(segments):
                if self.stop_flag:
                    log("🛑 Người dùng đã dừng TTS.", "WARN")
                    while not self.task_queue.empty(): self.task_queue.get()
                    break
                log(f"Đoạn {i+1}/{len(segments)}: {seg[:50]}…", "INFO")
                out_f = os.path.join(out_dir, f"tts_{stamp}_{i+1:02d}.mp3")
                if use_edge:
                    tmp = self._edge_tts_generate(seg, voice_type, rate)
                    shutil.move(tmp, out_f)
                else:
                    url = self._tts_get_url(seg, voice_type, rate, f"TTS[{i+1}]")
                    urllib.request.urlretrieve(url, out_f)
                saved.append(out_f)
                log(f"Lưu: {os.path.basename(out_f)}", "OK")

            if saved and not self.stop_flag:
                txt = f"Đã tạo {len(saved)} file ({engine}):\n" + "\n".join(saved)
                self._last_tts_file = saved[-1]
                log(f"=== TTS hoàn tất! {len(saved)} file. ===", "OK")
                self._schedule_ui(lambda: (
                    self.txt_tts_result.setPlainText(txt),
                    self.btn_play.setEnabled(True),
                ))
            elif self.stop_flag:
                self._schedule_ui(lambda: self.txt_tts_result.setPlainText(f"🛑 Đã dừng bởi người dùng!"))
        except Exception as ex:
            log(f"Lỗi TTS: {ex}", "ERROR")
            self._schedule_ui(lambda: self.txt_tts_result.setPlainText(f"Lỗi: {ex}"))
        finally:
            self.stop_flag = False
            self._set_busy(False, "tts")

    # ─────────────────────────────────────────────────────────────────
    #  SRT BATCH WORKER
    # ─────────────────────────────────────────────────────────────────
    def _run_srt_batch(self):
        if self.busy:
            if self.current_task_type == "tts":
                selected = self._get_checked_srt_paths()
                voice_type = self._get_voice_type(self.srt_cmb_voice)
                rate = self._get_rate(self.srt_spn_rate)
                out_root = self.srt_inp_out.text()
                export_split = self.chk_split.isChecked()
                export_merged = self.chk_merged.isChecked()
                self.task_queue.put(("srt", (selected, voice_type, rate, out_root, export_split, export_merged)))
                log("⏳ Đã thêm SRT Batch vào hàng đợi.", "WARN")
            else:
                QMessageBox.warning(self, "Đang bận", f"Đang xử lý {self.current_task_type}!")
            return

        selected = self._get_checked_srt_paths()
        if not selected:
            QMessageBox.warning(self, "Cảnh báo", "Bạn chưa tick chọn file SRT nào!"); return
        if not self.chk_split.isChecked() and not self.chk_merged.isChecked():
            QMessageBox.warning(self, "Cảnh báo", "Bạn chưa chọn kiểu xuất file nào!"); return

        voice_type = self._get_voice_type(self.srt_cmb_voice)
        rate = self._get_rate(self.srt_spn_rate)
        out_root = self.srt_inp_out.text()
        export_split = self.chk_split.isChecked()
        export_merged = self.chk_merged.isChecked()
        self._do_run_srt_batch(selected, voice_type, rate, out_root, export_split, export_merged)

    def _do_run_srt_batch(self, selected, voice_type, rate, out_root, export_split, export_merged):
        self.stop_flag = False
        self.current_task_type = "srt"
        self._set_busy(True, "srt")
        threading.Thread(target=self._srt_batch_worker,
                         args=(selected, voice_type, rate, out_root, export_split, export_merged),
                         daemon=True).start()

    def _srt_batch_worker(self, srt_paths, voice_type, rate, out_root, export_split, export_merged):
        try:
            import pysrt, urllib.request
            from pydub import AudioSegment

            use_edge = self._is_neural(voice_type)
            total_srt = len(srt_paths)
            final_log = ""
            base_name = ""

            for idx, srt_path in enumerate(srt_paths):
                if self.stop_flag:
                    log("🛑 Người dùng đã dừng Batch SRT.", "WARN")
                    while not self.task_queue.empty(): self.task_queue.get()
                    break

                base_name = os.path.splitext(os.path.basename(srt_path))[0]
                out_dir = os.path.join(out_root, base_name)
                os.makedirs(out_dir, exist_ok=True)
                log(f"=== [{idx+1}/{total_srt}] BẮT ĐẦU: {base_name} ===", "STEP")

                subs = pysrt.open(srt_path, encoding='utf-8')
                if not subs:
                    log(f"Bỏ qua {base_name}: SRT rỗng.", "WARN"); continue

                temp_dir = tempfile.mkdtemp(prefix=f"capcut_{base_name}_")
                for i, sub in enumerate(subs):
                    if self.stop_flag:
                        log(f"🛑 Dừng đột ngột tại câu {i+1} của {base_name}.", "WARN"); break
                    text = sub.text.replace('\n', ' ').strip()
                    if not text: continue
                    temp_audio = os.path.join(temp_dir, f"seg_{i:04d}.mp3")
                    # BƯỚC 1: tạo/tải audio TTS (bắt buộc phải có tiếng)
                    try:
                        if use_edge:
                            tmp = self._edge_tts_generate(text, voice_type, rate)
                            shutil.move(tmp, temp_audio)
                        else:
                            url = self._tts_get_url(text, voice_type, rate, f"Batch[{base_name}][{i+1}]")
                            urllib.request.urlretrieve(url, temp_audio)
                    except Exception as seg_e:
                        log(f"Lỗi TẠO audio dòng {i+1} {base_name}: {seg_e}", "ERROR")
                        continue
                    # BƯỚC 2: chỉnh độ dài (nếu lỗi vẫn GIỮ audio gốc, không mất tiếng)
                    try:
                        self._adjust_audio_to_target(temp_audio, sub.duration.ordinal)
                    except Exception as adj_e:
                        log(f"Bỏ qua chỉnh độ dài dòng {i+1}: {adj_e}", "WARN")

                if not self.stop_flag:
                    if export_split:
                        count_split = 0
                        for i, sub in enumerate(subs):
                            src = os.path.join(temp_dir, f"seg_{i:04d}.mp3")
                            if os.path.exists(src):
                                dst = os.path.join(out_dir, f"voice_{base_name}_{i+1:04d}.mp3")
                                shutil.copy2(src, dst); count_split += 1
                        final_log += f"- {base_name}: Xuất {count_split} file rời.\n"
                        log(f"✔ File rời đã lưu: {out_dir}", "OK")

                    if export_merged:
                        combined = AudioSegment.empty(); cur_ms = 0
                        for i, sub in enumerate(subs):
                            if not sub.text.strip(): continue
                            src = os.path.join(temp_dir, f"seg_{i:04d}.mp3")
                            if not os.path.exists(src): continue
                            start_ms = sub.start.ordinal
                            if start_ms > cur_ms:
                                combined += AudioSegment.silent(duration=start_ms - cur_ms)
                                cur_ms = start_ms
                            seg = AudioSegment.from_mp3(src)
                            combined += seg; cur_ms += len(seg)
                        merged = os.path.join(out_dir, f"tong_hop_voice_{base_name}.mp3")
                        combined.export(merged, format="mp3")
                        final_log += f"- {base_name}: Xuất 1 file gộp.\n"
                        log(f"✔ File gộp: {os.path.basename(merged)}", "OK")

                try: shutil.rmtree(temp_dir)
                except: pass

            if not self.stop_flag:
                log(f"=== BATCH HOÀN TẤT {total_srt} FILE! ===", "OK")
                self._last_tts_file = out_root
                self._schedule_ui(lambda: (
                    self.txt_srt_result.setPlainText(f"✅ BATCH SRT HOÀN TẤT!\n{final_log}\nOutput: {out_root}"),
                    self.btn_play.setEnabled(True),
                ))
            else:
                self._schedule_ui(lambda: self.txt_srt_result.setPlainText(
                    f"🛑 Đã bị dừng bởi người dùng!\nDừng tại: {base_name}"))
        except Exception as ex:
            log(f"Lỗi Batch: {ex}", "ERROR")
            self._schedule_ui(lambda: self.txt_srt_result.setPlainText(f"❌ Lỗi: {ex}"))
        finally:
            self.stop_flag = False
            self._set_busy(False, "srt")

    def _adjust_audio_to_target(self, audio_path, target_ms):
        """Chỉnh độ dài audio cho khớp thời lượng phụ đề.
        Dùng ffmpeg TRỰC TIẾP (không qua pydub) để khỏi phụ thuộc ffprobe.
        Nếu lỗi -> GIỮ NGUYÊN file gốc (vẫn có tiếng), không để mất audio."""
        try:
            if not FFMPEG_PATH or not os.path.exists(audio_path):
                return  # không có ffmpeg -> giữ file gốc, đừng động vào
            # Đọc độ dài hiện tại bằng pydub nếu được; không thì bỏ qua chỉnh
            cur = None
            try:
                from pydub import AudioSegment
                cur = len(AudioSegment.from_file(audio_path))
            except Exception:
                cur = None
            if cur is None:
                return  # không đo được -> giữ nguyên, vẫn có tiếng
            if abs(cur - target_ms) < 150:
                return

            out_tmp = audio_path + ".adj.mp3"
            if cur > target_ms:
                # audio dài hơn -> tăng tốc bằng atempo (giới hạn 2.5x)
                factor = min(cur / target_ms, 2.5)
                # atempo chỉ nhận 0.5..2.0 -> ghép nhiều bước nếu >2
                filters = []
                f = factor
                while f > 2.0:
                    filters.append("atempo=2.0"); f /= 2.0
                filters.append(f"atempo={f:.4f}")
                af = ",".join(filters)
                cmd = [FFMPEG_PATH, "-y", "-i", audio_path, "-filter:a", af, "-c:a", "libmp3lame", out_tmp]
            else:
                # audio ngắn hơn -> thêm im lặng vào cuối
                pad_sec = (target_ms - cur) / 1000.0
                cmd = [FFMPEG_PATH, "-y", "-i", audio_path, "-af", f"apad=pad_dur={pad_sec:.3f}", "-c:a", "libmp3lame", out_tmp]

            flags = 0
            if os.name == "nt":
                flags = 0x08000000  # CREATE_NO_WINDOW
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
            if r.returncode == 0 and os.path.exists(out_tmp):
                shutil.move(out_tmp, audio_path)
            else:
                try:
                    if os.path.exists(out_tmp): os.remove(out_tmp)
                except Exception:
                    pass
        except Exception as e:
            log(f"Bỏ qua chỉnh duration (giữ file gốc): {e}", "WARN")

    # ─────────────────────────────────────────────────────────────────
    #  STT WORKER
    # ─────────────────────────────────────────────────────────────────
    def _run_stt(self):
        if self.busy: return
        fp = self.inp_stt_file.text().strip()
        if not fp or not os.path.exists(fp):
            QMessageBox.warning(self, "Thiếu file", "Chọn file âm thanh/video hợp lệ!"); return
        if not self.client:
            QMessageBox.critical(self, "Lỗi", "Client chưa khởi tạo!"); return
        self._set_busy(True, "stt")
        threading.Thread(target=self._stt_worker,
                         args=(fp, self.cmb_stt_lang.currentText(),
                               self.cmb_trans_lang.currentText(),
                               self.chk_trans.isChecked(),
                               self.inp_out_stt.text()),
                         daemon=True).start()

    def _stt_worker(self, fp, lang, trans_lang, use_trans, out_dir):
        try:
            log(f"=== STT bắt đầu | file: {os.path.basename(fp)} ===", "STEP")
            upload = self.client.upload_audio(fp)
            log(f"Upload xong! vid={upload.vid} | duration={upload.duration_ms}ms", "OK")

            stt_res = self.client.create_stt_task(
                audio_vid=upload.vid, audio_md5=upload.md5,
                duration_ms=upload.duration_ms or 10000,
                language=lang, translation_language=trans_lang, use_translation=use_trans)

            tasks = (stt_res.get("data") or {}).get("tasks") or []
            if not tasks: raise RuntimeError(f"Không có STT task: {stt_res}")
            task_id = tasks[0]["id"]; token = tasks[0]["token"]
            log(f"STT task_id={task_id}", "INFO")

            SUCCEED = {"succeed","success","completed","done"}
            FAIL = {"failed","error","fail"}
            result = None; status = ""
            for attempt in range(90):
                time.sleep(2)
                q = self.client.query_stt_task(task_id, token)
                qtasks = (q.get("data") or {}).get("tasks") or []
                if not qtasks: continue
                status = qtasks[0].get("status", "")
                log(f"STT Poll {attempt+1} | status={status!r}", "INFO")
                if status in SUCCEED: result = q; break
                elif status in FAIL: raise RuntimeError(f"STT thất bại (status={status!r})")

            if result is None: raise RuntimeError(f"STT Timeout. Status cuối: {status!r}")

            subs = self.client.extract_subtitles(result)
            lines = [f"Văn bản đầy đủ:\n{subs.full_text}\n", "-"*50,
                     f"\nChi tiết {len(subs.utterances)} đoạn:\n"]
            for u in subs.utterances:
                lines.append(f"[{u.start_time/1000:.2f}s -> {u.end_time/1000:.2f}s]  {u.text}")
            out_text = "\n".join(lines)

            os.makedirs(out_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = os.path.splitext(os.path.basename(fp))[0]
            f_txt = os.path.join(out_dir, f"stt_{base}_{stamp}.txt")
            open(f_txt, "w", encoding="utf-8").write(out_text)
            log(f"=== STT hoàn tất! {len(subs.utterances)} đoạn phụ đề. ===", "OK")
            self._schedule_ui(lambda: self.txt_stt_result.setPlainText(out_text))
        except Exception as ex:
            log(f"Lỗi STT: {ex}", "ERROR")
            self._schedule_ui(lambda: self.txt_stt_result.setPlainText(f"Lỗi: {ex}"))
        finally:
            self._set_busy(False, "stt")

    # ─────────────────────────────────────────────────────────────────
    #  DEVICE
    # ─────────────────────────────────────────────────────────────────
    def _apply_device(self):
        try:
            fp = self.inp_dev_path.text().strip()
            if fp and os.path.exists(fp):
                self.client = CapCutClient(device=fp)
                log(f"Load device từ file: {fp}", "OK")
            else:
                overrides = {k: v.text() for k, v in self._dev_inputs.items() if v.text().strip()}
                self.client = CapCutClient(device=overrides if overrides else None)
                log("Áp dụng device " + ("tùy chỉnh" if overrides else "mặc định"), "OK")
            d = self.client.device.to_dict()
            info = (f"device_id : {d.get('device_id','')}\n"
                    f"iid       : {d.get('iid','')}\n"
                    f"appvr     : {d.get('appvr','')}\n"
                    f"region    : {d.get('region','')}\n"
                    f"lan       : {d.get('lan','')}")
            self.lbl_dev_info.setText(info)
            self._set_status("● Sẵn sàng", GREEN)
            self._load_voices()
        except Exception as ex:
            log(f"Lỗi áp dụng device: {ex}", "ERROR")
            QMessageBox.critical(self, "Lỗi", str(ex))

    def _reset_device(self):
        for inp in self._dev_inputs.values(): inp.clear()
        self.inp_dev_path.clear()
        self.client = CapCutClient()
        self.lbl_dev_info.setText("")
        log("Reset device về mặc định", "OK")

    # ─────────────────────────────────────────────────────────────────
    #  TTS ENGINE HELPERS
    # ─────────────────────────────────────────────────────────────────
    def _edge_tts_generate(self, text: str, voice_type: str, rate: str = "1.0") -> str:
        import asyncio, edge_tts
        try: r = float(rate)
        except: r = 1.0
        pct = int((r - 1.0) * 100)
        rate_str = f"+{pct}%" if pct >= 0 else f"{pct}%"
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, prefix="edge_tts_")
        tmp.close()
        async def _run():
            comm = edge_tts.Communicate(text=text, voice=voice_type, rate=rate_str)
            await comm.save(tmp.name)
        log(f"edge-tts: voice={voice_type} | rate={rate_str}", "INFO")
        asyncio.run(_run())
        log(f"edge-tts: Xong! → {os.path.basename(tmp.name)}", "OK")
        return tmp.name

    def _tts_get_url(self, text, voice_type, rate, label="TTS"):
        import urllib.request
        log(f"{label}: Gửi request…", "INFO")
        create = self.client.create_tts_task(texts=text, voice=voice_type, rate=rate)
        tasks = (create.get("data") or {}).get("tasks") or []
        if not tasks: raise RuntimeError(f"{label}: API không trả về task. Response: {create}")
        task_id = tasks[0]["id"]; token = tasks[0]["token"]
        log(f"{label}: task_id={task_id}", "INFO")

        SUCCEED = {"succeed","success","completed","done","finish"}
        FAIL = {"failed","error","fail"}
        for attempt in range(60):
            time.sleep(2)
            q = self.client.query_tts_task(task_id, token)
            qtasks = (q.get("data") or {}).get("tasks") or []
            if not qtasks: continue
            status = qtasks[0].get("status","")
            prog = qtasks[0].get("progress", 0)
            log(f"{label}: Poll {attempt+1} | status={status!r} | progress={prog}%", "INFO")
            if status in SUCCEED:
                raw = qtasks[0].get("payload","{}")
                payload = json.loads(raw) if isinstance(raw, str) else raw
                subs = payload.get("audio_subtitles") or []
                if subs:
                    url = subs[0].get("speech_url","")
                    if url: return url
                for key in ("audio_list","url_list"):
                    for u in (payload.get(key) or []):
                        url = (u.get("url") or u.get("audio_url") or u.get("speech_url")) if isinstance(u,dict) else str(u)
                        if url: return url
                raise RuntimeError(f"{label}: Task succeed nhưng không tìm thấy URL.")
            elif status in FAIL:
                raise RuntimeError(f"{label}: Task thất bại (status={status!r})")
        raise RuntimeError(f"{label}: Timeout 120s. Status cuối: {status!r}")

    # ─────────────────────────────────────────────────────────────────
    #  FILE PICKERS
    # ─────────────────────────────────────────────────────────────────
    def _pick_dir(self, inp: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "Chọn thư mục")
        if d: inp.setText(d)

    def _pick_stt_file(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "Chọn file âm thanh/video", "",
            "Media (*.mp3 *.mp4 *.m4a *.wav *.ogg *.flac *.aac);;All (*.*)")
        if p: self.inp_stt_file.setText(p); log(f"File: {os.path.basename(p)}", "INFO")

    def _pick_dev_file(self):
        p, _ = QFileDialog.getOpenFileName(self, "Chọn device.json", "", "JSON (*.json);;All (*.*)")
        if p: self.inp_dev_path.setText(p)

    def _play_last(self):
        if not self._last_tts_file or not os.path.exists(self._last_tts_file):
            QMessageBox.information(self, "Thông báo", "Chưa có file nào được tạo!"); return
        os.startfile(self._last_tts_file)

    def _open_folder(self, path):
        os.makedirs(path, exist_ok=True)
        os.startfile(path)

    # ─────────────────────────────────────────────────────────────────
    #  STATUS & LOG
    # ─────────────────────────────────────────────────────────────────
    def _set_status(self, text, color=FG):
        self._schedule_ui(lambda: (
            self.lbl_status.setText(text),
            self.lbl_status.setStyleSheet(f"background: {CARD}; color: {color}; "
                                           "font-weight: bold; padding: 0 10px;"),
        ))

    def _poll_log(self):
        try:
            while True:
                level, msg = _log_q.get_nowait()
                colors = {"INFO": "#7ec8e3", "OK": GREEN, "WARN": YELLOW,
                          "ERROR": RED, "STEP": "#c4a0ff"}
                color = colors.get(level, FG2)
                self.log_box.moveCursor(QTextCursor.MoveOperation.End)
                fmt = QTextCharFormat(); fmt.setForeground(QColor(color))
                cursor = self.log_box.textCursor()
                cursor.insertText(msg + "\n", fmt)
                self.log_box.setTextCursor(cursor)
                self.log_box.ensureCursorVisible()
        except queue.Empty:
            pass

    def _clear_log(self):
        self.log_box.clear()
