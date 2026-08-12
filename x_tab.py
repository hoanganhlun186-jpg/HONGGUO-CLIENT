import os, sys, re, threading, subprocess, concurrent.futures, urllib.request, time
import logging
from datetime import datetime
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QListWidget, QListWidgetItem, QTextEdit, QFileDialog, QProgressBar, QFrame, QSplitter, QAbstractItemView, QComboBox, QCheckBox)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QSettings, QSize
from PyQt6.QtGui import QTextCursor, QPixmap, QColor, QIcon
from shared_utils import CREATE_NO_WINDOW, browser_launch_kwargs
from cookie_tab import get_cookie_file

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

def _sanitize(name): 
    return re.sub(r'[<>:"/\\|?*\n\r\t]', '_', name).strip()[:60]

def load_netscape_cookies(filepath):
    cookies = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('#') or not line.strip(): continue
                parts = line.strip().split('\t')
                if len(parts) >= 7:
                    cookies.append({
                        "domain": parts[0],
                        "path": parts[2],
                        "secure": parts[3] == "TRUE",
                        "name": parts[5],
                        "value": parts[6]
                    })
    except Exception: pass
    return cookies

# ============================================================
# HÀM LƯU LOG ẨN VÀO APPDATA
# ============================================================
def setup_hidden_logger(platform_name):
    appdata_dir = os.getenv('APPDATA', os.path.expanduser('~'))
    log_dir = os.path.join(appdata_dir, 'AnhStudio', 'Logs')
    os.makedirs(log_dir, exist_ok=True)
    
    log_file = os.path.join(log_dir, f"{platform_name}_debug_{datetime.now().strftime('%Y%m%d')}.log")
    
    logger = logging.getLogger(platform_name)
    logger.setLevel(logging.DEBUG)
    
    if not logger.handlers:
        fh = logging.FileHandler(log_file, encoding='utf-8')
        formatter = logging.Formatter('%(asctime)s - [%(name)s] - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)
        
    return logger

# --- LUỒNG TẢI ẢNH THUMBNAIL CHO GIAO DIỆN ---
class InnerThumbLoader(QThread):
    loaded = pyqtSignal(QPixmap)
    def __init__(self, url):
        super().__init__()
        self.url = url
    def run(self):
        try:
            req = urllib.request.Request(self.url, headers={'User-Agent': UA})
            data = urllib.request.urlopen(req, timeout=5).read()
            pm = QPixmap()
            pm.loadFromData(data)
            self.loaded.emit(pm)
        except: pass

# ============================================================
# VIDEO CARD — Thẻ hiển thị thông tin video
# ============================================================
class XVideoCard(QWidget):
    check_changed = pyqtSignal()
    
    def __init__(self, vid_data, already_exists=False, parent=None):
        super().__init__(parent)
        self.vid_data = vid_data
        self.already_exists = already_exists
        
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(4, 4, 4, 4)
        
        self.main_frame = QFrame()
        self.main_frame.setObjectName("cardFrame")
        outer_layout.addWidget(self.main_frame)
        
        self.card_layout = QHBoxLayout(self.main_frame)
        self.card_layout.setContentsMargins(10, 10, 10, 10)
        self.card_layout.setSpacing(15)
        
        # --- Checkbox ---
        self.chk = QCheckBox()
        self.chk.setFixedSize(30, 30) 
        self.chk.setStyleSheet("""
            QCheckBox::indicator {
                width: 18px; height: 18px;
                border: 1px solid #444; border-radius: 4px;
                background: transparent;
                margin: 2px;
            }
            QCheckBox::indicator:checked {
                background: #00ffcc; border-color: #00ffcc;
                image: none;
            }
            QCheckBox::indicator:hover {
                border-color: #00ffcc;
            }
        """)
        self.chk.stateChanged.connect(lambda: self.check_changed.emit())
        self.card_layout.addWidget(self.chk)
        
        # --- Thumbnail ---
        self.thumb_lbl = QLabel("🌐")
        self.thumb_lbl.setFixedSize(112, 63) 
        self.thumb_lbl.setStyleSheet("background: #111; border-radius: 4px; border: 1px solid #222; color: #00ffcc; font-size: 24px; font-weight: bold;")
        self.thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_lbl.setScaledContents(True)
        self.card_layout.addWidget(self.thumb_lbl)
        
        # --- Thông tin video & Thanh tiến trình ---
        info_lay = QVBoxLayout()
        info_lay.setSpacing(4)
        
        self.title_lbl = QLabel(vid_data.get('desc') or "Video Stream Vô Danh")
        self.title_lbl.setWordWrap(True)
        self.title_lbl.setStyleSheet("color: #e0e0e0; font-size: 13px; font-weight: 500;")
        self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        info_lay.addWidget(self.title_lbl)
        
        author = vid_data.get('author', 'Web/Stream Source')
        self.id_lbl = QLabel(f"🔗 {author}  |  {vid_data.get('platform', '').upper()}")
        self.id_lbl.setStyleSheet("color: #666666; font-size: 11px;") 
        info_lay.addWidget(self.id_lbl)
        
        self.pbar = QProgressBar()
        self.pbar.setFixedHeight(4)
        self.pbar.setTextVisible(False)
        self.pbar.setStyleSheet("""
            QProgressBar { background: transparent; border: none; }
            QProgressBar::chunk { background: #00ffcc; border-radius: 2px; }
        """)
        self.pbar.hide()
        info_lay.addWidget(self.pbar)
        
        info_lay.addStretch()
        self.card_layout.addLayout(info_lay)
        self.card_layout.setStretch(2, 1)

        cover_url = vid_data.get('cover_url', "")
        if cover_url:
            self.thumb_loader = InnerThumbLoader(cover_url)
            self.thumb_loader.loaded.connect(self._set_thumb)
            self.thumb_loader.start()

        # --- Set Style Đã Có Sẵn ---
        if already_exists:
            self._apply_downloaded_style()
        else:
            self.main_frame.setStyleSheet("QFrame#cardFrame { background-color: transparent; border: none; }")

    def _set_thumb(self, pm):
        self.thumb_lbl.setPixmap(pm)
        self.thumb_lbl.setStyleSheet("border-radius: 4px; border: 1px solid #222;")

    def _apply_downloaded_style(self):
        has_badge = any(isinstance(self.card_layout.itemAt(i).widget(), QLabel) and self.card_layout.itemAt(i).widget().text() == "Đã tải" for i in range(self.card_layout.count()))
        if not has_badge:
            badge = QLabel("Đã tải")
            badge.setFixedWidth(50)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setStyleSheet("color: #a5d6a7; background: #1b5e20; font-size: 10px; border: 1px solid #4caf50; border-radius: 4px; padding: 3px 0; font-weight: bold;")
            self.card_layout.addWidget(badge)
            
        self.main_frame.setStyleSheet("""
            QFrame#cardFrame {
                background-color: rgba(76, 175, 80, 0.12);
                border: 1px solid #4caf50;
                border-radius: 6px;
            }
        """)
        self.title_lbl.setStyleSheet("color: #a5d6a7; font-size: 13px; font-weight: bold;")
        
    def set_downloaded_state_realtime(self):
        if not self.already_exists:
            self.already_exists = True
            self._apply_downloaded_style()
            self.chk.setChecked(False)

# ============================================================
# SCAN THREAD
# ============================================================
class XScanThread(QThread):
    log = pyqtSignal(str)
    user_log = pyqtSignal(str)
    video_found = pyqtSignal(dict)
    finished_signal = pyqtSignal(int)
    
    def __init__(self, url, cookie_file, is_headless=True):
        super().__init__()
        self.url = url
        self.cookie_file = cookie_file
        self.is_headless = is_headless
        self._cancel = False
        
    def cancel(self): self._cancel = True
        
    def run(self):
        url = self.url.strip()
        url = url if url.startswith("http") else "https://" + url
        
        is_x_domain = any(domain in url.lower() for domain in ["x.com", "twitter.com"])
        is_x_single_post = is_x_domain and "/status/" in url.lower()
        
        self.user_log.emit(f"🔍 Đang phân tích đường dẫn...\n")
        
        if is_x_single_post or ".m3u8" in url or ".mp4" in url:
            self.log.emit("⚡ Phát hiện link luồng trực tiếp hoặc bài viết mạng xã hội cụ thể!\n👉 Đưa thẳng vào danh sách tải.\n")
            self.user_log.emit("🎯 Phát hiện URL luồng trực tiếp / bài viết lẻ!\n")
            
            platform = "X (Twitter)" if is_x_single_post else "Direct File"
            tag = "⭐ Bài Viết X" if is_x_single_post else f"⭐ PHIM CHÍNH"
            v = {
                "id": "direct_" + str(os.urandom(3).hex()),
                "url": url,
                "platform": platform,
                "desc": f"{tag} (Sẵn sàng tải)",
                "author": "User Input",
                "cover_url": ""
            }
            self.video_found.emit(v)
            self.user_log.emit("✅ Đã thêm 1 mục vào danh sách.\n")
            self.finished_signal.emit(1)
            return

        self.log.emit(f"🚀 KHỞI ĐỘNG HỆ THỐNG TRÌNH DUYỆT ẢO (SNIFFER/DOM SCRAPER)...\n")
        self.user_log.emit(f"🤖 Đang kích hoạt Trình duyệt ảo để cào dữ liệu...\n")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.log.emit("❌ LỖI: Chưa cài thư viện Playwright!\n")
            self.user_log.emit("❌ LỖI: Thiếu module Playwright.\n")
            self.finished_signal.emit(0)
            return

        total_streams = 0
        seen_urls = set()
        
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(**browser_launch_kwargs(
                    headless=self.is_headless,
                    args=["--disable-gpu", "--no-sandbox", "--mute-audio", "--disable-web-security"]
                ))
                ctx = browser.new_context(viewport={"width": 1280, "height": 720}, user_agent=UA)

                if self.cookie_file and os.path.exists(self.cookie_file):
                    self.log.emit("🍪 Đang tiêm Cookies vào trình duyệt ảo...\n")
                    cks = load_netscape_cookies(self.cookie_file)
                    if cks: ctx.add_cookies(cks)

                page = ctx.new_page()
                page.on("popup", lambda popup: popup.close()) 

                # === CÀO DOM TRÊN PROFILE X ===
                if is_x_domain:
                    self.log.emit(f"🌐 Đang truy cập Profile X: {url}\n")
                    self.user_log.emit(f"📜 Đang cuộn trang mạng xã hội X để lấy video...\n")
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3500) 
                    
                    self.log.emit("🤖 Đang quét cấu trúc bài viết để lấy Tên & Thumbnail...\n")
                    scroll_attempts = 0
                    max_scrolls = 10 
                    
                    js_scraper = '''
                        () => {
                            let results = [];
                            document.querySelectorAll('a[href*="/status/"]').forEach(a => {
                                let rawUrl = a.href.split('?')[0];
                                
                                let match = rawUrl.match(/(.*\\/status\\/\\d+)/);
                                if (!match) return;
                                let statusUrl = match[1];

                                if (statusUrl.includes('/analytics') || statusUrl.includes('/photo/')) return;

                                let text = "";
                                let article = a.closest('article');
                                
                                if (article) {
                                    const textEl = article.querySelector('div[data-testid="tweetText"]');
                                    if (textEl) text = textEl.innerText.replace(/\\n/g, ' ');
                                }
                                
                                if (!text) {
                                    let img = a.querySelector('img');
                                    if (img && img.alt && !['Image', 'Video', 'Ảnh', 'Video'].includes(img.alt)) {
                                        text = img.alt;
                                    }
                                }
                                
                                if (!text && a.getAttribute('aria-label')) {
                                    text = a.getAttribute('aria-label');
                                }
                                
                                if (text && text.includes('·')) {
                                    text = text.split('·')[0].trim();
                                }
                                
                                if (!text || text === "") text = "Video/Media từ X (Twitter)";
                                if (text.length > 80) text = text.substring(0, 80) + "...";
                                
                                let imgEl = (article ? article.querySelector('img[src*="video_thumb"], img[src*="media"]') : null) || a.querySelector('img');
                                let cover = imgEl ? imgEl.src : "";
                                
                                if (cover.includes('profile_images')) cover = "";
                                
                                results.push({url: statusUrl, text: text, cover: cover});
                            });
                            return results;
                        }
                    '''
                    
                    while scroll_attempts < max_scrolls:
                        if self._cancel: break
                        
                        scraped_data = page.evaluate(js_scraper)
                        new_found = 0
                        for item in scraped_data:
                            link = item['url']
                            if link not in seen_urls:
                                seen_urls.add(link)
                                total_streams += 1
                                new_found += 1
                                
                                author = link.split('/')[3] if len(link.split('/')) > 3 else "X_User"
                                desc_text = item['text']
                                
                                v = {
                                    "id": f"x_{total_streams}_{os.urandom(2).hex()}",
                                    "url": link,
                                    "platform": "X (Twitter)",
                                    "desc": desc_text,
                                    "author": f"@{author}",
                                    "cover_url": item['cover']
                                }
                                self.video_found.emit(v)
                                self.log.emit(f"🎯 Tóm được: {desc_text[:30]}...\n")
                                
                        if new_found > 0:
                            self.user_log.emit(f"📦 Đã quét thêm {new_found} bài viết (Tổng: {total_streams})...\n")
                            
                        page.evaluate("window.scrollBy(0, 1500)")
                        page.wait_for_timeout(2000) 
                        scroll_attempts += 1
                        
                # === ĐÁNH HƠI NETWORK PHIM LẬU M3U8 ===
                else:
                    self.user_log.emit(f"🕵️ Đang truy cập web và cắm chốt đánh hơi luồng Video/Phim...\n")
                    def on_resp(response):
                        nonlocal total_streams
                        req_url = response.url.lower()
                        if ".m3u8" in req_url or ".mp4" in req_url:
                            blacklist = ["adserver", "tracking", "analytics", "banner", "doubleclick", "chaturbate", "bet", ".jpg", ".png"]
                            if any(bad_word in req_url for bad_word in blacklist): return 
                                
                            if req_url not in seen_urls:
                                seen_urls.add(req_url)
                                total_streams += 1
                                ext = "M3U8" if ".m3u8" in req_url else "MP4"
                                is_main = any(x in req_url for x in ["master", "index", "playlist", "1080", "720", "chunklist"])
                                tag = "⭐ PHIM CHÍNH" if is_main else "Luồng phụ/QC"
                                
                                v = {
                                    "id": f"stream_{total_streams}_{os.urandom(2).hex()}",
                                    "url": response.url,
                                    "platform": "Sniffer",
                                    "desc": f"[{tag}] Bắt được luồng {ext} (#{total_streams})",
                                    "author": "Trích xuất Tự Động",
                                    "cover_url": ""
                                }
                                self.video_found.emit(v)
                                self.log.emit(f"🎯 TÓM ĐƯỢC LUỒNG {ext}: {response.url[:60]}...\n")
                                self.user_log.emit(f"🎬 Bắt được 1 luồng Video/Phim mới ({ext})...\n")
                                
                    ctx.on("response", on_resp)
                    self.log.emit(f"🌐 Đang truy cập web phim: {url}\n")
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2500)
                    
                    try:
                        page.evaluate("window.scrollBy(0, 300)")
                        iframes = page.locator("iframe").all()
                        for iframe in iframes:
                            box = iframe.bounding_box()
                            if box and box['width'] > 300 and box['height'] > 200:
                                page.mouse.click(box['x'] + box['width']/2, box['y'] + box['height']/2)
                                page.wait_for_timeout(500)
                    except: pass
                    
                    self.log.emit("⏳ Đang cắm chốt theo dõi Network...\n")
                    wt = 0
                    while wt < 150: 
                        if self._cancel: break
                        page.wait_for_timeout(100)
                        if wt % 10 == 0:
                            try:
                                page.evaluate('''
                                    const adTexts = ['tắt qc', 'bỏ qua', 'skip', 'đóng qc', 'close', 'phát phim'];
                                    document.querySelectorAll('*').forEach(el => {
                                        if (el.innerText && el.offsetWidth > 0 && el.offsetHeight > 0) {
                                            let text = el.innerText.toLowerCase().trim();
                                            if (adTexts.some(t => text.includes(t)) && text.length < 20) el.click();
                                        }
                                    });
                                ''')
                            except: pass
                        wt += 1

                browser.close()
        except Exception as e:
            self.log.emit(f"❌ LỖI HỆ THỐNG: {str(e)}\n")
            
        self.log.emit(f"🏁 TỔNG KẾT: Đã bóc tách thành công {total_streams} liên kết!\n")
        self.user_log.emit(f"🏁 Hoàn tất — Đã tìm thấy {total_streams} liên kết\n")
        self.finished_signal.emit(total_streams)

# ============================================================
# DOWNLOAD THREAD (Kèm Realtime Progress)
# ============================================================
class XDownloadThread(QThread):
    log = pyqtSignal(str)
    user_log = pyqtSignal(str)
    total_progress = pyqtSignal(int, int)
    card_progress = pyqtSignal(str, int)
    
    def __init__(self, videos, save_dir, cookie_file, thread_count=3):
        super().__init__()
        self.videos = videos
        self.save_dir = save_dir
        self.cookie_file = cookie_file
        self.thread_count = thread_count
        self._cancel = False
        self.lock = threading.Lock()
        self.success_count = 0
        self.done_count = 0
        self.pause_event = threading.Event()
        self.pause_event.set()
        self._is_paused = False
                
    def cancel(self): self._cancel = True; self.pause_event.set()
    def toggle_pause(self):
        if self._is_paused: self._is_paused = False; self.pause_event.set(); return False
        else: self._is_paused = True; self.pause_event.clear(); return True
            
    def run(self):
        total = len(self.videos)
        self.log.emit(f"\n📥 BẮT ĐẦU TẢI XUỐNG {total} MỤC...\n")
        self.user_log.emit(f"📥 Bắt đầu tải {total} liên kết...\n")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.thread_count) as ex:
            futs = {ex.submit(self._dl_worker, v, i, total): v for i, v in enumerate(self.videos, 1)}
            concurrent.futures.wait(futs)
            
        self.log.emit(f"🎉 HOÀN THÀNH: {self.success_count}/{total} mục.\n")
        self.user_log.emit(f"🎉 Hoàn tất: {self.success_count}/{total} tải thành công\n")
        

# ============================================================
# X WIDGET — Giao diện đồng bộ
# ============================================================
class XWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scanned = []
        self.settings = QSettings("AnhStudio", "X")
        
        # ==============================================================
        # 1. CẤU HÌNH NHẬN DIỆN THƯƠNG HIỆU
        # ==============================================================
        THEME_COLOR = "#00ffcc"  
        HOVER_COLOR = "#00cc99"
        PLACEHOLDER_TEXT = "🔗 Dán Link Phim Lậu, Link X.com Profile, hoặc X Video lẻ..."
        PLATFORM_NAME = "X (Twitter)"
        
        self.hidden_logger = setup_hidden_logger("X")
        # ==============================================================

        self.setStyleSheet(f"""
            QWidget {{ background-color: #080808; color: #e0e0e0; font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, Arial, sans-serif; }}
            QFrame {{ border: none; }}
            QPushButton {{ background-color: #151515; border: 1px solid #2a2a2a; border-radius: 6px; color: #ccc; padding: 8px 12px; font-weight: bold; }}
            QPushButton:hover {{ background-color: #222; color: #000; border-color: {THEME_COLOR}; }}
            QScrollBar:vertical {{ border: none; background: #050505; width: 8px; border-radius: 4px; }}
            QScrollBar::handle:vertical {{ background: #333; border-radius: 4px; }}
            QScrollBar::handle:vertical:hover {{ background: {THEME_COLOR}; }}
        """)

        master_layout = QVBoxLayout(self)
        master_layout.setContentsMargins(0, 0, 0, 0)
        master_layout.setSpacing(0)

        main_h_layout = QHBoxLayout()
        main_h_layout.setContentsMargins(20, 20, 20, 10)
        main_h_layout.setSpacing(25)
        
        # === CỘT TRÁI (INPUT & DANH SÁCH) ===
        left_panel = QFrame()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(15)
        
        search_box = QHBoxLayout()
        self.url_input = QLineEdit() 
        self.url_input.setPlaceholderText(PLACEHOLDER_TEXT)
        self.url_input.setStyleSheet(f"""
            QLineEdit, QTextEdit {{ 
                background-color: #0f0f0f; border: 1px solid #2a2a2a; border-radius: 8px; 
                padding: 12px 15px; color: #fff; font-size: 13px; 
            }} 
            QLineEdit:focus, QTextEdit:focus {{ border: 1px solid {THEME_COLOR}; background-color: #141414; }}
        """)
        search_box.addWidget(self.url_input)
        
        self.btn_scan = QPushButton("Quét Dữ Liệu")
        self.btn_scan.setFixedSize(110, 45)
        self.btn_scan.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_scan.setStyleSheet(f"""
            QPushButton {{ 
                background-color: {THEME_COLOR}; border: none; border-radius: 8px; 
                color: black; font-weight: bold; font-size: 13px; 
            }} 
            QPushButton:hover {{ background-color: {HOVER_COLOR}; }} 
            QPushButton:disabled {{ background: #333; color: #777; }}
        """)
        self.btn_scan.clicked.connect(self._scan)
        search_box.addWidget(self.btn_scan)
        left_layout.addLayout(search_box)
        
        self.status_banner = QLabel("🎬 Chưa có liên kết — paste link để bắt đầu")
        self.status_banner.setStyleSheet("background-color: #0f0f0f; border: 1px solid #1f1f1f; border-radius: 8px; padding: 15px; font-size: 14px; font-weight: bold; color: #aaa;")
        left_layout.addWidget(self.status_banner)
        
        list_tools = QHBoxLayout()
        lbl_ds = QLabel("DANH SÁCH BÓC TÁCH")
        lbl_ds.setStyleSheet("color: #777; font-size: 11px; font-weight: bold; letter-spacing: 1.2px;")
        list_tools.addWidget(lbl_ds)
        list_tools.addStretch()
        
        self.btn_sel_all = QPushButton("✓ Chọn tất cả")
        self.btn_sel_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sel_all.setStyleSheet(f"QPushButton:hover {{ color: {THEME_COLOR}; border-color: {THEME_COLOR}; background-color: transparent; }}")
        self.btn_sel_all.clicked.connect(self._select_all)
        list_tools.addWidget(self.btn_sel_all)
        
        self.btn_sel_inv = QPushButton("✗ Bỏ chọn")
        self.btn_sel_inv.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sel_inv.setStyleSheet(f"QPushButton:hover {{ color: {THEME_COLOR}; border-color: {THEME_COLOR}; background-color: transparent; }}")
        self.btn_sel_inv.clicked.connect(self._invert_selection)
        list_tools.addWidget(self.btn_sel_inv)
        left_layout.addLayout(list_tools)
        
        self.v_list = QListWidget()
        self.v_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.v_list.setStyleSheet("QListWidget { background: transparent; border: none; outline: none; } QListWidget::item { border-bottom: 1px solid #1a1a1a; padding: 5px 0px; } QListWidget::item:hover { background-color: #111; }")
        left_layout.addWidget(self.v_list)
        
        dl_layout = QHBoxLayout()
        
        
        
        from PyQt6.QtWidgets import QSpinBox
        lbl_threads = QLabel("Luồng:")
        lbl_threads.setStyleSheet("color: #888; font-size: 11px; font-weight: bold;")
        dl_layout.addWidget(lbl_threads)
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 10); self.spin_threads.setValue(3); self.spin_threads.setFixedSize(55, 35)
        self.spin_threads.setStyleSheet(f"QSpinBox {{ background: #0f0f0f; border: 1px solid #2a2a2a; border-radius: 6px; color: #fff; padding: 2px 6px; font-weight: bold; }} QSpinBox:focus {{ border: 1px solid {THEME_COLOR}; }}")
        dl_layout.addWidget(self.spin_threads)
        
        self.btn_dl_sel = QPushButton("⬇ Thêm vào hàng đợi tải (0)")
        self.btn_dl_sel.setMinimumHeight(45)
        self.btn_dl_sel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_dl_sel.setStyleSheet(f"""
            QPushButton {{ background-color: #0f0f0f; border: 1px solid #2a2a2a; border-radius: 8px; color: #888; font-size: 14px; font-weight: bold; }} 
            QPushButton:enabled {{ color: {THEME_COLOR}; border: 1px solid {THEME_COLOR}; background-color: rgba(255,255,255,0.02); }} 
            QPushButton:hover:enabled {{ background-color: rgba(255,255,255,0.05); }}
        """)
        self.btn_dl_sel.setEnabled(False)
        self.btn_dl_sel.clicked.connect(self._dl_selected)
        dl_layout.addWidget(self.btn_dl_sel, stretch=1)
        
        left_layout.addLayout(dl_layout)
        
        main_h_layout.addWidget(left_panel, stretch=7)
        
        # === ĐƯỜNG PHÂN CÁCH ===
        v_line = QFrame()
        v_line.setFrameShape(QFrame.Shape.VLine)
        v_line.setStyleSheet("color: #222;")
        main_h_layout.addWidget(v_line)
        
        # === CỘT PHẢI (HÀNG ĐỢI & LOG NGƯỜI DÙNG) ===
        right_panel = QFrame()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(15)
        
        queue_header = QHBoxLayout()
        lbl_q = QLabel("HÀNG ĐỢI TẢI")
        lbl_q.setStyleSheet("color: #777; font-size: 11px; font-weight: bold; letter-spacing: 1.2px;")
        queue_header.addWidget(lbl_q)
        queue_header.addStretch()
        
        self.lbl_queue_count = QLabel("0")
        self.lbl_queue_count.setStyleSheet(f"color: {THEME_COLOR}; border: 1px solid {THEME_COLOR}; background: rgba(0,255,204,0.1); padding: 3px 15px; border-radius: 6px; font-weight: bold; font-size: 12px;")
        queue_header.addWidget(self.lbl_queue_count)
        right_layout.addLayout(queue_header)
        
        self.user_log = QTextEdit()
        self.user_log.setReadOnly(True)
        self.user_log.setPlaceholderText("Chưa có tiến trình nào đang chạy.")
        self.user_log.setStyleSheet("QTextEdit { background: transparent; color: #aaa; border: none; font-size: 12px; line-height: 1.5; }")
        right_layout.addWidget(self.user_log)
        
        self.t_bar = QProgressBar()
        self.t_bar.setFixedHeight(4)
        self.t_bar.setTextVisible(False)
        self.t_bar.setStyleSheet(f"QProgressBar {{ background: #1a1a1a; border: none; border-radius: 2px; }} QProgressBar::chunk {{ background: {THEME_COLOR}; border-radius: 2px; }}")
        right_layout.addWidget(self.t_bar)
        
        right_btns = QHBoxLayout()
        
        self.btn_pause = QPushButton("Tạm dừng")
        self.btn_pause.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._toggle_pause)
        right_btns.addWidget(self.btn_pause)
        
        self.btn_clear = QPushButton("Xóa tất cả")
        self.btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear.setStyleSheet(f"QPushButton:hover {{ color: {THEME_COLOR}; border-color: {THEME_COLOR}; background-color: transparent; }}")
        self.btn_clear.clicked.connect(self._clear_queue)
        right_btns.addWidget(self.btn_clear)
        
        right_layout.addLayout(right_btns)
        main_h_layout.addWidget(right_panel, stretch=3) 
        
        master_layout.addLayout(main_h_layout, stretch=1)
        
        # === THANH STATUS ĐÁY CÙNG ===
        bottom_bar = QFrame()
        bottom_bar.setFixedHeight(40)
        bottom_bar.setStyleSheet("background-color: #0a0a0a; border-top: 1px solid #1a1a1a;")
        b_layout = QHBoxLayout(bottom_bar)
        b_layout.setContentsMargins(20, 0, 20, 0)
        b_layout.setSpacing(15)
        
        self.dir_input = QLineEdit(self.settings.value("dir", os.path.expanduser("~/Downloads")))
        self.dir_input.setStyleSheet("background: transparent; border: none; color: #888; font-size: 12px;")
        self.dir_input.setReadOnly(True)
        b_layout.addWidget(self.dir_input)
        
        btn_change_dir = QPushButton("Thay đổi")
        btn_change_dir.setStyleSheet(f"background: transparent; border: none; color: {THEME_COLOR}; font-size: 12px; padding: 0;")
        btn_change_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_change_dir.clicked.connect(lambda: self.dir_input.setText(QFileDialog.getExistingDirectory(self) or self.dir_input.text()))
        b_layout.addWidget(btn_change_dir)
        
        btn_open_dir = QPushButton("Mở thư mục")
        btn_open_dir.setStyleSheet("background: transparent; border: none; color: #4caf50; font-size: 12px; padding: 0;")
        btn_open_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_open_dir.clicked.connect(self._open_folder)
        b_layout.addWidget(btn_open_dir)
        
        b_layout.addStretch()
        lbl_thread = QLabel(f"{PLATFORM_NAME} Downloader")
        lbl_thread.setStyleSheet("color: #555; font-size: 12px; font-weight: bold;")
        b_layout.addWidget(lbl_thread)
        
        master_layout.addWidget(bottom_bar)

    # ============================================================
    # CÁC HÀM XỬ LÝ
    # ============================================================
    def _write_hidden_log(self, msg):
        """Bắt tín hiệu Log kỹ thuật từ Thread và chuyển ngầm vào AppData"""
        if msg and msg.strip():
            self.hidden_logger.info(msg.strip())

    def _open_folder(self):
        path = self.dir_input.text().strip()
        if os.path.exists(path):
            if os.name == 'nt': os.startfile(path)
            elif sys.platform == 'darwin': subprocess.Popen(['open', path])
            else: subprocess.Popen(['xdg-open', path])
            
    def _update_card_progress(self, vid_id, pct):
        for card in self._get_cards():
            if card.vid_data["id"] == vid_id:
                if pct == -1: 
                    card.pbar.show()
                    card.pbar.setMaximum(0) 
                elif pct == 100:
                    card.pbar.setMaximum(100)
                    card.pbar.setValue(100)
                    card.pbar.setStyleSheet("QProgressBar { background: transparent; border: none; } QProgressBar::chunk { background: #4caf50; border-radius: 2px; }")
                    card.set_downloaded_state_realtime()
                else:
                    card.pbar.show()
                    card.pbar.setMaximum(100)
                    card.pbar.setValue(pct)
                break

    def _user_log(self, msg):
        self.user_log.setStyleSheet("QTextEdit { background: transparent; color: #ccc; border: none; font-size: 12px; font-style: normal; }")
        self.user_log.moveCursor(QTextCursor.MoveOperation.End)
        self.user_log.insertPlainText(msg)
        self.user_log.moveCursor(QTextCursor.MoveOperation.End)
        
    def _check_exists(self, vid_data):
        save_dir = self.dir_input.text().strip()
        is_social = "X (Twitter)" in vid_data.get("platform", "")
        
        if is_social:
            author = _sanitize(vid_data.get("author", "X_User"))
            user_dir = os.path.join(save_dir, "XDownload", author)
            if not os.path.isdir(user_dir): return False
            
            url = vid_data.get("url", "")
            m = re.search(r'/status/(\d+)', url)
            if m:
                status_id = m.group(1)
                for f in os.listdir(user_dir):
                    if status_id in f and f.endswith((".mp4", ".mkv", ".webm")): 
                        return True
        return False
    
    def _get_cards(self):
        cards = []
        for i in range(self.v_list.count()):
            w = self.v_list.itemWidget(self.v_list.item(i))
            if isinstance(w, XVideoCard): cards.append(w)
        return cards
    
    def _update_sel_count(self):
        checked = sum(1 for c in self._get_cards() if c.chk.isChecked())
        if checked > 0:
            self.btn_dl_sel.setText(f"⬇ Thêm vào hàng đợi tải ({checked})")
            self.btn_dl_sel.setEnabled(True)
        else:
            self.btn_dl_sel.setText("⬇ Thêm vào hàng đợi tải (0)")
            self.btn_dl_sel.setEnabled(False)
    
    def _select_all(self):
        for c in self._get_cards(): 
            if not c.already_exists:
                c.chk.setChecked(True)
                
    def _invert_selection(self):
        for c in self._get_cards(): c.chk.setChecked(False) 
        
    def _clear_queue(self):
        self.user_log.clear()
        self.t_bar.setValue(0)
        self.lbl_queue_count.setText("0")
        self.user_log.setStyleSheet("QTextEdit { background: transparent; color: #888; border: none; font-size: 11px; font-style: italic; }")
        
    def _scan(self):
        url = self.url_input.text().strip()
        if not url: return
        self.v_list.clear()
        self._scanned.clear()
        self.t_bar.setValue(0)
        self.user_log.clear()
        self._update_sel_count()
        self.btn_scan.setEnabled(False)
        self.btn_scan.setText("Đang quét...") 
        
        self.status_banner.setText("⏳ Đang cào dữ liệu từ DOM, vui lòng chờ...")
        self.status_banner.setStyleSheet("background-color: rgba(0, 255, 204, 0.1); border: 1px solid #00ffcc; border-radius: 8px; padding: 15px; font-size: 14px; font-weight: bold; color: #00ffcc;")
        
        self._scan_thread = XScanThread(url, get_cookie_file("x"), is_headless=True)
        self._scan_thread.log.connect(self._write_hidden_log) # Bắt log kỹ thuật ngầm
        self._scan_thread.user_log.connect(self._user_log)
        self._scan_thread.video_found.connect(self._add_video_card)
        self._scan_thread.finished_signal.connect(self._on_scan_finished)
        self._scan_thread.start()
        
    def _on_scan_finished(self, count):
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("Quét Dữ Liệu") 
        if count > 0:
            self.status_banner.setText(f"✅ Phân tích xong: Đã bắt được {count} liên kết/luồng")
            self.status_banner.setStyleSheet("background-color: rgba(76, 175, 80, 0.1); border: 1px solid #4caf50; border-radius: 8px; padding: 15px; font-size: 14px; font-weight: bold; color: #4caf50;")
        else:
            self.status_banner.setText("❌ Không bóc tách được luồng nào từ link này.")
            self.status_banner.setStyleSheet("background-color: rgba(244, 67, 54, 0.1); border: 1px solid #f44336; border-radius: 8px; padding: 15px; font-size: 14px; font-weight: bold; color: #f44336;")
        self._update_sel_count()
        
    def _add_video_card(self, v):
        self._scanned.append(v)
        exists = self._check_exists(v)
        card = XVideoCard(v, already_exists=exists)
        card.check_changed.connect(self._update_sel_count)
        
        item = QListWidgetItem(self.v_list)
        item.setSizeHint(QSize(0, 95)) 
        self.v_list.setItemWidget(item, card)
        self.v_list.scrollToBottom()
        self._update_sel_count()
        
    def _dl_selected(self):
        selected_indexes = self.v_list.selectedIndexes()
        selected_vids = []
        for i, card in enumerate(self._get_cards()):
            if card.chk.isChecked():
                selected_vids.append(self._scanned[i])
                
        if not selected_vids and selected_indexes:
            selected_vids = [self._scanned[i.row()] for i in selected_indexes]
            
        self._start_dl(selected_vids)
        
    def _start_dl(self, vids):
        if not vids: return
        self.settings.setValue("dir", self.dir_input.text().strip())
        self.t_bar.setValue(0)
        self.t_bar.setMaximum(len(vids))
        self.btn_dl_sel.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("Tạm dừng")
        self.lbl_queue_count.setText(str(len(vids)))
        self.user_log.clear()
        
        self._dl_thread = XDownloadThread(vids, self.dir_input.text().strip(), get_cookie_file("x"), self.spin_threads.value())
        self._dl_thread.log.connect(self._write_hidden_log) # Bắt log kỹ thuật ngầm
        self._dl_thread.user_log.connect(self._user_log)
        self._dl_thread.card_progress.connect(self._update_card_progress)
        self._dl_thread.total_progress.connect(lambda d, t: (self.t_bar.setMaximum(t), self.t_bar.setValue(d)))
        self._dl_thread.finished.connect(self._on_dl_finished)
        self._dl_thread.start()
        
    def _toggle_pause(self):
        if hasattr(self, '_dl_thread') and self._dl_thread.isRunning():
            is_paused = self._dl_thread.toggle_pause()
            if is_paused:
                self.btn_pause.setText("Tiếp tục")
                self.btn_pause.setStyleSheet("color: #4caf50; border-color: #4caf50;")
                self._user_log("⏸️ Đã tạm dừng\n")
            else:
                self.btn_pause.setText("Tạm dừng")
                self.btn_pause.setStyleSheet("")
                self._user_log("▶️ Đã tiếp tục\n")
                
    def _on_dl_finished(self):
        self._update_sel_count()
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("Tạm dừng")
        self.btn_pause.setStyleSheet("")
