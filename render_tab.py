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
import os, sys, subprocess, re, shutil, time, tempfile, base64, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea, QFileDialog, QTextEdit, QProgressBar,
    QComboBox, QLineEdit, QSpinBox, QMessageBox, QCheckBox, QSlider,
    QTabWidget, QDoubleSpinBox, QGridLayout, QPlainTextEdit,
    QGraphicsScene, QGraphicsView, QGraphicsTextItem, QGraphicsRectItem,
    QGraphicsPixmapItem, QGraphicsItem, QStyle, QApplication
)
from PyQt6.QtGui import QIcon
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSettings, QUrl, QPointF, QRectF, QTimer, QSize
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
    "TIÊU ĐỀ (nếu hợp): tự tạo 1 câu 2–7 từ, mạnh, ngắn, gây tò mò, liên quan trực "
    "tiếp nội dung, IN HOA dễ đọc (kiểu 'HẮN ĐÃ TRỞ LẠI!', 'KẺ PHẢN BỘI LỘ DIỆN', "
    "'CÔ ẤY ĐÃ TRẢ THÙ'). KHÔNG lấy nguyên câu thoại dài. Nếu hình đã đủ mạnh thì "
    "không cần thêm chữ, tránh quá nhiều chữ. KHÔNG để chữ che mặt.\n\n"
    "HIỆU ỨNG chỉ dùng khi hợp nội dung (khói/lửa/tia sáng/sấm sét/bụi/mưa/năng "
    "lượng/neon/lens flare/cinematic glow/depth of field), KHÔNG lạm dụng. Màu theo "
    "thể loại (hành động: tương phản mạnh; giang hồ: tối lạnh; thần bài: vàng/đỏ/"
    "neon; tình cảm: sáng mềm; fantasy: huyền ảo; trả thù: tối tương phản cao).\n\n"
    "MỤC TIÊU: nhìn là biết phim, thấy cao trào, muốn click, nhỏ vẫn rõ, nhận ra "
    "cùng 1 kênh. Kết quả NGẦU — ĐIỆN ẢNH — KỊCH TÍNH — SẮC NÉT — CHUYÊN NGHIỆP. "
    "CHỈ tạo 1 ảnh duy nhất, tỉ lệ 16:9 ngang (1536x1024)."
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

        # ── 1. Phát hiện resolution không đồng nhất ─────────────────────────
        resolutions = []
        for fp in self.file_list:
            res = self._probe_resolution(ffmpeg, fp)
            resolutions.append(res)

        unique_res = set(r for r in resolutions if r is not None)
        mixed = len(unique_res) > 1

        if mixed:
            res_list = ", ".join(f"{w}×{h}" for w, h in resolutions if (w, h) in unique_res)
            self.log.emit(
                f"⚠️ Phát hiện resolution KHÔNG đồng nhất: {res_list}\n"
                f"   → Bắt buộc re-encode để cố định kích thước và timestamp.\n"
                f"   (Dùng -c copy sẽ gây Access Violation khi swscale gặp frame đổi kích thước)\n\n"
            )
            # Chọn resolution lớn nhất (theo diện tích) làm chuẩn
            target_w, target_h = max(unique_res, key=lambda wh: wh[0] * wh[1])
            # Đảm bảo chia hết cho 2 (yêu cầu của H.264)
            target_w = (target_w // 2) * 2
            target_h = (target_h // 2) * 2
            self.log.emit(f"   🎯 Resolution đích: {target_w}×{target_h}\n\n")
        else:
            self.log.emit(f"🔗 Bắt đầu gộp {len(self.file_list)} file (resolution đồng nhất, không đổi âm thanh)...\n")

        list_txt = os.path.join(tempfile.gettempdir(), f"concat_list_{int(time.time())}.txt")
        try:
            with open(list_txt, "w", encoding="utf-8") as f:
                for fp in self.file_list:
                    safe_path = fp.replace('\\', '/').replace("'", r"\'")
                    f.write(f"file '{safe_path}'\n")

            si = subprocess.STARTUPINFO() if os.name == "nt" else None
            if si: si.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            if mixed:
                # ── Re-encode: scale mọi clip về resolution đích, reset timestamp ──
                # scale + setsar chuẩn hóa SAR; fps=copy giữ nguyên fps gốc;
                # aresample chuẩn hóa sample rate audio trước khi concat.
                vf = (
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                    f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,"
                    f"setsar=1,fps=fps=source_fps"
                )
                merge_codec = get_optimal_ffmpeg_codec()
                _fb = get_codec_fallback_reason()
                if _fb:
                    self.log.emit(f"   ⚠️ {_fb}\n")
                enc_args = build_video_encoder_args(merge_codec, crf_val=20,
                                                    preset_hw="quality", preset_sw="medium")
                cmd = [
                    ffmpeg, "-y",
                    "-f", "concat", "-safe", "0", "-i", list_txt,
                    # Chuẩn hóa timestamp (sửa non-monotonic DTS từ ghép đoạn)
                    "-video_track_timescale", "90000",
                    "-vf", vf,
                    *enc_args,
                    "-c:a", "aac", "-b:a", "192k",
                    "-af", "aresample=async=1",
                    "-movflags", "+faststart",
                    self.out_file
                ]
            else:
                # Resolution đồng nhất → copy stream, chỉ reset timestamp.
                # (Nhúng ảnh bìa + xuất thumbnail làm ở bước hậu xử lý bên dưới,
                #  áp dụng chung cho cả nhánh re-encode lẫn copy.)
                cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_txt]
                cmd.extend(["-c", "copy"])
                cmd.extend(["-movflags", "+faststart", self.out_file])

            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                startupinfo=si, text=True, encoding="utf-8", errors="replace"
            )

            if proc.returncode == 0 and os.path.exists(self.out_file):
                mode = "re-encode" if mixed else "copy stream"
                self.log.emit(f"✅ Gộp trọn bộ thành công ({mode}): {os.path.basename(self.out_file)}\n")
                thumb_jpg = self._prepare_thumbnail()
                if thumb_jpg:
                    self._embed_cover_mp4(ffmpeg, self.out_file, thumb_jpg, si)
                    self._export_thumbnail_beside_video(thumb_jpg)
                    try: os.remove(thumb_jpg)
                    except: pass
                self.done.emit(True, self.out_file)
            else:
                err = proc.stderr[-800:] if proc.stderr else "Lỗi không xác định"
                # Fallback: nếu re-encode bằng codec phần cứng lỗi -> thử lại
                # bằng libx264 (CPU) cho chắc ăn, tránh hỏng cả bản gộp trọn bộ.
                did_fallback = False
                if mixed and "libx264" not in merge_codec.lower():
                    self.log.emit("⚠️ Gộp bằng codec phần cứng lỗi → thử lại bằng libx264 (CPU)...\n")
                    cmd_fb = [
                        ffmpeg, "-y",
                        "-f", "concat", "-safe", "0", "-i", list_txt,
                        "-video_track_timescale", "90000",
                        "-vf", vf,
                        "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k",
                        "-af", "aresample=async=1",
                        "-movflags", "+faststart",
                        self.out_file
                    ]
                    proc = subprocess.run(
                        cmd_fb, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        startupinfo=si, text=True, encoding="utf-8", errors="replace"
                    )
                    did_fallback = True
                if did_fallback and proc.returncode == 0 and os.path.exists(self.out_file):
                    self.log.emit(f"✅ Gộp trọn bộ thành công (re-encode libx264): {os.path.basename(self.out_file)}\n")
                    thumb_jpg = self._prepare_thumbnail()
                    if thumb_jpg:
                        self._embed_cover_mp4(ffmpeg, self.out_file, thumb_jpg, si)
                        self._export_thumbnail_beside_video(thumb_jpg)
                        try: os.remove(thumb_jpg)
                        except: pass
                    self.done.emit(True, self.out_file)
                else:
                    if did_fallback and proc.stderr:
                        err = proc.stderr[-800:]
                    self.log.emit(f"❌ Lỗi gộp file FFmpeg:\n{err}\n")
                    self.done.emit(False, "")
        except Exception as e:
            self.log.emit(f"❌ Exception khi gộp: {e}\n")
            self.done.emit(False, "")
        finally:
            if os.path.exists(list_txt):
                try: os.remove(list_txt)
                except: pass

class SingleRenderThread(QThread):
    log = pyqtSignal(str); done = pyqtSignal(bool)
    def __init__(self, vp, vi_srt_path, tts_path, out_path, render_cfg):
        super().__init__(); self.vp = vp; self.sp = vi_srt_path; self.tts_path = tts_path; self.op = out_path; self.cfg = render_cfg; self._cancel = False
    def cancel(self): self._cancel = True
    
    def run(self):
        start_t = time.time() 
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
            
        cmd = ["ffmpeg", "-y"] + inputs; temp_filter = ""
        
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
                enc_args.extend(["-movflags", "+faststart", self.op])
            else:
                enc_args.extend(["-c:v", codec, "-pix_fmt", "yuv420p"])
                if audio_map: enc_args.extend(["-c:a", "aac", "-b:a", "192k"])
                if use_crf:
                    enc_args.extend(["-crf", str(crf_val), "-preset", preset_sw])
                else:
                    enc_args.extend(["-b:v", "1000k", "-preset", preset_sw])
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
            last_report = time.time()
            for line in proc.stderr:
                stderr_lines.append(line)
                if "time=" in line and time.time() - last_report > 20:
                    m = re.search(r"time=(\d+:\d+:\d+)", line)
                    if m: self.log.emit(f"   ⏳ Render: {m.group(1)}\n")
                    last_report = time.time()
                if self._cancel:
                    try: proc.terminate()
                    except Exception: pass
                    break
            proc.wait() 
            elapsed = time.time() - start_t
            if self._cancel:
                self.log.emit(f"⛔ Đã hủy render.\n")
                try:
                    if os.path.exists(self.op): os.remove(self.op)
                except Exception: pass
                self.done.emit(False)
            elif proc.returncode == 0:
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
                    cpu_args += ["-movflags", "+faststart", self.op]
                    cmd_cpu = base_cmd + cpu_args

                    kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
                    proc = subprocess.Popen(cmd_cpu, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            text=True, encoding="utf-8", errors="replace", **kw)
                    stderr_lines2 = deque(maxlen=40)
                    last_report = time.time()
                    for line in proc.stderr:
                        stderr_lines2.append(line)
                        if "time=" in line and time.time() - last_report > 20:
                            m = re.search(r"time=(\d+:\d+:\d+)", line)
                            if m: self.log.emit(f"   ⏳ Render (CPU): {m.group(1)}\n")
                            last_report = time.time()
                        if self._cancel:
                            try: proc.terminate()
                            except Exception: pass
                            break
                    proc.wait()
                    if self._cancel:
                        self.log.emit("⛔ Đã hủy render.\n"); self.done.emit(False)
                    elif proc.returncode == 0:
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
class EpisodeCard(QFrame):
    """1 ô trong lưới: hiển thị 1 tập đã ghép cặp video + srt.
    Cho phép đổi lại video/srt nếu ghép sai, và bấm chọn để preview."""
    clicked = pyqtSignal(object)      # phát chính card khi được bấm

    def __init__(self, video_path, srt_path, parent=None):
        super().__init__(parent)
        self.video_path = video_path
        self.srt_path = srt_path
        self.selected = False
        self.setFixedHeight(96)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._build()
        self._apply_style()

    def _build(self):
        lay = QVBoxLayout(self); lay.setContentsMargins(8, 6, 8, 6); lay.setSpacing(2)
        self.lbl_name = QLabel(os.path.basename(self.video_path))
        self.lbl_name.setStyleSheet("color:#E5E6E8; font-weight:bold; font-size:11px; border:none;")
        self.lbl_name.setWordWrap(True)
        srt_txt = os.path.basename(self.srt_path) if self.srt_path else "⚠️ CHƯA CÓ SUB"
        srt_col = "#8A8D98" if self.srt_path else "#F87171"
        self.lbl_srt = QLabel("📄 " + srt_txt)
        self.lbl_srt.setStyleSheet(f"color:{srt_col}; font-size:10px; border:none;")
        self.lbl_badge = QLabel("chờ")
        self.lbl_badge.setStyleSheet("background:#2D303D; color:#8A8D98; font-size:9px; padding:1px 6px; border-radius:4px; border:none;")
        top = QHBoxLayout(); top.addWidget(self.lbl_name, stretch=1); top.addWidget(self.lbl_badge)
        lay.addLayout(top); lay.addWidget(self.lbl_srt)

    def _apply_style(self):
        if self.selected:
            self.setStyleSheet("QFrame { background:#232533; border:2px solid #10B981; border-radius:8px; }")
        else:
            self.setStyleSheet("QFrame { background:#1C1D27; border:1px solid #2D303D; border-radius:8px; } QFrame:hover { border:1px solid #7452FF; }")

    def set_selected(self, val):
        self.selected = val; self._apply_style()

    def refresh_srt_from_disk(self):
        """Tự dò lại sub + video đã lồng tiếng cạnh video trên đĩa và cập
        nhật nhãn. Ưu tiên bản tiếng Việt (*_vi.srt), không có thì lấy .srt
        gốc (vừa tách xong). Nếu tìm thấy *_dubbed.mp4 cạnh video gốc thì
        chuyển video_path sang bản đã lồng để RENDER DÙNG ĐÚNG TIẾNG ĐÃ LỒNG
        (trước đây render vẫn lấy video gốc vì không ai cập nhật video_path).
        Trả về True nếu vừa tìm thấy sub mới hoặc video mới."""
        # Xác định stem gốc (bỏ hậu tố _dubbed nếu video_path đang trỏ
        # tới bản gốc chưa lồng)
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
            if hasattr(self, "lbl_name"):
                self.lbl_name.setText(os.path.basename(self.video_path))

        base = orig_stem
        vi = base + "_vi.srt"
        raw = base + ".srt"
        found = None
        if os.path.exists(vi):
            found = vi
        elif os.path.exists(raw):
            found = raw
        if not found:
            return video_changed
        changed = (self.srt_path != found) or video_changed
        self.srt_path = found
        # Nhãn: bản Việt -> xanh, sub gốc (chưa dịch) -> vàng nhắc nhở
        if found.endswith("_vi.srt"):
            self.lbl_srt.setText("📄 " + os.path.basename(found))
            self.lbl_srt.setStyleSheet("color:#10B981; font-size:10px; border:none;")
        else:
            self.lbl_srt.setText("📄 " + os.path.basename(found) + " (sub gốc)")
            self.lbl_srt.setStyleSheet("color:#FBBF24; font-size:10px; border:none;")
        return changed

    def set_status(self, status):
        colors = {
            "chờ": ("#2D303D", "#8A8D98"),
            "đang render": ("#3B2A1A", "#F37021"),
            "xong": ("#1B3320", "#10B981"),
            "lỗi": ("#3B1A1A", "#F87171"),
        }
        bg, fg = colors.get(status, ("#2D303D", "#8A8D98"))
        self.lbl_badge.setText(status)
        self.lbl_badge.setStyleSheet(f"background:{bg}; color:{fg}; font-size:9px; padding:1px 6px; border-radius:4px; border:none;")

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
        self.bar = QProgressBar(); self.bar.setFixedHeight(3); self.bar.setTextVisible(False)
        self.bar.setStyleSheet("QProgressBar { background:#2D303D; border:none; border-radius:1px; } QProgressBar::chunk { background:#10B981; border-radius:1px; }")
        lay.addWidget(self.bar)

    def set_status(self, status, progress=0):
        color = {"success": "#10B981", "processing": "#F37021"}.get(status, "#4B5563")
        self.dot.setStyleSheet(f"background:{color}; border-radius:4px;")
        self.lbl_status.setText({"success": "xong", "processing": "đang chạy"}.get(status, "chờ"))
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
        self.selected_card = None
        self._thumb_src_path = None     # ảnh gốc cho thumbnail AI
        self._thumb_srt_path = None     # SRT tập phim (không bắt buộc)
        self._thumb_thread = None
        self.blur_boxes = []
        self.sample_sub = None
        self.logo_item = None
        self._design_locked = None
        self._render_queue = []         # hàng đợi render (các card)
        self._render_running = False
        self._stopping = False
        self.render_thread = None

        self.setStyleSheet("""
            QWidget { background:#11121A; color:#E5E6E8; font-family:'Segoe UI',Arial,sans-serif; }
            QScrollArea { border:none; background:transparent; }
            QScrollBar:vertical { background:#11121A; width:8px; }
            QScrollBar::handle:vertical { background:#3B3E4D; border-radius:4px; }
            QPushButton { background:#2D303D; color:#E5E6E8; border-radius:6px; padding:7px; font-weight:bold; border:1px solid #3B3E4D; }
            QPushButton:hover { background:#3B3E4D; border:1px solid #7452FF; color:white; }
            QLineEdit, QSpinBox, QComboBox, QDoubleSpinBox { background:#11121A; border:1px solid #2D303D; padding:7px; color:white; border-radius:4px; font-weight:bold; }
            QComboBox QAbstractItemView { background:#1C1D27; border:1px solid #7452FF; selection-background-color:#2D303D; }
            QCheckBox { font-weight:bold; padding:3px; }
            QCheckBox::indicator { width:18px; height:18px; border-radius:4px; border:1px solid #3B3E4D; background:#11121A; }
            QCheckBox::indicator:checked { background:#10B981; border:1px solid #10B981; }
        """)

        # Helper: bọc 1 widget vào vùng cuộn dọc — để màn hình NHỎ / scale 125%
        # không bị tràn, các mục đè lên nhau. Nội dung dài tự có thanh cuộn.
        def _wrap_scroll(inner):
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            sc.setFrameShape(QFrame.Shape.NoFrame)
            sc.setStyleSheet("QScrollArea { border:none; background:transparent; }")
            sc.setWidget(inner)
            return sc
        self._wrap_scroll = _wrap_scroll

        main = QHBoxLayout(self); main.setContentsMargins(10, 10, 10, 10); main.setSpacing(10)

        # ---------- CỘT TRÁI: GRID GHÉP CẶP ----------
        left = QFrame(); left.setMinimumWidth(240); left.setMaximumWidth(340)
        left.setStyleSheet("background:#151821; border-radius:8px; border:1px solid #1F222D;")
        ll = QVBoxLayout(left); ll.setContentsMargins(10, 10, 10, 10)
        head_q = QHBoxLayout()
        head_q.addWidget(QLabel("🎞️ Hàng đợi Render", styleSheet="font-size:14px; font-weight:bold; color:#F37021; border:none;"))
        head_q.addStretch()
        self.btn_open_folder = QPushButton("📂 Mở thư mục")
        self.btn_open_folder.setStyleSheet("QPushButton { background:#2D303D; color:#E5E6E8; padding:6px 10px; font-size:11px; border-radius:6px; border:1px solid #3B3E4D; } QPushButton:hover { background:#3B3E4D; border:1px solid #7452FF; color:white; }")
        self.btn_open_folder.clicked.connect(self._open_render_folder)
        head_q.addWidget(self.btn_open_folder)
        ll.addLayout(head_q)

        btnrow = QHBoxLayout()
        b_folder = QPushButton("📁 Chọn thư mục"); b_folder.clicked.connect(self._pick_folder)
        b_files = QPushButton("+ File"); b_files.clicked.connect(self._pick_files)
        b_clear = QPushButton("🗑️"); b_clear.setFixedWidth(40); b_clear.clicked.connect(self._clear_all)
        b_clear.setStyleSheet("background:#2D303D; color:#F87171; border:1px dashed #EF4444;")
        btnrow.addWidget(b_folder); btnrow.addWidget(b_files); btnrow.addWidget(b_clear)
        ll.addLayout(btnrow)

        # Nút dọn file rác: sau khi render xong, xóa hết file trung gian, chỉ
        # giữ lại bản render cuối (*_final.mp4) và bản gộp trọn bộ.
        self.btn_clean_junk = QPushButton("🧹 Dọn file rác (chỉ giữ bản render)")
        self.btn_clean_junk.setStyleSheet("QPushButton { background:#3B2A12; color:#FBBF24; border:1px solid #92610a; border-radius:6px; padding:6px; font-size:11px; font-weight:bold; } QPushButton:hover { background:#4a350f; }")
        self.btn_clean_junk.clicked.connect(self._clean_junk_files)
        ll.addWidget(self.btn_clean_junk)

        self.scroll_grid = QScrollArea(); self.scroll_grid.setWidgetResizable(True)
        self.grid_host = QWidget(); self.grid_lay = QGridLayout(self.grid_host)
        self.grid_lay.setAlignment(Qt.AlignmentFlag.AlignTop); self.grid_lay.setSpacing(6)
        self.scroll_grid.setWidget(self.grid_host)
        ll.addWidget(self.scroll_grid, stretch=5)

        # sửa cặp khi chọn 1 card
        fix = QFrame(); fix.setStyleSheet("background:#0F1117; border-radius:6px; border:1px solid #1F222D;")
        fl = QVBoxLayout(fix); fl.setContentsMargins(8, 8, 8, 8); fl.setSpacing(4)
        fl.addWidget(QLabel("Sửa cặp đang chọn:", styleSheet="color:#8A8D98; font-size:10px; border:none;"))
        r1 = QHBoxLayout(); self.lbl_fix_v = QLabel("—"); self.lbl_fix_v.setStyleSheet("color:#E5E6E8; font-size:10px; border:none;")
        bv = QPushButton("Đổi video"); bv.setFixedWidth(80); bv.clicked.connect(self._change_video)
        r1.addWidget(self.lbl_fix_v, stretch=1); r1.addWidget(bv); fl.addLayout(r1)
        r2 = QHBoxLayout(); self.lbl_fix_s = QLabel("—"); self.lbl_fix_s.setStyleSheet("color:#8A8D98; font-size:10px; border:none;")
        bs = QPushButton("Đổi sub"); bs.setFixedWidth(80); bs.clicked.connect(self._change_srt)
        r2.addWidget(self.lbl_fix_s, stretch=1); r2.addWidget(bs); fl.addLayout(r2)
        ll.addWidget(fix)

        self.step_render = ProgressStep("Tiến độ render")
        ll.addWidget(self.step_render)
        self.txt_log = QTextEdit(); self.txt_log.setReadOnly(True); self.txt_log.document().setMaximumBlockCount(400)
        self.txt_log.setFixedHeight(90)
        self.txt_log.setStyleSheet("background:#0B0E14; color:#A7F3D0; font-family:Consolas; font-size:10px; padding:5px; border:1px solid #1F222D;")
        ll.addWidget(self.txt_log)
        main.addWidget(left)

        # ---------- CỘT GIỮA: PREVIEW ----------
        center = QFrame(); center.setStyleSheet("background:transparent; border:none;")
        cl = QVBoxLayout(center); cl.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(QLabel("🎬 Xem trước (bấm 1 tập bên trái · kéo chữ/logo · lăn chuột để zoom vật thể)",
                              styleSheet="color:#8A8D98; font-weight:bold; font-size:11px;"))
        head.addStretch()
        self.btn_reset = QPushButton("Reset vị trí"); self.btn_reset.setStyleSheet("background:#31265C; color:#7452FF; padding:4px;")
        self.btn_reset.clicked.connect(self._reset_pos); head.addWidget(self.btn_reset)
        cl.addLayout(head)

        self.scene = QGraphicsScene(self)
        self.video_item = QGraphicsVideoItem(); self.scene.addItem(self.video_item)
        self.media_player = QMediaPlayer(); self.audio_output = QAudioOutput()
        self.media_player.setAudioOutput(self.audio_output); self.media_player.setVideoOutput(self.video_item)
        self.video_item.nativeSizeChanged.connect(self._on_native_size)
        self.preview = PreviewGraphicsView(self.scene); cl.addWidget(self.preview, stretch=1)

        ctr = QHBoxLayout()
        self.btn_play = QPushButton("▶"); self.btn_play.setFixedWidth(70); self.btn_play.clicked.connect(self._toggle_play)
        self.lbl_time = QLabel("00:00 / 00:00"); self.lbl_time.setStyleSheet("color:#8A8D98; font-size:11px; padding:0 5px;")
        self.slider = QSlider(Qt.Orientation.Horizontal); self.slider.sliderMoved.connect(self.media_player.setPosition)
        self.media_player.positionChanged.connect(self._on_pos)
        self.media_player.durationChanged.connect(self._on_dur)
        ctr.addWidget(self.btn_play); ctr.addWidget(self.slider); ctr.addWidget(self.lbl_time)
        cl.addLayout(ctr)
        main.addWidget(center, stretch=6)

        # ---------- CỘT PHẢI: DESIGN ----------
        right = QFrame(); right.setMinimumWidth(280); right.setMaximumWidth(380)
        right.setStyleSheet("background:#151821; border-radius:8px; border:1px solid #1F222D;")
        rl = QVBoxLayout(right); rl.setContentsMargins(5, 5, 5, 5)

        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(
            "QTabWidget::pane { border: 1px solid #2D303D; border-radius: 4px; background: #151821; }"
            "QTabBar::tab { background: #1C1D27; color: #8A8D98; padding: 7px 4px; border: 1px solid #2D303D; border-bottom: none; border-top-left-radius: 4px; border-top-right-radius: 4px; font-weight: bold; font-size: 10px; }"
            "QTabBar::tab:selected { background: #2D303D; color: #10B981; }"
            "QTabBar::tab:hover:!selected { background: #232533; }"
        )
        # Cho 3 tab chia đều bề ngang, không dùng nút cuộn ‹ › gây phải bấm qua lại
        self.tabs.tabBar().setExpanding(True)
        self.tabs.setUsesScrollButtons(False)
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideNone)
        self.tab_design = QWidget()
        self.tab_thumb = QWidget()
        # Bọc trong vùng cuộn: màn nhỏ không còn tràn/đè các mục lên nhau.
        self.tabs.addTab(self._wrap_scroll(self.tab_design), "🎨 Thiết kế")
        self.tabs.addTab(self._wrap_scroll(self.tab_thumb), "🖼️ Thumbnail")
        # Tab con: Tách sub → Dịch → Lồng tiếng (tái dùng thread từ honggou_tab).
        # Import an toàn: thiếu module thì bỏ qua, không làm hỏng tab Render.
        try:
            from render_dub_feature import attach_dub_tab
            attach_dub_tab(self)
        except Exception as _dub_err:
            print(f"[WARN] Không nạp được tab Sub→Dịch→Lồng: {_dub_err}")
        rl.addWidget(self.tabs)

        design_lay = QVBoxLayout(self.tab_design); design_lay.setContentsMargins(5, 10, 5, 5)

        self.chk_hardsub = QCheckBox("Khắc Sub vào Video")
        self.chk_hardsub.setChecked(self.settings.value("hardsub_en", True, type=bool))
        self.chk_hardsub.setStyleSheet("color:#FBBF24; font-weight:bold;")
        design_lay.addWidget(self.chk_hardsub)

        # ── Nhúng Ảnh Bìa (đưa lên đầu tab thiết kế cho dễ thao tác) ──
        intro_row = QHBoxLayout()
        self.chk_intro = QCheckBox("Nhúng Ảnh Bìa (Cover)")
        self.chk_intro.setChecked(self.settings.value("intro_en", False, type=bool))
        self.chk_intro.setStyleSheet("color:#F37021; font-weight:bold; font-size:11px;")
        self.intro_input = QLineEdit(self.settings.value("intro_path", ""))
        self.intro_input.setPlaceholderText("File Ảnh Bìa...")
        btn_intro_pick = QPushButton("Chọn")
        btn_intro_pick.setFixedWidth(50)
        btn_intro_pick.clicked.connect(self._select_intro)
        intro_row.addWidget(self.chk_intro); intro_row.addWidget(self.intro_input); intro_row.addWidget(btn_intro_pick)
        design_lay.addLayout(intro_row)
        self.chk_intro.stateChanged.connect(lambda: self.settings.setValue("intro_en", self.chk_intro.isChecked()))
        self.intro_input.textChanged.connect(lambda: self.settings.setValue("intro_path", self.intro_input.text().strip()))

        ql = QHBoxLayout(); ql.addWidget(QLabel("Chất lượng:"))
        self.cb_quality = QComboBox()
        self.cb_quality.addItems([
            "🏆 Cao nhất (CRF 16 - Gần lossless)",
            "⭐ Tốt (CRF 20 - Đề xuất)",
            "👍 Vừa (CRF 26 - Cân bằng)",
            "⚡ Nhanh (1 Mbps - File nhỏ)",
        ])
        self.cb_quality.setCurrentText(self.settings.value("render_quality", "⭐ Tốt (CRF 20 - Đề xuất)"))
        ql.addWidget(self.cb_quality); design_lay.addLayout(ql)

        fb = QHBoxLayout(); fb.addWidget(QLabel("Font:"))
        self.cb_font = QComboBox(); self.cb_font.addItems(FONTS_LIST)
        self.cb_font.setCurrentText(self.settings.value("font_name", "Arial"))
        fb.addWidget(QLabel("Cỡ:")); self.spin_size = QSpinBox(); self.spin_size.setRange(10, 150)
        self.spin_size.setValue(int(self.settings.value("font_size", 24)))
        fb.addWidget(self.cb_font); fb.addWidget(self.spin_size); design_lay.addLayout(fb)

        cb = QHBoxLayout(); cb.addWidget(QLabel("Màu Sub:"))
        self.cb_color = QComboBox(); self.cb_color.addItems(list(COLOR_PRESETS.keys()))
        self.cb_color.setCurrentText(self.settings.value("font_color_name", "Trắng (White)"))
        cb.addWidget(self.cb_color); design_lay.addLayout(cb)

        box_row = QHBoxLayout()
        self.chk_subbox = QCheckBox("Nền ô chữ")
        self.chk_subbox.setChecked(self.settings.value("subbox_en", False, type=bool))
        self.chk_subbox.setStyleSheet("color:#93c5fd; font-weight:bold;")
        box_row.addWidget(self.chk_subbox)
        box_row.addWidget(QLabel("Màu nền:"))
        self.cb_subbox_color = QComboBox()
        self.cb_subbox_color.addItems(["Đen", "Xám đậm", "Xanh đen", "Trắng"])
        self.cb_subbox_color.setCurrentText(self.settings.value("subbox_color_name", "Đen"))
        box_row.addWidget(self.cb_subbox_color)
        design_lay.addLayout(box_row)

        op_row = QHBoxLayout()
        op_row.addWidget(QLabel("Độ mờ nền:"))
        self.spn_subbox_opacity = QSpinBox()
        self.spn_subbox_opacity.setRange(0, 100)
        self.spn_subbox_opacity.setValue(int(self.settings.value("subbox_opacity", 60)))
        self.spn_subbox_opacity.setSuffix(" %")
        op_row.addWidget(self.spn_subbox_opacity); op_row.addStretch()
        design_lay.addLayout(op_row)

        self.cb_font.currentTextChanged.connect(lambda *_: self._restyle_sample_sub())
        self.spin_size.valueChanged.connect(lambda *_: self._restyle_sample_sub())
        self.cb_color.currentTextChanged.connect(lambda *_: self._restyle_sample_sub())

        bl = QHBoxLayout()
        self.chk_blur = QCheckBox("Bật Khung Mờ"); self.chk_blur.setChecked(self.settings.value("bp_blur_en", False, type=bool))
        self.chk_blur.setStyleSheet("color:#F37021; font-weight:bold;")
        b_add = QPushButton("[+] Vùng che"); b_add.setStyleSheet("background:#2D303D; color:#10B981; padding:4px; font-size:10px;")
        b_add.clicked.connect(lambda: self._add_blur_box())
        b_clr = QPushButton("[-] Xóa"); b_clr.setStyleSheet("background:#2D303D; color:#EF4444; padding:4px; font-size:10px;")
        b_clr.clicked.connect(self._clear_blur_boxes)
        bl.addWidget(self.chk_blur); bl.addWidget(b_add); bl.addWidget(b_clr); design_lay.addLayout(bl)

        frl = QHBoxLayout()
        self.chk_frame = QCheckBox("Overlay PNG"); self.chk_frame.setChecked(self.settings.value("bp_frame_en", False, type=bool))
        self.chk_frame.setStyleSheet("color:#7452FF; font-weight:bold;")
        self.frame_input = QLineEdit(self.settings.value("frame_path", "")); self.frame_input.setPlaceholderText("Ảnh PNG...")
        bf = QPushButton("Chọn"); bf.setFixedWidth(55); bf.clicked.connect(self._select_frame)
        frl.addWidget(self.chk_frame); frl.addWidget(self.frame_input); frl.addWidget(bf); design_lay.addLayout(frl)

        lgl = QHBoxLayout()
        self.chk_logo = QCheckBox("Logo"); self.chk_logo.setChecked(self.settings.value("bp_logo_en", False, type=bool))
        self.logo_input = QLineEdit(self.settings.value("logo_path", ""))
        bg2 = QPushButton("Chọn"); bg2.setFixedWidth(55); bg2.clicked.connect(self._select_logo)
        lgl.addWidget(self.chk_logo); lgl.addWidget(self.logo_input); lgl.addWidget(bg2); design_lay.addLayout(lgl)

        self.chk_logo.stateChanged.connect(lambda: self._update_logo_preview())
        self.logo_input.textChanged.connect(lambda: self._update_logo_preview())
        self.chk_logo.stateChanged.connect(lambda: setattr(self, "_design_locked", None))
        self.logo_input.textChanged.connect(lambda: setattr(self, "_design_locked", None))

        design_lay.addWidget(QLabel("🎛️ Bộ lọc Bypass FX:", styleSheet="font-weight:bold; margin-top:5px; color:#8A8D98;"))
        self.chk_flip = QCheckBox("Lật ngang"); self.chk_zoom = QCheckBox("Phóng to 4%")
        self.chk_color = QCheckBox("Kích màu sáng"); self.chk_noise = QCheckBox("Nhiễu hạt")
        self.chk_speed = QCheckBox("Tốc độ 1.05x"); self.chk_pitch = QCheckBox("Đổi Tone")
        self.chk_rotate = QCheckBox("Xoay 1°")
        for k, chk in (("bp_flip", self.chk_flip), ("bp_zoom", self.chk_zoom), ("bp_color", self.chk_color),
                       ("bp_noise", self.chk_noise), ("bp_speed", self.chk_speed), ("bp_pitch", self.chk_pitch),
                       ("bp_rotate", self.chk_rotate)):
            chk.setChecked(self.settings.value(k, False, type=bool))
        gb = QGridLayout()
        gb.addWidget(self.chk_flip, 0, 0); gb.addWidget(self.chk_zoom, 0, 1)
        gb.addWidget(self.chk_color, 1, 0); gb.addWidget(self.chk_noise, 1, 1)
        gb.addWidget(self.chk_speed, 2, 0); gb.addWidget(self.chk_pitch, 2, 1)
        gb.addWidget(self.chk_rotate, 3, 0)
        design_lay.addLayout(gb)
        design_lay.addStretch()

        # ── Khu Thumbnail AI (Gemini) ──
        self._build_thumbnail_ui(self.tab_thumb)

        bot_lay = QVBoxLayout(); bot_lay.setContentsMargins(5, 5, 5, 5)
        self.btn_sync_design = QPushButton("🔄 Đồng bộ canh chỉnh cho tất cả file")
        self.btn_sync_design.setStyleSheet("QPushButton { background:#0891b2; color:white; padding:8px; font-size:12px; border-radius:8px; border:none; } QPushButton:hover { background:#0e7490; }")
        self.btn_sync_design.clicked.connect(self._sync_design_all)
        bot_lay.addWidget(self.btn_sync_design)

        merge_row = QHBoxLayout()
        self.chk_merge_all = QCheckBox("🔗 Gộp trọn bộ sau Render")
        self.chk_merge_all.setChecked(self.settings.value("merge_after_render", False, type=bool))
        self.chk_merge_all.setStyleSheet("color:#10B981; font-weight:bold; font-size:12px;")
        self.chk_merge_all.stateChanged.connect(lambda: self.settings.setValue("merge_after_render", self.chk_merge_all.isChecked()))
        merge_row.addWidget(self.chk_merge_all)
        bot_lay.addLayout(merge_row)

        # Nút chạy trọn quy trình: tách → dịch → lồng → render. Dùng đúng cấu
        # hình đã set trong tab con "Sub → Dịch → Lồng".
        self.btn_full_pipeline = QPushButton("🚀 LÀM TẤT CẢ: Tách → Dịch → Lồng → Render")
        self.btn_full_pipeline.setStyleSheet("QPushButton { background:#7452FF; color:white; padding:11px; font-size:12px; font-weight:bold; border-radius:8px; border:none; } QPushButton:hover { background:#5b3fd6; } QPushButton:disabled { background:#3B3E4D; color:#8A8D98; }")
        self.btn_full_pipeline.clicked.connect(self._run_full_pipeline_external)
        bot_lay.addWidget(self.btn_full_pipeline)

        run_merge_lay = QHBoxLayout()
        
        self.btn_run = QPushButton("🔥 RENDER TẤT CẢ (0)")
        self.btn_run.setStyleSheet("QPushButton { background:#F37021; color:white; padding:12px; font-size:14px; border-radius:8px; border:none; font-weight:bold; } QPushButton:hover { background:#e05f10; }")
        self.btn_run.clicked.connect(self._start_render_all)
        run_merge_lay.addWidget(self.btn_run)
        
        self.btn_merge_now = QPushButton("🔗 GỘP NGAY (0)")
        self.btn_merge_now.setStyleSheet("QPushButton { background:#10B981; color:white; padding:12px; font-size:14px; border-radius:8px; border:none; font-weight:bold; } QPushButton:hover { background:#059669; }")
        self.btn_merge_now.clicked.connect(self._start_merge_now)
        run_merge_lay.addWidget(self.btn_merge_now)
        
        bot_lay.addLayout(run_merge_lay)

        self.btn_stop = QPushButton("⛔ DỪNG RENDER")
        self.btn_stop.setStyleSheet("QPushButton { background:#7F1D1D; color:white; padding:10px; font-size:13px; border-radius:8px; border:none; } QPushButton:hover { background:#991B1B; } QPushButton:disabled { background:#3B2020; color:#8A8D98; }")
        self.btn_stop.clicked.connect(self._stop_render)
        self.btn_stop.setEnabled(False)
        bot_lay.addWidget(self.btn_stop)
        
        rl.addLayout(bot_lay)
        main.addWidget(right)    

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

    # ============ GHÉP CẶP VIDEO + SRT ============
    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Chọn thư mục chứa các tập")
        if not d:
            return
        pairs = self._auto_pair(d)
        if not pairs:
            QMessageBox.information(self, "Không thấy video", "Thư mục này không có file video (.mp4) nào.")
            return
        for vp, sp in pairs:
            self._add_card(vp, sp)
        self._relayout_grid()
        self._update_run_label()

    def _pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Chọn video", "", "Video (*.mp4 *.mkv *.mov *.avi)")
        for vp in files:
            sp = self._guess_srt_for(vp)
            self._add_card(vp, sp)
        if files:
            self._relayout_grid(); self._update_run_label()

    def _auto_pair(self, folder):
        """Quét folder, ghép cặp video+srt. Ưu tiên *_dubbed.mp4 + *_vi.srt;
        không có dubbed thì dùng video gốc; không có _vi.srt thì dùng .srt gốc.
        Gom theo 'stem gốc' của mỗi tập (bỏ hậu tố _dubbed)."""
        try:
            names = os.listdir(folder)
        except Exception:
            return []
        videos = [n for n in names if n.lower().endswith((".mp4", ".mkv", ".mov", ".avi"))]
        srt_set = set(n for n in names if n.lower().endswith(".srt"))

        # gom video theo stem gốc (bỏ _dubbed)
        groups = {}   # base_stem -> {"dubbed":..., "plain":...}
        for v in videos:
            stem = os.path.splitext(v)[0]
            if stem.endswith("_dubbed"):
                base = stem[:-len("_dubbed")]
                groups.setdefault(base, {})["dubbed"] = v
            else:
                groups.setdefault(stem, {})["plain"] = v

        pairs = []
        for base in sorted(groups.keys(), key=_natural_key):
            g = groups[base]
            video = g.get("dubbed") or g.get("plain")
            if not video:
                continue
            # chọn srt: ưu tiên <base>_vi.srt, rồi <base>.srt
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
        if stem.endswith("_dubbed"):
            stem = stem[:-len("_dubbed")]
        for cand in (stem + "_vi.srt", stem + ".srt"):
            if os.path.exists(cand):
                return cand
        return None

    def _add_card(self, video_path, srt_path):
        # tránh trùng
        for c in self.cards:
            if c.video_path == video_path:
                return
        card = EpisodeCard(video_path, srt_path)
        card.clicked.connect(self._on_card_clicked)
        self.cards.append(card)

    def _relayout_grid(self):
        # xếp lại lưới 2 cột
        for i in reversed(range(self.grid_lay.count())):
            w = self.grid_lay.itemAt(i).widget()
            if w:
                self.grid_lay.removeWidget(w)
        for idx, card in enumerate(self.cards):
            self.grid_lay.addWidget(card, idx // 1, idx % 1)   # 1 cột cho dễ đọc tên

    def _clear_all(self):
        self.media_player.stop()
        for c in self.cards:
            c.setParent(None)
        self.cards = []; self.selected_card = None
        self.lbl_fix_v.setText("—"); self.lbl_fix_s.setText("—")
        self._update_run_label()

    def _on_card_clicked(self, card):
        for c in self.cards:
            c.set_selected(c is card)
        self.selected_card = card
        self.lbl_fix_v.setText(os.path.basename(card.video_path))
        self.lbl_fix_s.setText(os.path.basename(card.srt_path) if card.srt_path else "⚠️ chưa có sub")
        self._load_preview(card.video_path)

    def _change_video(self):
        if not self.selected_card:
            return
        fp, _ = QFileDialog.getOpenFileName(self, "Chọn video khác", "", "Video (*.mp4 *.mkv *.mov *.avi)")
        if fp:
            self.selected_card.video_path = fp
            self.selected_card.lbl_name.setText(os.path.basename(fp))
            self.lbl_fix_v.setText(os.path.basename(fp))
            self._load_preview(fp)

    def _change_srt(self):
        if not self.selected_card:
            return
        fp, _ = QFileDialog.getOpenFileName(self, "Chọn sub khác", "", "Phụ đề (*.srt)")
        if fp:
            self.selected_card.srt_path = fp
            self.selected_card.lbl_srt.setText("📄 " + os.path.basename(fp))
            self.selected_card.lbl_srt.setStyleSheet("color:#8A8D98; font-size:10px; border:none;")
            self.lbl_fix_s.setText(os.path.basename(fp))

    def _update_run_label(self):
        self.btn_run.setText(f"🔥 RENDER TẤT CẢ ({len(self.cards)})")
        if hasattr(self, 'btn_merge_now'):
            self.btn_merge_now.setText(f"🔗 GỘP NGAY ({len(self.cards)})")

    # ============ PREVIEW ============
    def _load_preview(self, video_path):
        try:
            self.media_player.setSource(QUrl.fromLocalFile(video_path))
            self.media_player.pause()
        except Exception as e:
            self._log(f"⚠️ Không mở được preview: {e}")

    def _toggle_play(self):
        from PyQt6.QtMultimedia import QMediaPlayer as _QMP
        if self.media_player.playbackState() == _QMP.PlaybackState.PlayingState:
            self.media_player.pause(); self.btn_play.setText("▶")
        else:
            self.media_player.play(); self.btn_play.setText("⏸")

    def _on_pos(self, pos):
        self.slider.setValue(pos)
        dur = self.media_player.duration()
        self.lbl_time.setText(f"{format_time(pos/1000)} / {format_time(dur/1000)}")

    def _on_dur(self, dur):
        self.slider.setRange(0, dur)

    def _on_native_size(self, size):
        if size.width() > 0 and size.height() > 0:
            self.video_item.setSize(size)
            self.scene.setSceneRect(0, 0, size.width(), size.height())
            self.preview.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            self._ensure_sample_sub()   # hiện ô chữ mẫu để canh vị trí sub
            self._update_logo_preview() # hiện logo nếu có

    def _reset_pos(self):
        self.preview.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

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
    def _collect_design(self):
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
                "x": lr.x(), "y": lr.y(), "scale": self.logo_item.scale()
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
                }
        except Exception:
            sub_pos = None

        self.settings.setValue("font_name", self.cb_font.currentText())
        self.settings.setValue("font_size", self.spin_size.value())
        self.settings.setValue("font_color_name", color_name)
        self.settings.setValue("render_quality", self.cb_quality.currentText())
        self.settings.setValue("hardsub_en", self.chk_hardsub.isChecked())

        # Nền ô chữ -> mã màu ASS &HAABBGGRR (AA=alpha: 00 đặc, FF trong).
        subbox_en = self.chk_subbox.isChecked()
        _box_bgr = {"Đen": "000000", "Xám đậm": "202020", "Xanh đen": "301500", "Trắng": "FFFFFF"}
        bgr = _box_bgr.get(self.cb_subbox_color.currentText(), "000000")
        opac = int(self.spn_subbox_opacity.value())          # 0..100 (100 = đặc)
        alpha = int(round((100 - opac) * 255 / 100))         # ASS alpha: 0 đặc, 255 trong
        subbox_color = f"&H{alpha:02X}{bgr}"
        self.settings.setValue("subbox_en", subbox_en)
        self.settings.setValue("subbox_color_name", self.cb_subbox_color.currentText())
        self.settings.setValue("subbox_opacity", opac)
        # Lưu thêm các thiết lập Logo/Khung mờ/Overlay/Bypass FX -> trước đây
        # các giá trị này chỉ được ĐỌC lúc khởi động chứ chưa từng được GHI,
        # nên có thể bị "quên" nếu app khởi động lại giữa chừng.
        self.settings.setValue("bp_logo_en", self.chk_logo.isChecked())
        self.settings.setValue("logo_path", self.logo_input.text().strip())
        self.settings.setValue("bp_frame_en", self.chk_frame.isChecked())
        self.settings.setValue("frame_path", self.frame_input.text().strip())
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
            "sub_pos": sub_pos,
            "logo_pos": logo_pos,
            "SW": SW, "SH": SH,
            "subbox_en": subbox_en, "subbox_color": subbox_color,
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
            # KHÔNG bật tts_en -> SingleRenderThread bỏ qua phần audio TTS
        }

    # ============ RENDER HÀNG LOẠT ============
    def _sync_design_all(self):
        """Chốt cấu hình canh chỉnh hiện tại (vị trí sub + ô che + font/màu/FX)
        làm chuẩn dùng cho TẤT CẢ các tập khi render."""
        if not self.cards:
            QMessageBox.information(self, "Chưa có file", "Hãy thêm video vào hàng đợi trước.")
            return
        self._design_locked = self._collect_design()
        n_blur = len(self._design_locked.get("blur_list", []))
        self._log(f"🔄 Đã chốt canh chỉnh (vị trí sub + {n_blur} ô che) áp cho tất cả {len(self.cards)} tập.")
        QMessageBox.information(self, "Đã đồng bộ",
            f"Đã lưu canh chỉnh hiện tại làm chuẩn cho tất cả {len(self.cards)} tập.\n"
            f"Bấm 'RENDER TẤT CẢ' để render đồng loạt theo canh chỉnh này.")

    def _clean_junk_files(self):
        """Dọn sạch file trung gian sau khi render, CHỈ GIỮ bản render cuối
        (*_final.mp4) và bản gộp trọn bộ (*_TronBo_Rendered.mp4).
        Xóa: video gốc, *_dubbed.mp4, .srt, _vi.srt, .txt, .vocals_cache.wav.
        Xóa luôn, không hỏi (theo yêu cầu)."""
        if self._render_running:
            QMessageBox.information(self, "Đang render", "Đang render, dọn rác sau khi xong.")
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
        # Chuyển sang tab con để người dùng thấy log chạy
        try:
            self.tabs.setCurrentWidget(tab)
        except Exception:
            pass
        tab._run_full_pipeline()

    def _start_render_all(self):
        if self._render_running:
            QMessageBox.information(self, "Đang render", "Đang render, vui lòng đợi xong.")
            return
        if not self.cards:
            QMessageBox.information(self, "Chưa có video", "Hãy thêm video vào hàng đợi trước.")
            return
        # Ưu tiên cấu hình ĐÃ ĐỒNG BỘ (nếu bấm nút Đồng bộ trước đó); nếu chưa
        # đồng bộ thì lấy canh chỉnh hiện tại. Dù cách nào cũng áp CHUNG cho mọi tập.
        self._design = getattr(self, '_design_locked', None) or self._collect_design()
        # Sắp lại theo SỐ tập để gộp trọn bộ đúng thứ tự 1 -> cuối
        # (phòng khi thêm file thủ công bằng '+ File' không theo thứ tự).
        self.cards.sort(key=lambda c: _natural_key(os.path.basename(c.video_path)))
        self._relayout_grid()
        self._render_queue = list(self.cards)
        self._rendered_files = [] # Lưu danh sách file xuất ra để gộp
        self._render_running = True
        self._stopping = False
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        if hasattr(self, 'btn_merge_now'):
            self.btn_merge_now.setEnabled(False)
        self.chk_merge_all.setEnabled(False)
        self._log(f"🚀 Bắt đầu render {len(self._render_queue)} tập...")
        self._render_next()

    def _start_merge_now(self):
        if self._render_running or (hasattr(self, 'merge_thread') and self.merge_thread.isRunning()):
            QMessageBox.information(self, "Đang bận", "Hệ thống đang xử lý, vui lòng đợi xong.")
            return
        
        if not self.cards:
            QMessageBox.information(self, "Chưa có video", "Hãy thêm video vào hàng đợi trước.")
            return

        files_to_merge = []
        for c in self.cards:
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
        self._log("⛔ Đang dừng render... (đợi tập hiện tại thoát)")
        if self.render_thread and self.render_thread.isRunning():
            self.render_thread.cancel()  # hủy tập đang render (xóa file dở)

    def _render_next(self):
        if self._stopping or not self._render_queue:
            stopped = self._stopping
            self._render_running = False
            self._stopping = False
            self._render_queue = []
            
            self.btn_run.setEnabled(True)
            self.btn_stop.setEnabled(False)
            if hasattr(self, 'btn_merge_now'):
                self.btn_merge_now.setEnabled(True)
            self.chk_merge_all.setEnabled(True)
            
            if stopped:
                self.step_render.set_status("error", 0)
                self._log("⛔ Đã dừng render.")
                QMessageBox.information(self, "Đã dừng", "Đã dừng render theo yêu cầu.")
            else:
                if self.chk_merge_all.isChecked() and len(self._rendered_files) > 1:
                    self._start_merge(self._rendered_files)
                else:
                    self.step_render.set_status("success", 100)
                    self._log("🎉 Đã render xong tất cả!")
                    QMessageBox.information(self, "Xong", "Đã render xong tất cả các tập!")
            return
            
        card = self._render_queue.pop(0)
        card.set_status("đang render")
        self.step_render.set_status("processing", 30)
        vp = card.video_path
        sp = card.srt_path
        out_dir = os.path.dirname(vp)
        self._last_render_dir = out_dir
        stem = os.path.splitext(os.path.basename(vp))[0]
        if stem.endswith("_dubbed"):
            stem = stem[:-len("_dubbed")]
        out_path = os.path.join(out_dir, f"{stem}_final.mp4")
        cfg = self._build_cfg(vp, self._design)
        self._log(f"🎬 Render: {os.path.basename(vp)}...")
        
        self.render_thread = SingleRenderThread(vp, sp, None, out_path, cfg)
        self.render_thread.log.connect(self._log)
        self.render_thread.done.connect(lambda ok, c=card, op=out_path: self._on_one_done(ok, c, op))
        self.render_thread.start()

    def _on_one_done(self, ok, card, out_path):
        if self._stopping and not ok:
            card.set_status("đã dừng")
        else:
            card.set_status("xong" if ok else "lỗi")
            if ok:
                self._rendered_files.append(out_path)
        self._render_next()
        
    def _start_merge(self, file_list):
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
