"""
═══════════════════════════════════════════════════════════
  RENDER TAB — Tab render/edit video cho Hongguo
  ─────────────────────────────────────────────────────────
  Tách từ workflow_tab (bỏ Dịch + TTS), chỉ giữ:
    • Grid tự nhận diện & ghép cặp video + srt (ưu tiên
      *_dubbed.mp4 + *_vi.srt, không có thì dùng gốc)
    • Panel Design: font/màu sub, khung mờ, overlay PNG,
      logo kênh, bộ lọc Bypass FX
    • Preview canvas (kéo thả chữ/logo, xem video)
    • Render hàng loạt bằng ffmpeg (GPU nếu có)
    • [MỚI] Tự động gộp trọn bộ video sau khi render xong
═══════════════════════════════════════════════════════════
"""
import os, sys, subprocess, re, shutil, time, tempfile, base64, threading, copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea, QFileDialog, QTextEdit, QProgressBar,
    QComboBox, QLineEdit, QSpinBox, QMessageBox, QCheckBox, QSlider,
    QTabWidget, QDoubleSpinBox, QGridLayout, QPlainTextEdit,
    QGraphicsScene, QGraphicsView, QGraphicsTextItem, QGraphicsRectItem,
    QGraphicsPixmapItem, QGraphicsItem, QStyle, QApplication, QDialog
)
from PyQt6.QtGui import QIcon
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSettings, QUrl, QPointF, QPoint, QRectF, QTimer, QSize, QFileSystemWatcher
from PyQt6.QtGui import QCursor, QTextCursor, QFont, QPixmap, QPen, QBrush, QColor, QPainter
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem

# Tiện ích dùng chung (ffmpeg path, codec, cờ ẩn cửa sổ). Ưu tiên lấy từ
# shared_utils của app; nếu chạy lẻ không có thì tự fallback.
try:
    from shared_utils import (get_ffmpeg_path, get_optimal_ffmpeg_codec,
                              CREATE_NO_WINDOW)
    try:
        from shared_utils import get_codec_fallback_reason
    except Exception:
        def get_codec_fallback_reason(): return ""
except Exception:
    CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
    def get_ffmpeg_path(): return shutil.which("ffmpeg") or "ffmpeg"
    def get_optimal_ffmpeg_codec(): return "libx264"
    def get_codec_fallback_reason(): return ""

# Mượn hằng số đăng nhập Gemini từ tab dịch (CHỈ mượn hằng số + auth file,
# KHÔNG import/đụng luồng dịch GeminiTranslateThread). Luồng tạo thumbnail
# bên dưới dùng Chrome ẩn ĐỘC LẬP bằng storage_state nên chạy song song
# luồng dịch mà không giành profile.
try:
    from translate_tab import AUTH_FILE, UA, BROWSER_ARGS
except Exception:
    AUTH_FILE = "gemini_auth.json"
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
    BROWSER_ARGS = ["--disable-blink-features=AutomationControlled", "--disable-gpu",
                    "--no-sandbox", "--disable-dev-shm-usage", "--disable-software-rasterizer"]


def build_video_encoder_args(codec, crf_val=20, preset_hw="quality", preset_sw="medium"):
    """Trả về list tham số encoder video ĐÚNG theo từng codec.

    Cùng logic với phần render từng tập: codec phần cứng (AMF/NVENC/QSV) KHÔNG
    dùng được '-crf' và '-preset medium' của libx264 — mỗi hãng có cờ riêng.
    Dùng chung cho cả GỘP TRỌN BỘ để không lặp lại lỗi
    'Unable to parse preset medium' trên h264_amf/hevc_amf.

    crf_val<=0 -> chế độ bitrate ~1000k (nhanh, chất lượng thấp).
    """
    c = (codec or "").lower()
    args = ["-c:v", codec]
    # Ép 8-bit 4:2:0: video nguồn 10-bit/4:4:4 khiến NVENC card cũ lỗi và file
    # không phát được trên nhiều điện thoại/TV/QuickTime (màn đen, chỉ có tiếng).
    args += ["-pix_fmt", "yuv420p"]
    use_crf = crf_val and crf_val > 0

    if "nvenc" in c:
        nv_preset = "hq" if preset_hw == "quality" else "fast"
        if use_crf:
            args += ["-rc", "constqp", "-qp", str(crf_val), "-preset", nv_preset]
        else:
            args += ["-b:v", "1000k", "-preset", nv_preset]
    elif "amf" in c:
        # AMD AMF: KHÔNG có -crf / -preset; dùng -rc cqp + -quality
        if use_crf:
            args += ["-rc", "cqp", "-qp_i", str(crf_val), "-qp_p", str(crf_val),
                     "-qp_b", str(crf_val), "-quality", preset_hw]
        else:
            args += ["-b:v", "1000k", "-quality", preset_hw]
    elif "qsv" in c:
        if use_crf:
            args += ["-global_quality", str(crf_val), "-preset", preset_hw]
        else:
            args += ["-b:v", "1000k", "-preset", preset_hw]
    else:
        # libx264/libx265 (CPU) — mới dùng -crf + -preset medium/slow...
        if use_crf:
            args += ["-crf", str(crf_val), "-preset", preset_sw]
        else:
            args += ["-b:v", "1000k", "-preset", preset_sw]
    return args


FONTS_LIST = ["Arial", "Tahoma", "Verdana", "Times New Roman", "Segoe UI", "Impact", "Consolas", "Courier New"]

COLOR_PRESETS = {
    "Vàng (Yellow)":    {"ass": "&H0000FFFF", "qt": "#FFFF00"},
    "Trắng (White)":    {"ass": "&H00FFFFFF", "qt": "#FFFFFF"},
    "Đỏ (Red)":         {"ass": "&H000000FF", "qt": "#FF0000"},
    "Xanh lá (Green)":  {"ass": "&H0000FF00", "qt": "#00FF00"},
    "Xanh biển (Blue)": {"ass": "&H00FF0000", "qt": "#0000FF"},
    "Cam (Orange)":     {"ass": "&H0000A5FF", "qt": "#FFA500"},
    "Hồng (Pink)":      {"ass": "&H00FF00FF", "qt": "#FF00FF"},
    "Đen (Black)":      {"ass": "&H00000000", "qt": "#000000"},
}


def _natural_key(s):
    """Khóa sắp xếp tự nhiên: tách chữ và số để '16' đứng trước '160',
    và 'Tap_2' đứng trước 'Tap_10'. Nhờ vậy gộp trọn bộ đúng thứ tự tập."""
    s = str(s)
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r'(\d+)', s)]

def format_time(seconds):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"

def _escape_ffmpeg_path(path):
    p = path.replace('\\', '/')
    for ch in [":", "'", "[", "]", ",", ";"]: p = p.replace(ch, f"\\{ch}")
    return p

def _srt_has_content(srt_path):
    """True nếu file .srt có ÍT NHẤT 1 dòng thoại thật (có timecode + chữ).
    Tập cảnh đánh nhau/không thoại thường cho .srt rỗng hoặc chỉ vài dòng
    trắng — ép sub bằng file này sẽ khiến FFmpeg lỗi 'Invalid data'. Dùng để
    bỏ qua bước ép sub cho tập không thoại (vẫn render giữ tiếng gốc)."""
    try:
        if not srt_path or not os.path.exists(srt_path):
            return False
        if os.path.getsize(srt_path) < 8:
            return False
        with open(srt_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        # phải có timecode
        if not re.search(r"\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->", text):
            return False
        # và phải có chữ (dòng không phải số thứ tự / không phải timecode / không trắng)
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.isdigit():
                continue
            if "-->" in s:
                continue
            return True   # có 1 dòng chữ thật
        return False
    except Exception:
        return False


def _merge_srt_intervals(srt_path, gap=0.3, expand=0.5, max_intervals=150):
    try:
        with open(srt_path, "r", encoding="utf-8") as f: srt_text = f.read()
        times = re.findall(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})", srt_text)
        if not times: return []
        def to_sec(t):
            h, m, s = t.replace(",", ".").split(":")
            return float(h)*3600 + float(m)*60 + float(s)
        raw = [(max(0.0, to_sec(s) - expand), to_sec(e) + expand) for s, e in times]
        raw.sort(key=lambda x: x[0])

        def _merge(items, g):
            out = [items[0]]
            for s, e in items[1:]:
                if s <= out[-1][1] + g: out[-1] = (out[-1][0], max(out[-1][1], e))
                else: out.append((s, e))
            return out

        merged = _merge(raw, gap)
        g = gap
        while len(merged) > max_intervals and g < 20.0:
            g += 0.5
            merged = _merge(raw, g)
        return merged
    except Exception: return []

# ============================================================
# CÁC LUỒNG XỬ LÝ
# ============================================================

# ============================================================
# LUỒNG TẠO THUMBNAIL BẰNG AI (Gemini web, độc lập luồng dịch)
# ============================================================
# Selector RIÊNG cho luồng ảnh (tách hẳn khỏi luồng dịch).
_THUMB_INPUT_SELS = [
    "rich-textarea div.ql-editor[contenteditable='true']",
    "div[contenteditable='true'][role='textbox']",
]
_THUMB_SEND_SELS = [
    "button[aria-label='Send message']",
    "button[aria-label='Gửi']",
    "button.send-button",
]
_THUMB_UPLOAD_TRIGGER_SELS = [
    # Gemini UI tiếng Việt (từ log thực tế)
    "button[aria-label='Nội dung tải lên và công cụ']",
    "button[aria-label='Mở trình đơn tải tệp lên']",
    "button[aria-label='Thêm tệp']",
    # Gemini UI tiếng Anh
    "button[aria-label='Open upload file menu']",
    "button[aria-label='Add files']",
    "button[aria-label='Upload files and tools']",
    # Fallback fuzzy match
    "button[aria-label*='upload' i]",
    "button[aria-label*='tải lên' i]",
    "button[aria-label*='tệp' i]",
    "button[aria-label*='công cụ' i]",
]
_THUMB_FILE_INPUT_SEL = "input[type='file']"
_THUMB_RESULT_IMG_SELS = [
    "message-content img",
    ".model-response-text img",
    "[data-message-author-role='model'] img",
    "generated-image img",
    "img[src^='https://']",
    "img[src^='blob:']",
    "img[src^='data:image']",
]


def _thumb_find_el(page, sels, timeout=6000, cancel_check=None):
    step = 500
    for s in sels:
        try:
            for _ in range(max(1, timeout // step)):
                if cancel_check and cancel_check():
                    return None
                el = page.query_selector(s)
                if el and el.is_visible():
                    return el
                page.wait_for_timeout(step)
        except Exception:
            continue
    return None


class GeminiThumbnailThread(QThread):
    """Tạo N thumbnail từ 1 ảnh gốc bằng Gemini web. Gửi từng lượt riêng
    (mỗi lượt 1 biến thể góc nhìn) -> thu 1 ảnh/lượt. Chrome ẩn độc lập
    (storage_state) nên không đụng luồng dịch."""
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    one_done = pyqtSignal(str)
    all_done = pyqtSignal(list)

    def __init__(self, src_image, base_prompt, out_dir, n_variants=4,
                 variant_hints=None, model_key="Auto (Mặc định)", show_browser=False):
        super().__init__()
        self.src_image = src_image
        self.base_prompt = base_prompt.strip()
        self.out_dir = out_dir
        self.n_variants = max(1, min(8, int(n_variants)))
        self.variant_hints = variant_hints or [
            "Phong cách RỒNG LỬA: rồng vàng-đỏ khổng lồ bằng lửa cuộn quanh phía sau, biển lửa dung nham, tàn lửa bay, khí thế bá vương ngút trời.",
            "Phong cách SẤM SÉT VẠN QUÂN: tia sét xanh-tím xé trời, luồng năng lượng điện bùng nổ, mây bão vần vũ, hào quang chớp giật hùng vĩ.",
            "Phong cách BĂNG PHONG THẦN GIỚI: phượng hoàng băng, bão tuyết, ánh sáng vàng kim thần thánh, cung điện trên mây, huyền ảo choáng ngợp.",
            "Phong cách HOÀNG KIM SỬ THI: hào quang vàng rực chói lòa, linh thú vàng cuộn quanh, tia sáng thần thánh tỏa, bụi vàng bay, khí thế đế vương.",
        ]
        self.model_key = model_key
        self.show_browser = bool(show_browser)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _type_and_send(self, page, prompt_text):
        inp = _thumb_find_el(page, _THUMB_INPUT_SELS, timeout=8000,
                             cancel_check=lambda: self._cancel)
        if not inp:
            return False, "Không thấy ô nhập (có thể dính CAPTCHA)."
        inp.click()
        page.evaluate('''(text) => {
            const el = document.activeElement?.contentEditable === "true"
                ? document.activeElement
                : document.querySelector("[contenteditable='true']");
            if (el) { el.focus(); el.innerText = text;
                      el.dispatchEvent(new Event('input', {bubbles: true})); }
        }''', prompt_text)
        page.wait_for_timeout(400)
        page.keyboard.press("End")

        def _input_text():
            try:
                return page.evaluate('''() => {
                    const el = document.querySelector("[contenteditable='true']");
                    return el ? (el.innerText || "").trim() : null;
                }''')
            except Exception:
                return None

        for attempt in range(4):
            if self._cancel:
                return False, "Đã hủy."
            btn = _thumb_find_el(page, _THUMB_SEND_SELS, timeout=2000,
                                 cancel_check=lambda: self._cancel)
            sent = False
            if btn:
                try:
                    if not btn.is_disabled() and btn.get_attribute("aria-disabled") != "true":
                        btn.click(); sent = True
                except Exception:
                    pass
            if not sent:
                try:
                    inp.click(); page.keyboard.press("Enter")
                except Exception:
                    pass
                page.wait_for_timeout(400)
                if _input_text():
                    try: page.keyboard.press("Control+Enter")
                    except Exception: pass
            page.wait_for_timeout(800)
            txt = _input_text()
            if txt is None or txt == "":
                return True, ""
            if attempt < 3:
                self.log.emit(f"↩️ Chưa gửi được, thử lại ({attempt+2}/4)...\n")
        return False, "Gemini không nhận prompt sau 4 lần."

    def _dump_buttons(self, page):
        """In ra mọi nút/aria-label để bắt đúng selector khi dò thất bại."""
        try:
            labels = page.evaluate('''() => {
                const out = [];
                const push = (root) => {
                    root.querySelectorAll('button, [role=button], input').forEach(el => {
                        const a = el.getAttribute('aria-label') || '';
                        const t = (el.tagName || '').toLowerCase();
                        const ty = el.getAttribute('type') || '';
                        if (a || t === 'input') out.push(`${t}${ty?('['+ty+']'):''} :: ${a}`);
                    });
                    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) push(el.shadowRoot); });
                };
                push(document);
                return [...new Set(out)];
            }''')
            self.log.emit("🔎 Các nút/input tìm thấy trên trang:\n")
            for l in labels[:40]:
                self.log.emit(f"    • {l}\n")
        except Exception as e:
            self.log.emit(f"⚠️ Không dò được nút: {e}\n")

    def _find_file_input_deep(self, page):
        """Tìm input[type=file] khắp trang KỂ CẢ trong shadow DOM.
        Trả về ElementHandle hoặc None."""
        try:
            handle = page.evaluate_handle('''() => {
                const find = (root) => {
                    let el = root.querySelector("input[type='file']");
                    if (el) return el;
                    const hosts = root.querySelectorAll('*');
                    for (const h of hosts) {
                        if (h.shadowRoot) {
                            const found = find(h.shadowRoot);
                            if (found) return found;
                        }
                    }
                    return null;
                };
                return find(document);
            }''')
            el = handle.as_element()
            return el
        except Exception:
            return None

    def _copy_image_to_clipboard(self, img_path):
        """Đưa ảnh vào clipboard hệ thống để dán bằng Ctrl+V.
        Windows: dùng PowerShell (không cần cài thêm gì).
        macOS/Linux: thử qua thư viện nếu có."""
        try:
            if os.name == "nt":
                ps = (
                    "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                    f"$img=[System.Drawing.Image]::FromFile('{img_path}');"
                    "[System.Windows.Forms.Clipboard]::SetImage($img);"
                )
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                                   startupinfo=si, capture_output=True, text=True, timeout=20)
                return r.returncode == 0
            else:
                # macOS: osascript; Linux: xclip (nếu có)
                if sys.platform == "darwin":
                    scpt = (f'set the clipboard to (read (POSIX file "{img_path}") as JPEG picture)')
                    r = subprocess.run(["osascript", "-e", scpt], capture_output=True, timeout=20)
                    return r.returncode == 0
                else:
                    mime = "image/png" if img_path.lower().endswith(".png") else "image/jpeg"
                    r = subprocess.run(["xclip", "-selection", "clipboard", "-t", mime, "-i", img_path],
                                       capture_output=True, timeout=20)
                    return r.returncode == 0
        except Exception as e:
            self.log.emit(f"⚠️ Không copy được ảnh vào clipboard: {e}\n")
            return False

    def _paste_image(self, page):
        """Dán ảnh từ clipboard vào ô nhập bằng Ctrl+V (né được input ẩn)."""
        if not self._copy_image_to_clipboard(self.src_image):
            return False
        inp = _thumb_find_el(page, _THUMB_INPUT_SELS, timeout=6000,
                             cancel_check=lambda: self._cancel)
        if not inp:
            return False
        try:
            inp.click()
            page.wait_for_timeout(400)
            page.keyboard.press("Control+V")
            # chờ ảnh preview đính kèm hiện ra (chip/thumbnail trong ô soạn)
            for _ in range(20):  # ~10s
                if self._cancel:
                    return False
                page.wait_for_timeout(500)
                has_att = page.evaluate('''() => {
                    // có thẻ img preview hoặc vùng đính kèm xuất hiện gần ô nhập
                    const imgs = document.querySelectorAll("img");
                    for (const im of imgs) {
                        const s = im.src || "";
                        if (s.startsWith("blob:") || s.startsWith("data:image")) return true;
                    }
                    return !!document.querySelector("[class*='attachment'], [class*='file-preview'], [data-test-id*='file']");
                }''')
                if has_att:
                    return True
            return False
        except Exception as e:
            self.log.emit(f"⚠️ Lỗi dán ảnh: {e}\n")
            return False

    # Selector các mục trong submenu sau khi bấm nút "Nội dung tải lên và công cụ"
    _SUBMENU_FILE_SELS = [
        # Mục "Tải tệp lên" / "Upload file" trong submenu Gemini
        "li[aria-label*='tệp' i]",
        "li[aria-label*='file' i]",
        "li[data-value*='file' i]",
        "[role='menuitem'][aria-label*='tệp' i]",
        "[role='menuitem'][aria-label*='file' i]",
        "[role='menuitem'][aria-label*='upload' i]",
        "[role='menuitem'][aria-label*='tải lên' i]",
        # Gemini hay dùng mat-menu-item hoặc div dạng này
        "button[aria-label*='Tải tệp lên' i]",
        "button[aria-label*='Upload file' i]",
        "span[aria-label*='Tải tệp lên' i]",
    ]

    def _attach_image(self, page):
        # CÁCH 0 (ưu tiên): set_input_files thẳng vào input[type=file] ẩn
        # (an toàn nhất, không phụ thuộc UI, không cần clipboard).
        fi = page.query_selector(_THUMB_FILE_INPUT_SEL) or self._find_file_input_deep(page)
        if fi:
            try:
                fi.set_input_files(self.src_image)
                page.wait_for_timeout(2500)
                # Kiểm tra có preview ảnh xuất hiện không
                has_att = page.evaluate('''() => {
                    const imgs = document.querySelectorAll("img");
                    for (const im of imgs) {
                        const s = im.src || "";
                        if (s.startsWith("blob:") || s.startsWith("data:image")) return true;
                    }
                    return !!document.querySelector("[class*='attachment'], [class*='file-preview'], [data-test-id*='file']");
                }''')
                if has_att:
                    self.log.emit("   📎 Đã đính kèm ảnh qua input[type=file] ẩn.\n")
                    return True, ""
            except Exception as e:
                self.log.emit(f"   ↪️ input ẩn lỗi ({e}), thử cách khác...\n")

        # CÁCH 1: DÁN ảnh bằng Ctrl+V
        try:
            if self._paste_image(page):
                self.log.emit("   📋 Đã dán ảnh bằng Ctrl+V.\n")
                return True, ""
        except Exception:
            pass
        self.log.emit("   ↪️ Ctrl+V chưa ăn, thử nút đính kèm...\n")

        # CÁCH 2: bấm nút trigger, bắt file chooser (1 cấp)
        trig = _thumb_find_el(page, _THUMB_UPLOAD_TRIGGER_SELS, timeout=3000,
                              cancel_check=lambda: self._cancel)
        if trig:
            # 2a: bắt file chooser trực tiếp (nút mở thẳng dialog)
            try:
                with page.expect_file_chooser(timeout=5000) as fc_info:
                    trig.click()
                fc = fc_info.value
                fc.set_files(self.src_image)
                page.wait_for_timeout(2500)
                return True, ""
            except Exception:
                page.wait_for_timeout(500)

            # 2b: nút mở SUBMENU (2 cấp) — bấm trig rồi bấm mục con trong submenu
            try:
                trig.click()
                page.wait_for_timeout(800)
                # Tìm mục "Tải tệp lên" / "Upload file" trong submenu vừa mở
                sub_item = _thumb_find_el(page, self._SUBMENU_FILE_SELS, timeout=3000,
                                         cancel_check=lambda: self._cancel)
                if sub_item:
                    try:
                        with page.expect_file_chooser(timeout=5000) as fc_info:
                            sub_item.click()
                        fc = fc_info.value
                        fc.set_files(self.src_image)
                        page.wait_for_timeout(2500)
                        return True, ""
                    except Exception:
                        pass
                    # Nếu vẫn chưa được: thử set_input_files lần nữa sau khi submenu mở
                    fi2 = page.query_selector(_THUMB_FILE_INPUT_SEL) or self._find_file_input_deep(page)
                    if fi2:
                        try:
                            fi2.set_input_files(self.src_image)
                            page.wait_for_timeout(2500)
                            return True, ""
                        except Exception as e:
                            self.log.emit(f"⚠️ set_input_files sau submenu lỗi: {e}\n")
            except Exception as e:
                self.log.emit(f"⚠️ Thử submenu lỗi: {e}\n")

        self._dump_buttons(page)
        return False, ("Không tìm thấy chỗ tải ảnh lên. Xem danh sách nút ở trên, "
                       "gửi lại cho người phát triển để chỉnh selector.")

    def _grab_new_image(self, page, known_srcs, save_path):
        """Chờ ảnh Gemini VẼ XONG rồi tải. Lọc chặt để không vớ nhầm
        avatar/icon/ảnh gốc: chỉ lấy ảnh TO, nằm trong khối trả lời model,
        và phải ỔN ĐỊNH (không đổi qua vài nhịp = đã vẽ xong)."""
        stable_src, stable_count = None, 0
        for _ in range(480):  # ~240s, ảnh có thể vẽ lâu
            if self._cancel:
                return None
            page.wait_for_timeout(500)

            # Quét TOÀN TRANG (kể cả shadow DOM), lấy ảnh TO NHẤT mới xuất hiện
            # mà không nằm trong 'known' (ảnh gốc + ảnh cũ). Ảnh Gemini vẽ luôn
            # là ảnh lớn nhất trên trang -> không cần giới hạn trong scope model.
            cand = page.evaluate('''(args) => {
                const known = args.known, srcRatio = args.srcRatio;
                const knownSet = new Set(known);
                let best = null, bestArea = 0;
                const consider = (im) => {
                    const s = im.src || im.currentSrc || "";
                    if (!s) return;
                    if (knownSet.has(s)) return;
                    if (!im.complete) return;
                    const w = im.naturalWidth || 0;
                    const h = im.naturalHeight || 0;
                    if (w < 256 || h < 256) return;        // bỏ avatar/icon/placeholder nhỏ
                    if (h >= w) return;                    // thumbnail là NGANG -> bỏ ảnh vuông/dọc
                    const ratio = w / h;
                    if (ratio < 1.2) return;               // chưa đủ ngang
                    // Loại ảnh TRÙNG TỈ LỆ ảnh gốc (chính là ảnh gốc, dù src đã đổi).
                    if (srcRatio && Math.abs(ratio - srcRatio) < 0.03) return;
                    const area = w * h;
                    if (area > bestArea) { bestArea = area; best = {src: s, w, h}; }
                };
                const walk = (root) => {
                    root.querySelectorAll("img").forEach(consider);
                    root.querySelectorAll("*").forEach(el => {
                        if (el.shadowRoot) walk(el.shadowRoot);
                    });
                };
                walk(document);
                return best;
            }''', {"known": list(known_srcs), "srcRatio": self._src_ratio})

            if not cand:
                stable_src, stable_count = None, 0
                continue

            src = cand["src"]
            if stable_count == 0 or src != stable_src:
                self.log.emit(f"   👁️ Thấy ảnh ứng viên {cand['w']}x{cand['h']}, đang chờ vẽ xong...\n")
            # Chờ ỔN ĐỊNH: cùng 1 src xuất hiện liên tục >= 4 nhịp (~2s)
            # -> coi như Gemini đã vẽ xong, không còn đổi ảnh.
            if src == stable_src:
                stable_count += 1
            else:
                stable_src, stable_count = src, 1

            if stable_count >= 4:
                page.wait_for_timeout(800)
                got = self._capture_img_element(page, src, save_path)
                if got:
                    try:
                        if os.path.getsize(got) > 15000:  # >15KB
                            return got
                    except Exception:
                        return got
                # chụp hỏng -> thử fetch (dự phòng), rồi coi src là đã biết
                got = self._download_src(page, src, save_path)
                if got:
                    try:
                        if os.path.getsize(got) > 15000:
                            return got
                    except Exception:
                        return got
                known_srcs.add(src)
                stable_src, stable_count = None, 0
        # Hết giờ mà không có ảnh: đọc text Gemini trả về để chẩn đoán
        # (nếu Gemini từ chối / không hỗ trợ tạo ảnh thì báo cho người dùng).
        try:
            reply = page.evaluate('''() => {
                const el = document.querySelector(
                    ".model-response-text .markdown, message-content .markdown, [data-message-author-role='model']");
                return el ? (el.innerText || "").trim().slice(0, 400) : "";
            }''')
            if reply:
                self.log.emit(f"   💬 Gemini trả lời (chữ): {reply}\n")
                low = reply.lower()
                if any(k in low for k in ["can't create", "cannot create", "unable to",
                                          "không thể tạo", "không hỗ trợ", "i can't generate",
                                          "i'm not able"]):
                    self.log.emit("   ⛔ Có vẻ tài khoản/model này KHÔNG tạo được ảnh. "
                                  "Thử đổi sang model có tạo ảnh, hoặc tài khoản khác.\n")
        except Exception:
            pass
        return None

    def _capture_img_element(self, page, src, save_path):
        """Chụp THẲNG phần tử <img> đang hiển thị thành PNG (không fetch mạng,
        né được CORS/cookie mà Gemini chặn). Ảnh gì hiện trên màn thì lấy y hệt."""
        try:
            # Đánh dấu đúng img có src này để Playwright chụp được (kể cả shadow DOM
            # thì vẫn cuộn tới + chụp qua bounding box).
            handle = page.evaluate_handle('''(target) => {
                const findImg = (root) => {
                    for (const im of root.querySelectorAll("img")) {
                        if ((im.src || im.currentSrc || "") === target) return im;
                    }
                    for (const el of root.querySelectorAll("*")) {
                        if (el.shadowRoot) {
                            const f = findImg(el.shadowRoot);
                            if (f) return f;
                        }
                    }
                    return null;
                };
                const im = findImg(document);
                if (im) im.scrollIntoView({block: "center"});
                return im;
            }''', src)
            el = handle.as_element()
            if not el:
                return None
            page.wait_for_timeout(500)
            save_path = os.path.splitext(save_path)[0] + ".png"
            el.screenshot(path=save_path)
            # Bỏ góc bo tròn / viền đen do Gemini bọc ảnh trong khung border-radius.
            self._trim_rounded_corners(save_path)
            return save_path
        except Exception as e:
            self.log.emit(f"   ⚠️ Chụp phần tử ảnh lỗi: {e}\n")
            return None

    def _trim_rounded_corners(self, img_path):
        """Cắt ảnh Gemini trả về thành hình chữ nhật ĐẶC, không còn 4 góc bo
        tròn. Cách: dò bounding box vùng ảnh thật, rồi dò độ sâu góc bo và
        crop vào đủ để 4 góc đều kín (không còn pixel nền lọt ở góc)."""
        try:
            from PIL import Image, ImageChops
            im = Image.open(img_path).convert("RGB")
            bg = Image.new("RGB", im.size, (0, 0, 0))
            diff = ImageChops.difference(im, bg)
            bbox = diff.getbbox()
            if not bbox:
                return
            l, t, r, b = bbox
            im2 = im.crop((l, t, r, b))
            w, h = im2.size
            px = im2.load()

            def _is_bg(x, y):
                # Pixel gần đen coi như nền (góc bo để lộ nền tối)
                pr, pg, pb = px[x, y]
                return pr < 12 and pg < 12 and pb < 12

            # Dò độ sâu góc bo: quét đường chéo từ 4 góc vào tâm, tìm điểm
            # đầu tiên KHÔNG còn là nền -> đó là bán kính bo lớn nhất.
            max_scan = min(w, h) // 4      # không quét quá 1/4 cạnh
            corner_depth = 0
            corners = [(0, 0, 1, 1), (w - 1, 0, -1, 1),
                       (0, h - 1, 1, -1), (w - 1, h - 1, -1, -1)]
            for cx, cy, dx, dy in corners:
                d = 0
                while d < max_scan:
                    x = cx + dx * d
                    y = cy + dy * d
                    if not (0 <= x < w and 0 <= y < h):
                        break
                    if not _is_bg(x, y):
                        break
                    d += 1
                corner_depth = max(corner_depth, d)

            # Crop vào bằng độ sâu góc bo (thêm 1px cho chắc), giữ tối đa 92%
            # kích thước để không lẹm quá nhiều nếu dò sai.
            cut = min(corner_depth + 1, int(min(w, h) * 0.08))
            if cut > 0 and (w - 2 * cut) > 8 and (h - 2 * cut) > 8:
                im2 = im2.crop((cut, cut, w - cut, h - cut))
            im2.save(img_path)
            return
        except Exception:
            pass
        # Fallback không có Pillow: cắt cứng ~3% mỗi mép bằng Qt.
        try:
            pm = QPixmap(img_path)
            if pm.isNull():
                return
            w, h = pm.width(), pm.height()
            mx, my = max(3, int(w * 0.03)), max(3, int(h * 0.03))
            pm.copy(mx, my, w - 2 * mx, h - 2 * my).save(img_path, "PNG")
        except Exception:
            pass

        try:
            if src.startswith("data:image"):
                header, b64 = src.split(",", 1)
                ext = ".png"
                if "jpeg" in header or "jpg" in header: ext = ".jpg"
                if "webp" in header: ext = ".webp"
                save_path = os.path.splitext(save_path)[0] + ext
                with open(save_path, "wb") as f:
                    f.write(base64.b64decode(b64))
                return save_path
            data_url = page.evaluate('''async (url) => {
                try {
                    const r = await fetch(url);
                    const b = await r.blob();
                    if (!b || !b.type || !b.type.startsWith("image/")) {
                        // một số URL trả redirect/html -> thử vẫn đọc nếu blob có kích thước
                        if (!b || b.size < 5000) return null;
                    }
                    return await new Promise((res) => {
                        const fr = new FileReader();
                        fr.onload = () => res(fr.result);
                        fr.onerror = () => res(null);
                        fr.readAsDataURL(b);
                    });
                } catch (e) { return null; }
            }''', src)
            if not data_url or "," not in data_url:
                self.log.emit("   ⚠️ Fetch ảnh không trả về dữ liệu ảnh hợp lệ.\n")
                return None
            header, b64 = data_url.split(",", 1)
            ext = ".png"
            if "jpeg" in header or "jpg" in header: ext = ".jpg"
            if "webp" in header: ext = ".webp"
            save_path = os.path.splitext(save_path)[0] + ext
            with open(save_path, "wb") as f:
                f.write(base64.b64decode(b64))
            return save_path
        except Exception as e:
            self.log.emit(f"⚠️ Lỗi tải ảnh: {e}\n")
            return None

    def _make_one_variant(self, i, stem):
        """Tạo 1 biến thể thumbnail. Tự mở Chrome (playwright) RIÊNG cho
        biến thể này để có thể chạy song song với các biến thể khác."""
        if self._cancel:
            return None
        hint = self.variant_hints[i % len(self.variant_hints)]
        prompt = (
            f"{self.base_prompt}\n\n"
            f"PHONG CÁCH RIÊNG CHO ẢNH NÀY (biến thể {i+1}/{self.n_variants}): {hint}\n"
            f"Giữ nguyên nhân vật/nét đặc trưng của ảnh gốc, chỉ đổi hậu cảnh & hiệu ứng "
            f"theo phong cách trên. Vẫn có đầy đủ chữ tiêu đề tiếng Việt và badge. "
            f"Chỉ tạo 1 ảnh, tỉ lệ 16:9 ngang."
        )
        self.log.emit(f"\n🖼️ [{i+1}/{self.n_variants}] Đang tạo thumbnail... ({hint})\n")

        pw = browser = None
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=not self.show_browser,
                                         channel="chrome", args=BROWSER_ARGS)
            ctx = browser.new_context(storage_state=AUTH_FILE, user_agent=UA,
                                      viewport={"width": 1280, "height": 900})
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page = ctx.new_page()

            if self._cancel:
                return None

            page.goto("https://gemini.google.com/app",
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)

            try:
                known = set(page.eval_on_selector_all(
                    "img", "els => els.map(e => e.src || '')"))
            except Exception:
                known = set()

            ok, err = self._attach_image(page)
            if not ok:
                self.log.emit(f"⚠️ {err} -> bỏ qua biến thể {i+1}.\n")
                return None

            # Chụp lại danh sách ảnh SAU KHI đính kèm (gồm cả ảnh gốc vừa
            # dán/upload) -> để _grab_new_image loại đúng ảnh gốc, chỉ lấy
            # ảnh Gemini vẽ ra.
            try:
                known = set(page.eval_on_selector_all(
                    "img", "els => els.map(e => e.src || '')"))
            except Exception:
                pass

            ok, err = self._type_and_send(page, prompt)
            if not ok:
                self.log.emit(f"⚠️ {err} -> bỏ qua biến thể {i+1}.\n")
                return None

            out_path = os.path.join(self.out_dir, f"{stem}_thumb{i+1}.png")
            got = self._grab_new_image(page, known, out_path)
            if got:
                self.log.emit(f"✅ Đã lưu: {os.path.basename(got)}\n")
                self.one_done.emit(got)
            else:
                self.log.emit(f"❌ Không lấy được ảnh cho biến thể {i+1} "
                              f"(có thể Gemini không trả ảnh / đổi giao diện).\n")
            return got
        except Exception as e:
            self.log.emit(f"⚠️ Lỗi biến thể {i+1}: {e}\n")
            return None
        finally:
            try: ctx.close()
            except Exception: pass
            try:
                if browser: browser.close()
                if pw: pw.stop()
            except Exception:
                pass

    def run(self):
        if not os.path.exists(AUTH_FILE):
            self.log.emit("❌ Chưa đăng nhập Gemini. Hãy bấm 'Đồng bộ Gemini' ở tab dịch trước.\n")
            self.all_done.emit([]); return
        if not self.src_image or not os.path.exists(self.src_image):
            self.log.emit("❌ Không tìm thấy ảnh gốc.\n")
            self.all_done.emit([]); return

        os.makedirs(self.out_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(self.src_image))[0]
        # Đọc tỉ lệ ảnh gốc để KHÔNG chụp nhầm nó (dù src đổi sau khi Gemini vẽ).
        self._src_ratio = None
        try:
            pm = QPixmap(self.src_image)
            if not pm.isNull() and pm.height() > 0:
                self._src_ratio = pm.width() / pm.height()
        except Exception:
            pass

        saved = []
        saved_lock = threading.Lock()
        done_count = [0]
        n_workers = 1
        self.log.emit(f"🌐 Mở {n_workers} Chrome ẩn (tuần tự) để tạo thumbnail.\n")

        def _worker(i):
            got = self._make_one_variant(i, stem)
            with saved_lock:
                if got:
                    saved.append(got)
                done_count[0] += 1
                self.progress.emit(done_count[0], self.n_variants)
            return got

        try:
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = [pool.submit(_worker, i) for i in range(self.n_variants)]
                for f in as_completed(futures):
                    if self._cancel:
                        break
        except Exception as e:
            self.log.emit(f"❌ Lỗi luồng tạo thumbnail: {e}\n")

        self.log.emit(f"\n🏁 Xong. Tạo được {len(saved)}/{self.n_variants} thumbnail.\n")
        self.all_done.emit(saved)


# ============================================================
# LUỒNG TẠO THUMBNAIL BẰNG CHATGPT (web automation)
# Cùng interface signal với GeminiThumbnailThread để UI cắm thẳng vào.
# Auth riêng: chatgpt_auth.json (storage_state). Lần đầu chưa có sẽ mở
# Chrome hiện để người dùng đăng nhập rồi tự lưu session.
# ============================================================
CHATGPT_AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(AUTH_FILE)),
                                 "chatgpt_auth.json")

_CGPT_INPUT_SELS = [
    "#prompt-textarea",
    "div#prompt-textarea[contenteditable='true']",
    "div[contenteditable='true'].ProseMirror",
    "textarea[data-testid='prompt-textarea']",
    "textarea[placeholder]",
]
_CGPT_SEND_SELS = [
    "button[data-testid='send-button']",
    "button[aria-label*='Send' i]",
    "button[aria-label*='Gửi' i]",
]
_CGPT_FILE_INPUT_SEL = "input[type='file']"
_CGPT_RESULT_IMG_SELS = [
    "[data-message-author-role='assistant'] img",
    "img[src*='oaiusercontent']",
    "img[src*='files.oaiusercontent']",
    "img[alt*='Generated' i]",
    "img[src^='blob:']",
]


class ChatGPTThumbnailThread(QThread):
    """Tạo N thumbnail từ 1 ảnh gốc bằng ChatGPT web (tạo ảnh bằng DALL·E/
    gpt-image). Mỗi biến thể 1 phiên Chrome riêng. Auth qua storage_state."""
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    one_done = pyqtSignal(str)
    all_done = pyqtSignal(list)

    def __init__(self, src_image, base_prompt, out_dir, n_variants=4,
                 variant_hints=None, show_browser=False, srt_text="",
                 orientation="landscape"):
        super().__init__()
        self.src_image = src_image
        self.base_prompt = base_prompt.strip()
        self.out_dir = out_dir
        self.srt_text = (srt_text or "").strip()
        self.n_variants = max(1, min(8, int(n_variants)))
        self.variant_hints = variant_hints or [
            "Phong cách RỒNG LỬA: rồng vàng-đỏ khổng lồ bằng lửa cuộn quanh phía sau, biển lửa dung nham, tàn lửa bay, khí thế bá vương ngút trời.",
            "Phong cách SẤM SÉT VẠN QUÂN: tia sét xanh-tím xé trời, luồng năng lượng điện bùng nổ, mây bão vần vũ, hào quang chớp giật hùng vĩ.",
            "Phong cách BĂNG PHONG THẦN GIỚI: phượng hoàng băng, bão tuyết, ánh sáng vàng kim thần thánh, cung điện trên mây, huyền ảo choáng ngợp.",
            "Phong cách HOÀNG KIM SỬ THI: hào quang vàng rực chói lòa, linh thú vàng cuộn quanh, tia sáng thần thánh tỏa, bụi vàng bay, khí thế đế vương.",
        ]
        self.show_browser = bool(show_browser)
        self._cancel = False
        self._src_ratio = None
        self.orientation = orientation  # "landscape" = 16:9 ngang | "portrait" = 9:16 dọc

    def cancel(self):
        self._cancel = True

    def _find_el(self, page, sels, timeout=8000):
        return _thumb_find_el(page, sels, timeout=timeout,
                              cancel_check=lambda: self._cancel)

    def _attach_image(self, page):
        """Đính kèm ảnh qua input[type=file] ẩn (ưu tiên), fallback Ctrl+V."""
        fi = page.query_selector(_CGPT_FILE_INPUT_SEL)
        if not fi:
            # ChatGPT có thể ẩn input trong shadow/portal; thử tìm sâu
            try:
                handle = page.evaluate_handle('''() => {
                    const find = (root) => {
                        let el = root.querySelector("input[type='file']");
                        if (el) return el;
                        for (const h of root.querySelectorAll('*')) {
                            if (h.shadowRoot) { const f = find(h.shadowRoot); if (f) return f; }
                        }
                        return null;
                    };
                    return find(document);
                }''')
                fi = handle.as_element()
            except Exception:
                fi = None
        if fi:
            try:
                fi.set_input_files(self.src_image)
                # chờ ảnh preview đính kèm hiện trong ô soạn
                for _ in range(30):  # ~15s
                    if self._cancel:
                        return False
                    page.wait_for_timeout(500)
                    has_att = page.evaluate('''() => {
                        const imgs = document.querySelectorAll("img");
                        for (const im of imgs) {
                            const s = im.src || "";
                            if (s.startsWith("blob:") || s.startsWith("data:image")) return true;
                        }
                        return !!document.querySelector("[class*='attachment'], [data-testid*='attachment'], img[alt*='upload' i]");
                    }''')
                    if has_att:
                        self.log.emit("   📎 Đã đính kèm ảnh gốc vào ChatGPT.\n")
                        return True
            except Exception as e:
                self.log.emit(f"   ↪️ input ẩn lỗi ({e}).\n")
        self.log.emit("   ⚠️ Không đính kèm được ảnh (ChatGPT có thể đổi giao diện).\n")
        return False

    def _type_and_send(self, page, prompt_text):
        inp = self._find_el(page, _CGPT_INPUT_SELS, timeout=10000)
        if not inp:
            return False, "Không thấy ô nhập ChatGPT (có thể chưa đăng nhập / CAPTCHA)."
        try:
            inp.click()
            page.wait_for_timeout(400)
            # ProseMirror: gõ trực tiếp an toàn hơn set innerText
            page.keyboard.insert_text(prompt_text)
            # Prompt rất dài (~2000+ ký tự) → cần chờ ChatGPT render xong text
            # trong ô ProseMirror mới bấm Send, không thì nút còn disabled.
            page.wait_for_timeout(1500)
        except Exception:
            # fallback set nội dung
            try:
                page.evaluate(
                    '''(t)=>{const el=document.querySelector("#prompt-textarea")'''
                    '''||document.querySelector("[contenteditable='true']");'''
                    '''if(el){el.focus();el.innerText=t;'''
                    '''el.dispatchEvent(new Event('input',{bubbles:true}));}}''',
                    prompt_text)
                page.wait_for_timeout(1500)
            except Exception:
                pass

        # Chờ nút Send ACTIVE (không disabled) tối đa 10s trước khi bấm.
        # ChatGPT disable nút khi ô trống hoặc đang xử lý → cần đợi.
        for _ in range(20):
            if self._cancel:
                return False, "Đã hủy."
            try:
                btn = page.query_selector(_CGPT_SEND_SELS[0])
                if not btn:
                    for s in _CGPT_SEND_SELS[1:]:
                        btn = page.query_selector(s)
                        if btn: break
                if btn and btn.get_attribute("disabled") is None and btn.get_attribute("aria-disabled") != "true":
                    break
            except Exception:
                pass
            page.wait_for_timeout(500)

        for attempt in range(5):
            if self._cancel:
                return False, "Đã hủy."
            btn = self._find_el(page, _CGPT_SEND_SELS, timeout=3000)
            sent = False
            if btn:
                try:
                    if btn.get_attribute("disabled") is None and btn.get_attribute("aria-disabled") != "true":
                        btn.click(); sent = True
                except Exception:
                    pass
            if not sent:
                try:
                    inp.click(); page.keyboard.press("Enter")
                except Exception:
                    pass
            # Chờ lâu hơn: ChatGPT cần vài giây xử lý trước khi ô nhập trống
            page.wait_for_timeout(2000)
            # kiểm tra ô nhập đã trống (đã gửi thành công)
            try:
                left = page.evaluate(
                    '''()=>{const el=document.querySelector("#prompt-textarea")'''
                    '''||document.querySelector("[contenteditable='true']");'''
                    '''return el?(el.innerText||"").trim():"x";}''')
            except Exception:
                left = ""
            if not left:
                return True, ""
            if attempt < 4:
                self.log.emit(f"↩️ Chưa gửi được, thử lại ({attempt+2}/5)...\n")
        return False, "ChatGPT không nhận prompt sau 5 lần."

    def _grab_new_image(self, page, known_srcs, save_path):
        """Chờ ChatGPT vẽ xong ảnh (ổn định qua vài nhịp) rồi tải về."""
        stable_src, stable_count = None, 0
        for _ in range(600):  # ~300s, ảnh gpt-image vẽ khá lâu
            if self._cancel:
                return None
            page.wait_for_timeout(500)
            cand = page.evaluate('''(args) => {
                const known = new Set(args.known), srcRatio = args.srcRatio;
                let best = null, bestArea = 0;
                document.querySelectorAll("img").forEach(im => {
                    const s = im.src || im.currentSrc || "";
                    if (!s || known.has(s)) return;
                    if (!im.complete) return;
                    const w = im.naturalWidth||0, h = im.naturalHeight||0;
                    if (w < 256 || h < 256) return;
                    // ưu tiên ảnh sinh ra (oaiusercontent / blob), bỏ avatar/icon
                    const ratio = w/h;
                    if (srcRatio && Math.abs(ratio - srcRatio) < 0.03) return; // né ảnh gốc
                    const area = w*h;
                    if (area > bestArea) { bestArea = area; best = {src:s, w, h}; }
                });
                return best;
            }''', {"known": list(known_srcs), "srcRatio": self._src_ratio})
            if not cand:
                stable_src, stable_count = None, 0
                continue
            src = cand["src"]
            if stable_count == 0 or src != stable_src:
                self.log.emit(f"   👁️ Thấy ảnh {cand['w']}x{cand['h']}, chờ vẽ xong...\n")
            if src == stable_src:
                stable_count += 1
            else:
                stable_src, stable_count = src, 1
            if stable_count >= 4:  # ~2s ổn định
                return self._download_src(page, src, save_path)
        return None

    def _download_src(self, page, src, save_path):
        try:
            data_url = page.evaluate('''async (src) => {
                try {
                    const r = await fetch(src);
                    const b = await r.blob();
                    return await new Promise(res => {
                        const fr = new FileReader();
                        fr.onload = () => res(fr.result);
                        fr.readAsDataURL(b);
                    });
                } catch (e) { return null; }
            }''', src)
            if not data_url or "," not in data_url:
                self.log.emit("   ⚠️ Fetch ảnh không hợp lệ.\n")
                return None
            header, b64 = data_url.split(",", 1)
            ext = ".png"
            if "jpeg" in header or "jpg" in header: ext = ".jpg"
            if "webp" in header: ext = ".webp"
            save_path = os.path.splitext(save_path)[0] + ext
            with open(save_path, "wb") as f:
                f.write(base64.b64decode(b64))
            return save_path
        except Exception as e:
            self.log.emit(f"⚠️ Lỗi tải ảnh: {e}\n")
            return None

    def _make_one_variant(self, i, stem):
        if self._cancel:
            return None
        srt_block = ""
        if self.srt_text:
            # Cắt bớt SRT quá dài để không vỡ ô nhập (giữ ~8000 ký tự đầu là đủ
            # để nắm cốt truyện/cao trào). Bỏ số thứ tự & timestamp cho gọn.
            import re as _re
            clean = _re.sub(r'\d+\s*\n\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}\s*\n', '', self.srt_text)
            clean = _re.sub(r'\n{2,}', '\n', clean).strip()
            if len(clean) > 8000:
                clean = clean[:8000]
            srt_block = f"\n\n=== NỘI DUNG PHỤ ĐỀ (SRT) ĐỂ PHÂN TÍCH ===\n{clean}\n=== HẾT SRT ===\n"
        ratio_note = (
            "\n\nYÊU CẦU BẮT BUỘC VỀ TỈ LỆ: Tạo ảnh khổ DỌC tỉ lệ 9:16 (portrait), "
            "chiều cao lớn hơn chiều rộng. Phù hợp đăng Facebook/Reels/Stories. "
            "KHÔNG tạo ảnh ngang."
            if self.orientation == "portrait"
            else "\n\nYÊU CẦU TỈ LỆ: Tạo ảnh khổ NGANG tỉ lệ 16:9 (landscape, 1536x1024)."
        )
        prompt = f"{self.base_prompt}{srt_block}{ratio_note}"
        self.log.emit(f"\n🖼️ ChatGPT đang phân tích SRT & tạo thumbnail...\n")

        pw = browser = ctx = None
        try:
            from playwright.sync_api import sync_playwright
            try:
                from PyQt6.QtGui import QPixmap as _QPix
                pm = _QPix(self.src_image)
                if not pm.isNull() and pm.height() > 0:
                    self._src_ratio = pm.width() / pm.height()
            except Exception:
                pass

            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=not self.show_browser,
                                         channel="chrome", args=BROWSER_ARGS)
            ctx = browser.new_context(storage_state=CHATGPT_AUTH_FILE, user_agent=UA,
                                      viewport={"width": 1280, "height": 900})
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page = ctx.new_page()
            if self._cancel:
                return None
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)

            if self.src_image and os.path.exists(self.src_image):
                ok = self._attach_image(page)
                if not ok:
                    return None
            try:
                known = set(page.eval_on_selector_all("img", "els => els.map(e => e.src || '')"))
            except Exception:
                known = set()

            ok, err = self._type_and_send(page, prompt)
            if not ok:
                self.log.emit(f"⚠️ {err} -> bỏ qua biến thể {i+1}.\n")
                return None

            out_path = os.path.join(self.out_dir, f"{stem}_thumb{i+1}.png")
            got = self._grab_new_image(page, known, out_path)
            if got:
                self.log.emit(f"✅ Đã lưu: {os.path.basename(got)}\n")
                self.one_done.emit(got)
            else:
                self.log.emit(f"❌ Không lấy được ảnh biến thể {i+1} "
                              f"(ChatGPT không trả ảnh / đổi giao diện / hết lượt tạo ảnh).\n")
            return got
        except Exception as e:
            self.log.emit(f"⚠️ Lỗi biến thể {i+1}: {e}\n")
            return None
        finally:
            try:
                if ctx: ctx.close()
            except Exception: pass
            try:
                if browser: browser.close()
                if pw: pw.stop()
            except Exception: pass

    def run(self):
        if not os.path.exists(CHATGPT_AUTH_FILE):
            self.log.emit("❌ Chưa đăng nhập ChatGPT. Hãy bấm 'Đăng nhập ChatGPT' để login 1 lần trước.\n")
            self.all_done.emit([]); return
        has_img = bool(self.src_image and os.path.exists(self.src_image))
        if not has_img and not self.srt_text:
            self.log.emit("❌ Cần ít nhất ảnh gốc HOẶC file SRT để tạo thumbnail.\n")
            self.all_done.emit([]); return
        os.makedirs(self.out_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(self.src_image))[0] if has_img else "thumbnail"

        saved = []
        for i in range(self.n_variants):
            if self._cancel:
                break
            got = self._make_one_variant(i, stem)
            if got:
                saved.append(got)
            self.progress.emit(i + 1, self.n_variants)
        self.log.emit(f"\n🏁 Xong. Tạo được {len(saved)}/{self.n_variants} thumbnail (ChatGPT).\n")
        self.all_done.emit(saved)


class ChatGPTLoginThread(QThread):
    """Mở Chrome hiện để người dùng tự đăng nhập ChatGPT, GIỮ cửa sổ mở cho
    tới khi người dùng bấm nút xác nhận (confirm) trong app. Không tự phát hiện,
    không tự tắt — tránh việc lưu nhầm phiên chưa đăng nhập rồi đóng sớm."""
    log = pyqtSignal(str)
    done = pyqtSignal(bool)

    def __init__(self, timeout_login=600):
        super().__init__()
        self.timeout_login = timeout_login
        self._confirmed = False
        self._cancel = False

    def confirm(self):
        self._confirmed = True

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            self.log.emit(f"❌ Thiếu Playwright: {e}\n")
            self.done.emit(False); return

        pw = browser = None
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=False, channel="chrome", args=BROWSER_ARGS)
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
            self.log.emit("🔐 Cửa sổ Chrome đã mở. Hãy đăng nhập ChatGPT trong cửa sổ đó.\n")
            self.log.emit("   👉 SAU KHI đã vào được tài khoản, bấm nút '✅ Tôi đã đăng nhập xong' trong app.\n")

            import time as _t
            deadline = _t.time() + self.timeout_login
            while _t.time() < deadline:
                if self._cancel:
                    self.log.emit("⛔ Đã hủy đăng nhập.\n")
                    self.done.emit(False); return
                if self._confirmed:
                    break
                # kiểm tra cửa sổ còn sống không (người dùng lỡ tắt tay)
                try:
                    if page.is_closed():
                        self.log.emit("⚠️ Cửa sổ đã bị đóng trước khi xác nhận.\n")
                        self.done.emit(False); return
                except Exception:
                    pass
                self.msleep(400)

            if not self._confirmed:
                self.log.emit("⚠️ Hết thời gian chờ. Hãy thử lại.\n")
                self.done.emit(False); return

            # Xác minh có cookie session trước khi lưu (cảnh báo nếu chưa thấy)
            try:
                cookies = ctx.cookies()
                has_session = any(
                    c.get("name", "").startswith("__Secure-next-auth.session-token") and c.get("value")
                    for c in cookies
                )
            except Exception:
                has_session = False

            if not has_session:
                self.log.emit("⚠️ Chưa thấy phiên đăng nhập ChatGPT (cookie session). "
                              "Vẫn lưu, nhưng nếu tạo ảnh báo chưa đăng nhập thì hãy đăng nhập lại.\n")

            page.wait_for_timeout(800)
            ctx.storage_state(path=CHATGPT_AUTH_FILE)
            self.log.emit("✅ Đã lưu phiên đăng nhập ChatGPT.\n")
            self.done.emit(True)
        except Exception as e:
            self.log.emit(f"❌ Lỗi đăng nhập ChatGPT: {e}\n")
            self.done.emit(False)
        finally:
            try:
                if browser: browser.close()
                if pw: pw.stop()
            except Exception: pass


_DEFAULT_THUMB_PROMPT = (
    "Bạn là chuyên gia phân tích phim + thiết kế thumbnail YouTube chuyên nghiệp, "
    "chuyên phim ngắn, web drama, phim Trung Quốc, hành động, giang hồ, thần bài, "
    "trả thù, tình cảm, fantasy, xuyên không.\n\n"
    "Nếu có NỘI DUNG PHỤ ĐỀ (SRT) kèm bên dưới: TỰ đọc & phân tích để tìm nhân vật "
    "chính, phản diện, xung đột chính, cảnh cao trào/hành động/trả thù/đấu trí/gây "
    "sốc, khoảnh khắc biểu cảm mạnh nhất, và tình huống khiến người xem tò mò nhất. "
    "Tự xác định THỂ LOẠI rồi chọn MỘT concept thumbnail mạnh nhất — nhìn vào là "
    "muốn biết 'Chuyện gì đang xảy ra?'.\n\n"
    "PHONG CÁCH: EPIC CINEMATIC MOVIE POSTER — ULTRA DETAILED — DRAMATIC LIGHTING "
    "— HIGH IMPACT. Tỷ lệ 16:9. Nhân vật chính là trọng tâm, gương mặt rõ nét, cảm "
    "xúc mạnh, ánh mắt có thần. Bố cục điện ảnh, chiều sâu rõ, ánh sáng chuyên "
    "nghiệp, tương phản mạnh, màu bắt mắt, hậu cảnh hợp nội dung. Như poster phim "
    "kinh phí lớn, KHÔNG giống ảnh chụp màn hình.\n\n"
    "GIỮ NHÂN VẬT (nếu có ảnh gốc): giữ đúng khuôn mặt, thần thái, đặc điểm ngoại "
    "hình; KHÔNG đổi giới tính/độ tuổi, KHÔNG làm biến dạng mặt. Có thể đổi tư "
    "thế/trang phục/ánh sáng/bối cảnh/góc máy nhưng nhân vật vẫn dễ nhận diện.\n\n"
    "BỐ CỤC: NHÂN VẬT → TÌNH HUỐNG → HẬU CẢNH → BRANDING. Không nhồi nhiều nhân "
    "vật, không để hậu cảnh lấn át, không đặt chữ lên mặt. Rõ ngay cả khi hiển thị "
    "nhỏ.\n\n"
    "BRANDING CỐ ĐỊNH — BẮT BUỘC CÓ 2 THÀNH PHẦN:\n"
    "• GÓC TRÊN BÊN TRÁI: biểu tượng LOA/megaphone điện ảnh nhỏ, hiện đại, kèm chữ "
    "'PHIM MỚI'. Badge chuyên nghiệp, sắc nét, dễ đọc, có chiều sâu & ánh sáng nhẹ, "
    "không quá to, không che nhân vật.\n"
    "• GÓC TRÊN BÊN PHẢI: dải RUY-BĂNG/badge chữ 'TRỌN BỘ'. Có chiều sâu 3D nhẹ, "
    "viền rõ, chữ lớn dễ đọc, không che mặt nhân vật.\n"
    "Giữ ĐÚNG chữ 'PHIM MỚI' và 'TRỌN BỘ', KHÔNG đổi thành PHIM HAY/PHIM HOT/FULL "
    "PHIM/XEM NGAY. KHÔNG bỏ 2 badge này.\n\n"
    "TIÊU ĐỀ GIẬT TÍT — BẮT BUỘC CÓ, KHÔNG ĐƯỢC BỎ, IN THẲNG LÊN ẢNH: Tự sáng tạo "
    "1 câu tiêu đề giật tít kiểu drama Trung Quốc, IN HOA, chữ TO ĐẬM chiếm 2–3 "
    "dòng ở phần dưới hoặc vùng trống lớn của ảnh (kiểu chữ hiệu ứng vàng gradient / "
    "viền phát sáng / đổ bóng dày, nổi bật như poster phim, KHÔNG che mặt nhân vật). "
    "Câu tiêu đề BẮT BUỘC áp dụng 1 trong 4 CÔNG THỨC sau, chọn công thức hợp nội "
    "dung nhất:\n"
    "• Công thức Vả mặt: [Kẻ phản diện] khinh thường [Nhân vật chính] và CÁI KẾT "
    "rùng mình...\n"
    "• Công thức Bí mật: Sự thật tàn nhẫn đằng sau [Hành động gây sốc / Vụ việc] của "
    "[Nhân vật]...\n"
    "• Công thức Giấu nghề: Chủ tịch giả vờ làm [Nghề bần hèn] thử lòng vợ và cú lật "
    "kèo...\n"
    "• Công thức Kích thích tò mò: Nhìn thì tưởng [Bình thường] nhưng sự thật lại "
    "khiến tất cả câm nín...\n"
    "Điền nội dung phim vào chỗ [...] cho khớp cao trào. Câu tiêu đề là THÀNH PHẦN "
    "BẮT BUỘC ngang hàng 2 badge — thiếu coi như ảnh LỖI. Dùng tiếng Việt CÓ DẤU "
    "đầy đủ, đúng chính tả.\n\n"
    "HIỆU ỨNG chỉ dùng khi hợp nội dung (khói/lửa/tia sáng/sấm sét/bụi/mưa/năng "
    "lượng/neon/lens flare/cinematic glow/depth of field), KHÔNG lạm dụng. Màu theo "
    "thể loại (hành động: tương phản mạnh; giang hồ: tối lạnh; thần bài: vàng/đỏ/"
    "neon; tình cảm: sáng mềm; fantasy: huyền ảo; trả thù: tối tương phản cao).\n\n"
    "MỤC TIÊU: nhìn là biết phim, thấy cao trào, muốn click, nhỏ vẫn rõ, nhận ra "
    "cùng 1 kênh. Kết quả NGẦU — ĐIỆN ẢNH — KỊCH TÍNH — SẮC NÉT — CHUYÊN NGHIỆP. "
    "CHỈ tạo 1 ảnh duy nhất, tỉ lệ 16:9 ngang (1536x1024).\n\n"
    "QUY TRÌNH XUẤT (BẮT BUỘC — KHÔNG HỎI LẠI, KHÔNG GIẢI THÍCH DÀI DÒNG, KHÔNG ĐƯA "
    "NHIỀU LỰA CHỌN): Khi nhận SRT, in ra câu TIÊU ĐỀ GIẬT TÍT (theo 1 trong 4 công "
    "thức) TRƯỚC, rồi lập tức vẽ 1 ảnh 16:9 duy nhất có đủ 3 cụm chữ: 'PHIM MỚI' "
    "(góc trên trái + icon loa), 'TRỌN BỘ' (góc trên phải, ruy-băng 3D), và ĐÚNG câu "
    "tiêu đề vừa tạo (in hoa, to đậm, hiệu ứng nổi bật, đặt phần dưới, không che "
    "mặt). Bắt buộc có câu tiêu đề trong ảnh. Chỉ 1 ảnh duy nhất."
)


class MergeRenderedThread(QThread):
    log = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def __init__(self, file_list, out_file, intro_image=None):
        super().__init__()
        self.file_list = file_list
        self.out_file = out_file
        self.intro_image = intro_image

    def _probe_resolution(self, ffmpeg, filepath):
        """Trả về (width, height) của video, hoặc None nếu probe thất bại."""
        try:
            si = subprocess.STARTUPINFO() if os.name == "nt" else None
            if si: si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            r = subprocess.run(
                [ffmpeg, "-i", filepath],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                startupinfo=si, text=True, encoding="utf-8", errors="replace"
            )
            m = re.search(r"Stream.*Video.*?(\d{3,5})x(\d{3,5})", r.stderr)
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception:
            pass
        return None

    def _probe_fps(self, ffmpeg, filepath):
        """Trả về fps (float, làm tròn 3 số) của video, hoặc None nếu không đọc được.
        FPS lệch giữa các tập (VD tập 25fps, tập 30fps) là nguyên nhân gây ĐỨNG
        HÌNH ở tập giữa khi gộp bằng -c copy — phải phát hiện để ép re-encode."""
        try:
            si = subprocess.STARTUPINFO() if os.name == "nt" else None
            if si: si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            r = subprocess.run(
                [ffmpeg, "-i", filepath],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                startupinfo=si, text=True, encoding="utf-8", errors="replace"
            )
            m = re.search(r"Stream.*Video.*?([\d.]+)\s*fps", r.stderr)
            if m:
                return round(float(m.group(1)), 3)
        except Exception:
            pass
        return None

    def _probe_has_audio(self, ffmpeg, filepath):
        """True nếu file có ít nhất 1 stream audio."""
        try:
            si = subprocess.STARTUPINFO() if os.name == "nt" else None
            if si: si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            r = subprocess.run(
                [ffmpeg, "-i", filepath],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                startupinfo=si, text=True, encoding="utf-8", errors="replace"
            )
            return "Audio:" in (r.stderr or "")
        except Exception:
            return False

    def _prepare_thumbnail(self):
        """Chuẩn hóa ảnh bìa thành JPG 1280×720 (chuẩn thumbnail YouTube), nền phủ
        16:9 không méo. Trả về đường dẫn JPG tạm, hoặc None nếu không có ảnh."""
        if not self.intro_image or not os.path.exists(self.intro_image):
            return None
        try:
            from PIL import Image
            im = Image.open(self.intro_image).convert("RGB")
            tw, th = 1280, 720
            # Phủ kín khung 16:9 (cover), cắt phần thừa — không để viền đen
            src_ratio = im.width / im.height
            dst_ratio = tw / th
            if src_ratio > dst_ratio:
                new_h = th
                new_w = int(round(th * src_ratio))
            else:
                new_w = tw
                new_h = int(round(tw / src_ratio))
            try:
                _RESAMPLE = Image.Resampling.LANCZOS  # Pillow >= 9.1
            except AttributeError:
                _RESAMPLE = Image.LANCZOS
            im = im.resize((new_w, new_h), _RESAMPLE)
            left = (new_w - tw) // 2
            top = (new_h - th) // 2
            im = im.crop((left, top, left + tw, top + th))
            out_jpg = os.path.join(tempfile.gettempdir(), f"yt_thumb_{int(time.time())}.jpg")
            im.save(out_jpg, "JPEG", quality=92)
            return out_jpg
        except Exception as e:
            self.log.emit(f"   ⚠️ Không chuẩn hóa được ảnh bìa: {e}\n")
            return None

    def _export_thumbnail_beside_video(self, thumb_jpg):
        """Copy thumbnail ra cạnh video với tên <video>_thumbnail.jpg để upload
        thẳng lên YouTube Studio (YouTube KHÔNG đọc cover nhúng trong file)."""
        if not thumb_jpg or not os.path.exists(thumb_jpg):
            return
        try:
            import shutil
            stem = os.path.splitext(self.out_file)[0]
            dst = f"{stem}_thumbnail.jpg"
            shutil.copyfile(thumb_jpg, dst)
            self.log.emit(f"🖼️ Đã xuất thumbnail để up YouTube: {os.path.basename(dst)}\n")
        except Exception as e:
            self.log.emit(f"   ⚠️ Không xuất được file thumbnail: {e}\n")

    def _embed_cover_mp4(self, ffmpeg, video_in, thumb_jpg, si):
        """Nhúng thumbnail vào metadata tag 'covr' của MP4 (copy stream, rất nhanh).
        Trả về True nếu thành công (đã ghi đè out_file)."""
        if not thumb_jpg or not os.path.exists(thumb_jpg):
            return False
        tmp_out = os.path.splitext(self.out_file)[0] + f"_cov_{int(time.time())}.mp4"
        cmd = [
            ffmpeg, "-y",
            "-i", video_in,
            "-i", thumb_jpg,
            "-map", "0:v:0", "-map", "0:a?", "-map", "1:v:0",
            "-c", "copy",
            "-c:v:1", "png",
            "-disposition:v:1", "attached_pic",
            "-movflags", "+faststart",
            tmp_out
        ]
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               startupinfo=si, text=True, encoding="utf-8", errors="replace")
            if p.returncode == 0 and os.path.exists(tmp_out):
                os.replace(tmp_out, self.out_file)
                self.log.emit("🖼️ Đã nhúng ảnh bìa vào file MP4.\n")
                return True
            else:
                self.log.emit(f"   ⚠️ Nhúng cover thất bại (bỏ qua, video vẫn OK):\n{(p.stderr or '')[-300:]}\n")
        except Exception as e:
            self.log.emit(f"   ⚠️ Lỗi nhúng cover (bỏ qua): {e}\n")
        finally:
            if os.path.exists(tmp_out):
                try: os.remove(tmp_out)
                except: pass
        return False

    def run(self):
        ffmpeg = get_ffmpeg_path()
        import tempfile, time, subprocess

        files = [p for p in (self.file_list or []) if p and os.path.exists(p)]
        if len(files) < 2:
            self.log.emit("❌ Không đủ ít nhất 2 file hợp lệ để gộp.\n")
            self.done.emit(False, "")
            return

        # ── Probe thông số đầu vào ───────────────────────────────────────────
        resolutions = [self._probe_resolution(ffmpeg, fp) for fp in files]
        fps_list = [self._probe_fps(ffmpeg, fp) for fp in files]
        unique_res = set(r for r in resolutions if r is not None)
        unique_fps = set(f for f in fps_list if f is not None)
        mixed_res = len(unique_res) > 1
        mixed_fps = len(unique_fps) > 1
        mixed = mixed_res or mixed_fps

        target_fps = max(unique_fps) if unique_fps else 30.0
        if unique_res:
            target_w, target_h = max(unique_res, key=lambda wh: wh[0] * wh[1])
        else:
            target_w, target_h = 1920, 1080
        target_w = max(2, (int(target_w) // 2) * 2)
        target_h = max(2, (int(target_h) // 2) * 2)

        if mixed_res:
            res_list = ", ".join(f"{r[0]}×{r[1]}" for r in resolutions if r is not None)
            self.log.emit(f"⚠️ Resolution KHÔNG đồng nhất: {res_list}\n")
        if mixed_fps:
            fps_txt = ", ".join(f"{f:g}fps" for f in sorted(unique_fps))
            self.log.emit(f"⚠️ FPS KHÔNG đồng nhất: {fps_txt}\n")
        if mixed:
            self.log.emit(
                f"   → Chuẩn hóa từng input trước khi concat: {target_w}×{target_h} @ {target_fps:g}fps.\n"
            )
        else:
            self.log.emit(f"🔗 Bắt đầu gộp {len(files)} file bằng fast-copy an toàn...\n")

        stamp = f"{int(time.time() * 1000)}_{os.getpid()}"
        list_txt = os.path.join(tempfile.gettempdir(), f"concat_list_{stamp}.txt")
        filter_txt = os.path.join(tempfile.gettempdir(), f"concat_filter_{stamp}.txt")
        si = subprocess.STARTUPINFO() if os.name == "nt" else None
        if si: si.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        def _write_concat_list():
            with open(list_txt, "w", encoding="utf-8") as f:
                for fp in files:
                    safe_path = fp.replace('\\', '/').replace("'", r"\'")
                    f.write(f"file '{safe_path}'\n")

        def _build_filter_concat_base():
            """Dùng input riêng + concat filter để không bị sai duration/timebase
            khi các tập khác FPS/time_base/codec/sample-rate. Đồng thời tự chèn
            silent audio cho tập không có tiếng để concat vẫn giữ audio các tập khác.
            """
            input_args = []
            filter_parts = []
            labels = []
            input_index = 0
            for i, fp in enumerate(files):
                v_idx = input_index
                input_args += ["-i", fp]
                input_index += 1

                if self._probe_has_audio(ffmpeg, fp):
                    a_idx = v_idx
                else:
                    dur = max(0.1, float(_probe_duration_sec(fp) or 0.1))
                    input_args += ["-f", "lavfi", "-t", f"{dur:.3f}",
                                   "-i", "anullsrc=r=48000:cl=stereo"]
                    a_idx = input_index
                    input_index += 1
                    self.log.emit(f"   🔇 {os.path.basename(fp)} không có audio → chèn im lặng.\n")

                filter_parts.append(
                    f"[{v_idx}:v:0]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                    f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                    f"fps={target_fps:g},setpts=PTS-STARTPTS[v{i}]"
                )
                filter_parts.append(
                    f"[{a_idx}:a:0]aformat=sample_rates=48000:channel_layouts=stereo,"
                    f"aresample=async=1,asetpts=PTS-STARTPTS[a{i}]"
                )
                labels.append(f"[v{i}][a{i}]")

            filter_parts.append(
                "".join(labels) + f"concat=n={len(files)}:v=1:a=1[vmerge][amerge]"
            )
            with open(filter_txt, "w", encoding="utf-8") as f:
                f.write(";".join(filter_parts))
            return ([ffmpeg, "-y", "-nostdin", *input_args,
                     "-filter_complex_script", filter_txt,
                     "-map", "[vmerge]", "-map", "[amerge]"])

        def _finish_success(mode):
            self.log.emit(f"✅ Gộp trọn bộ thành công ({mode}): {os.path.basename(self.out_file)}\n")
            thumb_jpg = self._prepare_thumbnail()
            if thumb_jpg:
                self._embed_cover_mp4(ffmpeg, self.out_file, thumb_jpg, si)
                self._export_thumbnail_beside_video(thumb_jpg)
                try: os.remove(thumb_jpg)
                except Exception: pass
            self.done.emit(True, self.out_file)

        try:
            _write_concat_list()
            merge_codec = get_optimal_ffmpeg_codec()
            proc = None
            need_cpu_normalize = False

            if mixed:
                # KHÔNG dùng concat demuxer để re-encode file khác FPS/time_base:
                # nó có thể kéo sai timestamp/duration. Dùng concat filter input riêng.
                base_cmd = _build_filter_concat_base()
                enc_args = build_video_encoder_args(
                    merge_codec, crf_val=20, preset_hw="quality", preset_sw="medium"
                )
                cmd = [*base_cmd, *enc_args,
                       "-c:a", "aac", "-b:a", "192k", "-shortest",
                       "-video_track_timescale", "90000", "-movflags", "+faststart",
                       self.out_file]
                proc = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    startupinfo=si, text=True, encoding="utf-8", errors="replace"
                )
                if proc.returncode != 0 or not os.path.exists(self.out_file) or os.path.getsize(self.out_file) <= 1024:
                    if "libx264" not in (merge_codec or "").lower():
                        self.log.emit("⚠️ Codec phần cứng gộp lỗi → chuyển libx264 (CPU)...\n")
                        need_cpu_normalize = True
                    else:
                        err = (proc.stderr or "")[-1000:]
                        self.log.emit(f"❌ Lỗi gộp/chuẩn hóa FFmpeg:\n{err}\n")
                        self.done.emit(False, "")
                        return
                else:
                    _finish_success("chuẩn hóa + concat")
                    return
            else:
                # Fast path cho các file render mới đã cùng resolution/FPS.
                cmd = [ffmpeg, "-y", "-nostdin", "-f", "concat", "-safe", "0",
                       "-i", list_txt, "-c", "copy", "-movflags", "+faststart",
                       self.out_file]
                proc = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    startupinfo=si, text=True, encoding="utf-8", errors="replace"
                )
                fast_ok = (proc.returncode == 0 and os.path.exists(self.out_file)
                           and os.path.getsize(self.out_file) > 1024)
                if fast_ok and target_fps:
                    out_fps = self._probe_fps(ffmpeg, self.out_file)
                    if out_fps and abs(out_fps - target_fps) > 0.05:
                        self.log.emit(
                            f"⚠️ Fast-copy ra {out_fps:g}fps thay vì {target_fps:g}fps "
                            "→ chuẩn hóa lại bằng CPU.\n"
                        )
                        fast_ok = False
                if fast_ok:
                    _finish_success("copy stream")
                    return
                need_cpu_normalize = True

            if need_cpu_normalize:
                # Fallback chung: input riêng + filter concat + CPU. Không phụ thuộc
                # codec/time_base của từng tập nên an toàn hơn re-encode concat demuxer.
                try:
                    if os.path.exists(self.out_file):
                        os.remove(self.out_file)
                except Exception:
                    pass
                base_cmd = _build_filter_concat_base()
                cmd_fb = [*base_cmd,
                          "-c:v", "libx264", "-crf", "20", "-preset", "medium",
                          "-pix_fmt", "yuv420p",
                          "-c:a", "aac", "-b:a", "192k", "-shortest",
                          "-video_track_timescale", "90000", "-movflags", "+faststart",
                          self.out_file]
                proc = subprocess.run(
                    cmd_fb, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    startupinfo=si, text=True, encoding="utf-8", errors="replace"
                )
                if proc.returncode == 0 and os.path.exists(self.out_file) and os.path.getsize(self.out_file) > 1024:
                    # kiểm tra FPS sau fallback lần cuối
                    out_fps = self._probe_fps(ffmpeg, self.out_file)
                    if out_fps and abs(out_fps - target_fps) > 0.10:
                        self.log.emit(
                            f"⚠️ File gộp tạo được nhưng FPS còn lệch ({out_fps:g}/{target_fps:g}).\n"
                        )
                    _finish_success("libx264 CPU")
                else:
                    err = (proc.stderr or "")[-1000:]
                    self.log.emit(f"❌ Lỗi gộp file FFmpeg:\n{err}\n")
                    self.done.emit(False, "")
        except Exception as e:
            self.log.emit(f"❌ Exception khi gộp: {e}\n")
            self.done.emit(False, "")
        finally:
            for tmp in (list_txt, filter_txt):
                try:
                    if tmp and os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass

def _probe_duration_sec(video_path):
    """Đọc tổng thời lượng (giây) của video bằng ffmpeg -i. Trả 0 nếu không đọc được."""
    try:
        ffmpeg_bin = get_ffmpeg_path()
        p = subprocess.run([ffmpeg_bin, "-i", video_path], stderr=subprocess.PIPE,
                           text=True, errors="ignore",
                           creationflags=CREATE_NO_WINDOW if os.name == 'nt' else 0)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr)
        if m:
            h, mi, s = m.group(1), m.group(2), m.group(3)
            return float(h) * 3600 + float(mi) * 60 + float(s)
    except Exception:
        pass
    return 0.0


class SingleRenderThread(QThread):
    log = pyqtSignal(str); done = pyqtSignal(bool)
    progress = pyqtSignal(int)   # % thật của tập đang render (0..100)
    def __init__(self, vp, vi_srt_path, tts_path, out_path, render_cfg):
        super().__init__(); self.vp = vp; self.sp = vi_srt_path; self.tts_path = tts_path; self.op = out_path; self.cfg = render_cfg; self._cancel = False
        self._total_dur = 0.0
    def cancel(self): self._cancel = True

    def _emit_progress_from_line(self, line, last_pct):
        """Từ 1 dòng ffmpeg (out_time_ms=... hoặc time=HH:MM:SS), tính % và phát signal.
        Trả về pct mới (để không phát trùng). Cần self._total_dur > 0."""
        if self._total_dur <= 0:
            return last_pct
        cur = None
        m = re.search(r"out_time_ms=(\d+)", line)
        if m:
            cur = int(m.group(1)) / 1_000_000.0
        else:
            m = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
            if m:
                cur = float(m.group(1)) * 3600 + float(m.group(2)) * 60 + float(m.group(3))
        if cur is None:
            return last_pct
        pct = int(max(0, min(99, cur / self._total_dur * 100)))
        if pct != last_pct:
            self.progress.emit(pct)
        return pct
    
    def run(self):
        start_t = time.time() 
        self.progress.emit(0)
        self._total_dur = _probe_duration_sec(self.vp)
        self.log.emit(f"🎬 Bắt đầu Render & Ép phụ đề...\n")
        quality_text = self.cfg.get("render_quality", "⭐ Tốt (CRF 20)")
        self.log.emit(f"   📊 Chất lượng: {quality_text} | Codec: {get_optimal_ffmpeg_codec()}\n")
        _fb = get_codec_fallback_reason()
        if _fb:
            self.log.emit(f"   ⚠️ {_fb}\n")
        codec = get_optimal_ffmpeg_codec()

        has_audio = False
        try:
            ffmpeg_bin = get_ffmpeg_path()
            probe = subprocess.run([ffmpeg_bin, "-i", self.vp], stderr=subprocess.PIPE, text=True, errors="ignore", creationflags=CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if "Audio:" in probe.stderr:
                has_audio = True
        except: pass
        
        vid_w = int(self.cfg["scene_w"])
        vid_h = int(self.cfg["scene_h"])
        
        m_l, m_r, m_v = max(0, int(self.cfg["margin_l"])), max(0, int(self.cfg["margin_r"])), max(0, int(self.cfg["margin_v"]))
        f_size = max(10, int(self.cfg["font_size"]))
        f_color = self.cfg.get("font_color", "&H0000FFFF")

        # Nền ô chữ (opaque box) sau chữ: BorderStyle=3 + BackColour là màu nền.
        # Nếu tắt -> viền thường (BorderStyle=1, outline đen).
        if self.cfg.get("subbox_en"):
            back = self.cfg.get("subbox_color", "&H80000000")  # mặc định đen mờ ~50%
            style = (f"FontName={self.cfg['font_name']},FontSize={f_size},PrimaryColour={f_color},"
                     f"OutlineColour={back},BackColour={back},BorderStyle=3,Outline=6,Shadow=0,"
                     f"Alignment=2,PlayResX={vid_w},PlayResY={vid_h},MarginL={m_l},MarginR={m_r},MarginV={m_v}")
        else:
            style = (f"FontName={self.cfg['font_name']},FontSize={f_size},PrimaryColour={f_color},"
                     f"OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,"
                     f"Alignment=2,PlayResX={vid_w},PlayResY={vid_h},MarginL={m_l},MarginR={m_r},MarginV={m_v}")

        basename = os.path.basename(self.vp)
        temp_srt = ""; escaped_srt = ""
        if self.sp and os.path.exists(self.sp):
            if not _srt_has_content(self.sp):
                # Tập không có thoại (sub rỗng) -> KHÔNG ép sub, render giữ
                # tiếng gốc bình thường. Tránh FFmpeg lỗi 'Invalid data'.
                self.log.emit("   ℹ️ Phụ đề rỗng (tập không thoại) → render không ép sub, giữ tiếng gốc.\n")
            else:
                temp_srt = os.path.join(os.path.dirname(self.op), f"_temp_sub_{basename}.srt")
                try: shutil.copy2(self.sp, temp_srt); escaped_srt = _escape_ffmpeg_path(temp_srt)
                except Exception as e: self.log.emit(f"⚠️ Không thể copy SRT tạm: {e}\n"); escaped_srt = _escape_ffmpeg_path(self.sp)

        inputs = ["-hwaccel", "none", "-threads", "0", "-i", self.vp]
        filter_chains = []
        last_vid_out = "[0:v]"
        vid_filters = []
        
        if self.cfg.get("rotate_en"):
            vid_filters.append("rotate=1*PI/180:ow=iw:oh=ih")
            
        if self.cfg.get("blur_en") and self.cfg.get("blur_list"):
            enable_cmd = ""
            orig_srt = os.path.splitext(self.vp)[0] + ".srt"
            srt_for_blur = orig_srt if os.path.exists(orig_srt) else self.sp
            if srt_for_blur and os.path.exists(srt_for_blur):
                merged = _merge_srt_intervals(srt_for_blur, gap=0.3, expand=0.5)
                if merged:
                    covered = sum(e - s for s, e in merged)
                    span = merged[-1][1] - merged[0][0]
                    if span > 0 and covered / span > 0.85:
                        self.log.emit(f"   ℹ️ Thoại dày ({len(merged)} đoạn, phủ {covered/span*100:.0f}%) → làm mờ liên tục cho nhanh.\n")
                    else:
                        intervals = [f"between(t,{s:.3f},{e:.3f})" for s, e in merged]
                        enable_cmd = f":enable='{'+'.join(intervals)}'"
                        self.log.emit(f"   ℹ️ Vùng mờ: {len(merged)} đoạn theo phụ đề.\n")
                    
            for b in self.cfg["blur_list"]:
                bx = max(1, min(int(b['x']), vid_w - 3))
                by = max(1, min(int(b['y']), vid_h - 3))
                bw = max(2, min(int(b['w']), vid_w - bx - 1))
                bh = max(2, min(int(b['h']), vid_h - by - 1))
                vid_filters.append(f"delogo=x={bx}:y={by}:w={bw}:h={bh}{enable_cmd}")
            
        if self.cfg.get("flip"): vid_filters.append("hflip")
        if self.cfg.get("zoom"): vid_filters.append("crop=iw*0.96:ih*0.96,scale=trunc(iw/2)*2:trunc(ih/2)*2")
        if self.cfg.get("color"): vid_filters.append("eq=contrast=1.05:brightness=0.02:saturation=1.1")
        if self.cfg.get("noise"): vid_filters.append("noise=alls=1:allf=t")
        
        vid_filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")

        # Ép FPS đồng nhất (nếu người dùng chọn 24/25/30). Mọi tập ra cùng fps
        # -> gộp trọn bộ nhanh (copy) & KHÔNG bị đứng hình ở tập lệch fps.
        _tfps = self.cfg.get("target_fps")
        if _tfps:
            vid_filters.append(f"fps={_tfps:g}")
        
        if vid_filters: 
            filter_chains.append(f"[0:v] {','.join(vid_filters)} [v_base]")
            last_vid_out = "[v_base]"

        if self.cfg.get("frame_en") and self.cfg.get("frame_path") and os.path.exists(self.cfg.get("frame_path")):
            frame_idx = inputs.count("-i")
            inputs.extend(["-loop", "1", "-i", self.cfg["frame_path"]])
            filter_chains.append(f"[{frame_idx}:v] format=yuva420p,scale={vid_w}:{vid_h} [frame_s]")
            filter_chains.append(f"{last_vid_out}[frame_s] overlay=0:0:shortest=1 [v_framed]")
            last_vid_out = "[v_framed]"

        logo_en = self.cfg.get("logo_en")
        logo_path = self.cfg.get("logo_path")
        if logo_en and not logo_path:
            self.log.emit("⚠️ Logo đang BẬT nhưng chưa chọn file ảnh -> bỏ qua logo.\n")
        elif logo_en and logo_path and not os.path.exists(logo_path):
            self.log.emit(f"⚠️ Logo đang BẬT nhưng KHÔNG TÌM THẤY file tại đường dẫn:\n    {logo_path}\n"
                           f"    (File có thể đã bị xoá/di chuyển sau khi chọn) -> bỏ qua logo.\n")
        elif logo_en and logo_path and os.path.exists(logo_path):
            try:
                logo_idx = inputs.count("-i")
                inputs.extend(["-loop", "1", "-i", logo_path])
                lx, ly = int(self.cfg["logo_x"]), int(self.cfg["logo_y"]); logo_scale = self.cfg.get("logo_scale", 1.0)
                if abs(logo_scale - 1.0) > 0.01: 
                    filter_chains.append(f"[{logo_idx}:v] format=yuva420p,scale=iw*{logo_scale:.3f}:ih*{logo_scale:.3f} [logo_s]")
                else:
                    filter_chains.append(f"[{logo_idx}:v] format=yuva420p [logo_s]")
                filter_chains.append(f"{last_vid_out}[logo_s] overlay=x={lx}:y={ly}:shortest=1 [v_logo]")
                last_vid_out = "[v_logo]"
                self.log.emit(f"   🖼️ Đã chèn logo tại x={lx}, y={ly}, scale={logo_scale:.3f}\n")
            except Exception as e:
                self.log.emit(f"⚠️ Lỗi chèn logo vào filter chain, bỏ qua logo: {e}\n")

        if escaped_srt and self.cfg.get("hardsub_en", True): 
            filter_chains.append(f"{last_vid_out} subtitles='{escaped_srt}':force_style='{style}' [vout]")
            last_vid_out = "[vout]"
            
        if self.cfg.get("speed"): 
            filter_chains.append(f"{last_vid_out} setpts=PTS/1.05 [v_speed]")
            last_vid_out = "[v_speed]"

        # Ép RESOLUTION đích (nếu chọn) — bước CUỐI, sau khi khắc sub. Giữ đúng
        # tỷ lệ (scale + pad), nên sub co giãn theo khung, KHÔNG lệch/méo. Mọi
        # tập ra cùng size -> gộp trọn bộ nhanh (copy), khỏi re-encode.
        _tres = self.cfg.get("target_res")
        if _tres:
            tw, th = int(_tres[0]), int(_tres[1])
            tw = (tw // 2) * 2; th = (th // 2) * 2
            filter_chains.append(
                f"{last_vid_out} scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1 [v_res]")
            last_vid_out = "[v_res]"

        audio_map = ""
        if self.cfg.get("tts_en") and self.tts_path and os.path.exists(self.tts_path):
            tts_idx = inputs.count("-i")
            inputs.extend(["-i", self.tts_path])
            tts_v = self.cfg.get("tts_ai_vol", 150) / 100.0
            orig_v = self.cfg.get("tts_orig_vol", 15) / 100.0
            
            # ĐÃ FIX TRIỆT ĐỂ: Nếu có audio gốc VÀ Vol gốc > 0 thì mới mix, ngược lại bỏ qua hoàn toàn audio gốc để tránh lỗi file hỏng
            if has_audio and orig_v > 0:
                filter_chains.append(f"[0:a]aformat=channel_layouts=stereo,volume={orig_v:.2f}[a_orig];[{tts_idx}:a]aformat=channel_layouts=stereo,volume={tts_v:.2f}[a_tts];[a_orig][a_tts]amix=inputs=2:duration=first[a_mixed]")
                audio_map = "[a_mixed]"
            else:
                filter_chains.append(f"[{tts_idx}:a]aformat=channel_layouts=stereo,volume={tts_v:.2f}[a_mixed]")
                audio_map = "[a_mixed]"
        else:
            if has_audio:
                orig_v = self.cfg.get("tts_orig_vol", 15) / 100.0
                if orig_v > 0:
                    audio_map = "0:a"

        af_chain = []
        speed_val = 1.05 if self.cfg.get("speed") else 1.0
        pitch_val = 1.15 if self.cfg.get("pitch") else 1.0
        
        if self.cfg.get("pitch"):
            af_chain.append(f"aresample=48000,asetrate=48000*{pitch_val},aresample=48000,atempo={speed_val}/{pitch_val}")
        elif self.cfg.get("speed"):
            af_chain.append(f"atempo={speed_val}")
            
        if af_chain and audio_map: 
            pad = audio_map if audio_map.startswith("[") else f"[{audio_map}]"
            filter_chains.append(f"{pad} {','.join(af_chain)} [aout]")
            audio_map = "[aout]"
            
        cmd = [get_ffmpeg_path(), "-y", "-progress", "pipe:1"] + inputs; temp_filter = ""
        
        if filter_chains:
            basename = os.path.basename(self.vp)
            temp_filter = os.path.join(os.path.dirname(self.op), f"_temp_filter_{basename}.txt")
            with open(temp_filter, "w", encoding="utf-8") as f: f.write(";".join(filter_chains))
            
            vid_out_map = "0:v" if last_vid_out == "[0:v]" else last_vid_out
            cmd.extend(["-filter_complex_script", temp_filter, "-map", vid_out_map])
            
            if audio_map: cmd.extend(["-map", audio_map])
            
            quality_text = self.cfg.get("render_quality", "⭐ Tốt (CRF 20 - Đề xuất)")
            if "CRF 16" in quality_text:
                crf_val, preset_sw, preset_hw = 16, "slow", "quality"
            elif "CRF 20" in quality_text:
                crf_val, preset_sw, preset_hw = 20, "medium", "quality"
            elif "CRF 26" in quality_text:
                crf_val, preset_sw, preset_hw = 26, "medium", "speed"
            else:
                crf_val, preset_sw, preset_hw = 0, "fast", "speed"
            
            use_crf = crf_val > 0
            self._hw_codec = codec  # lưu để fallback CPU nếu HW lỗi
            self._enc_ctx = dict(crf_val=crf_val, preset_sw=preset_sw,
                                 preset_hw=preset_hw, use_crf=use_crf,
                                 audio_map=bool(audio_map))

            enc_args = []
            if "amf" in codec.lower() or "nvenc" in codec.lower() or "qsv" in codec.lower():
                enc_args.extend(["-c:v", codec, "-pix_fmt", "yuv420p"])
                if audio_map: enc_args.extend(["-c:a", "aac", "-b:a", "192k"])

                if use_crf:
                    if "nvenc" in codec.lower():
                        # Dịch preset riêng cho NVIDIA để không báo lỗi
                        nv_preset = "hq" if preset_hw == "quality" else "fast"
                        enc_args.extend(["-rc", "constqp", "-qp", str(crf_val), "-preset", nv_preset])
                    elif "amf" in codec.lower():
                        enc_args.extend(["-rc", "cqp", "-qp_i", str(crf_val), "-qp_p", str(crf_val), "-quality", preset_hw])
                    elif "qsv" in codec.lower():
                        enc_args.extend(["-global_quality", str(crf_val), "-preset", preset_hw])
                else:
                    if "nvenc" in codec.lower():
                        nv_preset = "hq" if preset_hw == "quality" else "fast"
                        enc_args.extend(["-b:v", "1000k", "-preset", nv_preset])
                    else:
                        enc_args.extend(["-b:v", "1000k", "-preset", preset_hw])
                if audio_map:
                    # Không để AAC dài hơn video vài ms; nếu audio dài hơn, concat -c copy
                    # sẽ tạo khe timestamp ở điểm nối và có thể làm FPS/tbr nhảy sai.
                    enc_args.extend(["-shortest"])
                enc_args.extend(["-movflags", "+faststart", self.op])
            else:
                enc_args.extend(["-c:v", codec, "-pix_fmt", "yuv420p"])
                if audio_map: enc_args.extend(["-c:a", "aac", "-b:a", "192k"])
                if use_crf:
                    enc_args.extend(["-crf", str(crf_val), "-preset", preset_sw])
                else:
                    enc_args.extend(["-b:v", "1000k", "-preset", preset_sw])
                if audio_map:
                    enc_args.extend(["-shortest"])
                enc_args.extend(["-movflags", "+faststart", self.op])
            cmd.extend(enc_args)
        else:
            cmd.extend(["-map", "0:v"])
            if audio_map: cmd.extend(["-map", audio_map, "-c", "copy"])
            else: cmd.extend(["-c:v", "copy"])
            cmd.extend(["-movflags", "+faststart", self.op])

        try:
            self.log.emit(f"  ⚙️ Đang xử lý FFmpeg, vui lòng đợi...\n")
            kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **kw)
            from collections import deque
            stderr_lines = deque(maxlen=40)
            def _drain_stderr(p=proc, buf=stderr_lines):
                try:
                    for ln in p.stderr:
                        buf.append(ln)
                except Exception:
                    pass
            _t_err = threading.Thread(target=_drain_stderr, daemon=True)
            _t_err.start()
            last_report = time.time(); last_pct = 0
            for line in proc.stdout:  # -progress ghi ra stdout: out_time_ms=, progress=
                if "out_time_ms=" in line or "time=" in line:
                    last_pct = self._emit_progress_from_line(line, last_pct)
                    if time.time() - last_report > 20:
                        m = re.search(r"out_time_ms=(\d+)", line)
                        if m:
                            self.log.emit(f"   ⏳ Render: {format_time(int(m.group(1))/1_000_000)} ({last_pct}%)\n")
                        last_report = time.time()
                if self._cancel:
                    try: proc.terminate()
                    except Exception: pass
                    break
            proc.wait()
            _t_err.join(timeout=2) 
            elapsed = time.time() - start_t
            if self._cancel:
                self.log.emit(f"⛔ Đã hủy render.\n")
                try:
                    if os.path.exists(self.op): os.remove(self.op)
                except Exception: pass
                self.done.emit(False)
            elif proc.returncode == 0:
                self.progress.emit(100)
                self.log.emit(f"⏱️ Render hoàn thành trong: {format_time(elapsed)}\n")
                self.done.emit(True)
            else:
                tail = "".join(list(stderr_lines)[-15:])
                # ── Fallback CPU: nếu vừa render bằng codec phần cứng (NVENC/AMF/QSV)
                #    mà lỗi (card đời cũ không hỗ trợ, hết session, driver cũ...) thì
                #    tự hạ về libx264 (CPU) rồi chạy lại — thay vì báo lỗi luôn. ──
                hw = getattr(self, "_hw_codec", "").lower()
                is_hw = any(k in hw for k in ("nvenc", "amf", "qsv"))
                if is_hw and not self._cancel:
                    self.log.emit(
                        f"⚠️ Card đồ họa không encode được bằng {self._hw_codec} "
                        f"(code {proc.returncode}) → tự chuyển sang CPU (libx264)...\n"
                    )
                    ctx = getattr(self, "_enc_ctx", {})
                    crf_val = ctx.get("crf_val", 20)
                    preset_sw = ctx.get("preset_sw", "medium")
                    use_crf = ctx.get("use_crf", True)
                    # Thay phần enc_args cũ (đuôi cmd) bằng libx264. enc_args cũ luôn
                    # kết thúc bằng [..., "-movflags", "+faststart", self.op] và bắt
                    # đầu bằng ["-c:v", <hw_codec>]; ta cắt từ "-c:v" cuối cùng.
                    try:
                        cut = len(cmd) - 1 - cmd[::-1].index("-c:v")
                        base_cmd = cmd[:cut]
                    except ValueError:
                        base_cmd = cmd  # phòng hờ, không nên xảy ra
                    cpu_args = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
                    if ctx.get("audio_map"):
                        cpu_args += ["-c:a", "aac", "-b:a", "192k"]
                    if use_crf:
                        cpu_args += ["-crf", str(crf_val), "-preset", preset_sw]
                    else:
                        cpu_args += ["-b:v", "1000k", "-preset", preset_sw]
                    if ctx.get("audio_map"):
                        cpu_args += ["-shortest"]
                    cpu_args += ["-movflags", "+faststart", self.op]
                    cmd_cpu = base_cmd + cpu_args

                    kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
                    proc = subprocess.Popen(cmd_cpu, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            text=True, encoding="utf-8", errors="replace", **kw)
                    stderr_lines2 = deque(maxlen=40)
                    def _drain_stderr2(p=proc, buf=stderr_lines2):
                        try:
                            for ln in p.stderr:
                                buf.append(ln)
                        except Exception:
                            pass
                    _t_err2 = threading.Thread(target=_drain_stderr2, daemon=True)
                    _t_err2.start()
                    last_report = time.time(); last_pct = 0
                    self.progress.emit(0)
                    for line in proc.stdout:
                        if "out_time_ms=" in line or "time=" in line:
                            last_pct = self._emit_progress_from_line(line, last_pct)
                            if time.time() - last_report > 20:
                                m = re.search(r"out_time_ms=(\d+)", line)
                                if m:
                                    self.log.emit(f"   ⏳ Render (CPU): {format_time(int(m.group(1))/1_000_000)} ({last_pct}%)\n")
                                last_report = time.time()
                        if self._cancel:
                            try: proc.terminate()
                            except Exception: pass
                            break
                    proc.wait()
                    _t_err2.join(timeout=2)
                    if self._cancel:
                        self.log.emit("⛔ Đã hủy render.\n"); self.done.emit(False)
                    elif proc.returncode == 0:
                        self.progress.emit(100)
                        self.log.emit(f"✅ Render thành công bằng CPU (libx264) trong: {format_time(time.time() - start_t)}\n")
                        self.done.emit(True)
                    else:
                        tail2 = "".join(list(stderr_lines2)[-15:])
                        self.log.emit(f"❌ FFmpeg lỗi cả khi dùng CPU (code {proc.returncode})\n📋 Chi tiết:\n{tail2}\n")
                        self.done.emit(False)
                else:
                    self.log.emit(f"❌ FFmpeg lỗi (code {proc.returncode})\n📋 Chi tiết:\n{tail}\n"); self.done.emit(False)
        except Exception as e:
            self.log.emit(f"❌ Lỗi: {e}\n"); self.done.emit(False)
        finally:
            for tmp in [temp_filter, temp_srt]:
                try:
                    if tmp and os.path.exists(tmp): os.remove(tmp)
                except Exception: pass

# ============================================================
# CÁC CLASS ĐỒ HỌA
# ============================================================
_HS = 8; _HH = _HS / 2
def _inset_corners(rect):
    r = rect; s = _HS
    return {"tl": QRectF(r.left(), r.top(), s, s), "tr": QRectF(r.right() - s, r.top(), s, s), "bl": QRectF(r.left(), r.bottom() - s, s, s), "br": QRectF(r.right() - s, r.bottom() - s, s, s)}

def _draw_handles(painter, handle_dict):
    painter.setPen(QPen(QColor(255, 255, 255, 200), 1)); painter.setBrush(QBrush(QColor(255, 255, 255, 200)))
    for hr in handle_dict.values(): painter.drawRect(hr)

class DraggableBlurBox(QGraphicsRectItem):
    _VIS = 12; _HIT = 24   
    def __init__(self, x, y, w, h):
        super().__init__(0, 0, w, h)
        self.setPos(x, y)
        self.setPen(QPen(QColor(255, 40, 40, 255), 2.5, Qt.PenStyle.DashLine))
        self.setBrush(QBrush(QColor(255, 0, 0, 40)))
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setZValue(1)
        self._resizing = False; self._handle = None; self._start_scene = QPointF(); self._start_pos = QPointF(); self._start_rect = QRectF()
    def _build_handles(self, rect, size):
        r = rect; hs = size / 2; cx, cy = r.center().x(), r.center().y()
        return {"tl": QRectF(r.left() - hs, r.top() - hs, size, size), "tm": QRectF(cx - hs, r.top() - hs, size, size), "tr": QRectF(r.right() - hs, r.top() - hs, size, size), "ml": QRectF(r.left() - hs, cy - hs, size, size), "mr": QRectF(r.right() - hs, cy - hs, size, size), "bl": QRectF(r.left() - hs, r.bottom() - hs, size, size), "bm": QRectF(cx - hs, r.bottom() - hs, size, size), "br": QRectF(r.right() - hs, r.bottom() - hs, size, size)}
    def boundingRect(self):
        br = super().boundingRect(); m = self._HIT / 2 + 2; return br.adjusted(-m, -m, m, m)
    def paint(self, painter, option, widget=None):
        painter.setPen(self.pen()); painter.setBrush(self.brush()); painter.drawRect(self.rect())
        if self.isSelected():
            vis = self._build_handles(self.rect(), self._VIS); painter.setPen(QPen(QColor(255, 255, 255, 220), 1)); painter.setBrush(QBrush(QColor(255, 80, 80, 220)))
            for hr in vis.values(): painter.drawRect(hr)
    def mousePressEvent(self, event):
        if self.isSelected() and event.button() == Qt.MouseButton.LeftButton:
            for name, hr in self._build_handles(self.rect(), self._HIT).items():
                if hr.contains(event.pos()):
                    self._resizing = True; self._handle = name; self._start_scene = event.scenePos(); self._start_pos = QPointF(self.pos()); self._start_rect = QRectF(self.rect()); event.accept(); return
        super().mousePressEvent(event)
    def mouseMoveEvent(self, event):
        if self._resizing:
            delta = event.scenePos() - self._start_scene; h = self._handle; ox, oy = self._start_pos.x(), self._start_pos.y(); ow, oh = self._start_rect.width(), self._start_rect.height(); nx, ny, nw, nh = ox, oy, ow, oh
            if "l" in h: nx = ox + delta.x(); nw = ow - delta.x()
            if "r" in h: nw = ow + delta.x()
            if "t" in h: ny = oy + delta.y(); nh = oh - delta.y()
            if "b" in h: nh = oh + delta.y()
            if nw < 20: nw = 20; nx = ox + ow - 20 if "l" in h else nx
            if nh < 20: nh = 20; ny = oy + oh - 20 if "t" in h else ny
            self.prepareGeometryChange(); self.setPos(nx, ny); self.setRect(0, 0, nw, nh); event.accept(); return
        super().mouseMoveEvent(event)
    def mouseReleaseEvent(self, event):
        if self._resizing: self._resizing = False; self._handle = None; event.accept(); return
        super().mouseReleaseEvent(event)

class ScalablePixmapItem(QGraphicsPixmapItem):
    def __init__(self):
        super().__init__(); self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable); self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache); self._resizing = False; self._handle = None; self._start_scene = QPointF(); self._start_scale = 1.0; self._start_diag = 1.0
    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        if self.isSelected() and not self.pixmap().isNull(): _draw_handles(painter, _inset_corners(super().boundingRect()))
    def _anchor_scene(self, handle):
        br = super().boundingRect(); opp = {"tl": br.bottomRight(), "tr": br.bottomLeft(), "bl": br.topRight(), "br": br.topLeft()}; return self.mapToScene(opp.get(handle, br.center()))
    def mousePressEvent(self, event):
        if self.isSelected() and event.button() == Qt.MouseButton.LeftButton:
            br = super().boundingRect()
            if not br.isEmpty():
                for name, hr in _inset_corners(br).items():
                    if hr.contains(event.pos()): self._resizing = True; self._handle = name; self._start_scene = event.scenePos(); self._start_scale = self.scale(); anchor = self._anchor_scene(name); self._start_diag = max(1.0, (self._start_scene - anchor).manhattanLength()); event.accept(); return
        super().mousePressEvent(event)
    def mouseMoveEvent(self, event):
        if self._resizing: anchor = self._anchor_scene(self._handle); cur_diag = max(1.0, (event.scenePos() - anchor).manhattanLength()); ratio = cur_diag / self._start_diag; new_scale = max(0.1, min(10.0, self._start_scale * ratio)); self.setScale(new_scale); event.accept(); return
        super().mouseMoveEvent(event)
    def mouseReleaseEvent(self, event):
        if self._resizing: self._resizing = False; event.accept(); return
        super().mouseReleaseEvent(event)

class ScalableTextItem(QGraphicsTextItem):
    def __init__(self, text=""):
        super().__init__(text); self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable); self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache); self._resizing = False; self._handle = None; self._start_scene = QPointF(); self._start_scale = 1.0; self._start_diag = 1.0
    def paint(self, painter, option, widget=None):
        option.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, option, widget)
        if self.isSelected():
            painter.setPen(QPen(QColor(255, 255, 255, 80), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(super().boundingRect())
            _draw_handles(painter, _inset_corners(super().boundingRect()))
    def _anchor_scene(self, handle):
        br = super().boundingRect(); opp = {"tl": br.bottomRight(), "tr": br.bottomLeft(), "bl": br.topRight(), "br": br.topLeft()}; return self.mapToScene(opp.get(handle, br.center()))
    def mousePressEvent(self, event):
        if self.isSelected() and event.button() == Qt.MouseButton.LeftButton:
            for name, hr in _inset_corners(super().boundingRect()).items():
                if hr.contains(event.pos()): self._resizing = True; self._handle = name; self._start_scene = event.scenePos(); self._start_scale = self.scale(); anchor = self._anchor_scene(name); self._start_diag = max(1.0, (self._start_scene - anchor).manhattanLength()); event.accept(); return
        super().mousePressEvent(event)
    def mouseMoveEvent(self, event):
        if self._resizing: anchor = self._anchor_scene(self._handle); cur_diag = max(1.0, (event.scenePos() - anchor).manhattanLength()); ratio = cur_diag / self._start_diag; new_scale = max(0.15, min(8.0, self._start_scale * ratio)); self.setScale(new_scale); event.accept(); return
        super().mouseMoveEvent(event)
    def mouseReleaseEvent(self, event):
        if self._resizing: self._resizing = False; event.accept(); return
        super().mouseReleaseEvent(event)

class PreviewGraphicsView(QGraphicsView):
    def __init__(self, scene, parent=None):
        super().__init__(scene, parent); self.setStyleSheet("background: #000; border-radius: 6px; border: none;"); self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); self.setRenderHint(QPainter.RenderHint.Antialiasing); self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform); self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
    def wheelEvent(self, event):
        item = self.scene().focusItem() or next(iter(self.scene().selectedItems()), None)
        if item and isinstance(item, (ScalablePixmapItem, ScalableTextItem)):
            factor = 1.1 if event.angleDelta().y() > 0 else 0.9; new_scale = max(0.15, min(8.0, item.scale() * factor)); item.setScale(new_scale); event.accept()
        elif item and isinstance(item, DraggableBlurBox):
            delta = 10 if event.angleDelta().y() > 0 else -10; r = item.rect(); nw = max(20, r.width() + delta); nh = max(20, r.height() + delta); item.prepareGeometryChange(); item.setRect(0, 0, nw, nh); event.accept()
        else: super().wheelEvent(event)
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.scene() and not self.scene().sceneRect().isEmpty(): self.fitInView(self.scene().sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

# ============================================================
# WIDGET & UI CẬP NHẬT
# ============================================================


# ============================================================
#  CARD MỖI TẬP TRONG GRID + THANH TIẾN ĐỘ
# ============================================================
_VIDEO_THUMB_CACHE = {}
_VIDEO_THUMB_POOL = ThreadPoolExecutor(max_workers=1)


def _video_thumb_path(video_path):
    """Tạo/cached thumbnail 16:9 thật từ video để card nhìn như editor chuyên nghiệp.
    Lỗi ffmpeg thì trả None và card dùng placeholder, không ảnh hưởng render."""
    try:
        if not video_path or not os.path.exists(video_path):
            return None
        mtime = int(os.path.getmtime(video_path))
        key = (os.path.abspath(video_path), mtime)
        cached = _VIDEO_THUMB_CACHE.get(key)
        if cached and os.path.exists(cached):
            return cached
        out_dir = os.path.join(tempfile.gettempdir(), "boom_render_thumbs")
        os.makedirs(out_dir, exist_ok=True)
        # Dùng hash ổn định để cache thumbnail còn dùng lại được sau khi mở app lần sau.
        import hashlib
        cache_id = hashlib.sha1(f"{key[0]}|{key[1]}".encode("utf-8", "ignore")).hexdigest()[:20]
        out = os.path.join(out_dir, f"thumb_{cache_id}.jpg")
        if not os.path.exists(out) or os.path.getsize(out) < 2500:
            ff = get_ffmpeg_path()
            # Thumbnail chỉ dùng để xem trong card nên 360px là đủ. Ép 1 thread để
            # không giành CPU với UI/render chính khi thư mục có 50-200 video.
            cmd = [ff, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-ss", "0.6", "-i", video_path, "-frames:v", "1",
                   "-threads", "1", "-filter_threads", "1",
                   "-vf", "scale=360:-2", "-q:v", "5", out]
            kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
            if os.name == "nt":
                kwargs["creationflags"] = CREATE_NO_WINDOW
            subprocess.run(cmd, **kwargs)
        if os.path.exists(out) and os.path.getsize(out) > 3000:
            _VIDEO_THUMB_CACHE[key] = out
            return out
    except Exception:
        pass
    return None


class EpisodeCard(QFrame):
    """Card kiểu Video Editor: checkbox + badge nguồn + thumbnail 16:9 + timeline.
    Giữ nguyên API cũ (lbl_name/lbl_srt/lbl_badge/video_path/srt_path) để backend
    render, dịch, lồng tiếng không phải thay đổi theo giao diện mới."""
    clicked = pyqtSignal(object)
    play_requested = pyqtSignal(object)
    zoom_requested = pyqtSignal(object)
    seek_requested = pyqtSignal(object, int)
    selection_changed = pyqtSignal()
    thumb_ready = pyqtSignal(str, str)   # source_video, thumb_path

    def __init__(self, video_path, srt_path, parent=None):
        super().__init__(parent)
        self.video_path = video_path
        self.srt_path = srt_path
        self.selected = False
        # Mỗi card giữ cấu hình chỉnh sửa RIÊNG. Chỉ khi bấm "Đồng bộ" mới
        # sao chép cấu hình của card đang chọn sang các card khác.
        self.design_config = None
        self._thumb_pix = QPixmap()
        self._thumb_loaded = False
        self._thumb_loading = False
        self._thumb_source = ""
        self._thumb_last_size = QSize()
        self.setMinimumWidth(245)
        self.setFixedHeight(276)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("EpisodeCard")
        self.thumb_ready.connect(self._apply_thumbnail_path)
        self._build()
        # KHÔNG tạo thumbnail cho toàn bộ 55/100 card ngay lúc mở. RenderWidget
        # sẽ chỉ gọi ensure_thumbnail() cho những card đang nằm gần vùng nhìn thấy.
        self.lbl_thumb.setText("🎬\nSẵn sàng xem trước")
        self._thumb_resize_timer = QTimer(self)
        self._thumb_resize_timer.setSingleShot(True)
        self._thumb_resize_timer.setInterval(90)
        self._thumb_resize_timer.timeout.connect(self._refresh_thumb_pixmap)
        self._update_source_badges()
        self._apply_style()

    def _badge(self, text, fg, bg):
        w = QLabel(text)
        w.setStyleSheet(
            f"QLabel {{ color:{fg}; background:{bg}; border:1px solid {fg}; "
            "border-radius:4px; padding:1px 5px; font-size:9px; font-weight:700; }}")
        return w

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(7, 7, 7, 7)
        lay.setSpacing(5)

        # Dòng đầu: chọn tập + tên + trạng thái
        top = QHBoxLayout(); top.setSpacing(5)
        self.chk_select = QCheckBox()
        self.chk_select.setChecked(True)
        self.chk_select.setToolTip("Chọn/bỏ chọn tập này khi Xuất hoặc Gộp")
        self.chk_select.toggled.connect(lambda _v: self.selection_changed.emit())
        top.addWidget(self.chk_select)
        self.lbl_name = QLabel(os.path.basename(self.video_path))
        self.lbl_name.setToolTip(self.video_path)
        self.lbl_name.setStyleSheet("color:#E8EAED; font-weight:700; font-size:10px; border:none;")
        self.lbl_name.setWordWrap(False)
        top.addWidget(self.lbl_name, 1)
        self.lbl_badge = QLabel("chờ")
        self.lbl_badge.setStyleSheet(
            "background:#30343B; color:#AEB4BE; font-size:9px; padding:2px 6px; "
            "border-radius:4px; border:none; font-weight:700;")
        top.addWidget(self.lbl_badge)
        lay.addLayout(top)

        # Badge Gốc / Dịch / Giọng như ảnh mẫu
        badges = QHBoxLayout(); badges.setSpacing(4)
        self.badge_original = self._badge("Gốc", "#7DB7FF", "#172A42")
        self.badge_translate = self._badge("Dịch", "#FFB15C", "#3A2815")
        self.badge_voice = self._badge("Giọng", "#63D79A", "#163326")
        badges.addWidget(self.badge_original)
        badges.addWidget(self.badge_translate)
        badges.addWidget(self.badge_voice)
        badges.addStretch()
        lay.addLayout(badges)

        # Khung xem trước 16:9 ngay TRÊN TỪNG CARD. Bình thường hiện thumbnail;
        # card đang chọn sẽ nhận shared PreviewGraphicsView từ RenderWidget để
        # phát video thật và kéo trực tiếp sub/logo/vùng mờ ngay tại đây.
        self.preview_host = QFrame()
        self.preview_host.setMinimumHeight(150)
        self.preview_host.setStyleSheet(
            "QFrame { background:#08111F; border:1px solid #233B5C; border-radius:7px; }")
        self.preview_lay = QVBoxLayout(self.preview_host)
        self.preview_lay.setContentsMargins(0, 0, 0, 0)
        self.preview_lay.setSpacing(0)
        self.lbl_thumb = QLabel("🎬\nĐang lấy khung hình...")
        self.lbl_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_thumb.setStyleSheet(
            "QLabel { background:#0B0D10; color:#737983; border:none; "
            "border-radius:5px; font-size:11px; }")
        self.preview_lay.addWidget(self.lbl_thumb)
        self._preview_attached = False
        lay.addWidget(self.preview_host, 1)

        # Phụ đề đi kèm, giữ object cũ để các hàm khác cập nhật được
        srt_txt = os.path.basename(self.srt_path) if self.srt_path else "CHƯA CÓ SUB"
        srt_col = "#9AA0A6" if self.srt_path else "#FF7373"
        self.lbl_srt = QLabel("S1  " + srt_txt)
        self.lbl_srt.setToolTip(self.srt_path or "Chưa có phụ đề")
        self.lbl_srt.setStyleSheet(f"color:{srt_col}; font-size:9px; border:none;")
        lay.addWidget(self.lbl_srt)

        # Timeline mini + play + track
        bottom = QHBoxLayout(); bottom.setSpacing(5)
        self.cmb_track = QComboBox()
        self.cmb_track.addItems(["Gốc", "Lồng tiếng"])
        self.cmb_track.setFixedWidth(86)
        self.cmb_track.setStyleSheet(
            "QComboBox { background:#171A1F; color:#C9CDD3; border:1px solid #30343B; "
            "border-radius:4px; padding:3px 5px; font-size:9px; }")
        bottom.addWidget(self.cmb_track)
        self.btn_card_play = QPushButton("▶")
        self.btn_card_play.setFixedSize(27, 24)
        self.btn_card_play.setStyleSheet(
            "QPushButton { background:#20242A; color:#E8EAED; border:1px solid #343A42; "
            "border-radius:4px; padding:0; } QPushButton:hover { background:#2B3138; border-color:#39C7D8; }")
        self.btn_card_play.clicked.connect(lambda: self.play_requested.emit(self))
        bottom.addWidget(self.btn_card_play)
        # Nút phóng to preview ra cửa sổ lớn để canh chữ/logo cho dễ.
        self.btn_card_zoom = QPushButton("⛶")
        self.btn_card_zoom.setFixedSize(27, 24)
        self.btn_card_zoom.setToolTip("Phóng to khung xem trước")
        self.btn_card_zoom.setStyleSheet(
            "QPushButton { background:#20242A; color:#E8EAED; border:1px solid #343A42; "
            "border-radius:4px; padding:0; } QPushButton:hover { background:#2B3138; border-color:#39C7D8; }")
        self.btn_card_zoom.clicked.connect(lambda: self.zoom_requested.emit(self))
        bottom.addWidget(self.btn_card_zoom)
        self.play_slider = QSlider(Qt.Orientation.Horizontal)
        self.play_slider.setRange(0, 0)
        self.play_slider.setSingleStep(250)
        self.play_slider.setStyleSheet(
            "QSlider::groove:horizontal { background:#2A2F36; height:4px; border-radius:2px; } "
            "QSlider::sub-page:horizontal { background:#30BFD0; border-radius:2px; } "
            "QSlider::handle:horizontal { background:#E8EAED; width:10px; margin:-3px 0; border-radius:5px; }")
        self.play_slider.sliderMoved.connect(lambda v: self.seek_requested.emit(self, int(v)))
        bottom.addWidget(self.play_slider, 1)
        self.lbl_time_card = QLabel("00:00 / 00:00")
        self.lbl_time_card.setStyleSheet("color:#8D949E; font-size:9px; border:none;")
        bottom.addWidget(self.lbl_time_card)
        lay.addLayout(bottom)

        # Thanh mảnh riêng cho tiến độ render, không chiếm timeline phát video.
        self.mini_progress = QProgressBar()
        self.mini_progress.setRange(0, 100); self.mini_progress.setValue(0)
        self.mini_progress.setTextVisible(False); self.mini_progress.setFixedHeight(3)
        self.mini_progress.setStyleSheet(
            "QProgressBar { background:#24292F; border:none; border-radius:1px; } "
            "QProgressBar::chunk { background:#63D79A; border-radius:1px; }")
        lay.addWidget(self.mini_progress)

    def ensure_thumbnail(self):
        """Chỉ nạp thumbnail khi card sắp/đang xuất hiện trên màn hình."""
        src = self.video_path
        try:
            same = self._thumb_source and os.path.abspath(self._thumb_source) == os.path.abspath(src)
        except Exception:
            same = (self._thumb_source == src)
        if same and (self._thumb_loaded or self._thumb_loading):
            return
        self._load_thumbnail()

    def _load_thumbnail(self):
        """Thumbnail nền nhẹ: tối đa 1 ffmpeg và chỉ chạy cho card đang nhìn thấy."""
        src = self.video_path
        self._thumb_source = src
        self._thumb_loaded = False
        self._thumb_loading = True
        self._thumb_last_size = QSize()
        self._thumb_pix = QPixmap()
        self.lbl_thumb.setPixmap(QPixmap())
        self.lbl_thumb.setText("🎬\nĐang lấy khung hình...")

        # Nếu đã cache trong RAM thì hiển thị ngay. Cache trên đĩa sẽ được
        # _video_thumb_path bắt lại bằng tên hash ổn định mà không decode video.
        try:
            key = (os.path.abspath(src), int(os.path.getmtime(src)))
            cached = _VIDEO_THUMB_CACHE.get(key)
        except Exception:
            cached = None
        if cached and os.path.exists(cached):
            self._apply_thumbnail_path(src, cached)
            return

        fut = _VIDEO_THUMB_POOL.submit(_video_thumb_path, src)
        def _done(f, source=src):
            try:
                result = f.result() or ""
                self.thumb_ready.emit(source, result)
            except Exception:
                try:
                    self.thumb_ready.emit(source, "")
                except Exception:
                    pass
        fut.add_done_callback(_done)

    def _apply_thumbnail_path(self, source_video, path):
        # Card có thể đã đổi video trong lúc thumbnail cũ đang chạy.
        if os.path.abspath(source_video) != os.path.abspath(self.video_path):
            return
        self._thumb_loading = False
        self._thumb_loaded = True
        if path:
            pix = QPixmap(path)
            if not pix.isNull():
                self._thumb_pix = pix
                self._thumb_last_size = QSize()
                self.lbl_thumb.setText("")
                self._refresh_thumb_pixmap()
                return
        self._thumb_pix = QPixmap()
        self.lbl_thumb.setPixmap(QPixmap())
        self.lbl_thumb.setText("🎬  Không lấy được thumbnail")

    def _refresh_thumb_pixmap(self):
        if self._thumb_pix.isNull() or self._preview_attached:
            return
        size = self.lbl_thumb.size()
        if size.width() < 20 or size.height() < 20:
            size = QSize(360, 150)
        # Không scale lại nếu kích thước thực tế không đổi. Đây là điểm giảm lag
        # rất nhiều khi cuộn/resize grid có 50-200 card.
        if self._thumb_last_size == size:
            return
        self._thumb_last_size = QSize(size)
        pix = self._thumb_pix.scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.lbl_thumb.setPixmap(pix)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Debounce: Qt có thể phát hàng chục resizeEvent trong một lần relayout.
        if hasattr(self, "_thumb_resize_timer"):
            self._thumb_resize_timer.start()

    def _update_source_badges(self):
        has_vi = bool(self.srt_path and self.srt_path.lower().endswith("_vi.srt"))
        stem, ext = os.path.splitext(self.video_path)
        is_dub = stem.lower().endswith("_dubbed") or os.path.exists(stem + "_dubbed" + ext)
        self.badge_translate.setVisible(has_vi)
        self.badge_voice.setVisible(is_dub)
        self.cmb_track.setCurrentIndex(1 if is_dub else 0)

    def is_checked(self):
        return self.chk_select.isChecked()

    def set_play_progress(self, pct, seconds=None):
        # Hàm này vẫn dành cho % render để giữ tương thích backend cũ.
        self.mini_progress.setValue(max(0, min(100, int(pct))))

    def set_media_position(self, pos_ms, dur_ms):
        dur_ms = max(0, int(dur_ms or 0))
        pos_ms = max(0, min(int(pos_ms or 0), dur_ms if dur_ms else int(pos_ms or 0)))
        self.play_slider.blockSignals(True)
        self.play_slider.setRange(0, dur_ms)
        self.play_slider.setValue(pos_ms)
        self.play_slider.blockSignals(False)
        self.lbl_time_card.setText(
            f"{format_time(pos_ms/1000)} / {format_time(dur_ms/1000)}")

    def attach_preview_widget(self, widget):
        if widget is None:
            return
        self.lbl_thumb.hide()
        self.preview_lay.addWidget(widget)
        widget.show()
        self._preview_attached = True

    def detach_preview_widget(self, widget=None):
        if widget is not None and self.preview_lay.indexOf(widget) >= 0:
            self.preview_lay.removeWidget(widget)
            widget.hide()
        self._preview_attached = False
        self.lbl_thumb.show()
        self.ensure_thumbnail()
        self._refresh_thumb_pixmap()

    def _apply_style(self):
        if self.selected:
            self.setStyleSheet(
                "QFrame#EpisodeCard { background:#20252B; border:2px solid #35C3D4; border-radius:7px; }"
                "QFrame#EpisodeCard:hover { background:#232A31; }")
        else:
            self.setStyleSheet(
                "QFrame#EpisodeCard { background:#181B20; border:1px solid #30353C; border-radius:7px; }"
                "QFrame#EpisodeCard:hover { background:#1E2329; border:1px solid #59616C; }")

    def set_selected(self, val):
        self.selected = val
        self._apply_style()

    def refresh_srt_from_disk(self):
        """Tự dò lại sub + video lồng tiếng như logic cũ, sau đó cập nhật badge/card."""
        stem, ext = os.path.splitext(self.video_path)
        if stem.endswith("_dubbed"):
            orig_stem = stem[:-len("_dubbed")]
        else:
            orig_stem = stem
        dubbed_video = orig_stem + "_dubbed" + ext
        video_changed = False
        if os.path.exists(dubbed_video) and self.video_path != dubbed_video:
            self.video_path = dubbed_video
            video_changed = True
            self.lbl_name.setText(os.path.basename(self.video_path))
            self.lbl_name.setToolTip(self.video_path)
            self._thumb_loaded = False
            self._thumb_loading = False
            self._thumb_source = ""
            self.ensure_thumbnail()

        vi = orig_stem + "_vi.srt"
        raw = orig_stem + ".srt"
        found = vi if os.path.exists(vi) else (raw if os.path.exists(raw) else None)
        if not found:
            self._update_source_badges()
            return video_changed
        changed = (self.srt_path != found) or video_changed
        self.srt_path = found
        self.lbl_srt.setToolTip(found)
        if found.endswith("_vi.srt"):
            self.lbl_srt.setText("S1  " + os.path.basename(found))
            self.lbl_srt.setStyleSheet("color:#63D79A; font-size:9px; border:none;")
        else:
            self.lbl_srt.setText("S0  " + os.path.basename(found) + " (sub gốc)")
            self.lbl_srt.setStyleSheet("color:#F5C26B; font-size:9px; border:none;")
        self._update_source_badges()
        return changed

    def set_status(self, status):
        colors = {
            "chờ": ("#30343B", "#AEB4BE"),
            "đang render": ("#3A2B18", "#FFB15C"),
            "xong": ("#173629", "#63D79A"),
            "lỗi": ("#3C1E21", "#FF7373"),
            "đã dừng": ("#353238", "#C6A7E8"),
        }
        bg, fg = colors.get(status, ("#30343B", "#AEB4BE"))
        self.lbl_badge.setText(status)
        self.lbl_badge.setStyleSheet(
            f"background:{bg}; color:{fg}; font-size:9px; padding:2px 6px; "
            "border-radius:4px; border:none; font-weight:700;")
        if status == "xong":
            self.mini_progress.setValue(100)
        elif status in ("chờ", "lỗi", "đã dừng"):
            self.mini_progress.setValue(0)

    def mousePressEvent(self, e):
        self.clicked.emit(self)
        super().mousePressEvent(e)


class ProgressStep(QWidget):
    def __init__(self, name, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 2, 0, 2); lay.setSpacing(2)
        top = QHBoxLayout()
        self.dot = QLabel(); self.dot.setFixedSize(8, 8)
        self.dot.setStyleSheet("background:#4B5563; border-radius:4px;")
        self.lbl_name = QLabel(name); self.lbl_name.setStyleSheet("color:#8A8D98; font-size:11px;")
        self.lbl_status = QLabel("chờ"); self.lbl_status.setStyleSheet("color:#8A8D98; font-size:10px;")
        top.addWidget(self.dot); top.addWidget(self.lbl_name, stretch=1); top.addWidget(self.lbl_status)
        lay.addLayout(top)
        self.bar = QProgressBar(); self.bar.setFixedHeight(14); self.bar.setTextVisible(True)
        self.bar.setRange(0, 100); self.bar.setFormat("%p%")
        self.bar.setStyleSheet(
            "QProgressBar { background:#2D303D; border:none; border-radius:3px; "
            "color:#E5E7EB; font-size:9px; text-align:center; } "
            "QProgressBar::chunk { background:#10B981; border-radius:3px; }")
        lay.addWidget(self.bar)
        self._count_txt = ""   # ví dụ "Tập 3/10"

    def set_count(self, done, total):
        """Đặt bộ đếm tập, hiện kèm trạng thái (VD 'đang chạy · Tập 3/10')."""
        self._count_txt = f"Tập {done}/{total}" if total else ""
        cur = self.lbl_status.text().split(" · ")[0]
        self.lbl_status.setText(f"{cur} · {self._count_txt}" if self._count_txt else cur)

    def set_percent(self, pct):
        """Chỉ cập nhật % thanh (không đổi trạng thái/màu) — dùng khi đang render 1 tập."""
        self.bar.setValue(max(0, min(100, int(pct))))

    def set_status(self, status, progress=0):
        color = {"success": "#10B981", "processing": "#F37021"}.get(status, "#4B5563")
        self.dot.setStyleSheet(f"background:{color}; border-radius:4px;")
        base = {"success": "xong", "processing": "đang chạy"}.get(status, "chờ")
        self.lbl_status.setText(f"{base} · {self._count_txt}" if self._count_txt else base)
        self.bar.setValue(int(progress))

# ============================================================
#  TAB RENDER CHÍNH
# ============================================================
class RenderWidget(QWidget):
    # Cặp file ưu tiên: *_dubbed.mp4 + *_vi.srt; không có thì dùng gốc.
    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = QSettings("HongguoDownloader", "RenderTab")
        self.cards = []                 # danh sách EpisodeCard trong grid
        self._card_path_set = set()      # tra trùng O(1), tránh quét lại hàng trăm card
        self._bulk_loading = False       # True khi đang nạp thư mục lớn theo từng lô
        self._bulk_pairs = []
        self._bulk_index = 0
        self._bulk_batch_size = 8        # mỗi nhịp chỉ tạo vài card để UI luôn phản hồi
        self._bulk_source_label = ""
        self.selected_card = None
        self._thumb_src_path = None     # ảnh gốc cho thumbnail AI
        self._thumb_srt_path = None     # SRT tập phim (không bắt buộc)
        self._thumb_thread = None
        self.blur_boxes = []
        self.sample_sub = None
        self.logo_item = None
        self._design_locked = None  # giữ tương thích code cũ; render mới dùng config từng card
        self._loading_card_design = False
        self._render_queue = []         # hàng đợi render (các card)
        self._render_running = False
        self._stopping = False
        self.render_thread = None
        # ── Thanh TỔNG cho quy trình Tách→Dịch→Lồng→Render ──
        self._total_units = 0     # = số tập × số bước
        self._done_units = 0      # số "việc" đã xong (mỗi tập mỗi bước = 1)
        self._total_active = False  # True khi đang chạy "Làm tất cả" trọn quy trình
        # BOOM Studio V2: trạng thái thư viện / bộ lọc. Chỉ ảnh hưởng giao diện,
        # backend vẫn luôn giữ self.cards đầy đủ để render đúng.
        self._library_filter = "all"
        self._search_text = ""
        self._filter_buttons = {}
        self._sidebar_counts = {}

        # ── REALTIME COUNTERS ──────────────────────────────────────────
        # Theo dõi THƯ MỤC bằng QFileSystemWatcher (event-driven), không quét
        # 200-500 file liên tục bằng timer. Khi pipeline tạo .srt/_vi.srt/
        # _dubbed.mp4/_final.mp4, Qt báo directoryChanged -> debounce -> chỉ
        # cập nhật những card có trạng thái thay đổi.
        self._rt_state_cache = {}
        self._rt_watch_dirs = set()
        self._rt_refresh_timer = QTimer(self)
        self._rt_refresh_timer.setSingleShot(True)
        self._rt_refresh_timer.setInterval(140)
        self._rt_refresh_timer.timeout.connect(self._realtime_refresh_states)
        self._rt_fs_watcher = QFileSystemWatcher(self)
        self._rt_fs_watcher.directoryChanged.connect(self._on_realtime_dir_changed)

        # Theme Video Editor chuyên nghiệp, gần bố cục ảnh tham chiếu:
        # nền charcoal, card tối, nhấn cyan, panel phải gọn và thanh thao tác cố định.
        self.setStyleSheet("""
            QWidget { background:#121416; color:#E8EAED; font-family:'Segoe UI',Arial,sans-serif; }
            QScrollArea { border:none; background:transparent; }
            QScrollBar:vertical { background:#15181B; width:9px; margin:1px; }
            QScrollBar::handle:vertical { background:#3A4048; border-radius:4px; min-height:34px; }
            QScrollBar::handle:vertical:hover { background:#4B535D; }
            QPushButton { background:#252A30; color:#DDE1E6; border-radius:5px; padding:6px 9px;
                          font-weight:600; border:1px solid #363D45; }
            QPushButton:hover { background:#30363D; border:1px solid #3BC2D2; color:white; }
            QPushButton:pressed { background:#1D2227; }
            QPushButton:disabled { background:#1C2024; color:#666D75; border-color:#282D33; }
            QLineEdit, QSpinBox, QComboBox, QDoubleSpinBox {
                background:#171A1E; border:1px solid #343A42; padding:6px; color:#F1F3F4;
                border-radius:4px; font-weight:600; }
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QDoubleSpinBox:focus { border:1px solid #32BECE; }
            QComboBox QAbstractItemView { background:#20242A; border:1px solid #3E454E; selection-background-color:#2A5058; }
            QCheckBox { font-weight:600; padding:2px; color:#D7DBE0; }
            QCheckBox::indicator { width:16px; height:16px; border-radius:3px; border:1px solid #4A515A; background:#15181B; }
            QCheckBox::indicator:checked { background:#31BFD0; border:1px solid #31BFD0; }
            QSlider::groove:horizontal { height:4px; background:#30353B; border-radius:2px; }
            QSlider::sub-page:horizontal { background:#31BFD0; border-radius:2px; }
            QSlider::handle:horizontal { width:11px; margin:-4px 0; border-radius:5px; background:#E8EAED; }
            QToolTip { background:#262B31; color:#F1F3F4; border:1px solid #464E57; padding:4px; }
        """)

        # Helper: bọc widget dài vào vùng cuộn dọc.
        def _wrap_scroll(inner):
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            sc.setFrameShape(QFrame.Shape.NoFrame)
            sc.setStyleSheet("QScrollArea { border:none; background:transparent; }")
            sc.setWidget(inner)
            return sc
        self._wrap_scroll = _wrap_scroll

        root = QVBoxLayout(self)
        root.setContentsMargins(7, 7, 7, 7)
        root.setSpacing(6)

        # ── Header riêng của BOOM Studio Render V2 ───────────────────────
        topbar = QFrame()
        topbar.setObjectName("BoomV2TopBar")
        topbar.setFixedHeight(58)
        topbar.setStyleSheet(
            "QFrame#BoomV2TopBar { background:#0E1A2D; border:1px solid #17355E; border-radius:10px; }")
        top_lay = QHBoxLayout(topbar); top_lay.setContentsMargins(13, 7, 10, 7); top_lay.setSpacing(8)

        brand_box = QVBoxLayout(); brand_box.setSpacing(0)
        title = QLabel("▶  BOOM STUDIO")
        title.setStyleSheet("color:#F6F8FF; font-size:16px; font-weight:900; letter-spacing:.8px; border:none;")
        subtitle = QLabel("Tạo video dễ hơn bao giờ hết")
        subtitle.setStyleSheet("color:#88A4C8; font-size:8px; border:none;")
        brand_box.addWidget(title); brand_box.addWidget(subtitle)
        top_lay.addLayout(brand_box)
        top_lay.addSpacing(18)

        self.btn_nav_edit = QPushButton("▣  Tải & Chỉnh sửa")
        self.btn_nav_edit.setCheckable(True); self.btn_nav_edit.setChecked(True)
        self.btn_nav_dub = QPushButton("◉  Dịch & Lồng tiếng")
        self.btn_nav_render = QPushButton("▤  Render & Xuất")
        self.btn_nav_project = QPushButton("▣  Quản lý dự án")
        for _b in (self.btn_nav_edit, self.btn_nav_dub, self.btn_nav_render, self.btn_nav_project):
            _b.setFixedHeight(36)
            _b.setStyleSheet(
                "QPushButton { background:#132743; color:#AFC3DE; border:1px solid #1D3C65; "
                "border-radius:8px; padding:7px 14px; font-size:9px; font-weight:800; } "
                "QPushButton:hover { background:#17355A; color:white; border-color:#2F8CFF; } "
                "QPushButton:checked { background:#154A8B; color:#FFFFFF; border:1px solid #2B8DFF; }")
        self.btn_nav_dub.clicked.connect(self._open_pipeline_tab)
        self.btn_nav_render.clicked.connect(lambda: self.btn_run.setFocus() if hasattr(self, "btn_run") else None)
        self.btn_nav_project.clicked.connect(lambda: QMessageBox.information(self, "Quản lý dự án", "Phần quản lý dự án sẽ dùng Auto Save của BOOM Studio."))
        top_lay.addWidget(self.btn_nav_edit); top_lay.addWidget(self.btn_nav_dub)
        top_lay.addWidget(self.btn_nav_render); top_lay.addWidget(self.btn_nav_project)
        top_lay.addStretch()

        self.lbl_editor_count = QLabel("0 video")
        self.lbl_editor_count.setStyleSheet(
            "color:#86E7FF; background:#102E45; border:1px solid #1B607C; border-radius:11px; "
            "padding:3px 9px; font-size:9px; font-weight:800;")
        top_lay.addWidget(self.lbl_editor_count)
        self.btn_open_folder = QPushButton("📂  Mở thư mục")
        self.btn_open_folder.setFixedHeight(32)
        self.btn_open_folder.clicked.connect(self._open_render_folder)
        top_lay.addWidget(self.btn_open_folder)
        root.addWidget(topbar)

        # ── Khu làm việc chính: Grid lớn + Inspector phải ─────────────
        body = QHBoxLayout(); body.setSpacing(8)
        root.addLayout(body, 1)

        # SIDEBAR — khác hẳn tool tham chiếu: thư viện + bộ lọc nhanh riêng của BOOM.
        sidebar = QFrame(); sidebar.setObjectName("BoomLibrarySidebar")
        sidebar.setFixedWidth(190)
        sidebar.setStyleSheet(
            "QFrame#BoomLibrarySidebar { background:#101D31; border:1px solid #183758; border-radius:9px; }")
        sl = QVBoxLayout(sidebar); sl.setContentsMargins(9, 10, 9, 10); sl.setSpacing(6)

        btn_side_add = QPushButton("＋  Thêm video\n    Chọn từng file video")
        btn_side_add.setFixedHeight(52)
        btn_side_add.setStyleSheet(
            "QPushButton { text-align:left; background:#106B66; color:#F4FFFF; border:1px solid #168E86; "
            "border-radius:9px; padding:7px 10px; font-size:9px; font-weight:900; } "
            "QPushButton:hover { background:#138278; }")
        btn_side_add.clicked.connect(self._pick_files)
        sl.addWidget(btn_side_add)

        btn_side_folder = QPushButton("📁  Thêm thư mục\n    Quét toàn bộ video")
        btn_side_folder.setFixedHeight(52)
        btn_side_folder.setStyleSheet(
            "QPushButton { text-align:left; background:#176CE5; color:white; border:1px solid #2B8CFF; "
            "border-radius:9px; padding:7px 10px; font-size:9px; font-weight:900; } "
            "QPushButton:hover { background:#237CF2; }")
        btn_side_folder.clicked.connect(self._pick_folder)
        sl.addWidget(btn_side_folder)
        sl.addSpacing(8)

        sl.addWidget(QLabel("THƯ VIỆN", styleSheet="color:#6E91BC; font-size:8px; font-weight:900; border:none;"))

        def _make_sidebar_filter(key, label, icon="•"):
            btn = QPushButton(f"{icon}   {label}")
            btn.setCheckable(True)
            btn.setFixedHeight(30)
            btn.setStyleSheet(
                "QPushButton { text-align:left; background:transparent; color:#C7D4E7; border:none; "
                "border-radius:7px; padding:5px 8px; font-size:9px; font-weight:700; } "
                "QPushButton:hover { background:#172E4C; color:white; } "
                "QPushButton:checked { background:#1B4D86; color:white; border:1px solid #2A70BC; }")
            btn.clicked.connect(lambda _checked=False, k=key: self._set_library_filter(k))
            self._filter_buttons[key] = btn
            row = QHBoxLayout(); row.setSpacing(3); row.addWidget(btn, 1)
            count = QLabel("0")
            count.setFixedWidth(34); count.setAlignment(Qt.AlignmentFlag.AlignCenter)
            count.setStyleSheet("color:#CDE4FF; background:#213A60; border-radius:9px; padding:2px; font-size:8px; font-weight:800;")
            row.addWidget(count)
            self._sidebar_counts[key] = count
            sl.addLayout(row)
            return btn

        _make_sidebar_filter("all", "Tất cả", "▣").setChecked(True)
        _make_sidebar_filter("edited", "Đã chỉnh sửa", "✎")
        _make_sidebar_filter("exported", "Đã xuất", "⬇")
        _make_sidebar_filter("error", "Lỗi / Cần xử lý", "⚠")

        sl.addSpacing(7)
        sl.addWidget(QLabel("BỘ LỌC NHANH", styleSheet="color:#6E91BC; font-size:8px; font-weight:900; border:none;"))
        _make_sidebar_filter("nosub", "Chưa sub", "◫")
        _make_sidebar_filter("notrans", "Chưa dịch", "文")
        _make_sidebar_filter("nodub", "Chưa lồng tiếng", "◉")
        _make_sidebar_filter("done", "Đã hoàn tất", "✓")

        sl.addStretch()
        self.btn_clean_junk_side = QPushButton("🧹  Dọn file trung gian")
        self.btn_clean_junk_side.setFixedHeight(30)
        self.btn_clean_junk_side.setStyleSheet(
            "QPushButton { background:#312719; color:#F5C96A; border:1px solid #5B4724; border-radius:7px; font-size:8px; } "
            "QPushButton:hover { background:#42331C; }")
        self.btn_clean_junk_side.clicked.connect(self._clean_junk_files)
        sl.addWidget(self.btn_clean_junk_side)
        body.addWidget(sidebar)

        # WORKSPACE / GRID — vùng chính lớn, card nằm ngay tại nơi preview/chỉnh sửa.
        left = QFrame(); left.setMinimumWidth(640)
        left.setStyleSheet("QFrame { background:#171A1D; border-radius:6px; border:1px solid #2D3238; }")
        ll = QVBoxLayout(left); ll.setContentsMargins(7, 7, 7, 7); ll.setSpacing(6)

        # Tìm kiếm + chip lọc ngang giống app quản lý media chuyên nghiệp.
        search_row = QHBoxLayout(); search_row.setSpacing(5)
        self.txt_video_search = QLineEdit()
        self.txt_video_search.setPlaceholderText("⌕  Tìm kiếm video...")
        self.txt_video_search.setClearButtonEnabled(True)
        self.txt_video_search.setFixedHeight(31)
        self.txt_video_search.textChanged.connect(self._set_search_text)
        search_row.addWidget(self.txt_video_search, 1)
        for _key, _label in (("all", "Tất cả"), ("portrait", "Video dọc"), ("landscape", "Video ngang"),
                             ("nosub", "Chưa sub"), ("translated", "Đã dịch"), ("dubbed", "Đã lồng"),
                             ("exported", "Đã xuất"), ("error", "Lỗi")):
            _btn = QPushButton(_label)
            _btn.setCheckable(True); _btn.setFixedHeight(29)
            _btn.setStyleSheet(
                "QPushButton { background:#162238; color:#AFC0D7; border:1px solid #243A5B; border-radius:7px; "
                "padding:4px 8px; font-size:8px; font-weight:700; } "
                "QPushButton:hover { background:#1D3453; color:white; } "
                "QPushButton:checked { background:#1677E8; color:white; border-color:#2F91FF; }")
            _btn.clicked.connect(lambda _checked=False, k=_key: self._set_library_filter(k))
            self._filter_buttons[f"top_{_key}"] = _btn
            search_row.addWidget(_btn)
        ll.addLayout(search_row)

        head_q = QHBoxLayout(); head_q.setSpacing(6)
        lbl_queue = QLabel("Danh sách video")
        lbl_queue.setStyleSheet("font-size:12px; font-weight:800; color:#DDE1E6; border:none;")
        head_q.addWidget(lbl_queue)
        self.lbl_selected_count = QLabel("Đã chọn 0/0")
        self.lbl_selected_count.setStyleSheet("color:#8E969F; font-size:9px; border:none;")
        head_q.addWidget(self.lbl_selected_count)
        self.chk_select_all = QCheckBox("Chọn tất cả")
        self.chk_select_all.setChecked(True)
        self.chk_select_all.setStyleSheet("color:#9FB4CF; font-size:8px;")
        self.chk_select_all.toggled.connect(self._toggle_select_all_visible)
        head_q.addWidget(self.chk_select_all)
        head_q.addStretch()
        b_folder = QPushButton("📁 Thêm thư mục"); b_folder.clicked.connect(self._pick_folder)
        b_folder.setFixedHeight(28)
        b_files = QPushButton("＋ Thêm video"); b_files.clicked.connect(self._pick_files)
        b_files.setFixedHeight(28)
        b_clear = QPushButton("🗑"); b_clear.setFixedSize(34, 28); b_clear.clicked.connect(self._clear_all)
        b_clear.setStyleSheet("QPushButton { background:#2B2022; color:#F28A8A; border:1px solid #553238; border-radius:5px; } QPushButton:hover { background:#3A2528; }")
        head_q.addWidget(b_folder); head_q.addWidget(b_files); head_q.addWidget(b_clear)
        ll.addLayout(head_q)

        self.scroll_grid = QScrollArea(); self.scroll_grid.setWidgetResizable(True)
        self.scroll_grid.setStyleSheet("QScrollArea { background:#141719; border:none; border-radius:4px; }")
        self.grid_host = QWidget(); self.grid_host.setStyleSheet("background:#141719;")
        self.grid_lay = QGridLayout(self.grid_host)
        self.grid_lay.setContentsMargins(6, 6, 6, 6)
        self.grid_lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.grid_lay.setHorizontalSpacing(7); self.grid_lay.setVerticalSpacing(7)
        self.scroll_grid.setWidget(self.grid_host)
        ll.addWidget(self.scroll_grid, 1)

        # Thumbnail LAZY: chỉ xử lý những card trong/giáp vùng đang nhìn thấy.
        # Không còn mở 55 tiến trình decode nối đuôi ngay khi thêm thư mục.
        self._thumb_view_timer = QTimer(self)
        self._thumb_view_timer.setSingleShot(True)
        self._thumb_view_timer.setInterval(120)
        self._thumb_view_timer.timeout.connect(self._load_visible_thumbnails)
        self.scroll_grid.verticalScrollBar().valueChanged.connect(
            lambda _v: self._schedule_visible_thumbnails())
        self.scroll_grid.verticalScrollBar().rangeChanged.connect(
            lambda _a, _b: self._schedule_visible_thumbnails())

        # Giữ attribute cũ để code ngoài không lỗi; nút thật đã chuyển sang sidebar.
        self.btn_clean_junk = self.btn_clean_junk_side
        body.addWidget(left, 1)

        # PANEL THÔNG TIN FILE ĐANG CHỌN — KHÔNG có preview bên phải.
        fix = QFrame(); fix.setObjectName("SelectedInfoCard")
        fix.setStyleSheet("QFrame#SelectedInfoCard { background:#12233A; border-radius:8px; border:1px solid #24466E; }")
        fl = QVBoxLayout(fix); fl.setContentsMargins(9, 8, 9, 8); fl.setSpacing(5)
        info_head = QHBoxLayout()
        self.lbl_selected_title = QLabel("🔧  Chỉnh sửa: Chưa chọn video")
        self.lbl_selected_title.setStyleSheet("color:#F1F7FF; font-size:10px; font-weight:900; border:none;")
        info_head.addWidget(self.lbl_selected_title, 1)
        self.btn_reset_current = QPushButton("Đặt lại")
        self.btn_reset_current.setFixedSize(58, 24)
        self.btn_reset_current.clicked.connect(self._reset_current_design)
        info_head.addWidget(self.btn_reset_current)
        fl.addLayout(info_head)
        fl.addWidget(QLabel("THÔNG TIN TỆP", styleSheet="color:#6E91BC; font-size:8px; font-weight:900; border:none; margin-top:2px;"))

        self.lbl_fix_v = QLabel("—")
        self.lbl_fix_v.setStyleSheet("color:#DDEBFF; font-size:9px; font-weight:800; border:none;")
        fl.addWidget(self.lbl_fix_v)
        info_grid = QGridLayout(); info_grid.setHorizontalSpacing(8); info_grid.setVerticalSpacing(3)
        for _r, (_k, _txt) in enumerate((("res", "Độ phân giải"), ("dur", "Thời lượng"), ("size", "Dung lượng"), ("sub", "Phụ đề"))):
            _lab = QLabel(_txt); _lab.setStyleSheet("color:#7D96B7; font-size:8px; border:none;")
            _val = QLabel("—"); _val.setStyleSheet("color:#D8E4F5; font-size:8px; border:none;")
            info_grid.addWidget(_lab, _r, 0); info_grid.addWidget(_val, _r, 1)
            setattr(self, f"lbl_info_{_k}", _val)
        fl.addLayout(info_grid)

        # lbl_fix_s vẫn giữ để backend/đổi sub dùng như cũ.
        self.lbl_fix_s = QLabel("—"); self.lbl_fix_s.hide()
        action_row = QHBoxLayout(); action_row.setSpacing(5)
        bv = QPushButton("Đổi video"); bv.setFixedHeight(25); bv.clicked.connect(self._change_video)
        bs = QPushButton("Đổi sub"); bs.setFixedHeight(25); bs.clicked.connect(self._change_srt)
        action_row.addWidget(bv); action_row.addWidget(bs)
        fl.addLayout(action_row)

        self.step_render = ProgressStep("Tiến độ render")
        self.step_render.setVisible(False)
        self.txt_log = QTextEdit(); self.txt_log.setReadOnly(True); self.txt_log.document().setMaximumBlockCount(400)
        self.txt_log.setFixedHeight(72)
        self.txt_log.setStyleSheet(
            "QTextEdit { background:#0E1113; color:#8FE0B6; font-family:Consolas; font-size:8px; "
            "padding:4px; border:1px solid #2A3036; border-radius:4px; }")
        self.txt_log.hide()  # V2: log kỹ thuật ẩn khỏi giao diện chính để người mới không bị rối.

        # Shared preview engine: KHÔNG còn ô Xem trước bên phải.
        # Chỉ dùng 1 QMediaPlayer/QGraphicsScene để nhẹ máy; khi chọn card nào,
        # PreviewGraphicsView được chuyển thẳng vào card đó. Vì scene cũ vẫn dùng
        # nên sub/logo/vùng mờ vẫn kéo trực tiếp trên chính video đang xem.
        self.scene = QGraphicsScene(self)
        self.video_item = QGraphicsVideoItem(); self.scene.addItem(self.video_item)
        self.media_player = QMediaPlayer(); self.audio_output = QAudioOutput()
        self.media_player.setAudioOutput(self.audio_output); self.media_player.setVideoOutput(self.video_item)
        self.video_item.nativeSizeChanged.connect(self._on_native_size)
        self.preview = PreviewGraphicsView(self.scene)
        self.preview.setMinimumHeight(145)
        self.preview.setStyleSheet("background:#050607; border:none;")
        self.preview.hide()
        self._preview_card = None
        self.media_player.positionChanged.connect(self._on_pos)
        self.media_player.durationChanged.connect(self._on_dur)

        # INSPECTOR PHẢI
        right = QFrame(); right.setMinimumWidth(315); right.setMaximumWidth(390)
        right.setStyleSheet("QFrame { background:#1B1E22; border-radius:6px; border:1px solid #2D3238; }")
        rl = QVBoxLayout(right); rl.setContentsMargins(6, 6, 6, 6); rl.setSpacing(6)
        rl.addWidget(fix)

        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(
            "QTabWidget::pane { border:1px solid #243E62; border-radius:7px; background:#101D31; }"
            "QTabBar::tab { background:#142642; color:#91A8C8; padding:7px 5px; border:1px solid #243E62; "
            "border-bottom:none; font-weight:800; font-size:8px; min-width:55px; }"
            "QTabBar::tab:selected { background:#147DF0; color:white; border-color:#3093FF; }"
            "QTabBar::tab:hover:!selected { background:#1A3559; color:#EAF4FF; }")
        self.tabs.tabBar().setExpanding(True)
        self.tabs.setUsesScrollButtons(True)

        self.tab_sub = QWidget(); self.tab_logo = QWidget(); self.tab_blur = QWidget()
        self.tab_cover = QWidget(); self.tab_fx = QWidget(); self.tab_thumb = QWidget()
        self.tab_design = self.tab_sub  # tương thích code cũ nếu module ngoài tham chiếu.
        self.tab_layers = self.tab_sub
        self.tabs.addTab(self._wrap_scroll(self.tab_sub), "Phụ đề")
        self.tabs.addTab(self._wrap_scroll(self.tab_logo), "Logo")
        self.tabs.addTab(self._wrap_scroll(self.tab_blur), "Che chữ")
        self.tabs.addTab(self._wrap_scroll(self.tab_cover), "Cover")
        self.tabs.addTab(self._wrap_scroll(self.tab_fx), "Hiệu ứng")

        # --- PHỤ ĐỀ ---
        sub_lay = QVBoxLayout(self.tab_sub); sub_lay.setContentsMargins(9, 9, 9, 9); sub_lay.setSpacing(7)
        self.chk_hardsub = QCheckBox("Hiển thị phụ đề")
        self.chk_hardsub.setChecked(self.settings.value("hardsub_en", True, type=bool))
        self.chk_hardsub.setStyleSheet("color:#EAF4FF; font-size:9px; font-weight:900;")
        sub_lay.addWidget(self.chk_hardsub)
        hint_sub = QLabel("Kéo trực tiếp chữ mẫu trên video đang chọn để đặt vị trí.")
        hint_sub.setWordWrap(True); hint_sub.setStyleSheet("color:#6F8DB4; font-size:8px; border:none;")
        sub_lay.addWidget(hint_sub)

        fb = QHBoxLayout(); fb.addWidget(QLabel("Font chữ:"))
        self.cb_font = QComboBox(); self.cb_font.addItems(FONTS_LIST)
        self.cb_font.setCurrentText(self.settings.value("font_name", "Arial"))
        fb.addWidget(self.cb_font, 1); sub_lay.addLayout(fb)
        sz = QHBoxLayout(); sz.addWidget(QLabel("Cỡ chữ:")); self.spin_size = QSpinBox(); self.spin_size.setRange(10, 150)
        self.spin_size.setValue(int(self.settings.value("font_size", 24))); self.spin_size.setFixedWidth(72)
        sz.addWidget(self.spin_size); sz.addStretch(); sub_lay.addLayout(sz)
        cb = QHBoxLayout(); cb.addWidget(QLabel("Màu chữ:")); self.cb_color = QComboBox(); self.cb_color.addItems(list(COLOR_PRESETS.keys()))
        self.cb_color.setCurrentText(self.settings.value("font_color_name", "Trắng (White)")); cb.addWidget(self.cb_color, 1); sub_lay.addLayout(cb)

        self.chk_subbox = QCheckBox("Nền chữ")
        self.chk_subbox.setChecked(self.settings.value("subbox_en", False, type=bool)); sub_lay.addWidget(self.chk_subbox)
        boxrow = QHBoxLayout(); boxrow.addWidget(QLabel("Màu nền:")); self.cb_subbox_color = QComboBox()
        self.cb_subbox_color.addItems(["Đen", "Xám đậm", "Xanh đen", "Trắng"])
        self.cb_subbox_color.setCurrentText(self.settings.value("subbox_color_name", "Đen")); boxrow.addWidget(self.cb_subbox_color, 1); sub_lay.addLayout(boxrow)
        op = QHBoxLayout(); op.addWidget(QLabel("Độ mờ:")); self.spn_subbox_opacity = QSpinBox(); self.spn_subbox_opacity.setRange(0,100)
        self.spn_subbox_opacity.setValue(int(self.settings.value("subbox_opacity", 60))); self.spn_subbox_opacity.setSuffix(" %")
        op.addWidget(self.spn_subbox_opacity); op.addStretch(); sub_lay.addLayout(op)
        self.cb_font.currentTextChanged.connect(lambda *_: self._restyle_sample_sub())
        self.spin_size.valueChanged.connect(lambda *_: self._restyle_sample_sub())
        self.cb_color.currentTextChanged.connect(lambda *_: self._restyle_sample_sub())
        sub_lay.addStretch()

        # --- LOGO ---
        logo_lay = QVBoxLayout(self.tab_logo); logo_lay.setContentsMargins(9,9,9,9); logo_lay.setSpacing(7)
        self.chk_logo = QCheckBox("Bật Logo / Tiêu đề"); self.chk_logo.setChecked(self.settings.value("bp_logo_en", False, type=bool))
        logo_lay.addWidget(self.chk_logo)
        lgrow = QHBoxLayout(); self.logo_input = QLineEdit(self.settings.value("logo_path", "")); self.logo_input.setPlaceholderText("Chọn ảnh logo PNG...")
        bg2 = QPushButton("Chọn"); bg2.setFixedWidth(58); bg2.clicked.connect(self._select_logo)
        lgrow.addWidget(self.logo_input,1); lgrow.addWidget(bg2); logo_lay.addLayout(lgrow)
        logo_lay.addWidget(QLabel("Bật logo rồi kéo trực tiếp trên video để đặt vị trí và kích thước.", styleSheet="color:#6F8DB4; font-size:8px; border:none;"))
        self.chk_logo.stateChanged.connect(lambda: self._update_logo_preview())
        self.logo_input.textChanged.connect(lambda: self._update_logo_preview())
        self.chk_logo.stateChanged.connect(lambda: setattr(self, "_design_locked", None))
        self.logo_input.textChanged.connect(lambda: setattr(self, "_design_locked", None))
        logo_lay.addStretch()

        # --- CHE CHỮ / VÙNG MỜ ---
        blur_lay = QVBoxLayout(self.tab_blur); blur_lay.setContentsMargins(9,9,9,9); blur_lay.setSpacing(7)
        self.chk_blur = QCheckBox("Bật vùng che / làm mờ"); self.chk_blur.setChecked(self.settings.value("bp_blur_en", False, type=bool))
        blur_lay.addWidget(self.chk_blur)
        br = QHBoxLayout(); b_add = QPushButton("＋ Thêm vùng che"); b_add.clicked.connect(lambda: self._add_blur_box())
        b_clr = QPushButton("Xóa tất cả"); b_clr.clicked.connect(self._clear_blur_boxes)
        br.addWidget(b_add); br.addWidget(b_clr); blur_lay.addLayout(br)
        blur_lay.addWidget(QLabel("Vùng che xuất hiện ngay trên video đang chọn. Kéo/resize trực tiếp bằng chuột.", styleSheet="color:#6F8DB4; font-size:8px; border:none;"))
        blur_lay.addStretch()

        # --- COVER / OVERLAY ---
        cover_lay = QVBoxLayout(self.tab_cover); cover_lay.setContentsMargins(9,9,9,9); cover_lay.setSpacing(7)
        self.chk_intro = QCheckBox("Nhúng Ảnh Bìa (Cover)")
        self.chk_intro.setChecked(self.settings.value("intro_en", False, type=bool)); cover_lay.addWidget(self.chk_intro)
        ir = QHBoxLayout(); self.intro_input = QLineEdit(self.settings.value("intro_path", "")); self.intro_input.setPlaceholderText("File ảnh bìa...")
        btn_intro_pick = QPushButton("Chọn"); btn_intro_pick.setFixedWidth(58); btn_intro_pick.clicked.connect(self._select_intro)
        ir.addWidget(self.intro_input,1); ir.addWidget(btn_intro_pick); cover_lay.addLayout(ir)
        self.chk_intro.stateChanged.connect(lambda: self.settings.setValue("intro_en", self.chk_intro.isChecked()))
        self.intro_input.textChanged.connect(lambda: self.settings.setValue("intro_path", self.intro_input.text().strip()))
        # Overlay PNG chỉ dùng trong phiên hiện tại / từng card, KHÔNG lưu QSettings.
        # Đồng thời xóa giá trị cũ đã từng lưu để lần mở app sau không tự hiện lại.
        try:
            self.settings.remove("bp_frame_en")
            self.settings.remove("frame_path")
        except Exception:
            pass
        self.chk_frame = QCheckBox("Overlay PNG"); self.chk_frame.setChecked(False); cover_lay.addWidget(self.chk_frame)
        fr = QHBoxLayout(); self.frame_input = QLineEdit(""); self.frame_input.setPlaceholderText("Ảnh PNG overlay...")
        bf = QPushButton("Chọn"); bf.setFixedWidth(58); bf.clicked.connect(self._select_frame)
        fr.addWidget(self.frame_input,1); fr.addWidget(bf); cover_lay.addLayout(fr); cover_lay.addStretch()

        # --- HIỆU ỨNG / CHẤT LƯỢNG ---
        fx_lay = QVBoxLayout(self.tab_fx); fx_lay.setContentsMargins(9,9,9,9); fx_lay.setSpacing(7)
        ql = QHBoxLayout(); ql.addWidget(QLabel("Chất lượng:")); self.cb_quality = QComboBox()
        self.cb_quality.addItems(["🏆 Cao nhất (CRF 16 - Gần lossless)", "⭐ Tốt (CRF 20 - Đề xuất)", "👍 Vừa (CRF 26 - Cân bằng)", "⚡ Nhanh (1 Mbps - File nhỏ)"])
        self.cb_quality.setCurrentText(self.settings.value("render_quality", "⭐ Tốt (CRF 20 - Đề xuất)")); ql.addWidget(self.cb_quality,1); fx_lay.addLayout(ql)
        fx_lay.addWidget(QLabel("Hiệu ứng nhanh", styleSheet="color:#6F8DB4; font-size:8px; font-weight:800; border:none;"))
        self.chk_flip = QCheckBox("Lật ngang"); self.chk_zoom = QCheckBox("Phóng to 4%")
        self.chk_color = QCheckBox("Kích màu sáng"); self.chk_noise = QCheckBox("Nhiễu hạt")
        self.chk_speed = QCheckBox("Tốc độ 1.05x"); self.chk_pitch = QCheckBox("Đổi Tone")
        self.chk_rotate = QCheckBox("Xoay 1°")
        for k, chk in (("bp_flip", self.chk_flip), ("bp_zoom", self.chk_zoom), ("bp_color", self.chk_color),
                       ("bp_noise", self.chk_noise), ("bp_speed", self.chk_speed), ("bp_pitch", self.chk_pitch),
                       ("bp_rotate", self.chk_rotate)):
            chk.setChecked(self.settings.value(k, False, type=bool))
        gb = QGridLayout(); gb.addWidget(self.chk_flip,0,0); gb.addWidget(self.chk_zoom,0,1)
        gb.addWidget(self.chk_color,1,0); gb.addWidget(self.chk_noise,1,1)
        gb.addWidget(self.chk_speed,2,0); gb.addWidget(self.chk_pitch,2,1); gb.addWidget(self.chk_rotate,3,0)
        fx_lay.addLayout(gb); fx_lay.addStretch()

        # Thumbnail AI vẫn giữ nguyên tính năng cũ, nhưng để ở tab cuối cùng.
        self.tabs.addTab(self._wrap_scroll(self.tab_thumb), "Thumbnail")
        self._build_thumbnail_ui(self.tab_thumb)

        # Tab Sub→Dịch→Lồng của module cũ vẫn được cắm vào cùng inspector.
        try:
            from render_dub_feature import attach_dub_tab
            attach_dub_tab(self)
        except Exception as _dub_err:
            print(f"[WARN] Không nạp được tab Sub→Dịch→Lồng: {_dub_err}")

        rl.addWidget(self.tabs, 1)
        rl.addWidget(self.txt_log)
        body.addWidget(right)

        # ── Inspector bottom: cấu hình xuất + tiến trình ───────────────
        bot_lay = QVBoxLayout(); bot_lay.setContentsMargins(0, 0, 0, 0); bot_lay.setSpacing(5)
        apply_row = QHBoxLayout(); apply_row.setSpacing(5)
        self.cmb_apply_scope = QComboBox()
        self.cmb_apply_scope.addItems(["Các video đã chọn", "Từ video này trở đi", "Tất cả video"])
        self.cmb_apply_scope.setToolTip("Chỉ khi bấm Áp dụng, thiết lập của video hiện tại mới được sao chép sang video khác.")
        apply_row.addWidget(self.cmb_apply_scope, 1)
        self.btn_sync_design = QPushButton("Áp dụng")
        self.btn_sync_design.setFixedHeight(29)
        self.btn_sync_design.setStyleSheet(
            "QPushButton { background:#155C8D; color:#E9F7FF; border:1px solid #2A86BC; "
            "border-radius:6px; padding:5px 10px; font-size:9px; font-weight:900; } "
            "QPushButton:hover { background:#1A72AA; }")
        self.btn_sync_design.clicked.connect(self._apply_design_scope)
        apply_row.addWidget(self.btn_sync_design)
        bot_lay.addLayout(apply_row)

        export_cfg = QFrame(); export_cfg.setStyleSheet(
            "QFrame { background:#171A1E; border:1px solid #2F353C; border-radius:5px; }")
        ec = QVBoxLayout(export_cfg); ec.setContentsMargins(7, 5, 7, 5); ec.setSpacing(4)
        merge_row = QHBoxLayout()
        self.chk_merge_all = QCheckBox("Gộp trọn bộ sau Xuất")
        self.chk_merge_all.setChecked(self.settings.value("merge_after_render", False, type=bool))
        self.chk_merge_all.setStyleSheet("color:#70D6A2; font-weight:700; font-size:9px;")
        self.chk_merge_all.stateChanged.connect(lambda: self.settings.setValue("merge_after_render", self.chk_merge_all.isChecked()))
        merge_row.addWidget(self.chk_merge_all); merge_row.addStretch()
        ec.addLayout(merge_row)

        rp_row = QHBoxLayout(); rp_row.setSpacing(5)
        rp_row.addWidget(QLabel("Song song", styleSheet="color:#858D96; font-size:8px; border:none;"))
        self.spn_render_parallel = QSpinBox(); self.spn_render_parallel.setRange(1, 4)
        self.spn_render_parallel.setValue(int(self.settings.value("render_parallel", 2)))
        self.spn_render_parallel.setFixedWidth(43)
        self.spn_render_parallel.valueChanged.connect(lambda v: self.settings.setValue("render_parallel", v))
        rp_row.addWidget(self.spn_render_parallel)
        rp_row.addSpacing(7)
        rp_row.addWidget(QLabel("FPS", styleSheet="color:#858D96; font-size:8px; border:none;"))
        self.cmb_fps = QComboBox(); self.cmb_fps.addItems(["Giữ gốc", "24", "25", "30"])
        _saved_fps = self.settings.value("render_target_fps", "25")
        _idx = self.cmb_fps.findText(str(_saved_fps)); self.cmb_fps.setCurrentIndex(_idx if _idx >= 0 else 2)
        self.cmb_fps.setFixedWidth(78)
        self.cmb_fps.currentTextChanged.connect(lambda t: self.settings.setValue("render_target_fps", t))
        rp_row.addWidget(self.cmb_fps); rp_row.addStretch()
        ec.addLayout(rp_row)
        bot_lay.addWidget(export_cfg)

        self.lbl_big_prog = QLabel("Tiến độ xuất")
        self.lbl_big_prog.setStyleSheet("color:#AAB0B7; font-size:8px; font-weight:700; border:none;")
        bot_lay.addWidget(self.lbl_big_prog)
        self.big_render_prog = QProgressBar()
        self.big_render_prog.setRange(0, 100); self.big_render_prog.setValue(0)
        self.big_render_prog.setFixedHeight(16); self.big_render_prog.setTextVisible(True); self.big_render_prog.setFormat("%p%")
        self.big_render_prog.setStyleSheet(
            "QProgressBar { background:#262B31; border:1px solid #343A42; border-radius:4px; "
            "color:#DDE1E6; font-size:8px; font-weight:700; text-align:center; } "
            "QProgressBar::chunk { background:#31BFD0; border-radius:3px; }")
        bot_lay.addWidget(self.big_render_prog)

        self.btn_merge_now = QPushButton("🔗  Gộp nhanh các video đã chọn")
        self.btn_merge_now.setFixedHeight(28)
        self.btn_merge_now.setStyleSheet(
            "QPushButton { background:#183127; color:#74D6A4; border:1px solid #2D5D48; border-radius:5px; font-size:9px; } "
            "QPushButton:hover { background:#214335; }")
        self.btn_merge_now.clicked.connect(self._start_merge_now)
        bot_lay.addWidget(self.btn_merge_now)
        rl.addLayout(bot_lay)

        # ── Pipeline dưới cùng: nhìn là hiểu luồng, người mới chỉ cần 1 nút.
        bottombar = QFrame(); bottombar.setObjectName("BoomPipelineBar"); bottombar.setFixedHeight(66)
        bottombar.setStyleSheet(
            "QFrame#BoomPipelineBar { background:#0F1D31; border:1px solid #1D3A5E; border-radius:9px; }")
        bb = QHBoxLayout(bottombar); bb.setContentsMargins(8, 7, 8, 7); bb.setSpacing(6)

        def _pipe_button(text, subtext):
            b = QPushButton(f"{text}\n{subtext}")
            b.setFixedHeight(48)
            b.setStyleSheet(
                "QPushButton { text-align:left; background:#142A48; color:#ECF5FF; border:1px solid #25517F; "
                "border-radius:9px; padding:5px 11px; font-size:8px; font-weight:900; } "
                "QPushButton:hover { background:#193B64; border-color:#2D8DFF; }")
            return b

        btn_extract = _pipe_button("①  Tách phụ đề", "Tự động nhận diện giọng nói")
        btn_extract.clicked.connect(self._open_pipeline_extract); bb.addWidget(btn_extract, 1)
        btn_translate = _pipe_button("②  Dịch phụ đề", "Dịch đa ngôn ngữ")
        btn_translate.clicked.connect(self._open_pipeline_translate); bb.addWidget(btn_translate, 1)
        btn_dub = _pipe_button("③  Lồng tiếng", "Giọng nói tự nhiên")
        btn_dub.clicked.connect(self._open_pipeline_dub); bb.addWidget(btn_dub, 1)

        self.btn_run = _pipe_button("④  Render & Xuất", "Tạo video hoàn chỉnh")
        self.btn_run.clicked.connect(self._start_render_all); bb.addWidget(self.btn_run, 1)

        quick = QFrame(); quick.setStyleSheet("QFrame { background:#12243D; border:1px solid #23486F; border-radius:9px; }")
        ql2 = QVBoxLayout(quick); ql2.setContentsMargins(8,4,8,4); ql2.setSpacing(2)
        ql2.addWidget(QLabel("⚙  Cài đặt nhanh", styleSheet="color:#DDEBFF; font-size:8px; font-weight:900; border:none;"))
        qr = QHBoxLayout(); qr.addWidget(QLabel("Layout", styleSheet="color:#7897BC; font-size:7px; border:none;"))
        self.cmb_layout = QComboBox(); self.cmb_layout.addItems(["2", "3", "4"])
        self.cmb_layout.setCurrentText(str(self.settings.value("editor_grid_columns", "3")))
        if self.cmb_layout.currentText() not in ("2", "3", "4"): self.cmb_layout.setCurrentText("3")
        self.cmb_layout.setFixedWidth(45); self.cmb_layout.currentTextChanged.connect(self._on_layout_columns_changed)
        qr.addWidget(self.cmb_layout); qr.addWidget(QLabel("FPS", styleSheet="color:#7897BC; font-size:7px; border:none;"))
        # cmb_fps đã tạo ở panel phải; chỉ hiển thị nhãn ở đây để tránh widget bị re-parent.
        self.lbl_bottom_count = QLabel("0 tập"); self.lbl_bottom_count.setStyleSheet("color:#8AA4C7; font-size:8px; border:none;")
        qr.addWidget(self.lbl_bottom_count); ql2.addLayout(qr)
        bb.addWidget(quick)

        self.btn_stop = QPushButton("■")
        self.btn_stop.setFixedSize(38, 48); self.btn_stop.setToolTip("Dừng xử lý")
        self.btn_stop.setStyleSheet("QPushButton { background:#3A2028; color:#FF9BAA; border:1px solid #6D3442; border-radius:9px; font-size:11px; }")
        self.btn_stop.clicked.connect(self._stop_render); self.btn_stop.setEnabled(False); bb.addWidget(self.btn_stop)

        self.btn_full_pipeline = QPushButton("🚀  LÀM TẤT CẢ\nXử lý toàn bộ video · Chỉ 1 lần bấm")
        self.btn_full_pipeline.setMinimumWidth(205); self.btn_full_pipeline.setFixedHeight(48)
        self.btn_full_pipeline.setStyleSheet(
            "QPushButton { color:white; border:1px solid #7A8CFF; border-radius:10px; font-size:9px; font-weight:900; "
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #168BFF, stop:.48 #7658FF, stop:1 #FF5E9A); } "
            "QPushButton:hover { border:2px solid #D3DBFF; }")
        self.btn_full_pipeline.clicked.connect(self._run_full_pipeline_external)
        bb.addWidget(self.btn_full_pipeline)
        root.addWidget(bottombar)

        # Mẫu mặc định dành cho video chưa từng chỉnh.
        self._default_design_template = copy.deepcopy(self._collect_design(persist=False))

    # ============ THUMBNAIL AI ============
    def _build_thumbnail_ui(self, parent_widget):
        v = QVBoxLayout(parent_widget)
        v.setContentsMargins(5, 5, 5, 5)
        v.addWidget(QLabel("Tùy chỉnh Prompt & Ảnh gốc", styleSheet="font-size:12px; font-weight:bold; color:#F59E0B; border:none; margin-bottom:5px;"))

        r1 = QHBoxLayout()
        self.lbl_thumb_src = QLabel("Chưa chọn ảnh gốc")
        self.lbl_thumb_src.setStyleSheet("color:#8A8D98; font-size:10px; border:none;")
        btn_pick = QPushButton("Chọn ảnh gốc"); btn_pick.setFixedWidth(90)
        btn_pick.clicked.connect(self._pick_thumb_source)
        r1.addWidget(self.lbl_thumb_src, stretch=1); r1.addWidget(btn_pick)
        v.addLayout(r1)

        r_srt = QHBoxLayout()
        self.lbl_thumb_srt = QLabel("Chưa chọn SRT (không bắt buộc)")
        self.lbl_thumb_srt.setStyleSheet("color:#8A8D98; font-size:10px; border:none;")
        btn_pick_srt = QPushButton("Chọn SRT"); btn_pick_srt.setFixedWidth(90)
        btn_pick_srt.clicked.connect(self._pick_thumb_srt)
        r_srt.addWidget(self.lbl_thumb_srt, stretch=1); r_srt.addWidget(btn_pick_srt)
        v.addLayout(r_srt)

        v.addWidget(QLabel("Prompt (sửa nếu thích):", styleSheet="color:#8A8D98; font-size:10px; border:none;"))
        self.txt_thumb_prompt = QPlainTextEdit()
        self.txt_thumb_prompt.setPlainText(_DEFAULT_THUMB_PROMPT)
        self.txt_thumb_prompt.setFixedHeight(120)
        # Ngắt dòng theo bề rộng + tắt thanh cuộn ngang -> không bị tràn/thụt ngang
        self.txt_thumb_prompt.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.txt_thumb_prompt.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.txt_thumb_prompt.setStyleSheet(
            "QPlainTextEdit { background:#1B1D25; color:#E5E6E8; border:1px solid #3B3E4D; "
            "border-radius:6px; font-size:11px; padding:4px; }")
        v.addWidget(self.txt_thumb_prompt)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Số ảnh:", styleSheet="color:#8A8D98; border:none; font-size:11px;"))
        self.spin_thumb_n = QSpinBox(); self.spin_thumb_n.setRange(1, 8)
        self.spin_thumb_n.setValue(4); self.spin_thumb_n.setFixedWidth(50)
        r2.addWidget(self.spin_thumb_n)

        r2.addWidget(QLabel("AI:", styleSheet="color:#8A8D98; border:none; font-size:11px;"))
        self.cmb_thumb_provider = QComboBox()
        self.cmb_thumb_provider.addItems(["ChatGPT", "Gemini"])
        self.cmb_thumb_provider.setStyleSheet(
            "QComboBox { background:#1B1D25; color:#ddd; border:1px solid #3B3E4D; border-radius:6px; padding:4px 8px; font-size:11px; }")
        self.cmb_thumb_provider.currentTextChanged.connect(self._on_provider_changed)
        r2.addWidget(self.cmb_thumb_provider, 1)   # co giãn theo cột, không cứng 100px
        # ChatGPT là mặc định -> khoá ô số ảnh ngay từ đầu
        self.spin_thumb_n.setEnabled(False)
        self.spin_thumb_n.setValue(1)
        v.addLayout(r2)

        # Hàng riêng cho 2 nút -> không dồn chung 1 hàng gây tràn ngang
        r2b = QHBoxLayout()
        self.btn_thumb_login = QPushButton("🔐 Đăng nhập ChatGPT")
        self.btn_thumb_login.setStyleSheet(
            "QPushButton { background:#22242E; color:#A78BFA; border:1px solid #3B3E4D; border-radius:6px; padding:6px 10px; font-size:11px; font-weight:bold; }"
            "QPushButton:hover { border-color:#7452FF; color:white; }")
        self.btn_thumb_login.clicked.connect(self._login_chatgpt)
        r2b.addWidget(self.btn_thumb_login, 1)

        self.btn_thumb_run = QPushButton("✨ Tạo Thumbnail")
        self.btn_thumb_run.setStyleSheet(
            "QPushButton { background:#7452FF; color:white; border-radius:6px; padding:7px 14px; font-weight:bold; border:none; }"
            "QPushButton:hover { background:#6035E0; }")
        self.btn_thumb_run.clicked.connect(self._start_thumbnail)
        r2b.addWidget(self.btn_thumb_run, 1)
        v.addLayout(r2b)

        # Checkbox chọn khổ ảnh (ngang / dọc)
        r_orient = QHBoxLayout()
        self.chk_portrait = QCheckBox("📱 Khổ dọc 9:16 (Facebook / Reels / Stories)")
        self.chk_portrait.setStyleSheet(
            "QCheckBox { color:#A78BFA; font-size:11px; font-weight:bold; }"
            "QCheckBox::indicator { width:14px; height:14px; }")
        self.chk_portrait.setToolTip(
            "Bỏ check = ảnh ngang 16:9 (YouTube)\n"
            "Check = ảnh dọc 9:16 (Facebook / Reels / Stories)")
        r_orient.addWidget(self.chk_portrait)
        r_orient.addStretch()
        v.addLayout(r_orient)

        self.thumb_result_row = QHBoxLayout()
        self.thumb_result_row.addStretch()
        
        scroll_res = QScrollArea()
        scroll_res.setFixedHeight(120)
        scroll_res.setWidgetResizable(True)
        scroll_res.setStyleSheet("border:none; background:transparent;")
        res_container = QWidget()
        res_container.setLayout(self.thumb_result_row)
        scroll_res.setWidget(res_container)
        
        v.addWidget(QLabel("Kết quả (bấm xem hoặc Set Ảnh Bìa):", styleSheet="color:#8A8D98; font-size:10px; border:none; margin-top:5px;"))
        v.addWidget(scroll_res)
        v.addStretch()

    def _pick_thumb_source(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Chọn ảnh gốc làm mẫu", "",
            "Ảnh (*.png *.jpg *.jpeg *.webp *.bmp);;Tất cả (*)")
        if f:
            self._thumb_src_path = f
            self.lbl_thumb_src.setText(os.path.basename(f))
            self.lbl_thumb_src.setStyleSheet("color:#10B981; font-size:10px; border:none;")

    def _pick_thumb_srt(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Chọn file SRT của tập phim", "",
            "Phụ đề (*.srt *.txt);;Tất cả (*)")
        if f:
            self._thumb_srt_path = f
            self.lbl_thumb_srt.setText(os.path.basename(f))
            self.lbl_thumb_srt.setStyleSheet("color:#10B981; font-size:10px; border:none;")

    def _start_thumbnail(self):
        if getattr(self, "_thumb_thread", None) and self._thumb_thread.isRunning():
            QMessageBox.information(self, "Đang chạy", "Đang tạo thumbnail, vui lòng đợi.")
            return

        provider = self.cmb_thumb_provider.currentText()
        src = getattr(self, "_thumb_src_path", None)
        srt_path = getattr(self, "_thumb_srt_path", None)
        has_img = bool(src and os.path.exists(src))
        has_srt = bool(srt_path and os.path.exists(srt_path))

        # Gemini vẫn cần ảnh gốc (luồng cũ). ChatGPT cho phép chỉ SRT.
        if provider == "Gemini" and not has_img:
            QMessageBox.information(self, "Chưa chọn ảnh", "Gemini cần ảnh gốc. Hãy chọn 1 ảnh gốc.")
            return
        if provider == "ChatGPT" and not has_img and not has_srt:
            QMessageBox.information(self, "Thiếu dữ liệu", "Hãy chọn ảnh gốc HOẶC file SRT.")
            return

        # Đọc SRT (nếu có)
        srt_text = ""
        if has_srt:
            try:
                with open(srt_path, "r", encoding="utf-8", errors="replace") as f:
                    srt_text = f.read()
            except Exception as e:
                self._log(f"⚠️ Không đọc được SRT: {e}\n")

        prompt = self.txt_thumb_prompt.toPlainText().strip() or _DEFAULT_THUMB_PROMPT
        n = self.spin_thumb_n.value()
        # out_dir: cạnh ảnh gốc, nếu không có ảnh thì cạnh SRT
        anchor = src if has_img else srt_path
        out_dir = os.path.join(os.path.dirname(anchor), "AI_Thumbnails")

        if provider == "ChatGPT":
            if not os.path.exists(CHATGPT_AUTH_FILE):
                QMessageBox.warning(self, "Chưa đăng nhập ChatGPT",
                                    "Hãy bấm '🔐 Đăng nhập ChatGPT' để login 1 lần trước.")
                return
        else:
            if not os.path.exists(AUTH_FILE):
                QMessageBox.warning(self, "Chưa đăng nhập Gemini",
                                    "Hãy bấm 'Đồng bộ Gemini' (ở tab dịch) để đăng nhập 1 lần trước.")
                return

        self._clear_thumb_results()
        self.btn_thumb_run.setEnabled(False)
        self.btn_thumb_run.setText("⏳ Đang tạo...")
        _src_name = os.path.basename(src) if has_img else "(không ảnh)"
        _srt_name = f" + SRT {os.path.basename(srt_path)}" if has_srt else ""
        self._log(f"✨ Bắt đầu tạo {n} thumbnail bằng {provider} từ: {_src_name}{_srt_name}\n")

        if provider == "ChatGPT":
            # ChatGPT: chỉ tạo 1 ảnh duy nhất theo SRT (không nhiều biến thể).
            orientation = "portrait" if getattr(self, "chk_portrait", None) and self.chk_portrait.isChecked() else "landscape"
            self._thumb_thread = ChatGPTThumbnailThread(
                src_image=(src if has_img else ""), base_prompt=prompt, out_dir=out_dir,
                n_variants=1, show_browser=True, srt_text=srt_text,
                orientation=orientation)
        else:
            self._thumb_thread = GeminiThumbnailThread(
                src_image=src, base_prompt=prompt, out_dir=out_dir, n_variants=n,
                show_browser=True)
        self._thumb_thread.log.connect(self._log)
        self._thumb_thread.one_done.connect(self._add_thumb_result)
        self._thumb_thread.all_done.connect(self._on_thumb_all_done)
        self._thumb_thread.start()

    def _on_provider_changed(self, name):
        # ChatGPT chỉ tạo 1 ảnh -> khoá ô số ảnh cho khỏi hiểu nhầm
        is_cgpt = (name == "ChatGPT")
        self.spin_thumb_n.setEnabled(not is_cgpt)
        if is_cgpt:
            self.spin_thumb_n.setValue(1)

    def _login_chatgpt(self):
        # Nếu cửa sổ đăng nhập đang mở -> nút này là "xác nhận đã đăng nhập xong"
        t = getattr(self, "_thumb_login_thread", None)
        if t and t.isRunning():
            self._log("✅ Đang lưu phiên đăng nhập...\n")
            self.btn_thumb_login.setEnabled(False)
            self.btn_thumb_login.setText("⏳ Đang lưu...")
            t.confirm()
            return

        self._log("🔐 Mở cửa sổ đăng nhập ChatGPT...\n")
        self._thumb_login_thread = ChatGPTLoginThread()
        self._thumb_login_thread.log.connect(self._log)
        self._thumb_login_thread.done.connect(self._on_chatgpt_login_done)
        self._thumb_login_thread.start()
        # Đổi nút thành nút xác nhận thủ công
        self.btn_thumb_login.setText("✅ Tôi đã đăng nhập xong")

    def _on_chatgpt_login_done(self, ok):
        self.btn_thumb_login.setEnabled(True)
        if ok:
            self.btn_thumb_login.setText("✅ ChatGPT đã đăng nhập")
        else:
            self.btn_thumb_login.setText("🔐 Đăng nhập ChatGPT")

    def _clear_thumb_results(self):
        while self.thumb_result_row.count():
            it = self.thumb_result_row.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self.thumb_result_row.addStretch()

    def _add_thumb_result(self, path):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        
        btn = QPushButton()
        pix = QPixmap(path)
        if not pix.isNull():
            btn.setIcon(QIcon(pix)); btn.setIconSize(QSize(96, 54))
        btn.setFixedSize(104, 62); btn.setToolTip(path)
        btn.setStyleSheet("QPushButton { border:1px solid #3B3E4D; border-radius:6px; background:#1B1D25; }"
                          "QPushButton:hover { border:1px solid #7452FF; }")
        btn.clicked.connect(lambda _=False, p=path: self._open_thumb_path(p))
        
        btn_intro = QPushButton("⭐ Làm Ảnh Bìa")
        btn_intro.setStyleSheet("QPushButton { background:#31265C; color:#A78BFA; font-size:10px; font-weight:bold; padding:4px; border-radius:4px; border:none; } QPushButton:hover { background:#7452FF; color:white; }")
        btn_intro.clicked.connect(lambda _=False, p=path: self._set_intro(p))
        
        lay.addWidget(btn)
        lay.addWidget(btn_intro)
        
        self.thumb_result_row.insertWidget(self.thumb_result_row.count() - 1, w)

    def _open_thumb_path(self, path):
        try:
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            self._log(f"⚠️ Không mở được ảnh: {e}\n")

    def _on_thumb_all_done(self, saved):
        self.btn_thumb_run.setEnabled(True)
        self.btn_thumb_run.setText("✨ Tạo Thumbnail")
        if saved:
            folder = os.path.dirname(saved[0])
            QMessageBox.information(self, "Xong",
                f"Đã tạo {len(saved)} thumbnail.\nLưu tại:\n{folder}")
        else:
            QMessageBox.warning(self, "Không có ảnh",
                "Không tạo được thumbnail nào. Kiểm tra log — có thể Gemini không trả ảnh, "
                "chưa đăng nhập, hoặc giao diện Gemini đã đổi (cần chỉnh selector).")

    def _select_intro(self):
        fp, _ = QFileDialog.getOpenFileName(self, "Chọn ảnh Bìa", "", "Ảnh (*.png *.jpg *.jpeg *.webp)")
        if fp:
            self.intro_input.setText(fp)
            self.chk_intro.setChecked(True)

    def _set_intro(self, path):
        self.intro_input.setText(path)
        self.chk_intro.setChecked(True)
        self._log(f"📌 Đã đặt ảnh này làm Ảnh Bìa cho Video trọn bộ.\n")
        QMessageBox.information(self, "Thành công", "Đã chọn ảnh này làm Ảnh Bìa (Cover) khi gộp trọn bộ!")

        # ============ LOG ============
    def _log(self, msg):
        self.txt_log.append(str(msg).strip())

    # ============ MỞ THƯ MỤC ĐANG RENDER ============
    def _open_render_folder(self):
        """Mở thư mục chứa file đang/đã render. Ưu tiên thư mục render gần
        nhất; nếu chưa render thì lấy thư mục của file đầu trong hàng đợi."""
        folder = getattr(self, "_last_render_dir", None)
        if not folder and self.cards:
            folder = os.path.dirname(self.cards[0].video_path)
        if not folder or not os.path.isdir(folder):
            QMessageBox.information(self, "Chưa có thư mục",
                "Chưa có file nào để mở. Hãy thêm video hoặc bắt đầu render trước.")
            return
        try:
            if os.name == "nt":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            QMessageBox.warning(self, "Lỗi", f"Không mở được thư mục:\n{e}")

    # ============ BOOM STUDIO V2: THƯ VIỆN / BỘ LỌC / INSPECTOR ============
    def _fmt_ms(self, ms):
        try:
            sec = max(0, int(ms) // 1000)
            h, rem = divmod(sec, 3600); m, s = divmod(rem, 60)
            return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        except Exception:
            return "—"

    def _card_state(self, card):
        """Trạng thái thật của 1 tập, đọc trực tiếp từ file đầu ra trên đĩa.

        Điểm quan trọng cho realtime: KHÔNG chỉ dựa vào card.srt_path hiện tại.
        Pipeline có thể vừa tạo *_vi.srt / *_dubbed.mp4 nhưng card chưa kịp đổi
        đường dẫn. Hàm này vẫn nhận ra ngay file mới xuất hiện.
        """
        vp = getattr(card, "video_path", "") or ""
        sp = getattr(card, "srt_path", None)
        stem, ext = os.path.splitext(vp)

        # Quy về stem GỐC để dò các output cạnh video.
        low_stem = stem.lower()
        base = stem
        for suffix in ("_dubbed", "_final"):
            if low_stem.endswith(suffix):
                base = stem[:-len(suffix)]
                break

        vi_path = base + "_vi.srt"
        raw_path = base + ".srt"
        dubbed_path = base + "_dubbed" + ext
        final_path = base + "_final.mp4"

        has_sp = bool(sp and os.path.exists(sp))
        has_vi_disk = os.path.exists(vi_path)
        has_raw_disk = os.path.exists(raw_path)
        has_sub = bool(has_sp or has_vi_disk or has_raw_disk)
        translated = bool(has_vi_disk or (has_sp and str(sp).lower().endswith("_vi.srt")))
        dubbed = bool(stem.lower().endswith("_dubbed") or os.path.exists(dubbed_path))
        exported = bool(os.path.exists(final_path) or str(vp).lower().endswith("_final.mp4"))

        status_text = ""
        try:
            status_text = card.lbl_badge.text().strip().lower()
        except Exception:
            pass
        error = ("lỗi" in status_text) or ("error" in status_text)
        edited = bool(getattr(card, "design_config", None))

        # orientation: không probe tất cả file. Dùng thumbnail nếu có; nếu chưa có
        # thì coi là chưa xác định để tránh lag lúc thêm hàng trăm video.
        portrait = landscape = False
        pix = getattr(card, "_thumb_pix", None)
        try:
            if pix is not None and not pix.isNull() and pix.height() > 0:
                portrait = pix.height() > pix.width()
                landscape = not portrait
        except Exception:
            pass

        done = exported or (has_sub and translated and dubbed)
        return {"has_sub": has_sub, "translated": translated, "dubbed": dubbed,
                "exported": exported, "error": error, "edited": edited,
                "portrait": portrait, "landscape": landscape, "done": done}

    @staticmethod
    def _rt_state_signature(st):
        """Chỉ các trạng thái ảnh hưởng bộ lọc/counter; bỏ orientation để tránh
        relayout khi thumbnail vừa load."""
        return (bool(st.get("has_sub")), bool(st.get("translated")),
                bool(st.get("dubbed")), bool(st.get("exported")),
                bool(st.get("error")), bool(st.get("edited")),
                bool(st.get("done")))

    def _ensure_realtime_watch_dir(self, folder):
        """Đăng ký 1 thư mục đúng 1 lần với QFileSystemWatcher."""
        try:
            if not folder or not os.path.isdir(folder):
                return
            full = os.path.abspath(folder)
            key = os.path.normcase(full)
            if key in self._rt_watch_dirs:
                return
            ok = self._rt_fs_watcher.addPath(full)
            if ok:
                self._rt_watch_dirs.add(key)
        except Exception:
            pass

    def _on_realtime_dir_changed(self, _path):
        # Pipeline thường tạo nhiều file tạm trong vài chục ms. Debounce để 20
        # sự kiện liên tiếp chỉ biến thành 1 lần refresh rất nhẹ.
        self._schedule_realtime_refresh(140)

    def _schedule_realtime_refresh(self, delay_ms=80):
        try:
            self._rt_refresh_timer.start(max(0, int(delay_ms)))
        except Exception:
            pass

    def _realtime_refresh_states(self):
        """Đồng bộ card + counter theo file vừa xuất hiện trên ổ đĩa.

        Đây là realtime theo SỰ KIỆN, không polling liên tục. Mỗi lần chạy chỉ
        so signature cũ/mới; card không đổi trạng thái thì không đụng UI.
        """
        if getattr(self, "_bulk_loading", False):
            # Đợi load thư mục xong rồi cập nhật một lần, tránh tranh main thread.
            self._schedule_realtime_refresh(220)
            return
        if not getattr(self, "cards", None):
            return

        changed_cards = []
        for card in list(self.cards):
            key = id(card)
            try:
                st_before_sync = self._card_state(card)
                sig_now = self._rt_state_signature(st_before_sync)
                old_sig = self._rt_state_cache.get(key)

                # Lần đầu chỉ lưu cache; các lần sau chỉ xử lý card thật sự đổi.
                if old_sig is None:
                    self._rt_state_cache[key] = sig_now
                    continue
                if sig_now == old_sig:
                    continue

                # Nếu SRT Việt/dubbed vừa xuất hiện, cập nhật luôn path/badge trên card.
                try:
                    card.refresh_srt_from_disk()
                except Exception:
                    pass
                try:
                    card._update_source_badges()
                except Exception:
                    pass

                st_after = self._card_state(card)
                self._rt_state_cache[key] = self._rt_state_signature(st_after)
                changed_cards.append(card)
            except Exception:
                continue

        if not changed_cards:
            # Cache có thể chưa có ở lần đầu sau khi load; counter hiện tại vẫn đúng.
            return

        # Counter đổi NGAY sau khi 1 tập hoàn thành Tách/Dịch/Lồng/Render.
        self._update_sidebar_counts()

        # Nếu đang lọc "Chưa dịch/Chưa lồng/...", tập vừa hoàn thành phải biến
        # khỏi danh sách ngay. Chỉ relayout khi filter phụ thuộc trạng thái.
        if (self._library_filter or "all") != "all":
            self._relayout_grid()

        # Inspector của tập đang chọn cũng cập nhật theo thời gian thực.
        if self.selected_card in changed_cards:
            self._refresh_selected_file_info(self.selected_card)

        # Log gọn, không spam từng event file tạm.
        if len(changed_cards) == 1:
            self._log(f"⚡ Realtime: đã cập nhật {os.path.basename(changed_cards[0].video_path)}")
        else:
            self._log(f"⚡ Realtime: đã cập nhật trạng thái {len(changed_cards)} tập")

    def _card_matches_filter(self, card):
        q = (self._search_text or "").strip().lower()
        if q:
            hay = f"{os.path.basename(getattr(card,'video_path',''))} {os.path.basename(getattr(card,'srt_path','') or '')}".lower()
            if q not in hay:
                return False
        mode = self._library_filter or "all"
        if mode == "all": return True
        st = self._card_state(card)
        if mode == "nosub": return not st["has_sub"]
        if mode == "notrans": return not st["translated"]
        if mode == "nodub": return not st["dubbed"]
        if mode == "translated": return st["translated"]
        if mode == "dubbed": return st["dubbed"]
        if mode == "exported": return st["exported"]
        if mode == "error": return st["error"]
        if mode == "edited": return st["edited"]
        if mode == "done": return st["done"]
        if mode == "portrait": return st["portrait"]
        if mode == "landscape": return st["landscape"]
        return True

    def _visible_cards_for_grid(self):
        return [c for c in self.cards if self._card_matches_filter(c)]

    def _toggle_select_all_visible(self, checked):
        for c in self._visible_cards_for_grid():
            try:
                c.chk_select.blockSignals(True); c.chk_select.setChecked(bool(checked)); c.chk_select.blockSignals(False)
            except Exception:
                pass
        self._update_run_label()

    def _set_search_text(self, text):
        self._search_text = (text or "").strip()
        if not self._bulk_loading:
            self._relayout_grid()

    def _set_library_filter(self, mode):
        self._library_filter = mode or "all"
        for key, btn in getattr(self, "_filter_buttons", {}).items():
            canonical = key[4:] if key.startswith("top_") else key
            btn.blockSignals(True); btn.setChecked(canonical == self._library_filter); btn.blockSignals(False)
        if not self._bulk_loading:
            self._relayout_grid()
        self._update_sidebar_counts()

    def _update_sidebar_counts(self):
        if not hasattr(self, "_sidebar_counts"):
            return
        stats = {k: 0 for k in ("all", "edited", "exported", "error", "nosub", "notrans", "nodub", "done")}
        stats["all"] = len(self.cards)
        for c in self.cards:
            st = self._card_state(c)
            stats["edited"] += int(st["edited"])
            stats["exported"] += int(st["exported"])
            stats["error"] += int(st["error"])
            stats["nosub"] += int(not st["has_sub"])
            stats["notrans"] += int(not st["translated"])
            stats["nodub"] += int(not st["dubbed"])
            stats["done"] += int(st["done"])
        for k, w in self._sidebar_counts.items():
            w.setText(str(stats.get(k, 0)))

    def _refresh_selected_file_info(self, card):
        if card is None: return
        try:
            name = os.path.basename(card.video_path)
            self.lbl_selected_title.setText(f"🔧  Chỉnh sửa: {name}")
            self.lbl_fix_v.setText(name); self.lbl_fix_v.setToolTip(card.video_path)
            self.lbl_info_size.setText(f"{os.path.getsize(card.video_path)/(1024*1024):.1f} MB" if os.path.exists(card.video_path) else "—")
            self.lbl_info_sub.setText(os.path.basename(card.srt_path) if card.srt_path else "Chưa có sub")
            ns = self.video_item.nativeSize()
            if ns.width() > 0 and ns.height() > 0:
                self.lbl_info_res.setText(f"{int(ns.width())} × {int(ns.height())}")
            else:
                self.lbl_info_res.setText("Đang đọc…")
            self.lbl_info_dur.setText(self._fmt_ms(self.media_player.duration()) if self.media_player.duration() else "Đang đọc…")
        except Exception:
            pass

    def _reset_current_design(self):
        if self.selected_card is None:
            return
        self.selected_card.design_config = copy.deepcopy(self._default_design_template)
        self._restore_design_from_card(self.selected_card)
        self._log(f"↩ Đã đặt lại thiết kế: {os.path.basename(self.selected_card.video_path)}")

    def _apply_design_scope(self):
        if self.selected_card is None:
            QMessageBox.information(self, "Chưa chọn video", "Hãy chọn một video làm mẫu trước.")
            return
        self._save_design_to_card(self.selected_card)
        master = copy.deepcopy(self.selected_card.design_config or self._default_design())
        scope = self.cmb_apply_scope.currentText() if hasattr(self, "cmb_apply_scope") else "Tất cả video"
        if scope == "Các video đã chọn":
            targets = self._selected_cards()
        elif scope == "Từ video này trở đi":
            try: targets = self.cards[self.cards.index(self.selected_card):]
            except Exception: targets = [self.selected_card]
        else:
            targets = list(self.cards)
        for c in targets:
            if c is not self.selected_card:
                c.design_config = copy.deepcopy(master)
        self._log(f"✓ Đã áp dụng thiết kế cho {len(targets)} video ({scope}).")
        QMessageBox.information(self, "Đã áp dụng", f"Đã áp dụng thiết lập hiện tại cho {len(targets)} video.\nCác video khác không bị thay đổi.")

    # ============ GHÉP CẶP VIDEO + SRT ============
    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Chọn thư mục chứa các tập")
        if not d:
            return
        pairs = self._auto_pair(d)
        if not pairs:
            QMessageBox.information(self, "Không thấy video", "Thư mục này không có file video (.mp4) nào.")
            return
        self._start_bulk_card_import(pairs, os.path.basename(d) or d)

    def _pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Chọn video", "", "Video (*.mp4 *.mkv *.mov *.avi)")
        if not files:
            return
        pairs = [(vp, self._guess_srt_for(vp)) for vp in files]
        self._start_bulk_card_import(pairs, "video đã chọn")

    def _start_bulk_card_import(self, pairs, source_label="thư mục"):
        """Nạp hàng trăm video theo lô nhỏ thay vì tạo toàn bộ QWidget trong một
        vòng for trên main-thread. Windows sẽ không còn báo Not Responding khi thư
        mục có 100-500 tập. Thumbnail cũng tạm hoãn tới khi nạp card xong."""
        if self._bulk_loading:
            QMessageBox.information(self, "Đang nạp video",
                                    "Tool đang nạp danh sách hiện tại. Vui lòng chờ vài giây.")
            return

        # Lọc trùng trước bằng set O(1), tránh _add_card phải quét 1..N card lặp lại.
        pending = []
        known = set(getattr(self, "_card_path_set", set()))
        for vp, sp in pairs:
            try:
                key = os.path.normcase(os.path.abspath(vp))
            except Exception:
                key = str(vp)
            if key in known:
                continue
            known.add(key)
            pending.append((vp, sp))

        if not pending:
            QMessageBox.information(self, "Không có video mới",
                                    "Các video trong lựa chọn này đã có trong danh sách.")
            return

        self._bulk_loading = True
        self._bulk_pairs = pending
        self._bulk_index = 0
        self._bulk_source_label = source_label
        self._bulk_started_at = time.time()

        # Dừng thumbnail trong lúc dựng hàng trăm card để CPU/ổ đĩa chỉ tập trung cho UI.
        try:
            self._thumb_view_timer.stop()
        except Exception:
            pass

        total = len(pending)
        if hasattr(self, "lbl_editor_count"):
            self.lbl_editor_count.setText(f"Đang nạp 0/{total}")
        if hasattr(self, "lbl_selected_count"):
            self.lbl_selected_count.setText(f"Đang đọc {total} video…")
        self.setCursor(Qt.CursorShape.WaitCursor)

        # Cho Qt vẽ lại giao diện trước, rồi mới bắt đầu tạo card.
        QTimer.singleShot(0, self._import_next_card_batch)

    def _import_next_card_batch(self):
        if not self._bulk_loading:
            return
        total = len(self._bulk_pairs)
        start = self._bulk_index
        end = min(total, start + max(1, int(self._bulk_batch_size)))

        try:
            cols = int(self.cmb_layout.currentText()) if hasattr(self, "cmb_layout") else 3
        except Exception:
            cols = 3
        cols = max(2, min(4, cols))

        # Tạo một lô nhỏ. Không gọi _relayout_grid() sau mỗi card vì đó là phần
        # gây lag lớn nhất khi có 200-500 video. Card mới được đặt thẳng đúng ô.
        for i in range(start, end):
            vp, sp = self._bulk_pairs[i]
            card = self._add_card(vp, sp)
            if card is not None:
                if self._library_filter == "all" and not self._search_text:
                    idx = self.cards.index(card)
                    self.grid_lay.addWidget(card, idx // cols, idx % cols)

        self._bulk_index = end
        if hasattr(self, "lbl_editor_count"):
            self.lbl_editor_count.setText(f"Đang nạp {end}/{total}")
        if hasattr(self, "lbl_selected_count"):
            self.lbl_selected_count.setText(f"Đang nạp video… {end}/{total}")

        if end < total:
            # Nhả event loop sau mỗi lô: cửa sổ vẫn kéo/di chuyển/bấm được,
            # Windows không đánh dấu ứng dụng Not Responding.
            QTimer.singleShot(0, self._import_next_card_batch)
            return

        # Hoàn tất. Lúc này mới chọn tập đầu + nạp thumbnail của vùng đang thấy.
        self._bulk_loading = False
        self._bulk_pairs = []
        self._bulk_index = 0
        self.unsetCursor()
        self._update_run_label()
        if self._library_filter != "all" or self._search_text:
            self._relayout_grid()
        if self.selected_card is None and self.cards:
            QTimer.singleShot(0, lambda c=self.cards[0]: self._on_card_clicked(c))
        self._schedule_visible_thumbnails()
        self._schedule_realtime_refresh(0)
        elapsed = max(0.0, time.time() - getattr(self, "_bulk_started_at", time.time()))
        self._log(f"📂 Đã nạp {total} video từ {self._bulk_source_label} trong {elapsed:.1f}s (không khóa giao diện).")

    def _auto_pair(self, folder):
        """Quét folder, ghép cặp video+srt.

        Ưu tiên theo mỗi tập: *_dubbed > video gốc > *_final.
        *_final chỉ được dùng khi video gốc/dubbed đã không còn (ví dụ sau khi
        dọn file trung gian). Nhờ vậy mở lại thư mục sau khi render KHÔNG sinh
        thêm 1 card *_final cho cùng một tập và không render lặp thành
        *_final_final.mp4. File *_TronBo_Rendered.mp4 không đưa vào grid tập.
        """
        try:
            names = os.listdir(folder)
        except Exception:
            return []
        videos = [n for n in names if n.lower().endswith((".mp4", ".mkv", ".mov", ".avi"))]
        srt_set = set(n for n in names if n.lower().endswith(".srt"))

        groups = {}   # base_stem -> {"dubbed":..., "plain":..., "final":...}
        for v in videos:
            stem = os.path.splitext(v)[0]
            low = stem.lower()
            if low.endswith("_tronbo_rendered"):
                continue
            if low.endswith("_dubbed"):
                base = stem[:-len("_dubbed")]
                groups.setdefault(base, {})["dubbed"] = v
            elif low.endswith("_final"):
                base = stem[:-len("_final")]
                groups.setdefault(base, {})["final"] = v
            else:
                groups.setdefault(stem, {})["plain"] = v

        pairs = []
        for base in sorted(groups.keys(), key=_natural_key):
            g = groups[base]
            video = g.get("dubbed") or g.get("plain") or g.get("final")
            if not video:
                continue
            srt = None
            if f"{base}_vi.srt" in srt_set:
                srt = f"{base}_vi.srt"
            elif f"{base}.srt" in srt_set:
                srt = f"{base}.srt"
            vp = os.path.join(folder, video)
            sp = os.path.join(folder, srt) if srt else None
            pairs.append((vp, sp))
        return pairs

    def _guess_srt_for(self, video_path):
        """Đoán srt đi kèm 1 video lẻ (khi thêm bằng + File)."""
        stem = os.path.splitext(video_path)[0]
        low = stem.lower()
        if low.endswith("_dubbed"):
            stem = stem[:-len("_dubbed")]
        elif low.endswith("_final"):
            stem = stem[:-len("_final")]
        for cand in (stem + "_vi.srt", stem + ".srt"):
            if os.path.exists(cand):
                return cand
        return None

    def _selected_cards(self):
        """Các tập đang được tick trong grid. Card cũ/ngoài editor vẫn fallback chọn."""
        return [c for c in self.cards if not hasattr(c, "is_checked") or c.is_checked()]

    def _find_pipeline_page_index(self):
        """Tìm đúng PAGE của QTabWidget đang chứa dub_feature_tab.

        render_dub_feature.attach_dub_tab() có thể thêm trực tiếp widget pipeline,
        hoặc bọc nó trong QScrollArea/QWidget. Bản V2 cũ gọi setCurrentWidget()
        bằng widget con nên Qt không báo exception nhưng cũng không đổi tab -> bấm
        Dịch/Lồng tưởng như "không hiện gì".
        """
        tab = getattr(self, "dub_feature_tab", None)
        if tab is None:
            return -1

        try:
            direct = self.tabs.indexOf(tab)
            if direct >= 0:
                return direct
        except Exception:
            pass

        # Pipeline thường được bọc trong QScrollArea/viewport. Tìm page cha.
        try:
            for i in range(self.tabs.count()):
                page = self.tabs.widget(i)
                if page is tab:
                    return i
                try:
                    if page.isAncestorOf(tab):
                        return i
                except Exception:
                    pass
                try:
                    if tab in page.findChildren(QWidget):
                        return i
                except Exception:
                    pass
        except Exception:
            pass

        # Fallback theo tên tab do module ngoài tạo.
        try:
            for i in range(self.tabs.count()):
                txt = (self.tabs.tabText(i) or "").lower()
                if any(k in txt for k in ("sub", "dịch", "dich", "lồng", "long", "tts")):
                    # Tránh chọn nhầm tab Phụ đề chính của editor.
                    if txt.strip() not in ("phụ đề", "phu de"):
                        return i
        except Exception:
            pass
        return -1

    def _focus_pipeline_stage(self, stage=None):
        """Sau khi mở pipeline, cố chuyển đến mục Tách/Dịch/Lồng tương ứng.
        Không phụ thuộc chặt vào implementation của render_dub_feature.py.
        """
        idx = self._find_pipeline_page_index()
        if idx < 0:
            QMessageBox.information(
                self, "Pipeline",
                "Không tìm thấy giao diện Sub → Dịch → Lồng.\n"
                "Hãy kiểm tra render_dub_feature.py có nằm cạnh app và được nạp thành công.")
            return False

        self.tabs.setCurrentIndex(idx)
        page = self.tabs.widget(idx)

        # Đảm bảo inspector phải nhìn thấy page vừa chọn.
        try:
            self.tabs.setFocus()
        except Exception:
            pass

        if not stage:
            return True

        targets = {
            "extract": ("tách", "tach", "stt", "sub"),
            "translate": ("dịch", "dich", "translate"),
            "dub": ("lồng", "long", "tts", "voice", "giọng", "giong"),
        }.get(stage, ())

        # Nếu module pipeline có QTabWidget con, chọn tab đúng theo text.
        try:
            for child_tabs in page.findChildren(QTabWidget):
                if child_tabs is self.tabs:
                    continue
                for j in range(child_tabs.count()):
                    txt = (child_tabs.tabText(j) or "").lower()
                    if targets and any(k in txt for k in targets):
                        child_tabs.setCurrentIndex(j)
                        return True
        except Exception:
            pass

        # Một số bản render_dub_feature lưu widget nội bộ trực tiếp trên tab.
        pipeline = getattr(self, "dub_feature_tab", None)
        for attr in {
            "extract": ("tab_extract", "tab_stt", "page_extract"),
            "translate": ("tab_translate", "page_translate", "translate_tab"),
            "dub": ("tab_dub", "tab_tts", "page_dub", "tts_tab"),
        }.get(stage, ()):
            try:
                target = getattr(pipeline, attr, None)
                if target is None:
                    continue
                for child_tabs in page.findChildren(QTabWidget):
                    k = child_tabs.indexOf(target)
                    if k >= 0:
                        child_tabs.setCurrentIndex(k)
                        return True
            except Exception:
                pass
        return True

    def _open_pipeline_tab(self):
        """Mở giao diện Sub → Dịch → Lồng."""
        self._focus_pipeline_stage(None)

    def _open_pipeline_extract(self):
        self._focus_pipeline_stage("extract")

    def _open_pipeline_translate(self):
        self._focus_pipeline_stage("translate")

    def _open_pipeline_dub(self):
        self._focus_pipeline_stage("dub")

    def _on_layout_columns_changed(self, text):
        try:
            cols = max(2, min(4, int(text)))
        except Exception:
            cols = 3
        self.settings.setValue("editor_grid_columns", str(cols))
        self._relayout_grid()

    def _add_card(self, video_path, srt_path):
        # Tránh trùng bằng set O(1). Với 260 video, cách cũ quét self.cards ở
        # mỗi lần thêm sẽ thành O(N²) và làm bước mở thư mục chậm rõ rệt.
        try:
            key = os.path.normcase(os.path.abspath(video_path))
        except Exception:
            key = str(video_path)
        if key in self._card_path_set:
            return None
        card = EpisodeCard(video_path, srt_path)
        card.clicked.connect(self._on_card_clicked)
        card.play_requested.connect(self._on_card_play)
        card.zoom_requested.connect(self._on_card_zoom)
        card.seek_requested.connect(self._on_card_seek)
        card.selection_changed.connect(self._update_run_label)
        self.cards.append(card)
        self._card_path_set.add(key)

        # Realtime: theo dõi thư mục chứa output của tập này. Nếu SRT nằm ở thư
        # mục khác (thêm thủ công) thì theo dõi luôn thư mục đó.
        self._ensure_realtime_watch_dir(os.path.dirname(video_path))
        if srt_path:
            self._ensure_realtime_watch_dir(os.path.dirname(srt_path))
        try:
            self._rt_state_cache[id(card)] = self._rt_state_signature(self._card_state(card))
        except Exception:
            pass

        if not self._bulk_loading:
            self._update_sidebar_counts()
        # Khi nạp thư mục lớn, không tự mở media của tập đầu giữa quá trình dựng card.
        if self.selected_card is None and not self._bulk_loading:
            QTimer.singleShot(0, lambda c=card: self._on_card_clicked(c))
        return card

    def _relayout_grid(self):
        # 2/3/4 cột theo lựa chọn ở góc phải dưới, mặc định 3 như ảnh mẫu.
        for i in reversed(range(self.grid_lay.count())):
            w = self.grid_lay.itemAt(i).widget()
            if w:
                self.grid_lay.removeWidget(w)
        try:
            cols = int(self.cmb_layout.currentText()) if hasattr(self, "cmb_layout") else 3
        except Exception:
            cols = 3
        cols = max(2, min(4, cols))
        # setColumnStretch chỉ cần làm 1 lần cho số cột hiện tại.
        for col in range(4):
            self.grid_lay.setColumnStretch(col, 1 if col < cols else 0)
        visible_cards = self._visible_cards_for_grid()
        for card in self.cards:
            card.setVisible(card in visible_cards)
        for idx, card in enumerate(visible_cards):
            self.grid_lay.addWidget(card, idx // cols, idx % cols)
        self._schedule_visible_thumbnails()

    def _schedule_visible_thumbnails(self):
        timer = getattr(self, "_thumb_view_timer", None)
        if timer is not None:
            timer.start()

    def _load_visible_thumbnails(self):
        """Chỉ nạp ảnh cho vùng nhìn thấy + một hàng đệm trên/dưới."""
        # Trong lúc nạp hàng trăm card, thumbnail phải đứng yên để không tranh CPU/IO
        # và không kích hoạt thêm layout/paint giữa quá trình import.
        if self._bulk_loading:
            return
        if not self.cards or not hasattr(self, "scroll_grid"):
            return
        vp = self.scroll_grid.viewport()
        h = vp.height()
        margin = 360  # preload xấp xỉ hơn 1 hàng card
        for card in self.cards:
            # Card đang có player thật thì không cần thumbnail.
            if getattr(card, "_preview_attached", False):
                continue
            try:
                p = card.mapTo(vp, QPoint(0, 0))
                y0, y1 = p.y(), p.y() + card.height()
                if y1 >= -margin and y0 <= h + margin:
                    card.ensure_thumbnail()
            except Exception:
                # Fallback an toàn: nếu Qt chưa map được lúc layout vừa tạo,
                # chỉ nạp vài card đầu thay vì nạp toàn bộ.
                for c in self.cards[:12]:
                    c.ensure_thumbnail()
                break

    def _ensure_preview_alive(self):
        """Đảm bảo shared PreviewGraphicsView chưa bị Qt xoá cùng EpisodeCard cũ.

        Preview được di chuyển qua lại giữa các card. Khi một card bị deleteLater(),
        mọi QWidget còn parent với card đó cũng bị Qt xoá. Nếu self.preview vẫn trỏ
        tới wrapper Python cũ thì lần click card kế tiếp sẽ crash với:
        RuntimeError: wrapped C/C++ object of type PreviewGraphicsView has been deleted.
        """
        p = getattr(self, "preview", None)
        alive = False
        if p is not None:
            try:
                p.parentWidget()
                alive = True
            except RuntimeError:
                alive = False
            except Exception:
                alive = True
        if not alive:
            self.preview = PreviewGraphicsView(self.scene, self)
            self.preview.setMinimumHeight(145)
            self.preview.setStyleSheet("background:#050607; border:none;")
            self.preview.hide()
            self._preview_card = None
        return self.preview

    def _park_preview(self):
        """Tách preview khỏi card hiện tại và chuyển quyền sở hữu về RenderWidget.
        Phải làm TRƯỚC khi xoá card để Qt không xoá luôn shared preview.
        """
        p = self._ensure_preview_alive()
        old = getattr(self, "_preview_card", None)
        if old is not None:
            try:
                old.detach_preview_widget(p)
            except Exception:
                pass
        self._preview_card = None
        try:
            p.setParent(self)
            p.hide()
        except RuntimeError:
            # Nếu Qt đã xoá đúng lúc chuyển parent, dựng lại ngay một preview sạch.
            self.preview = PreviewGraphicsView(self.scene, self)
            self.preview.setMinimumHeight(145)
            self.preview.setStyleSheet("background:#050607; border:none;")
            self.preview.hide()
        except Exception:
            pass

    def _clear_all(self):
        # Không xoá card khi worker còn dùng card đó; tránh wrapped C/C++ deleted.
        _merge_busy = False
        try:
            _merge_busy = hasattr(self, "merge_thread") and self.merge_thread is not None and self.merge_thread.isRunning()
        except Exception:
            _merge_busy = False
        if getattr(self, "_render_running", False) or _merge_busy:
            QMessageBox.information(self, "Đang xử lý", "Đang render/gộp file. Hãy dừng hoặc chờ xong trước khi xóa danh sách video.")
            return
        # Hủy phần còn lại nếu người dùng dọn danh sách giữa lúc đang nạp.
        self._bulk_loading = False
        self._bulk_pairs = []
        self._bulk_index = 0
        try:
            self.unsetCursor()
        except Exception:
            pass
        self.media_player.stop()
        # QUAN TRỌNG: preview là widget dùng chung. Remove khỏi layout thôi chưa đủ,
        # vì parent Qt vẫn là EpisodeCard cũ. Phải re-parent về RenderWidget trước
        # khi deleteLater() các card, nếu không preview sẽ bị xoá theo card.
        self._park_preview()
        for c in self.cards:
            c.setParent(None)
            c.deleteLater()
        self.cards = []
        self._card_path_set.clear()
        # Dọn realtime cache + watcher cũ để mở project/thư mục khác không giữ
        # hàng chục directory watch không còn dùng.
        try:
            self._rt_state_cache.clear()
            watched = list(self._rt_fs_watcher.directories())
            if watched:
                self._rt_fs_watcher.removePaths(watched)
            self._rt_watch_dirs.clear()
        except Exception:
            pass
        self.selected_card = None
        self.lbl_fix_v.setText("—"); self.lbl_fix_s.setText("—")
        if hasattr(self, "lbl_selected_title"): self.lbl_selected_title.setText("🔧  Chỉnh sửa: Chưa chọn video")
        for _name in ("res", "dur", "size", "sub"):
            _w = getattr(self, f"lbl_info_{_name}", None)
            if _w is not None: _w.setText("—")
        self._update_run_label()

    def _on_card_zoom(self, card):
        """Phóng to khung xem trước ra cửa sổ lớn để canh chữ/logo cho dễ.
        Dùng LẠI chính self.preview (PreviewGraphicsView) nên mọi kéo/thả chữ,
        logo, vùng mờ đều áp thẳng lên cấu hình card đang chọn. Đóng cửa sổ ->
        preview tự trả về card như cũ."""
        # Đảm bảo card này đang là card đang chọn + đang giữ preview.
        if self.selected_card is not card or getattr(self, "_preview_card", None) is not card:
            self._on_card_clicked(card)
        if getattr(self, "_preview_card", None) is not card:
            return

        # Gỡ preview khỏi card, nhét vào dialog lớn.
        try:
            card.detach_preview_widget(self.preview)
        except Exception:
            pass

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Xem trước — {os.path.basename(card.video_path)}")
        dlg.setStyleSheet("QDialog { background:#0b0d10; }")
        # To ~80% màn hình, ưu tiên cao cho video dọc 9:16.
        try:
            scr = QApplication.primaryScreen().availableGeometry()
            dlg.resize(int(scr.width() * 0.55), int(scr.height() * 0.9))
        except Exception:
            dlg.resize(700, 900)

        vlay = QVBoxLayout(dlg)
        vlay.setContentsMargins(8, 8, 8, 8)
        vlay.setSpacing(8)

        hint = QLabel("Kéo trực tiếp chữ / logo trên video. Lăn chuột để phóng to/thu nhỏ chữ. Đóng để lưu vị trí.")
        hint.setStyleSheet("color:#8D949E; font-size:12px; border:none;")
        hint.setWordWrap(True)
        vlay.addWidget(hint)

        self.preview.setMinimumHeight(0)
        self.preview.show()
        vlay.addWidget(self.preview, 1)

        btn_close = QPushButton("Đóng & lưu vị trí")
        btn_close.setStyleSheet(
            "QPushButton { background:#1f6feb; color:#fff; border:none; border-radius:6px; "
            "padding:8px 14px; font-weight:bold; } QPushButton:hover { background:#2f7bf6; }")
        btn_close.clicked.connect(dlg.accept)
        vlay.addWidget(btn_close)

        # Fit lại scene khi cửa sổ mở/đổi cỡ.
        def _fit():
            try:
                if self.scene and not self.scene.sceneRect().isEmpty():
                    self.preview.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            except Exception:
                pass
        QTimer.singleShot(0, _fit)
        QTimer.singleShot(120, _fit)

        dlg.exec()

        # Đóng dialog -> trả preview về đúng card, khôi phục chiều cao nhỏ.
        p = self._ensure_preview_alive()
        try:
            vlay.removeWidget(p)
        except Exception:
            pass
        try:
            p.setParent(self)
            p.hide()
            p.setMinimumHeight(145)
        except RuntimeError:
            p = self._ensure_preview_alive()
        except Exception:
            pass
        try:
            card.attach_preview_widget(p)
            self._preview_card = card
            QTimer.singleShot(0, self._reset_pos)
        except Exception:
            # Nếu card đã bị đóng/xoá lúc dialog mở, giữ preview an toàn ở host.
            try:
                p.setParent(self); p.hide()
            except Exception:
                pass
            self._preview_card = None

    def _attach_preview_to_card(self, card):
        if card is None:
            return
        p = self._ensure_preview_alive()
        old = getattr(self, "_preview_card", None)
        if old is not None and old is not card:
            try:
                old.detach_preview_widget(p)
                old.btn_card_play.setText("▶")
            except Exception:
                pass
            # RemoveWidget không đổi parent. Park tạm về RenderWidget để nếu card
            # cũ bị deleteLater() thì shared preview không bị chết theo.
            try:
                p.setParent(self)
                p.hide()
            except RuntimeError:
                p = self._ensure_preview_alive()
            except Exception:
                pass
        if old is not card:
            try:
                card.attach_preview_widget(p)
                self._preview_card = card
                QTimer.singleShot(0, self._reset_pos)
            except RuntimeError:
                # Phòng trường hợp wrapper C++ vừa bị xoá bởi event deleteLater cũ.
                p = self._ensure_preview_alive()
                card.attach_preview_widget(p)
                self._preview_card = card
                QTimer.singleShot(0, self._reset_pos)

    def _on_card_clicked(self, card):
        old = self.selected_card
        # Bấm lại đúng card đang chỉnh thì KHÔNG reload cấu hình, tránh mất thay đổi
        # vừa kéo/chỉnh nhưng chưa chuyển sang card khác.
        if old is card and getattr(self, "_preview_card", None) is card:
            card.set_selected(True)
            return
        if old is not None and old is not card:
            self._save_design_to_card(old)
            old.set_selected(False)
        card.set_selected(True)
        self.selected_card = card
        self.lbl_fix_v.setText(os.path.basename(card.video_path))
        self.lbl_fix_v.setToolTip(card.video_path)
        self.lbl_fix_s.setText(os.path.basename(card.srt_path) if card.srt_path else "⚠ chưa có sub")
        self.lbl_fix_s.setToolTip(card.srt_path or "")
        self._refresh_selected_file_info(card)
        self._attach_preview_to_card(card)
        self._load_preview(card.video_path)
        QTimer.singleShot(60, lambda c=card: self._restore_design_from_card(c)
                          if self.selected_card is c else None)

    def _on_card_play(self, card):
        same_card = (self.selected_card is card and getattr(self, "_preview_card", None) is card)
        if not same_card:
            self._on_card_clicked(card)
        self._toggle_play()

    def _on_card_seek(self, card, pos_ms):
        if card is not self.selected_card or getattr(self, "_preview_card", None) is not card:
            self._on_card_clicked(card)
        try:
            self.media_player.setPosition(int(pos_ms))
        except Exception:
            pass

    def _change_video(self):
        if not self.selected_card:
            return
        fp, _ = QFileDialog.getOpenFileName(self, "Chọn video khác", "", "Video (*.mp4 *.mkv *.mov *.avi)")
        if fp:
            old_path = self.selected_card.video_path
            try:
                self._card_path_set.discard(os.path.normcase(os.path.abspath(old_path)))
                self._card_path_set.add(os.path.normcase(os.path.abspath(fp)))
            except Exception:
                pass
            self.selected_card.video_path = fp
            self.selected_card.lbl_name.setText(os.path.basename(fp))
            self.selected_card.lbl_name.setToolTip(fp)
            try:
                self.selected_card._load_thumbnail()
                self.selected_card._update_source_badges()
            except Exception:
                pass
            self.lbl_fix_v.setText(os.path.basename(fp)); self.lbl_fix_v.setToolTip(fp)
            self._ensure_realtime_watch_dir(os.path.dirname(fp))
            self._refresh_selected_file_info(self.selected_card)
            self._update_run_label()
            self._schedule_realtime_refresh(0)
            self._load_preview(fp)

    def _change_srt(self):
        if not self.selected_card:
            return
        fp, _ = QFileDialog.getOpenFileName(self, "Chọn sub khác", "", "Phụ đề (*.srt)")
        if fp:
            self.selected_card.srt_path = fp
            self.selected_card.lbl_srt.setText("S1  " + os.path.basename(fp))
            self.selected_card.lbl_srt.setToolTip(fp)
            self.selected_card.lbl_srt.setStyleSheet("color:#63D79A; font-size:9px; border:none;")
            try:
                self.selected_card._update_source_badges()
            except Exception:
                pass
            self.lbl_fix_s.setText(os.path.basename(fp)); self.lbl_fix_s.setToolTip(fp)
            self._ensure_realtime_watch_dir(os.path.dirname(fp))
            self._refresh_selected_file_info(self.selected_card)
            self._update_run_label()
            self._schedule_realtime_refresh(0)

    def _update_run_label(self):
        n_all = len(self.cards)
        n_sel = len(self._selected_cards())
        if hasattr(self, "btn_run"):
            self.btn_run.setText(f"④  Render & Xuất ({n_sel})\nTạo video hoàn chỉnh")
        if hasattr(self, "btn_merge_now"):
            self.btn_merge_now.setText(f"🔗  Gộp nhanh ({n_sel})")
        if hasattr(self, "lbl_editor_count"):
            self.lbl_editor_count.setText(f"{n_all} video")
        if hasattr(self, "lbl_selected_count"):
            self.lbl_selected_count.setText(f"Đã chọn {n_sel}/{n_all}")
        if hasattr(self, "lbl_bottom_count"):
            self.lbl_bottom_count.setText(f"{n_all} tập")
        self._update_sidebar_counts()

    # ============ THIẾT KẾ RIÊNG CHO TỪNG VIDEO ============
    def _default_design(self):
        return copy.deepcopy(getattr(self, "_default_design_template", None) or self._collect_design(persist=False))

    def _save_design_to_card(self, card=None):
        if self._loading_card_design:
            return
        card = card or self.selected_card
        if card is None:
            return
        card.design_config = copy.deepcopy(self._collect_design(persist=True))
        self._design_locked = None

    def _clear_scene_design_items(self):
        for b in list(getattr(self, "blur_boxes", []) or []):
            try: self.scene.removeItem(b)
            except Exception: pass
        self.blur_boxes = []
        if getattr(self, "sample_sub", None) is not None:
            try: self.scene.removeItem(self.sample_sub)
            except Exception: pass
            self.sample_sub = None
        if getattr(self, "logo_item", None) is not None:
            try: self.scene.removeItem(self.logo_item)
            except Exception: pass
            self.logo_item = None

    def _set_controls_from_design(self, d):
        if not d:
            return
        self._loading_card_design = True
        try:
            def _set_combo(cb, text):
                if text is None: return
                i = cb.findText(str(text))
                if i >= 0: cb.setCurrentIndex(i)
            self.chk_hardsub.setChecked(bool(d.get("hardsub_en", True)))
            _set_combo(self.cb_quality, d.get("render_quality"))
            _set_combo(self.cb_font, d.get("font_name"))
            self.spin_size.setValue(int(d.get("font_size", self.spin_size.value())))
            _set_combo(self.cb_color, d.get("font_color_name", "Trắng (White)"))
            self.chk_subbox.setChecked(bool(d.get("subbox_en", False)))
            _set_combo(self.cb_subbox_color, d.get("subbox_color_name", "Đen"))
            self.spn_subbox_opacity.setValue(int(d.get("subbox_opacity", 60)))
            self.chk_blur.setChecked(bool(d.get("bp_blur_en", False)))
            self.chk_frame.setChecked(bool(d.get("bp_frame_en", False)))
            self.frame_input.setText(d.get("frame_path", "") or "")
            self.chk_logo.setChecked(bool(d.get("bp_logo_en", False)))
            self.logo_input.setText(d.get("logo_path", "") or "")
            self.chk_flip.setChecked(bool(d.get("bp_flip", False)))
            self.chk_zoom.setChecked(bool(d.get("bp_zoom", False)))
            self.chk_color.setChecked(bool(d.get("bp_color", False)))
            self.chk_noise.setChecked(bool(d.get("bp_noise", False)))
            self.chk_speed.setChecked(bool(d.get("bp_speed", False)))
            self.chk_pitch.setChecked(bool(d.get("bp_pitch", False)))
            self.chk_rotate.setChecked(bool(d.get("bp_rotate", False)))
        finally:
            self._loading_card_design = False

    def _restore_design_from_card(self, card):
        if card is None or self.selected_card is not card:
            return
        d = copy.deepcopy(card.design_config) if getattr(card, "design_config", None) else self._default_design()
        self._set_controls_from_design(d)
        self._clear_scene_design_items()
        rect = self.scene.sceneRect()
        W = rect.width() or 1080
        H = rect.height() or 1920
        srcW = float(d.get("SW") or W or 1)
        srcH = float(d.get("SH") or H or 1)
        sx = W / srcW if srcW else 1.0
        sy = H / srcH if srcH else 1.0

        self._ensure_sample_sub()
        sp = d.get("sub_pos")
        if sp and self.sample_sub is not None:
            try:
                self.sample_sub.setPos(float(sp.get("item_x", sp.get("left", W*.08))) * sx,
                                       float(sp.get("item_y", max(0, sp.get("bottom", H*.85)-50))) * sy)
                self.sample_sub.setScale(float(sp.get("item_scale", 1.0)) * min(sx, sy))
            except Exception: pass

        for br in d.get("blur_list", []) or []:
            try:
                box = DraggableBlurBox(float(br["x"])*sx, float(br["y"])*sy,
                                       max(10, float(br["w"])*sx), max(10, float(br["h"])*sy))
                self.scene.addItem(box); self.blur_boxes.append(box)
            except Exception: pass

        self._update_logo_preview()
        lp = d.get("logo_pos")
        if lp and self.logo_item is not None:
            try:
                self.logo_item.setPos(float(lp.get("item_x", lp.get("x", W*.05))) * sx,
                                      float(lp.get("item_y", lp.get("y", H*.05))) * sy)
                self.logo_item.setScale(float(lp.get("scale", 1.0)) * min(sx, sy))
            except Exception: pass
        try: self.preview.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        except Exception: pass

    # ============ PREVIEW TRỰC TIẾP TRÊN CARD ============
    def _load_preview(self, video_path):
        try:
            self.media_player.setSource(QUrl.fromLocalFile(video_path))
            self.media_player.pause()
            if self.selected_card:
                self.selected_card.btn_card_play.setText("▶")
                self.selected_card.set_media_position(0, 0)
        except Exception as e:
            self._log(f"⚠️ Không mở được preview: {e}")

    def _toggle_play(self):
        from PyQt6.QtMultimedia import QMediaPlayer as _QMP
        card = self.selected_card
        if not card:
            return
        if self.media_player.playbackState() == _QMP.PlaybackState.PlayingState:
            self.media_player.pause()
            card.btn_card_play.setText("▶")
        else:
            self.media_player.play()
            card.btn_card_play.setText("⏸")

    def _on_pos(self, pos):
        dur = self.media_player.duration()
        if self.selected_card and hasattr(self.selected_card, "set_media_position"):
            self.selected_card.set_media_position(pos, dur)

    def _on_dur(self, dur):
        if self.selected_card and hasattr(self.selected_card, "set_media_position"):
            self.selected_card.set_media_position(self.media_player.position(), dur)
        if hasattr(self, "lbl_info_dur") and self.selected_card:
            self.lbl_info_dur.setText(self._fmt_ms(dur))

    def _on_native_size(self, size):
        if size.width() > 0 and size.height() > 0:
            self.video_item.setSize(size)
            self.scene.setSceneRect(0, 0, size.width(), size.height())
            self.preview.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            if hasattr(self, "lbl_info_res") and self.selected_card is not None:
                self.lbl_info_res.setText(f"{int(size.width())} × {int(size.height())}")
            if self.selected_card is not None:
                self._restore_design_from_card(self.selected_card)

    def _reset_pos(self):
        try:
            if not self.scene.sceneRect().isEmpty():
                self.preview.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        except Exception:
            pass

    # ============ KHUNG MỜ / FRAME / LOGO ============
    def _add_blur_box(self):
        # Lấy kích thước vùng làm việc: ưu tiên sceneRect, nếu chưa có (video
        # chưa load xong) thì lấy nativeSize của video, cuối cùng mặc định
        # 1080x1920 (dọc - hợp phim ngắn). Nhờ vậy ô che LUÔN hiện đủ to để
        # nhìn thấy và kéo, kể cả khi thêm trước lúc video sẵn sàng.
        rect = self.scene.sceneRect()
        W = rect.width(); H = rect.height()
        if W < 10 or H < 10:
            ns = self.video_item.nativeSize()
            if ns.width() > 10 and ns.height() > 10:
                W, H = ns.width(), ns.height()
            else:
                W, H = 1080, 1920
            # đảm bảo scene có kích thước để đặt item
            self.scene.setSceneRect(0, 0, W, H)
        w = max(120, W * 0.5); h = max(60, H * 0.10)
        # đặt ô che ở GIỮA màn hình cho dễ thấy
        x = (W - w) / 2; y = (H - h) / 2
        box = DraggableBlurBox(x, y, w, h)
        box.setSelected(True)          # chọn sẵn để thấy handle + kéo ngay
        self.scene.addItem(box); self.blur_boxes.append(box)
        # canh lại khung nhìn để chắc chắn ô nằm trong vùng thấy
        try:
            self.preview.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        except Exception:
            pass

    def _ensure_sample_sub(self):
        """Tạo (nếu chưa có) ô CHỮ MẪU sub tiếng Việt trên preview để canh vị
        trí. Chữ kéo được, phóng to được."""
        rect = self.scene.sceneRect()
        W = rect.width() or 1080; H = rect.height() or 1920
        if getattr(self, 'sample_sub', None) is None:
            self.sample_sub = ScalableTextItem("Chữ mẫu — kéo để đặt chữ")
            self.sample_sub.setZValue(5)
            self.scene.addItem(self.sample_sub)
            self.sample_sub.setPos(W * 0.08, H * 0.80)
        self._restyle_sample_sub()

    def _restyle_sample_sub(self):
        """Áp font/cỡ/màu đang chọn lên ô chữ mẫu."""
        if getattr(self, 'sample_sub', None) is None:
            return
        try:
            font = QFont(self.cb_font.currentText(), int(self.spin_size.value()))
            font.setBold(True)
            self.sample_sub.setFont(font)
            qt_color = COLOR_PRESETS.get(self.cb_color.currentText(), {}).get("qt", "#FFFFFF")
            self.sample_sub.setDefaultTextColor(QColor(qt_color))
        except Exception:
            pass
            
    def _update_logo_preview(self):
        """Khởi tạo và hiển thị ảnh Logo lên màn hình Preview"""
        path = self.logo_input.text().strip()
        if not self.chk_logo.isChecked() or not os.path.exists(path):
            if getattr(self, 'logo_item', None):
                self.scene.removeItem(self.logo_item)
                self.logo_item = None
            return

        if getattr(self, 'logo_item', None) is None:
            self.logo_item = ScalablePixmapItem()
            self.logo_item.setZValue(6) # Đặt lớp trên cùng để dễ kéo
            self.scene.addItem(self.logo_item)
            
            # Căn góc logo lúc mới hiện
            scene_rect = self.scene.sceneRect()
            W = scene_rect.width() or 1080
            H = scene_rect.height() or 1920
            self.logo_item.setPos(W * 0.05, H * 0.05)

        pixmap = QPixmap(path)
        if not pixmap.isNull():
            self.logo_item.setPixmap(pixmap)

    def _clear_blur_boxes(self):
        for b in self.blur_boxes:
            try: self.scene.removeItem(b)
            except Exception: pass
        self.blur_boxes = []

    def _select_frame(self):
        fp, _ = QFileDialog.getOpenFileName(self, "Chọn ảnh Overlay PNG", "", "Ảnh (*.png)")
        if fp:
            self.frame_input.setText(fp)

    def _select_logo(self):
        fp, _ = QFileDialog.getOpenFileName(self, "Chọn Logo", "", "Ảnh (*.png *.jpg *.jpeg)")
        if fp:
            self.logo_input.setText(fp)

    # ============ THU THẬP CONFIG DESIGN ============
    def _collect_design(self, persist=True):
        color_name = self.cb_color.currentText()
        color_ass = COLOR_PRESETS.get(color_name, {}).get("ass", "&H00FFFFFF")
        blur_list = []
        for b in self.blur_boxes:
            r = b.sceneBoundingRect()
            blur_list.append({"x": int(r.x()), "y": int(r.y()), "w": int(r.width()), "h": int(r.height())})
            
        scene = self.scene.sceneRect()
        SW = scene.width() or 1080; SH = scene.height() or 1920

        # Lấy thông số tọa độ + scale của Logo
        logo_pos = None
        if getattr(self, 'logo_item', None) is not None and self.chk_logo.isChecked():
            lr = self.logo_item.sceneBoundingRect()
            logo_pos = {
                "x": lr.x(), "y": lr.y(), "scale": self.logo_item.scale(),
                "item_x": self.logo_item.pos().x(), "item_y": self.logo_item.pos().y()
            }

        # Đọc VỊ TRÍ + CỠ chữ mẫu (nếu người dùng đã kéo canh) để render sub
        # đúng chỗ + đúng cỡ. Quy ước theo hệ toạ độ scene = kích thước video.
        sub_pos = None
        try:
            if getattr(self, 'sample_sub', None) is not None:
                r = self.sample_sub.sceneBoundingRect()   # đã tính cả scale
                # Lấy trực tiếp chiều cao pixel của ô chữ trên màn hình làm chuẩn.
                # Nhân 0.75 để bù trừ khoảng trắng (padding/line-height) mặc định của Qt
                eff_size = int(r.height() * 0.75)
                sub_pos = {
                    "cx": r.center().x(), "cy": r.center().y(),
                    "left": r.left(), "bottom": r.bottom(),
                    "SW": SW, "SH": SH, "eff_size": eff_size,
                    "item_x": self.sample_sub.pos().x(),
                    "item_y": self.sample_sub.pos().y(),
                    "item_scale": self.sample_sub.scale(),
                }
        except Exception:
            sub_pos = None

        # Nền ô chữ -> mã màu ASS &HAABBGGRR (AA=alpha: 00 đặc, FF trong).
        subbox_en = self.chk_subbox.isChecked()
        _box_bgr = {"Đen": "000000", "Xám đậm": "202020", "Xanh đen": "301500", "Trắng": "FFFFFF"}
        bgr = _box_bgr.get(self.cb_subbox_color.currentText(), "000000")
        opac = int(self.spn_subbox_opacity.value())
        alpha = int(round((100 - opac) * 255 / 100))
        subbox_color = f"&H{alpha:02X}{bgr}"

        # QSettings chỉ lưu mặc định cho lần mở app sau; không phải state chung của mọi card.
        if persist:
            self.settings.setValue("font_name", self.cb_font.currentText())
            self.settings.setValue("font_size", self.spin_size.value())
            self.settings.setValue("font_color_name", color_name)
            self.settings.setValue("render_quality", self.cb_quality.currentText())
            self.settings.setValue("hardsub_en", self.chk_hardsub.isChecked())
            self.settings.setValue("subbox_en", subbox_en)
            self.settings.setValue("subbox_color_name", self.cb_subbox_color.currentText())
            self.settings.setValue("subbox_opacity", opac)
            self.settings.setValue("bp_logo_en", self.chk_logo.isChecked())
            self.settings.setValue("logo_path", self.logo_input.text().strip())
            # Overlay PNG cố ý KHÔNG lưu vào QSettings.
            # Nó chỉ thuộc design_config của card trong phiên hiện tại.
            self.settings.setValue("bp_blur_en", self.chk_blur.isChecked())
            for k, chk in (("bp_flip", self.chk_flip), ("bp_zoom", self.chk_zoom), ("bp_color", self.chk_color),
                           ("bp_noise", self.chk_noise), ("bp_speed", self.chk_speed), ("bp_pitch", self.chk_pitch),
                           ("bp_rotate", self.chk_rotate)):
                self.settings.setValue(k, chk.isChecked())
        return {
            "hardsub_en": self.chk_hardsub.isChecked(),
            "render_quality": self.cb_quality.currentText(),
            "font_name": self.cb_font.currentText(),
            "font_size": self.spin_size.value(),
            "font_color": color_ass,
            "font_color_name": color_name,
            "sub_pos": sub_pos,
            "logo_pos": logo_pos,
            "SW": SW, "SH": SH,
            "subbox_en": subbox_en, "subbox_color": subbox_color,
            "subbox_color_name": self.cb_subbox_color.currentText(),
            "subbox_opacity": opac,
            "bp_blur_en": self.chk_blur.isChecked(), "blur_list": blur_list,
            "bp_frame_en": self.chk_frame.isChecked(), "frame_path": self.frame_input.text().strip(),
            "bp_logo_en": self.chk_logo.isChecked(), "logo_path": self.logo_input.text().strip(),
            "bp_flip": self.chk_flip.isChecked(), "bp_zoom": self.chk_zoom.isChecked(),
            "bp_color": self.chk_color.isChecked(), "bp_noise": self.chk_noise.isChecked(),
            "bp_speed": self.chk_speed.isChecked(), "bp_pitch": self.chk_pitch.isChecked(),
            "bp_rotate": self.chk_rotate.isChecked(),
        }

    def _build_cfg(self, video_path, design):
        """Dò kích thước video rồi dựng cfg cho SingleRenderThread."""
        W, H = 1920, 1080
        try:
            probe = subprocess.run([get_ffmpeg_path(), "-i", video_path], stderr=subprocess.PIPE,
                                   text=True, errors="ignore",
                                   creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
            m = re.search(r"Video:.*?,.*? (\d+)x(\d+)", probe.stderr)
            if m:
                W, H = int(m.group(1)), int(m.group(2))
        except Exception:
            pass
        if design.get("bp_zoom"):
            W *= 0.96; H *= 0.96
            
        SW = design.get("SW", W); SH = design.get("SH", H)
        sy = H / SH; sx = W / SW

        # Vị trí + cỡ chữ: nếu người dùng đã kéo canh chữ mẫu (sub_pos) thì
        # dùng đúng vị trí/cỡ đó; nếu không thì mặc định giữa-dưới, cách đáy 8%.
        eff_font = int(design.get("font_size", 24))
        margin_v = int(H * 0.08)
        sp = design.get("sub_pos")
        if sp:
            try:
                # libass Alignment=2 neo ĐÁY dòng chữ cách đáy màn hình = MarginV.
                # Dùng đáy chữ (bottom) trên scene, quy về pixel video.
                margin_v = int(max(0, (SH - sp["bottom"]) * sy))
                eff_font = int(max(8, sp["eff_size"] * sy))
                self._log(
                    f"   🔧 [canh sub] scene={int(SW)}x{int(SH)} video={int(W)}x{int(H)} "
                    f"| đáy_chữ={int(sp['bottom'])} | margin_v={margin_v} "
                    f"| cỡ_gốc={design.get('font_size')} scale~{sp['eff_size']/max(1,design.get('font_size',24)):.2f} eff_font={eff_font}\n"
                )
            except Exception as _e:
                self._log(f"   ⚠️ [canh sub] lỗi tính vị trí: {_e}\n")
                
        # Quy đổi vị trí + Cỡ Logo từ Preview sang chuẩn FFmpeg
        lx, ly, lscale = 20, 20, 1.0
        lp = design.get("logo_pos")
        if lp:
            lx = int(max(0, lp["x"] * sx))
            ly = int(max(0, lp["y"] * sy))
            lscale = lp["scale"] * sx
            if lscale <= 0.01:
                # An toàn: nếu vì lý do gì đó (scene chưa kịp load kích thước
                # khi tick Logo, v.v.) hệ số scale tính ra ~0, logo sẽ bị co
                # về kích thước gần như vô hình dù overlay vẫn "chạy thành
                # công" không báo lỗi gì. Ép về 1.0 để logo luôn thấy được.
                self._log(f"   ⚠️ [logo] scale tính ra bất thường ({lscale:.4f}) -> dùng scale=1.0 để logo không bị vô hình.\n")
                lscale = 1.0
            # Đảm bảo logo không bị đẩy hẳn ra ngoài khung hình do toạ độ
            # scene không khớp với kích thước video thật.
            lx = min(lx, max(0, W - 10))
            ly = min(ly, max(0, H - 10))

        return {
            "scene_w": int(W), "scene_h": int(H),
            "blur_en": design["bp_blur_en"], "blur_list": design["blur_list"],
            "frame_en": design["bp_frame_en"], "frame_path": design["frame_path"],
            "logo_en": design["bp_logo_en"], "logo_path": design["logo_path"],
            "logo_x": lx, "logo_y": ly, "logo_scale": lscale,
            "flip": design["bp_flip"], "zoom": design["bp_zoom"], "color": design["bp_color"],
            "noise": design["bp_noise"], "speed": design["bp_speed"], "pitch": design["bp_pitch"],
            "rotate_en": design["bp_rotate"],
            "font_name": design["font_name"], "font_size": eff_font,
            "font_color": design["font_color"],
            "subbox_en": design.get("subbox_en", False),
            "subbox_color": design.get("subbox_color", "&H80000000"),
            "margin_l": 0, "margin_r": 0, "margin_v": margin_v,
            "hardsub_en": design["hardsub_en"],
            "render_quality": design["render_quality"],
            "target_fps": self._get_target_fps(),
            "target_res": getattr(self, "_target_res", None),
            # KHÔNG bật tts_en -> SingleRenderThread bỏ qua phần audio TTS
        }

    def _get_target_fps(self):
        """Trả về fps đích (float) do người dùng chọn, hoặc None nếu 'Giữ gốc'."""
        try:
            t = self.cmb_fps.currentText() if hasattr(self, "cmb_fps") else "25"
            if t and t != "Giữ gốc":
                return float(t)
        except Exception:
            pass
        return None

    def _get_target_res(self):
        """TỰ ĐỘNG lấy resolution 'số đông' của các tập trong hàng đợi để ép mọi
        tập về chung 1 size (gộp nhanh, hết re-encode). Quét header song song
        cho nhanh. Trả về (w, h), hoặc None nếu không đọc được HOẶC mọi tập đã
        đồng nhất sẵn (khỏi cần ép)."""
        try:
            from collections import Counter
            from concurrent.futures import ThreadPoolExecutor
            ffmpeg = get_ffmpeg_path()
            _scope_cards = self._selected_cards() if hasattr(self, "_selected_cards") else list(self.cards)
            if not _scope_cards:
                _scope_cards = list(self.cards)
            vids = [getattr(c, "video_path", None) for c in _scope_cards]
            vids = [v for v in vids if v and os.path.exists(v)]
            if not vids:
                return None

            def _probe(vp):
                try:
                    r = subprocess.run([ffmpeg, "-i", vp], stderr=subprocess.PIPE,
                                       text=True, errors="ignore",
                                       creationflags=CREATE_NO_WINDOW if os.name == 'nt' else 0)
                    m = re.search(r"Stream.*Video.*?(\d{3,5})x(\d{3,5})", r.stderr)
                    if m:
                        return (int(m.group(1)), int(m.group(2)))
                except Exception:
                    pass
                return None

            counts = Counter()
            with ThreadPoolExecutor(max_workers=8) as ex:  # quét song song, chỉ đọc header
                for res in ex.map(_probe, vids):
                    if res:
                        counts[res] += 1
            if not counts:
                return None
            if len(counts) == 1:   # mọi tập đã cùng size -> khỏi ép
                return None
            return counts.most_common(1)[0][0]
        except Exception:
            return None

    # ============ RENDER HÀNG LOẠT ============
    def _sync_design_all(self):
        """Chỉ nút này mới sao chép thiết kế video hiện tại sang toàn bộ video."""
        if not self.cards:
            QMessageBox.information(self, "Chưa có file", "Hãy thêm video vào hàng đợi trước.")
            return
        if self.selected_card is None:
            QMessageBox.information(self, "Chưa chọn video", "Hãy chọn 1 video làm mẫu trước.")
            return
        self._save_design_to_card(self.selected_card)
        master = copy.deepcopy(self.selected_card.design_config or self._default_design())
        for c in self.cards:
            c.design_config = copy.deepcopy(master)
        self._design_locked = None
        n_blur = len(master.get("blur_list", []))
        self._log(f"🔄 Đã đồng bộ từ {os.path.basename(self.selected_card.video_path)} sang {len(self.cards)} tập ({n_blur} vùng mờ).")
        QMessageBox.information(self, "Đã đồng bộ",
            f"Đã sao chép canh chỉnh của video đang chọn sang tất cả {len(self.cards)} video.\n"
            "Sau đó bạn vẫn có thể chọn từng video để chỉnh riêng tiếp.")

    def _clean_junk_files(self):
        """Dọn sạch file trung gian sau khi render, CHỈ GIỮ bản render cuối
        (*_final.mp4) và bản gộp trọn bộ (*_TronBo_Rendered.mp4).
        Xóa: video gốc, *_dubbed.mp4, .srt, _vi.srt, .txt, .vocals_cache.wav.
        Xóa luôn, không hỏi (theo yêu cầu)."""
        if self._render_running:
            QMessageBox.information(self, "Đang render", "Đang render, dọn rác sau khi xong.")
            return

        ans = QMessageBox.question(
            self, "Dọn file trung gian",
            "Thao tác này có thể xóa video gốc, file lồng tiếng, SRT và file trung gian.\n"
            "BOOM Studio sẽ chỉ giữ bản *_final.mp4 và bản gộp.\n\nBạn có chắc muốn tiếp tục?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return

        # Gom các thư mục chứa video trong hàng đợi (kể cả khi card đã bị xóa,
        # dùng thư mục render gần nhất).
        dirs = set()
        for c in getattr(self, "cards", []) or []:
            vp = getattr(c, "video_path", None)
            if vp:
                dirs.add(os.path.dirname(vp))
        last = getattr(self, "_last_render_dir", None)
        if last:
            dirs.add(last)

        if not dirs:
            QMessageBox.information(self, "Chưa có gì để dọn",
                                    "Chưa có thư mục nào (thêm video hoặc render trước đã).")
            return

        KEEP_SUFFIX = ("_final.mp4", "_tronbo_rendered.mp4")
        # Đuôi/hậu tố được coi là rác
        JUNK_EXT = (".srt", ".txt", ".vocals_cache.wav", ".ass")
        VIDEO_EXT = (".mp4", ".mkv", ".mov", ".avi", ".ts", ".webm")

        removed = 0
        errors = 0
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                low = name.lower()
                # Giữ lại bản render cuối và bản gộp trọn bộ
                if low.endswith(KEEP_SUFFIX):
                    continue
                path = os.path.join(d, name)
                if not os.path.isfile(path):
                    continue

                is_junk = False
                if low.endswith(JUNK_EXT):
                    is_junk = True
                elif low.endswith(".vocals_cache.wav"):
                    is_junk = True
                elif low.endswith(VIDEO_EXT):
                    # Video: xóa video gốc và *_dubbed.mp4 (không phải _final)
                    is_junk = True
                # (các đuôi khác như ảnh thumbnail, png overlay... KHÔNG đụng tới)

                if is_junk:
                    try:
                        os.remove(path)
                        removed += 1
                    except Exception:
                        errors += 1

        self._log(f"🧹 Đã dọn {removed} file rác (giữ *_final.mp4 & bản gộp).")
        if errors:
            self._log(f"⚠️ {errors} file không xóa được (đang mở?).")
        # Xóa các card khỏi hàng đợi vì video gốc đã bị xóa
        try:
            self._clear_all()
        except Exception:
            pass
        QMessageBox.information(self, "Dọn rác xong",
                                f"Đã xóa {removed} file rác.\nChỉ còn lại bản render cuối (*_final.mp4)"
                                + (" và bản gộp trọn bộ." if True else "."))

    def _run_full_pipeline_external(self):
        """Nút 'LÀM TẤT CẢ' ngoài: chuyển việc cho tab con Sub→Dịch→Lồng chạy
        trọn quy trình (dùng cấu hình đã set trong tab đó)."""
        tab = getattr(self, "dub_feature_tab", None)
        if tab is None or not hasattr(tab, "_run_full_pipeline"):
            QMessageBox.warning(self, "Thiếu tính năng",
                                "Không tìm thấy tab 'Sub → Dịch → Lồng'.\n"
                                "Hãy đảm bảo render_dub_feature.py và honggou_tab.py nằm cạnh app.")
            return
        if not self.cards:
            QMessageBox.information(self, "Chưa có video", "Hãy thêm video vào hàng đợi trước.")
            return
        # Chuyển sang đúng PAGE chứa pipeline để người dùng thấy giao diện/log chạy.
        # dub_feature_tab có thể là widget con nằm trong QScrollArea, vì vậy không
        # dùng setCurrentWidget(tab) trực tiếp nữa.
        self._focus_pipeline_stage(None)
        tab._run_full_pipeline()

    def _start_render_all(self):
        if self._render_running:
            QMessageBox.information(self, "Đang render", "Đang render, vui lòng đợi xong.")
            return
        if not self.cards:
            QMessageBox.information(self, "Chưa có video", "Hãy thêm video vào hàng đợi trước.")
            return
        selected_cards = self._selected_cards()
        if not selected_cards:
            QMessageBox.information(self, "Chưa chọn video", "Hãy tick ít nhất 1 video trong grid để Xuất.")
            return
        # Chốt riêng card đang mở trước khi render.
        if self.selected_card is not None:
            self._save_design_to_card(self.selected_card)
        # Chỉ ép resolution đồng nhất KHI có tích 'Gộp trọn bộ sau Render' — vì
        # chỉ lúc gộp mới cần các tập cùng size. Không gộp thì giữ size gốc,
        # khỏi quét (đỡ khựng nút RENDER).
        self._target_res = None
        if hasattr(self, "chk_merge_all") and self.chk_merge_all.isChecked():
            self._target_res = self._get_target_res()
            if self._target_res:
                self._log(f"📐 Có gộp trọn bộ → ép mọi tập về {self._target_res[0]}×{self._target_res[1]} cho đồng nhất.")
        # Sắp lại theo SỐ tập để gộp trọn bộ đúng thứ tự 1 -> cuối
        # (phòng khi thêm file thủ công bằng '+ File' không theo thứ tự).
        self.cards.sort(key=lambda c: _natural_key(os.path.basename(c.video_path)))
        self._relayout_grid()
        selected_cards = [c for c in self.cards if c in selected_cards]
        self._render_queue = list(selected_cards)
        self._render_total = len(self._render_queue)
        self._render_done_count = 0
        self._rendered_files = [] # Lưu danh sách file xuất ra để gộp
        self._render_failed_files = []  # không tự gộp thiếu tập nếu có render lỗi
        self._render_running = True
        self._stopping = False
        self._render_threads = {}         # out_path -> SingleRenderThread đang chạy
        self._render_pct = {}             # out_path -> % hiện tại (cho song song)
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        if hasattr(self, 'btn_merge_now'):
            self.btn_merge_now.setEnabled(False)
        self.chk_merge_all.setEnabled(False)
        if not getattr(self, "_total_active", False):
            self._big_set_percent(0)
            self._big_set_count(0, self._render_total)
        else:
            if hasattr(self, "lbl_big_prog"):
                self.lbl_big_prog.setText(
                    f"Tiến độ tổng · Render {self._render_total} tập · "
                    f"{self._done_units}/{self._total_units} việc")
        _par = self.spn_render_parallel.value() if hasattr(self, "spn_render_parallel") else 1
        self._log(f"🚀 Bắt đầu render {len(self._render_queue)} tập ({_par} tập/lượt)...")
        self._pump_render_queue()

    def _big_set_percent(self, pct):
        if hasattr(self, "big_render_prog"):
            self.big_render_prog.setValue(max(0, min(100, int(pct))))
        # đồng bộ luôn thanh nhỏ cũ
        if hasattr(self, "step_render"):
            self.step_render.set_percent(pct)

    # ═══════════ THANH TỔNG (Tách→Dịch→Lồng→Render) ═══════════
    def total_progress_begin(self, n_files, n_steps):
        """Bắt đầu 1 phiên 'Làm tất cả'. n_steps = số bước mỗi tập phải qua
        (VD: tách+dịch+lồng+render = 4; nếu bỏ dịch/lồng thì ít hơn)."""
        self._total_units = max(1, int(n_files) * max(1, int(n_steps)))
        self._done_units = 0
        self._total_active = True
        self._total_nfiles = int(n_files)
        self._total_nsteps = int(n_steps)
        self._big_render_pct_within = 0
        self._paint_total(0)
        if hasattr(self, "lbl_big_prog"):
            self.lbl_big_prog.setText(f"Tiến độ tổng · 0/{self._total_units} việc")

    def total_progress_step(self, done_delta=1, stage=""):
        """+done_delta 'việc' đã xong (1 tập xong 1 bước). Cập nhật thanh tổng."""
        if not self._total_active:
            return
        self._done_units = min(self._total_units, self._done_units + int(done_delta))
        self._paint_total()
        # Pipeline vừa báo xong 1 việc -> cập nhật bộ lọc ngay. Đây là fallback
        # event-driven thứ hai bên cạnh QFileSystemWatcher, không phải polling.
        self._schedule_realtime_refresh(20)
        if hasattr(self, "lbl_big_prog"):
            tag = f" · {stage}" if stage else ""
            self.lbl_big_prog.setText(
                f"Tiến độ tổng{tag} · {self._done_units}/{self._total_units} việc")

    def total_progress_render_within(self, pct_of_one_episode):
        """Trong lúc RENDER 1 tập, cộng % mượt của tập đó vào thanh tổng
        (mỗi tập render = 1 'việc', nên đóng góp = pct/100 của 1 unit)."""
        if not self._total_active:
            return
        frac = max(0.0, min(1.0, pct_of_one_episode / 100.0))
        base = self._done_units
        self._paint_total(base + frac)

    def total_progress_end(self):
        self._total_active = False

    def _paint_total(self, done_units_float=None):
        if done_units_float is None:
            done_units_float = self._done_units
        pct = int(done_units_float / max(1, self._total_units) * 100)
        pct = max(0, min(100, pct))
        if hasattr(self, "big_render_prog"):
            self.big_render_prog.setValue(pct)

    def _big_set_count(self, done, total):
        self._big_done = done; self._big_total = total
        if hasattr(self, "lbl_big_prog"):
            if total:
                self.lbl_big_prog.setText(f"Tiến độ render · Tập {done}/{total}")
            else:
                self.lbl_big_prog.setText("Tiến độ render")

    def _on_render_percent(self, pct):
        """% thật của tập đang render."""
        if getattr(self, "_total_active", False):
            # Đang trong quy trình tổng → cộng mượt vào thanh tổng
            self.total_progress_render_within(pct)
            if hasattr(self, "step_render"):
                self.step_render.set_percent(pct)
            return
        if hasattr(self, "big_render_prog"):
            self.big_render_prog.setValue(max(0, min(100, int(pct))))
        if hasattr(self, "step_render"):
            self.step_render.set_percent(pct)

    def _start_merge_now(self):
        if self._render_running or (hasattr(self, 'merge_thread') and self.merge_thread.isRunning()):
            QMessageBox.information(self, "Đang bận", "Hệ thống đang xử lý, vui lòng đợi xong.")
            return
        
        if not self.cards:
            QMessageBox.information(self, "Chưa có video", "Hãy thêm video vào hàng đợi trước.")
            return

        selected_cards = self._selected_cards()
        if not selected_cards:
            QMessageBox.information(self, "Chưa chọn video", "Hãy tick ít nhất 2 video muốn gộp.")
            return

        files_to_merge = []
        for c in selected_cards:
            vp = getattr(c, "video_path", None)
            if vp and os.path.exists(vp) and vp not in files_to_merge:
                files_to_merge.append(vp)
                
        if len(files_to_merge) < 2:
            QMessageBox.information(self, "Chưa đủ file", "Cần ít nhất 2 file video để gộp.")
            return

        # Sắp xếp đúng theo thứ tự tự nhiên (Tập 1, 2, ... 10)
        files_to_merge.sort(key=lambda x: _natural_key(os.path.basename(x)))

        reply = QMessageBox.question(
            self, "Xác nhận Gộp", 
            f"Bạn có chắc muốn gộp nhanh {len(files_to_merge)} video này thành 1 file không?\n\n"
            "(Lưu ý: Quá trình sẽ bỏ qua render ghép sub/màu. Thứ tự đã được tự động sắp xếp từ 1 đến hết)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._start_merge(files_to_merge)

    def _stop_render(self):
        if not self._render_running:
            return
        self._stopping = True
        self._render_queue = []          # xóa các tập chưa render
        self.btn_stop.setEnabled(False)
        self._log("⛔ Đang dừng render... (đợi các tập đang chạy thoát)")
        for th in list(getattr(self, "_render_threads", {}).values()):
            try:
                if th and th.isRunning():
                    th.cancel()
            except Exception:
                pass

    def _pump_render_queue(self):
        """Khởi động thêm tập cho tới khi đủ số song song hoặc hết hàng đợi."""
        if self._stopping:
            if not self._render_threads:
                self._finish_render_all()
            return
        par = self.spn_render_parallel.value() if hasattr(self, "spn_render_parallel") else 1
        while self._render_queue and len(self._render_threads) < par:
            card = self._render_queue.pop(0)
            self._start_one_render(card)
        # Hết hàng đợi và không còn thread nào chạy -> xong
        if not self._render_queue and not self._render_threads:
            self._finish_render_all()

    def _start_one_render(self, card):
        card.set_status("đang render")
        vp = card.video_path
        sp = card.srt_path
        out_dir = os.path.dirname(vp)
        self._last_render_dir = out_dir
        stem = os.path.splitext(os.path.basename(vp))[0]
        if stem.endswith("_dubbed"):
            stem = stem[:-len("_dubbed")]
        out_path = os.path.join(out_dir, f"{stem}_final.mp4")
        design = copy.deepcopy(getattr(card, "design_config", None) or self._default_design())
        cfg = self._build_cfg(vp, design)

        done_so_far = getattr(self, "_render_done_count", 0)
        total = getattr(self, "_render_total", 0)
        running = len(self._render_threads) + 1
        self._log(f"🎬 Render [{done_so_far + running}/{total}]: {os.path.basename(vp)}...")
        self.step_render.set_status("processing", 0)
        self.step_render.set_count(done_so_far, total)

        th = SingleRenderThread(vp, sp, None, out_path, cfg)
        th.log.connect(self._log)
        th.progress.connect(lambda pct, op=out_path: self._on_render_percent_multi(op, pct))
        th.done.connect(lambda ok, c=card, op=out_path: self._on_one_done(ok, c, op))
        self._render_threads[out_path] = th
        self._render_pct[out_path] = 0
        if not hasattr(self, "_render_card_map"):
            self._render_card_map = {}
        self._render_card_map[out_path] = card

        # GIỮ QThread SỐNG cho tới khi QThread.finished phát ra thật sự.
        # SingleRenderThread.done được emit trước khi run() thoát hoàn toàn
        # (sau đó vẫn còn khối finally dọn file tạm). Nếu _on_one_done() pop
        # reference cuối cùng quá sớm, PyQt có thể huỷ QThread khi nó vẫn chạy
        # và làm crash toàn app với lỗi:
        #   QThread: Destroyed while thread is still running
        if not hasattr(self, "_render_threads_alive"):
            self._render_threads_alive = []
        self._render_threads_alive.append(th)

        def _release_render_thread(t=th):
            try:
                if t in self._render_threads_alive:
                    self._render_threads_alive.remove(t)
            except Exception:
                pass

        th.finished.connect(_release_render_thread)
        th.start()

    def _on_render_percent_multi(self, out_path, pct):
        """% của 1 tập trong nhóm song song. Thanh hiển thị = trung bình các tập
        đang chạy (đủ mượt & phản ánh cả nhóm)."""
        self._render_pct[out_path] = pct
        card = getattr(self, "_render_card_map", {}).get(out_path)
        if card is not None and hasattr(card, "set_play_progress"):
            card.set_play_progress(pct)
        vals = list(self._render_pct.values())
        avg = sum(vals) / len(vals) if vals else 0
        if getattr(self, "_total_active", False):
            # Trong quy trình tổng: mỗi tập render = 1 việc, cộng phần đang chạy
            running_frac = sum(v / 100.0 for v in vals)
            self._paint_total(self._done_units + running_frac)
            if hasattr(self, "step_render"):
                self.step_render.set_percent(avg)
        else:
            self._big_set_percent(avg)

    def _finish_render_all(self):
        stopped = self._stopping
        self._render_running = False
        self._stopping = False
        self._render_queue = []
        self._render_threads = {}
        self._render_pct = {}
        self._render_card_map = {}

        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        if hasattr(self, 'btn_merge_now'):
            self.btn_merge_now.setEnabled(True)
        self.chk_merge_all.setEnabled(True)

        if stopped:
            self.step_render.set_status("error", 0)
            self.lbl_big_prog.setText("Tiến độ tổng · ĐÃ DỪNG" if getattr(self, "_total_active", False) else "Tiến độ render · ĐÃ DỪNG")
            self.total_progress_end()
            self._log("⛔ Đã dừng render.")
            QMessageBox.information(self, "Đã dừng", "Đã dừng render theo yêu cầu.")
        else:
            failed = list(getattr(self, "_render_failed_files", []) or [])
            if failed:
                self.step_render.set_status("error", 100)
                if getattr(self, "_total_active", False):
                    self.total_progress_end()
                self._log(f"⚠️ Có {len(failed)} tập render lỗi → KHÔNG tự gộp để tránh xuất trọn bộ bị thiếu tập.")
                names = "\n".join("• " + os.path.basename(p) for p in failed[:10])
                more = f"\n... và {len(failed)-10} tập khác" if len(failed) > 10 else ""
                QMessageBox.warning(
                    self, "Render chưa đủ tập",
                    f"Có {len(failed)} tập render lỗi nên BOOM Studio không tự gộp trọn bộ.\n\n{names}{more}\n\nHãy render lại các tập lỗi rồi gộp."
                )
                return
            if self.chk_merge_all.isChecked() and len(self._rendered_files) > 1:
                self._start_merge(self._rendered_files)
            else:
                self.step_render.set_status("success", 100)
                if getattr(self, "_total_active", False):
                    self._done_units = self._total_units
                    self._paint_total(self._total_units)
                    self.lbl_big_prog.setText(
                        f"Tiến độ tổng · XONG {self._total_units}/{self._total_units} việc ✅")
                    self.total_progress_end()
                else:
                    self._big_set_percent(100)
                    total = getattr(self, "_render_total", 0)
                    self.lbl_big_prog.setText(f"Tiến độ render · XONG {total}/{total} tập ✅")
                self._log("🎉 Đã render xong tất cả!")
                QMessageBox.information(self, "Xong", "Đã render xong tất cả các tập!")

    def _on_one_done(self, ok, card, out_path):
        # Gỡ thread vừa xong khỏi nhóm đang chạy
        self._render_threads.pop(out_path, None)
        self._render_pct.pop(out_path, None)
        if self._stopping and not ok:
            card.set_status("đã dừng")
        else:
            card.set_status("xong" if ok else "lỗi")
            if ok:
                self._rendered_files.append(out_path)
            else:
                if not hasattr(self, "_render_failed_files"):
                    self._render_failed_files = []
                self._render_failed_files.append(getattr(card, "video_path", out_path))
        self._render_done_count = getattr(self, "_render_done_count", 0) + 1
        self.step_render.set_count(self._render_done_count, getattr(self, "_render_total", 0))
        self._schedule_realtime_refresh(10)
        if getattr(self, "_total_active", False):
            self.total_progress_step(1, stage="Render")
        else:
            self._big_set_count(self._render_done_count, getattr(self, "_render_total", 0))
        # Khởi động tập kế cho đủ số song song (hoặc kết thúc nếu hết)
        self._pump_render_queue()
        
    def _start_merge(self, file_list):
        file_list = [p for p in (file_list or []) if p and os.path.exists(p)]
        if len(file_list) < 2:
            self._log("⚠️ Không đủ ít nhất 2 file hợp lệ để gộp.")
            QMessageBox.warning(self, "Không đủ file", "Cần ít nhất 2 file video hợp lệ để gộp.")
            return
        # Render song song -> file xong KHÔNG theo thứ tự. Sắp lại theo số tập
        # để gộp trọn bộ đúng 1 -> cuối.
        file_list = sorted(file_list, key=lambda p: _natural_key(os.path.basename(p)))
        self._log(f"🔗 Đang chuẩn bị gộp {len(file_list)} file thành 1...")
        self.btn_run.setEnabled(False)
        self.btn_run.setText("⏳ ĐANG GỘP FILE...")
        if hasattr(self, 'btn_merge_now'):
            self.btn_merge_now.setEnabled(False)
            
        first_file = file_list[0]
        out_dir = os.path.dirname(first_file)
        dir_name = os.path.basename(out_dir)
        if not dir_name: dir_name = "TronBo"
        out_path = os.path.join(out_dir, f"{dir_name}_TronBo_Rendered.mp4")
        
        if os.path.exists(out_path):
            try: os.remove(out_path)
            except: pass
            
        intro_img = None
        if self.chk_intro.isChecked() and self.intro_input.text().strip():
            if os.path.exists(self.intro_input.text().strip()):
                intro_img = self.intro_input.text().strip()
            else:
                self._log("⚠️ Không tìm thấy ảnh bìa, bỏ qua nhúng.\n")
                
        self.merge_thread = MergeRenderedThread(file_list, out_path, intro_img)
        self.merge_thread.log.connect(self._log)
        self.merge_thread.done.connect(self._on_merge_done)
        self.merge_thread.start()

    def _on_merge_done(self, ok, final_path):
        self.btn_run.setEnabled(True)
        if hasattr(self, 'btn_merge_now'):
            self.btn_merge_now.setEnabled(True)
        self._update_run_label()
        
        if ok:
            self.step_render.set_status("success", 100)
            self._log(f"🎉 Đã hoàn tất gộp trọn bộ: {os.path.basename(final_path)}")
            QMessageBox.information(self, "Hoàn tất Trọn bộ", f"Đã render và gộp thành công!\\nFile được lưu tại:\\n{final_path}")
        else:
            self.step_render.set_status("error", 100)
            QMessageBox.warning(self, "Lỗi gộp file", "Quá trình gộp file gặp sự cố. Bạn có thể kiểm tra log hoặc gộp thủ công.")

# (MergeRenderedThread đã được định nghĩa đầy đủ ở trên, không lặp lại tại đây)
