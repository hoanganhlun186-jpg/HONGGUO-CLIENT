# -*- coding: utf-8 -*-
"""
render_dub_feature.py
─────────────────────
Gắn thêm 1 tab con "🔤 Tách sub → Dịch → Lồng tiếng" vào RenderWidget.

Toàn bộ phần nặng (tách sub / dịch / lồng tiếng) KHÔNG viết lại — chỉ import
lại các QThread đã có sẵn trong honggou_tab.py:
    - SttBatchThread          : tách phụ đề (CapCut STT)
    - DubThread               : lồng tiếng (CapCut TTS / Edge TTS)
    - GeminiTranslateThread   : dịch bằng Gemini (trình duyệt, free)
    - DeepSeekTranslateThread : dịch bằng DeepSeek (API key)
    - EDGE_TTS_VOICES         : bảng giọng Edge TTS + pitch

Nguồn video/sub lấy từ RenderWidget.cards (mỗi card có .video_path / .srt_path),
đúng những cặp đang nằm trong "Hàng đợi Render", nên khách không phải chọn lại.

CÁCH DÙNG (chỉ thêm 1 dòng trong RenderWidget.__init__, sau khi self.tabs đã tạo
và addTab cho tab_design / tab_thumb):

    from render_dub_feature import attach_dub_tab
    attach_dub_tab(self)

Nếu thiếu honggou_tab hoặc các module phụ, hàm attach_dub_tab sẽ báo lỗi nhẹ
trong tab con và app vẫn chạy bình thường (không crash).
"""

import os
import subprocess

# Tiện ích ffmpeg dùng chung (để tạo _dubbed cho tập không thoại). Ưu tiên
# shared_utils như các module khác; không có thì fallback.
try:
    from shared_utils import get_ffmpeg_path, CREATE_NO_WINDOW
except Exception:
    import shutil as _shutil
    CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
    def get_ffmpeg_path():
        return _shutil.which("ffmpeg") or "ffmpeg"

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QComboBox,
    QCheckBox, QSpinBox, QDoubleSpinBox, QTextEdit, QMessageBox, QLineEdit,
    QScrollArea, QFrame, QDialog, QListView
)
from PyQt6.QtCore import Qt, QSettings, QTimer

# ── Whisper STT (offline, cho VIDEO DÀI) — import mềm, thiếu không sập app ────
try:
    from whisper_stt import WhisperSttThread, _HAS_FW as _WHISPER_AVAILABLE
except Exception:
    WhisperSttThread = None
    _WHISPER_AVAILABLE = False

# ── Import lại toàn bộ "động cơ" từ honggou_tab (không viết lại) ──────────────
_ENGINE_OK = True
_ENGINE_ERR = ""
try:
    from honggou_tab import (
        SttBatchThread, DubThread, EDGE_TTS_VOICES,
        GeminiTranslateThread, DeepSeekTranslateThread,
        PROMPT_PRESETS, AUTH_FILE, _GEMINI_AVAILABLE,
        GoogleManualLoginThread,
    )
except Exception as e:  # noqa
    _ENGINE_OK = False
    _ENGINE_ERR = str(e)
    SttBatchThread = DubThread = None
    GeminiTranslateThread = DeepSeekTranslateThread = None
    GoogleManualLoginThread = None
    EDGE_TTS_VOICES = {}
    PROMPT_PRESETS = {}
    AUTH_FILE = "gemini_auth.json"
    _GEMINI_AVAILABLE = False


CUSTOM_PROMPT_KEY = "✏️ Tự nhập prompt"


def _load_dub_voices():
    """Nạp danh sách giọng: Voice.json (CapCut) + Edge TTS. Giống hệt
    honggou_tab._load_dub_voices nhưng đứng độc lập."""
    import json
    voices = []
    try:
        vpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Voice.json")
        with open(vpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        for v in data:
            if v.get("lan") == "vi" or v.get("lang") == "vi-VN":
                name = v.get("display_name") or v.get("voice_type")
                vt = v.get("voice_type")
                if vt:
                    voices.append(f"{name} [{vt}]")
    except Exception:
        pass
    if not voices:
        voices = [
            "Cô Gái Hoạt Ngôn [BV074_streaming]",
            "Giọng Bé [BV074_streaming_dsp]",
            "Nhỏ Ngọt Ngào [BV421_vivn_streaming]",
            "Thanh Niên Tự Tin [BV075_streaming]",
        ]
    edge_items = [f"🌐 {label} [{vid}]" for label, (vid, _p, _r) in EDGE_TTS_VOICES.items()]
    return voices + edge_items


class DubFeatureWidget(QWidget):
    """Tab con gắn vào RenderWidget. Lấy danh sách file từ host.cards."""

    def __init__(self, host):
        super().__init__()
        self.host = host                      # RenderWidget
        self.settings = QSettings("HongguoDownloader", "RenderDubTab")
        self._stt_thread = None
        self._dub_thread = None
        self._gtrans_thread = None
        self._gemini_login_thread = None
        # trạng thái pipeline dịch→lồng tiếng cuốn chiếu
        self._dub_queue = []
        self._dub_running = False
        self._auto_dub_on = False
        # ── Kiểm tra + retry trước khi render ──
        self._MAX_VERIFY_RETRY = 2        # số vòng làm lại tối đa cho tập lỗi
        self._verify_round = 0            # đã làm lại mấy vòng
        self._translate_failed = set()    # video_path dịch fail (để log)
        self._skip_from_render = set()    # video_path bỏ hẳn khỏi render (fail hết retry)
        self._bad_found = []              # kết quả nút '🔎 Tìm file lỗi' [(video, lý do)]
        self._build_ui()

    # ────────────────────────────────────────────────────────────────────
    #  GIAO DIỆN
    # ────────────────────────────────────────────────────────────────────
    def _style_num(self, spin):
        """Ép nền + viền cho ô số (QSpinBox/QDoubleSpinBox) để nhìn rõ là ô
        nhập được, và có nút tăng/giảm."""
        spin.setMinimumWidth(64)
        spin.setStyleSheet(
            "QSpinBox, QDoubleSpinBox { background:#11121A; border:1px solid #3B3E4D; "
            "border-radius:4px; padding:4px 6px; color:#FFFFFF; font-weight:bold; }"
            "QSpinBox:focus, QDoubleSpinBox:focus { border:1px solid #7452FF; }"
            "QSpinBox::up-button, QDoubleSpinBox::up-button, "
            "QSpinBox::down-button, QDoubleSpinBox::down-button { "
            "background:#2D303D; border:none; width:16px; }"
            "QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { "
            "image:none; border-left:4px solid transparent; border-right:4px solid transparent; "
            "border-bottom:5px solid #A7F3D0; width:0; height:0; }"
            "QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { "
            "image:none; border-left:4px solid transparent; border-right:4px solid transparent; "
            "border-top:5px solid #A7F3D0; width:0; height:0; }"
        )

    def _style_combo_popup(self, combo):
        """Ép danh sách xổ ra (popup) của QComboBox có nền tối, chữ sáng.
        Popup là cửa sổ riêng nên không nhận stylesheet kế thừa -> phải gán
        QListView riêng, style + set palette (ép màu chữ ở tầng thấp nhất,
        tránh chữ đen trên nền đen)."""
        from PyQt6.QtGui import QPalette, QColor
        view = QListView()
        pal = view.palette()
        pal.setColor(QPalette.ColorRole.Base, QColor("#1C1D27"))
        pal.setColor(QPalette.ColorRole.Text, QColor("#E5E6E8"))
        pal.setColor(QPalette.ColorRole.Highlight, QColor("#31265C"))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
        view.setPalette(pal)
        # Ép mọi dòng cùng chiều cao cố định -> tránh dòng có emoji cao, dòng
        # không emoji thấp bị dồn/đè lên nhau.
        view.setUniformItemSizes(True)
        view.setStyleSheet(
            "QListView { background:#1C1D27; color:#E5E6E8; border:1px solid #7452FF; outline:0; }"
            "QListView::item { color:#E5E6E8; height:26px; padding-left:8px; padding-right:8px; }"
            "QListView::item:selected { background:#31265C; color:#FFFFFF; }"
        )
        combo.setView(view)
        # Ép cả màu chữ của ô combo (phần đang hiển thị) cho chắc
        combo.setStyleSheet(
            "QComboBox { background:#11121A; border:1px solid #2D303D; padding:6px; "
            "color:#FFFFFF; border-radius:4px; font-weight:bold; }"
            "QComboBox QAbstractItemView { background:#1C1D27; color:#E5E6E8; "
            "selection-background-color:#31265C; selection-color:#FFFFFF; }"
        )

    def _build_ui(self):
        # Nền tối + style control đồng bộ với tab Render (widget con độc lập
        # KHÔNG tự thừa hưởng stylesheet của RenderWidget, nên phải set lại ở đây,
        # nếu không nền sẽ trắng xóa và checkbox hiện thành chấm tròn).
        self.setStyleSheet("""
            QWidget { background:#151821; color:#E5E6E8; font-family:'Segoe UI',Arial,sans-serif; }
            QScrollArea { border:none; background:transparent; }
            QScrollBar:vertical { background:#151821; width:8px; }
            QScrollBar::handle:vertical { background:#3B3E4D; border-radius:4px; }
            QLabel { background:transparent; }
            QPushButton { background:#2D303D; color:#E5E6E8; border-radius:6px; padding:7px; font-weight:bold; border:1px solid #3B3E4D; }
            QPushButton:hover { background:#3B3E4D; border:1px solid #7452FF; color:white; }
            QLineEdit, QSpinBox, QComboBox, QDoubleSpinBox { background:#11121A; border:1px solid #2D303D; padding:6px; color:white; border-radius:4px; font-weight:bold; }
            QComboBox QAbstractItemView { background:#1C1D27; border:1px solid #7452FF; selection-background-color:#2D303D; color:#E5E6E8; }
            QCheckBox { color:#E5E6E8; font-weight:bold; padding:3px; background:transparent; }
            QCheckBox::indicator { width:18px; height:18px; border-radius:4px; border:1px solid #3B3E4D; background:#11121A; }
            QCheckBox::indicator:checked { background:#10B981; border:1px solid #10B981; }
            QTextEdit { background:#0B0E14; color:#A7F3D0; border:1px solid #1F222D; }
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 8, 6, 6)
        root.setSpacing(6)

        if not _ENGINE_OK:
            warn = QLabel(
                "⚠️ Không nạp được động cơ tách sub/dịch/lồng tiếng từ honggou_tab.\n"
                f"Chi tiết: {_ENGINE_ERR}\n\n"
                "Hãy đảm bảo honggou_tab.py và các module phụ (capcut_tts_api.py, "
                "translate_tab.py, demucs_manager.py, shared_utils.py, Voice.json) "
                "nằm cùng thư mục với app."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color:#F87171; font-weight:bold; padding:10px;")
            root.addWidget(warn)
            root.addStretch()
            return

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        # KHÔNG cho thanh cuộn ngang -> tránh nội dung tràn rộng khỏi cột phải
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet("border:none; background:transparent;")
        inner = QWidget()
        # Ép widget co theo bề ngang cột, không bung ra theo nội dung
        inner.setMaximumWidth(360)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(5)
        scroll.setWidget(inner)
        root.addWidget(scroll, stretch=1)

        _lbl = lambda t: QLabel(t, styleSheet="color:#8A8D98; font-size:10px; border:none;")

        # Nút Đồng bộ Gemini (đăng nhập + chọn prompt)
        self.btn_gemini = QPushButton("🔑 Đồng bộ Gemini")
        self.btn_gemini.clicked.connect(self._open_gemini_sync)
        self.btn_gemini.setStyleSheet(
            "QPushButton { background:#7c3aed; color:white; padding:7px; border-radius:6px; font-weight:bold; border:none; }"
            "QPushButton:hover { background:#6d28d9; }")
        lay.addWidget(self.btn_gemini)
        self._refresh_gemini_btn()

        # ── 1) TÁCH SUB ──────────────────────────────────────────────
        lay.addWidget(QLabel("① Tách phụ đề (STT)", styleSheet="font-weight:bold; color:#10B981; border:none;"))
        row_src = QHBoxLayout()
        row_src.addWidget(_lbl("Ngôn ngữ gốc:"))
        self.cmb_stt_src = QComboBox()
        self.cmb_stt_src.addItems(["zh-CN", "en-US", "ko-KR", "ja-JP", "vi-VN"])
        self._style_combo_popup(self.cmb_stt_src)
        self.cmb_stt_src.setCurrentText(self.settings.value("stt_src", "zh-CN"))
        row_src.addWidget(self.cmb_stt_src, 1)
        lay.addLayout(row_src)

        # Chọn ENGINE tách sub: CapCut (nhanh, cần mạng, KHÔNG làm video dài)
        # hoặc Whisper (offline, chạy được video dài, có VAD lọc nhạc nền).
        row_eng_stt = QHBoxLayout()
        row_eng_stt.addWidget(_lbl("Engine STT:"))
        self.cmb_stt_engine = QComboBox()
        self.cmb_stt_engine.addItems(["☁️ CapCut (video ngắn)",
                                      "💻 Whisper (video dài, offline)"])
        self._style_combo_popup(self.cmb_stt_engine)
        self.cmb_stt_engine.setCurrentText(
            self.settings.value("stt_engine", "☁️ CapCut (video ngắn)"))
        self.cmb_stt_engine.currentTextChanged.connect(self._on_stt_engine_changed)
        row_eng_stt.addWidget(self.cmb_stt_engine, 1)
        lay.addLayout(row_eng_stt)

        # Chọn cỡ model Whisper (chỉ hiện khi dùng Whisper).
        #   small  ~480MB — nhẹ; tiếng Anh tốt, tiếng Trung tạm
        #   medium ~1.5GB — tiếng Trung tốt (khuyên cho drama Trung)
        #   large-v3 ~3GB — chính xác nhất, cần máy khỏe
        self.row_whisper_model = QHBoxLayout()
        self.row_whisper_model.addWidget(_lbl("Model:"))
        self.cmb_whisper_model = QComboBox()
        self.cmb_whisper_model.addItems([
            "small (nhẹ · EN tốt)",
            "medium (khuyên · ZH tốt)",
            "large-v3 (chính xác nhất)",
        ])
        self._style_combo_popup(self.cmb_whisper_model)
        self.cmb_whisper_model.setCurrentText(
            self.settings.value("whisper_model", "medium (khuyên · ZH tốt)"))
        self.row_whisper_model.addWidget(self.cmb_whisper_model, 1)
        lay.addLayout(self.row_whisper_model)
        # Ẩn/hiện model theo engine đang chọn
        self._on_stt_engine_changed(self.cmb_stt_engine.currentText())

        lay.addWidget(QLabel("ℹ️ Nút 'LÀM TẤT CẢ' tự nhận diện sub sẵn có:\n"
                             "sub Việt → lồng luôn; sub Trung/khác → dịch; không có → tách.",
                             styleSheet="color:#64748b; font-size:9px; border:none;"))

        # ── 2) DỊCH ──────────────────────────────────────────────────
        lay.addWidget(QLabel("② Dịch sang tiếng Việt", styleSheet="font-weight:bold; color:#10B981; border:none; margin-top:6px;"))

        row_eng = QHBoxLayout()
        row_eng.addWidget(_lbl("Engine:"))
        self.cb_translate_engine = QComboBox()
        self.cb_translate_engine.addItems(["🌐 Gemini (free)", "🚀 DeepSeek (API)"])
        self._style_combo_popup(self.cb_translate_engine)
        self.cb_translate_engine.setCurrentText(self.settings.value("trans_engine", "🌐 Gemini (free)"))
        self.cb_translate_engine.currentTextChanged.connect(self._on_engine_changed)
        row_eng.addWidget(self.cb_translate_engine, 1)
        lay.addLayout(row_eng)

        self.txt_ds_key = QLineEdit(self.settings.value("ds_key", ""))
        self.txt_ds_key.setPlaceholderText("DeepSeek API key...")
        self.txt_ds_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_ds_key.setVisible(self.cb_translate_engine.currentText().startswith("🚀"))
        lay.addWidget(self.txt_ds_key)

        row_tw = QHBoxLayout()
        row_tw.addWidget(_lbl("Số tập dịch song song:"))
        self.spn_trans_workers = QSpinBox()
        self.spn_trans_workers.setRange(1, 5)
        self.spn_trans_workers.setValue(int(self.settings.value("trans_workers", 2)))
        self._style_num(self.spn_trans_workers)
        row_tw.addWidget(self.spn_trans_workers)
        self.chk_show_browser = QCheckBox("👁 Hiện trình duyệt")
        row_tw.addWidget(self.chk_show_browser)
        row_tw.addStretch()
        lay.addLayout(row_tw)

        # ── 3) LỒNG TIẾNG ────────────────────────────────────────────
        lay.addWidget(QLabel("③ Lồng tiếng (TTS)", styleSheet="font-weight:bold; color:#10B981; border:none; margin-top:6px;"))
        self.chk_auto_dub = QCheckBox("🎙 Có lồng tiếng (trong 'LÀM TẤT CẢ')")
        self.chk_auto_dub.setChecked(self.settings.value("auto_dub", True, type=bool))
        lay.addWidget(self.chk_auto_dub)

        row_voice = QHBoxLayout()
        row_voice.addWidget(_lbl("Giọng:"))
        self.cmb_dub_voice = QComboBox()
        self.cmb_dub_voice.addItems(_load_dub_voices())
        self._style_combo_popup(self.cmb_dub_voice)
        saved_voice = self.settings.value("dub_voice", "")
        if saved_voice:
            i = self.cmb_dub_voice.findText(saved_voice)
            if i >= 0:
                self.cmb_dub_voice.setCurrentIndex(i)
        else:
            for i in range(self.cmb_dub_voice.count()):
                if "BV074_streaming]" in self.cmb_dub_voice.itemText(i):
                    self.cmb_dub_voice.setCurrentIndex(i)
                    break
        row_voice.addWidget(self.cmb_dub_voice, 1)
        lay.addLayout(row_voice)

        row_rate = QHBoxLayout()
        row_rate.addWidget(_lbl("Tốc độ:"))
        self.spn_dub_rate = QDoubleSpinBox()
        self.spn_dub_rate.setRange(0.5, 2.0)
        self.spn_dub_rate.setSingleStep(0.1)
        self.spn_dub_rate.setValue(float(self.settings.value("dub_rate", 1.0)))
        self._style_num(self.spn_dub_rate)
        row_rate.addWidget(self.spn_dub_rate)
        row_rate.addWidget(_lbl("Số luồng TTS:"))
        self.spn_tts_workers = QSpinBox()
        self.spn_tts_workers.setRange(1, 8)
        self.spn_tts_workers.setValue(int(self.settings.value("tts_workers", 4)))
        self._style_num(self.spn_tts_workers)
        row_rate.addWidget(self.spn_tts_workers)
        row_rate.addStretch()
        lay.addLayout(row_rate)

        self.chk_mute_original = QCheckBox("Tắt tiếng gốc")
        self.chk_mute_original.setChecked(self.settings.value("mute_original", True, type=bool))
        lay.addWidget(self.chk_mute_original)

        row_ov = QHBoxLayout()
        row_ov.addWidget(_lbl("Âm lượng tiếng gốc giữ lại (%):"))
        self.spn_orig_volume = QSpinBox()
        self.spn_orig_volume.setRange(0, 100)
        self.spn_orig_volume.setValue(int(self.settings.value("orig_volume", 15)))
        self._style_num(self.spn_orig_volume)
        row_ov.addWidget(self.spn_orig_volume)
        row_ov.addStretch()
        lay.addLayout(row_ov)

        self.chk_remove_bgm = QCheckBox("🎵 Tách nhạc nền (Demucs) trước khi lồng")
        self.chk_remove_bgm.setChecked(self.settings.value("remove_bgm", False, type=bool))
        lay.addWidget(self.chk_remove_bgm)
        self.chk_use_gpu = QCheckBox("⚡ Dùng GPU cho Demucs")
        self.chk_use_gpu.setChecked(self.settings.value("use_gpu", False, type=bool))
        lay.addWidget(self.chk_use_gpu)

        lay.addStretch()

        # ── CÁC NÚT CHẠY ĐỘC LẬP ─────────────────────────────────────
        # Mỗi bước chạy riêng được. Nút "LÀM TẤT CẢ" tự dò từng video xem
        # đã có sub Việt/Trung chưa để bỏ qua bước thừa.
        def _mk_btn(text, color, hover, slot, big=False):
            b = QPushButton(text)
            pad = "11px" if big else "8px"
            fs = "13px" if big else "11px"
            b.setStyleSheet(
                f"QPushButton {{ background:{color}; color:white; padding:{pad}; font-size:{fs}; "
                f"font-weight:bold; border-radius:8px; border:none; }}"
                f"QPushButton:hover {{ background:{hover}; }}"
                f"QPushButton:disabled {{ background:#3B3E4D; color:#8A8D98; }}")
            b.clicked.connect(slot)
            return b

        # Ghi chú: nút "LÀM TẤT CẢ QUY TRÌNH" nằm NGOÀI (cạnh RENDER TẤT CẢ),
        # dùng đúng cấu hình đã set trong tab này.
        lbl_note = QLabel("🚀 Nút 'LÀM TẤT CẢ QUY TRÌNH' nằm ở cột phải ngoài, "
                          "cạnh nút RENDER — dùng đúng cấu hình bên trong tab này.")
        lbl_note.setWordWrap(True)
        lbl_note.setStyleSheet("color:#F37021; font-weight:bold; font-size:10px; border:none;")
        root.addWidget(lbl_note)

        self.chk_auto_render = QCheckBox("🎬 Tự Render sau khi lồng tiếng xong (dùng cấu hình tab Thiết kế)")
        self.chk_auto_render.setChecked(self.settings.value("auto_render", True, type=bool))
        self.chk_auto_render.setStyleSheet("color:#10B981; font-weight:bold; font-size:10px;")
        root.addWidget(self.chk_auto_render)

        # 4 nút chạy từng bước riêng
        row_steps = QHBoxLayout()
        self.btn_only_stt = _mk_btn("① Tách sub", "#2D303D", "#3B3E4D", self._run_only_stt)
        self.btn_only_trans = _mk_btn("② Dịch", "#2D303D", "#3B3E4D", self._run_only_translate)
        row_steps.addWidget(self.btn_only_stt)
        row_steps.addWidget(self.btn_only_trans)
        root.addLayout(row_steps)

        row_steps2 = QHBoxLayout()
        self.btn_only_dub = _mk_btn("③ Lồng tiếng", "#2D303D", "#3B3E4D", self._run_only_dub)
        self.btn_only_render = _mk_btn("④ Render", "#2D303D", "#3B3E4D", self._run_only_render)
        row_steps2.addWidget(self.btn_only_dub)
        row_steps2.addWidget(self.btn_only_render)
        root.addLayout(row_steps2)

        # Hàng nút tìm & fix file lỗi (chưa dịch / chưa lồng)
        row_fix = QHBoxLayout()
        self.btn_find_bad = _mk_btn("🔎 Tìm file lỗi", "#B45309", "#92400E", self._find_bad_files)
        self.btn_fix_bad = _mk_btn("🔧 Fix file lỗi", "#B45309", "#92400E", self._fix_bad_files)
        self.btn_fix_bad.setEnabled(False)   # chỉ bật sau khi đã tìm ra danh sách
        row_fix.addWidget(self.btn_find_bad)
        row_fix.addWidget(self.btn_fix_bad)
        root.addLayout(row_fix)

        # Log
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.document().setMaximumBlockCount(500)
        self.txt_log.setFixedHeight(120)
        self.txt_log.setStyleSheet(
            "background:#0B0E14; color:#A7F3D0; font-family:Consolas; font-size:10px; padding:5px; border:1px solid #1F222D;")
        root.addWidget(self.txt_log)

        self._on_engine_changed(self.cb_translate_engine.currentText())

    # ────────────────────────────────────────────────────────────────────
    #  TIỆN ÍCH
    # ────────────────────────────────────────────────────────────────────
    def _log(self, msg):
        self.txt_log.append(msg)
        self.txt_log.verticalScrollBar().setValue(self.txt_log.verticalScrollBar().maximum())

    def _keep_alive(self, th):
        if not hasattr(self, "_threads_alive"):
            self._threads_alive = []
        self._threads_alive.append(th)
        th.finished.connect(lambda: self._threads_alive.remove(th) if th in self._threads_alive else None)

    def _files_from_host(self):
        """Lấy danh sách video đang có trong hàng đợi Render."""
        cards = getattr(self.host, "cards", []) or []
        files = []
        for c in cards:
            vp = getattr(c, "video_path", None)
            if vp and os.path.exists(vp) and vp not in files:
                files.append(vp)
        return files

    def _save_settings(self):
        s = self.settings
        s.setValue("stt_src", self.cmb_stt_src.currentText())
        s.setValue("stt_engine", self.cmb_stt_engine.currentText())
        s.setValue("whisper_model", self.cmb_whisper_model.currentText())
        s.setValue("trans_engine", self.cb_translate_engine.currentText())
        s.setValue("ds_key", self.txt_ds_key.text().strip())
        s.setValue("trans_workers", self.spn_trans_workers.value())
        s.setValue("auto_dub", self.chk_auto_dub.isChecked())
        s.setValue("dub_voice", self.cmb_dub_voice.currentText())
        s.setValue("dub_rate", self.spn_dub_rate.value())
        s.setValue("tts_workers", self.spn_tts_workers.value())
        s.setValue("mute_original", self.chk_mute_original.isChecked())
        s.setValue("orig_volume", self.spn_orig_volume.value())
        s.setValue("remove_bgm", self.chk_remove_bgm.isChecked())
        s.setValue("use_gpu", self.chk_use_gpu.isChecked())
        if hasattr(self, "chk_auto_render"):
            s.setValue("auto_render", self.chk_auto_render.isChecked())

    def _on_engine_changed(self, text):
        self.txt_ds_key.setVisible(text.startswith("🚀"))

    def _on_stt_engine_changed(self, text):
        """Ẩn dropdown model khi dùng CapCut, hiện khi dùng Whisper."""
        is_whisper = text.startswith("💻")
        for i in range(self.row_whisper_model.count()):
            w = self.row_whisper_model.itemAt(i).widget()
            if w:
                w.setVisible(is_whisper)

    def _whisper_model_name(self):
        """Nhãn dropdown ('medium (khuyên...)') → tên model faster-whisper."""
        label = self.cmb_whisper_model.currentText()
        return label.split(" ", 1)[0].strip()   # 'medium', 'small', 'large-v3'

    # ════════════════════════════════════════════════════════════════════
    #  NHẬN DIỆN NGÔN NGỮ SRT — đọc nội dung thật, không dựa vào tên file
    # ════════════════════════════════════════════════════════════════════
    @staticmethod
    def _detect_srt_lang(srt_path):
        """Trả về 'vi' (tiếng Việt), 'zh' (tiếng Trung) hoặc 'other'.
        Cách: đọc nội dung, đếm ký tự Hán vs ký tự có dấu tiếng Việt."""
        try:
            with open(srt_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception:
            return "other"
        # Bỏ dòng số thứ tự và dòng timecode để chỉ soi phần chữ
        import re
        text = re.sub(r"\d+:\d+:\d+[,\.]\d+\s*-->\s*\d+:\d+:\d+[,\.]\d+", "", text)
        han = 0          # ký tự Hán (CJK)
        viet = 0         # ký tự riêng của tiếng Việt (có dấu)
        VIET_CHARS = set("ăâđêôơưÀÁẢÃẠàáảãạ ăằắẳẵặâầấẩẫậ đèéẻẽẹêềếểễệ ìíỉĩị "
                         "òóỏõọôồốổỗộơờớởỡợ ùúủũụưừứửữự ỳýỷỹỵ".replace(" ", ""))
        for ch in text:
            o = ord(ch)
            if 0x4E00 <= o <= 0x9FFF:      # khối CJK Unified Ideographs
                han += 1
            elif ch in VIET_CHARS or ch.lower() in VIET_CHARS:
                viet += 1
        if han >= 5 and han > viet:
            return "zh"
        if viet >= 3 and viet >= han:
            return "vi"
        # Không rõ Trung, không rõ Việt -> coi là ngôn ngữ khác (sẽ đem dịch)
        return "other"

    @staticmethod
    def _orig_stem_for(video_path):
        """Stem gốc của video (bỏ hậu tố _dubbed nếu video_path đang trỏ
        tới bản đã lồng tiếng), để luôn ghép đúng tên srt cạnh video gốc."""
        stem, _ext = os.path.splitext(video_path)
        if stem.endswith("_dubbed"):
            return stem[:-len("_dubbed")]
        return stem

    def _vi_srt_for(self, video_path):
        """Đường dẫn sub tiếng Việt đích của 1 video."""
        return self._orig_stem_for(video_path) + "_vi.srt"

    def _find_existing_srt(self, video_path):
        """Tìm srt đang có cạnh video. Ưu tiên *_vi.srt, rồi tới *.srt.
        Trả về (path, lang) hoặc (None, None)."""
        base = self._orig_stem_for(video_path)
        vi = base + "_vi.srt"
        raw = base + ".srt"
        # Ưu tiên bản _vi.srt nếu nội dung đúng là tiếng Việt
        if os.path.exists(vi):
            lang = self._detect_srt_lang(vi)
            return vi, lang
        if os.path.exists(raw):
            lang = self._detect_srt_lang(raw)
            return raw, lang
        return None, None

    @staticmethod
    def _srt_is_empty(srt_path):
        """True nếu .srt KHÔNG có dòng thoại thật (tập không thoại / cảnh đánh
        nhau). Khi đó không cần dịch/lồng — chỉ giữ tiếng gốc."""
        try:
            if not srt_path or not os.path.exists(srt_path):
                return True
            if os.path.getsize(srt_path) < 8:
                return True
            import re
            with open(srt_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            if not re.search(r"\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->", text):
                return True
            for line in text.splitlines():
                s = line.strip()
                if not s or s.isdigit() or "-->" in s:
                    continue
                return False   # có 1 dòng chữ thật
            return True
        except Exception:
            return False

    def _prepare_no_dialogue(self, video_path, srt_path=None):
        """Tập không thoại: tạo _vi.srt (rỗng hợp lệ) + _dubbed.mp4 với tiếng
        gốc đã ĐƯA VỀ ĐÚNG mức như khi lồng tiếng (theo 'Tắt tiếng gốc' /
        'Vol gốc'), để âm lượng ĐỒNG BỘ với các tập có lồng tiếng. Trả về True
        nếu chuẩn bị xong."""
        import shutil
        vi = self._vi_srt_for(video_path)
        # 1) _vi.srt rỗng hợp lệ (render tự bỏ ép sub khi thấy rỗng)
        try:
            if srt_path and os.path.exists(srt_path) and srt_path != vi:
                shutil.copyfile(srt_path, vi)
            elif not os.path.exists(vi):
                with open(vi, "w", encoding="utf-8") as f:
                    f.write("")
        except Exception as e:
            self._log(f"⚠️ {os.path.basename(video_path)}: không tạo được _vi.srt rỗng: {e}")
            return False

        # 2) _dubbed.mp4 với tiếng gốc theo đúng cấu hình lồng tiếng
        if self._auto_dub_on:
            stem = os.path.splitext(video_path)[0]
            if stem.endswith("_dubbed"):
                return True   # đã là bản dubbed
            dub = self._dubbed_for(video_path)
            if os.path.exists(dub):
                return True

            mute = self.chk_mute_original.isChecked()
            orig_v = 0.0 if mute else (self.spn_orig_volume.value() / 100.0)

            try:
                ff = get_ffmpeg_path()
                si = None
                if os.name == "nt":
                    si = subprocess.STARTUPINFO()
                    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW

                if orig_v <= 0:
                    # Câm tiếng gốc: giữ video, thay bằng audio im lặng cùng độ dài
                    cmd = [ff, "-y", "-i", video_path,
                           "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                           "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                           "-movflags", "+faststart", dub]
                    self._log(f"🔇 {os.path.basename(video_path)}: tập không thoại → tắt tiếng gốc cho đồng bộ.")
                elif abs(orig_v - 1.0) < 0.001:
                    # Giữ nguyên 100% -> copy trần cho nhanh
                    shutil.copyfile(video_path, dub)
                    self._log(f"🔇 {os.path.basename(video_path)}: tập không thoại → giữ tiếng gốc 100%.")
                    return True
                else:
                    # Hạ tiếng gốc về đúng mức 'Vol gốc' như khi lồng tiếng
                    cmd = [ff, "-y", "-i", video_path,
                           "-map", "0:v:0", "-map", "0:a:0?",
                           "-filter:a", f"volume={orig_v:.2f}",
                           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                           "-movflags", "+faststart", dub]
                    self._log(f"🔇 {os.path.basename(video_path)}: tập không thoại → "
                              f"tiếng gốc {int(orig_v*100)}% cho đồng bộ.")

                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      startupinfo=si, text=True, errors="ignore")
                if proc.returncode != 0 or not os.path.exists(dub):
                    # Encode lỗi -> fallback copy trần để ít nhất vẫn render được
                    self._log(f"⚠️ {os.path.basename(video_path)}: chỉnh tiếng gốc lỗi, copy trần video.")
                    shutil.copyfile(video_path, dub)
            except Exception as e:
                self._log(f"⚠️ {os.path.basename(video_path)}: lỗi tạo _dubbed ({e}), copy trần.")
                try:
                    shutil.copyfile(video_path, dub)
                except Exception:
                    return False
        else:
            self._log(f"🔇 {os.path.basename(video_path)}: tập không thoại → giữ tiếng gốc, không lồng.")
        return True

    def _set_buttons_enabled(self, on):
        for b in ("btn_only_stt", "btn_only_trans", "btn_only_dub", "btn_only_render",
                  "btn_find_bad"):
            if hasattr(self, b):
                getattr(self, b).setEnabled(on)
        # Nút Fix chỉ bật khi rảnh VÀ đã có danh sách lỗi tìm được
        if hasattr(self, "btn_fix_bad"):
            self.btn_fix_bad.setEnabled(on and bool(self._bad_found))
        # Nút "LÀM TẤT CẢ" nằm ngoài host — bật/tắt nếu có tham chiếu
        ext = getattr(self.host, "btn_full_pipeline", None)
        if ext is not None:
            ext.setEnabled(on)

    # ════════════════════════════════════════════════════════════════════
    #  ① NÚT: CHỈ TÁCH SUB
    # ════════════════════════════════════════════════════════════════════
    def _run_only_stt(self):
        files = self._files_from_host()
        if not files:
            QMessageBox.warning(self, "Không có file", "Hàng đợi Render đang trống!")
            return
        self._save_settings()
        self._chain_after_stt = None   # chạy riêng, không nối bước sau
        self._start_stt(files)

    def _start_stt(self, files):
        self._log(f"① Tách phụ đề {len(files)} video...")
        self._set_buttons_enabled(False)
        self._start_card_poll()
        self._stt_files = list(files)
        src = self.cmb_stt_src.currentText()

        use_whisper = self.cmb_stt_engine.currentText().startswith("💻")
        if use_whisper:
            if not (WhisperSttThread and _WHISPER_AVAILABLE):
                self._log("❌ Chưa cài faster-whisper. Mở CMD chạy:  "
                          "pip install faster-whisper  — rồi thử lại.")
                self._set_buttons_enabled(True)
                self._stop_card_poll()
                return
            model_name = self._whisper_model_name()
            self._log(f"🧠 Dùng Whisper (model '{model_name}') — chạy được video dài.")
            self._stt_thread = WhisperSttThread(
                files, src_lang=src, out_lang="vi-VN",
                use_trans=False, model_name=model_name)
        else:
            self._stt_thread = SttBatchThread(files, src_lang=src, out_lang="vi-VN",
                                              use_trans=False, stt_workers=3)

        self._stt_thread.progress_signal.connect(self._log)
        self._stt_thread.finished_signal.connect(self._on_stt_finished)
        self._keep_alive(self._stt_thread)
        self._stt_thread.start()

    def _on_stt_finished(self, ok, failed):
        self._log(f"✅ Tách sub xong: {ok} ok, {failed} lỗi.")
        self._set_buttons_enabled(True)
        chain = getattr(self, "_chain_after_stt", None)
        if chain:
            chain(ok, failed)          # nối bước tiếp trong quy trình full
        else:
            self._stop_card_poll()

    # ════════════════════════════════════════════════════════════════════
    #  ② NÚT: CHỈ DỊCH  (nhận diện; Việt thì bỏ qua, còn lại dịch sang Việt)
    # ════════════════════════════════════════════════════════════════════
    def _run_only_translate(self):
        files = self._files_from_host()
        if not files:
            QMessageBox.warning(self, "Không có file", "Hàng đợi Render đang trống!")
            return
        self._save_settings()

        to_translate = []     # [(video, srt_nguồn)]
        for vp in files:
            srt, lang = self._find_existing_srt(vp)
            if not srt:
                self._log(f"⏭ Bỏ qua (chưa có srt): {os.path.basename(vp)}")
                continue
            if self._srt_is_empty(srt):
                self._log(f"🔇 {os.path.basename(vp)}: tập không thoại — bỏ qua dịch.")
                continue
            if lang == "vi":
                # Đã là tiếng Việt -> chỉ cần bảo đảm có bản _vi.srt
                vi = self._vi_srt_for(vp)
                if srt != vi:
                    try:
                        import shutil
                        shutil.copyfile(srt, vi)
                    except Exception:
                        pass
                self._log(f"✅ {os.path.basename(vp)}: sub đã là tiếng Việt — bỏ qua dịch.")
            else:
                self._log(f"🌐 {os.path.basename(vp)}: sub {('tiếng Trung' if lang=='zh' else 'ngôn ngữ khác')} → sẽ dịch sang Việt.")
                to_translate.append((vp, srt))

        if not to_translate:
            self._log("🎉 Không có gì cần dịch. Xong.")
            self._refresh_host_cards()
            return
        self._auto_dub_on = False       # chạy riêng: dịch xong thì dừng
        self._chain_dub_after_translate = False
        self._start_translate(to_translate)

    # ════════════════════════════════════════════════════════════════════
    #  DỊCH (dùng chung) — dịch xong tập nào, nếu đang trong quy trình full
    #  thì xếp hàng lồng tiếng tập đó (cuốn chiếu).
    # ════════════════════════════════════════════════════════════════════
    def _start_translate(self, srt_files):
        use_deepseek = self.cb_translate_engine.currentText().startswith("🚀")
        queue = [{"video": v, "srt": s} for (v, s) in srt_files]
        self._dub_queue = []
        self._dub_running = False

        def _on_item_done(idx, video_path, vi_path):
            if getattr(self, "_chain_dub_after_translate", False) and vi_path and os.path.exists(vi_path):
                self._log(f"✅ Dịch xong {os.path.basename(video_path)} → xếp hàng lồng tiếng.")
                self._dub_queue.append(video_path)
                self._pump_dub_queue()

        def _on_item_failed(idx, msg):
            self._log(f"⚠️ Dịch lỗi 1 file: {msg}")
            try:
                if 0 <= idx < len(queue):
                    vp = queue[idx].get("video")
                    if vp:
                        self._translate_failed.add(vp)
            except Exception:
                pass

        if use_deepseek:
            key = self.txt_ds_key.text().strip()
            if not key:
                self._log("❌ Chưa nhập DeepSeek API key.")
                self._set_buttons_enabled(True)
                return
            if DeepSeekTranslateThread is None:
                self._log("❌ Thiếu module deepseek_translate.py.")
                self._set_buttons_enabled(True)
                return
            self._log("🚀 Dịch bằng DeepSeek...")
            self._gtrans_thread = DeepSeekTranslateThread(queue, api_key=key, full_series_mode=False)
        else:
            if not _GEMINI_AVAILABLE or GeminiTranslateThread is None:
                self._log("❌ Thiếu module dịch Gemini (translate_tab.py).")
                self._set_buttons_enabled(True)
                return
            if not os.path.exists(AUTH_FILE):
                QMessageBox.warning(self, "Chưa đăng nhập Gemini",
                                    "Bấm '🔑 Đồng bộ Gemini' để đăng nhập trước khi dịch.")
                self._set_buttons_enabled(True)
                return
            preset_keys = list(PROMPT_PRESETS.keys())
            _s = QSettings("HongguoDownloader", "ClientApp")
            preset = _s.value("trans_preset", preset_keys[0] if preset_keys else "")
            if preset == CUSTOM_PROMPT_KEY:
                custom = _s.value("trans_custom_prompt", "").strip()
                if custom:
                    PROMPT_PRESETS[CUSTOM_PROMPT_KEY] = custom
            show_browser = self.chk_show_browser.isChecked()
            workers = self.spn_trans_workers.value()
            if show_browser and workers > 1:
                workers = 1
                self._log("👁 Bật hiện trình duyệt → tạm 1 tập/lượt.")
            self._log("🌐 Dịch bằng Gemini...")
            self._gtrans_thread = GeminiTranslateThread(
                queue, preset, "Auto (Mặc định)", 80,
                translate_workers=workers, show_browser=show_browser)

        self._set_buttons_enabled(False)
        self._start_card_poll()
        self._gtrans_thread.log.connect(lambda m: self._log(m.strip()))
        self._gtrans_thread.item_done.connect(_on_item_done)
        self._gtrans_thread.item_failed.connect(_on_item_failed)
        self._gtrans_thread.all_done.connect(self._on_translate_all_done)
        self._keep_alive(self._gtrans_thread)
        self._gtrans_thread.start()

    def _on_translate_all_done(self, *args):
        self._log("✅ Dịch xong toàn bộ.")
        self._refresh_host_cards()
        if not getattr(self, "_chain_dub_after_translate", False):
            self._set_buttons_enabled(True)
            self._stop_card_poll()
            self._log("🎉 Hoàn tất bước dịch.")
            return
        # Đang chuỗi full: nếu hàng đợi lồng đã cạn và không còn tập nào đang
        # lồng (mọi tập đã lồng xong trước khi dịch kết thúc), thì tự kích
        # render ở đây (vì _pump_dub_queue sẽ không được gọi thêm lần nào).
        if not self._dub_queue and not self._dub_running:
            self._set_buttons_enabled(True)
            self._stop_card_poll()
            self._log("✅ Lồng tiếng xong toàn bộ.")
            if getattr(self, "_render_after_dub", False):
                self._verify_before_render()

    # ════════════════════════════════════════════════════════════════════
    #  ③ NÚT: CHỈ LỒNG TIẾNG  (lấy sub Việt cạnh video)
    # ════════════════════════════════════════════════════════════════════
    def _run_only_dub(self):
        files = self._files_from_host()
        if not files:
            QMessageBox.warning(self, "Không có file", "Hàng đợi Render đang trống!")
            return
        self._save_settings()

        ready = []
        for vp in files:
            vi = self._vi_srt_for(vp)
            if os.path.exists(vi):
                ready.append(vp)
                continue
            # Chưa có _vi.srt: nếu có srt cạnh đó mà là tiếng Việt thì dùng luôn
            srt, lang = self._find_existing_srt(vp)
            if srt and lang == "vi":
                try:
                    import shutil
                    shutil.copyfile(srt, vi)
                    ready.append(vp)
                except Exception:
                    self._log(f"⏭ {os.path.basename(vp)}: không copy được sub Việt.")
            else:
                self._log(f"⏭ Bỏ qua (chưa có sub tiếng Việt): {os.path.basename(vp)}")

        if not ready:
            QMessageBox.warning(self, "Không có sub Việt",
                                "Không tập nào có sub tiếng Việt để lồng.\nHãy dịch trước.")
            return
        self._render_after_dub = False   # chạy riêng: lồng xong thì dừng
        self._dub_queue = list(ready)
        self._dub_running = False
        self._set_buttons_enabled(False)
        self._start_card_poll()
        self._log(f"③ Lồng tiếng {len(ready)} tập...")
        self._pump_dub_queue()

    # ── Lồng tiếng cuốn chiếu từng tập ──────────────────────────────────
    def _pump_dub_queue(self):
        if self._dub_running or not self._dub_queue:
            return
        video_path = self._dub_queue.pop(0)
        vi_srt = self._vi_srt_for(video_path)
        if not os.path.exists(vi_srt):
            QTimer.singleShot(120, self._pump_dub_queue)
            return
        self._dub_running = True

        sel = self.cmb_dub_voice.currentText()
        voice_type = sel[sel.rfind("[") + 1:-1] if "[" in sel else "BV074_streaming"
        rate = f"{self.spn_dub_rate.value():.1f}"
        pitch = "+0Hz"
        if sel.startswith("🌐 "):
            label = sel[2:sel.rfind("[")].strip()
            preset = EDGE_TTS_VOICES.get(label)
            if preset:
                pitch = preset[1]

        mute_orig = self.chk_mute_original.isChecked()
        orig_vol = self.spn_orig_volume.value()
        remove_bgm = self.chk_remove_bgm.isChecked()
        use_gpu = self.chk_use_gpu.isChecked()
        tts_workers = self.spn_tts_workers.value()

        self._log(f"🎙 Lồng tiếng: {os.path.basename(video_path)}...")
        self._dub_thread = DubThread(
            [{"video": video_path, "srt": vi_srt}],
            voice_type=voice_type, rate=rate, pitch=pitch,
            mute_original=mute_orig, orig_volume=orig_vol,
            remove_bgm=remove_bgm, use_gpu=use_gpu, tts_workers=tts_workers)
        self._dub_thread.progress_signal.connect(lambda m: self._log(m.strip()))

        def _one_done(ok, failed):
            self._dub_running = False
            if self._dub_queue:
                self._pump_dub_queue()
            else:
                trans_running = (getattr(self, "_gtrans_thread", None) is not None
                                 and self._gtrans_thread.isRunning())
                if not trans_running:
                    self._log("✅ Lồng tiếng xong toàn bộ.")
                    self._refresh_host_cards()
                    self._set_buttons_enabled(True)
                    self._stop_card_poll()
                    if getattr(self, "_render_after_dub", False):
                        self._verify_before_render()

        self._dub_thread.finished_signal.connect(_one_done)
        self._keep_alive(self._dub_thread)
        self._dub_thread.start()

    # ════════════════════════════════════════════════════════════════════
    #  ④ NÚT: CHỈ RENDER  (gọi thẳng render của tab Thiết kế)
    # ════════════════════════════════════════════════════════════════════
    def _run_only_render(self):
        self._refresh_host_cards()
        self._start_render()

    # ════════════════════════════════════════════════════════════════════
    #  ✅ KIỂM TRA TRƯỚC KHI RENDER + RETRY
    #  Trước khi render cả loạt: soi từng tập xem đã dịch (_vi.srt) và (nếu
    #  bật lồng) đã lồng tiếng (_dubbed.mp4) chưa. Tập nào thiếu -> gom lại
    #  làm lại dịch→lồng. Làm lại tối đa _MAX_VERIFY_RETRY vòng; tập nào vẫn
    #  hỏng sau khi hết vòng -> loại khỏi render (không đem video/sub lỗi đi
    #  render), phần còn lại vẫn render bình thường.
    # ════════════════════════════════════════════════════════════════════
    # ════════════════════════════════════════════════════════════════════
    #  🔎 TÌM FILE LỖI  /  🔧 FIX FILE LỖI  (chạy độc lập, không render)
    #  Tìm: quét hàng đợi, liệt kê tập chưa dịch (thiếu _vi.srt) và — nếu ô
    #  'Có lồng tiếng' đang bật — tập chưa lồng (thiếu _dubbed.mp4).
    #  Fix: chỉ làm lại ĐÚNG những tập vừa tìm ra (dịch lại → lồng lại), tối
    #  đa _MAX_VERIFY_RETRY vòng; KHÔNG tự render.
    # ════════════════════════════════════════════════════════════════════
    def _find_bad_files(self):
        files = self._files_from_host()
        if not files:
            QMessageBox.warning(self, "Không có file", "Hàng đợi Render đang trống!")
            return
        # Tôn trọng lựa chọn lồng tiếng hiện tại khi đánh giá 'đủ/thiếu'
        self._auto_dub_on = self.chk_auto_dub.isChecked()
        bad = []
        for vp in files:
            reason = self._episode_incomplete(vp)
            if reason:
                bad.append((vp, reason))
        self._bad_found = list(bad)

        self._log("──────── 🔎 KẾT QUẢ TÌM FILE LỖI ────────")
        if not bad:
            self._log(f"✅ Cả {len(files)} tập đều đã đủ "
                      + ("(dịch + lồng tiếng)." if self._auto_dub_on else "(đã dịch).")
                      + " Không có file lỗi.")
        else:
            self._log(f"⚠️ Có {len(bad)}/{len(files)} tập lỗi:")
            for i, (vp, reason) in enumerate(bad, 1):
                self._log(f"   {i}. {os.path.basename(vp)} — {reason}")
            self._log("👉 Bấm '🔧 Fix file lỗi' để làm lại các tập trên.")
        # Bật/tắt nút Fix theo kết quả
        if hasattr(self, "btn_fix_bad"):
            self.btn_fix_bad.setEnabled(bool(bad))

    def _fix_bad_files(self):
        if not self._bad_found:
            QMessageBox.information(self, "Chưa có danh sách",
                                    "Bấm '🔎 Tìm file lỗi' trước để quét đã.")
            return
        self._save_settings()
        self._auto_dub_on = self.chk_auto_dub.isChecked()
        videos = [vp for vp, _r in self._bad_found]
        # Chỉ fix, không render; reset bộ đếm retry cho phiên fix này
        self._fix_only_mode = True
        self._render_after_dub = False
        self._verify_round = 0
        self._skip_from_render = set()
        self._log(f"🔧 Bắt đầu FIX {len(videos)} tập lỗi (làm lại dịch → lồng, tối đa "
                  f"{self._MAX_VERIFY_RETRY} vòng)...")
        self._start_verify_retry(videos)

    def _dubbed_for(self, video_path):
        """Đường dẫn video đã lồng tiếng của 1 tập (theo stem gốc)."""
        stem, ext = os.path.splitext(video_path)
        if stem.endswith("_dubbed"):
            return video_path
        return stem + "_dubbed" + ext

    def _episode_incomplete(self, video_path):
        """Trả về lý do 1 tập CHƯA sẵn sàng render, hoặc None nếu đã đủ.
        - Luôn cần _vi.srt (bản dịch tiếng Việt).
        - Nếu bật lồng tiếng thì cần thêm _dubbed.mp4."""
        vi = self._vi_srt_for(video_path)
        if not os.path.exists(vi):
            # Có thể là tập KHÔNG THOẠI (sub gốc rỗng) — không phải lỗi dịch.
            # Nếu đúng vậy thì chuẩn bị giữ tiếng gốc rồi coi như đã đủ.
            srt, _lang = self._find_existing_srt(video_path)
            if srt and self._srt_is_empty(srt):
                self._prepare_no_dialogue(video_path, srt)
            else:
                return "chưa có sub Việt (dịch lỗi/chưa dịch)"
        if getattr(self, "_auto_dub_on", False):
            dub = self._dubbed_for(video_path)
            if not os.path.exists(dub):
                # Nếu là tập không thoại (vi rỗng) thì tạo _dubbed = copy gốc
                if self._srt_is_empty(vi):
                    self._prepare_no_dialogue(video_path, vi)
                    dub = self._dubbed_for(video_path)
                if not os.path.exists(dub):
                    return "chưa lồng tiếng"
        return None

    def _verify_before_render(self):
        """Cổng kiểm tra chèn ngay trước render trong quy trình full.
        Quét hàng đợi, gom tập lỗi rồi retry; hết retry mới render phần đạt."""
        files = self._files_from_host()
        bad = []
        for vp in files:
            if vp in self._skip_from_render:
                continue
            reason = self._episode_incomplete(vp)
            if reason:
                bad.append((vp, reason))

        if not bad:
            # Tất cả đã đủ
            self._verify_round = 0
            if getattr(self, "_fix_only_mode", False):
                # Chạy từ nút '🔧 Fix' -> chỉ sửa, KHÔNG render
                self._fix_only_mode = False
                self._bad_found = []
                if self._skip_from_render:
                    self._log(f"⚠️ {len(self._skip_from_render)} tập không sửa được "
                              "(thiếu srt nguồn để dịch lại): "
                              + ", ".join(os.path.basename(v) for v in self._skip_from_render))
                    self._log("✅ Các tập còn lại đã được làm lại xong.")
                else:
                    self._log("✅ Fix xong: mọi tập lỗi đã được làm lại thành công.")
                self._set_buttons_enabled(True)
                self._stop_card_poll()
                return
            if self._skip_from_render:
                self._log(f"⚠️ Bỏ {len(self._skip_from_render)} tập lỗi khỏi render: "
                          + ", ".join(os.path.basename(v) for v in self._skip_from_render))
                self._exclude_skipped_cards()
            self._log("✅ Kiểm tra xong: mọi tập đã dịch"
                      + (" + lồng tiếng" if self._auto_dub_on else "") + ". Bắt đầu render.")
            self._start_render()
            return

        # Còn tập lỗi
        for vp, reason in bad:
            self._log(f"🔎 Chưa đạt: {os.path.basename(vp)} — {reason}")

        if self._verify_round >= self._MAX_VERIFY_RETRY:
            # Hết lượt retry
            still_bad = [os.path.basename(v) for v, _r in bad]
            if getattr(self, "_fix_only_mode", False):
                # Nút Fix: không render, chỉ báo tập nào không sửa được
                self._fix_only_mode = False
                self._verify_round = 0
                self._bad_found = list(bad)   # giữ lại để bấm Fix tiếp nếu muốn
                self._log(f"⛔ Đã thử làm lại {self._MAX_VERIFY_RETRY} vòng nhưng "
                          f"{len(bad)} tập vẫn lỗi: " + ", ".join(still_bad))
                self._log("   → Kiểm tra file srt gốc / đăng nhập Gemini rồi bấm '🔧 Fix' lại.")
                self._set_buttons_enabled(True)
                self._stop_card_poll()
                return
            # Luồng full: loại các tập vẫn lỗi, render phần còn lại
            for vp, _r in bad:
                self._skip_from_render.add(vp)
            self._log(f"⛔ Đã retry {self._verify_round} vòng, "
                      f"{len(bad)} tập vẫn lỗi → BỎ QUA, chỉ render các tập đạt.")
            self._exclude_skipped_cards()
            remain = [v for v in files if v not in self._skip_from_render]
            if remain:
                self._start_render()
            else:
                self._log("❌ Không còn tập nào đạt để render.")
                self._set_buttons_enabled(True)
                self._stop_card_poll()
            return

        # Còn lượt retry -> làm lại dịch + lồng cho các tập lỗi
        self._verify_round += 1
        self._log(f"🔁 RETRY vòng {self._verify_round}/{self._MAX_VERIFY_RETRY} "
                  f"cho {len(bad)} tập lỗi (làm lại dịch → lồng tiếng)...")
        self._start_verify_retry([vp for vp, _r in bad])

    def _start_verify_retry(self, videos):
        """Làm lại cho các tập lỗi: tập nào thiếu _vi.srt thì dịch lại từ srt
        gốc; tập nào đã có _vi.srt mà thiếu _dubbed thì chỉ lồng lại. Xong
        vòng này thì quay lại _verify_before_render để kiểm tra tiếp."""
        self._translate_failed.clear()
        self._dub_queue = []
        self._dub_running = False

        need_translate = []   # [(video, srt_gốc)]
        need_dub_only = []    # [video] — đã có _vi.srt, chỉ thiếu lồng

        for vp in videos:
            vi = self._vi_srt_for(vp)
            # Tập không thoại (srt gốc rỗng) -> giữ tiếng gốc, không dịch/lồng
            srt0, _l0 = self._find_existing_srt(vp)
            if (srt0 and self._srt_is_empty(srt0)) or (os.path.exists(vi) and self._srt_is_empty(vi)):
                self._prepare_no_dialogue(vp, srt0 or vi)
                continue
            if not os.path.exists(vi):
                # Chưa có bản dịch -> tìm srt nguồn để dịch lại
                srt, lang = self._find_existing_srt(vp)
                if srt and lang != "vi":
                    need_translate.append((vp, srt))
                elif srt and lang == "vi" and srt != vi:
                    # srt cạnh là tiếng Việt sẵn -> copy thành _vi.srt
                    try:
                        import shutil
                        shutil.copyfile(srt, vi)
                        if self._auto_dub_on:
                            need_dub_only.append(vp)
                    except Exception:
                        self._log(f"⏭ {os.path.basename(vp)}: không copy được sub Việt.")
                else:
                    self._log(f"⏭ {os.path.basename(vp)}: không có srt nguồn để dịch lại → bỏ.")
                    self._skip_from_render.add(vp)
            else:
                # Đã có _vi.srt, chỉ thiếu lồng
                if self._auto_dub_on:
                    need_dub_only.append(vp)

        self._set_buttons_enabled(False)
        self._start_card_poll()

        if need_translate:
            # Dịch lại; sau khi dịch xong toàn bộ + lồng xong -> _on_translate_all_done
            # sẽ gọi lại _verify_before_render.
            self._chain_dub_after_translate = self._auto_dub_on
            self._start_translate(need_translate)   # ⚠ hàm này reset _dub_queue=[]
            # -> xếp hàng lồng cho tập chỉ thiếu lồng SAU khi reset, rồi đá chạy
            if self._auto_dub_on and need_dub_only:
                for vp in need_dub_only:
                    if vp not in self._dub_queue:
                        self._dub_queue.append(vp)
                self._pump_dub_queue()
        elif self._auto_dub_on and need_dub_only:
            for vp in need_dub_only:
                if vp not in self._dub_queue:
                    self._dub_queue.append(vp)
            # Không cần dịch, chỉ lồng lại
            self._pump_dub_queue()
        else:
            # Không có gì để làm lại (không dịch được, không lồng) -> kiểm tra tiếp
            QTimer.singleShot(200, self._verify_before_render)

    def _exclude_skipped_cards(self):
        """Gỡ các card thuộc _skip_from_render khỏi hàng đợi render của host,
        để _start_render_all không đem tập lỗi đi render."""
        if not self._skip_from_render:
            return
        cards = getattr(self.host, "cards", None)
        if not isinstance(cards, list):
            return
        removed = []
        for c in list(cards):
            vp = getattr(c, "video_path", None)
            # so cả bản gốc lẫn bản _dubbed cùng stem
            stem = os.path.splitext(vp or "")[0]
            base = stem[:-len("_dubbed")] if stem.endswith("_dubbed") else stem
            hit = False
            for bad in self._skip_from_render:
                bstem = os.path.splitext(bad)[0]
                bbase = bstem[:-len("_dubbed")] if bstem.endswith("_dubbed") else bstem
                if base == bbase:
                    hit = True
                    break
            if hit:
                try:
                    cards.remove(c)
                    c.setParent(None)
                    removed.append(vp)
                except Exception:
                    pass
        if removed:
            # cập nhật lại số đếm trên các nút render nếu host có hàm đó
            for m in ("_reindex_cards", "_update_run_button", "_refresh_counts"):
                fn = getattr(self.host, m, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass

    def _start_render(self):
        fn = getattr(self.host, "_start_render_all", None)
        if callable(fn):
            self._log("🎬 Bắt đầu render bằng cấu hình tab Thiết kế...")
            fn()
        else:
            self._log("❌ Không tìm thấy chức năng render của tab Thiết kế.")

    # ════════════════════════════════════════════════════════════════════
    #  🚀 NÚT: LÀM TẤT CẢ QUY TRÌNH → RENDER
    #  Mỗi video tự dò: có sub Việt -> bỏ tách+dịch; có sub Trung/khác -> bỏ
    #  tách, đem dịch; không có srt -> tách rồi mới dịch. Dịch cuốn chiếu đa
    #  luồng, lồng tiếng theo sau. Khi TẤT CẢ lồng xong mới render cả loạt.
    # ════════════════════════════════════════════════════════════════════
    def _run_full_pipeline(self):
        files = self._files_from_host()
        if not files:
            QMessageBox.warning(self, "Không có file", "Hàng đợi Render đang trống!")
            return
        self._save_settings()
        self.txt_log.clear()
        # Reset trạng thái kiểm tra/retry cho lần chạy mới
        self._verify_round = 0
        self._translate_failed = set()
        self._skip_from_render = set()
        self._log(f"🚀 LÀM TẤT CẢ với {len(files)} video. Đang phân loại...")

        # Đặt sớm để _prepare_no_dialogue biết có cần tạo _dubbed hay không
        self._auto_dub_on = self.chk_auto_dub.isChecked()

        need_stt = []          # chưa có srt -> phải tách
        need_translate = []    # có srt Trung/khác -> dịch
        ready_vi = []          # đã có sub Việt -> lồng luôn
        no_dialogue = []       # srt rỗng (tập không thoại) -> giữ tiếng gốc

        for vp in files:
            srt, lang = self._find_existing_srt(vp)
            if not srt:
                need_stt.append(vp)
            elif self._srt_is_empty(srt):
                # Tập không thoại: chuẩn bị _vi.srt rỗng + _dubbed (copy gốc)
                if self._prepare_no_dialogue(vp, srt):
                    no_dialogue.append(vp)
                else:
                    need_stt.append(vp)   # chuẩn bị fail -> thử tách lại
            elif lang == "vi":
                vi = self._vi_srt_for(vp)
                if srt != vi:
                    try:
                        import shutil
                        shutil.copyfile(srt, vi)
                    except Exception:
                        pass
                ready_vi.append(vp)
            else:
                need_translate.append((vp, srt))

        self._log(f"   • Cần tách sub: {len(need_stt)}  |  cần dịch: {len(need_translate)}  "
                  f"|  đã có sub Việt: {len(ready_vi)}  |  không thoại: {len(no_dialogue)}")

        # Cấu hình chuỗi full
        self._auto_dub_on = self.chk_auto_dub.isChecked()
        self._chain_dub_after_translate = self._auto_dub_on
        self._render_after_dub = (self._auto_dub_on and self.chk_auto_render.isChecked())
        self._dub_queue = []
        self._dub_running = False

        # Các tập đã có sub Việt -> nếu bật lồng thì xếp hàng lồng ngay
        if self._auto_dub_on:
            for vp in ready_vi:
                self._dub_queue.append(vp)

        # Gom danh sách cần dịch: gồm cả tập có sẵn srt Trung + tập vừa tách xong
        self._full_need_translate = list(need_translate)
        self._full_ready_vi = list(ready_vi)

        # Bắt đầu: nếu có tập cần tách -> tách trước, xong nối sang dịch;
        # nếu không -> đi thẳng vào dịch + lồng.
        if need_stt:
            def _after_stt(ok, failed):
                # Sau khi tách xong, nhận diện lại srt của các tập vừa tách
                for vp in need_stt:
                    srt, lang = self._find_existing_srt(vp)
                    if not srt:
                        self._log(f"⚠️ {os.path.basename(vp)}: tách sub thất bại, bỏ qua.")
                        continue
                    if self._srt_is_empty(srt):
                        # Tách ra sub rỗng -> tập không thoại, giữ tiếng gốc
                        self._prepare_no_dialogue(vp, srt)
                        continue
                    if lang == "vi":
                        vi = self._vi_srt_for(vp)
                        if srt != vi:
                            try:
                                import shutil
                                shutil.copyfile(srt, vi)
                            except Exception:
                                pass
                        if self._auto_dub_on:
                            self._dub_queue.append(vp)
                    else:
                        self._full_need_translate.append((vp, srt))
                self._full_start_translate_stage()
            self._chain_after_stt = _after_stt
            self._start_stt(need_stt)
        else:
            self._full_start_translate_stage()

    def _full_start_translate_stage(self):
        """Sau khi tách (nếu có): khởi động dịch cho các tập cần dịch, đồng
        thời đẩy hàng đợi lồng cho các tập đã sẵn sub Việt."""
        # Đá hàng đợi lồng cho các tập đã có sub Việt ngay từ đầu
        if self._auto_dub_on and self._dub_queue:
            self._pump_dub_queue()

        if self._full_need_translate:
            self._chain_dub_after_translate = self._auto_dub_on
            self._start_translate(self._full_need_translate)
        else:
            # Không có gì để dịch. Nếu không lồng -> xong; nếu lồng -> chờ
            # hàng đợi lồng chạy hết rồi render sẽ tự kích ở _pump_dub_queue.
            if not self._auto_dub_on:
                self._log("🎉 Hoàn tất (chỉ tách sub, không dịch/lồng).")
                self._refresh_host_cards()
            elif not self._dub_queue and not self._dub_running:
                # chẳng có gì lồng
                self._refresh_host_cards()
                if self._render_after_dub:
                    self._verify_before_render()

    def _refresh_host_cards(self):
        """Cho mỗi card trong hàng đợi tự dò lại sub trên đĩa (bản Việt hoặc
        sub gốc vừa tách) và cập nhật nhãn — để nhìn card là biết tập nào đã
        có sub, tập nào chưa."""
        cards = getattr(self.host, "cards", []) or []
        for c in cards:
            if hasattr(c, "refresh_srt_from_disk"):
                try:
                    c.refresh_srt_from_disk()
                except Exception:
                    pass
            else:
                # Card kiểu cũ: cập nhật thủ công bản _vi.srt
                vp = getattr(c, "video_path", None)
                if not vp:
                    continue
                vi = self._vi_srt_for(vp)
                if os.path.exists(vi) and getattr(c, "srt_path", None) != vi:
                    c.srt_path = vi
                    if hasattr(c, "lbl_srt"):
                        try:
                            c.lbl_srt.setText(os.path.basename(vi))
                            c.lbl_srt.setStyleSheet("color:#10B981; font-size:9px; border:none;")
                        except Exception:
                            pass

    def _start_card_poll(self):
        """Bật đồng hồ cập nhật nhãn sub trên card mỗi 2.5s trong lúc pipeline
        chạy, để card phản ánh sub vừa tách/dịch xong ngay, không đợi tới cuối."""
        if getattr(self, "_card_poll_timer", None) is None:
            self._card_poll_timer = QTimer(self)
            self._card_poll_timer.timeout.connect(self._refresh_host_cards)
        if not self._card_poll_timer.isActive():
            self._card_poll_timer.start(2500)

    def _stop_card_poll(self):
        t = getattr(self, "_card_poll_timer", None)
        if t is not None and t.isActive():
            t.stop()
        self._refresh_host_cards()   # cập nhật lần cuối cho chắc

    # ────────────────────────────────────────────────────────────────────
    #  ĐỒNG BỘ GEMINI (đăng nhập 1 lần + chọn prompt) — bản gọn
    # ────────────────────────────────────────────────────────────────────
    def _refresh_gemini_btn(self):
        try:
            logged = os.path.exists(AUTH_FILE)
        except Exception:
            logged = False
        if logged:
            self.btn_gemini.setText("🟢 Gemini đã đăng nhập")
            self.btn_gemini.setStyleSheet(
                "QPushButton { background:#16a34a; color:white; padding:7px; border-radius:6px; font-weight:bold; border:none; }"
                "QPushButton:hover { background:#15803d; }")
        else:
            self.btn_gemini.setText("🔑 Đồng bộ Gemini")
            self.btn_gemini.setStyleSheet(
                "QPushButton { background:#7c3aed; color:white; padding:7px; border-radius:6px; font-weight:bold; border:none; }"
                "QPushButton:hover { background:#6d28d9; }")

    def _open_gemini_sync(self):
        if not _GEMINI_AVAILABLE or GoogleManualLoginThread is None:
            QMessageBox.warning(self, "Thiếu module",
                                "Không tìm thấy translate_tab.py cạnh app.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Đồng bộ Gemini")
        dlg.setMinimumWidth(460)
        dlg.setStyleSheet(
            "QDialog { background:#0f172a; } QLabel { color:#e2e8f0; } "
            "QComboBox, QTextEdit { background:#1e293b; color:#e2e8f0; border:1px solid #334155; border-radius:6px; padding:6px; } "
            "QPushButton { padding:8px 14px; border:none; border-radius:6px; font-weight:bold; color:white; }")
        lay = QVBoxLayout(dlg)

        logged = os.path.exists(AUTH_FILE)
        lbl_status = QLabel("🟢 Đã đăng nhập Gemini" if logged else "🔴 Chưa đăng nhập Gemini")
        lay.addWidget(lbl_status)

        btn_login = QPushButton("🔑 Đăng nhập Gemini (1 lần)")
        btn_login.setStyleSheet("background:#7c3aed;")
        lay.addWidget(btn_login)

        _s = QSettings("HongguoDownloader", "ClientApp")
        lay.addWidget(QLabel("Chọn prompt dịch:"))
        cb_preset = QComboBox()
        keys = list(PROMPT_PRESETS.keys()) + [CUSTOM_PROMPT_KEY]
        cb_preset.addItems(keys)
        saved = _s.value("trans_preset", keys[0] if keys else CUSTOM_PROMPT_KEY)
        cb_preset.setCurrentText(saved if saved in keys else CUSTOM_PROMPT_KEY)
        lay.addWidget(cb_preset)

        lay.addWidget(QLabel("Nội dung prompt:"))
        txt_preview = QTextEdit()
        txt_preview.setFixedHeight(150)
        lay.addWidget(txt_preview)
        saved_custom = _s.value("trans_custom_prompt", "")

        def _update():
            sel = cb_preset.currentText()
            if sel == CUSTOM_PROMPT_KEY:
                txt_preview.setReadOnly(False)
                txt_preview.setStyleSheet("background:#1e293b; color:#fde68a; border:2px solid #f59e0b; border-radius:6px; padding:6px;")
                if txt_preview.toPlainText() == "":
                    txt_preview.setPlainText(saved_custom)
            else:
                txt_preview.setReadOnly(True)
                txt_preview.setStyleSheet("background:#1e293b; color:#e2e8f0; border:1px solid #334155; border-radius:6px; padding:6px;")
                txt_preview.setPlainText(PROMPT_PRESETS.get(sel, ""))
        cb_preset.currentTextChanged.connect(lambda _: _update())
        _update()

        log_box = QTextEdit(); log_box.setReadOnly(True); log_box.setFixedHeight(60)
        lay.addWidget(log_box)

        def _do_login():
            btn_login.setEnabled(False)
            log_box.append("⏳ Đang mở trình duyệt đăng nhập...")
            self._gemini_login_thread = GoogleManualLoginThread()
            self._gemini_login_thread.log.connect(lambda m: log_box.append(m.strip()))
            def _fin(okk):
                btn_login.setEnabled(True)
                lbl_status.setText("🟢 Đã đăng nhập Gemini" if okk else "🔴 Đăng nhập thất bại")
                self._refresh_gemini_btn()
            self._gemini_login_thread.finished_signal.connect(_fin)
            self._gemini_login_thread.start()
        btn_login.clicked.connect(_do_login)

        btn_save = QPushButton("💾 Lưu & Đóng")
        btn_save.setStyleSheet("background:#16a34a;")
        def _save():
            sel = cb_preset.currentText()
            _s.setValue("trans_preset", sel)
            if sel == CUSTOM_PROMPT_KEY:
                _s.setValue("trans_custom_prompt", txt_preview.toPlainText().strip())
            dlg.accept()
        btn_save.clicked.connect(_save)
        lay.addWidget(btn_save)

        dlg.exec()
        self._refresh_gemini_btn()


def attach_dub_tab(render_widget):
    """Gắn tab con vào RenderWidget.tabs. Gọi 1 lần trong RenderWidget.__init__
    (sau khi self.tabs đã tồn tại)."""
    try:
        tabs = getattr(render_widget, "tabs", None)
        if tabs is None:
            return None
        w = DubFeatureWidget(render_widget)
        tabs.addTab(w, "🔤 Sub·Dịch·Lồng")
        render_widget.dub_feature_tab = w
        return w
    except Exception as e:  # noqa
        print(f"[WARN] Không gắn được tab lồng tiếng vào Render: {e}")
        return None
