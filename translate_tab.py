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
    QDialog, QTableWidget, QTableWidgetItem, QHeaderView, QSpinBox
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QSettings, QSize
from PyQt6.QtGui import QTextCursor, QBrush, QColor, QFont

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
AUTH_FILE = "gemini_auth.json"
BROWSER_ARGS = ["--disable-blink-features=AutomationControlled", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", "--disable-software-rasterizer"]

# ============================================================
# BỘ QUY TẮC DỊCH THUẬT (PROMPT PRESETS ĐA DẠNG)
# ============================================================
PROMPT_PRESETS = {
    "🌟 VIP: Tự động phân tích & Suy luận (Đề xuất)": "Bạn là biên dịch viên phim chuyên nghiệp. Dịch sát nghĩa, mượt mà. Hãy tuân thủ nghiêm ngặt bối cảnh và xưng hô đã được phân tích.",
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
# THREAD THAO TÁC ĐĂNG NHẬP (Dùng Chrome Thật + Profile Độc lập)
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
                
                # Tạo thư mục Profile Tool cạnh nơi lưu code để lách luật
                base_dir = os.path.dirname(os.path.abspath(__file__))
                tool_profile_path = os.path.join(base_dir, "AnhStudio_ChromeData")
                
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=tool_profile_path,
                    channel="chrome", # Chỉ định xài Chrome thật cài trong máy, không xài Chromium ảo
                    headless=False,
                    user_agent=UA,
                    viewport={"width": 1280, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"]
                )
                
                # Bơm Script Tàng hình vào tất cả các trang được mở ra
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
                    # Ghi đè giả lập trạng thái AUTH_FILE để tool hoạt động bình thường
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

class GeminiTranslateThread(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    context_extracted = pyqtSignal(int, str) # Gửi Context lên UI
    chunk_done = pyqtSignal(int, dict)       # Quăng chữ dịch lên UI (Real-time)
    item_done = pyqtSignal(int, str, str)
    item_failed = pyqtSignal(int, str)
    all_done = pyqtSignal()
    
    def __init__(self, queue_items, prompt_preset_key, model_key, chunk_size=100):
        super().__init__()
        self.queue_items = list(queue_items)
        self.preset_text = PROMPT_PRESETS.get(prompt_preset_key, list(PROMPT_PRESETS.values())[0])
        self.model_key = model_key
        self.chunk_size = chunk_size
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
        total = len(self.queue_items)
        done = 0
        if not os.path.exists(AUTH_FILE):
            self.log.emit("❌ Lỗi: Bạn chưa Đăng nhập Google.\n"); self.all_done.emit(); return
            
        ctx, pw = None, None
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            
            # Sử dụng thư mục Profile chung
            base_dir = os.path.dirname(os.path.abspath(__file__))
            tool_profile_path = os.path.join(base_dir, "AnhStudio_ChromeData")
            
            launch_err = None
            for attempt in range(3):
                try:
                    ctx = pw.chromium.launch_persistent_context(
                        user_data_dir=tool_profile_path,
                        channel="chrome", # Dùng Chrome thật
                        headless=True,    # Ẩn trình duyệt khi đang dịch tự động
                        user_agent=UA,
                        viewport={"width": 1280, "height": 900},
                        args=BROWSER_ARGS
                    )
                    launch_err = None
                    break
                except Exception as e:
                    launch_err = e
                    ctx = None
                    if attempt < 2:
                        self.log.emit(
                            f"⚠️ Mở Chrome thất bại (lần {attempt+1}/3): {e}\n"
                            f"⏳ Có thể do phiên Chrome trước chưa kịp giải phóng profile. Đợi 3s rồi thử lại...\n"
                        )
                        import time as _time
                        _time.sleep(3)
            if ctx is None:
                raise RuntimeError(f"Không thể mở Chrome sau 3 lần thử: {launch_err}")
            
            # Bơm Script Tàng hình để qua mặt CAPTCHA
            ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            self.log.emit("🌐 Đã khởi tạo trình duyệt Chrome ngầm (Standalone Profile).\n")
            
            for idx, item in enumerate(self.queue_items):
                if self._cancel: break
                video_path, srt_path = item["video"], item["srt"]
                base = os.path.basename(srt_path)
                self.log.emit(f"\n{'='*50}\n📄 [{idx+1}/{total}] Đang xử lý: {base}\n")
                try: 
                    # TRUYỀN THÊM ctx ĐỂ HÀM BÊN TRONG CÓ THỂ QUẢN LÝ VÀ ĐÓNG MỞ PAGE
                    page = self._translate_smart(ctx, page, idx, video_path, srt_path)
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

    def _translate_smart(self, ctx, page, idx, video_path, srt_path):
        with open(srt_path, "r", encoding="utf-8-sig") as f: srt_content = f.read()
        blocks = self._parse_srt(srt_content)
        if not blocks:
            self.item_failed.emit(idx, "File trống hoặc sai định dạng SRT."); return page
            
        # ==========================================
        # BƯỚC 1: TRINH SÁT BỐI CẢNH (AI EXTRACT CONTEXT)
        # ==========================================
        sample_blocks = blocks[:150] # Lấy 150 câu đầu để nhận diện
        sample_text = "\n".join([b["text"] for b in sample_blocks])
        
        context_prompt = (
            "Đọc kịch bản sau và hãy đóng vai nhà phê bình phim để trả lời NGẮN GỌN 2 câu hỏi:\n"
            "1. Phim này thuộc thể loại gì?\n"
            "2. Nhận diện các nhân vật xuất hiện và cách họ xưng hô với nhau sao cho chuẩn tiếng Việt nhất (Ví dụ: Lâm Xung (y/hắn), đại ca - đệ, hoàng thượng - thần thiếp, anh - em...)?\n\n"
            "TUYỆT ĐỐI KHÔNG DỊCH VĂN BẢN. CHỈ TRẢ VỀ TÓM TẮT ĐÁNH GIÁ (Khoảng 4-5 dòng).\n"
            f"Văn bản trích xuất:\n{sample_text}"
        )
        
        self.log.emit("🔍 Đang phân tích kịch bản & mối quan hệ nhân vật...\n")
        context_res = self._send_and_wait(page, "Bot-Trinh-Sat", context_prompt)
        
        if "ERROR" in context_res:
            context_res = "Không thể phân tích bối cảnh. Hệ thống sẽ dịch theo mặc định."
        
        # Làm sạch markdown để quăng lên UI đẹp hơn
        clean_ctx = re.sub(r'```[a-zA-Z]*\n?', '', context_res).replace('```', '')
        self.context_extracted.emit(idx, clean_ctx.strip())
        self.log.emit("🧠 Phân tích xong bối cảnh! Bắt đầu dịch...\n")

        # ==========================================
        # BƯỚC 2: DỊCH CHIA KHỐI & TÍCH LŨY DỊCH NỐT PHẦN THIẾU
        # ==========================================
        chunks = [blocks[i:i + self.chunk_size] for i in range(0, len(blocks), self.chunk_size)]
        translated_results = {} 
        has_error = False

        for i, chunk in enumerate(chunks):
            if self._cancel: break
            
            chunk_to_translate = chunk.copy()
            translated_chunk_lines = []
            
            max_retries = 5  # Đã tăng lên 5 lần để bắt dịch lại nếu sót chữ
            retry_count = 0
            progressive_steps = 0
            
            # Vòng lặp này sẽ bám đuổi cho đến khi dịch ĐỦ số câu trong khối thì thôi
            while len(chunk_to_translate) > 0 and retry_count < max_retries and progressive_steps < 10:
                if self._cancel: break
                
                lines_to_translate = [b["text"] for b in chunk_to_translate]
                text_payload = "\n".join(lines_to_translate)
                
                # BẮT BUỘC: Nhồi lại biến {clean_ctx} (Bối cảnh vừa nhận diện) vào đây.
                strict_rules = f"""QUY TẮC TUYỆT ĐỐI (VI PHẠM SẼ LỖI PHẦN MỀM):
1. BẮT BUỘC trả về ĐÚNG {len(lines_to_translate)} dòng. Không gộp, không tách.
2. KHÔNG giải thích, KHÔNG CHÀO HỎI (Tuyệt đối không được nói "Dạ", "Đây là bản dịch"...), KHÔNG dùng thẻ markdown. CHỈ TRẢ VỀ NỘI DUNG DỊCH.
3. DỊCH SẠCH 100% SANG TIẾNG VIỆT, TUYỆT ĐỐI KHÔNG ĐỂ SÓT LẠI CHỮ HÁN/TRUNG QUỐC.
4. ÁP DỤNG BỐI CẢNH VÀ XƯNG HÔ SAU ĐÂY VÀO BẢN DỊCH:
---
{clean_ctx}
---"""
                
                final_prompt = f"{self.preset_text}\n\n{strict_rules}\n\nDịch {len(lines_to_translate)} dòng sau:\n{text_payload}"
                
                if retry_count == 0:
                    if len(chunk_to_translate) == len(chunk):
                        self.log.emit(f"⏳ Đang dịch khối {i+1}/{len(chunks)} ({len(lines_to_translate)} câu)...\n")
                    else:
                        self.log.emit(f"🔄 Nạp lại bối cảnh, mở trang mới dịch TIẾP {len(lines_to_translate)} câu bị thiếu của khối {i+1}...\n")
                else:
                    self.log.emit(f"🔄 [Cứu hộ] Thử lại khối {i+1} (Lần {retry_count}/{max_retries})...\n")
                    
                c_res = self._send_and_wait(page, f"Khối-{i+1}", final_prompt, expected_min_lines=len(lines_to_translate))
                
                if c_res.startswith("ERROR"): 
                    self.log.emit(f"⚠️ Lỗi mạng/gửi: {c_res}\n")
                    self.log.emit(f"⚙️ Đang Thoát vào lại (Mở trang mới) để reset AI...\n")
                    try:
                        page.close()
                        page = ctx.new_page()
                    except Exception: pass
                    retry_count += 1
                    continue
                    
                # Làm sạch kết quả trả về
                res_clean = re.sub(r'```[a-zA-Z]*\n?', '', c_res)
                res_clean = res_clean.replace('```', '').replace('*', '')
                temp_lines_raw = [l.strip() for l in res_clean.split('\n') if l.strip()]
                
                # ====================================================
                # LỚP BẢO VỆ 2: PHẪU THUẬT SUB (SUB-SURGEON)
                # ====================================================
                # 1. Chặt bỏ câu chào hỏi luyên thuyên của AI
                while temp_lines_raw:
                    first_line = temp_lines_raw[0].lower()
                    if any(kw in first_line for kw in ["dạ,", "dạ ", "đây là bản", "bản dịch", "dưới đây là", "chắc chắn", "tất nhiên", "theo yêu cầu"]):
                        self.log.emit(f"✂️ Đã chặt bỏ câu chào hỏi thừa của AI: '{temp_lines_raw[0]}'\n")
                        temp_lines_raw.pop(0)
                    else:
                        break
                        
                # 2. Xử lý ảo giác lặp từ (Vâng vâng vâng...)
                temp_lines = []
                for line in temp_lines_raw:
                    clean_line = re.sub(r'(\b\w+\b)(?:\s+\1){2,}', r'\1', line, flags=re.IGNORECASE)
                    clean_line = re.sub(r' +', ' ', clean_line)
                    temp_lines.append(clean_line)
                # ====================================================
                
                if len(temp_lines) == 0:
                    self.log.emit(f"⚠️ AI không trả về dòng nào hợp lệ. Thoát vào lại và thử lại...\n")
                    try:
                        page.close()
                        page = ctx.new_page()
                    except Exception: pass
                    retry_count += 1
                    continue
                
                # ====================================================
                # BỘ LỌC ĐÁNH GIÁ SÓT CHỮ TRUNG (Tỷ lệ > 3%)
                # ====================================================
                joined_temp = " ".join(temp_lines)
                total_chars = len(re.sub(r"\s", "", joined_temp))
                cjk_count = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7a3]", joined_temp))
                
                if total_chars > 0 and (cjk_count / total_chars) > 0.03:
                    ratio = cjk_count / total_chars
                    self.log.emit(f"⚠️ CẢNH BÁO: Khối {i+1} AI lười dịch, sót {cjk_count} chữ Hán ({ratio*100:.1f}% > 3%). Ép AI dịch lại!\n")
                    try:
                        # Reset trang để xóa bộ nhớ tạm của con AI cứng đầu
                        page.close()
                        page = ctx.new_page()
                    except Exception: pass
                    retry_count += 1
                    continue
                # ====================================================
                    
                if len(temp_lines) < len(chunk_to_translate):
                    self.log.emit(f"⚠️ AI dịch được {len(temp_lines)}/{len(chunk_to_translate)} dòng. Đã lưu an toàn phần thành công!\n")
                    self.log.emit(f"⚙️ Ép AI dịch nốt {len(chunk_to_translate) - len(temp_lines)} dòng còn thiếu...\n")
                    
                    translated_chunk_lines.extend(temp_lines)
                    chunk_to_translate = chunk_to_translate[len(temp_lines):] # Cắt bỏ những câu đã dịch xong
                    progressive_steps += 1
                    retry_count = 0 # Reset lỗi vì chúng ta đã có tiến triển!
                    
                    # Thoát vào lại bằng trang mới để AI không bị nhớ nhầm vết xe đổ cũ
                    try:
                        page.close()
                        page = ctx.new_page()
                    except Exception: pass
                    
                elif len(temp_lines) > len(chunk_to_translate):
                    # Nếu AI bị "ảo giác" tự đẻ thêm dòng, ta chỉ lấy đúng số lượng cần
                    translated_chunk_lines.extend(temp_lines[:len(chunk_to_translate)])
                    chunk_to_translate = []
                else:
                    # Hoàn hảo 100%
                    translated_chunk_lines.extend(temp_lines)
                    chunk_to_translate = []

            # ==========================================
            # KẾT THÚC VÒNG LẶP CHO 1 KHỐI (CHUNK)
            # ==========================================
            if len(chunk_to_translate) > 0:
                has_error = True
                self.log.emit(f"❌ Khối {i+1} vẫn thất bại sau mọi nỗ lực. Đành khớp bù bản gốc phần thiếu.\n")
                for b in chunk_to_translate:
                    translated_chunk_lines.append(b["text"])
                    
            for j, b in enumerate(chunk):
                translated_results[b["stt"]] = translated_chunk_lines[j]
            
            # Quăng trực tiếp chữ vừa dịch xong lên Bảng UI (Real-time)
            self.chunk_done.emit(idx, translated_results)

            # ----------------------------------------------------
            # CƠ CHẾ NGHỈ 1 GIÂY (DELAY) TRƯỚC KHI GỬI KHỐI MỚI
            # ----------------------------------------------------
            if i < len(chunks) - 1 and not self._cancel:
                self.log.emit("⏸️ Đã nhận kết quả, nghỉ 1 giây trước khi gửi tiếp...\n")
                page.wait_for_timeout(1000) # Delay 1000ms = 1s

        if self._cancel: return page
        
        # Ráp file lưu lại
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
            self.item_failed.emit(idx, "Hoàn thành nhưng có lỗi ở vài dòng cuối (Đã giữ nguyên bản gốc).")
        else:
            self.log.emit(f"✅ Đã lưu file khớp 100% Timeline: {os.path.basename(vi_path)}\n")
            self.item_done.emit(idx, video_path, vi_path)

        # Quan trọng: Trả lại page đã được tạo mới (hoặc page cũ) ra ngoài để các file SRT tiếp theo sử dụng
        return page

    def _send_and_wait(self, page, bot_name, prompt_message, expected_min_lines=None):
        try:
            # Việc gọi goto ở đây sẽ luôn kích hoạt trang chủ Gemini trong page (đã reset nếu được khởi tạo lại)
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
            btn = _find_el(page, _SEND_SELS, timeout=2000, cancel_check=lambda: self._cancel)
            if btn:
                try: btn.click()
                except Exception: page.keyboard.press("Enter")
            else: page.keyboard.press("Enter")
            
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
                    # Ngưỡng ổn định mặc định: 8 lần x 0.5s = 4 giây không đổi -> coi là AI đã trả lời xong.
                    required_stable = 8
                    if expected_min_lines:
                        got_lines = len([l for l in cur.split('\n') if l.strip()])
                        if got_lines < expected_min_lines:
                            # Số dòng nhận được còn ÍT HƠN mong đợi -> rất có thể Gemini chỉ đang
                            # KHỰNG TẠM (lag mạng/model nghỉ giữa câu) chứ chưa thực sự dừng hẳn.
                            # Với chunk dài, khoảng khựng như vậy có thể kéo dài vài giây.
                            # => Bắt chờ ổn định LÂU HƠN (30 lần x 0.5s = 15 giây) trước khi
                            # chấp nhận, để tránh cắt ngang phản hồi giữa chừng gây thiếu dòng.
                            required_stable = 30
                    if stable >= required_stable: return cur
                else: stable = 0; prev = cur
            return prev if prev else f"ERROR [{bot_name}]: Quá thời gian chờ"
        except Exception as e: return f"ERROR [{bot_name}]: {e}"

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
# GIAO DIỆN CHÍNH (ĐÃ XÓA BẢNG CHỈNH SỬA - THÊM KHUNG BỐI CẢNH)
# ============================================================
class TranslateWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue = []
        self.settings = QSettings("AnhStudio", "TranslateTab")
        self._translate_thread = None
        self.current_selected_item = None
        
        # Từ điển lưu Context của từng file để khi bấm chuyển file không bị mất
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
        
        # =========================================
        # 1. CỘT TRÁI (BẢNG ĐIỀU KHIỂN)
        # =========================================
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

        # =========================================
        # 2. CỘT PHẢI (BẢNG DỊCH & BỐI CẢNH)
        # =========================================
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
        
        # Nút Lưu SRT
        btn_save_box = QHBoxLayout()
        btn_save_box.addStretch()
        self.btn_save_table = QPushButton("💾 XUẤT / LƯU FILE SRT")
        self.btn_save_table.setStyleSheet("background: #10B981; color: white; font-weight: bold; padding: 8px 30px; font-size: 13px;")
        self.btn_save_table.clicked.connect(self._save_table_to_srt)
        btn_save_box.addWidget(self.btn_save_table)
        rl.addLayout(btn_save_box)
        right_sp.addWidget(table_frame)
        
        # =========================================
        # 3. KHUNG HIỂN THỊ BỐI CẢNH (MỚI)
        # =========================================
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

    # --------------------------------------------------------
    # HÀM LOGIC GIAO DIỆN & XỬ LÝ
    # --------------------------------------------------------
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
            # Font chữ mập mạp cho dễ nhìn
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
        
        # Load lại Context AI đã lưu
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
        # Lưu vào từ điển bộ nhớ
        item = self.q_list.item(queue_idx)
        if item:
            widget = self.q_list.itemWidget(item)
            if widget:
                self.context_memory[widget.srt_path] = context_text
                # Nếu đang chọn đúng file đó, hiển thị luôn lên màn hình
                if self.current_selected_item == widget:
                    self.txt_context.setPlainText(context_text)

    def _on_chunk_done(self, queue_idx, translated_dict):
        # Update UI Table THEO THỜI GIAN THỰC
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
            # Cuộn xuống dòng cuối cùng đang dịch
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
            # Xóa luôn context trong bộ nhớ
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
        
        self._set_ui_lock(True)
        self._proceed_translate()

    def _proceed_translate(self):
        if not [it for it in self._queue if os.path.exists(it["srt"])]:
            self._set_ui_lock(False); return
            
        self.pbar.setMaximum(len(self._queue)); self.pbar.setValue(0)
        for i in range(self.q_list.count()): self._update_item_status(i, "waiting")
        
        preset_key = self.cb_preset.currentText()
        model_key = self.model_combo.currentText()
        chunk_val = self.spin_chunk.value()
        
        self._translate_thread = GeminiTranslateThread(self._queue, preset_key, model_key, chunk_val)
        self._translate_thread.log.connect(self._log)
        self._translate_thread.progress.connect(self.pbar.setValue)
        
        # Kết nối tín hiệu Context và Chunk UI
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
