import os, sys, re, json, threading, subprocess, concurrent.futures, time, random, shutil
import logging
import requests, hashlib, urllib.parse
from datetime import datetime
from functools import reduce
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QListWidget, QListWidgetItem, QTextEdit, QFileDialog, QProgressBar, QFrame, QSplitter, QAbstractItemView, QSizePolicy, QComboBox, QCheckBox)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QSettings, QSize
from PyQt6.QtGui import QTextCursor, QPixmap, QColor, QIcon
from shared_utils import AsyncImageLoader, CREATE_NO_WINDOW
from cookie_tab import get_cookie_file
import yt_dlp

def _sanitize(name): 
    return re.sub(r'[<>:"/\\|?*\n\r\t]', '_', name).strip()[:60]

# ============================================================
# DÒ TÌM FFMPEG — tránh lỗi "merging of multiple formats but
# ffmpeg is not installed" khi ghép video+audio
# ============================================================
_FFMPEG_LOCATION_CACHE = None

def find_ffmpeg():
    global _FFMPEG_LOCATION_CACHE
    if _FFMPEG_LOCATION_CACHE is not None:
        return _FFMPEG_LOCATION_CACHE or None

    exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    candidates = [
        shutil.which('ffmpeg'),
        os.path.join(exe_dir, 'ffmpeg.exe'),
        os.path.join(exe_dir, 'ffmpeg', 'ffmpeg.exe'),
        os.path.join(exe_dir, 'bin', 'ffmpeg.exe'),
        os.path.join(exe_dir, 'ffmpeg'),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            _FFMPEG_LOCATION_CACHE = c
            return c
    _FFMPEG_LOCATION_CACHE = ""
    return None

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
# THUẬT TOÁN WBI SIGNATURE CỦA BILIBILI (Bypass Anti-Bot)
# ============================================================
mixinKeyEncTab = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52
]

def getMixinKey(orig: str):
    return reduce(lambda s, i: s + orig[i], mixinKeyEncTab, '')[:32]

def encWbi(params: dict, img_key: str, sub_key: str):
    mixin_key = getMixinKey(img_key + sub_key)
    curr_time = round(time.time())
    params['wts'] = curr_time
    params = dict(sorted(params.items()))
    query = urllib.parse.urlencode(params)
    query = query.replace("!", "").replace("'", "").replace("(", "").replace(")", "").replace("*", "")
    wbi_sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return query + '&w_rid=' + wbi_sign

DEFAULT_BILI_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36',
    'Referer': 'https://www.bilibili.com/',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Origin': 'https://www.bilibili.com',
}

def make_bili_session(cookies_dict):
    """Tạo 1 Session dùng chung xuyên suốt phiên quét, giữ lại cookie thiết bị
    (buvid3, b_nut...) mà Bilibili trả về — tránh bị Risk Control từ trang 2 trở đi."""
    session = requests.Session()
    session.headers.update(DEFAULT_BILI_HEADERS)
    if cookies_dict:
        session.cookies.update(cookies_dict)
    try:
        # "Khởi động": ghé trang chủ 1 lần để Bilibili cấp cookie thiết bị
        # giống một trình duyệt thật, thay vì gọi thẳng API ngay lập tức.
        session.get('https://www.bilibili.com/', timeout=10)
    except Exception:
        pass
    return session

def getWbiKeys(session):
    resp = session.get('https://api.bilibili.com/x/web-interface/nav', timeout=10)
    resp.raise_for_status()
    json_data = resp.json()
    img_url = json_data['data']['wbi_img']['img_url']
    sub_url = json_data['data']['wbi_img']['sub_url']
    img_key = img_url.rsplit('/', 1)[1].split('.')[0]
    sub_key = sub_url.rsplit('/', 1)[1].split('.')[0]
    return img_key, sub_key

# ============================================================
# BẢNG DỊCH MÃ LỖI BILIBILI (Risk Control / Auth)
# ============================================================
BILI_ERROR_CODES = {
    -101: "Chưa đăng nhập / cookie hết hạn.",
    -352: "Bị chặn bởi Risk Control (nghi ngờ bot) — thử lại chậm hơn hoặc đổi cookie.",
    -401: "Chữ ký WBI không hợp lệ (thuật toán có thể đã bị Bilibili cập nhật).",
    -403: "Không có quyền truy cập (có thể do khu vực hoặc quyền riêng tư).",
    -404: "Không tìm thấy dữ liệu (video/kênh không tồn tại hoặc đã bị xoá).",
    412: "Bị chặn tốc độ truy cập (HTTP 412 - quá nhiều request cùng lúc).",
}

def _describe_bili_error(code):
    return BILI_ERROR_CODES.get(code, f"Mã lỗi không xác định ({code}).")

def _request_with_retry(session, url, log_emit=None, max_retries=3, timeout=10):
    """Gọi API Bilibili qua Session kèm retry + backoff, phân biệt lỗi JSON/HTTP/risk-control."""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code == 412:
                raise Exception(_describe_bili_error(412))
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError:
                raise Exception("Phản hồi không phải JSON (rất có thể bị Bilibili chặn / yêu cầu captcha).")
            code = data.get('code')
            if code is not None and code != 0:
                raise Exception(f"{_describe_bili_error(code)} (chi tiết API: {data.get('message')})")
            return data
        except Exception as e:
            last_err = e
            if log_emit:
                log_emit(f"⚠️ Thử lại lần {attempt}/{max_retries} thất bại: {e}\n")
            if attempt < max_retries:
                time.sleep(1.5 * attempt)  # backoff tăng dần
    raise last_err

def load_cookies_to_dict(filepath):
    cookie_dict = {}
    if not filepath or not os.path.exists(filepath): return cookie_dict
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('#HttpOnly_'): line = line[10:]
                if line.strip() and not line.startswith('#'):
                    parts = line.strip().split('\t')
                    if len(parts) >= 7 and "bilibili.com" in parts[0]:
                        cookie_dict[parts[5]] = parts[6]
    except Exception: pass
    return cookie_dict

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
        
        self.chk = QCheckBox()
        self.chk.setFixedSize(30, 30) 
        self.chk.setStyleSheet("""
            QCheckBox::indicator { width: 18px; height: 18px; border: 1px solid #444; border-radius: 4px; background: transparent; margin: 2px; }
            QCheckBox::indicator:checked { background: #00A1D6; border-color: #00A1D6; image: none; }
            QCheckBox::indicator:hover { border-color: #00A1D6; }
        """)
        self.chk.stateChanged.connect(lambda: self.check_changed.emit())
        self.card_layout.addWidget(self.chk)
        
        self.thumb_lbl = QLabel()
        self.thumb_lbl.setFixedSize(112, 63)
        self.thumb_lbl.setStyleSheet("background: #111; border-radius: 4px; border: 1px solid #222;")
        self.thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.card_layout.addWidget(self.thumb_lbl)
        
        if vid_data.get("cover_url"):
            self.loader = AsyncImageLoader(vid_data["cover_url"], vid_data["id"])
            self.loader.image_loaded.connect(self._set_image)
            self.loader.start()
            
        info_lay = QVBoxLayout()
        info_lay.setSpacing(4)
        
        self.title_lbl = QLabel(vid_data.get('desc') or "Không có tiêu đề")
        self.title_lbl.setWordWrap(True)
        self.title_lbl.setStyleSheet("color: #e0e0e0; font-size: 13px; font-weight: 500;")
        self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        info_lay.addWidget(self.title_lbl)
        
        author = vid_data.get('author', 'BilibiliUser')
        status_text = f"👤 {author}   |   🆔 {vid_data.get('id')}"
        self.id_lbl = QLabel(status_text)
        self.id_lbl.setStyleSheet("color: #666666; font-size: 11px;")
        info_lay.addWidget(self.id_lbl)
        
        self.pbar = QProgressBar()
        self.pbar.setFixedHeight(4)
        self.pbar.setTextVisible(False)
        self.pbar.setStyleSheet("QProgressBar { background: transparent; border: none; } QProgressBar::chunk { background: #00A1D6; border-radius: 2px; }")
        self.pbar.hide()
        info_lay.addWidget(self.pbar)
        
        info_lay.addStretch()
        self.card_layout.addLayout(info_lay)
        self.card_layout.setStretch(2, 1)
        
        if already_exists: self._apply_downloaded_style()
        else: self.main_frame.setStyleSheet("QFrame#cardFrame { background-color: transparent; border: none; }")

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
        self.main_frame.setStyleSheet("QFrame#cardFrame { background-color: rgba(76, 175, 80, 0.12); border: 1px solid #4caf50; border-radius: 6px; }")
        self.title_lbl.setStyleSheet("color: #a5d6a7; font-size: 13px; font-weight: bold;")
        
    def set_downloaded_state_realtime(self):
        if not self.already_exists:
            self.already_exists = True
            self._apply_downloaded_style()
            self.chk.setChecked(False)

# ============================================================
# SCAN THREAD — 100% NATIVE API (SIÊU TỐC ĐỘ)
# ============================================================
class BilibiliScanThread(QThread):
    log = pyqtSignal(str)
    user_log = pyqtSignal(str)
    video_found = pyqtSignal(dict)
    finished_signal = pyqtSignal(int)
    
    def __init__(self, url, cookie_file, scan_limit=0):
        super().__init__()
        self.url = url
        self.cookie_file = cookie_file
        self.scan_limit = scan_limit  # 0 = không giới hạn
        self._cancel = False
        
    def cancel(self):
        self._cancel = True
        
    def run(self):
        url = self.url if self.url.startswith("http") else "https://" + self.url
        self.log.emit(f"🚀 BẮT ĐẦU QUÉT BẰNG NATIVE API: {url}\n")
        
        cookie_dict = load_cookies_to_dict(self.cookie_file)
        session = make_bili_session(cookie_dict)
        total_found = 0
        expected_total = None

        try:
            if "space.bilibili.com" in url:
                m = re.search(r'space\.bilibili\.com/(\d+)', url)
                if not m:
                    raise Exception("Không tìm thấy ID Kênh (mid) trong link.")
                mid = m.group(1)
                
                self.user_log.emit(f"⚡ Đang bẻ khóa thuật toán WBI của kênh: {mid}...\n")
                img_key, sub_key = getWbiKeys(session)
                self.log.emit(f"🔓 Bẻ khóa WBI thành công. Bắt đầu tải danh sách trang...\n")
                
                pn = 1
                page_fail_count = 0
                MAX_PAGE_RETRIES = 5
                while not self._cancel:
                    params = {'mid': mid, 'ps': 30, 'pn': pn}
                    query = encWbi(params, img_key, sub_key)
                    api_url = f"https://api.bilibili.com/x/space/wbi/arc/search?{query}"

                    try:
                        data = _request_with_retry(session, api_url, log_emit=self.log.emit)
                        page_fail_count = 0
                    except Exception as e:
                        page_fail_count += 1
                        self.log.emit(f"⚠️ Trang {pn} lỗi (lần {page_fail_count}/{MAX_PAGE_RETRIES}): {e}\n")
                        if page_fail_count >= MAX_PAGE_RETRIES:
                            # Chỉ bỏ qua trang này sau khi đã thử đủ số lần - và CẢNH BÁO RÕ
                            # để người dùng biết danh sách có thể bị thiếu video, không âm thầm bỏ qua.
                            self.log.emit(f"❌ Trang {pn} thất bại sau {MAX_PAGE_RETRIES} lần thử — BỎ QUA trang này.\n")
                            self.user_log.emit(f"🟠 CẢNH BÁO: Trang {pn} lỗi liên tục, đã bỏ qua — danh sách có thể THIẾU video!\n")
                            page_fail_count = 0
                            pn += 1
                            time.sleep(3 + random.uniform(1, 2))
                            continue
                        else:
                            # Thử lại CHÍNH trang vừa lỗi (không tăng pn), chờ lâu hơn
                            # để Risk Control của Bilibili có thời gian "nguội".
                            wait_s = 8 * page_fail_count + random.uniform(2, 5)
                            self.user_log.emit(f"⏳ Trang {pn} bị chặn, đang chờ {int(wait_s)}s rồi thử lại...\n")
                            time.sleep(wait_s)
                            continue

                    vlist = data.get('data', {}).get('list', {}).get('vlist', [])
                    if expected_total is None:
                        expected_total = data.get('data', {}).get('page', {}).get('count')
                    if not vlist: break
                    
                    reached_limit = False
                    for v in vlist:
                        if self._cancel: break
                        if self.scan_limit > 0 and total_found >= self.scan_limit:
                            reached_limit = True
                            break
                        vid_id = v.get('bvid')
                        item = {
                            "id": vid_id,
                            "url": f"https://www.bilibili.com/video/{vid_id}",
                            "platform": "bilibili",
                            "desc": v.get('title', 'Không có tiêu đề'),
                            "author": v.get('author', 'BilibiliUser'),
                            "cover_url": v.get('pic', '')
                        }
                        self.video_found.emit(item)
                        total_found += 1
                        
                    self.log.emit(f"✅ Quét xong trang {pn} ({len(vlist)} video)...\n")
                    self.user_log.emit(f"📦 Đã tìm thấy {total_found} video...\n")
                    if reached_limit:
                        self.user_log.emit(f"🔒 Đã đạt giới hạn quét {self.scan_limit} video — dừng lại.\n")
                        break
                    pn += 1
                    page_fail_count = 0
                    time.sleep(1.5 + random.uniform(0.5, 1.5))  # trễ ngẫu nhiên, tránh giống bot

            else:
                m = re.search(r'(BV[a-zA-Z0-9]+)', url)
                if not m:
                    raise Exception("Không tìm thấy mã bvid trong link.")
                bvid = m.group(1)
                
                self.user_log.emit(f"🔍 Phân tích API video lẻ: {bvid}...\n")
                api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"

                data = _request_with_retry(session, api_url, log_emit=self.log.emit)
                v = data.get('data', {})

                # Số tập/phần (multi-P) nếu có — để cảnh báo người dùng biết trước
                pages = v.get('pages', [])
                part_count = len(pages) if isinstance(pages, list) else 1
                if part_count > 1:
                    self.user_log.emit(f"📑 Video có {part_count} tập/phần — sẽ tải đầy đủ khi bấm tải.\n")

                item = {
                    "id": bvid,
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "platform": "bilibili",
                    "desc": v.get('title', 'Không có tiêu đề'),
                    "author": v.get('owner', {}).get('name', 'BilibiliUser'),
                    "cover_url": v.get('pic', ''),
                    "part_count": part_count,
                }
                self.video_found.emit(item)
                total_found += 1

        except Exception as e:
            self.log.emit(f"❌ Lỗi nghiêm trọng khi quét API: {e}\n")
            self.user_log.emit("❌ Quét thất bại. Xem Log kỹ thuật để biết chi tiết.\n")

        self.log.emit(f"🏁 TỔNG KẾT API: {total_found} video.\n")
        if expected_total is not None and total_found < expected_total:
            missing = expected_total - total_found
            self.log.emit(f"🟠 THIẾU {missing} video so với tổng thực tế ({expected_total}) theo Bilibili.\n")
            self.user_log.emit(f"🟠 Hoàn tất — Phát hiện {total_found}/{expected_total} video (THIẾU {missing}, do bị chặn giữa chừng — thử quét lại)\n")
        else:
            self.user_log.emit(f"🏁 Hoàn tất — Phát hiện {total_found} video\n")
        self.finished_signal.emit(total_found)

# ============================================================
# DOWNLOAD THREAD
# ============================================================
class BilibiliDownloadThread(QThread):
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
        self.log.emit(f"\n📥 BẮT ĐẦU TẢI {total} VIDEO BILIBILI...\n")
        self.user_log.emit(f"📥 Bắt đầu tải {total} video...\n")
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.thread_count) as ex:
            futs = {ex.submit(self._dl_worker, v, i, total): v for i, v in enumerate(self.videos, 1)}
            concurrent.futures.wait(futs)
        self.log.emit(f"🎉 HOÀN TẤT TẢI: {self.success_count}/{total} video.\n")
        self.user_log.emit(f"🎉 Hoàn tất: {self.success_count}/{total} tải thành công\n")

    def _dl_worker(self, vid, idx, tot):
        self.pause_event.wait()
        if self._cancel:
            return False

        vid_id = str(vid.get("id", ""))
        desc = _sanitize(vid.get("desc", "") or "")
        author = _sanitize(vid.get("author", "BilibiliUser") or "BilibiliUser")
        user_dir = os.path.join(self.save_dir, "BilibiliDownload", author)
        os.makedirs(user_dir, exist_ok=True)

        # Lưu thẳng file video vào thư mục tên kênh (KHÔNG tạo thư mục con riêng cho từng video).
        # Vẫn giữ %(playlist_index)s trong tên file để video nhiều tập/phần không bị ghi đè lẫn nhau.
        video_base_name = f"Bili_{vid_id} - {desc}" if desc else f"Bili_{vid_id}"
        filepath = os.path.join(user_dir, f"{video_base_name} - %(playlist_index|01)02d.%(ext)s")
        
        self.log.emit(f"[{idx}/{tot}] ⬇️ Bắt đầu tải: {vid_id}\n")
        success = False
        self.card_progress.emit(vid_id, -1)

        def progress_hook(d):
            if self._cancel: raise Exception("Đã hủy tải")
            if d['status'] == 'downloading':
                t_bytes = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                d_bytes = d.get('downloaded_bytes', 0)
                if t_bytes > 0:
                    pct = int((d_bytes / t_bytes) * 100)
                    self.card_progress.emit(vid_id, pct)
            elif d['status'] == 'finished':
                self.card_progress.emit(vid_id, 100)

        ffmpeg_path = find_ffmpeg()
        if ffmpeg_path:
            fmt = 'bestvideo+bestaudio/best'
        else:
            # Không có ffmpeg -> không thể ghép video+audio riêng biệt.
            # Chuyển sang tải 1 luồng đã có sẵn cả hình+tiếng (chất lượng có thể thấp hơn)
            # để vẫn tải THÀNH CÔNG thay vì lỗi hoàn toàn.
            fmt = 'best'
            self.log.emit(f"[{idx}/{tot}] ⚠️ Không tìm thấy ffmpeg trên máy -> tải luồng đơn (best), có thể chất lượng thấp hơn.\n")

        ydl_opts = {
            'format': fmt,
            'merge_output_format': 'mp4',
            'outtmpl': filepath,
            'progress_hooks': [progress_hook],
            'cookiefile': self.cookie_file if self.cookie_file and os.path.exists(self.cookie_file) else None,
            'writethumbnail': False, 
            'quiet': True,
            'no_warnings': True,
            'http_headers': {
                'Referer': 'https://www.bilibili.com/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36'
            }
        }
        if ffmpeg_path:
            ydl_opts['ffmpeg_location'] = ffmpeg_path

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            if self._cancel: break
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([vid["url"]])
                success = True
                break 
            except Exception as e:
                err_msg = str(e).replace('\n', ' ').strip()
                if attempt < max_retries:
                    self.log.emit(f"⚠️ [{idx}] Lỗi tải (thử lại lần {attempt}/{max_retries}) | {err_msg[:100]}...\n")
                    time.sleep(2) 
                else:
                    self.log.emit(f"❌ [{idx}] Thất bại hoàn toàn. Lỗi: {err_msg}\n")

        with self.lock:
            self.done_count += 1
            if success:
                self.success_count += 1
                self.log.emit(f"✅ [XONG] {vid_id}\n")
                self.user_log.emit(f"✅ Tải xong ({self.success_count}/{tot}): {vid_id}\n")
            else:
                self.log.emit(f"❌ [BỎ QUA] {vid_id} do lỗi.\n")
                self.user_log.emit(f"❌ Lỗi tải: {vid_id}\n")
            self.total_progress.emit(self.done_count, tot)
            
        return success

# ============================================================
# BILIBILI WIDGET — Giao diện đồng bộ
# ============================================================
class BilibiliWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scanned = []
        self.settings = QSettings("AnhStudio", "Bilibili")
        
        # ==============================================================
        # 1. CẤU HÌNH NHẬN DIỆN THƯƠNG HIỆU
        # ==============================================================
        THEME_COLOR = "#00A1D6"  
        HOVER_COLOR = "#008db8"
        PLACEHOLDER_TEXT = "🔗 Dán link kênh (space) hoặc link video Bilibili lẻ vào đây..."
        PLATFORM_NAME = "Bilibili"
        
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
        
        self.cmb_scan_limit = QComboBox()
        self.cmb_scan_limit.addItems(["100 tập", "200 tập", "300 tập", "500 tập", "Tất cả"])
        self.cmb_scan_limit.setCurrentText(self.settings.value("scan_limit", "Tất cả"))
        self.cmb_scan_limit.setFixedSize(95, 45)
        self.cmb_scan_limit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cmb_scan_limit.setStyleSheet(f"""
            QComboBox {{ 
                background-color: #0f0f0f; border: 1px solid #2a2a2a; border-radius: 8px; 
                padding: 0 10px; color: #ccc; font-size: 12px; font-weight: bold;
            }}
            QComboBox:hover {{ border-color: {THEME_COLOR}; }}
            QComboBox::drop-down {{ border: none; width: 20px; }}
            QComboBox::down-arrow {{ image: none; border-left: 4px solid transparent; border-right: 4px solid transparent; border-top: 5px solid #888; }}
            QComboBox QAbstractItemView {{ background: #1a1a1a; border: 1px solid #333; color: #ccc; selection-background-color: {THEME_COLOR}; selection-color: #fff; padding: 4px; }}
        """)
        self.cmb_scan_limit.currentTextChanged.connect(lambda t: self.settings.setValue("scan_limit", t))
        search_box.addWidget(self.cmb_scan_limit)
        
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
        self.lbl_queue_count.setStyleSheet(f"color: {THEME_COLOR}; border: 1px solid {THEME_COLOR}; background: rgba(0, 161, 214, 0.1); padding: 3px 15px; border-radius: 6px; font-weight: bold; font-size: 12px;")
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
        author = _sanitize(vid_data.get("author", "BilibiliUser"))
        user_dir = os.path.join(save_dir, "BilibiliDownload", author)
        if not os.path.isdir(user_dir): return False
        prefix = f"Bili_{vid_id}"
        video_exts = (".mp4", ".mkv", ".flv")
        for name in os.listdir(user_dir):
            if not name.startswith(prefix):
                continue
            full = os.path.join(user_dir, name)
            if os.path.isdir(full):
                # Thư mục riêng của video (có thể chứa nhiều tập bên trong)
                for f in os.listdir(full):
                    if f.endswith(video_exts):
                        return True
            elif name.endswith(video_exts):
                # Tương thích ngược với file cũ tải theo cấu trúc trước đây
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
        self.status_banner.setText("⏳ Đang gọi API Bilibili tốc độ cao, vui lòng đợi...")
        self.status_banner.setStyleSheet("background-color: rgba(0, 161, 214, 0.1); border: 1px solid #00A1D6; border-radius: 8px; padding: 15px; font-size: 14px; font-weight: bold; color: #00A1D6;")
        
        limit_text = self.cmb_scan_limit.currentText()
        scan_limit = 0 if limit_text == "Tất cả" else int(limit_text.split()[0])
        self._scan_thread = BilibiliScanThread(url, get_cookie_file("bilibili"), scan_limit)
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
        
        self._dl_thread = BilibiliDownloadThread(vids, self.dir_input.text().strip(), get_cookie_file("bilibili"), self.spin_threads.value())
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
