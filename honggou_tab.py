import base64 as _base64
import sys
import time
import json
import requests
import os
import re
import tempfile
import subprocess
import uuid
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, 
    QTableWidget, QTableWidgetItem, QLabel, QMessageBox, 
    QHeaderView, QListWidget, QListWidgetItem, QApplication, QMainWindow, QStackedWidget,
    QFileDialog, QProgressDialog, QProgressBar, QComboBox
)
from PyQt6.QtCore import Qt, QThread, QObject, pyqtSignal, QSize, QSettings, QTimer
from PyQt6.QtGui import QIcon, QPixmap, QImage, QFont, QColor

# ==========================================
# CẤU HÌNH SERVER & PHIÊN BẢN
# ==========================================
APP_VERSION = "1.0.24"
SERVER_URL = "http://163.61.182.119:8000"
MAX_CONCURRENT_DOWNLOADS = 3  

# ==========================================
# THREAD 1: TẢI DANH SÁCH PHIM HOT NGẦM
# ==========================================
class HotMoviesLoadThread(QThread):
    item_loaded_signal = pyqtSignal(dict)
    finished_signal = pyqtSignal()

    def __init__(self, genre=None):
        super().__init__()
        self.genre = genre

    def run(self):
        try:
            url = f"{SERVER_URL}/api/client/hot_movies"
            params = {}
            if self.genre: params["genre"] = self.genre

            res = requests.get(url, params=params, timeout=10)
            if res.status_code == 200:
                movies = res.json()
                seen_titles = set()
                for m in movies:
                    title = m.get("title", "")
                    if isinstance(title, str) and "\\u" in title:
                        try:
                            title = title.encode('utf-8').decode('unicode_escape')
                            m["title"] = title
                        except: pass

                    key = (m.get("title") or "").strip()
                    if key and key in seen_titles: continue
                    if key: seen_titles.add(key)

                    cover_b64 = m.get("cover_base64")
                    if cover_b64:
                        try: m["img_data"] = _base64.b64decode(cover_b64)
                        except: pass

                    self.item_loaded_signal.emit(m)
            self.finished_signal.emit()
        except Exception:
            self.finished_signal.emit()

# ==========================================
# THREAD 1B: TẢI BỔ SUNG ẢNH BÌA CHO LỊCH SỬ (chạy nền, không đứng UI)
# ==========================================
class HistoryCoverThread(QThread):
    cover_ready = pyqtSignal(int, bytes)

    def __init__(self, jobs, covers_dir):
        super().__init__()
        self.jobs = jobs  # list các (row, series_id, cover_url) còn thiếu ảnh
        self.covers_dir = covers_dir

    def run(self):
        for row, sid, url in self.jobs:
            if not url: continue
            if url.startswith('//'): url = 'https:' + url
            try:
                r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://hongguoduanju.com/"})
                if r.status_code == 200 and r.content:
                    try:
                        with open(os.path.join(self.covers_dir, f"{sid}.img"), 'wb') as f: f.write(r.content)
                    except Exception: pass
                    self.cover_ready.emit(row, r.content)
            except Exception: pass

# ==========================================
# THREAD 2: TÌM KIẾM PHIM THEO TÊN
# ==========================================
class SearchMoviesThread(QThread):
    results_signal = pyqtSignal(list)
    error_signal = pyqtSignal(str)

    def __init__(self, keyword, auth_token):
        super().__init__()
        self.keyword = keyword
        self.auth_token = auth_token

    def run(self):
        try:
            url = f"{SERVER_URL}/api/client/search"
            params = {"keyword": self.keyword}
            headers = {"Authorization": f"Bearer {self.auth_token}"}
            res = requests.get(url, params=params, headers=headers, timeout=15)
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "success": self.results_signal.emit(data.get("data", []))
                else: self.error_signal.emit(data.get("message", "Lỗi tìm kiếm"))
            else: self.error_signal.emit(f"Máy chủ trả về mã lỗi: {res.status_code}")
        except Exception as e: self.error_signal.emit(str(e))

# ==========================================
# THREAD 3: MÁY KHÁCH TỰ QUÉT LINK
# ==========================================
class HonggouScanThread(QThread):
    scan_result = pyqtSignal(dict)
    error_signal = pyqtSignal(str)
    url_resolved_signal = pyqtSignal(str) 

    def __init__(self, url, auth_token=""):
        super().__init__()
        self.url = url
        self.auth_token = auth_token  

    def _resolve_to_detail_url(self, url):
        if "hongguoduanju.com/detail" in url or "hongguoduanju.com/player" in url: return url
        if re.search(r'novelquickapp\.com/s/', url):
            try:
                headers = {"User-Agent": "Mozilla/5.0"}
                resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
                url = resp.url
            except: pass 

        decoded = url
        for _ in range(4):
            new_decoded = unquote(decoded)
            if new_decoded == decoded: break
            decoded = new_decoded

        match = re.search(r'"video_series_id"\s*:\s*"(\d+)"', decoded)
        if match: return f"https://hongguoduanju.com/detail?series_id={match.group(1)}"

        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            zlink = params.get("zlink", [None])[0]
            if zlink:
                zlink_decoded = unquote(zlink)
                zlink_parsed = urlparse(zlink_decoded)
                zlink_params = parse_qs(zlink_parsed.query)
                scheme_raw = zlink_params.get("schemeParams", [None])[0]
                if scheme_raw:
                    scheme_json = json.loads(unquote(scheme_raw))
                    vid = str(scheme_json.get("video_series_id", ""))
                    if vid: return f"https://hongguoduanju.com/detail?series_id={vid}"
        except: pass

        match = re.search(r'video_series_id[=%22":]+(\d{15,25})', decoded)
        if match: return f"https://hongguoduanju.com/detail?series_id={match.group(1)}"
        return url  

    def run(self):
        try:
            self.url = self._resolve_to_detail_url(self.url)
            self.url_resolved_signal.emit(self.url) 

            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(self.url, headers=headers, timeout=30)
            resp.raise_for_status()
            html = resp.text

            detail = None
            json_match = re.search(r'window\._ROUTER_DATA\s*=\s*(\{.+?\})\s*;?\s*</script>', html, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(1))
                    detail = data.get("loaderData", {}).get("detail_page", {}).get("seriesDetail", None)
                except json.JSONDecodeError: pass

            if not detail:
                self.error_signal.emit("Không bóc tách được dữ liệu. Vui lòng kiểm tra lại link.")
                return

            series_id = str(detail.get("series_id", ""))
            title = detail.get("series_name", "")
            cover_url = detail.get("series_cover", "")

            total_episodes = 0
            right_text = detail.get("episode_right_text", "")
            if right_text:
                num_match = re.search(r'(\d+)', right_text)
                if num_match: total_episodes = int(num_match.group(1))
            if total_episodes == 0:
                vid_list = detail.get("vid_list", [])
                if isinstance(vid_list, list) and len(vid_list) > 0: total_episodes = len(vid_list)
            if total_episodes == 0:
                total_episodes = int(detail.get("episode_cnt", 0))

            payload = {
                "url": self.url, "series_id": series_id, "expected_total": total_episodes,
                "title": title, "cover_url": cover_url
            }
            res = requests.post(f"{SERVER_URL}/api/client/add_job", json=payload, headers={"Authorization": f"Bearer {self.auth_token}"}, timeout=10)
            
            if res.status_code == 200:
                data = res.json()
                data["title"] = title
                data["cover_url"] = cover_url
                data["total_episodes"] = total_episodes
                self.scan_result.emit(data)
            else: self.error_signal.emit("Lỗi kết nối đến máy chủ điều phối!")

        except requests.exceptions.RequestException as e: self.error_signal.emit(f"Lỗi kết nối web: {str(e)}")
        except Exception as e: self.error_signal.emit(f"Lỗi quét link: {str(e)}")

# ==========================================
# THREAD 4: THEO DÕI TRẠNG THÁI REAL-TIME
# ==========================================
class JobStatusMonitorThread(QThread):
    update_signal = pyqtSignal(dict)

    def __init__(self, job_id, auth_token=""):
        super().__init__()
        self.job_id = job_id
        self.auth_token = auth_token  
        self.running = True

    def run(self):
        while self.running:
            try:
                res = requests.get(f"{SERVER_URL}/api/client/job_status/{self.job_id}", headers={"Authorization": f"Bearer {self.auth_token}"}, timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    self.update_signal.emit(data)
                    if data.get("status") in ["completed", "error"]:
                        self.running = False
                        break
            except: pass
            time.sleep(3)

    def stop(self): self.running = False

# ==========================================
# THREAD 5: LUỒNG TẢI 1 FILE MP4 TỪ GOOGLE DRIVE
# ==========================================
class SingleDriveDownloadThread(QThread):
    progress_signal = pyqtSignal(int, int, float)  
    done_signal = pyqtSignal(int, str)              
    error_signal = pyqtSignal(int, str)             

    def __init__(self, ep_data, save_folder):
        super().__init__()
        self.ep_data = ep_data
        self.save_folder = save_folder

    def _extract_file_id(self, drive_link):
        match_web = re.search(r'/d/([a-zA-Z0-9_-]+)', drive_link)
        if match_web: return match_web.group(1)
        match_dl = re.search(r'id=([a-zA-Z0-9_-]+)', drive_link)
        if match_dl: return match_dl.group(1)
        return None

    def _download_direct(self, ep_num, url, save_path):
        """Tải thẳng qua HTTP (dùng cho presigned URL từ R2). Thử tối đa 3 lần."""
        for attempt in range(3):
            try:
                with requests.get(url, stream=True, timeout=30) as resp:
                    if resp.status_code == 403:
                        self.error_signal.emit(ep_num, "Link tải đã hết hạn - hãy quét lại phim")
                        return
                    resp.raise_for_status()
                    total_size = int(resp.headers.get('content-length', 0))
                    downloaded = 0
                    start_time = time.time()
                    with open(save_path, 'wb') as f:
                        for chunk in resp.iter_content(chunk_size=1024 * 512):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                elapsed = time.time() - start_time
                                speed = (downloaded / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                                percent = int(downloaded * 100 / total_size) if total_size > 0 else 0
                                self.progress_signal.emit(ep_num, percent, speed)
                self.done_signal.emit(ep_num, save_path)
                return
            except Exception as e:
                if attempt < 2: time.sleep(2); continue
                self.error_signal.emit(ep_num, f"Lỗi: {str(e)}")

    def _is_quota_page(self, text):
        # Các câu Google thực sự dùng trong trang báo hết quota (Anh + Việt)
        markers = [
            "Too many users have viewed or downloaded",
            "quá nhiều người dùng đã xem hoặc tải",
            "downloadQuotaExceeded",
            "exceeded the download quota",
        ]
        return any(m in text for m in markers)

    def run(self):
        ep_num = self.ep_data["episode_number"]
        raw_file_name = self.ep_data.get("file_name", f"Tap_{ep_num}.mp4")
        safe_file_name = re.sub(r'[\\/*?:"<>|]', "", raw_file_name)
        save_path = os.path.join(self.save_folder, safe_file_name)

        drive_link = self.ep_data.get("drive_link", "")

        # LINK R2/HTTP TRỰC TIẾP (presigned URL từ server): tải thẳng, không cần confirm gì
        if drive_link.startswith("http") and "drive.google.com" not in drive_link:
            self._download_direct(ep_num, drive_link, save_path)
            return

        file_id = self._extract_file_id(drive_link)
        
        if not file_id:
            self.error_signal.emit(ep_num, "Link rỗng hoặc sai định dạng")
            return

        for attempt in range(3):
            try:
                URL = "https://drive.google.com/uc?export=download"
                session = requests.Session()

                # Ngụy trang thành trình duyệt Chrome thật để không bị Google block bot
                session.headers.update({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                })

                # 1. Gửi request đầu tiên
                resp = session.get(URL, params={'id': file_id}, stream=True, timeout=30)

                # 2. Nếu Google trả về trang HTML (trang xác nhận hoặc trang lỗi)
                if 'text/html' in resp.headers.get('Content-Type', ''):
                    text = resp.text

                    # Chỉ báo quota khi có đúng dấu hiệu quota thật của Google
                    # (tránh bắt nhầm chữ "giới hạn"/"quota" chung chung trong trang xác nhận)
                    if self._is_quota_page(text):
                        self.error_signal.emit(ep_num, "Lỗi: Quá giới hạn lượt tải 24h của Drive")
                        return

                    # FLOW MỚI CỦA DRIVE: trang xác nhận file lớn là 1 <form>
                    # trỏ tới drive.usercontent.google.com/download kèm các input ẩn
                    m_action = re.search(r'<form[^>]+action="([^"]+)"', text)
                    hidden_inputs = dict(re.findall(
                        r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', text))
                    if m_action and hidden_inputs.get("id"):
                        form_url = m_action.group(1).replace("&amp;", "&")
                        resp = session.get(form_url, params=hidden_inputs, stream=True, timeout=30)
                    else:
                        # FLOW CŨ: token trong cookie hoặc confirm= trong HTML
                        token = None
                        for key, value in resp.cookies.items():
                            if key.startswith('download_warning'):
                                token = value
                                break
                        if not token:
                            match = re.search(r'confirm=([0-9A-Za-z_-]+)', text)
                            token = match.group(1) if match else 't'
                        resp = session.get(URL, params={'id': file_id, 'confirm': token}, stream=True, timeout=30)

                # 3. CHỐT CHẶN: Ngăn lưu HTML thành file MP4
                final_content_type = resp.headers.get('Content-Type', '')
                if 'text/html' in final_content_type:
                    text = resp.text
                    # Lưu trang lỗi ra file để chẩn đoán khi khách báo lỗi
                    try:
                        debug_path = save_path + ".error.html"
                        with open(debug_path, 'w', encoding='utf-8') as df:
                            df.write(text)
                    except Exception:
                        pass
                    if self._is_quota_page(text):
                        self.error_signal.emit(ep_num, "Lỗi: Quá giới hạn lượt tải 24h của Drive")
                    else:
                        self.error_signal.emit(ep_num, "Bị Google Drive chặn (xem file .error.html trong thư mục tải)")
                    return

                resp.raise_for_status()
                total_size = int(resp.headers.get('content-length', 0))
                downloaded = 0
                start_time = time.time()

                with open(save_path, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 512):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            elapsed = time.time() - start_time
                            speed = (downloaded / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                            percent = int(downloaded * 100 / total_size) if total_size > 0 else 0
                            self.progress_signal.emit(ep_num, percent, speed)

                self.done_signal.emit(ep_num, save_path)
                return 
            except Exception as e:
                if attempt < 2: time.sleep(2); continue
                self.error_signal.emit(ep_num, f"Lỗi: {str(e)}")

# ==========================================
# TRÌNH QUẢN LÝ TẢI SONG SONG
# ==========================================
class DriveDownloadManager(QObject):
    progress_signal = pyqtSignal(int, int, float)  
    done_signal = pyqtSignal(int, str)              
    error_signal = pyqtSignal(int, str)             
    all_done_signal = pyqtSignal(int)               

    def __init__(self, episodes, save_folder, parent=None, max_concurrent=None):
        super().__init__(parent)
        self._pending = list(episodes)
        self.save_folder = save_folder
        self._workers = []         
        self._success_count = 0
        self._finished_count = 0
        self._total = len(episodes)
        self.max_concurrent = max_concurrent or MAX_CONCURRENT_DOWNLOADS

    def start(self):
        for _ in range(min(self.max_concurrent, len(self._pending))):
            self._launch_next()

    def _launch_next(self):
        if not self._pending: return
        ep_data = self._pending.pop(0)
        worker = SingleDriveDownloadThread(ep_data, self.save_folder)
        worker.progress_signal.connect(self.progress_signal.emit)
        worker.done_signal.connect(self._on_worker_done)
        worker.error_signal.connect(self._on_worker_error)
        self._workers.append(worker)
        worker.start()

    def _on_worker_done(self, ep_num, file_path):
        self._success_count += 1
        self._finished_count += 1
        self.done_signal.emit(ep_num, file_path)
        self._launch_next()
        if self._finished_count >= self._total: self.all_done_signal.emit(self._success_count)

    def _on_worker_error(self, ep_num, error_msg):
        self._finished_count += 1
        self.error_signal.emit(ep_num, error_msg)
        self._launch_next()
        if self._finished_count >= self._total: self.all_done_signal.emit(self._success_count)

# ==========================================
# GIAO DIỆN CHÍNH: HONGGOU WIDGET
# ==========================================
class HonggouWidget(QWidget):
    def __init__(self, username):
        super().__init__()
        self.username = username
        self.current_job_id = None
        self.current_series_id = "" 
        self.current_title = ""     
        self.current_cover_url = "" 
        self.current_episodes = []
        self.monitor_thread = None
        self.is_first_movie = True 
        
        self.settings = QSettings("HongguoDownloader", "ClientApp")
        self._cached_history_ids = set() 
        self.auth_token = self.settings.value("auth_token", "")
        default_folder = os.path.join(os.path.expanduser("~"), "Desktop", "Hongguo_Downloads")
        self.save_folder = self.settings.value(f"download_folder_{username}", default_folder)
        os.makedirs(self.save_folder, exist_ok=True)
        self._setup_ui()
        self.load_hot_movies_shelf()

    def _get_history_file(self): return os.path.join(os.path.expanduser("~"), f".hongguo_history_{self.username}.json")

    def _load_history(self):
        history_file = self._get_history_file()
        if os.path.exists(history_file):
            try:
                with open(history_file, 'r', encoding='utf-8') as f: return json.load(f)
            except: return []
        return []

    def _keep_thread_alive(self, t):
        """Giữ tham chiếu thread nền tới khi nó tự chạy xong.
        Nếu gán đè biến khi thread cũ còn chạy, Python dọn rác thread đang chạy -> Qt sập cả app."""
        if not hasattr(self, '_bg_threads'): self._bg_threads = []
        self._bg_threads.append(t)
        def _cleanup():
            try: self._bg_threads.remove(t)
            except ValueError: pass
        t.finished.connect(_cleanup)

    def _get_covers_dir(self):
        d = os.path.join(os.path.expanduser("~"), ".hongguo_covers")
        try: os.makedirs(d, exist_ok=True)
        except Exception: pass
        return d

    def _save_to_history(self, series_id, title, cover_url, total_eps=0, cover_bytes=None):
        if not series_id: return
        # Lưu ảnh bìa xuống máy 1 lần -> lịch sử luôn có hình, kể cả offline
        try:
            cover_path = os.path.join(self._get_covers_dir(), f"{series_id}.img")
            if not os.path.exists(cover_path):
                if not cover_bytes and cover_url:
                    try:
                        u = 'https:' + cover_url if cover_url.startswith('//') else cover_url
                        r = requests.get(u, timeout=6, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://hongguoduanju.com/"})
                        if r.status_code == 200: cover_bytes = r.content
                    except Exception: pass
                if cover_bytes:
                    with open(cover_path, 'wb') as f: f.write(cover_bytes)
        except Exception: pass
        history_file = self._get_history_file()
        history = self._load_history()
        old_entry = next((h for h in history if h.get('series_id') == series_id), None)
        history = [h for h in history if h.get('series_id') != series_id]
        if not total_eps and old_entry: total_eps = old_entry.get('total_episodes', 0)
        history.insert(0, {
            "series_id": series_id,
            "title": title if title else "Phim không rõ tên",
            "cover_url": cover_url,
            "total_episodes": total_eps,
            "timestamp": datetime.now().strftime("%d/%m/%Y %H:%M")
        })
        try:
            with open(history_file, 'w', encoding='utf-8') as f: json.dump(history, f, ensure_ascii=False, indent=2)
            self._cached_history_ids.add(str(series_id))
        except: pass
        self._remove_from_scan_history(series_id)
        try: self._render_history_sidebar()
        except Exception: pass

    # ==========================================
    # LỊCH SỬ QUÉT (CHƯA TẢI) - tự xóa sau 30 phút
    # ==========================================
    def _get_scan_history_file(self): return os.path.join(os.path.expanduser("~"), f".hongguo_scan_history_{self.username}.json")

    def _load_scan_history(self):
        f = self._get_scan_history_file()
        items = []
        if os.path.exists(f):
            try:
                with open(f, 'r', encoding='utf-8') as fh: items = json.load(fh)
            except Exception: items = []
        now = time.time()
        alive = [it for it in items if now - it.get('ts', 0) < 1800]  # 30 phút
        if len(alive) != len(items):
            try:
                with open(f, 'w', encoding='utf-8') as fh: json.dump(alive, fh, ensure_ascii=False, indent=2)
            except Exception: pass
        return alive

    def _save_to_scan_history(self, series_id, title, cover_url, total_eps=0, cover_bytes=None):
        if not series_id: return
        series_id = str(series_id)
        # Đã nằm trong lịch sử TẢI rồi thì không cần nhắc "chưa tải"
        if series_id in getattr(self, '_cached_history_ids', set()): return
        try:
            cover_path = os.path.join(self._get_covers_dir(), f"{series_id}.img")
            if cover_bytes and not os.path.exists(cover_path):
                with open(cover_path, 'wb') as f: f.write(cover_bytes)
        except Exception: pass
        items = self._load_scan_history()
        items = [it for it in items if str(it.get('series_id')) != series_id]
        items.insert(0, {
            "series_id": series_id,
            "title": title if title else "Phim không rõ tên",
            "cover_url": cover_url,
            "total_episodes": total_eps,
            "ts": time.time(),
            "timestamp": datetime.now().strftime("%H:%M")
        })
        items = items[:20]
        try:
            with open(self._get_scan_history_file(), 'w', encoding='utf-8') as fh: json.dump(items, fh, ensure_ascii=False, indent=2)
        except Exception: pass
        try: self._render_history_sidebar()
        except Exception: pass

    def _remove_from_scan_history(self, series_id):
        try:
            items = self._load_scan_history()
            new_items = [it for it in items if str(it.get('series_id')) != str(series_id)]
            if len(new_items) != len(items):
                with open(self._get_scan_history_file(), 'w', encoding='utf-8') as fh: json.dump(new_items, fh, ensure_ascii=False, indent=2)
        except Exception: pass

    def _setup_ui(self):
        master_layout = QVBoxLayout(self)
        master_layout.setSpacing(15)
        master_layout.setContentsMargins(20, 20, 20, 20)

        top_bar = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Dán Link phim HOẶC Nhập Tên Phim vào đây rồi nhấn Enter...")
        self.url_input.setStyleSheet("QLineEdit { padding: 12px; font-size: 14px; border-radius: 8px; border: 1px solid #374151; background: #1f2937; color: #f8fafc; } QLineEdit:focus { border: 1px solid #3b82f6; background: #1e293b; }")
        self.url_input.returnPressed.connect(self._scan)
        self.btn_scan = QPushButton("🔍 Tìm / Quét Phim")
        self.btn_scan.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_scan.setStyleSheet("QPushButton { padding: 12px 24px; font-size: 14px; background-color: #2563eb; color: white; border-radius: 8px; font-weight: bold; border: none; } QPushButton:hover { background-color: #1d4ed8; } QPushButton:disabled { background-color: #374151; color: #64748b; }")
        self.btn_scan.clicked.connect(self._scan)
        top_bar.addWidget(self.url_input); top_bar.addWidget(self.btn_scan)
        master_layout.addLayout(top_bar)

        folder_bar = QHBoxLayout()
        self.lbl_folder = QLabel(f"📂 Lưu vào: {self.save_folder}")
        self.lbl_folder.setStyleSheet("color: #94a3b8; font-size: 12px; padding: 4px;")
        btn_change_folder = QPushButton("Đổi thư mục")
        btn_change_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_change_folder.setStyleSheet("QPushButton { padding: 6px 14px; font-size: 12px; background-color: transparent; color: #3b82f6; border: 1px solid #374151; border-radius: 6px; } QPushButton:hover { background-color: #1e293b; border: 1px solid #3b82f6; }")
        btn_change_folder.clicked.connect(self._change_folder)
        folder_bar.addWidget(self.lbl_folder); folder_bar.addStretch(); folder_bar.addWidget(btn_change_folder)
        master_layout.addLayout(folder_bar)

        self.content_stack = QStackedWidget()

        # ===== PANEL LỊCH SỬ TẢI - cột dọc cố định bên phải =====
        self.history_panel = QWidget()
        self.history_panel.setFixedWidth(240)
        hp_layout = QVBoxLayout(self.history_panel)
        hp_layout.setContentsMargins(10, 0, 0, 0)
        hp_layout.setSpacing(8)
        lbl_hp = QLabel("🕒 Lịch Sử Tải")
        lbl_hp.setStyleSheet("color: #f59e0b; font-size: 15px; font-weight: bold; padding: 4px 2px;")
        hp_layout.addWidget(lbl_hp)
        self.history_list = QListWidget()
        self.history_list.setIconSize(QSize(52, 70))
        self.history_list.setWordWrap(True)
        self.history_list.setStyleSheet("QListWidget { background-color: #0f172a; border: 1px solid #1e293b; border-radius: 10px; outline: none; padding: 4px; } QListWidget::item { color: #e2e8f0; font-size: 12px; padding: 6px; border-radius: 8px; border-bottom: 1px solid #1e293b; } QListWidget::item:hover { background-color: #1e293b; } QScrollBar:vertical { border: none; background: #111827; width: 6px; margin: 0px; } QScrollBar::handle:vertical { background: #374151; border-radius: 3px; min-height: 20px; }")
        self.history_list.itemClicked.connect(self._on_history_item_clicked)
        hp_layout.addWidget(self.history_list)

        body_layout = QHBoxLayout()
        body_layout.setSpacing(0)
        body_layout.addWidget(self.content_stack, 1)
        body_layout.addWidget(self.history_panel)
        master_layout.addLayout(body_layout)

        self.page_grid = QWidget()
        grid_layout = QVBoxLayout(self.page_grid)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        self.genre_container = QWidget()
        genre_layout = QHBoxLayout(self.genre_container)
        genre_layout.setContentsMargins(0, 5, 0, 10)
        genre_layout.setSpacing(10)
        
        self.genre_buttons = []
        genres = [("🔥 Tất Cả", None), ("👍 BXH Đề Cử", "BXH Đề Cử"), ("📈 BXH Lượt Xem", "BXH Lượt Xem"), ("🆕 BXH Phim Mới", "BXH Phim Mới"), ("🐼 BXH Hoạt Hình", "BXH Hoạt Hình")]
        for name, tag in genres:
            btn = QPushButton(name)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("genre_tag", tag)
            btn.clicked.connect(lambda checked, b=btn: self._on_genre_clicked(b))
            genre_layout.addWidget(btn)
            self.genre_buttons.append(btn)
        genre_layout.addStretch() 
        grid_layout.addWidget(self.genre_container)
        self._update_genre_styles(self.genre_buttons[0])

        self.loading_bar = QProgressBar()
        self.loading_bar.setRange(0, 0); self.loading_bar.setTextVisible(False); self.loading_bar.setFixedHeight(4)
        self.loading_bar.setStyleSheet("QProgressBar { background-color: #1e293b; border: none; border-radius: 2px; } QProgressBar::chunk { background-color: #38bdf8; border-radius: 2px; }")
        self.loading_bar.hide(); grid_layout.addWidget(self.loading_bar)

        self.hot_list = QListWidget()
        self.hot_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.hot_list.setIconSize(QSize(160, 220))
        self.hot_list.setGridSize(QSize(180, 280))
        self.hot_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.hot_list.setWordWrap(True)
        self.hot_list.setStyleSheet("QListWidget { background-color: transparent; border: none; outline: none; } QListWidget::item { color: #e2e8f0; font-weight: bold; font-size: 13px; padding-top: 5px; border-radius: 10px; } QListWidget::item:hover { background-color: #1e293b; } QScrollBar:vertical { border: none; background: #111827; width: 8px; margin: 0px; } QScrollBar::handle:vertical { background: #374151; border-radius: 4px; min-height: 20px; } QScrollBar::handle:vertical:hover { background: #4b5563; }")
        self.hot_list.itemClicked.connect(self._on_hot_movie_clicked)
        grid_layout.addWidget(self.hot_list)
        self.content_stack.addWidget(self.page_grid)

        self.page_detail = QWidget()
        detail_layout = QVBoxLayout(self.page_detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)

        self.btn_back = QPushButton("⬅ Quay lại danh sách phim")
        self.btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_back.setStyleSheet("QPushButton { padding: 8px 15px; background-color: transparent; color: #94a3b8; border: 1px solid #374151; border-radius: 6px; font-weight: bold; text-align: left; } QPushButton:hover { background-color: #1e293b; color: #f8fafc; border: 1px solid #4b5563; }")
        self.btn_back.clicked.connect(self._go_back)
        btn_back_layout = QHBoxLayout(); btn_back_layout.addWidget(self.btn_back); btn_back_layout.addStretch()

        # Huy hiệu "ĐÃ TẢI RỒI" - chỉ hiện khi bộ phim đã nằm trong lịch sử tải
        self.lbl_downloaded_badge = QLabel("✅ BỘ NÀY ĐÃ TẢI RỒI")
        self.lbl_downloaded_badge.setStyleSheet("QLabel { background-color: #064e3b; color: #34d399; font-weight: bold; font-size: 13px; padding: 8px 14px; border-radius: 8px; border: 1px solid #10b981; }")
        self.lbl_downloaded_badge.hide()
        btn_back_layout.addWidget(self.lbl_downloaded_badge)

        # Chọn số luồng tải song song (ghi nhớ lựa chọn)
        lbl_threads = QLabel("⚡ Luồng tải:")
        lbl_threads.setStyleSheet("color: #94a3b8; font-size: 13px; padding-left: 12px;")
        btn_back_layout.addWidget(lbl_threads)
        self.threads_combo = QComboBox()
        self.threads_combo.addItems(["3", "5", "10"])
        try: self.threads_combo.setCurrentText(str(self.settings.value("threads_count", "3")))
        except Exception: pass
        self.threads_combo.currentTextChanged.connect(lambda v: self.settings.setValue("threads_count", v))
        self.threads_combo.setStyleSheet("QComboBox { background: #1f2937; color: #f8fafc; border: 1px solid #374151; border-radius: 6px; padding: 6px 12px; font-weight: bold; } QComboBox QAbstractItemView { background: #1f2937; color: #f8fafc; selection-background-color: #2563eb; }")
        btn_back_layout.addWidget(self.threads_combo)

        # Mở thư mục chứa đúng bộ phim đang xem
        self.btn_open_folder = QPushButton("📂 Thư mục phim")
        self.btn_open_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_folder.setStyleSheet("QPushButton { padding: 8px 15px; background-color: transparent; color: #38bdf8; border: 1px solid #374151; border-radius: 6px; font-weight: bold; } QPushButton:hover { background-color: #1e293b; border: 1px solid #38bdf8; }")
        self.btn_open_folder.clicked.connect(self._open_movie_folder)
        btn_back_layout.addWidget(self.btn_open_folder)
        detail_layout.addLayout(btn_back_layout)

        self.lbl_status = QLabel("Trạng thái: Sẵn sàng phục vụ...")
        self.lbl_status.setStyleSheet("color: #10b981; font-size: 14px; font-weight: bold; margin-top: 10px;")
        detail_layout.addWidget(self.lbl_status)

        self.total_progress = QProgressBar()
        self.total_progress.setFormat("Đã tải %v/%m tập  (%p%)")
        self.total_progress.setStyleSheet("QProgressBar { background: #1f2937; border: 1px solid #374151; border-radius: 8px; color: #f8fafc; font-weight: bold; text-align: center; min-height: 22px; } QProgressBar::chunk { background-color: #10b981; border-radius: 7px; }")
        self.total_progress.hide()
        detail_layout.addWidget(self.total_progress)

        self.table = QTableWidget(); self.table.setColumnCount(4); self.table.setHorizontalHeaderLabels(["Chọn Tập", "", "Tên File", "Trạng Thái Link"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 90); self.table.setColumnWidth(1, 50); self.table.setColumnWidth(3, 150)
        self.table.verticalHeader().setVisible(False); self.table.setShowGrid(False); self.table.setAlternatingRowColors(True); self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus); self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers); self.table.setIconSize(QSize(40, 50))  
        self.table.setStyleSheet("QTableWidget { background-color: #111827; alternate-background-color: #1f2937; color: #e2e8f0; border: 1px solid #374151; border-radius: 8px; outline: none; margin-top: 10px; font-size: 13px; } QHeaderView::section { background-color: #0f172a; color: #94a3b8; padding: 12px; font-weight: bold; border: none; border-bottom: 1px solid #374151; } QTableWidget::item { padding: 6px; border-bottom: 1px solid transparent; } QTableWidget::item:hover { background-color: #334155; } QTableWidget::indicator { width: 18px; height: 18px; border: 2px solid #475569; border-radius: 4px; } QTableWidget::indicator:checked { background-color: #10b981; border-color: #10b981; } QScrollBar:vertical { border: none; background: #111827; width: 8px; margin: 0px; } QScrollBar::handle:vertical { background: #374151; border-radius: 4px; min-height: 20px; } QScrollBar::handle:vertical:hover { background: #4b5563; }")
        detail_layout.addWidget(self.table)

        bottom_layout = QHBoxLayout()
        self.btn_select_all = QPushButton("☑ Chọn / Bỏ chọn tất cả")
        self.btn_select_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_select_all.setStyleSheet("QPushButton { padding: 14px; background-color: #4b5563; color: white; border-radius: 8px; font-weight: bold; font-size: 14px; margin-top: 10px; border: none;} QPushButton:hover { background-color: #64748b; }")
        self.btn_select_all.clicked.connect(self._toggle_select_all)
        self.btn_download = QPushButton("📥 Tải đã chọn")
        self.btn_download.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_download.setStyleSheet("QPushButton { padding: 14px 30px; background-color: #10b981; color: white; border-radius: 8px; font-weight: bold; font-size: 15px; margin-top: 10px; border: none; } QPushButton:hover { background-color: #059669; } QPushButton:disabled { background-color: #374151; color: #64748b; }")
        self.btn_download.setEnabled(False)
        self.btn_download.clicked.connect(self._download_selected)
        bottom_layout.addWidget(self.btn_select_all); bottom_layout.addWidget(self.btn_download)
        detail_layout.addLayout(bottom_layout)
        self.content_stack.addWidget(self.page_detail)
        self._render_history_sidebar()
        # Mỗi 60s kiểm tra: mục "chưa tải" quá 30 phút sẽ tự biến mất khỏi panel
        self._history_timer = QTimer(self)
        self._history_timer.timeout.connect(self._render_history_sidebar)
        self._history_timer.start(60000)

    def _update_genre_styles(self, active_btn):
        for btn in self.genre_buttons:
            if btn == active_btn: btn.setStyleSheet("QPushButton { background-color: #f59e0b; color: #ffffff; font-weight: bold; font-size: 14px; border-radius: 16px; padding: 8px 20px; border: none; } QPushButton:hover { background-color: #d97706; }")
            else: btn.setStyleSheet("QPushButton { background-color: #0ea5e9; color: #ffffff; font-weight: bold; font-size: 13px; border-radius: 16px; padding: 8px 18px; border: none; } QPushButton:hover { background-color: #38bdf8; }")

    def _on_genre_clicked(self, clicked_btn):
        self._update_genre_styles(clicked_btn)
        genre_tag = clicked_btn.property("genre_tag")
        self.url_input.clear()
        self.load_hot_movies_shelf(genre_tag)

    def load_hot_movies_shelf(self, genre=None):
        if hasattr(self, 'hot_thread') and self.hot_thread:
            try: self.hot_thread.item_loaded_signal.disconnect()
            except: pass
        if hasattr(self, 'search_thread') and self.search_thread:
            try: self.search_thread.results_signal.disconnect()
            except: pass

        self.hot_list.clear()
        self.current_genre = genre
        self.content_stack.setCurrentWidget(self.page_grid)
        self._cached_history_ids = {str(h.get('series_id', '')) for h in self._load_history()}

        if genre == "HISTORY":
            self.loading_bar.hide()
            history = self._load_history()
            if not history:
                empty = QListWidgetItem("📭 Bạn chưa tải bộ phim nào.")
                empty.setTextAlignment(Qt.AlignmentFlag.AlignCenter); empty.setFlags(Qt.ItemFlag.NoItemFlags)
                self.hot_list.addItem(empty); return

            covers_dir = self._get_covers_dir()
            missing_covers = []
            for row, h in enumerate(history):
                item = QListWidgetItem()
                title = h.get("title", "Không rõ tên")
                eps = h.get("total_episodes", 0)
                time_str = h.get("timestamp", "")
                eps_line = f"\n({eps} Tập)" if eps else ""
                item.setText(f"{title}{eps_line}\n[✅ Đã tải: {time_str}]")
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                # Ảnh bìa: đọc từ máy (đã lưu lúc tải phim); thiếu thì xếp hàng tải nền
                img_data = None
                cover_path = os.path.join(covers_dir, f"{h.get('series_id')}.img")
                if os.path.exists(cover_path):
                    try:
                        with open(cover_path, 'rb') as f: img_data = f.read()
                    except Exception: pass
                if img_data:
                    pixmap = QPixmap()
                    if pixmap.loadFromData(img_data) and not pixmap.isNull():
                        pixmap = pixmap.scaled(160, 220, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
                        item.setIcon(QIcon(pixmap))
                elif h.get('cover_url'):
                    missing_covers.append((row, str(h.get('series_id')), h.get('cover_url')))
                item.setData(Qt.ItemDataRole.UserRole, f"https://hongguoduanju.com/detail?series_id={h.get('series_id')}")
                self.hot_list.addItem(item)

            if missing_covers:
                if getattr(self, 'history_cover_thread', None) and self.history_cover_thread.isRunning():
                    return
                self.history_cover_thread = HistoryCoverThread(missing_covers, covers_dir)
                self.history_cover_thread.cover_ready.connect(self._on_history_cover_ready)
                self._keep_thread_alive(self.history_cover_thread)
                self.history_cover_thread.start()
            return

        self.loading_bar.show()
        msg = "⏳ Đang kết nối máy chủ để tải kệ phim...\nVui lòng chờ trong giây lát."
        if genre: msg = f"⏳ Đang lọc phim theo danh mục [{genre}]...\nVui lòng chờ trong giây lát."
            
        loading_item = QListWidgetItem(msg)
        loading_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter); loading_item.setFlags(Qt.ItemFlag.NoItemFlags) 
        self.hot_list.addItem(loading_item)
        self.is_first_movie = True 
        
        self.hot_thread = HotMoviesLoadThread(genre)
        self.hot_thread.item_loaded_signal.connect(self._render_single_hot_movie)
        self.hot_thread.finished_signal.connect(self.loading_bar.hide)
        self._keep_thread_alive(self.hot_thread)
        self.hot_thread.start()

    def _on_history_cover_ready(self, row, img_bytes):
        item = self.hot_list.item(row)
        if not item: return
        pixmap = QPixmap()
        if pixmap.loadFromData(img_bytes) and not pixmap.isNull():
            pixmap = pixmap.scaled(160, 220, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            item.setIcon(QIcon(pixmap))

    def _render_history_sidebar(self):
        """Vẽ panel Lịch Sử Tải bên phải: ảnh bìa nhỏ + tên + số tập + ngày tải."""
        if not hasattr(self, 'history_list'): return
        history = self._load_history()
        downloaded_ids = {str(h.get('series_id')) for h in history}
        scanned = [s for s in self._load_scan_history() if str(s.get('series_id')) not in downloaded_ids]
        # Không có gì đổi -> giữ nguyên panel, không vẽ lại (chống lag)
        sig = (tuple((s.get('series_id'), s.get('ts')) for s in scanned),
               tuple((h.get('series_id'), h.get('timestamp'), h.get('total_episodes')) for h in history))
        if sig == getattr(self, '_history_sig', None) and self.history_list.count() > 0:
            return
        self._history_sig = sig
        if not hasattr(self, '_sidebar_icon_cache'): self._sidebar_icon_cache = {}
        self.history_list.clear()
        if not history and not scanned:
            empty = QListWidgetItem("📭 Chưa tải phim nào")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.history_list.addItem(empty)
            return
        covers_dir = self._get_covers_dir()
        missing = []
        # ⏳ Phim ĐÃ QUÉT nhưng CHƯA TẢI (nằm trên cùng, chữ cam, tự xóa sau 30 phút)
        merged = [("scan", s) for s in scanned] + [("done", h) for h in history]
        for row, (kind, h) in enumerate(merged):
            item = QListWidgetItem()
            title = h.get("title", "Không rõ tên")
            eps = h.get("total_episodes", 0)
            time_str = h.get("timestamp", "")
            eps_line = f" • {eps} Tập" if eps else ""
            if kind == "scan":
                item.setText(f"{title}{eps_line}\n⏳ Chưa tải • quét lúc {time_str}")
                item.setForeground(QColor("#f59e0b"))
            else:
                item.setText(f"{title}{eps_line}\n✅ {time_str}")
            sid = str(h.get('series_id'))
            icon = self._sidebar_icon_cache.get(sid)
            if icon is None:
                cover_path = os.path.join(covers_dir, f"{sid}.img")
                if os.path.exists(cover_path):
                    try:
                        pixmap = QPixmap()
                        with open(cover_path, 'rb') as f:
                            if pixmap.loadFromData(f.read()) and not pixmap.isNull():
                                icon = QIcon(pixmap.scaled(52, 70, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation))
                                self._sidebar_icon_cache[sid] = icon
                    except Exception: pass
            if icon:
                item.setIcon(icon)
            elif h.get('cover_url'):
                missing.append((row, sid, h.get('cover_url')))
            item.setData(Qt.ItemDataRole.UserRole, f"https://hongguoduanju.com/detail?series_id={h.get('series_id')}")
            self.history_list.addItem(item)
        if missing:
            # Thread cũ còn đang tải thì thôi - ảnh sẽ vào cache, lần vẽ sau tự có
            if getattr(self, 'sidebar_cover_thread', None) and self.sidebar_cover_thread.isRunning():
                return
            self.sidebar_cover_thread = HistoryCoverThread(missing, covers_dir)
            self.sidebar_cover_thread.cover_ready.connect(self._on_sidebar_cover_ready)
            self._keep_thread_alive(self.sidebar_cover_thread)
            self.sidebar_cover_thread.start()

    def _on_sidebar_cover_ready(self, row, img_bytes):
        item = self.history_list.item(row)
        if not item: return
        pixmap = QPixmap()
        if pixmap.loadFromData(img_bytes) and not pixmap.isNull():
            icon = QIcon(pixmap.scaled(52, 70, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation))
            item.setIcon(icon)
            # Nạp vào cache để lần vẽ sau không phải đọc lại
            url = item.data(Qt.ItemDataRole.UserRole) or ""
            sid = url.split("series_id=")[-1] if "series_id=" in url else ""
            if sid and hasattr(self, '_sidebar_icon_cache'): self._sidebar_icon_cache[sid] = icon

    def _on_history_item_clicked(self, item):
        url = item.data(Qt.ItemDataRole.UserRole)
        if not url: return
        # Đang quét dở 1 bộ thì bỏ qua click mới (chống spam tạo thread)
        if getattr(self, 'scan_thread', None) and self.scan_thread.isRunning(): return
        # Đúng bộ đang mở sẵn -> nhảy thẳng vào trang tập, khỏi quét lại (chống lag)
        sid = url.split("series_id=")[-1] if "series_id=" in url else ""
        if sid and sid == str(getattr(self, 'current_series_id', '')) and self.table.rowCount() > 0:
            self.content_stack.setCurrentWidget(self.page_detail)
            return
        self.url_input.setText(url)
        self._scan()

    def _render_single_hot_movie(self, m):
        if self.is_first_movie:
            self.hot_list.clear()
            self.is_first_movie = False

        item = QListWidgetItem()
        title = m.get("title", "Phim Hot Gợi Ý")
        eps = m.get("total_episodes", 0)
        series_id = str(m.get("series_id", ""))
        
        downloaded_tag = "\n[✅ Đã tải trong bộ nhớ]" if series_id in self._cached_history_ids else ""
        item.setText(f"{title}\n({eps} Tập){downloaded_tag}")
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # Chỉ dựa vào Server để vẽ ảnh. Không có = vẽ chữ. Bất tử, không văng app.
        img_data = m.get("img_data")
        if img_data:
            pixmap = QPixmap()
            if pixmap.loadFromData(img_data) and not pixmap.isNull():
                pixmap = pixmap.scaled(160, 220, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
                item.setIcon(QIcon(pixmap))
        
        item.setData(Qt.ItemDataRole.UserRole, m.get("url", "")) 
        self.hot_list.addItem(item)

    def _on_hot_movie_clicked(self, item):
        url = item.data(Qt.ItemDataRole.UserRole)
        if not url: return
        if getattr(self, 'scan_thread', None) and self.scan_thread.isRunning(): return
        sid = url.split("series_id=")[-1] if "series_id=" in url else ""
        if sid and sid == str(getattr(self, 'current_series_id', '')) and self.table.rowCount() > 0:
            self.content_stack.setCurrentWidget(self.page_detail)
            return
        self.url_input.setText(url)
        self._scan()

    def _go_back(self):
        if self.monitor_thread: self.monitor_thread.stop()
        self.url_input.clear()
        # Đang đứng ở tab Lịch Sử Tải -> vẽ lại lịch sử (kèm phim vừa tải xong), không nhảy về kệ phim hot
        if getattr(self, 'current_genre', None) == "HISTORY":
            self.load_hot_movies_shelf("HISTORY")
            return
        self.content_stack.setCurrentWidget(self.page_grid)

    def _normalize_url(self, raw_url):
        if "hongguoduanju.com/detail" in raw_url or "hongguoduanju.com/player" in raw_url: return raw_url
        video_series_id = None
        decoded = raw_url
        for _ in range(4):
            new_decoded = unquote(decoded)
            if new_decoded == decoded: break
            decoded = new_decoded

        match = re.search(r'"video_series_id"\s*:\s*"(\d+)"', decoded)
        if match: video_series_id = match.group(1)

        if not video_series_id:
            try:
                parsed = urlparse(raw_url); params = parse_qs(parsed.query)
                zlink = params.get("zlink", [None])[0]
                if zlink:
                    zlink_decoded = unquote(zlink); zlink_parsed = urlparse(zlink_decoded); zlink_params = parse_qs(zlink_parsed.query)
                    scheme_params_raw = zlink_params.get("schemeParams", [None])[0]
                    if scheme_params_raw:
                        try: scheme_json = json.loads(unquote(scheme_params_raw)); video_series_id = str(scheme_json.get("video_series_id", ""))
                        except: pass
                if not video_series_id:
                    scheme_params_raw = params.get("schemeParams", [None])[0]
                    if scheme_params_raw:
                        try: scheme_json = json.loads(unquote(scheme_params_raw)); video_series_id = str(scheme_json.get("video_series_id", ""))
                        except: pass
            except: pass

        if not video_series_id:
            match = re.search(r'video_series_id[=%22":]+(\d{15,25})', decoded)
            if match: video_series_id = match.group(1)

        if video_series_id: return f"https://hongguoduanju.com/detail?series_id={video_series_id}"
        return raw_url

    def _extract_url_from_text(self, text):
        match = re.search(r'(https?://\S+)', text)
        return match.group(1) if match else text

    def _scan(self):
        raw_text = self.url_input.text().strip()
        if not raw_text:
            QMessageBox.warning(self, "Lỗi", "Vui lòng dán Link hoặc nhập Tên phim vào ô trống!")
            return
            
        is_url = False
        if re.search(r'https?://', raw_text) or "hongguoduanju.com" in raw_text: is_url = True

        if not is_url:
            self._search_keyword(raw_text)
            return

        if self.monitor_thread: self.monitor_thread.stop()
        raw_url = self._extract_url_from_text(raw_text)
        url = self._normalize_url(raw_url)
        if url != raw_text: self.url_input.setText(url) 

        self.content_stack.setCurrentWidget(self.page_detail)
        self.btn_scan.setEnabled(False)
        self.btn_scan.setText("⏳ Đang xử lý yêu cầu...")
        self.lbl_status.setText("Trạng thái: Đang kết nối phân tích dữ liệu...")
        self.table.setRowCount(0)

        self.scan_thread = HonggouScanThread(url, self.auth_token)
        self._keep_thread_alive(self.scan_thread)
        self.scan_thread.scan_result.connect(self._on_scan_result)
        self.scan_thread.error_signal.connect(self._on_scan_error)
        self.scan_thread.url_resolved_signal.connect(self._on_url_resolved)
        self.scan_thread.start()

    def _search_keyword(self, keyword):
        self.content_stack.setCurrentWidget(self.page_grid)
        for btn in self.genre_buttons: btn.setStyleSheet("QPushButton { background-color: #0ea5e9; color: #ffffff; font-weight: bold; font-size: 13px; border-radius: 16px; padding: 8px 18px; border: none; } QPushButton:hover { background-color: #38bdf8; }")
        self.hot_list.clear()
        loading_item = QListWidgetItem(f"🔍 Đang tìm kiếm phim: '{keyword}'...")
        loading_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter); loading_item.setFlags(Qt.ItemFlag.NoItemFlags) 
        self.hot_list.addItem(loading_item)
        self.loading_bar.show()

        if hasattr(self, 'search_thread') and self.search_thread:
            try: self.search_thread.results_signal.disconnect()
            except: pass
        if hasattr(self, 'hot_thread') and self.hot_thread:
            try: self.hot_thread.item_loaded_signal.disconnect()
            except: pass

        self._cached_history_ids = {str(h.get('series_id', '')) for h in self._load_history()}

        self.search_thread = SearchMoviesThread(keyword, self.auth_token)
        self._keep_thread_alive(self.search_thread)
        self.search_thread.results_signal.connect(self._on_search_results)
        self.search_thread.error_signal.connect(self._on_search_error)
        self.search_thread.start()

    def _on_search_results(self, results):
        self.loading_bar.hide() 
        self.hot_list.clear()
        if not results:
            empty_item = QListWidgetItem("❌ Không tìm thấy bộ phim nào phù hợp.")
            empty_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter); empty_item.setFlags(Qt.ItemFlag.NoItemFlags) 
            self.hot_list.addItem(empty_item); return

        for m in results:
            item = QListWidgetItem()
            title = m.get("title", "Không rõ tên")
            eps = m.get("total_episodes", 0)
            series_id = str(m.get("series_id", ""))
            source_tag = "🚀 Nguồn VIP" if m.get("is_local") else "🌐 Nguồn Web"
            
            if series_id in getattr(self, '_cached_history_ids', set()):
                source_tag += "\n[✅ Đã tải trong bộ nhớ]"
            
            item.setText(f"{title}\n({eps} Tập)\n[{source_tag}]")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setData(Qt.ItemDataRole.UserRole, f"https://hongguoduanju.com/detail?series_id={series_id}") 
            self.hot_list.addItem(item)

    def _on_search_error(self, error_msg):
        self.loading_bar.hide() 
        self.hot_list.clear()
        empty_item = QListWidgetItem(f"❌ Lỗi tìm kiếm: {error_msg}")
        empty_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter); empty_item.setFlags(Qt.ItemFlag.NoItemFlags) 
        self.hot_list.addItem(empty_item)

    def _on_scan_result(self, data):
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("🔍 Tìm / Quét Phim")
        
        status = data.get("status")
        self.current_job_id = data.get("job_id")
        self.current_series_id = str(data.get("series_id", "")) 
        self.current_title = data.get("title", "Không rõ tên")
        self.current_cover_url = data.get("cover_url", "")
        total_eps = data.get("total_episodes", 0)
        self.current_episodes = data.get("episodes", [])
        
        self.current_cover_pixmap = None
        self.current_cover_bytes = None
        self.current_total_eps = total_eps
        if self.current_cover_url:
            try:
                _cu = 'https:' + self.current_cover_url if self.current_cover_url.startswith('//') else self.current_cover_url
                resp = requests.get(_cu, timeout=8, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://hongguoduanju.com/"})
                if resp.status_code == 200:
                    self.current_cover_bytes = resp.content
                    pix = QPixmap()
                    if pix.loadFromData(resp.content) and not pix.isNull():
                        self.current_cover_pixmap = pix.scaled(40, 50, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            except: pass
        
        # Huy hiệu ĐÃ TẢI RỒI
        if hasattr(self, 'lbl_downloaded_badge'):
            if str(self.current_series_id) in getattr(self, '_cached_history_ids', set()): self.lbl_downloaded_badge.show()
            else: self.lbl_downloaded_badge.hide()

        # Ghi vào lịch sử QUÉT để khách nhớ bộ vừa xem (tự xóa sau 30 phút nếu không tải)
        try:
            self._save_to_scan_history(self.current_series_id, self.current_title, self.current_cover_url,
                                       total_eps=total_eps or len(self.current_episodes),
                                       cover_bytes=self.current_cover_bytes)
        except Exception: pass

        if status == "cache_hit":
            self.lbl_status.setText(f"✅ Trích xuất thành công từ Nguồn VIP! (Tổng: {total_eps} tập). Bạn có thể chọn tập và lưu ngay.")
            self.btn_download.setEnabled(True)
        elif status == "retrying":
            self.lbl_status.setText(f"⚠️ Hệ thống đang tiến hành trích xuất bổ sung.")
            self.btn_download.setEnabled(True) 
        elif status == "processing":
            self.lbl_status.setText("⏳ Hệ thống đang xử lý phân tích. Link sẽ tự động cập nhật khi có!")
            self.btn_download.setEnabled(True)
        else:
            self.lbl_status.setText(f"🕒 Đã tiếp nhận yêu cầu! Hệ thống chuẩn bị trích xuất {total_eps} tập.")
            self.btn_download.setEnabled(False)

        self._render_table(total_eps, self.current_episodes)

        if status not in ["cache_hit", "completed"] and self.current_job_id:
            self.monitor_thread = JobStatusMonitorThread(self.current_job_id, self.auth_token)
            self._keep_thread_alive(self.monitor_thread)
            self.monitor_thread.update_signal.connect(self._on_monitor_update)
            self.monitor_thread.start()

    def _on_monitor_update(self, data):
        status = data.get("status")
        total_eps = data.get("total_episodes", self.table.rowCount())
        self.current_episodes = data.get("episodes", [])

        if status == "completed":
            self.lbl_status.setText(f"✅ Quá trình phân tích hoàn tất! Nguồn VIP đã sẵn sàng (Tổng: {total_eps} tập).")
            self.btn_download.setEnabled(True)
        elif status == "partial":
            self.lbl_status.setText(f"⚠️ Hệ thống đã trích xuất được một phần. (Hiện có: {len(self.current_episodes)} tập).")
            self.btn_download.setEnabled(True)
        elif status == "processing":
            self.lbl_status.setText(f"⏳ Đang xử lý... Đã trích xuất {len(self.current_episodes)}/{total_eps} tập.")
            self.btn_download.setEnabled(True)

        self._render_table(total_eps, self.current_episodes)

    def _render_table(self, total_eps, episodes):
        checked_eps = set()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked: checked_eps.add(row)

        if self.table.rowCount() != total_eps: self.table.setRowCount(total_eps)
        readonly_flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        movie_folder = os.path.join(self.save_folder, str(getattr(self, 'current_series_id', '') or ''))
            
        for i in range(total_eps):
            ep_num = i + 1
            ep_data = next((e for e in episodes if e.get("episode_number") == ep_num), None)
            
            ep_item = QTableWidgetItem(f" Tập {ep_num}")
            ep_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            ep_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            if i in checked_eps: ep_item.setCheckState(Qt.CheckState.Checked)
            else: ep_item.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(i, 0, ep_item)
            self.table.setRowHeight(i, 55)
            
            thumb_item = QTableWidgetItem()
            thumb_item.setFlags(readonly_flags)
            if hasattr(self, 'current_cover_pixmap') and self.current_cover_pixmap: thumb_item.setIcon(QIcon(self.current_cover_pixmap))
            self.table.setItem(i, 1, thumb_item)
            
            if ep_data and ep_data.get("drive_link"):
                file_item = QTableWidgetItem(ep_data.get("file_name", f"Tap_{ep_num}.mp4"))
                file_item.setFlags(readonly_flags); file_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter); self.table.setItem(i, 2, file_item)
                # Soi ổ đĩa: tập này đã có file thật trong thư mục phim chưa?
                safe_name = re.sub(r'[\\/*?:"<>|]', "", ep_data.get("file_name", f"Tap_{ep_num}.mp4"))
                local_path = os.path.join(movie_folder, safe_name)
                if os.path.exists(local_path) and os.path.getsize(local_path) > 1024:
                    link_item = QTableWidgetItem("💾 Đã có trên máy")
                    link_item.setForeground(QColor("#c084fc"))
                    _f = link_item.font(); _f.setBold(True); link_item.setFont(_f)
                else:
                    link_item = QTableWidgetItem("✅ Sẵn sàng")
                    link_item.setForeground(QColor("#10b981"))
                link_item.setFlags(readonly_flags); link_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter); self.table.setItem(i, 3, link_item)
            else:
                file_item = QTableWidgetItem("---")
                file_item.setFlags(readonly_flags); file_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter); self.table.setItem(i, 2, file_item)
                wait_item = QTableWidgetItem("⏳ Đang đợi")
                wait_item.setFlags(readonly_flags); wait_item.setForeground(QColor("#f59e0b")); wait_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter); self.table.setItem(i, 3, wait_item)

    def _toggle_select_all(self):
        def _on_disk(i):
            st = self.table.item(i, 3)
            return bool(st and "Đã có trên máy" in st.text())

        # Chỉ xét các tập CHƯA có trên máy - tập đã tải rồi không bị chọn theo (khỏi mất tiền oan)
        selectable = [i for i in range(self.table.rowCount()) if not _on_disk(i)]
        all_checked = bool(selectable) and all(
            (self.table.item(i, 0) is not None and self.table.item(i, 0).checkState() == Qt.CheckState.Checked)
            for i in selectable
        )
        new_state = Qt.CheckState.Unchecked if all_checked else Qt.CheckState.Checked
        skipped = 0
        for i in range(self.table.rowCount()):
            item = self.table.item(i, 0)
            if not item: continue
            if new_state == Qt.CheckState.Checked and _on_disk(i):
                item.setCheckState(Qt.CheckState.Unchecked)
                skipped += 1
            else:
                item.setCheckState(new_state)
        if new_state == Qt.CheckState.Checked and skipped:
            self.lbl_status.setText(f"☑ Đã chọn {len(selectable)} tập cần tải (bỏ qua {skipped} tập đã có trên máy).")

    def _download_selected(self):
        selected_eps = []
        for i in range(self.table.rowCount()):
            item = self.table.item(i, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                ep_num = i + 1
                ep_data = next((e for e in self.current_episodes if e.get("episode_number") == ep_num), None)
                if ep_data and ep_data.get("drive_link"): selected_eps.append(ep_data)

        if not selected_eps:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn (tích) ít nhất 1 tập đã xử lý xong!")
            return
            
        if self.current_series_id in getattr(self, '_cached_history_ids', set()):
            reply = QMessageBox.question(self, "Cảnh báo tải trùng", "Bộ phim này bạn ĐÃ TẢI VỀ máy trước đó rồi!\nBạn có chắc chắn muốn TẢI LẠI và BỊ TRỪ TIỀN không?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No: return

        num_eps = len(selected_eps)
        folder_name = self.current_series_id if self.current_series_id else "Phim_Khong_Ro_ID"
        final_save_path = os.path.join(self.save_folder, folder_name)
        os.makedirs(final_save_path, exist_ok=True)

        try:
            res = requests.post(f"{SERVER_URL}/api/client/pay_for_download", json={"username": self.username, "num_episodes": num_eps}, headers={"Authorization": f"Bearer {self.auth_token}"}, timeout=10)
            data = res.json()
            if data.get("status") == "success":
                self._save_to_history(self.current_series_id, self.current_title, self.current_cover_url,
                                      total_eps=len(self.current_episodes) or getattr(self, 'current_total_eps', 0),
                                      cover_bytes=getattr(self, 'current_cover_bytes', None))
                self.btn_download.setEnabled(False)
                self.btn_download.setText("⏳ Đang lưu về máy...")
                self.lbl_status.setText(f"⏳ Đang lưu {num_eps} tập về máy...")
                self._refresh_balance()

                self._dl_finished = 0
                self.total_progress.setMaximum(num_eps); self.total_progress.setValue(0); self.total_progress.show()
                try: max_threads = int(self.threads_combo.currentText())
                except Exception: max_threads = 3
                self.download_manager = DriveDownloadManager(selected_eps, final_save_path, parent=self, max_concurrent=max_threads)
                self.download_manager.progress_signal.connect(self._on_download_progress)
                self.download_manager.done_signal.connect(self._on_episode_downloaded)
                self.download_manager.error_signal.connect(self._on_download_error)
                self.download_manager.all_done_signal.connect(self._on_all_downloads_done)
                self.download_manager.start()
            else: QMessageBox.critical(self, "Không đủ số dư", data.get("message", "Vui lòng nạp thêm tiền!"))
        except Exception as e: QMessageBox.critical(self, "Lỗi mạng", f"Không thể kết nối đến Hệ thống: {e}")

    def _open_movie_folder(self):
        folder = os.path.join(self.save_folder, str(getattr(self, 'current_series_id', '') or ''))
        if not os.path.isdir(folder): folder = self.save_folder
        try: os.startfile(folder)
        except Exception as e: QMessageBox.warning(self, "Lỗi", f"Không mở được thư mục: {e}")

    def _bump_total_progress(self):
        if not hasattr(self, 'total_progress'): return
        self._dl_finished = getattr(self, '_dl_finished', 0) + 1
        self.total_progress.setValue(min(self._dl_finished, self.total_progress.maximum()))

    def _refresh_balance(self):
        try:
            res = requests.get(f"{SERVER_URL}/api/client/balance/{self.username}", headers={"Authorization": f"Bearer {self.auth_token}"}, timeout=5)
            if res.status_code == 200:
                balance = res.json().get("balance", 0)
                if hasattr(self, 'balance_changed') and self.balance_changed: self.balance_changed(balance)
        except: pass

    def _on_download_progress(self, ep_num, percent, speed_mb):
        row = ep_num - 1
        if row < self.table.rowCount():
            status_item = QTableWidgetItem(f"⬇️ {percent}% ({speed_mb:.1f} MB/s)")
            status_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable); status_item.setForeground(QColor("#38bdf8")); status_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 3, status_item)

    def _on_episode_downloaded(self, ep_num, file_path):
        row = ep_num - 1
        if row < self.table.rowCount():
            done_item = QTableWidgetItem("✔ ĐÃ XONG")
            done_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable); done_item.setForeground(QColor("#22d3ee")); done_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            _f = done_item.font(); _f.setBold(True); done_item.setFont(_f)
            self.table.setItem(row, 3, done_item)
        self._bump_total_progress()

    def _on_download_error(self, ep_num, error_msg):
        row = ep_num - 1
        if row < self.table.rowCount():
            short_msg = error_msg[:25] + "..." if len(error_msg) > 25 else error_msg
            err_item = QTableWidgetItem(f"❌ {short_msg}")
            err_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable); err_item.setForeground(QColor("#ef4444")); err_item.setToolTip(str(error_msg)); err_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 3, err_item)
        self._bump_total_progress()

    def _on_all_downloads_done(self, total_downloaded):
        self.btn_download.setEnabled(True)
        self.btn_download.setText("📥 Tải đã chọn")
        self.lbl_status.setText(f"✅ Hoàn tất! Đã lưu {total_downloaded} tập về máy.")
        if hasattr(self, 'lbl_downloaded_badge'): self.lbl_downloaded_badge.show()
        self._refresh_balance()
        QMessageBox.information(self, "Thành công", f"Đã lưu thành công {total_downloaded} tập phim về máy bạn!")

    def _change_folder(self):
        new_folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu phim", self.save_folder)
        if new_folder:
            self.save_folder = new_folder
            self.settings.setValue(f"download_folder_{self.username}", new_folder)
            self.lbl_folder.setText(f"📂 Lưu vào: {self.save_folder}")

    def _on_scan_error(self, error_msg):
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("🔍 Tìm / Quét Phim")
        self.lbl_status.setText("Trạng thái: Sẵn sàng phục vụ...")
        QMessageBox.critical(self, "Lỗi Hệ Thống", error_msg)

    def _on_url_resolved(self, resolved_url):
        self.url_input.setText(resolved_url)

# ==========================================
# AUTO-UPDATER: KIỂM TRA & CẬP NHẬT PHIÊN BẢN MỚI
# ==========================================
def _compare_versions(current: str, latest: str) -> bool:
    try: return [int(x) for x in latest.split(".")] > [int(x) for x in current.split(".")]
    except: return False

def _get_exe_path() -> str:
    if getattr(sys, 'frozen', False): return sys.executable
    else: return os.path.abspath(sys.argv[0])

class UpdateCheckThread(QThread):
    update_available = pyqtSignal(str, str, str, bool) 
    no_update = pyqtSignal()

    def run(self):
        try:
            res = requests.get(f"{SERVER_URL}/api/client/check_update", params={"current_version": APP_VERSION}, timeout=10)
            if res.status_code == 200:
                data = res.json()
                latest = data.get("latest_version", APP_VERSION)
                if _compare_versions(APP_VERSION, latest): self.update_available.emit(latest, data.get("download_url", ""), data.get("changelog", ""), data.get("force_update", False))
                else: self.no_update.emit()
            else: self.no_update.emit()
        except: pass

class DownloadUpdateThread(QThread):
    progress_signal = pyqtSignal(int)
    done_signal = pyqtSignal(str)
    error_signal = pyqtSignal(str)

    def __init__(self, download_url: str):
        super().__init__()
        self.download_url = download_url

    def _extract_file_id(self, drive_link):
        match_web = re.search(r'/d/([a-zA-Z0-9_-]+)', drive_link)
        if match_web: return match_web.group(1)
        match_dl = re.search(r'id=([a-zA-Z0-9_-]+)', drive_link)
        if match_dl: return match_dl.group(1)
        return None

    def run(self):
        try:
            url = self.download_url
            session = requests.Session()
            
            # Xử lý đặc biệt nếu link update cũng là Google Drive
            if 'drive.google.com' in url:
                file_id = self._extract_file_id(url)
                if file_id:
                    URL_BASE = "https://drive.google.com/uc?export=download"
                    resp = session.get(URL_BASE, params={'id': file_id}, stream=True, timeout=30)
                    
                    token = None
                    for key, value in resp.cookies.items():
                        if key.startswith('download_warning'):
                            token = value
                            break
                    
                    if token:
                        resp = session.get(URL_BASE, params={'id': file_id, 'confirm': token}, stream=True, timeout=30)
                    else:
                        content_type = resp.headers.get('Content-Type', '')
                        if 'text/html' in content_type:
                            match = re.search(r'confirm=([0-9A-Za-z_-]+)', resp.text)
                            if match:
                                token = match.group(1)
                                resp = session.get(URL_BASE, params={'id': file_id, 'confirm': token}, stream=True, timeout=30)
                            else:
                                resp = session.get(URL_BASE, params={'id': file_id, 'confirm': 't'}, stream=True, timeout=30)
                else:
                    resp = session.get(url, stream=True, timeout=30)
            else:
                # Tải bình thường từ host khác
                resp = session.get(url, stream=True, timeout=30)

            # CHỐT CHẶN: Đảm bảo không lưu nhầm file HTML thành file EXE cập nhật
            if 'text/html' in resp.headers.get('Content-Type', '') and 'drive.google.com' in url:
                self.error_signal.emit("Lỗi: Không thể tải bản cập nhật (Bị chặn bởi Google Drive).")
                return

            total_size = int(resp.headers.get('content-length', 0))
            downloaded = 0
            temp_path = os.path.join(tempfile.gettempdir(), "Hongguo_Update.exe")

            with open(temp_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0: self.progress_signal.emit(int(downloaded * 100 / total_size))

            if os.path.getsize(temp_path) < 1_000_000:
                self.error_signal.emit("File tải về bị lỗi. Vui lòng báo cho Admin!")
                return
            self.done_signal.emit(temp_path)
        except Exception as e: self.error_signal.emit(str(e))

def _apply_update_and_restart(new_exe_path: str):
    current_exe = _get_exe_path()
    bat_path = os.path.join(tempfile.gettempdir(), "hongguo_update.bat")
    bat_content = f'''@echo off
timeout /t 3 /nobreak >nul
set RETRY=0
:COPY_LOOP
if %RETRY% GEQ 20 goto COPY_DONE
copy /Y "{new_exe_path}" "{current_exe}" >nul 2>&1
if %ERRORLEVEL%==0 goto COPY_DONE
set /a RETRY+=1
timeout /t 1 /nobreak >nul
goto COPY_LOOP
:COPY_DONE
timeout /t 2 /nobreak >nul
start "" "{current_exe}"
del /f /q "{new_exe_path}" >nul 2>&1
del /f /q "%~f0" >nul 2>&1
'''
    try:
        with open(bat_path, "w", encoding="utf-8") as f: f.write(bat_content)
        subprocess.Popen(["cmd", "/c", bat_path], creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        QMessageBox.critical(None, "Lỗi", f"Không thể khởi chạy trình cập nhật: {e}")
        return
    QApplication.instance().quit()

class AutoUpdater:
    def __init__(self, header_layout: QHBoxLayout, parent_widget=None):
        self.parent = parent_widget
        self._download_url = ""
        self._latest_version = ""
        self._changelog = ""

        self.btn_update = QPushButton()
        self.btn_update.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_update.setVisible(False)
        self.btn_update.clicked.connect(self._on_update_clicked)
        self.btn_update.setStyleSheet("QPushButton { padding: 8px 16px; background-color: #f59e0b; color: #000; border-radius: 6px; font-weight: bold; font-size: 13px; border: none; } QPushButton:hover { background-color: #d97706; }")

        logout_index = header_layout.count() - 1
        header_layout.insertWidget(logout_index, self.btn_update)
        header_layout.insertSpacing(logout_index + 1, 10)

        self._check_thread = UpdateCheckThread()
        self._check_thread.update_available.connect(self._on_update_found)
        self._check_thread.start()

    def _on_update_found(self, version, url, changelog, force):
        self._latest_version = version; self._download_url = url; self._changelog = changelog
        self.btn_update.setText(f"🔄 Cập nhật v{version}"); self.btn_update.setVisible(True)
        if force: QMessageBox.warning(self.parent, "Bắt buộc cập nhật", f"Phiên bản {version} là bản cập nhật bắt buộc.\nVui lòng cập nhật để tiếp tục sử dụng.\n\n{changelog}")

    def _on_update_clicked(self):
        msg = f"Phiên bản mới: v{self._latest_version}\nHiện tại: v{APP_VERSION}\n\n"
        if self._changelog: msg += f"Thay đổi:\n{self._changelog}\n\n"
        msg += "Nhấn OK để tải bản mới.\nApp sẽ tự tắt → cập nhật → mở lại."
        if QMessageBox.question(self.parent, "Cập nhật phần mềm", msg, QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Ok: return

        self.progress = QProgressDialog("Đang tải phiên bản mới...", None, 0, 100, self.parent)
        self.progress.setWindowTitle("Cập nhật Hongguo Downloader")
        self.progress.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress.setCancelButton(None); self.progress.setMinimumDuration(0); self.progress.setValue(0)
        self.progress.setStyleSheet("QProgressDialog { background: #1e293b; color: white; } QProgressBar { border: 1px solid #374151; border-radius: 6px; background: #111827; text-align: center; color: white; } QProgressBar::chunk { background-color: #10b981; border-radius: 5px; }")
        self.progress.show()
        self.btn_update.setEnabled(False); self.btn_update.setText("⏳ Đang tải...")

        self._dl_thread = DownloadUpdateThread(self._download_url)
        self._dl_thread.progress_signal.connect(lambda p: (self.progress.setValue(p), self.progress.setLabelText(f"Đang tải... {p}%")))
        self._dl_thread.done_signal.connect(self._on_dl_done)
        self._dl_thread.error_signal.connect(self._on_dl_error)
        self._dl_thread.start()

    def _on_dl_done(self, new_exe_path):
        self.progress.close()
        QMessageBox.information(self.parent, "Sẵn sàng", "Tải xong bản mới!\nApp sẽ tự đóng, cập nhật, và mở lại.\nNhấn OK.")
        _apply_update_and_restart(new_exe_path)

    def _on_dl_error(self, error_msg):
        self.progress.close(); self.btn_update.setEnabled(True); self.btn_update.setText(f"🔄 Cập nhật v{self._latest_version}")
        QMessageBox.critical(self.parent, "Lỗi cập nhật", f"Không thể tải bản mới:\n{error_msg}")

# ==========================================
# MÀN HÌNH ĐĂNG NHẬP
# ==========================================
class LoginScreen(QWidget):
    login_success = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.setStyleSheet("background-color: #0f172a; color: white;")
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.settings = QSettings("HongguoDownloader", "ClientApp")

        login_box = QWidget(); login_box.setFixedWidth(400)
        login_box.setStyleSheet("background-color: #1e293b; border-radius: 12px; border: 1px solid #334155;")
        box_layout = QVBoxLayout(login_box); box_layout.setContentsMargins(30, 40, 30, 40); box_layout.setSpacing(15)

        title = QLabel("ĐĂNG NHẬP HỆ THỐNG")
        title.setFont(QFont("Arial", 18, QFont.Weight.Bold)); title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color: #38bdf8; border: none; margin-bottom: 15px;")
        box_layout.addWidget(title)

        self.inp_user = QLineEdit(); self.inp_user.setPlaceholderText("Tên đăng nhập")
        self.inp_user.setStyleSheet("padding: 14px; border-radius: 8px; border: 1px solid #475569; background: #0f172a;")
        box_layout.addWidget(self.inp_user)

        self.inp_pass = QLineEdit(); self.inp_pass.setPlaceholderText("Mật khẩu"); self.inp_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.inp_pass.setStyleSheet("padding: 14px; border-radius: 8px; border: 1px solid #475569; background: #0f172a;")
        box_layout.addWidget(self.inp_pass)

        saved_user = self.settings.value("username", ""); saved_pwd = self.settings.value("password", "")
        if saved_user: self.inp_user.setText(saved_user); self.inp_pass.setText(saved_pwd)

        self.btn_login = QPushButton("Đăng Nhập")
        self.btn_login.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_login.setStyleSheet("QPushButton { padding: 14px; background-color: #2563eb; color: white; border-radius: 8px; font-weight: bold; font-size: 14px; margin-top: 10px; border: none;} QPushButton:hover { background-color: #1d4ed8; }")
        self.btn_login.clicked.connect(self._handle_login)
        box_layout.addWidget(self.btn_login)

        self.btn_register = QPushButton("Tạo Tài Khoản Mới")
        self.btn_register.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_register.setStyleSheet("QPushButton { padding: 14px; background-color: #10b981; color: white; border-radius: 8px; font-weight: bold; font-size: 14px; border: none;} QPushButton:hover { background-color: #059669; }")
        self.btn_register.clicked.connect(self._handle_register)
        box_layout.addWidget(self.btn_register)
        layout.addWidget(login_box)

    def _handle_login(self):
        user = self.inp_user.text().strip(); pwd = self.inp_pass.text().strip()
        if not user or not pwd: QMessageBox.warning(self, "Lỗi", "Vui lòng nhập đủ thông tin!"); return
        self.btn_login.setText("Đang kết nối..."); self.btn_login.setEnabled(False)
        try: real_hwid = str(uuid.getnode())
        except: real_hwid = "unknown_hwid"

        try:
            res = requests.post(f"{SERVER_URL}/api/login", json={"username": user, "password": pwd, "hwid": real_hwid, "platform": "honggou"}, timeout=10)
            data = res.json()
            if data.get("status") == "success":
                self.settings.setValue("username", user); self.settings.setValue("password", pwd); self.settings.setValue("auth_token", data.get("token", "")) 
                self.login_success.emit(user, data.get("expiry", "Vô thời hạn"))
            else: QMessageBox.critical(self, "Lỗi", data.get("message", "Đăng nhập thất bại"))
        except Exception as e: QMessageBox.critical(self, "Lỗi mạng", f"Không thể kết nối đến Hệ thống:\n{e}")
        self.btn_login.setText("Đăng Nhập"); self.btn_login.setEnabled(True)

    def _handle_register(self):
        user = self.inp_user.text().strip(); pwd = self.inp_pass.text().strip()
        if not user or not pwd: QMessageBox.warning(self, "Lỗi", "Vui lòng nhập Tên đăng nhập và Mật khẩu bạn muốn tạo vào 2 ô trên, sau đó bấm Đăng Ký!"); return
        self.btn_register.setText("Đang xử lý..."); self.btn_register.setEnabled(False)
        try:
            res = requests.post(f"{SERVER_URL}/api/register", json={"username": user, "password": pwd, "zalo": "", "platform": "honggou"}, timeout=10)
            data = res.json()
            if data.get("status") == "success": QMessageBox.information(self, "Thành công", data.get("message", "Đăng ký thành công!"))
            else: QMessageBox.critical(self, "Lỗi", data.get("message", "Đăng ký thất bại"))
        except Exception as e: QMessageBox.critical(self, "Lỗi mạng", f"Không thể kết nối đến Hệ thống:\n{e}")
        self.btn_register.setText("Tạo Tài Khoản Mới"); self.btn_register.setEnabled(True)

# ==========================================
# CỬA SỔ CHÍNH
# ==========================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Hongguo Downloader Pro v{APP_VERSION}")
        self.resize(1050, 780); self.setStyleSheet("background-color: #0f172a;")
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.login_screen = LoginScreen()
        self.login_screen.login_success.connect(self.show_main_app)
        self.stack.addWidget(self.login_screen)

    def show_main_app(self, username, expiry):
        main_widget = QWidget(); main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(0, 0, 0, 0); main_layout.setSpacing(0)

        header = QWidget(); header.setFixedHeight(65)
        header.setStyleSheet("background-color: #1e293b; border-bottom: 1px solid #334155;")
        header_layout = QHBoxLayout(header); header_layout.setContentsMargins(25, 0, 25, 0)

        lbl_logo = QLabel("👑 Hongguo Downloader Pro")
        lbl_logo.setFont(QFont("Arial", 16, QFont.Weight.Bold)); lbl_logo.setStyleSheet("color: #38bdf8;")
        lbl_user_info = QLabel(f"👤 Khách hàng: <b>{username}</b>  |  ⏳ Hạn VIP: {expiry}")
        lbl_user_info.setStyleSheet("color: #cbd5e1; font-size: 14px;")

        self.lbl_balance = QLabel("💰 Số dư: --- đ")
        self.lbl_balance.setStyleSheet("color: #10b981; font-size: 14px; font-weight: bold;")

        btn_logout = QPushButton("🚪 Đăng Xuất")
        btn_logout.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_logout.setStyleSheet("QPushButton { padding: 8px 16px; background-color: #ef4444; color: white; border-radius: 6px; font-weight: bold; border: none; } QPushButton:hover { background-color: #dc2626; }")
        btn_logout.clicked.connect(self.logout)

        header_layout.addWidget(lbl_logo); header_layout.addStretch(); header_layout.addWidget(lbl_user_info)
        header_layout.addSpacing(20); header_layout.addWidget(self.lbl_balance); header_layout.addSpacing(20); header_layout.addWidget(btn_logout)

        self.updater = AutoUpdater(header_layout, parent_widget=self)
        main_layout.addWidget(header)

        self.honggou_tab = HonggouWidget(username)
        self.honggou_tab.balance_changed = self._update_balance_display
        main_layout.addWidget(self.honggou_tab)

        self.stack.addWidget(main_widget); self.stack.setCurrentWidget(main_widget)
        self._fetch_balance(username)
        self._hb_username = username
        self._hb_timer = QTimer(self); self._hb_timer.timeout.connect(self._send_heartbeat); self._hb_timer.start(20000); self._send_heartbeat()
    
    def _fetch_balance(self, username):
        try:
            token = QSettings("HongguoDownloader", "ClientApp").value("auth_token", "")
            res = requests.get(f"{SERVER_URL}/api/client/balance/{username}", headers={"Authorization": f"Bearer {token}"}, timeout=5)
            if res.status_code == 200:
                balance = res.json().get("balance", 0)
                self._update_balance_display(balance)
        except: pass

    def _update_balance_display(self, balance): self.lbl_balance.setText(f"💰 Số dư: {balance:,} đ".replace(",", "."))

    def _send_heartbeat(self):
        try:
            token = QSettings("HongguoDownloader", "ClientApp").value("auth_token", "")
            if not token: return
            payload = {"current_job_id": "", "series_id": "", "action": ""}
            if hasattr(self, 'honggou_tab'):
                tab = self.honggou_tab
                if tab.current_job_id: payload["current_job_id"] = str(tab.current_job_id)
                if tab.current_series_id: payload["series_id"] = str(tab.current_series_id)
                if tab.monitor_thread and tab.monitor_thread.isRunning(): payload["action"] = "Đang chờ tải phim"
                elif tab.current_episodes: payload["action"] = "Đang xem danh sách tập"
                else: payload["action"] = "Đang lướt kho phim"
            requests.post(f"{SERVER_URL}/api/client/heartbeat", json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=5)
        except: pass

    def logout(self):
        reply = QMessageBox.question(self, "Đăng xuất", "Bạn có chắc chắn muốn đăng xuất không?\n(Sẽ xóa thông tin tài khoản đã ghi nhớ)", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            if hasattr(self, '_hb_timer'): self._hb_timer.stop()
            settings = QSettings("HongguoDownloader", "ClientApp")
            settings.remove("username"); settings.remove("password")
            self.login_screen.inp_user.clear(); self.login_screen.inp_pass.clear() 
            if hasattr(self, 'honggou_tab') and self.honggou_tab.monitor_thread: self.honggou_tab.monitor_thread.stop()
            self.stack.setCurrentWidget(self.login_screen)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
