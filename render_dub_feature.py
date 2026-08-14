# -*- coding: utf-8 -*-
"""
render_dub_feature.py
─────────────────────
Gắn thêm 1 tab con "🔤 Tách sub → Dịch → Lồng tiếng" vào RenderWidget.
"""

import os
import subprocess

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
from PyQt6.QtCore import Qt, QSettings, QTimer, QThread, pyqtSignal

# ── Whisper STT ────
try:
    from whisper_stt import WhisperSttThread, _HAS_FW as _WHISPER_AVAILABLE
    try:
        from whisper_stt import _FW_IMPORT_ERROR as _WHISPER_IMPORT_ERROR
    except Exception:
        _WHISPER_IMPORT_ERROR = ""
except Exception as _we:
    WhisperSttThread = None
    _WHISPER_AVAILABLE = False
    _WHISPER_IMPORT_ERROR = str(_we)

# ── Động cơ từ honggou_tab ──────────────
_ENGINE_OK = True
_ENGINE_ERR = ""
try:
    from honggou_tab import (
        SttBatchThread, DubThread, EDGE_TTS_VOICES,
        GeminiTranslateThread, DeepSeekTranslateThread,
        PROMPT_PRESETS, AUTH_FILE, _GEMINI_AVAILABLE,
        GoogleManualLoginThread,
    )
except Exception as e:
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

def _load_dub_voices(lang="vi", source="capcut"):
    """Nạp danh sách giọng theo nguồn. Trả về list các cặp (tên_hiển_thị, giá_trị_đầy_đủ):
       - tên_hiển_thị: chỉ tên gọn để hiện trong dropdown
       - giá_trị_đầy_đủ: chuỗi "🔊 Tên (Pekka) [pekka:id]" hoặc "🌟 Tên [code]" để app dùng
       - source='capcut': giọng miễn phí (CapCut)
       - source='pekka' : giọng Pekka (trả phí qua API)
    """
    lang = (lang or "vi").lower()
    source = (source or "capcut").lower()
    voices = []

    if source == "pekka":
        if lang == "en":
            raw = [
                ("Jessica", "7idd8r5DBSfrZ4zsvbG25J"),
                ("Theo",    "5ZsmEgM69V3DNJy6V1WP84"),
                ("Mark",    "qFeSMpoHP3ZhoDwXbe1354"),
                ("Alex",    "hUJaV4ijMC3oLYQEHygPJt"),
            ]
        else:
            raw = [
                ("Thư Review",           "orBfJ4Q68FyVbckjJgDvkj"),
                ("Phật Pháp",            "8u97ewbLyV5dwePspwJY1w"),
                ("Ngọc Huyền",           "mhsL3CPLxmLYdSTKp3GANz"),
                ("Minh Anh",             "cZgBA3YXc4tD8QiLJDvr4z"),
                ("Quang Anh",            "24oEtXGic7NhDjXzmDbDvt"),
                ("Adam 3",               "5r2MVjMfzwsSDzTpaLjbY9"),
                ("Chi Chi",              "nqak8C85bsAG5mihyunRkj"),
                ("Sarah",                "jQhKABCZ2B7L4zncWcNb4Q"),
                ("Quỳnh Giao",           "97zRSQPtS6Fg3KEKekxssu"),
            ]
        for name, vid in raw:
            display = f"🔊 {name}"
            value = f"🔊 {name} (Pekka) [pekka:{vid}]"
            voices.append((display, value))
    else:
        # Giọng CapCut miễn phí
        if lang == "en":
            raw = [
                ("Jessie",          "DiT_en_female_jessie"),
                ("Male Profess",    "en_us_007"),
                ("English",         "BV510_streaming"),
                ("American Female", "BV029_streaming"),
            ]
        else:
            raw = [
                ("Cô Gái Hoạt Ngôn", "BV074_streaming"),
                ("Thanh Niên Tự Tin","BV075_streaming"),
                ("Nhỏ Ngọt Ngào",    "BV421_vivn_streaming"),
                ("Giọng Bé",         "BV074_streaming_dsp"),
            ]
        for name, code in raw:
            display = f"🌟 {name}"
            value = f"🌟 {name} [{code}]"
            voices.append((display, value))

    return voices

# --------------------------------------------------------------------------- #
# LUỒNG TẢI DANH SÁCH GIỌNG PEKKA (API)                                       #
# --------------------------------------------------------------------------- #
class PekkaVoicesWorker(QThread):
    done = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, api_key):
        super().__init__()
        self.api_key = api_key

    def run(self):
        import requests
        try:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            items, page = [], 1
            while True:
                r = requests.get(
                    "https://voice.getpekka.com/api/v1/voices",
                    headers=headers,
                    params={"page": page, "limit": 50},
                    timeout=30,
                )
                if r.status_code != 200:
                    self.error.emit(f"HTTP {r.status_code}: {r.text[:100]}")
                    return
                data = r.json()
                items.extend(data.get("items", []))
                if not data.get("hasNext"):
                    break
                page += 1
                if page > 50:  # Chống lặp vô hạn
                    break
            self.done.emit(items)
        except Exception as e:
            self.error.emit(str(e))

# --------------------------------------------------------------------------- #
# LUỒNG TẢI GIỌNG MẪU CHUẨN QTHREAD (Chống kẹt)                               #
# --------------------------------------------------------------------------- #
class PekkaTestWorker(QThread):
    done = pyqtSignal(bool, str)

    def __init__(self, api_key, voice_code, out_file, test_text=None):
        super().__init__()
        self.api_key = api_key
        self.voice_code = voice_code
        self.out_file = out_file
        self.test_text = test_text or "Xin chào, đây là giọng đọc thử nghiệm của hệ thống."

    def run(self):
        import requests
        text_test = self.test_text
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        body = {"text": text_test, "voiceId": self.voice_code, "speed": 1.0}
        
        try:
            r = requests.post("https://voice.getpekka.com/api/v1/tts/sync", json=body, headers=headers, timeout=30)
            if r.status_code != 200:
                self.done.emit(False, f"Lỗi API {r.status_code}: {r.text[:100]}")
                return
                
            data = r.json()
            if "url" in data:
                url = data["url"]
                if url.startswith("/"): 
                    url = "https://voice.getpekka.com" + url
                
                dl_req = requests.get(url, timeout=30)
                if dl_req.status_code == 200:
                    with open(self.out_file, 'wb') as f:
                        f.write(dl_req.content)
                    self.done.emit(True, "")
                else:
                    self.done.emit(False, "Không tải được file audio từ server.")
            else:
                self.done.emit(False, "Server không trả về URL tải audio.")
        except Exception as e:
            self.done.emit(False, f"Lỗi kết nối: {str(e)}")

# --------------------------------------------------------------------------- #
# LUỒNG TẢI HÀNG LOẠT TOÀN BỘ GIỌNG MẪU VỀ MÁY (Tự động)                      #
# --------------------------------------------------------------------------- #
class PekkaBatchTestWorker(QThread):
    progress = pyqtSignal(int, int, str)
    log = pyqtSignal(str)
    done = pyqtSignal(int, int)

    def __init__(self, api_key, voice_list, save_dir, test_text=None):
        super().__init__()
        self.api_key = api_key
        self.voice_list = voice_list
        self.save_dir = save_dir
        self.test_text = test_text or "Xin chào, đây là giọng đọc thử nghiệm của hệ thống."
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        import requests, os, time
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        text_test = self.test_text
        succ = 0
        fail = 0

        for i, (v_name, v_code) in enumerate(self.voice_list):
            if self._stop: break
            self.progress.emit(i + 1, len(self.voice_list), v_name)
            
            out_file = os.path.join(self.save_dir, f"{v_code}.mp3")
            # Bỏ qua nếu file đã tồn tại và không bị lỗi (size > 1KB)
            if os.path.exists(out_file) and os.path.getsize(out_file) > 1000:
                succ += 1
                continue
            
            try:
                body = {"text": text_test, "voiceId": v_code, "speed": 1.0}
                r = requests.post("https://voice.getpekka.com/api/v1/tts/sync", json=body, headers=headers, timeout=20)
                if r.status_code == 200:
                    data = r.json()
                    url = data.get("url", "")
                    if url.startswith("/"): url = "https://voice.getpekka.com" + url
                    
                    dl = requests.get(url, timeout=20)
                    if dl.status_code == 200:
                        with open(out_file, 'wb') as f:
                            f.write(dl.content)
                        succ += 1
                    else:
                        fail += 1
                        self.log.emit(f"⚠️ Lỗi tải file: {v_name}")
                else:
                    fail += 1
                    self.log.emit(f"⚠️ Lỗi API {v_name}: HTTP {r.status_code}")
            except Exception as e:
                fail += 1
                self.log.emit(f"⚠️ Lỗi mạng {v_name}: {e}")
            
            # Ngủ 1.5 giây giữa mỗi file để không bị chặn do spam API quá nhanh
            time.sleep(1.5)
            
        self.done.emit(succ, fail)


class DubFeatureWidget(QWidget):
    """Tab con gắn vào RenderWidget. Lấy danh sách file từ host.cards."""

    def __init__(self, host):
        super().__init__()
        self.host = host                      
        self.settings = QSettings("HongguoDownloader", "RenderDubTab")
        self._stt_thread = None
        self._dub_thread = None
        self._gtrans_thread = None
        self._gemini_login_thread = None
        self._dub_queue = []
        self._dub_running = False
        self._auto_dub_on = False
        self._MAX_VERIFY_RETRY = 2        
        self._verify_round = 0            
        self._translate_failed = set()    
        self._skip_from_render = set()    
        self._bad_found = []              
        self._build_ui()

    def _style_num(self, spin):
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
        from PyQt6.QtGui import QPalette, QColor
        combo.setMaxVisibleItems(12)            # chỉ hiện 12 dòng rồi cuộn
        view = QListView()
        pal = view.palette()
        pal.setColor(QPalette.ColorRole.Base, QColor("#1C1D27"))
        pal.setColor(QPalette.ColorRole.Text, QColor("#E5E6E8"))
        pal.setColor(QPalette.ColorRole.Highlight, QColor("#31265C"))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
        view.setPalette(pal)
        view.setUniformItemSizes(True)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        view.setStyleSheet(
            "QListView { background:#1C1D27; color:#E5E6E8; border:1px solid #7452FF; outline:0; }"
            "QListView::item { color:#E5E6E8; height:26px; padding-left:8px; padding-right:8px; }"
            "QListView::item:selected { background:#31265C; color:#FFFFFF; }"
        )
        combo.setView(view)
        combo.setStyleSheet(
            "QComboBox { background:#11121A; border:1px solid #2D303D; padding:6px; "
            "color:#FFFFFF; border-radius:4px; font-weight:bold; combobox-popup:0; }"
            "QComboBox QAbstractItemView { background:#1C1D27; color:#E5E6E8; "
            "selection-background-color:#31265C; selection-color:#FFFFFF; }"
        )

    def _build_ui(self):
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
        root.setContentsMargins(8, 8, 8, 6)
        root.setSpacing(6)

        if not _ENGINE_OK:
            warn = QLabel(
                "⚠️ Không nạp được động cơ tách sub/dịch/lồng tiếng từ honggou_tab.\n"
                f"Chi tiết: {_ENGINE_ERR}"
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color:#F87171; font-weight:bold; padding:10px;")
            root.addWidget(warn)
            root.addStretch()
            return

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet("border:none; background:transparent;")
        inner = QWidget()
        inner.setMinimumWidth(300)      # co giãn theo tab, không ép cứng 360
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(10, 4, 10, 4)   # chừa lề 2 bên để chữ không bị cắt
        lay.setSpacing(5)
        scroll.setWidget(inner)
        root.addWidget(scroll, stretch=1)

        def _lbl(t):
            w = QLabel(t, styleSheet="color:#8A8D98; font-size:10px; border:none;")
            w.setMinimumWidth(58)   # đủ rộng để không bị cắt mất chữ đầu
            return w

        self.btn_gemini = QPushButton("🔑 Đồng bộ Gemini")
        self.btn_gemini.clicked.connect(self._open_gemini_sync)
        self.btn_gemini.setStyleSheet(
            "QPushButton { background:#7c3aed; color:white; padding:7px; border-radius:6px; font-weight:bold; border:none; }"
            "QPushButton:hover { background:#6d28d9; }")
        lay.addWidget(self.btn_gemini)
        self._refresh_gemini_btn()

        lay.addWidget(QLabel("① Tách phụ đề (STT)", styleSheet="font-weight:bold; color:#10B981; border:none;"))
        row_src = QHBoxLayout()
        row_src.addWidget(_lbl("Ngôn ngữ gốc:"))
        self.cmb_stt_src = QComboBox()
        self.cmb_stt_src.addItems(["zh-CN", "en-US", "ko-KR", "ja-JP", "vi-VN"])
        self._style_combo_popup(self.cmb_stt_src)
        self.cmb_stt_src.setCurrentText(self.settings.value("stt_src", "zh-CN"))
        row_src.addWidget(self.cmb_stt_src, 1)
        lay.addLayout(row_src)

        row_eng_stt = QHBoxLayout()
        row_eng_stt.addWidget(_lbl("Engine STT:"))
        self.cmb_stt_engine = QComboBox()
        self.cmb_stt_engine.addItems(["☁️ CapCut (video ngắn)", "💻 Whisper (video dài, offline)"])
        self._style_combo_popup(self.cmb_stt_engine)
        self.cmb_stt_engine.setCurrentText(self.settings.value("stt_engine", "☁️ CapCut (video ngắn)"))
        self.cmb_stt_engine.currentTextChanged.connect(self._on_stt_engine_changed)
        row_eng_stt.addWidget(self.cmb_stt_engine, 1)
        lay.addLayout(row_eng_stt)

        self.row_whisper_model = QHBoxLayout()
        self.row_whisper_model.addWidget(_lbl("Model:"))
        self.cmb_whisper_model = QComboBox()
        self.cmb_whisper_model.addItems(["small (nhẹ · EN tốt)", "medium (khuyên · ZH tốt)", "large-v3 (chính xác nhất)"])
        self._style_combo_popup(self.cmb_whisper_model)
        self.cmb_whisper_model.setCurrentText(self.settings.value("whisper_model", "medium (khuyên · ZH tốt)"))
        self.cmb_whisper_model.currentTextChanged.connect(self._on_whisper_model_changed)
        self.row_whisper_model.addWidget(self.cmb_whisper_model, 1)
        lay.addLayout(self.row_whisper_model)
        self._on_stt_engine_changed(self.cmb_stt_engine.currentText())

        lay.addWidget(QLabel("ℹ️ Nút 'LÀM TẤT CẢ' tự nhận diện sub sẵn có:\n"
                             "sub Việt → lồng luôn; sub Trung/khác → dịch; không có → tách.",
                             styleSheet="color:#64748b; font-size:9px; border:none;"))

        lay.addWidget(QLabel("② Dịch & Lồng tiếng", styleSheet="font-weight:bold; color:#10B981; border:none; margin-top:6px;"))

        row_tgt = QHBoxLayout()
        row_tgt.addWidget(_lbl("Ngôn ngữ đích:"))
        self.cmb_target_lang = QComboBox()
        self.cmb_target_lang.addItems(["🇻🇳 Tiếng Việt", "🇬🇧 Tiếng Anh"])
        self._style_combo_popup(self.cmb_target_lang)
        _saved_tgt = self.settings.value("target_lang", "vi")
        self.cmb_target_lang.setCurrentIndex(1 if _saved_tgt == "en" else 0)
        self.cmb_target_lang.currentIndexChanged.connect(self._on_target_lang_changed)
        row_tgt.addWidget(self.cmb_target_lang, 1)
        lay.addLayout(row_tgt)

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

        lay.addWidget(QLabel("③ Lồng tiếng (TTS)", styleSheet="font-weight:bold; color:#10B981; border:none; margin-top:6px;"))
        self.chk_auto_dub = QCheckBox("🎙 Có lồng tiếng (trong 'LÀM TẤT CẢ')")
        self.chk_auto_dub.setChecked(self.settings.value("auto_dub", True, type=bool))
        lay.addWidget(self.chk_auto_dub)

        # ── Nguồn giọng: Miễn phí (CapCut) hay Trả phí (Pekka API) ──────────────
        row_src = QHBoxLayout()
        row_src.addWidget(_lbl("Nguồn giọng:"))
        self.cmb_voice_source = QComboBox()
        self.cmb_voice_source.addItems(["🆓 CapCut (miễn phí)", "💎 Pekka (API - trả phí)"])
        self._style_combo_popup(self.cmb_voice_source)
        _saved_src = self.settings.value("voice_source", "capcut")
        self.cmb_voice_source.setCurrentIndex(1 if _saved_src == "pekka" else 0)
        self.cmb_voice_source.currentIndexChanged.connect(self._on_voice_source_changed)
        row_src.addWidget(self.cmb_voice_source, 1)
        lay.addLayout(row_src)

        row_voice = QHBoxLayout()
        row_voice.addWidget(_lbl("Giọng:"))
        self.cmb_dub_voice = QComboBox()
        _init_lang = "en" if self.settings.value("target_lang", "vi") == "en" else "vi"
        self._populate_voice_combo(_init_lang)
        self._style_combo_popup(self.cmb_dub_voice)
        saved_voice = self.settings.value("dub_voice", "")
        if saved_voice:
            i = self.cmb_dub_voice.findText(saved_voice)
            if i >= 0:
                self.cmb_dub_voice.setCurrentIndex(i)
        row_voice.addWidget(self.cmb_dub_voice, 1)

        # --- NÚT NGHE THỬ ---
        self.btn_test_voice = QPushButton("🔊 Nghe thử")
        self.btn_test_voice.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_test_voice.setStyleSheet(
            "QPushButton { background:#0ea5e9; color:white; border-radius:4px; font-weight:bold; padding: 6px 12px; border:none; }"
            "QPushButton:hover { background:#38bdf8; }"
        )
        self.btn_test_voice.clicked.connect(self._test_current_voice)
        row_voice.addWidget(self.btn_test_voice)
        # --------------------------------------

        lay.addLayout(row_voice)

        # ── Ô nhập Pekka API Key và Nút tải danh sách giọng ──────────────
        self.wdg_pekka_key = QWidget()
        lay_pekka = QHBoxLayout(self.wdg_pekka_key)
        lay_pekka.setContentsMargins(0, 0, 0, 0)
        
        _pekka_style = ("QLineEdit { background:#11121A; border:1px solid #2D303D; "
                       "padding:7px; color:white; border-radius:4px; font-weight:bold; }")
        self.txt_pekka_apikey = QLineEdit(self.settings.value("pekka_api_key", ""))
        self.txt_pekka_apikey.setPlaceholderText("Nhập Pekka API Key (sk_live_...)...")
        self.txt_pekka_apikey.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_pekka_apikey.setStyleSheet(_pekka_style)
        
        self.btn_load_pekka = QPushButton("🔄 Tải danh sách giọng")
        self.btn_load_pekka.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_load_pekka.setStyleSheet("QPushButton { background:#10B981; color:white; padding:7px 12px; border-radius:4px; font-weight:bold; border:none; } QPushButton:hover { background:#059669; }")
        self.btn_load_pekka.clicked.connect(self._fetch_pekka_voices)
        
        lay_pekka.addWidget(self.txt_pekka_apikey)
        lay_pekka.addWidget(self.btn_load_pekka)
        
        lay.addWidget(self.wdg_pekka_key)

        # ── Ô tìm kiếm / lọc giọng ──────────────
        self._all_pekka_items = []   # lưu toàn bộ giọng để lọc lại không cần gọi API
        self.wdg_voice_filter = QWidget()
        lay_flt = QHBoxLayout(self.wdg_voice_filter)
        lay_flt.setContentsMargins(0, 0, 0, 0)
        self.txt_voice_filter = QLineEdit()
        self.txt_voice_filter.setPlaceholderText("🔍 Gõ để lọc giọng (vd: Review, Adam, nữ...)")
        self.txt_voice_filter.setStyleSheet(_pekka_style)
        self.txt_voice_filter.textChanged.connect(self._apply_voice_filter)
        lay_flt.addWidget(self.txt_voice_filter)
        lay.addWidget(self.wdg_voice_filter)
        self.wdg_voice_filter.setVisible(False)   # chỉ hiện sau khi tải từ API
        
        self.cmb_dub_voice.currentTextChanged.connect(self._on_dub_voice_changed)
        self._on_dub_voice_changed(self.cmb_dub_voice.currentText())
        self._on_voice_source_changed()   # khởi tạo đúng nguồn giọng đang chọn

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

        lbl_note = QLabel("🚀 Nút 'LÀM TẤT CẢ QUY TRÌNH' nằm ở cột phải ngoài, "
                          "cạnh nút RENDER — dùng đúng cấu hình bên trong tab này.")
        lbl_note.setWordWrap(True)
        lbl_note.setStyleSheet("color:#F37021; font-weight:bold; font-size:10px; border:none;")
        root.addWidget(lbl_note)

        self.chk_auto_render = QCheckBox("🎬 Tự Render sau khi lồng tiếng xong (dùng cấu hình tab Thiết kế)")
        self.chk_auto_render.setChecked(self.settings.value("auto_render", True, type=bool))
        self.chk_auto_render.setStyleSheet("color:#10B981; font-weight:bold; font-size:10px;")
        root.addWidget(self.chk_auto_render)

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

        row_fix = QHBoxLayout()
        self.btn_find_bad = _mk_btn("🔎 Tìm file lỗi", "#B45309", "#92400E", self._find_bad_files)
        self.btn_fix_bad = _mk_btn("🔧 Fix file lỗi", "#B45309", "#92400E", self._fix_bad_files)
        self.btn_fix_bad.setEnabled(False) 
        row_fix.addWidget(self.btn_find_bad)
        row_fix.addWidget(self.btn_fix_bad)
        root.addLayout(row_fix)

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.document().setMaximumBlockCount(500)
        self.txt_log.setFixedHeight(120)
        self.txt_log.setStyleSheet(
            "background:#0B0E14; color:#A7F3D0; font-family:Consolas; font-size:10px; padding:5px; border:1px solid #1F222D;")
        root.addWidget(self.txt_log)

        self._on_engine_changed(self.cb_translate_engine.currentText())

    def _fetch_pekka_voices(self):
        """Xử lý sự kiện bấm nút Tải danh sách giọng Pekka"""
        api_key = self.txt_pekka_apikey.text().strip()
        if not api_key:
            QMessageBox.warning(self, "Lỗi", "Vui lòng nhập API Key trước khi tải danh sách giọng!")
            return
            
        self.btn_load_pekka.setEnabled(False)
        self.btn_load_pekka.setText("⏳ Đang tải...")
        
        self._voices_worker = PekkaVoicesWorker(api_key)
        self._voices_worker.done.connect(self._on_voices_loaded)
        self._voices_worker.error.connect(self._on_voices_error)
        self._voices_worker.start()

    def _on_voices_loaded(self, items):
        self.btn_load_pekka.setEnabled(True)
        self.btn_load_pekka.setText("🔄 Tải danh sách giọng")

        # Lưu lại toàn bộ để lọc mà không cần gọi API lại
        self._all_pekka_items = items or []
        self.wdg_voice_filter.setVisible(True)
        self.txt_voice_filter.blockSignals(True)
        self.txt_voice_filter.clear()
        self.txt_voice_filter.blockSignals(False)
        self._apply_voice_filter("")   # đổ đầy lần đầu

        QMessageBox.information(self, "Thành công", f"Đã tải thành công {len(items)} giọng từ Pekka!")

    def _apply_voice_filter(self, kw):
        kw = (kw or "").strip().lower()
        cur = self.cmb_dub_voice.currentText()

        self.cmb_dub_voice.blockSignals(True)
        self.cmb_dub_voice.clear()

        # 1. Giọng Pekka từ API.
        #    - Không gõ lọc: chỉ hiện 10 giọng đầu cho gọn.
        #    - Có gõ lọc: hiện tất cả giọng khớp từ khoá.
        added = 0
        for v in self._all_pekka_items:
            name = v.get('name', 'Unknown')
            vid = v.get('id', '')
            if kw:
                if kw not in name.lower():
                    continue
            else:
                if added >= 10:
                    break
            self.cmb_dub_voice.addItem(f"🔊 {name}")
            idx = self.cmb_dub_voice.count() - 1
            self.cmb_dub_voice.setItemData(
                idx, f"🔊 {name} (Pekka) [pekka:{vid}]", Qt.ItemDataRole.UserRole)
            added += 1

        self.cmb_dub_voice.blockSignals(False)

        # Giữ lựa chọn cũ nếu vẫn còn, không thì về đầu
        i = self.cmb_dub_voice.findText(cur)
        if i >= 0:
            self.cmb_dub_voice.setCurrentIndex(i)
        elif self.cmb_dub_voice.count() > 0:
            self.cmb_dub_voice.setCurrentIndex(0)
        self._on_dub_voice_changed(self.cmb_dub_voice.currentText())

    def _on_voices_error(self, err_msg):
        self.btn_load_pekka.setEnabled(True)
        self.btn_load_pekka.setText("🔄 Tải danh sách giọng")
        QMessageBox.warning(self, "Lỗi tải giọng", f"Không thể tải danh sách giọng:\n{err_msg}")

    def _test_sample_text(self):
        """Câu đọc thử theo ngôn ngữ đích."""
        if self._target_lang() == "en":
            return "Hello, this is a voice sample test of the system."
        return "Xin chào, đây là giọng đọc thử nghiệm của hệ thống."

    def _test_current_voice(self):
        sel_text = self._current_voice_value()
        if not sel_text: return
        
        if "[" in sel_text and sel_text.endswith("]"):
            voice_type = sel_text[sel_text.rfind("[")+1:-1]
        else:
            QMessageBox.information(self, "Lỗi", "Định dạng giọng không hợp lệ.")
            return

        import os
        base_dir = os.path.dirname(os.path.abspath(__file__))
        samples_dir = os.path.join(base_dir, "voice_samples")
        os.makedirs(samples_dir, exist_ok=True)
        _suffix = "_en" if self._target_lang() == "en" else "_vi"

        is_pekka = voice_type.startswith("pekka:")
        # Mã file: bỏ tiền tố "pekka:" nếu có, để tên file gọn
        code_for_file = voice_type[len("pekka:"):] if is_pekka else voice_type
        out_file = os.path.join(samples_dir, f"{code_for_file}{_suffix}.mp3")

        # 1. Nếu đã có file mẫu sẵn (đóng gói kèm hoặc đã tạo trước) → phát luôn
        if os.path.exists(out_file) and os.path.getsize(out_file) > 1000:
            try: os.startfile(out_file)
            except Exception as e: QMessageBox.warning(self, "Lỗi mở file", f"Chi tiết: {e}")
            return

        # 2. Giọng CapCut không tạo online tại đây → cần file mẫu có sẵn
        if not is_pekka:
            QMessageBox.information(
                self, "Chưa có mẫu",
                "Giọng CapCut này chưa có file mẫu trong thư mục 'voice_samples'.\n"
                "Vui lòng chạy script tạo mẫu (tao_mau_giong.py) để tạo sẵn, "
                "hoặc dùng giọng đã có mẫu.")
            return

        # 3. Giọng Pekka → tải mẫu online bằng API key
        voice_code = code_for_file
        api_key = self.txt_pekka_apikey.text().strip() if hasattr(self, "txt_pekka_apikey") else ""
        if not api_key:
            QMessageBox.warning(self, "Thiếu API Key", "Chưa có file mẫu của giọng này trên máy!\nVui lòng nhập Pekka API Key vào ô trống bên dưới để hệ thống tải file mẫu về.")
            return
            
        self.btn_test_voice.setEnabled(False)
        self.btn_test_voice.setText("⏳ Đang tải...")
        
        self._test_worker = PekkaTestWorker(api_key, voice_code, out_file, self._test_sample_text())
        self._test_worker.done.connect(self._on_test_done)
        self._test_worker.start()

    def _on_test_done(self, success, msg):
        self.btn_test_voice.setEnabled(True)
        self.btn_test_voice.setText("🔊 Nghe thử")
        if success:
            import os
            try: os.startfile(self._test_worker.out_file)
            except: pass
        else:
            QMessageBox.warning(self, "Thất bại", f"Không thể tải mẫu thử:\n{msg}")

    def _log(self, msg):
        self.txt_log.append(msg)
        self.txt_log.verticalScrollBar().setValue(self.txt_log.verticalScrollBar().maximum())

    def _keep_alive(self, th):
        if not hasattr(self, "_threads_alive"):
            self._threads_alive = []
        self._threads_alive.append(th)
        th.finished.connect(lambda: self._threads_alive.remove(th) if th in self._threads_alive else None)

    def _files_from_host(self, all_series=False):
        files = []
        if all_series and getattr(self.host, "_folder_tabs", None):
            for t in self.host._folder_tabs:
                for c in t.get("cards", []):
                    vp = getattr(c, "video_path", None)
                    if vp and os.path.exists(vp) and vp not in files:
                        files.append(vp)
            if files:
                return files
        cards = getattr(self.host, "cards", []) or []
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
        if hasattr(self, "cmb_target_lang"):
            s.setValue("target_lang", self._target_lang())
        if hasattr(self, "txt_pekka_apikey"):
            s.setValue("pekka_api_key", self.txt_pekka_apikey.text().strip())
        s.setValue("dub_rate", self.spn_dub_rate.value())
        s.setValue("tts_workers", self.spn_tts_workers.value())
        s.setValue("mute_original", self.chk_mute_original.isChecked())
        s.setValue("orig_volume", self.spn_orig_volume.value())
        s.setValue("remove_bgm", self.chk_remove_bgm.isChecked())
        s.setValue("use_gpu", self.chk_use_gpu.isChecked())
        if hasattr(self, "chk_auto_render"):
            s.setValue("auto_render", self.chk_auto_render.isChecked())

    def _target_lang(self):
        try:
            return "en" if self.cmb_target_lang.currentIndex() == 1 else "vi"
        except Exception:
            return "vi"

    def _target_lang_name(self):
        return "English" if self._target_lang() == "en" else "Vietnamese"

    def _voice_source(self):
        try:
            return "pekka" if self.cmb_voice_source.currentIndex() == 1 else "capcut"
        except Exception:
            return "capcut"

    def _current_voice_value(self):
        """Chuỗi giá trị đầy đủ (có mã trong [...]) của giọng đang chọn.
        Ưu tiên itemData; nếu không có (vd giọng tải từ API) thì dùng text."""
        try:
            data = self.cmb_dub_voice.currentData(Qt.ItemDataRole.UserRole)
            if data:
                return data
        except Exception:
            pass
        return self.cmb_dub_voice.currentText()

    def _populate_voice_combo(self, lang):
        from PyQt6.QtGui import QColor
        source = self._voice_source()
        self.cmb_dub_voice.clear()
        items = _load_dub_voices(lang, source)
        if not items and source == "pekka" and lang == "en":
            # Không có ID giọng Anh cứng → nhắc người dùng tải từ API
            self.cmb_dub_voice.addItem("⚠️ Bấm '🔄 Tải danh sách giọng' để lấy giọng tiếng Anh")
            return
        for display, value in items:
            if lang == "en":
                self.cmb_dub_voice.addItem("🇬🇧 " + display)
                idx = self.cmb_dub_voice.count() - 1
                self.cmb_dub_voice.setItemData(
                    idx, QColor("#38bdf8"), Qt.ItemDataRole.ForegroundRole)
            else:
                self.cmb_dub_voice.addItem(display)
                idx = self.cmb_dub_voice.count() - 1
            # Lưu chuỗi giá trị đầy đủ (có mã) vào itemData để app dùng
            self.cmb_dub_voice.setItemData(idx, value, Qt.ItemDataRole.UserRole)

    def _on_voice_source_changed(self, *_):
        source = self._voice_source()
        self.settings.setValue("voice_source", source)
        is_pekka = (source == "pekka")

        # Nạp lại combo giọng theo nguồn
        self._populate_voice_combo(self._target_lang())
        if self.cmb_dub_voice.count() > 0:
            self.cmb_dub_voice.setCurrentIndex(0)

        # Chỉ hiện ô API key + nút tải + ô lọc khi dùng Pekka
        if hasattr(self, "wdg_pekka_key"):
            self.wdg_pekka_key.setVisible(is_pekka)
        if hasattr(self, "wdg_voice_filter"):
            # ô lọc chỉ có ý nghĩa khi đã tải danh sách đầy đủ từ API
            self.wdg_voice_filter.setVisible(is_pekka and bool(self._all_pekka_items))

        self._on_dub_voice_changed(self.cmb_dub_voice.currentText())

    def _on_target_lang_changed(self, _idx):
        lang = self._target_lang()
        cur = self.cmb_dub_voice.currentText()
        self._populate_voice_combo(lang)
        i = self.cmb_dub_voice.findText(cur)
        if i >= 0:
            self.cmb_dub_voice.setCurrentIndex(i)
        self.settings.setValue("target_lang", lang)

    def _on_dub_voice_changed(self, text):
        # Việc ẩn/hiện ô API key nay do nguồn giọng (_on_voice_source_changed) quyết định.
        pass

    def _on_engine_changed(self, text):
        self.txt_ds_key.setVisible(text.startswith("🚀"))

    def _on_stt_engine_changed(self, text):
        is_whisper = text.startswith("💻")
        for i in range(self.row_whisper_model.count()):
            w = self.row_whisper_model.itemAt(i).widget()
            if w:
                w.setVisible(is_whisper)

    def _whisper_model_name(self):
        label = self.cmb_whisper_model.currentText()
        return label.split(" ", 1)[0].strip()

    @staticmethod
    def _whisper_model_downloaded(model_name):
        """Đoán model đã tải về máy chưa — dò cache HuggingFace của
        faster-whisper. Trả về True nếu tìm thấy thư mục model."""
        import os, glob
        home = os.path.expanduser("~")
        patterns = [
            os.path.join(home, ".cache", "huggingface", "hub",
                         f"models--Systran--faster-whisper-{model_name}"),
            os.path.join(home, ".cache", "huggingface", "hub",
                         f"*faster-whisper-{model_name}*"),
        ]
        for p in patterns:
            if glob.glob(p):
                return True
        return False

    def _on_whisper_model_changed(self, _text=None):
        """Cảnh báo dung lượng + thời gian tải khi khách chọn model nặng
        mà máy chưa có sẵn."""
        name = self._whisper_model_name()
        info = {
            "small":    ("~480 MB", "1–3 phút"),
            "medium":   ("~1.5 GB", "5–10 phút"),
            "large-v3": ("~3 GB",   "10–20 phút"),
        }
        if name not in info:
            return
        # Đã tải rồi thì thôi, không làm phiền
        if self._whisper_model_downloaded(name):
            return
        # small nhẹ, không cần cảnh báo mạnh
        if name == "small":
            return

        size, dur = info[name]
        QMessageBox.information(
            self, "Model cần tải về (chỉ 1 lần)",
            f"Bạn chọn model '{name}' ({size}).\n\n"
            f"Lần đầu dùng, app sẽ TỰ TẢI model này về máy — mất khoảng {dur} "
            f"tùy tốc độ mạng. Trong lúc tải, giao diện có thể như đứng yên, "
            f"đây là bình thường, hãy CHỜ và GIỮ MẠNG ổn định.\n\n"
            f"Tải xong sẽ lưu lại, các lần sau dùng ngay không cần tải nữa.\n\n"
            f"💡 Máy yếu hoặc mạng chậm nên chọn 'small' (~480 MB) — vẫn tách "
            f"phụ đề tốt.")

    def _try_install_vc_redist(self):
        """Tìm vc_redist.x64.exe (đi kèm gói) và mời khách cài để sửa lỗi
        Whisper thiếu Visual C++ Runtime. Không cần 'pip install'."""
        import os, sys, glob, subprocess
        # Tìm vc_redist ở cạnh exe / cạnh module / thư mục cha
        candidates = []
        try:
            candidates.append(os.path.dirname(os.path.abspath(sys.executable)))
        except Exception:
            pass
        try:
            candidates.append(os.path.dirname(os.path.abspath(__file__)))
        except Exception:
            pass
        candidates.append(os.getcwd())

        vc_path = None
        seen = set()
        for base in candidates:
            if not base or base in seen:
                continue
            seen.add(base)
            for pat in ("vc_redist.x64.exe", os.path.join("**", "vc_redist.x64.exe")):
                hits = glob.glob(os.path.join(base, pat), recursive=True)
                if hits:
                    vc_path = hits[0]
                    break
            if vc_path:
                break

        if not vc_path:
            self._log("   ➤ Thiếu Visual C++ Runtime. Không tìm thấy 'vc_redist.x64.exe' "
                      "trong thư mục app — hãy cập nhật app lên bản mới nhất rồi thử lại.")
            return

        ret = QMessageBox.question(
            self, "Cần cài thư viện hệ thống",
            "Whisper cần Visual C++ Runtime (Microsoft) nhưng máy chưa có.\n\n"
            "File cài đã đi kèm sẵn trong app. Cài ngay bây giờ?\n"
            "(Chỉ mất ~30 giây, cài xong mở lại app là dùng được Whisper.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            self._log("   ➤ Bạn đã bỏ qua. Có thể cài thủ công 'vc_redist.x64.exe' "
                      "trong thư mục app bất cứ lúc nào.")
            return

        try:
            self._log("   ⏳ Đang cài Visual C++ Runtime...")
            _flags = 0x08000000 if os.name == "nt" else 0
            subprocess.Popen([vc_path, "/install", "/quiet", "/norestart"],
                             creationflags=_flags)
            self._log("   ✅ Đã khởi chạy trình cài. Cài xong, hãy ĐÓNG và MỞ LẠI app "
                      "rồi bấm Tách sub lại.")
            QMessageBox.information(
                self, "Đang cài",
                "Trình cài Visual C++ Runtime đang chạy.\n"
                "Sau khi cài xong, hãy ĐÓNG và MỞ LẠI app rồi thử lại Whisper.")
        except Exception as e:
            self._log(f"   ❌ Không chạy được trình cài: {e}")
            self._log(f"   ➤ Hãy mở thủ công: {vc_path}")

    @staticmethod
    def _detect_srt_lang(srt_path):
        try:
            with open(srt_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception:
            return "other"
        import re
        text = re.sub(r"\d+:\d+:\d+[,\.]\d+\s*-->\s*\d+:\d+:\d+[,\.]\d+", "", text)
        han = 0
        viet = 0
        VIET_CHARS = set("ăâđêôơưÀÁẢÃẠàáảãạ ăằắẳẵặâầấẩẫậ đèéẻẽẹêềếểễệ ìíỉĩị "
                         "òóỏõọôồốổỗộơờớởỡợ ùúủũụưừứửữự ỳýỷỹỵ".replace(" ", ""))
        for ch in text:
            o = ord(ch)
            if 0x4E00 <= o <= 0x9FFF:
                han += 1
            elif ch in VIET_CHARS or ch.lower() in VIET_CHARS:
                viet += 1
        if han >= 5 and han > viet:
            return "zh"
        if viet >= 3 and viet >= han:
            return "vi"
        return "other"

    @staticmethod
    def _orig_stem_for(video_path):
        stem, _ext = os.path.splitext(video_path)
        if stem.endswith("_dubbed"):
            return stem[:-len("_dubbed")]
        return stem

    def _tgt_suffix(self):
        return "_en" if self._target_lang() == "en" else "_vi"

    def _vi_srt_for(self, video_path):
        return self._orig_stem_for(video_path) + self._tgt_suffix() + ".srt"

    def _find_existing_srt(self, video_path):
        base = self._orig_stem_for(video_path)
        tgt = base + self._tgt_suffix() + ".srt"
        raw = base + ".srt"
        if os.path.exists(tgt):
            lang = self._detect_srt_lang(tgt)
            return tgt, lang
        if os.path.exists(raw):
            lang = self._detect_srt_lang(raw)
            return raw, lang
        return None, None

    @staticmethod
    def _srt_is_empty(srt_path):
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
                return False
            return True
        except Exception:
            return False

    def _prepare_no_dialogue(self, video_path, srt_path=None):
        import shutil
        vi = self._vi_srt_for(video_path)
        try:
            if srt_path and os.path.exists(srt_path) and srt_path != vi:
                shutil.copyfile(srt_path, vi)
            elif not os.path.exists(vi):
                with open(vi, "w", encoding="utf-8") as f:
                    f.write("")
        except Exception as e:
            self._log(f"⚠️ {os.path.basename(video_path)}: không tạo được _vi.srt rỗng: {e}")
            return False

        if self._auto_dub_on:
            stem = os.path.splitext(video_path)[0]
            if stem.endswith("_dubbed"):
                return True
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
                    cmd = [ff, "-y", "-i", video_path,
                           "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                           "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                           "-movflags", "+faststart", dub]
                    self._log(f"🔇 {os.path.basename(video_path)}: tập không thoại → tắt tiếng gốc cho đồng bộ.")
                elif abs(orig_v - 1.0) < 0.001:
                    shutil.copyfile(video_path, dub)
                    self._log(f"🔇 {os.path.basename(video_path)}: tập không thoại → giữ tiếng gốc 100%.")
                    return True
                else:
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
        if hasattr(self, "btn_fix_bad"):
            self.btn_fix_bad.setEnabled(on and bool(self._bad_found))
        ext = getattr(self.host, "btn_full_pipeline", None)
        if ext is not None:
            ext.setEnabled(on)

    def _run_only_stt(self):
        files = self._files_from_host()
        if not files:
            QMessageBox.warning(self, "Không có file", "Hàng đợi Render đang trống!")
            return
        self._save_settings()
        self._chain_after_stt = None
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
                if _WHISPER_IMPORT_ERROR:
                    # Thư viện CÓ trong gói nhưng nạp lỗi — thường thiếu
                    # Visual C++ Runtime (ctranslate2) hoặc thiếu DLL.
                    self._log("❌ Whisper không dùng được — thư viện có sẵn nhưng nạp lỗi.")
                    self._log(f"   Lý do: {_WHISPER_IMPORT_ERROR}")
                    self._try_install_vc_redist()
                else:
                    self._log("❌ Chưa cài faster-whisper. Mở CMD chạy:  "
                              "pip install faster-whisper  — rồi thử lại.")
                self._set_buttons_enabled(True)
                self._stop_card_poll()
                return
            model_name = self._whisper_model_name()
            self._log(f"🧠 Dùng Whisper (model '{model_name}') — chạy được video dài.")
            if not self._whisper_model_downloaded(model_name):
                self._log(f"⏬ Model '{model_name}' chưa có trên máy — sẽ TỰ TẢI về "
                          f"lần đầu (giữ mạng ổn định, giao diện có thể như đứng yên, "
                          f"hãy chờ). Lần sau dùng ngay không cần tải lại.")
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
            chain(ok, failed)
        else:
            self._stop_card_poll()

    def _run_only_translate(self):
        files = self._files_from_host()
        if not files:
            QMessageBox.warning(self, "Không có file", "Hàng đợi Render đang trống!")
            return
        self._save_settings()

        to_translate = []
        for vp in files:
            srt, lang = self._find_existing_srt(vp)
            if not srt:
                self._log(f"⏭ Bỏ qua (chưa có srt): {os.path.basename(vp)}")
                continue
            if self._srt_is_empty(srt):
                self._log(f"🔇 {os.path.basename(vp)}: tập không thoại — bỏ qua dịch.")
                continue
            tgt = self._target_lang()
            tgt_name = "tiếng Anh" if tgt == "en" else "tiếng Việt"
            if lang == tgt:
                dst = self._vi_srt_for(vp)
                if srt != dst:
                    try:
                        import shutil
                        shutil.copyfile(srt, dst)
                    except Exception:
                        pass
                self._log(f"✅ {os.path.basename(vp)}: sub đã là {tgt_name} — bỏ qua dịch.")
            else:
                src_desc = ("tiếng Trung" if lang == "zh" else
                            "tiếng Việt" if lang == "vi" else "ngôn ngữ khác")
                self._log(f"🌐 {os.path.basename(vp)}: sub {src_desc} → sẽ dịch sang {tgt_name}.")
                to_translate.append((vp, srt))

        if not to_translate:
            self._log("🎉 Không có gì cần dịch. Xong.")
            self._refresh_host_cards()
            return
        self._auto_dub_on = False
        self._chain_dub_after_translate = False
        self._start_translate(to_translate)

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
            self._gtrans_thread = DeepSeekTranslateThread(queue, api_key=key, full_series_mode=False, target_lang=self._target_lang())
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
            _tgt = self._target_lang()
            self._log(f"🌐 Dịch bằng Gemini sang {'tiếng Anh' if _tgt=='en' else 'tiếng Việt'}...")
            self._gtrans_thread = GeminiTranslateThread(
                queue, preset, "Auto (Mặc định)", 80,
                translate_workers=workers, show_browser=show_browser,
                target_lang=_tgt)

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
        if not self._dub_queue and not self._dub_running:
            self._set_buttons_enabled(True)
            self._stop_card_poll()
            self._log("✅ Lồng tiếng xong toàn bộ.")
            if getattr(self, "_render_after_dub", False):
                self._verify_before_render()

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
            srt, lang = self._find_existing_srt(vp)
            if srt and lang == self._target_lang():
                try:
                    import shutil
                    shutil.copyfile(srt, vi)
                    ready.append(vp)
                except Exception:
                    self._log(f"⏭ {os.path.basename(vp)}: không copy được sub.")
            else:
                _tn = "tiếng Anh" if self._target_lang() == "en" else "tiếng Việt"
                self._log(f"⏭ Bỏ qua (chưa có sub {_tn}): {os.path.basename(vp)}")

        if not ready:
            QMessageBox.warning(self, "Chưa có sub để lồng",
                                f"Không tập nào có sub {'tiếng Anh' if self._target_lang()=='en' else 'tiếng Việt'} để lồng.\nHãy dịch trước.")
            return
        self._render_after_dub = False
        self._dub_queue = list(ready)
        self._dub_running = False
        self._set_buttons_enabled(False)
        self._start_card_poll()
        self._log(f"③ Lồng tiếng {len(ready)} tập...")
        self._pump_dub_queue()

    def _pump_dub_queue(self):
        if self._dub_running or not self._dub_queue:
            return
        video_path = self._dub_queue.pop(0)
        vi_srt = self._vi_srt_for(video_path)
        if not os.path.exists(vi_srt):
            QTimer.singleShot(120, self._pump_dub_queue)
            return
        self._dub_running = True

        sel = self._current_voice_value()
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
        _pekka_apikey = self.txt_pekka_apikey.text().strip() if hasattr(self, "txt_pekka_apikey") else ""
        self._dub_thread = DubThread(
            [{"video": video_path, "srt": vi_srt}],
            voice_type=voice_type, rate=rate, pitch=pitch,
            mute_original=mute_orig, orig_volume=orig_vol,
            remove_bgm=remove_bgm, use_gpu=use_gpu, tts_workers=tts_workers,
            pekka_api_key=_pekka_apikey)
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

    def _run_only_render(self):
        self._refresh_host_cards()
        self._start_render()

    def _find_bad_files(self):
        files = self._files_from_host()
        if not files:
            QMessageBox.warning(self, "Không có file", "Hàng đợi Render đang trống!")
            return
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
        self._fix_only_mode = True
        self._render_after_dub = False
        self._verify_round = 0
        self._skip_from_render = set()
        self._log(f"🔧 Bắt đầu FIX {len(videos)} tập lỗi (làm lại dịch → lồng, tối đa "
                  f"{self._MAX_VERIFY_RETRY} vòng)...")
        self._start_verify_retry(videos)

    def _dubbed_for(self, video_path):
        stem, ext = os.path.splitext(video_path)
        if stem.endswith("_dubbed"):
            return video_path
        return stem + "_dubbed" + ext

    def _episode_incomplete(self, video_path):
        vi = self._vi_srt_for(video_path)
        if not os.path.exists(vi):
            srt, _lang = self._find_existing_srt(video_path)
            if srt and self._srt_is_empty(srt):
                self._prepare_no_dialogue(video_path, srt)
            else:
                return "chưa có sub Việt (dịch lỗi/chưa dịch)"
        if getattr(self, "_auto_dub_on", False):
            dub = self._dubbed_for(video_path)
            if not os.path.exists(dub):
                if self._srt_is_empty(vi):
                    self._prepare_no_dialogue(video_path, vi)
                    dub = self._dubbed_for(video_path)
                if not os.path.exists(dub):
                    return "chưa lồng tiếng"
        return None

    def _verify_before_render(self):
        files = self._files_from_host()
        bad = []
        for vp in files:
            if vp in self._skip_from_render:
                continue
            reason = self._episode_incomplete(vp)
            if reason:
                bad.append((vp, reason))

        if not bad:
            self._verify_round = 0
            if getattr(self, "_fix_only_mode", False):
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

        for vp, reason in bad:
            self._log(f"🔎 Chưa đạt: {os.path.basename(vp)} — {reason}")

        if self._verify_round >= self._MAX_VERIFY_RETRY:
            still_bad = [os.path.basename(v) for v, _r in bad]
            if getattr(self, "_fix_only_mode", False):
                self._fix_only_mode = False
                self._verify_round = 0
                self._bad_found = list(bad)
                self._log(f"⛔ Đã thử làm lại {self._MAX_VERIFY_RETRY} vòng nhưng "
                          f"{len(bad)} tập vẫn lỗi: " + ", ".join(still_bad))
                self._log("   → Kiểm tra file srt gốc / đăng nhập Gemini rồi bấm '🔧 Fix' lại.")
                self._set_buttons_enabled(True)
                self._stop_card_poll()
                return
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

        self._verify_round += 1
        self._log(f"🔁 RETRY vòng {self._verify_round}/{self._MAX_VERIFY_RETRY} "
                  f"cho {len(bad)} tập lỗi (làm lại dịch → lồng tiếng)...")
        self._start_verify_retry([vp for vp, _r in bad])

    def _start_verify_retry(self, videos):
        self._translate_failed.clear()
        self._dub_queue = []
        self._dub_running = False

        need_translate = []
        need_dub_only = []

        for vp in videos:
            vi = self._vi_srt_for(vp)
            srt0, _l0 = self._find_existing_srt(vp)
            if (srt0 and self._srt_is_empty(srt0)) or (os.path.exists(vi) and self._srt_is_empty(vi)):
                self._prepare_no_dialogue(vp, srt0 or vi)
                continue
            if not os.path.exists(vi):
                srt, lang = self._find_existing_srt(vp)
                if srt and lang != self._target_lang():
                    need_translate.append((vp, srt))
                elif srt and lang == self._target_lang() and srt != vi:
                    try:
                        import shutil
                        shutil.copyfile(srt, vi)
                        if self._auto_dub_on:
                            need_dub_only.append(vp)
                    except Exception:
                        self._log(f"⏭ {os.path.basename(vp)}: không copy được sub.")
                else:
                    self._log(f"⏭ {os.path.basename(vp)}: không có srt nguồn để dịch lại → bỏ.")
                    self._skip_from_render.add(vp)
            else:
                if self._auto_dub_on:
                    need_dub_only.append(vp)

        self._set_buttons_enabled(False)
        self._start_card_poll()

        if need_translate:
            self._chain_dub_after_translate = self._auto_dub_on
            self._start_translate(need_translate)
            if self._auto_dub_on and need_dub_only:
                for vp in need_dub_only:
                    if vp not in self._dub_queue:
                        self._dub_queue.append(vp)
                self._pump_dub_queue()
        elif self._auto_dub_on and need_dub_only:
            for vp in need_dub_only:
                if vp not in self._dub_queue:
                    self._dub_queue.append(vp)
            self._pump_dub_queue()
        else:
            QTimer.singleShot(200, self._verify_before_render)

    def _exclude_skipped_cards(self):
        if not self._skip_from_render:
            return
        cards = getattr(self.host, "cards", None)
        if not isinstance(cards, list):
            return
        removed = []
        for c in list(cards):
            vp = getattr(c, "video_path", None)
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

    def _run_full_pipeline(self, all_series=False):
        files = self._files_from_host(all_series=all_series)
        if not files:
            QMessageBox.warning(self, "Không có file", "Hàng đợi Render đang trống!")
            return
        self._save_settings()
        self.txt_log.clear()
        self._verify_round = 0
        self._translate_failed = set()
        self._skip_from_render = set()
        _scope = "TẤT CẢ CÁC BỘ" if all_series else "bộ đang mở"
        self._log(f"🚀 LÀM TẤT CẢ ({_scope}) với {len(files)} video. Đang phân loại...")

        self._auto_dub_on = self.chk_auto_dub.isChecked()

        need_stt = []
        need_translate = []
        ready_vi = []
        no_dialogue = []

        for vp in files:
            srt, lang = self._find_existing_srt(vp)
            if not srt:
                need_stt.append(vp)
            elif self._srt_is_empty(srt):
                if self._prepare_no_dialogue(vp, srt):
                    no_dialogue.append(vp)
                else:
                    need_stt.append(vp)
            elif lang == self._target_lang():
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

        self._auto_dub_on = self.chk_auto_dub.isChecked()
        self._chain_dub_after_translate = self._auto_dub_on
        if all_series:
            self._render_after_dub = False
            self._log("   ℹ️ Chế độ tất cả các bộ: sẽ tách→dịch→lồng cho mọi bộ. "
                      "Render riêng bằng nút 🎬 RENDER TẤT CẢ CÁC BỘ (đúng cấu hình từng bộ).")
        else:
            self._render_after_dub = (self._auto_dub_on and self.chk_auto_render.isChecked())
        self._dub_queue = []
        self._dub_running = False

        if self._auto_dub_on:
            for vp in ready_vi:
                self._dub_queue.append(vp)

        self._full_need_translate = list(need_translate)
        self._full_ready_vi = list(ready_vi)

        if need_stt:
            def _after_stt(ok, failed):
                for vp in need_stt:
                    srt, lang = self._find_existing_srt(vp)
                    if not srt:
                        self._log(f"⚠️ {os.path.basename(vp)}: tách sub thất bại, bỏ qua.")
                        continue
                    if self._srt_is_empty(srt):
                        self._prepare_no_dialogue(vp, srt)
                        continue
                    if lang == self._target_lang():
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
        if self._auto_dub_on and self._dub_queue:
            self._pump_dub_queue()

        if self._full_need_translate:
            self._chain_dub_after_translate = self._auto_dub_on
            self._start_translate(self._full_need_translate)
        else:
            if not self._auto_dub_on:
                self._log("🎉 Hoàn tất (chỉ tách sub, không dịch/lồng).")
                self._refresh_host_cards()
            elif not self._dub_queue and not self._dub_running:
                self._refresh_host_cards()
                if self._render_after_dub:
                    self._verify_before_render()

    def _refresh_host_cards(self):
        cards = getattr(self.host, "cards", []) or []
        for c in cards:
            if hasattr(c, "refresh_srt_from_disk"):
                try:
                    c.refresh_srt_from_disk()
                except Exception:
                    pass
            else:
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
        if getattr(self, "_card_poll_timer", None) is None:
            self._card_poll_timer = QTimer(self)
            self._card_poll_timer.timeout.connect(self._refresh_host_cards)
        if not self._card_poll_timer.isActive():
            self._card_poll_timer.start(2500)

    def _stop_card_poll(self):
        t = getattr(self, "_card_poll_timer", None)
        if t is not None and t.isActive():
            t.stop()
        self._refresh_host_cards()

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
    try:
        tabs = getattr(render_widget, "tabs", None)
        if tabs is None:
            return None
        w = DubFeatureWidget(render_widget)
        _wrap = getattr(render_widget, "_wrap_scroll", None)
        if _wrap is not None:
            tabs.addTab(_wrap(w), "🔤 Sub·Dịch·Lồng")
        else:
            tabs.addTab(w, "🔤 Sub·Dịch·Lồng")
        render_widget.dub_feature_tab = w
        return w
    except Exception as e:
        print(f"[WARN] Không gắn được tab lồng tiếng vào Render: {e}")
        return None
