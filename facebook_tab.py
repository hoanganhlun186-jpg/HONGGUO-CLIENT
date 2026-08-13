import os, sys, re, time, traceback
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QListWidget, QListWidgetItem, QTextEdit, QFileDialog, QProgressBar, QFrame, QSplitter, QAbstractItemView, QComboBox, QCheckBox)
from PyQt6.QtCore import Qt, QSize, QSettings, pyqtSignal, QThread
from PyQt6.QtGui import QTextCursor, QPixmap
from shared_utils import AsyncImageLoader, CREATE_NO_WINDOW, browser_launch_kwargs
from cookie_tab import get_cookie_file

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

# ============================================================
# HÀM BÓC TÁCH COOKIE (ĐỂ TIÊM VÀO BOT TRÌNH DUYỆT)
# ============================================================
def load_netscape_cookies(filepath):
    cookies = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('#HttpOnly_'):
                    line = line[10:]
                if line.startswith('#') or not line.strip(): 
                    continue
                parts = line.strip().split('\t')
                if len(parts) >= 7:
                    cookies.append({
                        "domain": parts[0],
                        "path": parts[2],
                        "secure": parts[3].upper() == "TRUE",
                        "name": parts[5],
                        "value": parts[6]
                    })
    except Exception: pass
    return cookies

_NETSCAPE_CACHE = {}

def ensure_netscape_cookiefile(filepath):
    """yt-dlp yêu cầu file cookie đúng chuẩn Netscape (có header, phân tách bằng Tab).
    Nhiều file export bị thiếu header hoặc dùng khoảng trắng -> yt-dlp báo
    'does not look like a Netscape format cookies file'. Hàm này chuẩn hoá lại,
    ghi ra file tạm hợp lệ trong AppData và trả về đường dẫn file tạm đó.
    Trả về None nếu không có gì để nạp."""
    if not filepath or not os.path.exists(filepath):
        return None
    try:
        mtime = os.path.getmtime(filepath)
    except OSError:
        mtime = 0
    cached = _NETSCAPE_CACHE.get(filepath)
    if cached and cached[0] == mtime and os.path.exists(cached[1]):
        return cached[1]

    rows = []  # (domain, subdomains, path, secure, expiry, name, value)
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            raw = f.read()
    except Exception:
        return None

    stripped = raw.lstrip()
    # Trường hợp file JSON (export từ extension kiểu EditThisCookie / Cookie-Editor)
    if stripped.startswith('[') or stripped.startswith('{'):
        try:
            import json
            data = json.loads(raw)
            if isinstance(data, dict):
                data = data.get('cookies', [])
            for c in data:
                domain = c.get('domain', '')
                if not domain:
                    continue
                rows.append((
                    domain,
                    "TRUE" if domain.startswith('.') else "FALSE",
                    c.get('path', '/') or '/',
                    "TRUE" if c.get('secure') else "FALSE",
                    str(int(c.get('expirationDate', 0)) or 0),
                    c.get('name', ''),
                    c.get('value', ''),
                ))
        except Exception:
            return None
    else:
        # File dạng text: chấp nhận cả Tab lẫn khoảng trắng, bỏ qua comment
        for line in raw.splitlines():
            if line.startswith('#HttpOnly_'):
                line = line[10:]
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) < 7:
                parts = line.split()  # dự phòng: file dùng khoảng trắng
            if len(parts) >= 7:
                rows.append((parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], " ".join(parts[6:])))

    if not rows:
        return None

    out_dir = os.path.join(os.getenv('APPDATA', os.path.expanduser('~')), 'AnhStudio', 'Cookies')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'facebook_netscape.txt')
    try:
        with open(out_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write("# This file is generated by AnhStudio. Do not edit.\n\n")
            for r in rows:
                domain, subdom, path, secure, expiry, name, value = r
                if subdom not in ("TRUE", "FALSE"):
                    subdom = "TRUE" if domain.startswith('.') else "FALSE"
                if secure not in ("TRUE", "FALSE"):
                    secure = "FALSE"
                if not str(expiry).isdigit():
                    expiry = "0"
                f.write("\t".join([domain, subdom, path or '/', secure, str(expiry), name, value]) + "\n")
    except Exception:
        return None

    _NETSCAPE_CACHE[filepath] = (mtime, out_path)
    return out_path

# ============================================================
# FACEBOOK URL DETECTION
# ============================================================
_SINGLE_VIDEO_RE = re.compile(
    r'(?:/reel/\d+|/watch/?\?v=\d+|/share/[rv]/|fb\.watch/|/videos/\d+/?$|/video\.php)',
    re.IGNORECASE
)

FB_URL_PATTERN = re.compile(
    r'https?://(?:www\.|m\.|web\.|mbasic\.)?(?:facebook\.com|fb\.watch|fb\.com)[^\s<>"\']*',
    re.IGNORECASE
)

_REEL_LINK_RE = re.compile(r'/reel/(\d+)')
_VIDEO_LINK_RE = re.compile(r'/(?:watch/?\?v=|videos/)(\d+)')

def _is_single_video(url):
    return bool(_SINGLE_VIDEO_RE.search(url))

# ============================================================
# YT-DLP OPTIONS 
# ============================================================
def _get_pw_user_data_dir():
    d = os.path.join(os.path.expanduser("~"), ".anh_studio", "fb_playwright_profile")
    os.makedirs(d, exist_ok=True)
    return d

def _build_ydl_opts(quiet=True, extract_only=True, cookiefile=None):
    opts = {
        'format': 'best[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'ignoreerrors': True,
        'no_warnings': quiet,
        'quiet': quiet,
        'extract_flat': False,
        'socket_timeout': 30,
        'retries': 3,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate',
        },
    }
    if cookiefile:
        nf = ensure_netscape_cookiefile(cookiefile)
        if nf:
            opts['cookiefile'] = nf
    if extract_only:
        opts['skip_download'] = True
    return opts

def _parse_entry(entry):
    if not entry: return None
    vid_id = entry.get('id') or entry.get('display_id') or ''
    title = entry.get('title') or entry.get('description') or 'Không có tiêu đề'
    if len(title) > 120: title = title[:117] + '...'

    formats = entry.get('formats') or []
    download_urls = []
    for f in formats:
        f_url = f.get('url')
        if not f_url: continue
        download_urls.append({
            'url': f_url,
            'format_id': f.get('format_id', ''),
            'ext': f.get('ext', 'mp4'),
            'width': f.get('width') or 0,
            'height': f.get('height') or 0,
            'filesize': f.get('filesize') or f.get('filesize_approx') or 0,
            'vcodec': f.get('vcodec', ''),
            'acodec': f.get('acodec', ''),
            'tbr': f.get('tbr') or 0,
            'format_note': f.get('format_note', ''),
        })

    download_urls.sort(key=lambda x: (
        1 if (x['vcodec'] != 'none' and x['acodec'] != 'none') else 0,
        x['height'], x['tbr']
    ), reverse=True)

    best_url = entry.get('url') or ''
    if not best_url and download_urls:
        best_url = download_urls[0]['url']

    return {
        'id': vid_id,
        'title': title,
        'desc': entry.get('description') or '',
        'author': entry.get('uploader') or entry.get('uploader_id') or 'Facebook User',
        'duration': entry.get('duration') or 0,
        'cover_url': entry.get('thumbnail') or '',
        'webpage_url': entry.get('webpage_url') or entry.get('original_url') or '',
        'download_url': best_url,
        'formats': download_urls,
        'view_count': entry.get('view_count') or 0,
        'upload_date': entry.get('upload_date') or '',
    }

# ============================================================
# VIDEO CARD — Thẻ hiển thị thông tin video
# ============================================================
class VideoCard(QWidget):
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
                background: #1877F2; border-color: #1877F2;
                image: none;
            }
            QCheckBox::indicator:hover {
                border-color: #1877F2;
            }
        """)
        self.chk.stateChanged.connect(lambda: self.check_changed.emit())
        self.card_layout.addWidget(self.chk)
        
        # --- Thumbnail ---
        self.thumb_lbl = QLabel()
        self.thumb_lbl.setFixedSize(112, 63)
        self.thumb_lbl.setStyleSheet("background: #111; border-radius: 4px; border: 1px solid #222;")
        self.thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.card_layout.addWidget(self.thumb_lbl)
        
        if vid_data.get("cover_url"):
            self.loader = AsyncImageLoader(vid_data["cover_url"], vid_data["id"])
            self.loader.image_loaded.connect(self._set_image)
            self.loader.start()
            
        # --- Thông tin video ---
        info_lay = QVBoxLayout()
        info_lay.setSpacing(4)
        self.title_lbl = QLabel(vid_data.get('title') or vid_data.get('desc') or "Không có tiêu đề")
        self.title_lbl.setWordWrap(True)
        self.title_lbl.setStyleSheet("color: #e0e0e0; font-size: 13px; font-weight: 500;")
        self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        info_lay.addWidget(self.title_lbl)
        
        author = vid_data.get('author', 'Facebook User')
        duration = vid_data.get('duration', 0)
        dur_str = ''
        if duration:
            m, s = divmod(int(duration), 60)
            h, m = divmod(m, 60)
            dur_str = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

        formats = vid_data.get('formats', [])
        res_info = ''
        if formats:
            best_h = max((f.get('height', 0) for f in formats), default=0)
            if best_h: res_info = f"  |  📐 {best_h}p"

        meta_parts = [f"👤 {author}"]
        if dur_str: meta_parts.append(f"⏱ {dur_str}")
        if vid_data.get('id'): meta_parts.append(f"🆔 {vid_data['id']}")
        meta_text = "  |  ".join(meta_parts) + res_info

        self.id_lbl = QLabel(meta_text)
        self.id_lbl.setStyleSheet("color: #666666; font-size: 11px;")
        info_lay.addWidget(self.id_lbl)
        
        self.pbar = QProgressBar()
        self.pbar.setFixedHeight(4)
        self.pbar.setTextVisible(False)
        self.pbar.setStyleSheet("""
            QProgressBar { background: transparent; border: none; }
            QProgressBar::chunk { background: #1877F2; border-radius: 2px; }
        """)
        self.pbar.hide()
        info_lay.addWidget(self.pbar)
        
        info_lay.addStretch()
        self.card_layout.addLayout(info_lay)
        self.card_layout.setStretch(2, 1)
        
        # --- Set Style ---
        if already_exists:
            self._apply_downloaded_style()
        else:
            self.main_frame.setStyleSheet("QFrame#cardFrame { background-color: transparent; border: none; }")

    def _set_image(self, vid_id, img_bytes):
        pixmap = QPixmap()
        pixmap.loadFromData(img_bytes)
        self.thumb_lbl.setPixmap(pixmap.scaled(self.thumb_lbl.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation))

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
class FacebookScanThread(QThread):
    video_found = pyqtSignal(dict)
    log_signal = pyqtSignal(str)
    user_log = pyqtSignal(str)
    finished_signal = pyqtSignal(int)
    error_signal = pyqtSignal(str)

    def __init__(self, urls: list, cookie_file: str = '', parent=None):
        super().__init__(parent)
        self.urls = urls
        self.max_scroll = 30
        self.cookie_file = cookie_file
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        total_found = 0

        for url in self.urls:
            if self._stop: break
            url = url.strip()
            if not url: continue

            if _is_single_video(url):
                self.log_signal.emit(f"🔗 Link video lẻ → Trích xuất ẩn danh: {url}\n")
                self.user_log.emit(f"🎯 Phát hiện link video lẻ: Đang trích xuất...\n")
                vid = self._ytdlp_extract_one(url)
                if vid:
                    self.video_found.emit(vid)
                    total_found += 1
                    self.log_signal.emit(f"✅ [{total_found}] {vid['title']}\n")
                    self.user_log.emit(f"✅ Đã trích xuất thành công 1 video.\n")
            else:
                self.log_signal.emit(f"📄 Link page/profile → Khởi động Bot thu thập links...\n")
                self.log_signal.emit(f"   URL: {url}\n")
                self.user_log.emit(f"🤖 Đang khởi động Bot quét kênh/page Facebook...\n")
                
                collected = self._playwright_crawl_page(url)
                if not collected:
                    self.log_signal.emit(f"⚠️ Không thu thập được link nào từ page.\n")
                    self.user_log.emit(f"⚠️ Không thu thập được link nào từ page.\n")
                    continue

                self.log_signal.emit(f"📋 Thu được {len(collected)} link → Đang bóc tách dữ liệu (Xử lý 10 link cùng lúc)...\n\n")
                self.user_log.emit(f"📋 Thu được {len(collected)} link. Đang bóc tách dữ liệu chi tiết...\n")

                with ThreadPoolExecutor(max_workers=10) as executor:
                    future_to_url = {executor.submit(self._ytdlp_extract_one, reel_url): reel_url for reel_url in collected}
                    
                    processed_count = 0
                    for future in as_completed(future_to_url):
                        if self._stop: 
                            executor.shutdown(wait=False)
                            break
                            
                        processed_count += 1
                        reel_url = future_to_url[future]
                        self.log_signal.emit(f"🔍 [{processed_count}/{len(collected)}] Đã xử lý: {reel_url}\n")
                        
                        try:
                            vid = future.result()
                            if vid:
                                self.video_found.emit(vid)
                                total_found += 1
                                self.log_signal.emit(f"   ✅ {vid['title']}\n")
                                if total_found % 5 == 0: self.user_log.emit(f"📦 Đã bóc tách được {total_found} video...\n")
                            else:
                                self.log_signal.emit(f"   ⚠️ Không bóc tách được (có thể do giới hạn của FB).\n")
                        except Exception as e:
                            self.log_signal.emit(f"   ❌ Lỗi khi bóc tách: {e}\n")

        self.log_signal.emit(f"\n🏁 Hoàn tất quét — Tìm thấy {total_found} video.\n")
        self.user_log.emit(f"🏁 Hoàn tất quét — Tìm thấy {total_found} video.\n")
        self.finished_signal.emit(total_found)

    def _ytdlp_extract_one(self, url):
        import yt_dlp
        opts = _build_ydl_opts(quiet=True, extract_only=True, cookiefile=self.cookie_file)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info: return None
            entries = info.get('entries')
            if entries:
                for e in entries:
                    if e: return _parse_entry(e)
                return None
            return _parse_entry(info)
        except Exception as e:
            msg = str(e)
            if "Cannot parse data" in msg or "Unable to extract" in msg:
                # Lỗi extractor Facebook lỗi thời -> báo rõ, không spam kỹ thuật
                if not getattr(self, "_warned_stale", False):
                    self._warned_stale = True
                    self.user_log.emit(
                        "❌ yt-dlp không đọc được dữ liệu Facebook — RẤT CÓ THỂ do yt-dlp đã cũ.\n"
                        "   👉 Hãy cập nhật yt-dlp (chạy: yt-dlp -U) hoặc thay yt-dlp.exe bản mới nhất.\n"
                    )
                self.log_signal.emit(f"   ❌ [yt-dlp cũ?] Cannot parse data: {url}\n")
            else:
                self.log_signal.emit(f"   ❌ Lỗi hệ thống yt-dlp: {e}\n")
            return None

    def _playwright_crawl_page(self, page_url):
        from playwright.sync_api import sync_playwright

        normalized = page_url.rstrip('/')
        if not any(x in normalized for x in ['/reels', '/videos', '/watch', '/reel/']):
            if 'profile.php?id=' in normalized:
                pass 
            elif '?' in normalized:
                base_url = normalized.split('?')[0]
                normalized = base_url.rstrip('/') + '/reels/'
                self.log_signal.emit(f"   → Tự động loại bỏ tham số thừa và thêm /reels/ vào link: {normalized}\n")
            else:
                normalized += '/reels/'
                self.log_signal.emit(f"   → Tự động thêm /reels/ vào link: {normalized}\n")

        collected_ids = set()
        collected_urls = []

        try:
            with sync_playwright() as pw:
                user_data_dir = _get_pw_user_data_dir()
                ctx = pw.chromium.launch_persistent_context(
                    user_data_dir,
                    **browser_launch_kwargs(
                        headless=True,
                        args=['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-dev-shm-usage'],
                        viewport={'width': 1280, 'height': 900},
                        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                        locale='vi-VN',
                    )
                )
                
                if self.cookie_file and os.path.exists(self.cookie_file):
                    self.log_signal.emit(f"   🔄 Đang ép Bot nạp Cookie Facebook...\n")
                    cks = load_netscape_cookies(self.cookie_file)
                    fb_cks = [c for c in cks if "facebook.com" in c["domain"] or "fb.com" in c["domain"]]
                    if fb_cks:
                        try:
                            ctx.add_cookies(fb_cks)
                            self.log_signal.emit(f"   ✅ Nạp thành công {len(fb_cks)} khóa đăng nhập!\n")
                        except Exception as e:
                            self.log_signal.emit(f"   ⚠️ Lỗi khi nạp cookie: {e}\n")
                
                page = ctx.new_page()
                self.log_signal.emit(f"   🌐 Đang mở trang web ẩn...\n")
                page.goto(normalized, wait_until='domcontentloaded', timeout=30000)
                page.wait_for_timeout(3000)

                if '/login' in page.url.lower() or 'login_alert' in page.url.lower():
                    self.log_signal.emit(f"   ❌ TÀI KHOẢN CHƯA ĐƯỢC KẾT NỐI!\n")
                    self.log_signal.emit(f"   Vui lòng kiểm tra lại Cookie ở Tab Cookie.\n")
                    self.user_log.emit(f"❌ Lỗi: Tài khoản chưa được đăng nhập. Hãy cập nhật lại Cookie Facebook.\n")
                    ctx.close()
                    return collected_urls

                self._close_login_popup(page)

                self.log_signal.emit(f"   🕵️ Đang kiểm tra giao diện Facebook...\n")
                reload_count = 0
                while reload_count < 5:
                    if self._stop: break
                    
                    page.mouse.wheel(0, 400)
                    page.wait_for_timeout(2000)
                    
                    temp_ids = set()
                    self._collect_links_from_page(page, temp_ids, []) 
                    
                    if len(temp_ids) > 0:
                        self.log_signal.emit(f"   ✅ Đã nhận diện được giao diện chứa Video!\n")
                        break
                        
                    reload_count += 1
                    self.log_signal.emit(f"   ⚠️ Giao diện trống, đang ép tải lại trang (F5) lần {reload_count}/5...\n")
                    page.reload(wait_until='domcontentloaded', timeout=30000)
                    page.wait_for_timeout(3000)
                    self._close_login_popup(page) 

                self.log_signal.emit(f"   📜 Bot bắt đầu cuộn trang (Tối đa {self.max_scroll} lần)...\n")
                self.user_log.emit(f"📜 Đang tự động cuộn trang (Thiết lập {self.max_scroll} lần)...\n")

                no_new_count = 0
                for scroll_i in range(self.max_scroll):
                    if self._stop: break

                    prev_count = len(collected_ids)
                    self._collect_links_from_page(page, collected_ids, collected_urls)

                    new_found = len(collected_ids) - prev_count
                    if new_found > 0:
                        self.log_signal.emit(f"   📜 Cuộn {scroll_i+1}: Tìm thêm {new_found} link (Tổng: {len(collected_ids)})\n")
                        no_new_count = 0
                    else:
                        no_new_count += 1

                    if no_new_count >= 5:
                        self.log_signal.emit(f"   ⏹ Đã lướt hết video trên kênh.\n")
                        break

                    page.mouse.wheel(0, 800)
                    page.wait_for_timeout(1500)

                self._collect_links_from_page(page, collected_ids, collected_urls)
                ctx.close()

        except Exception as e:
            self.log_signal.emit(f"   ❌ Lỗi Bot: {e}\n")
            self.error_signal.emit(str(e))

        self.log_signal.emit(f"   🔗 Hoàn tất bóc tách {len(collected_urls)} đường dẫn.\n")
        return collected_urls

    def _collect_links_from_page(self, page, collected_ids, collected_urls):
        try:
            hrefs = page.eval_on_selector_all('a[href]', 'els => els.map(e => e.getAttribute("href"))')
        except: return

        for href in hrefs:
            if not href: continue
            
            m = _REEL_LINK_RE.search(href)
            if m:
                vid_id = m.group(1)
                if vid_id not in collected_ids:
                    collected_ids.add(vid_id)
                    collected_urls.append(f"https://www.facebook.com/reel/{vid_id}")
                continue

            m = _VIDEO_LINK_RE.search(href)
            if m:
                vid_id = m.group(1)
                if vid_id not in collected_ids:
                    collected_ids.add(vid_id)
                    collected_urls.append(f"https://www.facebook.com/watch/?v={vid_id}")

    def _close_login_popup(self, page):
        try:
            selectors = [
                '[aria-label="Close"]', '[aria-label="Đóng"]',
                'div[role="dialog"] div[aria-label="Close"]', 'div[role="dialog"] div[aria-label="Đóng"]',
            ]
            for sel in selectors:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    page.wait_for_timeout(500)
                    return
        except: pass

# ============================================================
# DOWNLOAD THREAD 
# ============================================================
class FacebookDownloadThread(QThread):
    log_signal = pyqtSignal(str)
    user_log = pyqtSignal(str)
    progress_signal = pyqtSignal(int, int)
    dl_progress = pyqtSignal(float)
    card_progress = pyqtSignal(str, int)
    finished_signal = pyqtSignal(int, int)
    error_signal = pyqtSignal(str)

    def __init__(self, videos: list, save_dir: str, time_start='', time_end='', thread_count=3, cookie_file='', parent=None):
        super().__init__(parent)
        self.videos = videos
        self.save_dir = save_dir
        self.time_start = time_start.strip()
        self.time_end = time_end.strip()
        self.cookie_file = cookie_file
        self._stop = False
        self._paused = False

    def stop(self): self._stop = True
    def pause(self): self._paused = not self._paused

    def run(self):
        import yt_dlp
        import time
        os.makedirs(self.save_dir, exist_ok=True)
        success = 0
        fail = 0
        total = len(self.videos)
        
        self.user_log.emit(f"📥 Bắt đầu tải {total} video Facebook...\n")

        for idx, vid in enumerate(self.videos):
            if self._stop: break
            while self._paused and not self._stop: self.msleep(300)

            self.progress_signal.emit(idx + 1, total)
            vid_id = str(vid.get('id', ''))
            title = vid.get('title', f"video_{vid_id if vid_id else idx}")
            safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:100]
            author = vid.get('author', 'FacebookUser')
            fb_dir = os.path.join(self.save_dir, 'FacebookDownload', re.sub(r'[\\/:*?"<>|]', '_', author)[:60])
            os.makedirs(fb_dir, exist_ok=True)
            
            self.log_signal.emit(f"\n📥 [{idx+1}/{total}] Đang tải: {safe_title}\n")

            dl_url = vid.get('webpage_url') or vid.get('download_url') or ''
            if not dl_url:
                self.log_signal.emit(f"⚠️ Không có URL tải cho: {safe_title}\n")
                fail += 1
                continue

            outtmpl = os.path.join(fb_dir, f"{safe_title}.%(ext)s")
            max_retries = 3
            vid_success = False
            
            self.card_progress.emit(vid_id, -1)

            def local_hook(d):
                self._progress_hook(d) 
                if d['status'] == 'downloading':
                    t_bytes = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    d_bytes = d.get('downloaded_bytes', 0)
                    if t_bytes > 0:
                        pct = (d_bytes / t_bytes) * 100
                        self.card_progress.emit(vid_id, int(pct))
                elif d['status'] == 'finished':
                    self.card_progress.emit(vid_id, 100)

            for attempt in range(1, max_retries + 1):
                if self._stop: break
                
                opts = _build_ydl_opts(quiet=True, extract_only=False, cookiefile=self.cookie_file)
                opts['skip_download'] = False
                opts['outtmpl'] = outtmpl
                opts['progress_hooks'] = [local_hook]
                opts['retries'] = 3
                opts['fragment_retries'] = 3

                if self.time_start or self.time_end:
                    pp_opts = {}
                    if self.time_start: pp_opts['start_time'] = self._time_to_seconds(self.time_start)
                    if self.time_end: pp_opts['end_time'] = self._time_to_seconds(self.time_end)
                    if pp_opts: opts['download_ranges'] = lambda info, ydl: [pp_opts]

                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([dl_url])
                    vid_success = True
                    break 
                except Exception as e:
                    if attempt < max_retries:
                        self.log_signal.emit(f"⚠️ Lỗi tải (thử lại lần {attempt}/{max_retries}): {str(e)[:50]}...\n")
                        time.sleep(2)
                    else:
                        self.log_signal.emit(f"❌ Thất bại hoàn toàn sau {max_retries} lần thử: {e}\n")
                        self.error_signal.emit(f"{safe_title}: {e}")

            if vid_success:
                success += 1
                self.log_signal.emit(f"✅ Hoàn tất: {safe_title}\n")
                self.user_log.emit(f"✅ Tải xong ({success}/{total}): {safe_title}\n")

        self.log_signal.emit(f"\n🏁 Kết thúc tải — Thành công: {success}, Thất bại: {fail}\n")
        self.user_log.emit(f"🎉 Hoàn tất: {success}/{total} tải thành công\n")
        # Pha dịch thuật

    def _progress_hook(self, d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            if total > 0:
                pct = downloaded / total * 100
                self.dl_progress.emit(pct)
                speed = d.get('speed')
                if speed:
                    speed_mb = speed / 1024 / 1024
                    self.log_signal.emit(f"\r   ↓ {pct:.1f}% — {speed_mb:.2f} MB/s")
        elif d['status'] == 'finished':
            self.dl_progress.emit(100.0)

    @staticmethod
    def _time_to_seconds(t):
        parts = t.split(':')
        try:
            if len(parts) == 3: return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2: return int(parts[0]) * 60 + float(parts[1])
            else: return float(parts[0])
        except: return 0

# ============================================================
# FACEBOOK WIDGET — Giao diện đồng bộ
# ============================================================
class FacebookWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scanned = []
        self._scan_thread = None
        self._dl_thread = None
        self.settings = QSettings("AnhStudio", "Facebook")
        
        # ==============================================================
        # 1. CẤU HÌNH NHẬN DIỆN THƯƠNG HIỆU
        # ==============================================================
        THEME_COLOR = "#1877F2"  
        HOVER_COLOR = "#166FE5"
        PLACEHOLDER_TEXT = "🔗 Dán link Facebook vào đây (có thể dán nhiều link, mỗi dòng 1 link)...\n✅ Hỗ trợ link video lẻ hoặc link Page"
        PLATFORM_NAME = "Facebook"
        
        self.hidden_logger = setup_hidden_logger(PLATFORM_NAME)
        # ==============================================================

        self.setStyleSheet(f"""
            QWidget {{ background-color: #080808; color: #e0e0e0; font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, Arial, sans-serif; }}
            QFrame {{ border: none; }}
            QPushButton {{ background-color: #151515; border: 1px solid #2a2a2a; border-radius: 6px; color: #ccc; padding: 8px 12px; font-weight: bold; }}
            QPushButton:hover {{ background-color: #222; color: #fff; border-color: {THEME_COLOR}; }}
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
        # Đối với Facebook dùng QTextEdit để có thể paste nhiều link
        self.url_input = QTextEdit() 
        self.url_input.setFixedHeight(55)
        self.url_input.setPlaceholderText(PLACEHOLDER_TEXT)
        self.url_input.setStyleSheet(f"""
            QTextEdit {{ 
                background-color: #0f0f0f; border: 1px solid #2a2a2a; border-radius: 8px; 
                padding: 10px 15px; color: #fff; font-size: 13px; 
            }} 
            QTextEdit:focus {{ border: 1px solid {THEME_COLOR}; background-color: #141414; }}
        """)
        search_box.addWidget(self.url_input)
        
        self.btn_scan = QPushButton("Quét Dữ Liệu")
        self.btn_scan.setFixedSize(110, 55)
        self.btn_scan.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_scan.setStyleSheet(f"""
            QPushButton {{ 
                background-color: {THEME_COLOR}; border: none; border-radius: 8px; 
                color: white; font-weight: bold; font-size: 13px; 
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
        self.btn_sel_all.clicked.connect(self._select_all)
        list_tools.addWidget(self.btn_sel_all)
        
        self.btn_sel_inv = QPushButton("✗ Bỏ chọn")
        self.btn_sel_inv.setCursor(Qt.CursorShape.PointingHandCursor)
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
        self.lbl_queue_count.setStyleSheet(f"color: {THEME_COLOR}; border: 1px solid {THEME_COLOR}; background: rgba(24, 119, 242, 0.1); padding: 3px 15px; border-radius: 6px; font-weight: bold; font-size: 12px;")
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
        self.btn_clear.setStyleSheet(f"QPushButton:hover {{ color: {THEME_COLOR}; border-color: {THEME_COLOR}; }}")
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
        author = re.sub(r'[\\/:*?"<>|]', '_', vid_data.get('author', 'FacebookUser'))[:60]
        
        vid_id = str(vid_data.get('id', ''))
        title = vid_data.get('title', f"video_{vid_id}")
        safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:100]
        
        user_dir = os.path.join(save_dir, "FacebookDownload", author)
        if not os.path.isdir(user_dir): return False
        
        for f in os.listdir(user_dir):
            if safe_title in f and f.endswith((".mp4", ".mkv", ".webm")): 
                return True
        return False
    
    def _get_cards(self):
        cards = []
        for i in range(self.v_list.count()):
            w = self.v_list.itemWidget(self.v_list.item(i))
            if isinstance(w, VideoCard): cards.append(w)
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
        raw = self.url_input.toPlainText().strip()
        if not raw: return

        urls = []
        for line in raw.splitlines():
            line = line.strip()
            if line:
                found = FB_URL_PATTERN.findall(line)
                if found: urls.extend(found)
                elif line.startswith('http'): urls.append(line)

        if not urls:
            self.user_log.emit("⚠️ Không tìm thấy URL Facebook hợp lệ.\n")
            return
            
        self.v_list.clear()
        self._scanned.clear()
        self.t_bar.setValue(0)
        self.user_log.clear()
        self._update_sel_count()
        
        self.btn_scan.setEnabled(False)
        self.btn_scan.setText("Đang quét...")
        self.status_banner.setText("⏳ Đang phân tích dữ liệu, vui lòng đợi...")
        self.status_banner.setStyleSheet("background-color: rgba(24, 119, 242, 0.1); border: 1px solid #1877F2; border-radius: 8px; padding: 15px; font-size: 14px; font-weight: bold; color: #1877F2;")
        
        cookie_file = get_cookie_file("facebook")
        if cookie_file:
            self._write_hidden_log(f"   🍪 Đã nạp khóa Cookie: {os.path.basename(cookie_file)}\n")
        
        self._scan_thread = FacebookScanThread(urls, cookie_file=cookie_file, parent=self)
        self._scan_thread.log_signal.connect(self._write_hidden_log) # Bắt log kỹ thuật ẩn
        self._scan_thread.user_log.connect(self._user_log)
        self._scan_thread.video_found.connect(self._add_video_card)
        self._scan_thread.finished_signal.connect(self._on_scan_finished)
        self._scan_thread.error_signal.connect(lambda e: self._write_hidden_log(f"⚠️ {e}\n"))
        self._scan_thread.start()
        
    def _on_scan_finished(self, count):
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("Quét Dữ Liệu")
        if count > 0:
            self.status_banner.setText(f"✅ Quét thành công: Phát hiện {count} đối tượng")
            self.status_banner.setStyleSheet("background-color: rgba(76, 175, 80, 0.1); border: 1px solid #4caf50; border-radius: 8px; padding: 15px; font-size: 14px; font-weight: bold; color: #4caf50;")
        else:
            self.status_banner.setText("❌ Không tìm thấy dữ liệu. Hãy kiểm tra lại link.")
            self.status_banner.setStyleSheet("background-color: rgba(244, 67, 54, 0.1); border: 1px solid #f44336; border-radius: 8px; padding: 15px; font-size: 14px; font-weight: bold; color: #f44336;")
        self._update_sel_count()
        
    def _add_video_card(self, v):
        self._scanned.append(v)
        exists = self._check_exists(v)
        card = VideoCard(v, already_exists=exists)
        card.check_changed.connect(self._update_sel_count)
        
        item = QListWidgetItem(self.v_list)
        item.setSizeHint(QSize(0, 95)) 
        self.v_list.setItemWidget(item, card)
        self.v_list.scrollToBottom()
        self._update_sel_count()
        
    def _dl_selected(self):
        selected = []
        for i, card in enumerate(self._get_cards()):
            if card.chk.isChecked():
                selected.append(self._scanned[i])
        self._start_dl(selected)
        
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
        
        self._dl_thread = FacebookDownloadThread(vids, self.dir_input.text().strip(), thread_count=self.spin_threads.value(), cookie_file=get_cookie_file("facebook"), parent=self)
        self._dl_thread.log_signal.connect(self._write_hidden_log) # Bắt log kỹ thuật ẩn
        self._dl_thread.user_log.connect(self._user_log)
        self._dl_thread.card_progress.connect(self._update_card_progress)
        self._dl_thread.progress_signal.connect(lambda cur, _: self.t_bar.setValue(cur))
        self._dl_thread.finished_signal.connect(self._on_dl_finished)
        self._dl_thread.error_signal.connect(lambda e: self._write_hidden_log(f"⚠️ {e}\n"))
        self._dl_thread.start()
        
    def _toggle_pause(self):
        if hasattr(self, '_dl_thread') and self._dl_thread.isRunning():
            self._dl_thread.pause()
            if self._dl_thread._paused:
                self.btn_pause.setText("Tiếp tục")
                self.btn_pause.setStyleSheet("color: #4caf50; border-color: #4caf50;")
                self._user_log("⏸️ Đã tạm dừng\n")
            else:
                self.btn_pause.setText("Tạm dừng")
                self.btn_pause.setStyleSheet("")
                self._user_log("▶️ Đã tiếp tục\n")
                
    def _on_dl_finished(self, success, fail):
        self._update_sel_count()
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("Tạm dừng")
        self.btn_pause.setStyleSheet("")
        self.t_bar.setValue(self.t_bar.maximum())
