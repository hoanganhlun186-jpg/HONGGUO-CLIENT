import os, sys, re, threading, subprocess, concurrent.futures, time
import logging
import urllib.request, urllib.parse
from datetime import datetime
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QListWidget, QListWidgetItem, QTextEdit, QFileDialog, QProgressBar, QFrame, QSplitter, QAbstractItemView, QSizePolicy, QComboBox, QCheckBox)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QSettings, QSize
from PyQt6.QtGui import QTextCursor, QPixmap, QColor, QIcon
from shared_utils import AsyncImageLoader, CREATE_NO_WINDOW, browser_launch_kwargs, get_ytdlp_path
from cookie_tab import get_cookie_file

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0.0.0 Safari/537.36"

# File lưu session đăng nhập Douyin (giống gemini_auth.json). Lưu cạnh app.
DOUYIN_AUTH_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "douyin_auth.json")


def _douyin_logged_in() -> bool:
    """True nếu đã có file session Douyin (đã đăng nhập trước đó)."""
    try:
        return os.path.exists(DOUYIN_AUTH_FILE) and os.path.getsize(DOUYIN_AUTH_FILE) > 10
    except Exception:
        return False


class DouyinLoginThread(QThread):
    """Mở Chrome cho người dùng tự đăng nhập Douyin, rồi LƯU session vào
    douyin_auth.json (giống cơ chế login Gemini). Lần sau quét dùng lại
    session này, không cần file cookies.txt."""
    log = pyqtSignal(str)
    finished_signal = pyqtSignal(bool)

    def run(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.log.emit("❌ Thiếu thư viện Playwright!\n")
            self.finished_signal.emit(False)
            return

        self.log.emit("🔑 Đang mở trình duyệt để đăng nhập Douyin...\n")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(**browser_launch_kwargs(
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled", "--disable-gpu", "--no-sandbox"]
                ))
                ctx = browser.new_context(viewport={"width": 1280, "height": 900}, user_agent=UA)
                ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)

                self.log.emit("⏳ Hãy ĐĂNG NHẬP Douyin trên cửa sổ vừa mở. "
                              "Tool sẽ tự lưu khi phát hiện đã đăng nhập (chờ tối đa 5 phút)...\n")

                logged_in = False
                for _ in range(100):   # ~5 phút (100 x 3s)
                    if self.isInterruptionRequested():
                        break
                    try:
                        # Đăng nhập xong Douyin đặt cookie sessionid / sessionid_ss
                        cks = ctx.cookies()
                        has_session = any(
                            c.get("name") in ("sessionid", "sessionid_ss")
                            and len(str(c.get("value") or "")) >= 20
                            for c in cks
                        )
                        if has_session:
                            logged_in = True
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(3000)

                if logged_in:
                    ctx.storage_state(path=DOUYIN_AUTH_FILE)
                    self.log.emit("✅ Đăng nhập thành công! Đã lưu phiên đăng nhập Douyin.\n")
                    page.wait_for_timeout(800)
                    try: ctx.close()
                    except Exception: pass
                    try: browser.close()
                    except Exception: pass
                    self.finished_signal.emit(True)
                    return

                self.log.emit("⏱️ Hết thời gian chờ đăng nhập (5 phút). Chưa lưu được phiên.\n")
                try: ctx.close()
                except Exception: pass
                try: browser.close()
                except Exception: pass
                self.finished_signal.emit(False)
        except Exception as e:
            self.log.emit(f"❌ Lỗi khi đăng nhập Douyin: {e}\n")
            self.finished_signal.emit(False)


def _sanitize(name): 
    return re.sub(r'[<>:"/\\|?*\n\r\t]', '_', name).strip()[:60]

# ============================================================
# HÀM LƯU LOG ẨN VÀO APPDATA
# ============================================================
def setup_hidden_logger(platform_name):
    appdata_dir = os.getenv('APPDATA', os.path.expanduser('~'))
    log_dir = os.path.join(appdata_dir, 'BoomStudio', 'Logs')
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
                background: #fe2c55; border-color: #fe2c55;
                image: none;
            }
            QCheckBox::indicator:hover {
                border-color: #fe2c55;
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
        
        self.title_lbl = QLabel(vid_data.get('desc') or "Không có tiêu đề")
        self.title_lbl.setWordWrap(True)
        self.title_lbl.setStyleSheet("color: #e0e0e0; font-size: 13px; font-weight: 500;")
        self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        info_lay.addWidget(self.title_lbl)
        
        author = vid_data.get('author', 'DouyinUser')
        status_text = f"👤 {author}   |   🆔 {vid_data.get('id')}"
        self.id_lbl = QLabel(status_text)
        self.id_lbl.setStyleSheet("color: #666666; font-size: 11px;")
        info_lay.addWidget(self.id_lbl)

        if any(k in vid_data for k in ("likes", "comments", "shares")):
            def _fmt(n):
                n = n or 0
                if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
                if n >= 1_000: return f"{n/1_000:.1f}K"
                return str(n)
            stats_text = f"❤️ {_fmt(vid_data.get('likes', 0))}   💬 {_fmt(vid_data.get('comments', 0))}   🔁 {_fmt(vid_data.get('shares', 0))}"
            self.stats_lbl = QLabel(stats_text)
            self.stats_lbl.setStyleSheet("color: #fe2c55; font-size: 11px; font-weight: bold;")
            info_lay.addWidget(self.stats_lbl)
        
        self.pbar = QProgressBar()
        self.pbar.setFixedHeight(4)
        self.pbar.setTextVisible(False)
        self.pbar.setStyleSheet("""
            QProgressBar { background: transparent; border: none; }
            QProgressBar::chunk { background: #fe2c55; border-radius: 2px; }
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
            self.main_frame.setStyleSheet("""
                QFrame#cardFrame {
                    background-color: transparent;
                    border: none;
                }
            """)

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

class TrendCard(QWidget):
    scan_requested = pyqtSignal(str)

    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.data = data

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(4, 4, 4, 4)

        frame = QFrame()
        frame.setObjectName("trendFrame")
        frame.setStyleSheet("QFrame#trendFrame { background-color: transparent; border: none; }")
        outer_layout.addWidget(frame)

        lay = QHBoxLayout(frame)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(15)

        rank = data.get("rank", 0)
        rank_color = "#fe2c55" if rank <= 3 else "#666"
        rank_lbl = QLabel(f"#{rank}")
        rank_lbl.setFixedWidth(34)
        rank_lbl.setStyleSheet(f"color: {rank_color}; font-size: 15px; font-weight: bold;")
        lay.addWidget(rank_lbl)

        self.thumb_lbl = QLabel()
        self.thumb_lbl.setFixedSize(50, 50)
        self.thumb_lbl.setStyleSheet("background: #111; border-radius: 4px; border: 1px solid #222;")
        self.thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.thumb_lbl)
        if data.get("cover_url"):
            self.loader = AsyncImageLoader(data["cover_url"], f"trend_{rank}")
            self.loader.image_loaded.connect(self._set_image)
            self.loader.start()

        info_lay = QVBoxLayout()
        info_lay.setSpacing(4)
        title_lbl = QLabel(data.get("word", ""))
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet("color: #e0e0e0; font-size: 13px; font-weight: 500;")
        info_lay.addWidget(title_lbl)

        hv = data.get("hot_value", 0) or 0
        sub_lbl = QLabel(f"🔥 Độ hot: {hv:,}".replace(",", "."))
        sub_lbl.setStyleSheet("color: #666; font-size: 11px;")
        info_lay.addWidget(sub_lbl)
        lay.addLayout(info_lay)
        lay.setStretch(2, 1)

        btn = QPushButton("🔍 Quét video")
        btn.setFixedWidth(100)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet("QPushButton { background-color: #1a1a1a; border: 1px solid #333; border-radius: 6px; color: #ccc; padding: 6px; } QPushButton:hover { border-color: #fe2c55; color: #fff; }")
        btn.clicked.connect(lambda: self.scan_requested.emit(self.data.get("word", "")))
        lay.addWidget(btn)

    def _set_image(self, _id, img_bytes):
        pixmap = QPixmap()
        pixmap.loadFromData(img_bytes)
        self.thumb_lbl.setPixmap(pixmap.scaled(self.thumb_lbl.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation))


def _fetch_douyin_csrf():
    """Lấy passport_csrf_token công khai của Douyin (không cần đăng nhập)."""
    req = urllib.request.Request(
        "https://www.douyin.com/passport/general/login_guiding_strategy/"
        "?device_platform=webapp&aid=6383&channel=channel_pc_web",
        headers={"User-Agent": UA}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        for c in (resp.headers.get_all("Set-Cookie") or []):
            if "passport_csrf_token=" in c:
                return c.split("passport_csrf_token=", 1)[1].split(";", 1)[0]
    return ""

# ============================================================
# HOT THREAD — Quét bảng xếp hạng "Thịnh hành" (Hot Search)
# ============================================================
def _get_real_chrome_profile_copy():
    """
    Sao chép nhanh profile Chrome thật ('Default') của người dùng sang thư mục tạm,
    để Playwright chạy trên đó -> trông giống hệt trình duyệt thật đã đăng nhập
    (có cache, lịch sử, dấu vân tay thiết bị quen thuộc), giảm khả năng bị Douyin
    nghi ngờ là bot và đòi xác minh lại. Trả về None nếu không tìm thấy / lỗi.
    """
    import shutil, tempfile
    if sys.platform != "win32":
        return None
    src = os.path.join(os.getenv("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
    if not os.path.isdir(src):
        return None
    dst = os.path.join(tempfile.gettempdir(), "DouyinTool_ChromeProfile")
    try:
        default_src = os.path.join(src, "Default")
        default_dst = os.path.join(dst, "Default")
        # Chỉ copy lại nếu chưa có bản sao, để lần chạy sau nhanh hơn (đăng nhập vẫn giữ nguyên)
        if not os.path.isdir(default_dst) and os.path.isdir(default_src):
            os.makedirs(dst, exist_ok=True)
            shutil.copytree(
                default_src, default_dst,
                ignore=shutil.ignore_patterns("Cache", "Code Cache", "GPUCache", "*.log", "Service Worker")
            )
            local_state_src = os.path.join(src, "Local State")
            if os.path.isfile(local_state_src):
                shutil.copy2(local_state_src, os.path.join(dst, "Local State"))
        return dst if os.path.isdir(default_dst) else None
    except Exception:
        return None

class DouyinHotThread(QThread):
    hot_found = pyqtSignal(dict)
    finished_signal = pyqtSignal(int)
    error = pyqtSignal(str)

    def run(self):
        import json as _json
        try:
            csrf = ""
            try:
                csrf = _fetch_douyin_csrf()
            except Exception:
                pass

            url = ("https://www.douyin.com/aweme/v1/web/hot/search/list/"
                   "?device_platform=webapp&aid=6383&channel=channel_pc_web&detail_list=1")
            headers = {"User-Agent": UA, "Referer": "https://www.douyin.com/"}
            if csrf:
                headers["Cookie"] = f"passport_csrf_token={csrf}"

            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            data = _json.loads(raw)
            words = (data.get("data") or {}).get("word_list") or []

            count = 0
            for i, w in enumerate(words, 1):
                cover = ""
                try:
                    cover = (w.get("word_cover") or {}).get("url_list", [""])[0]
                except Exception:
                    pass
                item = {
                    "rank": i,
                    "word": w.get("word", "") or w.get("sentence", ""),
                    "hot_value": w.get("hot_value", 0),
                    "cover_url": cover,
                    "sentence_id": w.get("sentence_id", ""),
                }
                if item["word"]:
                    self.hot_found.emit(item)
                    count += 1
            self.finished_signal.emit(count)
        except Exception as e:
            self.error.emit(str(e))
            self.finished_signal.emit(0)

# ============================================================
# SCAN THREAD
# ============================================================
class DouyinScanThread(QThread):
    log = pyqtSignal(str)        
    user_log = pyqtSignal(str)   
    video_found = pyqtSignal(dict)
    finished_signal = pyqtSignal(int)
    
    def __init__(self, url, cookie_file, sort_type=0, publish_time=0):
        super().__init__()
        self.url = url
        self.cookie_file = cookie_file
        self.sort_type = sort_type
        self.publish_time = publish_time
        self._cancel = False
        self.seen_ids = set()
        self._force_visible = False   # True -> mở trình duyệt HIỆN để tự giải captcha
        
    def cancel(self):
        self._cancel = True
        
    def _detect_single_video(self, url):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        m = re.search(r'/(?:video|note)/(\d+)', url)
        if m: return m.group(1)
        if "v.douyin.com" in url: return url 
        if "modal_id" in params: return params["modal_id"][0]
        return None

    def _fetch_single_info(self, video_url):
        import json as _json
        base_cmd = [get_ytdlp_path(), "--dump-json", "--no-warnings", "--referer", "https://www.douyin.com/", "--add-header", f"User-Agent:{UA}"]
        attempts = []
        if self.cookie_file and os.path.exists(self.cookie_file): attempts.append(("Cookie file", ["--cookies", self.cookie_file]))
        for browser_name in ["chrome", "edge", "chromium"]: attempts.append((f"Cookie từ {browser_name}", ["--cookies-from-browser", browser_name]))
        attempts.append(("Không cookie", []))
        
        kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
        for strategy_name, extra_args in attempts:
            if self._cancel: return None
            cmd = base_cmd + extra_args + [video_url]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, **kw)
                if proc.returncode == 0 and proc.stdout.strip():
                    info = _json.loads(proc.stdout.strip())
                    vid_id = str(info.get("id") or info.get("display_id") or "")
                    title = info.get("title") or info.get("description") or "Không có tiêu đề"
                    author = info.get("uploader") or info.get("channel") or "DouyinUser"
                    thumbs = info.get("thumbnails", [])
                    cover = thumbs[-1]["url"] if thumbs else (info.get("thumbnail") or "")
                    return {
                        "id": vid_id, "url": video_url, "platform": "douyin", "desc": title, "author": author,
                        "cover_url": cover, "_cookie_args": extra_args,
                        "likes": info.get("like_count", 0) or 0,
                        "comments": info.get("comment_count", 0) or 0,
                        "shares": info.get("repost_count", 0) or 0,
                    }
            except:
                pass
        return None

    def run(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.log.emit("❌ Thiếu thư viện Playwright!\n")
            self.finished_signal.emit(0)
            return
            
        raw = self.url.strip()
        if raw.startswith("http"):
            url = raw
        elif re.match(r'^[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}(/|$)', raw):
            # Trông giống 1 tên miền/link thiếu scheme (vd: douyin.com/video/123)
            url = "https://" + raw
        else:
            # Không phải link -> coi là từ khóa, tự build link tìm kiếm Douyin
            url = (f"https://www.douyin.com/search/{urllib.parse.quote(raw)}"
                   f"?type=video&sort_type={self.sort_type}&publish_time={self.publish_time}")
            self.log.emit(f"🔤 Nhận diện là từ khóa, tự chuyển thành link tìm kiếm.\n")
        self.log.emit(f"🚀 QUÉT DOUYIN: {url}\n")
        
        single_id = self._detect_single_video(url)
        if single_id:
            self.user_log.emit(f"🎯 Phát hiện link video lẻ\n")
            video_url = single_id if single_id.startswith("http") else f"https://www.douyin.com/video/{single_id}"
            v = self._fetch_single_info(video_url)
            if v:
                self.video_found.emit(v)
                self.user_log.emit(f"✅ Đã phát hiện 1 video\n")
                self.finished_signal.emit(1)
            else:
                self.user_log.emit(f"❌ Không lấy được thông tin video. Hãy kiểm tra link hoặc cookie.\n")
                self.finished_signal.emit(0)
            return
        
        self.user_log.emit(f"🔍 Đang quét kênh Douyin...\n")
        total_videos = 0
        is_search = "/search/" in url
        # Khi cần tự giải captcha -> coi như phải hiện trình duyệt (như trang search)
        show_browser = is_search or self._force_visible
        # ƯU TIÊN phiên đăng nhập đã lưu qua nút Đăng nhập (douyin_auth.json).
        # Chỉ khi CHƯA đăng nhập mới thử copy profile Chrome thật để né captcha.
        has_saved_login = _douyin_logged_in()
        real_profile_dir = None
        if show_browser and not has_saved_login:
            real_profile_dir = _get_real_chrome_profile_copy()
        used_persistent = False
        try:
            with sync_playwright() as p:
                if real_profile_dir:
                    # Dùng bản sao profile Chrome thật (đã đăng nhập sẵn) -> giống hệt
                    # trình duyệt thật của người dùng, hạn chế bị Douyin đòi xác minh lại.
                    self.log.emit(f"🧬 Dùng bản sao profile Chrome thật: {real_profile_dir}\n")
                    try:
                        ctx = p.chromium.launch_persistent_context(
                            real_profile_dir, channel="chrome", headless=False,
                            viewport={"width": 1280, "height": 720}, user_agent=UA,
                            args=["--disable-blink-features=AutomationControlled"]
                        )
                        used_persistent = True
                        browser = None
                    except Exception as e:
                        self.log.emit(f"⚠️ Không dùng được profile Chrome thật ({e}), chuyển sang cách cũ.\n")
                        real_profile_dir = None

                if not real_profile_dir:
                    # Mở trình duyệt: ẩn khi quét thường, hiện khi cần giải captcha.
                    browser = p.chromium.launch(**browser_launch_kwargs(
                        headless=not show_browser,
                        args=["--disable-blink-features=AutomationControlled", "--disable-gpu", "--no-sandbox"]
                    ))
                    # Dùng phiên đăng nhập đã lưu (douyin_auth.json) nếu có.
                    if has_saved_login:
                        self.log.emit("🔐 Dùng phiên đăng nhập Douyin đã lưu.\n")
                        ctx = browser.new_context(
                            viewport={"width": 1280, "height": 720}, user_agent=UA,
                            storage_state=DOUYIN_AUTH_FILE)
                    else:
                        self.log.emit("⚠️ Chưa đăng nhập Douyin — hãy vào tab Cookie bấm 'Đăng nhập' để quét ổn định hơn.\n")
                        ctx = browser.new_context(viewport={"width": 1280, "height": 720}, user_agent=UA)
                page = ctx.new_page()
                def route_intercept(route):
                    if route.request.resource_type in ["image", "media", "font", "stylesheet"]: route.abort()
                    else: route.continue_()
                # Khi HIỂN THỊ trình duyệt cho người dùng (search hoặc giải
                # captcha) thì KHÔNG chặn ảnh/media để họ thấy captcha bình thường.
                if not show_browser:
                    page.route("**/*", route_intercept)
                def _emit_aweme_item(it):
                    nonlocal total_videos
                    vid_id = str(it.get("aweme_id") or "")
                    if vid_id and vid_id not in self.seen_ids:
                        self.seen_ids.add(vid_id)
                        play_addr = it.get("video", {}).get("play_addr", {}).get("url_list", [])
                        cover_url = it.get("video", {}).get("cover", {}).get("url_list", [""])[0]
                        stats = it.get("statistics", {}) or {}
                        v = {
                            "id": vid_id, "url": play_addr[0] if play_addr else "", "platform": "douyin",
                            "desc": it.get("desc", ""), "author": it.get("author", {}).get("nickname", "DouyinUser"),
                            "cover_url": cover_url,
                            "likes": stats.get("digg_count", 0),
                            "comments": stats.get("comment_count", 0),
                            "shares": stats.get("share_count", 0),
                        }
                        total_videos += 1
                        self.video_found.emit(v)
                        self.log.emit(f"🔎 Quét thấy: {vid_id} (👍 {v['likes']})\n")
                        if total_videos % 10 == 0: self.user_log.emit(f"📦 Đã phát hiện {total_videos} video...\n")

                def on_resp(resp):
                    try:
                        url_l = resp.url
                        if "aweme/v1/web/aweme/post" in url_l:
                            # Trang cá nhân (channel/profile)
                            for it in resp.json().get("aweme_list", []):
                                _emit_aweme_item(it)
                        elif ("aweme/v1/web/general/search/single" in url_l
                              or "aweme/v1/web/search/item" in url_l):
                            # Trang tìm kiếm (dùng khi quét theo từ khóa thịnh hành)
                            for entry in resp.json().get("data", []):
                                it = entry.get("aweme_info") or entry.get("aweme")
                                if it: _emit_aweme_item(it)
                        # --- DEBUG: ghi lại MỌI request liên quan aweme/search để chẩn đoán endpoint thật ---
                        if ("aweme" in url_l or "/search" in url_l) and "static" not in url_l:
                            try:
                                body_preview = resp.text()[:200]
                            except Exception:
                                body_preview = "(không đọc được body)"
                            self.log.emit(f"🩺 DEBUG [{resp.status}] {url_l}\n    ↳ {body_preview}\n")
                    except: pass
                page.on("response", on_resp)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2000)

                if show_browser:
                    # Nếu Douyin yêu cầu xác minh -> đợi người dùng tự giải captcha trên cửa sổ vừa mở
                    try:
                        title = page.title()
                    except Exception:
                        title = ""
                    if "验证" in title or "验证码" in title:
                        self.user_log.emit("🧩 Douyin yêu cầu xác minh! Hãy giải captcha trên cửa sổ trình duyệt vừa mở, tool sẽ tự quét tiếp sau khi bạn giải xong (tối đa 5 phút chờ)...\n")
                        self.log.emit(f"🧩 Gặp trang captcha: '{title}'. Đợi người dùng tự giải...\n")
                        waited = 0
                        while waited < 300:  # tối đa 5 phút
                            if self._cancel: break
                            page.wait_for_timeout(3000)
                            waited += 3
                            try:
                                cur_title = page.title()
                            except Exception:
                                cur_title = title
                            if "验证" not in cur_title:
                                self.user_log.emit("✅ Đã qua trang xác minh, tiếp tục quét...\n")
                                self.log.emit("✅ Đã qua captcha, tiếp tục quét.\n")
                                page.wait_for_timeout(1500)
                                break
                        else:
                            self.user_log.emit("⏱️ Hết thời gian chờ giải captcha (5 phút).\n")
                            self.log.emit("⏱️ Timeout chờ captcha.\n")

                prev_len, retries = 0, 0
                # Ở chế độ HIỆN trình duyệt (để tự giải captcha) thì kiên nhẫn
                # hơn: chờ lâu hơn trước khi bỏ cuộc, và nhắc người dùng giải.
                captcha_hinted = False
                max_retries = 40 if show_browser else 20
                for _ in range(1500): 
                    if self._cancel: break
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                    page.wait_for_timeout(2500) 
                    
                    if total_videos == prev_len:
                        retries += 1
                        if retries >= 10: 
                            page.mouse.click(500, 500)
                            page.wait_for_timeout(1500)
                        # Ở chế độ hiện: nếu vẫn 0 video sau 1 lúc -> nhắc giải captcha
                        if show_browser and total_videos == 0 and retries == 6 and not captcha_hinted:
                            captcha_hinted = True
                            self.user_log.emit(
                                "🧩 Nếu thấy trang xác minh/captcha trên cửa sổ trình duyệt, "
                                "hãy giải xong — tool sẽ tự quét tiếp (chờ tối đa ~1.5 phút).\n")
                        if retries >= max_retries: 
                            self.user_log.emit(f"⚠️ Đã chạm đáy trang hoặc bị Douyin chặn đăng nhập...\n")
                            self.log.emit("⚠️ Dừng do chạm đáy trang hoặc block.\n")
                            break
                    else:
                        retries = 0
                        prev_len = total_videos
                if used_persistent:
                    ctx.close()
                else:
                    browser.close()
        except Exception as e:
            self.log.emit(f"❌ Lỗi hệ thống: {str(e)}\n")
            pass
            
        self.log.emit(f"🏁 TỔNG KẾT: {total_videos} video.\n")

        # Quét ẩn ra 0 video + CHƯA thử hiện trình duyệt + chưa bị hủy
        # -> nhiều khả năng dính captcha/chặn đăng nhập. Tự MỞ trình duyệt
        # hiện lên để người dùng tự giải, rồi quét lại 1 lần.
        if (total_videos == 0 and not self._force_visible and not self._cancel):
            self.user_log.emit(
                "🧩 Không lấy được video (có thể dính captcha). "
                "Đang mở trình duyệt để bạn tự xác minh...\n")
            self.log.emit("🧩 0 video ở chế độ ẩn -> thử lại ở chế độ HIỆN trình duyệt.\n")
            self._force_visible = True
            self.seen_ids = set()   # quét lại từ đầu
            return self.run()       # chạy lại 1 lần ở chế độ hiện

        self.user_log.emit(f"🏁 Hoàn tất — Tổng cộng {total_videos} video\n")
        self.finished_signal.emit(total_videos)

# ============================================================
# DOWNLOAD THREAD 
# ============================================================
class DouyinDownloadThread(QThread):
    log = pyqtSignal(str) # Bổ sung thêm tín hiệu log kỹ thuật
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
                
    def cancel(self):
        self._cancel = True
        self.pause_event.set()
        
    def toggle_pause(self):
        if self._is_paused:
            self._is_paused = False
            self.pause_event.set()
            return False
        else:
            self._is_paused = True
            self.pause_event.clear()
            return True
            
    def run(self):
        total = len(self.videos)
        self.log.emit(f"\n📥 BẮT ĐẦU TẢI {total} VIDEO DOUYIN...\n")
        self.user_log.emit(f"📥 Bắt đầu tải {total} video ({self.thread_count} luồng)...\n")
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.thread_count) as ex:
            futs = {ex.submit(self._dl_worker, v, i, total): v for i, v in enumerate(self.videos, 1)}
            concurrent.futures.wait(futs)
            
        self.log.emit(f"🎉 HOÀN TẤT TẢI: {self.success_count}/{total} video.\n")
        self.user_log.emit(f"🎉 Hoàn tất: {self.success_count}/{total} tải thành công\n")
        

        with self.lock:
            self.done_count += 1
            if success:
                self.success_count += 1
                self.log.emit(f"✅ [XONG] {vid_id}\n")
                self.user_log.emit(f"✅ Tải xong ({self.success_count}/{tot}): {vid_id}\n")
            else:
                self.log.emit(f"❌ [BỎ QUA] {vid_id} do lỗi.\n")
                self.user_log.emit(f"❌ Lỗi mạng: {vid_id}\n")
            self.total_progress.emit(self.done_count, tot)
        return success

# ============================================================
# DOUYIN WIDGET — Giao diện đồng bộ
# ============================================================
class DouyinWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scanned = []
        self.settings = QSettings("BoomStudio", "Douyin")
        
        # ==============================================================
        # 1. CẤU HÌNH NHẬN DIỆN THƯƠNG HIỆU
        # ==============================================================
        THEME_COLOR = "#fe2c55"  
        HOVER_COLOR = "#ff4b72"
        PLACEHOLDER_TEXT = "🔗 Dán share link Douyin kênh hoặc ID video lẻ vào đây..."
        PLATFORM_NAME = "Douyin"
        
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
                color: white; font-weight: bold; font-size: 13px; 
            }} 
            QPushButton:hover {{ background-color: {HOVER_COLOR}; }} 
            QPushButton:disabled {{ background: #333; color: #777; }}
        """)
        self.btn_scan.clicked.connect(self._scan)
        search_box.addWidget(self.btn_scan)

        self.btn_hot = QPushButton("🔥 Thịnh hành")
        self.btn_hot.setFixedSize(110, 45)
        self.btn_hot.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_hot.setStyleSheet("""
            QPushButton {
                background-color: #1a1a1a; border: 1px solid #333; border-radius: 8px;
                color: #ccc; font-weight: bold; font-size: 13px;
            }
            QPushButton:hover { border-color: #fe2c55; color: #fff; }
            QPushButton:disabled { background: #333; color: #777; }
        """)
        self.btn_hot.clicked.connect(self._scan_hot)
        search_box.addWidget(self.btn_hot)
        left_layout.addLayout(search_box)

        filter_box = QHBoxLayout()
        filter_box.setSpacing(10)
        cbo_style = """
            QComboBox { background-color: #0f0f0f; border: 1px solid #2a2a2a; border-radius: 6px; padding: 6px 10px; color: #ccc; font-size: 12px; }
            QComboBox:hover { border-color: #555; }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView { background-color: #151515; color: #ccc; selection-background-color: #fe2c55; }
        """
        lbl_sort = QLabel("Sắp xếp:")
        lbl_sort.setStyleSheet("color: #777; font-size: 12px;")
        filter_box.addWidget(lbl_sort)
        self.cbo_sort = QComboBox()
        self.cbo_sort.addItem("Liên quan", 0)
        self.cbo_sort.addItem("Nhiều lượt thích nhất", 1)
        self.cbo_sort.addItem("Mới nhất", 2)
        self.cbo_sort.setStyleSheet(cbo_style)
        filter_box.addWidget(self.cbo_sort)

        lbl_time = QLabel("Thời gian:")
        lbl_time.setStyleSheet("color: #777; font-size: 12px;")
        filter_box.addWidget(lbl_time)
        self.cbo_time = QComboBox()
        self.cbo_time.addItem("Không giới hạn", 0)
        self.cbo_time.addItem("Trong 1 ngày", 1)
        self.cbo_time.addItem("Trong 1 tuần", 7)
        self.cbo_time.addItem("Trong 6 tháng", 180)
        self.cbo_time.setStyleSheet(cbo_style)
        filter_box.addWidget(self.cbo_time)
        filter_box.addStretch()
        left_layout.addLayout(filter_box)
        
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
        self.lbl_queue_count.setStyleSheet(f"color: {THEME_COLOR}; border: 1px solid {THEME_COLOR}; background: rgba(254, 44, 85, 0.1); padding: 3px 15px; border-radius: 6px; font-weight: bold; font-size: 12px;")
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
        vid_id = str(vid_data.get("id", ""))
        
        author = _sanitize(vid_data.get("author", "DouyinUser"))
        user_dir = os.path.join(save_dir, "DouyinDownload", author)
        
        if not os.path.isdir(user_dir): return False
        for f in os.listdir(user_dir):
            if vid_id in f and f.endswith(".mp4"): return True
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
        url = self.url_input.text().strip()
        if not url: return
        self.v_list.clear()
        self._scanned.clear()
        self.t_bar.setValue(0)
        self.user_log.clear()
        self._update_sel_count()
        self.btn_scan.setEnabled(False)
        self.btn_scan.setText("Đang quét...")
        
        # Banner đang xử lý (Màu Theme)
        self.status_banner.setText("⏳ Đang phân tích dữ liệu, vui lòng đợi...")
        self.status_banner.setStyleSheet("background-color: rgba(254, 44, 85, 0.1); border: 1px solid #fe2c55; border-radius: 8px; padding: 15px; font-size: 14px; font-weight: bold; color: #fe2c55;")
        
        sort_type = self.cbo_sort.currentData()
        publish_time = self.cbo_time.currentData()
        self._scan_thread = DouyinScanThread(url, get_cookie_file("douyin"), sort_type, publish_time)
        self._scan_thread.log.connect(self._write_hidden_log) # Bắt log kỹ thuật ẩn
        self._scan_thread.user_log.connect(self._user_log)
        self._scan_thread.video_found.connect(self._add_video_card)
        self._scan_thread.finished_signal.connect(self._on_scan_finished)
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
        
    def _scan_hot(self):
        self.v_list.clear()
        self._scanned.clear()
        self.t_bar.setValue(0)
        self.user_log.clear()
        self._update_sel_count()
        self.btn_hot.setEnabled(False)
        self.btn_hot.setText("Đang tải...")

        self.status_banner.setText("⏳ Đang tải bảng xếp hạng thịnh hành...")
        self.status_banner.setStyleSheet("background-color: rgba(254, 44, 85, 0.1); border: 1px solid #fe2c55; border-radius: 8px; padding: 15px; font-size: 14px; font-weight: bold; color: #fe2c55;")

        self._hot_thread = DouyinHotThread()
        self._hot_thread.hot_found.connect(self._add_trend_card)
        self._hot_thread.finished_signal.connect(self._on_hot_finished)
        self._hot_thread.error.connect(lambda msg: self._user_log(f"⚠️ Lỗi tải thịnh hành: {msg}\n"))
        self._hot_thread.start()

    def _add_trend_card(self, item):
        card = TrendCard(item)
        card.scan_requested.connect(self._use_trend)
        list_item = QListWidgetItem(self.v_list)
        list_item.setSizeHint(QSize(0, 70))
        self.v_list.setItemWidget(list_item, card)

    def _on_hot_finished(self, count):
        self.btn_hot.setEnabled(True)
        self.btn_hot.setText("🔥 Thịnh hành")
        if count > 0:
            self.status_banner.setText(f"✅ Đã tải {count} từ khóa đang thịnh hành. Bấm 'Quét video' ở mỗi mục để tìm video liên quan.")
            self.status_banner.setStyleSheet("background-color: rgba(76, 175, 80, 0.1); border: 1px solid #4caf50; border-radius: 8px; padding: 15px; font-size: 14px; font-weight: bold; color: #4caf50;")
        else:
            self.status_banner.setText("❌ Không lấy được bảng thịnh hành (có thể do mạng/chặn khu vực). Xem log kỹ thuật để biết chi tiết.")
            self.status_banner.setStyleSheet("background-color: rgba(244, 67, 54, 0.1); border: 1px solid #f44336; border-radius: 8px; padding: 15px; font-size: 14px; font-weight: bold; color: #f44336;")

    def _use_trend(self, keyword):
        if not keyword: return
        sort_type = self.cbo_sort.currentData()
        publish_time = self.cbo_time.currentData()
        search_url = (f"https://www.douyin.com/search/{urllib.parse.quote(keyword)}"
                      f"?type=video&sort_type={sort_type}&publish_time={publish_time}")
        self.url_input.setText(search_url)
        self._scan()

    def _add_video_card(self, v):
        self._scanned.append(v)
        exists = self._check_exists(v)
        card = VideoCard(v, already_exists=exists)
        card.check_changed.connect(self._update_sel_count)
        
        item = QListWidgetItem(self.v_list)
        item.setSizeHint(QSize(0, 112)) 
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
        
        self._dl_thread = DouyinDownloadThread(vids, self.dir_input.text().strip(), get_cookie_file("douyin"), self.spin_threads.value())
        self._dl_thread.log.connect(self._write_hidden_log) # Bắt log kỹ thuật ẩn
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
