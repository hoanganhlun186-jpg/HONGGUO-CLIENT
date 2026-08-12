"""
═══════════════════════════════════════════════════════════
  TRANSLATE TAB — Dịch phụ đề (AI TRINH SÁT BỐI CẢNH)
  ─────────────────────────────────────────────────────────
  Tự động quét bối cảnh -> Cập nhật lên UI -> Dịch Real-time
═══════════════════════════════════════════════════════════
"""
import os, re, glob, shutil, time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QTextEdit, QFileDialog, QProgressBar,
    QFrame, QSplitter, QComboBox, QAbstractItemView, QMessageBox,
    QDialog, QTableWidget, QTableWidgetItem, QHeaderView, QSpinBox,
    QLineEdit, QCheckBox
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QSettings, QSize
from PyQt6.QtGui import QTextCursor, QBrush, QColor, QFont

import deepseek_translate as dst  # module dịch DeepSeek V4 Pro (giữ ngữ cảnh xuyên suốt)

# Ưu tiên Chrome khách, thiếu thì tự lùi về Chromium (dùng chung toàn app)
try:
    from shared_utils import browser_launch_kwargs
except Exception:
    def browser_launch_kwargs(headless=True, args=None, **extra):
        kw = dict(extra); kw["headless"] = headless
        if args is not None: kw["args"] = args
        kw["channel"] = "chrome"   # fallback: giữ hành vi cũ
        return kw

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
AUTH_FILE = "gemini_auth.json"
BROWSER_ARGS = ["--disable-blink-features=AutomationControlled", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", "--disable-software-rasterizer"]

# ============================================================
# BỘ QUY TẮC DỊCH THUẬT (PROMPT PRESETS ĐA DẠNG)
# ============================================================
PROMPT_PRESETS = {
    "🌟 SUPER VIP: Tối ưu Dubbing AI (Mọi Bối Cảnh)": """Bạn là chuyên gia biên dịch phim và viết kịch bản lồng tiếng (Dubbing). Nhiệm vụ của bạn là dịch mượt mà, tự nhiên và tối ưu tuyệt đối cho giọng đọc AI (Text-to-Speech).
YÊU CẦU DỊCH THUẬT TỐI ƯU:
1. Súc tích tối đa (Quy tắc 3 giây): Rút ngắn số lượng từ 20-30% so với gốc để tránh lố nhịp audio. Lược bỏ từ đệm (đã, đang, sẽ, những, các...) và bỏ bớt đại từ/chủ ngữ nếu ngữ cảnh đã rõ.
2. Thích nghi văn phong (Cực kỳ quan trọng): Tự động điều chỉnh tỷ lệ từ Hán Việt, thuần Việt hoặc tiếng lóng dựa CHÍNH XÁC vào bối cảnh (cổ trang, hiện đại, hoặc xuyên không). Xử lý mượt sự giao thoa ngôn ngữ nếu là phim xuyên không.
3. Tối ưu câu chữ Audio: Thay thế các cụm thuần Việt dài dòng bằng từ súc tích (VD: 'người làm cho tôi' -> 'thuộc hạ'). Văn phong gãy gọn như người thật đang nói chuyện. Phiên âm tên riêng chuẩn Hán Việt.
4. Xử lý điểm mù: Tuyệt đối không dịch word-by-word. Nếu mơ hồ chủ thể, dùng cách diễn đạt trung tính thay vì đoán bừa.""",
    "1. Tiên Hiệp / Huyền Huyễn (Tu tiên)": "Bạn là dịch giả truyện Tiên Hiệp. Dịch sang tiếng Việt, ƯU TIÊN dùng từ Hán Việt (đạo hữu, bổn tọa, tại hạ, tông môn, sư tôn, sư muội...).",
    "2. Hào Môn / Ngôn Tình (Tổng tài)": "Bạn là dịch giả truyện Ngôn Tình. Dịch với giọng văn bá đạo, sến súa hoặc lạnh lùng (hắn, cô ta, thiếu gia, phu nhân, bảo bối...).",
    "3. Giang Hồ / Xã Hội Đen (Hành động)": "Bạn là dịch giả phim Xã Hội Đen. Dịch dùng từ lóng, xưng hô giang hồ (đại ca, lão đại, sếp, tao/mày, anh/chú, tụi bấy...).",
    "4. Đô Thị / Thanh Xuân (Đời thường)": "Bạn là dịch giả phim thanh xuân vườn trường/đời thường. Xưng hô tự nhiên, gần gũi, trẻ trung (anh/em, cậu/tớ, mày/tao, ba/mẹ...).",
    "5. Cổ Trang / Cung Đấu (Lịch sử)": "Bạn là dịch giả phim Cổ Trang. Xưng hô chuẩn cung đình (hoàng thượng, thần thiếp, vi thần, nương nương, nô tài, trẫm, bệ hạ...).",
    "6. Kinh Dị / Trinh Thám (Phá án)": "Bạn là dịch giả phim Trinh Thám. Giọng văn lạnh lùng, logic, sắc bén, hồi hộp và đầy kịch tính.",
    "7. Hài Hước / Meme (Mạng xã hội)": "Bạn là dịch giả video hài hước. Dùng nhiều từ lóng trend mạng xã hội hiện nay, giọng điệu cợt nhả, vui nhộn, xéo xắt.",
    "8. Review Phim (Đọc nhanh)": "Bạn là người viết kịch bản Review Phim. Dịch cực kỳ ngắn gọn, súc tích, cắt bỏ từ rườm rà, nhịp điệu dồn dập, gãy gọn.",
    "9. Khoa Học Viễn Tưởng (Sci-fi)": "Bạn là dịch giả phim viễn tưởng. Sử dụng thuật ngữ công nghệ, không gian, vũ trụ chuẩn xác, giọng văn trung lập, máy móc."
}

# ============================================================
# HÀM TIỆN ÍCH PLAYWRIGHT
# ============================================================
_INPUT_SELS = ["rich-textarea div.ql-editor[contenteditable='true']", "div[contenteditable='true'][role='textbox']"]
_SEND_SELS = ["button[aria-label='Send message']", "button[aria-label='Gửi']", "button.send-button"]
_RESP_SELS = [".model-response-text .markdown", "message-content .markdown", "[data-message-author-role='model']"]

def _find_el(page, sels, timeout=3000, cancel_check=None):
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

def _select_model(page, model_key, log_fn=None):
    if not model_key or model_key == "Auto (Mặc định)": return
    try:
        opened = page.evaluate('''() => {
            const btn = document.querySelector('[data-test-id="logs-pill-label-container"]') || 
                        document.querySelector('button[aria-haspopup="true"]');
            if (btn) { btn.click(); return true; }
            return false;
        }''')
        if not opened: return
        page.wait_for_timeout(1000)
        found = page.evaluate('''(targetModel) => {
            const items = document.querySelectorAll("[role='option'], [role='menuitem'], [role='menuitemradio'], li");
            for (const el of items) {
                const txt = (el.innerText || el.textContent || "").trim();
                if (txt.toLowerCase().includes(targetModel.toLowerCase())) {
                    el.click(); return txt.split('\\n')[0];
                }
            }
            return null;
        }''', model_key)
        if found and log_fn: log_fn(f"🔧 Đã kích hoạt model: {found}\n")
    except Exception as e:
        if log_fn: log_fn(f"⚠️ Không chọn được model {model_key}: {e}\n")

# ============================================================
# THREAD THAO TÁC ĐĂNG NHẬP
# ============================================================
class GoogleManualLoginThread(QThread):
    log = pyqtSignal(str)
    models_found = pyqtSignal(list)
    finished_signal = pyqtSignal(bool)
    
    def run(self):
        self.log.emit("\n" + "═" * 55 + "\n  🔑 ĐANG MỞ GOOGLE CHROME (PROFILE TOOL ĐỘC LẬP)\n" + "═" * 55 + "\n")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                base_dir = os.path.dirname(os.path.abspath(__file__))
                tool_profile_path = os.path.join(base_dir, "BoomStudio_ChromeData")
                
                ctx = p.chromium.launch_persistent_context(
                    tool_profile_path,
                    **browser_launch_kwargs(
                        headless=False,
                        user_agent=UA,
                        viewport={"width": 1280, "height": 900},
                        args=["--disable-blink-features=AutomationControlled"]
                    )
                )
                
                ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=60000)
                
                logged_in = False
                self.log.emit("⏳ Trình duyệt đang chạy. Nếu chưa đăng nhập, vui lòng thực hiện trên màn hình...\n")

                for _ in range(100):
                    if self.isInterruptionRequested(): break
                    try:
                        is_guest = page.evaluate('''() => {
                            const btns = Array.from(document.querySelectorAll('a, button, span'));
                            return btns.some(el => {
                                const txt = (el.innerText || "").trim().toLowerCase();
                                return txt === 'sign in' || txt === 'đăng nhập';
                            });
                        }''')
                        has_chatbox = page.query_selector("rich-textarea div.ql-editor") or page.query_selector("div[contenteditable='true'][role='textbox']")
                        if not is_guest and has_chatbox: logged_in = True; break
                    except Exception: pass
                    page.wait_for_timeout(3000)
                
                if logged_in:
                    ctx.storage_state(path=AUTH_FILE)
                    self.log.emit(f"✅ Đăng nhập hợp lệ. Bắt đầu quét menu phiên bản...\n")
                    page.wait_for_timeout(2000)
                    
                    page.evaluate('''() => {
                        const btn = document.querySelector('[data-test-id="logs-pill-label-container"]') || document.querySelector('button[aria-haspopup="true"]');
                        if (btn) btn.click();
                    }''')
                    page.wait_for_timeout(1200)
                    
                    models_list = page.evaluate('''() => {
                        const list = [];
                        const options = document.querySelectorAll("[role='menuitemradio'], [role='option'], [role='menuitem'], li");
                        for (const opt of options) {
                            const txt = (opt.innerText || "").split('\\n')[0].trim();
                            if (txt && txt.length > 2 && !list.includes(txt)) list.push(txt);
                        }
                        return list;
                    }''')
                    page.keyboard.press("Escape")
                    
                    if models_list:
                        self.log.emit(f"🌟 Đã tìm thấy {len(models_list)} phiên bản khả dụng!\n")
                        self.models_found.emit(models_list)
                    else:
                        self.log.emit("⚠️ Không cào được menu. Sẽ sử dụng danh sách mặc định.\n")
                    self.finished_signal.emit(True)
                else:
                    self.log.emit("❌ Quá thời gian đăng nhập hoặc thất bại.\n")
                    self.finished_signal.emit(False)
                    
                ctx.close()
        except Exception as e:
            self.log.emit(f"❌ Lỗi: {e} (Hãy chắc chắn bạn đã cài đặt Google Chrome trên máy tính)\n"); self.finished_signal.emit(False)

_TIMESTAMP_LINE_RE = re.compile(r'^\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}$')

def _count_real_lines(text):
    """Đếm số dòng THẬT (không tính rác Gemini hay tự chèn thêm: số thứ tự
    trần trụi, dòng timestamp tự bịa). Dùng chung cho cả bước chờ Gemini
    dịch xong (_send_and_wait) lẫn bước ghép kết quả (_translate_smart) -
    tránh 2 nơi đếm khác kiểu gây lệch pha."""
    count = 0
    for l in text.split('\n'):
        s = l.strip()
        if not s: continue
        if s.isdigit(): continue
        if _TIMESTAMP_LINE_RE.match(s): continue
        count += 1
    return count

class GeminiTranslateThread(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    context_extracted = pyqtSignal(int, str)
    chunk_done = pyqtSignal(int, dict)
    item_done = pyqtSignal(int, str, str)
    item_failed = pyqtSignal(int, str)
    all_done = pyqtSignal()
    
    def __init__(self, queue_items, prompt_preset_key, model_key, chunk_size=100, translate_workers=1, show_browser=False):
        super().__init__()
        self.queue_items = list(queue_items)
        self.preset_text = PROMPT_PRESETS.get(prompt_preset_key, list(PROMPT_PRESETS.values())[0])
        self.model_key = model_key
        self.chunk_size = chunk_size
        self.translate_workers = max(1, min(4, int(translate_workers)))
        # Hiện trình duyệt Chrome khi dịch (để soi Gemini chạy) hay chạy ẩn.
        self.show_browser = bool(show_browser)
        self._cancel = False
        
    def cancel(self): self._cancel = True
    
    def _parse_srt(self, content):
        blocks = []
        pattern = r"(?m)^(\d+)\s*\n(\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3})\s*\n([\s\S]+?)(?=\n\s*\n|\Z)"
        for match in re.finditer(pattern, content.strip()):
            blocks.append({
                "stt": match.group(1).strip(), 
                "time": match.group(2).strip(), 
                "text": match.group(3).strip().replace('\n', ' ')
            })
        return blocks

    def run(self):
        if not os.path.exists(AUTH_FILE):
            self.log.emit("❌ Lỗi: Bạn chưa Đăng nhập Google.\n"); self.all_done.emit(); return
        if self.translate_workers <= 1:
            self._run_sequential()
        else:
            self._run_parallel()

    def _run_sequential(self):
        """Chế độ CŨ (1 Chrome, dịch tuần tự từng tập) - giữ nguyên hành vi
        mặc định khi khách để 'Số tập dịch song song' = 1, tránh rủi ro hồi
        quy cho trường hợp phổ biến nhất. Bối cảnh vẫn chỉ phân tích 1 lần
        (dùng tập đầu tiên làm mẫu) rồi tái sử dụng cho các tập sau, thay vì
        mỗi tập tự phân tích riêng như bản cũ - tiết kiệm thời gian."""
        total = len(self.queue_items)
        done = 0
        ctx, pw = None, None
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            
            base_dir = os.path.dirname(os.path.abspath(__file__))
            tool_profile_path = os.path.join(base_dir, "BoomStudio_ChromeData")
            
            launch_err = None
            for attempt in range(3):
                try:
                    ctx = pw.chromium.launch_persistent_context(
                        tool_profile_path,
                        **browser_launch_kwargs(
                            headless=not self.show_browser,
                            user_agent=UA,
                            viewport={"width": 1280, "height": 900},
                            args=BROWSER_ARGS
                        )
                    )
                    launch_err = None
                    break
                except Exception as e:
                    launch_err = e
                    ctx = None
                    if attempt < 2:
                        self.log.emit(
                            f"⚠️ Mở Chrome thất bại (lần {attempt+1}/3): {e}\n"
                            f"⏳ Đợi 3s rồi thử lại...\n"
                        )
                        import time as _time
                        _time.sleep(3)
            if ctx is None:
                raise RuntimeError(f"Không thể mở Chrome sau 3 lần thử: {launch_err}\n=> Hãy mở Task Manager và End Task các tiến trình 'chrome.exe' đang chạy ngầm rồi thử lại.")
            
            ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            self.log.emit("🌐 Đã khởi tạo trình duyệt Chrome ngầm (Standalone Profile).\n")
            
            clean_ctx = None
            for idx, item in enumerate(self.queue_items):
                if self._cancel: break
                video_path, srt_path = item["video"], item["srt"]
                base = os.path.basename(srt_path)
                self.log.emit(f"\n{'='*50}\n📄 [{idx+1}/{total}] Đang xử lý: {base}\n")
                try:
                    if clean_ctx is None:
                        clean_ctx = self._extract_shared_context(page, self._context_sample_paths())
                    self._translate_smart(clean_ctx, page, idx, video_path, srt_path)
                except Exception as e: 
                    self.item_failed.emit(idx, str(e))
                done += 1
                self.progress.emit(done)
        except Exception as e:
            self.log.emit(f"❌ Lỗi nghiêm trọng trong luồng dịch thuật: {e}\n")
        finally:
            try:
                if ctx: ctx.close()
                if pw: pw.stop()
            except Exception: pass
        self.log.emit(f"\n🏁 XONG CHIẾN DỊCH.\n")
        self.all_done.emit()

    def _launch_authenticated_context(self, pw):
        """Mở 1 Chrome ẨN ĐỘC LẬP (không dùng chung profile bị khóa), đăng
        nhập sẵn bằng storage_state đã lưu (AUTH_FILE) từ lúc đăng nhập ban
        đầu. Mỗi luồng dịch song song gọi hàm này để có Chrome RIÊNG, không
        đụng độ luồng khác (Playwright sync API không an toàn khi dùng chung
        giữa nhiều luồng, và Chrome persistent profile không cho 2 tiến
        trình cùng mở 1 lúc)."""
        browser = pw.chromium.launch(**browser_launch_kwargs(headless=not self.show_browser, args=BROWSER_ARGS))
        ctx = browser.new_context(
            storage_state=AUTH_FILE,
            user_agent=UA,
            viewport={"width": 1280, "height": 900}
        )
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        return browser, ctx

    def _run_parallel(self):
        """Dịch NHIỀU TẬP CÙNG LÚC (self.translate_workers luồng, mỗi luồng
        1 Chrome ẩn riêng biệt). Bối cảnh chỉ phân tích 1 LẦN DUY NHẤT (dùng
        tập đầu tiên trong hàng đợi làm mẫu), áp dụng chung cho mọi tập dịch
        song song phía sau."""
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed

        total = len(self.queue_items)
        done = 0
        done_lock = threading.Lock()

        try:
            from playwright.sync_api import sync_playwright

            # BƯỚC 1: Lấy bối cảnh 1 LẦN, dùng tập đầu tiên trong hàng đợi làm mẫu.
            pw_main = sync_playwright().start()
            browser_main = None
            try:
                browser_main, ctx_main = self._launch_authenticated_context(pw_main)
                page_main = ctx_main.new_page()
                self.log.emit("🌐 Đã khởi tạo trình duyệt Chrome ngầm (lấy bối cảnh chung).\n")
                clean_ctx = self._extract_shared_context(page_main, self._context_sample_paths())
            finally:
                try:
                    if browser_main: browser_main.close()
                except Exception: pass
                try: pw_main.stop()
                except Exception: pass

            if self._cancel:
                self.all_done.emit(); return

            # BƯỚC 2: Dịch song song self.translate_workers tập cùng lúc.
            self.log.emit(f"🚀 Dịch song song {self.translate_workers} tập/lúc ({total} tập trong hàng đợi)...\n")

            def _worker(idx, item):
                nonlocal done
                if self._cancel: return
                video_path, srt_path = item["video"], item["srt"]
                base = os.path.basename(srt_path)
                self.log.emit(f"\n{'='*50}\n📄 [{idx+1}/{total}] Đang xử lý (song song): {base}\n")

                # KHỞI ĐỘNG LẠI CHROME NGẦM KHI ĐƠ: mỗi tập được thử tối đa
                # MAX_BROWSER_RESTART lần. Mỗi lần thất bại (Gemini đơ / Chrome
                # treo / mất kết nối) sẽ ĐÓNG SẠCH browser cũ rồi MỞ browser MỚI
                # hoàn toàn để dịch lại tập đó — không tái dùng phiên Chrome đã
                # hỏng. Nếu vẫn thất bại sau các lần thử, đánh dấu tập lỗi và
                # ĐI TIẾP (không để 1 tập treo làm đứng cả hàng đợi).
                MAX_BROWSER_RESTART = 3
                ok_this = False
                last_err = ""
                for attempt in range(1, MAX_BROWSER_RESTART + 1):
                    if self._cancel: return
                    if attempt > 1:
                        self.log.emit(
                            f"🔁 [{idx+1}/{total}] Gemini đơ — khởi động lại Chrome ngầm "
                            f"(lần {attempt}/{MAX_BROWSER_RESTART})...\n")
                    pw_w = None
                    browser_w = None
                    try:
                        pw_w = sync_playwright().start()
                        browser_w, ctx_w = self._launch_authenticated_context(pw_w)
                        page_w = ctx_w.new_page()
                        # _translate_smart tự phát tín hiệu item_done/item_failed.
                        # Nếu Gemini đơ, nó sẽ ném lỗi (hoặc trả kết quả lỗi) ->
                        # nhảy xuống except/vòng lặp để restart browser.
                        self._translate_smart(clean_ctx, page_w, idx, video_path, srt_path)
                        ok_this = True
                        break
                    except Exception as e:
                        last_err = str(e)
                        self.log.emit(f"⚠️ [{idx+1}/{total}] Lỗi khi dịch: {last_err[:120]}\n")
                    finally:
                        # Đóng SẠCH browser + playwright của lần thử này, kể cả
                        # khi đang treo, để lần sau mở phiên hoàn toàn mới.
                        try:
                            if browser_w: browser_w.close()
                        except Exception: pass
                        try:
                            if pw_w: pw_w.stop()
                        except Exception: pass
                    if not self._cancel and attempt < MAX_BROWSER_RESTART:
                        time.sleep(3)

                if not ok_this and not self._cancel:
                    self.item_failed.emit(idx, f"Gemini đơ sau {MAX_BROWSER_RESTART} lần khởi động lại. {last_err[:120]}")
                    self.log.emit(
                        f"❌ [{idx+1}/{total}] Bỏ qua tập này sau {MAX_BROWSER_RESTART} lần thử. "
                        f"Các tập khác vẫn chạy tiếp.\n")

                with done_lock:
                    done += 1
                    self.progress.emit(done)

            with ThreadPoolExecutor(max_workers=self.translate_workers) as ex:
                futs = [ex.submit(_worker, idx, item) for idx, item in enumerate(self.queue_items)]
                for fut in as_completed(futs):
                    if self._cancel: break
                    try: fut.result()
                    except Exception as e:
                        self.log.emit(f"⚠️ Lỗi luồng: {e}\n")

        except Exception as e:
            self.log.emit(f"❌ Lỗi nghiêm trọng trong luồng dịch thuật: {e}\n")

        self.log.emit(f"\n🏁 XONG CHIẾN DỊCH.\n")
        self.all_done.emit()

    # Số tập đầu hàng đợi dùng làm mẫu phân tích bối cảnh dùng chung.
    CONTEXT_SAMPLE_EPISODES = 3
    # Giới hạn số dòng gộp lại khi lấy mẫu (an toàn nếu lỡ có tập dài bất thường).
    CONTEXT_MAX_LINES = 400

    def _extract_shared_context(self, page, sample_srt_paths):
        """Phân tích bối cảnh (thể loại + văn phong + xưng hô + thuật ngữ)
        DÙNG CHUNG cho toàn bộ hàng đợi. Chỉ chạy 1 LẦN DUY NHẤT.

        Nhận vào DANH SÁCH đường dẫn srt (thường là 3 tập đầu hàng đợi).
        Gộp nội dung các tập đó làm mẫu để bối cảnh sát hơn (bắt được nhiều
        nhân vật, xưng hô ổn định hơn so với chỉ 1 tập). Tự co giãn: có bao
        nhiêu tập trong danh sách thì dùng bấy nhiêu (1, 2 hay 3 đều chạy).

        Tương thích ngược: nếu lỡ truyền vào 1 chuỗi path đơn (str) thay vì
        list, vẫn xử lý được như 1 tập."""
        # Chấp nhận cả str (1 tập) lẫn list (nhiều tập) cho an toàn.
        if isinstance(sample_srt_paths, str):
            sample_srt_paths = [sample_srt_paths]
        sample_srt_paths = [p for p in (sample_srt_paths or []) if p]

        if not sample_srt_paths:
            return "Không thể phân tích bối cảnh. Hệ thống sẽ dịch theo mặc định."

        # ── Gộp nội dung tối đa CONTEXT_SAMPLE_EPISODES tập đầu làm mẫu ──
        parts = []
        used = 0
        total_lines = 0
        for ep_i, sp in enumerate(sample_srt_paths[:self.CONTEXT_SAMPLE_EPISODES]):
            try:
                with open(sp, "r", encoding="utf-8-sig") as f:
                    srt_content = f.read()
            except Exception as e:
                self.log.emit(f"⚠️ Không đọc được tập mẫu {os.path.basename(str(sp))}: {e}\n")
                continue
            blocks = self._parse_srt(srt_content)
            if not blocks:
                continue
            ep_text = "\n".join(b["text"] for b in blocks)
            parts.append(f"[TẬP {ep_i + 1}]\n{ep_text}")
            used += 1
            total_lines += len(blocks)

        if used == 0:
            return "Không thể phân tích bối cảnh. Hệ thống sẽ dịch theo mặc định."

        sample_text = "\n\n".join(parts)
        # Chốt chặn an toàn: nếu gộp lại quá dài thì cắt bớt (phim ngắn 60-80
        # dòng/tập thì gần như không bao giờ chạm ngưỡng này).
        sample_lines = sample_text.split("\n")
        if len(sample_lines) > self.CONTEXT_MAX_LINES:
            sample_text = "\n".join(sample_lines[:self.CONTEXT_MAX_LINES])

        context_prompt = (
            f"Dưới đây là phụ đề {used} tập đầu của một bộ phim ngắn Trung Quốc. "
            "Hãy đọc và rút ra HỒ SƠ BỐI CẢNH để dịch cả bộ cho NHẤT QUÁN. "
            "Chỉ trả về đúng 4 mục sau, ngắn gọn, KHÔNG dịch toàn bộ phụ đề:\n\n"
            "1. THỂ LOẠI & BỐI CẢNH: cổ trang / hiện đại / đô thị / tiên hiệp..., không gian - thời gian chính.\n"
            "2. VĂN PHONG: giọng phim (trang trọng hay đời thường), mức dùng Hán-Việt.\n"
            "3. NHÂN VẬT & XƯNG HÔ: liệt kê các nhân vật chính, và giữa từng cặp thì xưng hô ra sao "
            "(ví dụ: A gọi B là 'ca ca' -> 'anh'; B tự xưng 'muội' -> 'em'; hoàng thượng - thần thiếp...). "
            "Chốt cố định để cả bộ dùng thống nhất.\n"
            "4. THUẬT NGỮ RIÊNG: tên môn phái, chức tước, biệt danh, cách gọi đặc biệt + bản dịch Việt đã chốt.\n\n"
            "TUYỆT ĐỐI KHÔNG DỊCH TOÀN BỘ PHỤ ĐỀ. CHỈ TRẢ VỀ HỒ SƠ 4 MỤC TRÊN.\n\n"
            f"Phụ đề trích xuất:\n{sample_text}"
        )

        self.log.emit(
            f"🔍 Đang phân tích {used} tập đầu để lấy văn phong & xưng hô "
            f"(dùng chung cho cả hàng đợi)...\n"
        )
        context_res = self._send_and_wait(page, "Bot-Trinh-Sat", context_prompt)

        if "ERROR" in context_res:
            context_res = "Không thể phân tích bối cảnh. Hệ thống sẽ dịch theo mặc định."

        clean_ctx = re.sub(r'```[a-zA-Z]*\n?', '', context_res).replace('```', '')
        self.log.emit("🧠 Phân tích xong bối cảnh! Bắt đầu dịch...\n")
        return clean_ctx.strip()

    def _context_sample_paths(self):
        """Lấy đường dẫn srt của tối đa CONTEXT_SAMPLE_EPISODES tập ĐẦU hàng
        đợi để làm mẫu phân tích bối cảnh dùng chung."""
        paths = []
        for item in self.queue_items[:self.CONTEXT_SAMPLE_EPISODES]:
            sp = item.get("srt")
            if sp:
                paths.append(sp)
        return paths

    def _translate_smart(self, clean_ctx, page, idx, video_path, srt_path):
        with open(srt_path, "r", encoding="utf-8-sig") as f: srt_content = f.read()
        blocks = self._parse_srt(srt_content)
        if not blocks:
            self.item_failed.emit(idx, "File trống hoặc sai định dạng SRT."); return

        self.context_extracted.emit(idx, clean_ctx)

        chunks = [blocks[i:i + self.chunk_size] for i in range(0, len(blocks), self.chunk_size)]
        translated_results = {} 
        has_error = False

        for i, chunk in enumerate(chunks):
            if self._cancel: break
            
            chunk_to_translate = chunk.copy()
            translated_chunk_lines = []
            
            max_retries = 5
            retry_count = 0
            progressive_steps = 0
            
            # SỬ DỤNG batch_size CHIA LƯỢNG GỬI (TRÁNH LỖI OUT OF RANGE)
            batch_size = len(chunk_to_translate)
            
            while len(chunk_to_translate) > 0 and retry_count < max_retries and progressive_steps < 10:
                if self._cancel: break
                
                # Chỉ lấy ra đúng lượng batch_size để dịch
                current_batch = chunk_to_translate[:batch_size]
                lines_to_translate = [b["text"] for b in current_batch]
                # ĐÁNH SỐ mỗi câu dạng [n] để AI KHÔNG gộp 2 câu giống nhau
                # (VD 2 thán từ "嗯" liền nhau) và để app ghép lại ĐÚNG VỊ TRÍ
                # theo số, thay vì ghép mù theo thứ tự (dễ lệch nếu thiếu 1 dòng).
                text_payload = "\n".join(f"[{n+1}] {t}" for n, t in enumerate(lines_to_translate))
                
                # ====================================================
                # BẢN FIX: ÉP BUỘC AI PHẢI DÙNG TIẾNG VIỆT CÓ DẤU
                # ====================================================
                strict_rules = f"""QUY TẮC TUYỆT ĐỐI (VI PHẠM SẼ LỖI PHẦN MỀM):
1. MỖI dòng gốc có đánh số dạng [1], [2], [3]... BẮT BUỘC trả về ĐÚNG {len(lines_to_translate)} dòng, mỗi dòng GIỮ NGUYÊN số đó ở đầu theo định dạng: [số] bản dịch tiếng Việt. Ví dụ: "[1] Xin chào". Dịch đủ từ [1] đến [{len(lines_to_translate)}], không thiếu số nào, không gộp 2 số vào 1 dòng, kể cả khi 2 câu gốc giống hệt nhau.
2. KHÔNG giải thích, KHÔNG CHÀO HỎI. KHÔNG dùng thẻ markdown. CHỈ TRẢ VỀ các dòng "[số] nội dung".
3. BẮT BUỘC SỬ DỤNG TIẾNG VIỆT CÓ DẤU CHUẨN CHÍNH TẢ (Ví dụ: "Không", tuyệt đối không viết "Khong"). Đảm bảo giữ nguyên các dấu thanh của tiếng Việt.
4. DỊCH SẠCH 100%, KHÔNG ĐỂ SÓT LẠI KÝ TỰ HÁN/TRUNG QUỐC. KHÔNG để dòng nào trống - thán từ ngắn ("嗯","啊","哎") vẫn phải dịch ("Ừm","À","Ơ"...).
5. DỊCH SÚC TÍCH VỪA PHẢI ĐỂ GIỌNG ĐỌC TTS KHÔNG BỊ DỒN:
   - Dịch theo Ý, gọn, đủ để đọc thành tiếng thoải mái trong thời lượng câu - không dài lê thê, không cụt lủn.
   - Bỏ từ đệm/từ thừa ("thì","mà","là","rồi","đó","vậy"...) khi bỏ đi câu vẫn tự nhiên lúc ĐỌC LÊN.
   - Rút gọn là làm TỪNG CÂU ngắn lại, KHÔNG phải gộp dòng - vẫn giữ đúng số dòng ở quy tắc 1.
   - Câu phải NGHE tự nhiên như lời thoại phim, đọc trôi, không vấp.
6. ÁP DỤNG ĐÚNG BỐI CẢNH, VĂN PHONG VÀ XƯNG HÔ SAU ĐÂY VÀO BẢN DỊCH (không tự đổi xưng hô giữa chừng):
---
{clean_ctx}
---"""
                
                final_prompt = f"{self.preset_text}\n\n{strict_rules}\n\nDịch {len(lines_to_translate)} dòng sau (giữ nguyên số [n] ở đầu mỗi dòng):\n{text_payload}"
                
                if retry_count == 0:
                    if batch_size == len(chunk):
                        self.log.emit(f"⏳ Đang dịch khối {i+1}/{len(chunks)} ({len(lines_to_translate)} câu)...\n")
                    else:
                        self.log.emit(f"🔄 Nạp lại bối cảnh, mở trang mới dịch TIẾP {len(lines_to_translate)} câu bị thiếu của khối {i+1}...\n")
                else:
                    self.log.emit(f"🔄 [Cứu hộ] Thử lại khối {i+1} (Lần {retry_count}/{max_retries})...\n")
                    
                c_res = self._send_and_wait(page, f"Khối-{i+1}", final_prompt, expected_min_lines=len(lines_to_translate))
                
                if c_res.startswith("ERROR"): 
                    self.log.emit(f"⚠️ Lỗi mạng/gửi: {c_res}\n")
                    self.log.emit(f"⚙️ Đang Thoát vào lại (Mở trang mới) để reset AI...\n")
                    # KHÔNG mở tab mới (tránh tab dồn đầy Chrome gây chậm/treo).
                    # Tái dùng CHÍNH tab hiện tại: chỉ điều hướng về trang trắng
                    # để reset ngữ cảnh AI. Luôn chỉ 1 tab/luồng.
                    try: page.goto("about:blank"); page.wait_for_timeout(200)
                    except Exception: pass
                    retry_count += 1
                    time.sleep(2)
                    continue
                    
                res_clean = re.sub(r'```[a-zA-Z]*\n?', '', c_res)
                res_clean = res_clean.replace('```', '').replace('*', '')
                temp_lines_raw = [l.strip() for l in res_clean.split('\n') if l.strip()]
                
                # Phẫu thuật sub
                while temp_lines_raw:
                    first_line = temp_lines_raw[0].strip().lower()
                    forbidden_starts = ("dạ,", "dạ ", "vâng", "đây là bản", "bản dịch", "dưới đây là", "chắc chắn", "tất nhiên", "theo yêu cầu")
                    
                    if first_line.startswith(forbidden_starts):
                        self.log.emit(f"✂️ Đã chặt bỏ câu chào hỏi thừa của AI: '{temp_lines_raw[0]}'\n")
                        temp_lines_raw.pop(0)
                    else:
                        break
                        
                # ── GHÉP KẾT QUẢ THEO SỐ [n] ──────────────────────────────
                # AI trả về dạng "[n] bản dịch". Tách số n để đặt bản dịch vào
                # ĐÚNG vị trí thứ n, thay vì ghép mù theo thứ tự dòng (dễ lệch
                # nếu AI thiếu/thừa 1 dòng ở giữa). Nhờ đó biết CHÍNH XÁC dòng
                # nào bị thiếu.
                n_expected = len(current_batch)
                slots = [None] * n_expected      # slots[k] = bản dịch của câu thứ k+1
                num_re = re.compile(r'^\s*\[(\d+)\]\s*(.*)$')
                matched_any = False
                for line in temp_lines_raw:
                    m = num_re.match(line)
                    if not m:
                        continue
                    idx_n = int(m.group(1)) - 1
                    body = m.group(2).strip()
                    if 0 <= idx_n < n_expected:
                        matched_any = True
                        # làm sạch nhẹ như logic cũ (bỏ lặp từ, gộp space)
                        body = re.sub(r'(\b\w+\b)(?:\s+\1){2,}', r'\1', body, flags=re.IGNORECASE)
                        body = re.sub(r' +', ' ', body).strip()
                        if body:
                            slots[idx_n] = body

                if matched_any:
                    # Có đánh số -> dùng cơ chế số. temp_lines chỉ gồm các dòng
                    # ĐÃ điền được (để các bước kiểm tra CJK/độ dài phía dưới
                    # vẫn chạy). Số ô còn None = số câu AI bỏ sót.
                    missing_slots = [k + 1 for k, v in enumerate(slots) if v is None]
                    temp_lines = [v for v in slots if v is not None]
                    if missing_slots:
                        preview = ", ".join(str(x) for x in missing_slots[:10])
                        self.log.emit(f"🔎 Thiếu số dòng: {preview}{'...' if len(missing_slots) > 10 else ''}\n")
                else:
                    # AI KHÔNG trả số nào -> fallback về cách cũ (tách theo dòng,
                    # lọc rác) để không vỡ so với hành vi trước đây.
                    temp_lines = []
                    for line in temp_lines_raw:
                        stripped = line.strip()
                        if stripped.isdigit():
                            continue
                        if _TIMESTAMP_LINE_RE.match(stripped):
                            continue
                        clean_line = re.sub(r'(\b\w+\b)(?:\s+\1){2,}', r'\1', line, flags=re.IGNORECASE)
                        clean_line = re.sub(r' +', ' ', clean_line)
                        temp_lines.append(clean_line)
                
                if len(temp_lines) == 0:
                    self.log.emit(f"⚠️ AI không trả về dòng nào hợp lệ. Thoát vào lại và thử lại...\n")
                    # KHÔNG mở tab mới (tránh tab dồn đầy Chrome gây chậm/treo).
                    # Tái dùng CHÍNH tab hiện tại: chỉ điều hướng về trang trắng
                    # để reset ngữ cảnh AI. Luôn chỉ 1 tab/luồng.
                    try: page.goto("about:blank"); page.wait_for_timeout(200)
                    except Exception: pass
                    retry_count += 1
                    time.sleep(2)
                    continue
                
                joined_temp = " ".join(temp_lines)
                total_chars = len(re.sub(r"\s", "", joined_temp))
                cjk_count = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7a3]", joined_temp))
                
                if total_chars > 0 and (cjk_count / total_chars) > 0.03:
                    ratio = cjk_count / total_chars
                    self.log.emit(f"⚠️ CẢNH BÁO: Khối {i+1} AI lười dịch, sót {cjk_count} chữ Hán ({ratio*100:.1f}% > 3%). Ép AI dịch lại!\n")
                    
                    # FIX 1: Chỉ thu hẹp batch_size, không gọt mảng gốc
                    if retry_count >= 2 and batch_size > 20:
                        half = max(1, batch_size // 2)
                        self.log.emit(f"✂️ Tự động chia nhỏ: {batch_size} câu → {half} câu để AI bớt lười...\n")
                        batch_size = half

                    # KHÔNG mở tab mới (tránh tab dồn đầy Chrome gây chậm/treo).
                    # Tái dùng CHÍNH tab hiện tại: chỉ điều hướng về trang trắng
                    # để reset ngữ cảnh AI. Luôn chỉ 1 tab/luồng.
                    try: page.goto("about:blank"); page.wait_for_timeout(200)
                    except Exception: pass
                    retry_count += 1
                    time.sleep(2)
                    continue
                    
                # FIX 2: Xử lý lệch Timeline dựa trên batch_size và current_batch
                if len(temp_lines) < len(current_batch):
                    self.log.emit(f"⚠️ AI dịch thiếu ({len(temp_lines)}/{len(current_batch)} dòng). Chắc chắn AI đã bỏ sót câu ở giữa!\n")
                    self.log.emit("✂️ Vứt bỏ bản dịch lỗi. Đang chia nhỏ khối để ép AI dịch lại chính xác...\n")
                    
                    if batch_size > 15:
                        batch_size = max(1, batch_size // 2)
                    
                    retry_count += 1
                    # KHÔNG mở tab mới (tránh tab dồn đầy Chrome gây chậm/treo).
                    # Tái dùng CHÍNH tab hiện tại: chỉ điều hướng về trang trắng
                    # để reset ngữ cảnh AI. Luôn chỉ 1 tab/luồng.
                    try: page.goto("about:blank"); page.wait_for_timeout(200)
                    except Exception: pass
                    time.sleep(2)
                    continue
                    
                elif len(temp_lines) > len(current_batch):
                    translated_chunk_lines.extend(temp_lines[:len(current_batch)])
                    chunk_to_translate = chunk_to_translate[len(current_batch):]
                else:
                    translated_chunk_lines.extend(temp_lines)
                    chunk_to_translate = chunk_to_translate[len(current_batch):]
                    
                # Tới bước này là đã thành công, chuẩn bị chạy phần tiếp theo của Chunk
                progressive_steps += 1
                retry_count = 0
                batch_size = len(chunk_to_translate)
                
                # Sang trang mới để reset bộ nhớ đệm AI
                if len(chunk_to_translate) > 0:
                    # KHÔNG mở tab mới (tránh tab dồn đầy Chrome gây chậm/treo).
                    # Tái dùng CHÍNH tab hiện tại: chỉ điều hướng về trang trắng
                    # để reset ngữ cảnh AI. Luôn chỉ 1 tab/luồng.
                    try: page.goto("about:blank"); page.wait_for_timeout(200)
                    except Exception: pass

            # Nếu nỗ lực thử lại đều thất bại, giữ nguyên gốc
            if len(chunk_to_translate) > 0:
                has_error = True
                self.log.emit(f"❌ Khối {i+1} vẫn thất bại sau mọi nỗ lực. Đành khớp bù bản gốc phần thiếu.\n")
                for b in chunk_to_translate:
                    translated_chunk_lines.append(b["text"])
                    
            # FIX TẬN GỐC LỖI INDEX OUT OF RANGE: Lúc này số lượng translated_chunk_lines luôn = len(chunk)
            for j, b in enumerate(chunk):
                translated_results[b["stt"]] = translated_chunk_lines[j]
            
            self.chunk_done.emit(idx, translated_results)

            if i < len(chunks) - 1 and not self._cancel:
                self.log.emit("⏸️ Đã nhận kết quả, nghỉ 1 giây trước khi gửi tiếp...\n")
                page.wait_for_timeout(1000)

        if self._cancel: return
        
        final_srt_content = ""
        for b in blocks:
            stt = b["stt"]
            timecode = b["time"].replace('.', ',')
            text_vi = translated_results.get(stt, b["text"])
            final_srt_content += f"{stt}\n{timecode}\n{text_vi}\n\n"
            
        vi_path = os.path.splitext(srt_path)[0] + "_vi.srt"
        with open(vi_path, "w", encoding="utf-8") as f: 
            f.write(final_srt_content.strip() + "\n")
            
        if has_error:
            self.item_failed.emit(idx, "Hoàn thành nhưng có lỗi ở vài dòng (Đã giữ nguyên bản gốc phần thiếu).")
        else:
            self.log.emit(f"✅ Đã lưu file khớp 100% Timeline: {os.path.basename(vi_path)}\n")
            self.item_done.emit(idx, video_path, vi_path)

    def _send_and_wait(self, page, bot_name, prompt_message, expected_min_lines=None):
        try:
            page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1000)
            _select_model(page, self.model_key, log_fn=self.log.emit)
            inp = _find_el(page, _INPUT_SELS, timeout=5000, cancel_check=lambda: self._cancel)
            if self._cancel: return "ERROR: Cancelled"
            if not inp: return f"ERROR [{bot_name}]: Không thấy ô nhập. Có thể bị dính CAPTCHA."
            inp.click()
            page.evaluate('''(text) => {
                const el = document.activeElement?.contentEditable === "true" ? document.activeElement : document.querySelector("[contenteditable='true']");
                if (el) { el.focus(); el.innerText = text; el.dispatchEvent(new Event('input', {bubbles: true})); }
            }''', prompt_message)
            page.wait_for_timeout(300)
            page.keyboard.press("End"); page.keyboard.press("Space"); page.wait_for_timeout(300)

            # ── GỬI CHẮC CHẮN: dán xong Gemini THƯỜNG KHÔNG tự gửi. Nút gửi có
            # thể còn disabled (Gemini chưa nhận ra ô nhập có chữ), hoặc Enter
            # lần đầu không ăn. Phải THỬ NHIỀU CÁCH và XÁC NHẬN đã gửi thật —
            # cách xác nhận đáng tin nhất: ô nhập đã TRỐNG sau khi gửi.
            def _input_text():
                # Đọc nội dung ô nhập hiện tại (để biết đã gửi đi chưa).
                try:
                    return page.evaluate('''() => {
                        const el = document.querySelector("[contenteditable='true']");
                        return el ? (el.innerText || "").trim() : null;
                    }''')
                except Exception:
                    return None

            def _try_send_once():
                # Ưu tiên bấm nút gửi nếu nó ĐANG BẬT (không disabled);
                # nếu không có/bị khóa thì dùng Enter rồi Ctrl+Enter.
                sent = False
                btn = _find_el(page, _SEND_SELS, timeout=2000, cancel_check=lambda: self._cancel)
                if btn:
                    try:
                        _disabled = btn.get_attribute("aria-disabled")
                        _disabled2 = btn.is_disabled()
                        if not _disabled2 and _disabled != "true":
                            btn.click()
                            sent = True
                    except Exception:
                        pass
                if not sent:
                    try:
                        inp.click()
                        page.keyboard.press("Enter")
                    except Exception:
                        pass
                    page.wait_for_timeout(400)
                    if _input_text():  # vẫn còn chữ -> Enter chưa ăn, thử Ctrl+Enter
                        try:
                            page.keyboard.press("Control+Enter")
                        except Exception:
                            pass

            _sent_ok = False
            for _attempt in range(4):  # thử tối đa 4 lần gửi
                if self._cancel: return "ERROR: Cancelled"
                _try_send_once()
                page.wait_for_timeout(700)
                _txt = _input_text()
                # Ô nhập trống (hoặc không đọc được nữa vì đã submit) = đã gửi.
                if _txt is None or _txt == "":
                    _sent_ok = True
                    break
                # Chưa gửi được: dán lại (phòng khi nội dung bị mất) rồi thử tiếp.
                if _attempt < 3:
                    self.log.emit(f"↩️ [{bot_name}] Chưa gửi được, thử Enter lại (lần {_attempt+2}/4)...\n")
                    try:
                        inp.click()
                        page.evaluate('''(text) => {
                            const el = document.querySelector("[contenteditable='true']");
                            if (el) { el.focus(); el.innerText = text; el.dispatchEvent(new Event('input', {bubbles: true})); }
                        }''', prompt_message)
                        page.wait_for_timeout(300)
                        page.keyboard.press("End")
                    except Exception:
                        pass

            if not _sent_ok:
                return f"ERROR [{bot_name}]: Gửi prompt thất bại (Gemini không nhận Enter sau 4 lần)."

            prev, stable = "", 0
            for _ in range(720): 
                if self._cancel: return "ERROR: Cancelled"
                page.wait_for_timeout(500)
                cur = ""
                for s in _RESP_SELS:
                    try:
                        els = page.query_selector_all(s)
                        if els and els[-1].inner_text().strip(): cur = els[-1].inner_text().strip(); break
                    except Exception: continue
                if cur and cur == prev:
                    stable += 1
                    required_stable = 8
                    if expected_min_lines:
                        # QUAN TRỌNG: đếm dòng THẬT (loại rác số đếm/timestamp
                        # Gemini tự chèn) - nếu đếm thô, rác làm phồng số dòng
                        # lên ~3 lần, khiến code tưởng "đã đủ dòng" quá sớm và
                        # DỪNG CHỜ TRƯỚC KHI GEMINI DỊCH XONG THẬT - đây chính
                        # là nguyên nhân gây "AI dịch thiếu" dù đã tăng thời
                        # gian chờ, vì bị cắt ngang chứ không phải AI lười.
                        got_lines = _count_real_lines(cur)
                        if got_lines < expected_min_lines:
                            required_stable = 30
                    if stable >= required_stable: return cur
                else: stable = 0; prev = cur
            return prev if prev else f"ERROR [{bot_name}]: Quá thời gian chờ"
        except Exception as e: return f"ERROR [{bot_name}]: {e}"

class DeepSeekTranslateThread(QThread):
    """Thread dịch bằng DeepSeek V4 Pro (API), bắn đúng bộ signal như
    GeminiTranslateThread để dùng chung toàn bộ UI (bảng dịch, panel ngữ cảnh,
    progress bar...) không cần đổi gì ở phần giao diện.

    2 mode:
      - full_series_mode=False (khách tải lẻ): mỗi tập tự phân tích ngữ cảnh
        riêng, dịch độc lập.
      - full_series_mode=True  (khách chọn trọn bộ): phân tích ngữ cảnh 1 lần
        cho TOÀN BỘ queue rồi dịch tuần tự, ngữ cảnh chảy liên tục xuyên suốt.
    """
    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    context_extracted = pyqtSignal(int, str)
    chunk_done = pyqtSignal(int, dict)
    item_done = pyqtSignal(int, str, str)
    item_failed = pyqtSignal(int, str)
    all_done = pyqtSignal()

    def __init__(self, queue_items, api_key, genre="Phụ đề phim",
                 target_style="Tự nhiên, dễ nghe", full_series_mode=False):
        super().__init__()
        self.queue_items = list(queue_items)
        self.api_key = api_key
        self.genre = genre
        self.target_style = target_style
        self.full_series_mode = full_series_mode
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _ctx_display_text(self, ctx) -> str:
        return ctx.character_profiles or "(chưa có bối cảnh)"

    def _translate_one_episode(self, idx, video_path, srt_path, ctx, done_offset, total_lines):
        """Dịch 1 tập bằng ctx đã có sẵn (dùng chung cho cả 'each' lẫn 'full').
        Trả về done_offset mới sau khi dịch xong tập này."""
        with open(srt_path, "r", encoding="utf-8-sig") as f:
            blocks = dst.parse_srt(f.read())
        if not blocks:
            self.item_failed.emit(idx, "File trống hoặc sai định dạng SRT.")
            return done_offset

        translated_results = {}
        done = done_offset
        for start in range(0, len(blocks), dst.LINES_PER_CHUNK):
            if self._cancel:
                return done
            chunk = blocks[start:start + dst.LINES_PER_CHUNK]
            try:
                texts = dst.translate_chunk(self.api_key, ctx, chunk, log_callback=self.log.emit)
            except dst.DeepSeekAPIError as e:
                self.item_failed.emit(idx, f"Lỗi gọi DeepSeek: {e}")
                return done

            for b, vi in zip(chunk, texts):
                translated_results[b.idx] = vi
            ctx.update_previous_context(texts)

            self.chunk_done.emit(idx, dict(translated_results))
            done += len(chunk)
            self.progress.emit(done)

        if self._cancel:
            return done

        for b in blocks:
            b.text = translated_results.get(b.idx, b.text)
        vi_path = os.path.splitext(srt_path)[0] + "_vi.srt"
        with open(vi_path, "w", encoding="utf-8") as f:
            f.write(dst.rebuild_srt(blocks))

        self.log.emit(f"✅ Đã lưu: {os.path.basename(vi_path)}\n")
        self.item_done.emit(idx, video_path, vi_path)
        return done

    def run(self):
        if not self.api_key:
            self.log.emit("❌ Chưa nhập DeepSeek API key.\n")
            self.all_done.emit()
            return

        total = len(self.queue_items)
        if total == 0:
            self.all_done.emit()
            return

        try:
            total_lines = 0
            episodes_blocks = {}
            for item in self.queue_items:
                with open(item["srt"], "r", encoding="utf-8-sig") as f:
                    b = dst.parse_srt(f.read())
                episodes_blocks[item["srt"]] = b
                total_lines += len(b)

            if self.full_series_mode and total > 1:
                # ── MODE TRỌN BỘ: phân tích ngữ cảnh 1 LẦN cho cả series ──
                self.log.emit(f"🔗 Chế độ TRỌN BỘ: phân tích ngữ cảnh chung cho {total} tập...\n")
                ctx = dst.SeriesContext(genre=self.genre, target_style=self.target_style)
                full_script = "\n\n".join(
                    f"[Tập {i+1}]\n" + "\n".join(b.text for b in episodes_blocks[item["srt"]])
                    for i, item in enumerate(self.queue_items)
                )
                dst.analyze_movie_context(self.api_key, full_script, ctx)
                ctx_text = self._ctx_display_text(ctx)
                for idx in range(total):
                    self.context_extracted.emit(idx, ctx_text)
                self.log.emit("🧠 Phân tích xong! Bắt đầu dịch tuần tự các tập (ngữ cảnh nối liền)...\n")

                done = 0
                for idx, item in enumerate(self.queue_items):
                    if self._cancel:
                        break
                    self.log.emit(f"\n{'='*50}\n📄 [{idx+1}/{total}] {os.path.basename(item['srt'])}\n")
                    done = self._translate_one_episode(idx, item["video"], item["srt"], ctx, done, total_lines)

            else:
                # ── MODE TẢI LẺ: mỗi tập tự phân tích ngữ cảnh riêng ──
                done = 0
                for idx, item in enumerate(self.queue_items):
                    if self._cancel:
                        break
                    srt_path = item["srt"]
                    self.log.emit(f"\n{'='*50}\n📄 [{idx+1}/{total}] {os.path.basename(srt_path)}\n")

                    ctx = dst.SeriesContext(genre=self.genre, target_style=self.target_style)
                    blocks = episodes_blocks[srt_path]
                    full_text = "\n".join(b.text for b in blocks)

                    self.log.emit("🔍 Đang phân tích kịch bản & mối quan hệ nhân vật...\n")
                    dst.analyze_movie_context(self.api_key, full_text, ctx)
                    self.context_extracted.emit(idx, self._ctx_display_text(ctx))
                    self.log.emit("🧠 Phân tích xong bối cảnh! Bắt đầu dịch...\n")

                    done = self._translate_one_episode(idx, item["video"], srt_path, ctx, done, total_lines)

        except Exception as e:
            self.log.emit(f"❌ Lỗi nghiêm trọng trong luồng dịch DeepSeek: {e}\n")

        self.log.emit(f"\n🏁 XONG CHIẾN DỊCH (DeepSeek).\n")
        self.all_done.emit()


class QueueCard(QWidget):
    def __init__(self, video_path, srt_path, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.video_path = video_path
        self.srt_path = srt_path
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        self.badge = QLabel("⏳")
        self.badge.setFixedSize(28, 28)
        self.badge.setStyleSheet("font-size: 16px; background: #2D303D; border-radius: 6px;")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.badge)
        info = QVBoxLayout()
        self.n = QLabel(os.path.basename(srt_path))
        self.n.setStyleSheet("color: #fff; font-size: 12px; font-weight: bold;")
        self.v = QLabel(f"File AI: {os.path.basename(video_path) if video_path else 'N/A'}")
        self.v.setStyleSheet("color: #8A8D98; font-size: 10px;")
        info.addWidget(self.n); info.addWidget(self.v)
        lay.addLayout(info); lay.setStretch(1, 1)
    def set_status(self, s):
        self.badge.setText({"waiting": "⏳", "done": "✅", "error": "❌"}.get(s, "⏳"))

# ============================================================
# GIAO DIỆN CHÍNH
# ============================================================
class TranslateWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue = []
        self.settings = QSettings("BoomStudio", "TranslateTab")
        self._translate_thread = None
        self.current_selected_item = None
        self.context_memory = {} 
        
        self.setStyleSheet("""
            QWidget { background: #11121A; color: #E5E6E8; }
            QFrame { background: #1C1D27; border-radius: 8px; }
            QLabel { background: transparent; }
            QPushButton { background: #2D303D; color: white; border-radius: 4px; font-weight: bold; padding: 6px; }
            QPushButton:hover { background: #3B3E4D; }
            QListWidget, QTableWidget { background: #11121A; border: 1px solid #2D303D; border-radius: 6px; }
            QComboBox, QSpinBox { background: #11121A; border: 1px solid #2D303D; border-radius: 4px; padding: 6px; font-weight: bold;}
            QScrollBar:vertical { background: #11121A; width: 10px; }
            QScrollBar::handle:vertical { background: #3B3E4D; border-radius: 5px; }
        """)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(15, 15, 15, 15)
        self.main_sp = QSplitter(Qt.Orientation.Horizontal)
        
        # 1. CỘT TRÁI
        left_frame = QFrame()
        ll = QVBoxLayout(left_frame)
        ll.setContentsMargins(15, 15, 15, 15)
        
        lbl_list = QLabel("📂 QUẢN LÝ DỮ LIỆU SRT")
        lbl_list.setStyleSheet("color: #7452FF; font-weight: bold; font-size: 14px;")
        ll.addWidget(lbl_list)
        
        btn_folder = QHBoxLayout()
        self.btn_add = QPushButton("📄 Thêm SRT"); self.btn_add.clicked.connect(self._manual_add)
        self.btn_add_folder = QPushButton("📁 Thêm Thư Mục")
        self.btn_add_folder.setStyleSheet("background: #2D303D; color: #10B981; border: 1px solid #10B981;")
        self.btn_add_folder.clicked.connect(self._manual_add_folder)
        btn_folder.addWidget(self.btn_add, stretch=5); btn_folder.addWidget(self.btn_add_folder, stretch=5)
        ll.addLayout(btn_folder)
        
        self.btn_rm = QPushButton("🗑️ Xóa File Chọn"); self.btn_rm.clicked.connect(self._remove_selected)
        ll.addWidget(self.btn_rm)
        
        self.q_list = QListWidget()
        self.q_list.setStyleSheet("QListWidget::item:selected { background: #2A2359; border-left: 3px solid #7452FF; }")
        self.q_list.itemClicked.connect(self._on_item_clicked)
        ll.addWidget(self.q_list, stretch=1)
        
        ll.addWidget(QLabel("⚙️ CẤU HÌNH SMART TRANSLATE", styleSheet="color: #7452FF; font-weight: bold; margin-top: 15px; font-size: 13px;"))

        # ── CHỌN ENGINE DỊCH: Gemini (trình duyệt, free) hoặc DeepSeek (API) ──
        ll.addWidget(QLabel("Engine dịch:", styleSheet="color: #8A8D98; font-size: 11px;"))
        self.cb_engine = QComboBox()
        self.cb_engine.addItems([
            "🌐 Gemini (Trình duyệt - Miễn phí)",
            "🚀 DeepSeek V4 Pro (API Key - Nhanh & Rẻ)",
        ])
        self.cb_engine.setCurrentText(self.settings.value("trans_engine", self.cb_engine.itemText(0)))
        self.cb_engine.currentTextChanged.connect(self._on_engine_changed)
        ll.addWidget(self.cb_engine)

        # ── Ô nhập API key DeepSeek (chỉ hiện khi chọn engine DeepSeek) ──
        self.deepseek_key_box = QWidget()
        dsk_lay = QVBoxLayout(self.deepseek_key_box)
        dsk_lay.setContentsMargins(0, 4, 0, 0)
        dsk_lay.addWidget(QLabel("DeepSeek API Key:", styleSheet="color: #8A8D98; font-size: 11px;"))
        self.txt_deepseek_key = QLineEdit()
        self.txt_deepseek_key.setPlaceholderText("sk-xxxxxxxxxxxxxxxx")
        self.txt_deepseek_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_deepseek_key.setText(self.settings.value("deepseek_api_key", ""))
        self.txt_deepseek_key.textChanged.connect(
            lambda t: self.settings.setValue("deepseek_api_key", t)
        )
        dsk_lay.addWidget(self.txt_deepseek_key)
        ll.addWidget(self.deepseek_key_box)

        # ── Checkbox chọn mode: tải lẻ (mỗi tập tự phân tích riêng) hay
        #    trọn bộ (phân tích ngữ cảnh 1 lần, dịch xuyên suốt cả series) ──
        self.chk_full_series = QCheckBox("🔗 Dịch trọn bộ (giữ ngữ cảnh xuyên suốt cả series)")
        self.chk_full_series.setToolTip(
            "Bật: phân tích ngữ cảnh 1 LẦN cho toàn bộ các tập trong danh sách,\n"
            "biết trước cốt truyện/nhân vật xuất hiện muộn, dịch xuyên suốt không đứt mạch.\n"
            "Tắt (tải lẻ): mỗi tập tự phân tích ngữ cảnh riêng, độc lập với các tập khác."
        )
        self.chk_full_series.setChecked(self.settings.value("trans_full_series", "false") == "true")
        self.chk_full_series.stateChanged.connect(
            lambda s: self.settings.setValue("trans_full_series", "true" if s else "false")
        )
        ll.addWidget(self.chk_full_series)

        self.cb_preset = QComboBox()
        self.cb_preset.addItems(list(PROMPT_PRESETS.keys()))
        saved_preset = self.settings.value("trans_preset", list(PROMPT_PRESETS.keys())[0])
        self.cb_preset.setCurrentText(saved_preset)
        ll.addWidget(QLabel("Quy tắc dịch:", styleSheet="color: #8A8D98; font-size: 11px;"))
        ll.addWidget(self.cb_preset)
        
        self.model_combo = QComboBox()
        saved_models = self.settings.value("cached_models", ["Auto (Mặc định)"])
        if isinstance(saved_models, str): saved_models = [saved_models]
        self.model_combo.addItems(saved_models)
        self.model_combo.setCurrentText(self.settings.value("gemini_model", "Auto (Mặc định)"))
        ll.addWidget(QLabel("Mô hình AI:", styleSheet="color: #8A8D98; font-size: 11px; margin-top: 5px;"))
        ll.addWidget(self.model_combo)

        ll.addWidget(QLabel("Số câu / 1 lần gửi (Chunk):", styleSheet="color: #8A8D98; font-size: 11px; margin-top: 5px;"))
        self.spin_chunk = QSpinBox()
        self.spin_chunk.setRange(20, 500)
        self.spin_chunk.setSingleStep(10)
        self.spin_chunk.setValue(int(self.settings.value("chunk_size", 100)))
        ll.addWidget(self.spin_chunk)

        ll.addWidget(QLabel("Số tập dịch song song (Gemini):", styleSheet="color: #8A8D98; font-size: 11px; margin-top: 5px;"))
        self.spin_translate_workers = QSpinBox()
        self.spin_translate_workers.setRange(1, 4)
        self.spin_translate_workers.setValue(int(self.settings.value("translate_workers", 1)))
        self.spin_translate_workers.setToolTip(
            "Số tập dịch CÙNG LÚC bằng Gemini (mỗi tập 1 trình duyệt Chrome ẩn\n"
            "riêng). Phân tích bối cảnh chỉ làm 1 LẦN DUY NHẤT (dùng tập đầu\n"
            "tiên trong hàng đợi làm mẫu), áp dụng chung cho mọi tập dịch song\n"
            "song phía sau.\n"
            "⚠️ Mỗi luồng = 1 Chrome ẩn riêng - chọn cao tốn thêm RAM/CPU rõ rệt."
        )
        self.spin_translate_workers.valueChanged.connect(
            lambda v: self.settings.setValue("translate_workers", v)
        )
        ll.addWidget(self.spin_translate_workers)

        auth_box = QHBoxLayout()
        self.lbl_auth_status = QLabel("🔴 Chưa Login Gemini" if not os.path.exists(AUTH_FILE) else "🟢 Đã Login Gemini")
        self.lbl_auth_status.setStyleSheet("font-size: 11px; font-weight: bold;")
        self.btn_login = QPushButton("🔑 Auth")
        self.btn_login.clicked.connect(self._manual_login)
        auth_box.addWidget(self.lbl_auth_status, stretch=1); auth_box.addWidget(self.btn_login)
        ll.addLayout(auth_box)
        
        action_box = QHBoxLayout()
        self.btn_start = QPushButton("🚀 BẮT ĐẦU DỊCH")
        self.btn_start.setStyleSheet("background: #7452FF; color: white; font-weight: bold; font-size: 14px; padding: 12px; border-radius: 6px;")
        self.btn_start.clicked.connect(self._start_translate)
        self.btn_cancel = QPushButton("⛔ HỦY")
        self.btn_cancel.setStyleSheet("background: #E94560; color: white; font-weight: bold; font-size: 14px; padding: 12px; border-radius: 6px;")
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_cancel.setEnabled(False)
        action_box.addWidget(self.btn_start, stretch=7); action_box.addWidget(self.btn_cancel, stretch=3)
        ll.addLayout(action_box)
        
        self.main_sp.addWidget(left_frame)

        # 2. CỘT PHẢI
        right_sp = QSplitter(Qt.Orientation.Vertical)
        table_frame = QFrame()
        rl = QVBoxLayout(table_frame)
        
        lbl_hint = QLabel("✍️ BẢNG DỊCH (Double Click vào ô chữ màu xanh để SỬA TRỰC TIẾP)")
        lbl_hint.setStyleSheet("color: #7452FF; font-weight: bold; font-size: 14px;")
        rl.addWidget(lbl_hint)
        
        self.table_widget = QTableWidget(0, 4)
        self.table_widget.setHorizontalHeaderLabels(["STT", "Thời gian", "Bản Gốc (Chỉ Đọc)", "Bản Dịch (Gõ để sửa)"])
        self.table_widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_widget.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table_widget.setStyleSheet("""
            QTableWidget { font-family: 'Segoe UI'; font-size: 13px; gridline-color: #2D303D; } 
            QHeaderView::section { background: #2D303D; color: #10B981; font-weight: bold; border: none; padding: 8px; } 
            QTableWidget::item:selected { background: #2A2359; }
        """)
        header = self.table_widget.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        rl.addWidget(self.table_widget)
        
        btn_save_box = QHBoxLayout()
        btn_save_box.addStretch()
        self.btn_save_table = QPushButton("💾 XUẤT / LƯU FILE SRT")
        self.btn_save_table.setStyleSheet("background: #10B981; color: white; font-weight: bold; padding: 8px 30px; font-size: 13px;")
        self.btn_save_table.clicked.connect(self._save_table_to_srt)
        btn_save_box.addWidget(self.btn_save_table)
        rl.addLayout(btn_save_box)
        right_sp.addWidget(table_frame)
        
        # 3. KHUNG HIỂN THỊ BỐI CẢNH
        ctx_box = QFrame()
        cl = QVBoxLayout(ctx_box)
        cl.setContentsMargins(15, 10, 15, 10)
        
        lbl_ctx = QLabel("🧠 NHẬN DIỆN BỐI CẢNH & XƯNG HÔ (AI TRINH SÁT)")
        lbl_ctx.setStyleSheet("color: #F37021; font-weight: bold; font-size: 12px;")
        cl.addWidget(lbl_ctx)
        
        self.txt_context = QTextEdit()
        self.txt_context.setReadOnly(True)
        self.txt_context.setPlaceholderText("Bắt đầu dịch để AI tự động trinh sát kịch bản và phân tích nhân vật...")
        self.txt_context.setStyleSheet("background: #1C1D27; color: #E5E6E8; border: 1px dashed #7452FF; font-size: 13px; padding: 8px; font-family: 'Segoe UI';")
        cl.addWidget(self.txt_context)
        right_sp.addWidget(ctx_box)
        
        # LOG
        log_box = QFrame()
        llog = QVBoxLayout(log_box)
        llog.addWidget(QLabel("📝 NHẬT KÝ HỆ THỐNG", styleSheet="color: #8A8D98; font-weight: bold; font-size: 12px;"))
        self.pbar = QProgressBar(); self.pbar.setFixedHeight(6)
        self.pbar.setStyleSheet("QProgressBar { background: #11121A; border: none; text-align: center; color: transparent;} QProgressBar::chunk { background: #10B981; }")
        llog.addWidget(self.pbar)
        self.log_view = QTextEdit(); self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("background: #11121A; color: #A7F3D0; font-family: Consolas; font-size: 11px; border: 1px solid #2D303D; padding: 8px;")
        llog.addWidget(self.log_view)
        right_sp.addWidget(log_box)
        
        right_sp.setStretchFactor(0, 60); right_sp.setStretchFactor(1, 20); right_sp.setStretchFactor(2, 20)
        self.main_sp.addWidget(right_sp)
        self.main_sp.setStretchFactor(0, 25); self.main_sp.setStretchFactor(1, 75)
        main_layout.addWidget(self.main_sp)

        # Set trạng thái ẩn/hiện ban đầu cho ô nhập key DeepSeek theo engine đã lưu
        self._on_engine_changed(self.cb_engine.currentText())

    # --------------------------------------------------------
    # HÀM LOGIC GIAO DIỆN & XỬ LÝ
    # --------------------------------------------------------
    def _on_engine_changed(self, text):
        self.settings.setValue("trans_engine", text)
        is_deepseek = text.startswith("🚀")
        self.deepseek_key_box.setVisible(is_deepseek)
        self.chk_full_series.setVisible(is_deepseek)  # mode trọn bộ chỉ áp dụng cho DeepSeek
        # model_combo/spin_chunk là cấu hình riêng của Gemini (chọn phiên bản, chunk theo trình duyệt)
        if hasattr(self, 'model_combo'):
            self.model_combo.setEnabled(not is_deepseek)
        if hasattr(self, 'spin_chunk'):
            self.spin_chunk.setEnabled(not is_deepseek)
        if hasattr(self, 'btn_login'):
            self.btn_login.setEnabled(not is_deepseek)

    def _update_ui_models_list(self, models):
        self.model_combo.clear(); self.model_combo.addItem("Auto (Mặc định)"); self.model_combo.addItems(models)
        self.settings.setValue("cached_models", ["Auto (Mặc định)"] + models)
        self._log("🔄 Hệ thống đã đồng bộ danh sách phiên bản thành công!\n")

    def _parse_srt(self, text):
        blocks = []
        pattern = r"(?m)^(\d+)\s*\n(\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3})\s*\n([\s\S]+?)(?=\n\s*\n|\Z)"
        for match in re.finditer(pattern, text):
            blocks.append({"stt": match.group(1).strip(), "time": match.group(2).strip(), "text": match.group(3).strip().replace('\n', ' ')})
        return blocks

    def _load_data_to_table(self, orig_srt_path, vi_srt_path):
        self.table_widget.setRowCount(0)
        orig_text = ""; vi_text = ""
        
        if os.path.exists(orig_srt_path):
            with open(orig_srt_path, "r", encoding="utf-8-sig") as f: orig_text = f.read().strip()
        if vi_srt_path and os.path.exists(vi_srt_path):
            with open(vi_srt_path, "r", encoding="utf-8-sig") as f: vi_text = f.read().strip()
            
        orig_blocks = self._parse_srt(orig_text)
        vi_blocks = {b["stt"]: b["text"] for b in self._parse_srt(vi_text)}
        
        if not orig_blocks: return
            
        self.table_widget.setRowCount(len(orig_blocks))
        for row, block in enumerate(orig_blocks):
            stt = block["stt"]
            item_stt = QTableWidgetItem(stt)
            item_stt.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_stt.setFlags(item_stt.flags() ^ Qt.ItemFlag.ItemIsEditable) 
            self.table_widget.setItem(row, 0, item_stt)
            
            item_time = QTableWidgetItem(block["time"])
            item_time.setFlags(item_time.flags() ^ Qt.ItemFlag.ItemIsEditable) 
            self.table_widget.setItem(row, 1, item_time)
            
            item_orig = QTableWidgetItem(block["text"])
            item_orig.setFlags(item_orig.flags() ^ Qt.ItemFlag.ItemIsEditable) 
            self.table_widget.setItem(row, 2, item_orig)
            
            item_vi = QTableWidgetItem(vi_blocks.get(stt, ""))
            item_vi.setForeground(QBrush(QColor("#A7F3D0")))
            font = QFont(); font.setBold(True); item_vi.setFont(font)
            item_vi.setFlags(item_vi.flags() | Qt.ItemFlag.ItemIsEditable) 
            self.table_widget.setItem(row, 3, item_vi)
            
        self.table_widget.resizeRowsToContents()

    def _save_table_to_srt(self):
        if not self.current_selected_item: return
        vi_path = os.path.splitext(self.current_selected_item.srt_path)[0] + "_vi.srt"
        srt_content = ""
        for row in range(self.table_widget.rowCount()):
            stt_item = self.table_widget.item(row, 0)
            time_item = self.table_widget.item(row, 1)
            trans_item = self.table_widget.item(row, 3)
            if stt_item and time_item and trans_item:
                srt_content += f"{stt_item.text()}\n{time_item.text().replace('.', ',')}\n{trans_item.text().strip()}\n\n"
        try:
            with open(vi_path, "w", encoding="utf-8") as f: f.write(srt_content.strip() + "\n")
            QMessageBox.information(self, "Thành công", f"Đã lưu nội dung mới vào file:\n{os.path.basename(vi_path)}")
        except Exception as e: QMessageBox.critical(self, "Lỗi", f"Lỗi không thể lưu: {e}")

    def _on_item_clicked(self, item):
        if not item: return
        widget = self.q_list.itemWidget(item)
        if not widget: return
        self.current_selected_item = widget
        self._load_data_to_table(widget.srt_path, os.path.splitext(widget.srt_path)[0] + "_vi.srt")
        
        if widget.srt_path in self.context_memory:
            self.txt_context.setPlainText(self.context_memory[widget.srt_path])
        else:
            self.txt_context.clear()

    def _update_item_status(self, idx, status):
        item = self.q_list.item(idx)
        if item:
            widget = self.q_list.itemWidget(item)
            if widget: widget.set_status(status)

    def _on_context_extracted(self, queue_idx, context_text):
        item = self.q_list.item(queue_idx)
        if item:
            widget = self.q_list.itemWidget(item)
            if widget:
                self.context_memory[widget.srt_path] = context_text
                if self.current_selected_item == widget:
                    self.txt_context.setPlainText(context_text)

    def _on_chunk_done(self, queue_idx, translated_dict):
        if queue_idx >= self.q_list.count(): return
        if self.current_selected_item == self.q_list.itemWidget(self.q_list.item(queue_idx)):
            for row in range(self.table_widget.rowCount()):
                stt_item = self.table_widget.item(row, 0)
                if stt_item and stt_item.text() in translated_dict:
                    vi_text = translated_dict[stt_item.text()]
                    vi_item = self.table_widget.item(row, 3)
                    if vi_item: 
                        vi_item.setText(vi_text)
                    else: 
                        new_item = QTableWidgetItem(vi_text)
                        new_item.setForeground(QBrush(QColor("#A7F3D0")))
                        font = QFont(); font.setBold(True); new_item.setFont(font)
                        new_item.setFlags(new_item.flags() | Qt.ItemFlag.ItemIsEditable)
                        self.table_widget.setItem(row, 3, new_item)
            self.table_widget.resizeRowsToContents()
            self.table_widget.scrollToBottom()

    def add_to_queue(self, vp, sp):
        if any(i["srt"] == sp for i in self._queue): return
        self._queue.append({"video": vp, "srt": sp})
        item = QListWidgetItem(self.q_list); item.setSizeHint(QSize(0, 52))
        self.q_list.setItemWidget(item, QueueCard(vp, sp))
        self.q_list.scrollToBottom()

    def _manual_add(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Chọn SRT", "", "SubRip (*.srt);;All (*)")
        for f in files: self.add_to_queue(os.path.splitext(f)[0] + ".mp4", f)

    def _manual_add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Chọn Thư mục chứa SRT")
        if folder:
            srt_files = glob.glob(os.path.join(folder, '*.srt'))
            count = 0
            for vf in srt_files:
                if not any(i["srt"] == vf for i in self._queue):
                    self.add_to_queue(os.path.splitext(vf)[0] + ".mp4", vf); count += 1
            self._log(f"✅ Đã đưa {count} file SRT vào danh sách!\n")

    def _remove_selected(self):
        for i in sorted([x.row() for x in self.q_list.selectedIndexes()], reverse=True):
            popped = self._queue.pop(i)
            if popped["srt"] in self.context_memory: del self.context_memory[popped["srt"]]
            self.q_list.takeItem(i)

    def _manual_login(self):
        self.btn_login.setEnabled(False)
        self._login_thread = GoogleManualLoginThread()
        self._login_thread.log.connect(self._log)
        self._login_thread.models_found.connect(self._update_ui_models_list)
        self._login_thread.finished_signal.connect(lambda ok: self.btn_login.setEnabled(True))
        self._login_thread.start()

    def _set_ui_lock(self, locked):
        for btn in [self.btn_add, self.btn_add_folder, self.btn_rm, self.btn_start]:
            btn.setEnabled(not locked)
        self.btn_cancel.setEnabled(locked)

    def _start_translate(self):
        if not self._queue: return
        self.settings.setValue("gemini_model", self.model_combo.currentText())
        self.settings.setValue("chunk_size", self.spin_chunk.value())
        self.settings.setValue("trans_preset", self.cb_preset.currentText())

        if self.cb_engine.currentText().startswith("🚀"):
            if not self.txt_deepseek_key.text().strip():
                QMessageBox.warning(self, "Thiếu API Key", "Vui lòng nhập DeepSeek API key trước khi dịch.")
                return

        self._set_ui_lock(True)
        self._proceed_translate()

    def _proceed_translate(self):
        if not [it for it in self._queue if os.path.exists(it["srt"])]:
            self._set_ui_lock(False); return

        for i in range(self.q_list.count()): self._update_item_status(i, "waiting")

        preset_key = self.cb_preset.currentText()
        use_deepseek = self.cb_engine.currentText().startswith("🚀")

        if use_deepseek:
            # Pbar theo TỔNG SỐ DÒNG (mượt hơn, vì DeepSeek báo tiến trình theo dòng chứ không theo tập)
            total_lines = 0
            for it in self._queue:
                if os.path.exists(it["srt"]):
                    with open(it["srt"], "r", encoding="utf-8-sig") as f:
                        total_lines += len(dst.parse_srt(f.read()))
            self.pbar.setMaximum(max(1, total_lines)); self.pbar.setValue(0)

            api_key = self.txt_deepseek_key.text().strip()
            # Gửi NGUYÊN VĂN hướng dẫn thể loại (giống hệt Gemini đang dùng qua
            # PROMPT_PRESETS), không chỉ tên rút gọn - để giữ đúng hướng dẫn từ
            # vựng/xưng hô đặc thù từng thể loại (VD: "ưu tiên đạo hữu, bổn tọa...").
            genre = PROMPT_PRESETS.get(preset_key, list(PROMPT_PRESETS.values())[0])
            full_series = self.chk_full_series.isChecked()

            self._translate_thread = DeepSeekTranslateThread(
                self._queue, api_key=api_key, genre=genre, full_series_mode=full_series
            )
        else:
            self.pbar.setMaximum(len(self._queue)); self.pbar.setValue(0)
            model_key = self.model_combo.currentText()
            chunk_val = self.spin_chunk.value()
            workers_val = self.spin_translate_workers.value() if hasattr(self, 'spin_translate_workers') else 1
            self._translate_thread = GeminiTranslateThread(self._queue, preset_key, model_key, chunk_val, translate_workers=workers_val)

        self._translate_thread.log.connect(self._log)
        self._translate_thread.progress.connect(self.pbar.setValue)

        self._translate_thread.context_extracted.connect(self._on_context_extracted)
        self._translate_thread.chunk_done.connect(self._on_chunk_done)

        def on_done(idx, vp, vsp):
            self._update_item_status(idx, "done")
            if self.current_selected_item == self.q_list.itemWidget(self.q_list.item(idx)):
                self._on_item_clicked(self.q_list.item(idx))

        self._translate_thread.item_done.connect(on_done)
        self._translate_thread.item_failed.connect(lambda idx, msg: self._update_item_status(idx, "error"))
        self._translate_thread.all_done.connect(lambda: self._set_ui_lock(False))
        self._translate_thread.start()

    def _cancel(self):
        if getattr(self, '_translate_thread', None): self._translate_thread.cancel()

    def _log(self, msg):
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)
        self.log_view.insertPlainText(msg)
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)
