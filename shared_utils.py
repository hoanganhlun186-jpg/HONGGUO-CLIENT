import os
import sys
import threading
import subprocess
import re
import shutil
import concurrent.futures
import requests
from PyQt6.QtCore import QObject, pyqtSignal

# Flag ẩn cửa sổ console (cmd) khi gọi subprocess trên Windows
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# ====================================================================
# 0. ĐỊNH VỊ CÔNG CỤ NGOÀI (ffmpeg/ffprobe/ffplay/aria2c)
#    Thứ tự ưu tiên: bundle PyInstaller (_MEIPASS) -> cạnh file .exe
#    -> thư mục hiện tại -> PATH hệ thống. Nhờ vậy máy khách chỉ cần
#    có ffmpeg.exe nằm cạnh app là chạy, KHÔNG phụ thuộc PATH máy Anh.
# ====================================================================
def _tool_dirs() -> list:
    """Danh sách thư mục có thể chứa .exe kèm theo, theo thứ tự ưu tiên."""
    dirs = []
    
    # 1) Thư mục bundle tạm khi chạy .exe onefile (PyInstaller)
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        dirs.append(mei)
        
    # 2) Thư mục chứa file .exe thật (khi đóng gói) hoặc file script (khi chạy .py)
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(sys.executable))
    else:
        dirs.append(os.path.dirname(os.path.abspath(__file__)))
        
    # 3) Thư mục làm việc hiện tại
    dirs.append(os.path.abspath("."))
    
    # Khử trùng lặp, giữ đúng thứ tự ưu tiên
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out

def find_tool(name: str):
    """
    Tìm 1 công cụ (vd 'ffmpeg', 'ffplay', 'ffprobe', 'aria2c').
    Trả về đường dẫn tuyệt đối nếu thấy file, hoặc kết quả shutil.which nếu có trong PATH, hoặc None.
    """
    exe = name if name.lower().endswith(".exe") or os.name != "nt" else name + ".exe"
    for d in _tool_dirs():
        cand = os.path.join(d, exe)
        if os.path.exists(cand):
            return cand
    return shutil.which(name) or shutil.which(exe)

def get_ffmpeg_path() -> str:
    """Đường dẫn ffmpeg (ưu tiên file kèm app trước PATH). Fallback về 'ffmpeg'."""
    return find_tool("ffmpeg") or "ffmpeg"

def get_ffprobe_path() -> str:
    return find_tool("ffprobe") or "ffprobe"

def get_ffplay_path():
    """Đường dẫn ffplay, hoặc None nếu không tìm thấy."""
    return find_tool("ffplay")

def get_ytdlp_path() -> str:
    """Đường dẫn yt-dlp (ưu tiên yt-dlp.exe kèm app, rồi PATH). Fallback
    'yt-dlp' để Windows tự tìm. Dùng cho các tab gọi yt-dlp qua subprocess,
    tránh phụ thuộc thư mục làm việc hiện tại của khách."""
    return find_tool("yt-dlp") or "yt-dlp"


# ====================================================================
# 0b. CHỌN TRÌNH DUYỆT CHO PLAYWRIGHT (Chrome khách -> Chromium bundled)
#     Ưu tiên Google Chrome CÀI SẴN trên máy khách (channel="chrome"):
#     nhẹ, không phải tải Chromium. Nếu máy KHÔNG có Chrome thì tự lùi về
#     Chromium do Playwright tải (cần đã 'playwright install chromium').
#     Nhờ vậy: khách có Chrome -> chạy ngay; không có -> vẫn chạy được.
# ====================================================================
_CHROME_CHECK_CACHE = None

def _win_chrome_paths():
    """Các vị trí Google Chrome hay được cài trên Windows."""
    paths = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            paths.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    return paths

def chrome_channel_available() -> bool:
    """True nếu máy có Google Chrome (để dùng channel='chrome'). Có cache."""
    global _CHROME_CHECK_CACHE
    if _CHROME_CHECK_CACHE is not None:
        return _CHROME_CHECK_CACHE
    ok = False
    try:
        if os.name == "nt":
            for p in _win_chrome_paths():
                if p and os.path.exists(p):
                    ok = True
                    break
            if not ok and (shutil.which("chrome") or shutil.which("chrome.exe")):
                ok = True
        else:
            for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"):
                if shutil.which(name):
                    ok = True
                    break
    except Exception:
        ok = False
    _CHROME_CHECK_CACHE = ok
    return ok

def browser_launch_kwargs(headless=True, args=None, **extra) -> dict:
    """Trả về kwargs cho chromium.launch() / launch_persistent_context().
    Tự thêm channel='chrome' NẾU máy có Chrome; nếu không, bỏ channel để
    Playwright dùng Chromium bundled. Gộp thêm headless/args/extra."""
    kw = dict(extra)
    kw["headless"] = headless
    if args is not None:
        kw["args"] = args
    if chrome_channel_available():
        kw["channel"] = "chrome"
    return kw

# Thông báo hướng dẫn khi thiếu cả Chrome lẫn Chromium (dùng chung ở các tab)
BROWSER_MISSING_MSG = (
    "❌ Không tìm thấy trình duyệt để chạy.\n"
    "   • Cách 1 (khuyên dùng): Cài Google Chrome rồi mở lại app.\n"
    "   • Cách 2: Chạy lệnh 'playwright install chromium' một lần.\n"
)

def is_browser_missing_error(err) -> bool:
    """Nhận diện lỗi Playwright do thiếu trình duyệt, để hiện hướng dẫn."""
    s = str(err).lower()
    return ("executable doesn't exist" in s or "channel" in s and "chrome" in s
            or "playwright install" in s or "looks like playwright" in s
            or "browsertype.launch" in s)


# ====================================================================
# 1. THUMBNAIL CACHING & ASYNC LOADER (Tối ưu hóa CPU & RAM)
# ====================================================================
_image_cache = {}
_thumb_executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

class AsyncImageLoader(QObject):
    image_loaded = pyqtSignal(str, bytes)
    
    def __init__(self, url: str, vid_id: str):
        super().__init__()
        self.url = url
        self.vid_id = vid_id
        
    def start(self):
        if not self.url: 
            return
            
        if self.vid_id in _image_cache:
            self.image_loaded.emit(self.vid_id, _image_cache[self.vid_id])
            return
            
        def fetch_image():
            try:
                resp = requests.get(self.url, timeout=5)
                if resp.status_code == 200:
                    _image_cache[self.vid_id] = resp.content
                    self.image_loaded.emit(self.vid_id, resp.content)
            except Exception: 
                pass
                
        _thumb_executor.submit(fetch_image)

# ====================================================================
# 2. (Đã bỏ Whisper) — Tách phụ đề nay dùng CapCut STT trong honggou_tab.
#    Khối get_whisper_model / generate_srt_pipeline cũ đã gỡ để không kéo
#    theo faster-whisper + ctranslate2 (nặng, không còn dùng).
# ====================================================================

def find_downloaded_video(directory: str, name_prefix: str) -> str:
    """Tìm file video đã tải trong thư mục theo tiền tố tên file.
    Dùng cho các tab mà filepath có chứa %(ext)s template của yt-dlp."""
    video_exts = (".mp4", ".mkv", ".webm", ".flv", ".avi")
    if not os.path.isdir(directory):
        return ""
    for f in os.listdir(directory):
        if f.startswith(name_prefix) and f.lower().endswith(video_exts):
            return os.path.join(directory, f)
    return ""

# ====================================================================
# 4. MẮT THẦN DÒ CARD ĐỒ HỌA (Dùng cho Workflow Render FFmpeg)
# ====================================================================
_CODEC_CACHE = None

def get_optimal_ffmpeg_codec() -> str:
    """Dò card đồ họa để chọn codec nén phần cứng. Có CACHE (chỉ dò 1 lần).
    Dùng PowerShell CIM thay 'wmic' vì Windows 11 mới đã gỡ wmic."""
    global _CODEC_CACHE
    if _CODEC_CACHE is not None:
        return _CODEC_CACHE
    if os.name != "nt":
        _CODEC_CACHE = "libx264"
        return _CODEC_CACHE

    vga_name = ""
    # 1) nvidia-smi (nhanh nhất) -> chắc chắn NVIDIA
    try:
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW, timeout=6)
        if r.returncode == 0 and "gpu" in r.stdout.lower():
            _CODEC_CACHE = "h264_nvenc"
            return _CODEC_CACHE
    except Exception:
        pass
    # 2) PowerShell CIM (thay wmic, chạy được trên Win10/11)
    try:
        ps = ("Get-CimInstance Win32_VideoController | "
              "Select-Object -ExpandProperty Name")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                          capture_output=True, text=True,
                          creationflags=CREATE_NO_WINDOW, timeout=8)
        vga_name = (r.stdout or "").lower()
    except Exception:
        vga_name = ""

    if "nvidia" in vga_name:
        _CODEC_CACHE = "h264_nvenc"
    elif "amd" in vga_name or "radeon" in vga_name:
        _CODEC_CACHE = "h264_amf"
    elif "intel" in vga_name:
        _CODEC_CACHE = "h264_qsv"
    else:
        _CODEC_CACHE = "libx264"
    return _CODEC_CACHE

# ====================================================================
# 5. DỊCH SRT SANG TIẾNG VIỆT QUA GEMINI (PLAYWRIGHT HEADLESS)
#    Trích xuất từ translate_tab.py để tái sử dụng trong pipeline download.
#    Dùng chung Chrome Profile "BoomStudio_ChromeData" để xài session đã login.
# ====================================================================
_GEMINI_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
_GEMINI_BROWSER_ARGS = ["--disable-blink-features=AutomationControlled", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", "--disable-software-rasterizer"]
_GEMINI_INPUT_SELS = ["rich-textarea div.ql-editor[contenteditable='true']", "div[contenteditable='true'][role='textbox']"]
_GEMINI_SEND_SELS = ["button[aria-label='Send message']", "button[aria-label='Gửi']", "button.send-button"]
_GEMINI_RESP_SELS = [".model-response-text .markdown", "message-content .markdown", "[data-message-author-role='model']"]

_translate_lock = threading.Lock()

def _gemini_find_el(page, sels, timeout=3000, cancel_check=None):
    for s in sels:
        try:
            step = 500
            for _ in range(timeout // step):
                if cancel_check and cancel_check(): return None
                el = page.query_selector(s)
                if el and el.is_visible(): return el
                page.wait_for_timeout(step)
        except Exception: continue
    return None

def _gemini_send_and_wait(page, prompt_message, expected_min_lines=None, cancel_check=None):
    """Gửi prompt lên Gemini và chờ phản hồi ổn định."""
    try:
        page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        inp = _gemini_find_el(page, _GEMINI_INPUT_SELS, timeout=5000, cancel_check=cancel_check)
        if cancel_check and cancel_check(): return "ERROR: Cancelled"
        if not inp: return "ERROR: Không thấy ô nhập Gemini. Có thể bị dính CAPTCHA hoặc chưa đăng nhập."
        inp.click()
        page.evaluate('''(text) => {
            const el = document.activeElement?.contentEditable === "true" ? document.activeElement : document.querySelector("[contenteditable='true']");
            if (el) { el.focus(); el.innerText = text; el.dispatchEvent(new Event('input', {bubbles: true})); }
        }''', prompt_message)
        page.wait_for_timeout(300)
        page.keyboard.press("End"); page.keyboard.press("Space"); page.wait_for_timeout(300)
        btn = _gemini_find_el(page, _GEMINI_SEND_SELS, timeout=2000, cancel_check=cancel_check)
        if btn:
            try: btn.click()
            except Exception: page.keyboard.press("Enter")
        else: page.keyboard.press("Enter")
        
        prev, stable = "", 0
        for _ in range(720):
            if cancel_check and cancel_check(): return "ERROR: Cancelled"
            page.wait_for_timeout(500)
            cur = ""
            for s in _GEMINI_RESP_SELS:
                try:
                    els = page.query_selector_all(s)
                    if els and els[-1].inner_text().strip(): cur = els[-1].inner_text().strip(); break
                except Exception: continue
            if cur and cur == prev:
                stable += 1
                required_stable = 8
                if expected_min_lines:
                    got_lines = len([l for l in cur.split('\n') if l.strip()])
                    if got_lines < expected_min_lines: required_stable = 30
                if stable >= required_stable: return cur
            else: stable = 0; prev = cur
        return prev if prev else "ERROR: Quá thời gian chờ Gemini"
    except Exception as e: return f"ERROR: {e}"

def _parse_srt_blocks(content):
    """Parse nội dung SRT thành danh sách block {stt, time, text}."""
    blocks = []
    pattern = r"(?m)^(\d+)\s*\n(\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3})\s*\n([\s\S]+?)(?=\n\s*\n|\Z)"
    for match in re.finditer(pattern, content.strip()):
        blocks.append({"stt": match.group(1).strip(), "time": match.group(2).strip(), "text": match.group(3).strip().replace('\n', ' ')})
    return blocks

def translate_srt_to_vietnamese(srt_path: str, log_fn=None, cancel_check=None):
    """
    Dịch 1 file SRT sang tiếng Việt qua Gemini (Playwright headless).
    Dùng lock để chỉ cho 1 luồng dịch tại 1 thời điểm (Playwright không hỗ trợ đa luồng).
    Trả về đường dẫn file _vi.srt nếu thành công, None nếu thất bại.
    """
    if not os.path.exists(srt_path):
        if log_fn: log_fn(f"❌ File SRT không tồn tại: {srt_path}\n")
        return None

    with _translate_lock:
        return _translate_srt_locked(srt_path, log_fn, cancel_check)

def _translate_srt_locked(srt_path, log_fn=None, cancel_check=None):
    """Logic dịch chính (chỉ chạy trong lock)."""
    base_name = os.path.basename(srt_path)
    vi_path = os.path.splitext(srt_path)[0] + "_vi.srt"
    
    with open(srt_path, "r", encoding="utf-8-sig") as f:
        srt_content = f.read()
    blocks = _parse_srt_blocks(srt_content)
    if not blocks:
        if log_fn: log_fn(f"⚠️ File SRT trống hoặc sai định dạng: {base_name}\n")
        return None

    # Tìm Chrome Profile
    base_dir = os.path.dirname(os.path.abspath(__file__))
    tool_profile = os.path.join(base_dir, "BoomStudio_ChromeData")
    
    # Kiểm tra đăng nhập
    auth_file = os.path.join(base_dir, "gemini_auth.json")
    if not os.path.exists(auth_file):
        if log_fn: log_fn("❌ Chưa đăng nhập Gemini! Hãy vào tab Dịch Thuật và nhấn Auth trước.\n")
        return None

    ctx = None
    pw_instance = None
    try:
        from playwright.sync_api import sync_playwright
        pw_instance = sync_playwright().start()
        
        # Mở Chrome headless với Profile đã login
        launch_err = None
        for attempt in range(3):
            try:
                ctx = pw_instance.chromium.launch_persistent_context(
                    tool_profile,
                    **browser_launch_kwargs(
                        headless=True,
                        user_agent=_GEMINI_UA, viewport={"width": 1280, "height": 900},
                        args=_GEMINI_BROWSER_ARGS
                    )
                )
                launch_err = None; break
            except Exception as e:
                launch_err = e; ctx = None
                if attempt < 2:
                    if log_fn: log_fn(f"⚠️ Chrome bận (lần {attempt+1}/3), chờ 3s...\n")
                    import time as _t; _t.sleep(3)
        if ctx is None:
            if log_fn: log_fn(f"❌ Không mở được Chrome: {launch_err}\n")
            return None
        
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        
        if log_fn: log_fn(f"🌐 Đã mở Chrome ngầm, bắt đầu dịch {base_name}...\n")
        
        # === BƯỚC 1: TRINH SÁT BỐI CẢNH ===
        sample_text = "\n".join([b["text"] for b in blocks[:150]])
        context_prompt = (
            "Đọc kịch bản sau và trả lời NGẮN GỌN:\n"
            "1. Phim này thuộc thể loại gì?\n"
            "2. Nhận diện nhân vật và cách xưng hô chuẩn tiếng Việt?\n\n"
            "TUYỆT ĐỐI KHÔNG DỊCH VĂN BẢN. CHỈ TRẢ VỀ TÓM TẮT (4-5 dòng).\n"
            f"Văn bản:\n{sample_text}"
        )
        if log_fn: log_fn("🔍 AI đang phân tích bối cảnh & nhân vật...\n")
        context_res = _gemini_send_and_wait(page, context_prompt, cancel_check=cancel_check)
        if "ERROR" in context_res:
            context_res = "Không thể phân tích bối cảnh. Dịch theo mặc định."
        clean_ctx = re.sub(r'```[a-zA-Z]*\n?', '', context_res).replace('```', '').strip()
        if log_fn: log_fn(f"🧠 Bối cảnh: {clean_ctx[:120]}...\n")
        
        # === BƯỚC 2: DỊCH THEO KHỐI ===
        chunk_size = 80
        chunks = [blocks[i:i + chunk_size] for i in range(0, len(blocks), chunk_size)]
        translated = {}
        
        preset = "Bạn là biên dịch viên phim chuyên nghiệp. Dịch sát nghĩa, mượt mà. Tuân thủ nghiêm ngặt bối cảnh và xưng hô đã phân tích."
        
        for ci, chunk in enumerate(chunks):
            if cancel_check and cancel_check(): break
            
            chunk_to_translate = chunk[:]
            translated_lines = []
            max_retries = 5
            retry_count = 0
            
            while len(chunk_to_translate) > 0 and retry_count < max_retries:
                if cancel_check and cancel_check(): break
                
                lines_to_translate = [b["text"] for b in chunk_to_translate]
                text_payload = "\n".join(lines_to_translate)
                
                rules = f"""QUY TẮC TUYỆT ĐỐI:
1. BẮT BUỘC trả về ĐÚNG {len(lines_to_translate)} dòng. Không gộp, không tách.
2. KHÔNG giải thích, KHÔNG CHÀO HỎI. CHỈ TRẢ VỀ NỘI DUNG DỊCH.
3. DỊCH 100% SANG TIẾNG VIỆT, KHÔNG ĐỂ SÓT CHỮ HÁN/TRUNG.
4. ÁP DỤNG BỐI CẢNH:
---
{clean_ctx}
---"""
                final_prompt = f"{preset}\n\n{rules}\n\nDịch {len(lines_to_translate)} dòng sau:\n{text_payload}"
                
                if log_fn: log_fn(f"⏳ Dịch khối {ci+1}/{len(chunks)} ({len(lines_to_translate)} câu)...\n")
                
                c_res = _gemini_send_and_wait(page, final_prompt, expected_min_lines=len(lines_to_translate), cancel_check=cancel_check)
                
                if c_res.startswith("ERROR"):
                    if log_fn: log_fn(f"⚠️ Lỗi: {c_res[:80]}... Thử lại...\n")
                    try: page.close(); page = ctx.new_page()
                    except Exception: pass
                    retry_count += 1; continue
                
                # Làm sạch
                res_clean = re.sub(r'```[a-zA-Z]*\n?', '', c_res).replace('```', '').replace('*', '')
                temp_lines = [l.strip() for l in res_clean.split('\n') if l.strip()]
                
                # Chặt câu chào hỏi
                while temp_lines:
                    first = temp_lines[0].lower()
                    if any(kw in first for kw in ["dạ,", "dạ ", "đây là bản", "bản dịch", "dưới đây là", "chắc chắn", "tất nhiên"]):
                        temp_lines.pop(0)
                    else: break
                
                if len(temp_lines) == 0:
                    try: page.close(); page = ctx.new_page()
                    except Exception: pass
                    retry_count += 1; continue
                
                if len(temp_lines) < len(chunk_to_translate):
                    translated_lines.extend(temp_lines)
                    chunk_to_translate = chunk_to_translate[len(temp_lines):]
                    try: page.close(); page = ctx.new_page()
                    except Exception: pass
                    retry_count = 0
                elif len(temp_lines) > len(chunk_to_translate):
                    translated_lines.extend(temp_lines[:len(chunk_to_translate)])
                    chunk_to_translate = []
                else:
                    translated_lines.extend(temp_lines)
                    chunk_to_translate = []
            
            # Bù phần thiếu bằng bản gốc
            if len(chunk_to_translate) > 0:
                for b in chunk_to_translate:
                    translated_lines.append(b["text"])
            
            for j, b in enumerate(chunk):
                if j < len(translated_lines):
                    translated[b["stt"]] = translated_lines[j]
            
            # Delay giữa các khối
            if ci < len(chunks) - 1 and not (cancel_check and cancel_check()):
                page.wait_for_timeout(1000)
        
        # === BƯỚC 3: XUẤT FILE _vi.srt ===
        final_srt = ""
        for b in blocks:
            text_vi = translated.get(b["stt"], b["text"])
            final_srt += f"{b['stt']}\n{b['time'].replace('.', ',')}\n{text_vi}\n\n"
        
        with open(vi_path, "w", encoding="utf-8") as f:
            f.write(final_srt.strip() + "\n")
        
        if log_fn: log_fn(f"✅ Đã lưu bản dịch: {os.path.basename(vi_path)}\n")
        return vi_path
        
    except ImportError:
        if log_fn: log_fn("❌ Chưa cài Playwright! Chạy: pip install playwright && playwright install chromium\n")
        return None
    except Exception as e:
        if log_fn: log_fn(f"❌ Lỗi dịch thuật: {e}\n")
        return None
    finally:
        try:
            if ctx: ctx.close()
            if pw_instance: pw_instance.stop()
        except Exception: pass
