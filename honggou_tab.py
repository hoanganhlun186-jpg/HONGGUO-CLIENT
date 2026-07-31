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
import shutil
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, 
    QTableWidget, QTableWidgetItem, QLabel, QMessageBox, 
    QHeaderView, QListWidget, QListWidgetItem, QApplication, QMainWindow, QStackedWidget,
    QFileDialog, QProgressDialog, QProgressBar, QComboBox, QSpinBox,
    QCheckBox, QTextEdit, QTabWidget, QSplitter, QAbstractItemView,
    QTreeWidget, QTreeWidgetItem, QDoubleSpinBox, QScrollArea, QFrame, QSizePolicy, QDialog
)
from PyQt6.QtCore import Qt, QThread, QObject, pyqtSignal, QSize, QSettings, QTimer
from PyQt6.QtGui import QIcon, QPixmap, QImage, QFont, QColor

# === Cấu hình ffmpeg cho pydub (tránh WinError 2 khi lồng tiếng) ===
def _setup_ffmpeg_pydub():
    _here = os.path.dirname(os.path.abspath(__file__))
    cand = [os.path.join(_here, "ffmpeg.exe"), os.path.join(_here, "ffmpeg"),
            os.path.join(_here, "bin", "ffmpeg.exe")]
    fp = next((p for p in cand if os.path.exists(p)), None) or shutil.which("ffmpeg")
    try:
        from pydub import AudioSegment as _AS
        if fp:
            _AS.converter = fp
            _AS.ffmpeg = fp
            probe = os.path.join(os.path.dirname(fp), "ffprobe.exe")
            _AS.ffprobe = probe if os.path.exists(probe) else fp  # không có ffprobe thì trỏ tạm ffmpeg
            os.environ["PATH"] = os.path.dirname(fp) + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass
    return fp

FFMPEG_PATH = _setup_ffmpeg_pydub()

# Đồng bộ Gemini: tái dùng login + prompt presets từ tab dịch (nếu có)
try:
    from translate_tab import GoogleManualLoginThread, PROMPT_PRESETS, AUTH_FILE, GeminiTranslateThread
    _GEMINI_AVAILABLE = True
except Exception:
    GoogleManualLoginThread = None
    PROMPT_PRESETS = {}
    AUTH_FILE = "gemini_auth.json"
    GeminiTranslateThread = None
    _GEMINI_AVAILABLE = False

# ==========================================
# CẤU HÌNH SERVER & PHIÊN BẢN
# ==========================================
APP_VERSION = "1.0.29"
SERVER_URL = "http://163.61.182.119:8000"
MAX_CONCURRENT_DOWNLOADS = 3  

# ==========================================
# FFMPEG HELPER VÀ LUỒNG GỘP FILE (MERGE)
# ==========================================
def get_ffmpeg_path():
    """Tìm file ffmpeg trong thư mục chạy tool hoặc trong hệ thống"""
    if sys.platform == "win32":
        if os.path.exists("ffmpeg.exe"): 
            return os.path.abspath("ffmpeg.exe")
    else:
        if os.path.exists("ffmpeg"): 
            return os.path.abspath("ffmpeg")
    
    ffmpeg_system = shutil.which("ffmpeg")
    return ffmpeg_system if ffmpeg_system else None

class HonggouMergeThread(QThread):
    progress_msg = pyqtSignal(str)
    finished_signal = pyqtSignal()
    error_signal = pyqtSignal(str)

    def __init__(self, movie_folder, merge_tasks):
        super().__init__()
        self.movie_folder = movie_folder
        self.merge_tasks = merge_tasks 

    def run(self):
        ffmpeg_path = get_ffmpeg_path()
        if not ffmpeg_path:
            self.error_signal.emit("Không tìm thấy phần mềm FFmpeg để gộp file! Vui lòng tải ffmpeg.exe đặt cùng thư mục app.")
            return

        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW 

        total_tasks = len(self.merge_tasks)
        for i, task in enumerate(self.merge_tasks):
            out_name = task["output_name"]
            files_to_merge = task["files"]
            
            if len(files_to_merge) <= 1:
                continue 
                
            self.progress_msg.emit(f"⚡ Đang chuẩn bị định dạng file phần {i+1}/{total_tasks}...")
            
            # 1. Chuyển MP4 sang định dạng luồng TS tạm thời để ghép không bị vỡ hình
            temp_ts_files = []
            for j, fp in enumerate(files_to_merge):
                self.progress_msg.emit(f"⚙️ Xử lý chống vỡ hình tập {j+1}/{len(files_to_merge)} (Phần {i+1})...")
                ts_path = fp + ".ts"
                ts_cmd = [ffmpeg_path, '-y', '-i', fp, '-c', 'copy', '-f', 'mpegts', ts_path]
                try:
                    res_ts = subprocess.run(ts_cmd, startupinfo=startupinfo, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    if res_ts.returncode != 0:
                        # Thử lại với bitstream filter thủ công nếu bản FFmpeg cũ
                        ts_cmd_fallback = [ffmpeg_path, '-y', '-i', fp, '-c', 'copy', '-bsf:v', 'h264_mp4toannexb', '-f', 'mpegts', ts_path]
                        res_fallback = subprocess.run(ts_cmd_fallback, startupinfo=startupinfo, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                        if res_fallback.returncode != 0:
                             raise Exception(res_fallback.stderr.decode('utf-8', errors='ignore'))
                    temp_ts_files.append(ts_path)
                except Exception as e:
                    self.error_signal.emit(f"Lỗi xử lý file {os.path.basename(fp)}: {str(e)}")
                    # Dọn dẹp nếu lỗi
                    for t in temp_ts_files:
                        if os.path.exists(t): os.remove(t)
                    return

            self.progress_msg.emit(f"⚡ Đang ráp mạch phim phần {i+1}/{total_tasks}: {out_name}...")
            
            # 2. Tạo danh sách các file TS để nối
            list_txt_path = os.path.join(self.movie_folder, f"merge_list_{i}.txt")
            try:
                with open(list_txt_path, 'w', encoding='utf-8') as f:
                    for tp in temp_ts_files:
                        f.write(f"file '{tp.replace(os.sep, '/')}'\n")
            except Exception as e:
                self.error_signal.emit(f"Lỗi tạo file danh sách: {str(e)}")
                for t in temp_ts_files:
                    if os.path.exists(t): os.remove(t)
                return

            # 3. Nối các file TS và xuất ngược ra MP4
            final_output = os.path.join(self.movie_folder, out_name)
            # Lưu ý: cần filter -bsf:a aac_adtstoasc để tránh lỗi âm thanh khi từ TS sang MP4
            cmd = [ffmpeg_path, '-y', '-f', 'concat', '-safe', '0', '-i', list_txt_path, '-c', 'copy', '-bsf:a', 'aac_adtstoasc', final_output]

            try:
                result = subprocess.run(cmd, startupinfo=startupinfo, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                
                # 4. Dọn dẹp file danh sách và các file TS tạm
                if os.path.exists(list_txt_path): 
                    os.remove(list_txt_path)
                for t in temp_ts_files:
                    if os.path.exists(t): os.remove(t)
                
                # 5. Nếu gộp thành công, xóa các file MP4 tập lẻ đi
                if result.returncode == 0:
                    for fp in files_to_merge:
                        try:
                            if os.path.exists(fp): os.remove(fp)
                        except: pass
                else:
                    self.error_signal.emit(f"FFmpeg thất bại ở file {out_name}. Code: {result.returncode}")
                    return
            except Exception as e:
                self.error_signal.emit(str(e))
                return

        self.finished_signal.emit()

# ==========================================
# CÁC LUỒNG XỬ LÝ NỀN (THREADS)
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

class HistoryCoverThread(QThread):
    cover_ready = pyqtSignal(int, bytes)

    def __init__(self, jobs, covers_dir):
        super().__init__()
        self.jobs = jobs  
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
                    detail = data.get("loaderData", {}).get("detail_page", {}).get("seriesDetail", {})
                except json.JSONDecodeError: pass

            if not detail:
                self.error_signal.emit("Không bóc tách được dữ liệu. Vui lòng kiểm tra lại link.")
                return

            series_id = str(detail.get("series_id") or "")
            title = detail.get("series_name") or "Phim không rõ tên"
            cover_url = detail.get("series_cover") or ""

            total_episodes = 0
            right_text = detail.get("episode_right_text") or ""
            if right_text:
                num_match = re.search(r'(\d+)', right_text)
                if num_match: total_episodes = int(num_match.group(1))
            if total_episodes == 0:
                vid_list = detail.get("vid_list") or []
                if isinstance(vid_list, list) and len(vid_list) > 0: total_episodes = len(vid_list)
            if total_episodes == 0:
                total_episodes = int(detail.get("episode_cnt") or 0)

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
            else: 
                err_msg = res.text
                try: err_msg = res.json().get("detail", res.text)
                except: pass
                self.error_signal.emit(f"Máy chủ từ chối (Mã {res.status_code}):\n{err_msg}")

        except requests.exceptions.RequestException as e: 
            self.error_signal.emit(f"Lỗi kết nối web gốc: {str(e)}")
        except Exception as e: 
            self.error_signal.emit(f"Lỗi quét link: {str(e)}")

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
            time.sleep(5) 

    def stop(self): self.running = False

class StreamDownloadThread(QThread):
    link_ready_signal = pyqtSignal(dict)  
    error_signal = pyqtSignal(str)
    all_done_signal = pyqtSignal()

    def __init__(self, payload, auth_token):
        super().__init__()
        self.payload = payload
        self.auth_token = auth_token

    def run(self):
        try:
            url = f"{SERVER_URL}/api/client/stream_download_links"
            headers = {"Authorization": f"Bearer {self.auth_token}", "Content-Type": "application/json"}
            
            with requests.post(url, json=self.payload, headers=headers, timeout=180, stream=True) as resp:
                if resp.status_code != 200:
                    try: 
                        err_data = resp.json()
                        err_msg = err_data.get("message") or err_data.get("detail", "Lỗi thanh toán hoặc lấy link")
                    except: 
                        err_msg = f"Máy chủ trả về lỗi: {resp.status_code}"
                    self.error_signal.emit(err_msg)
                    return

                for line in resp.iter_lines():
                    if line:
                        decoded_line = line.decode('utf-8').strip()
                        if decoded_line.startswith("data: "):
                            json_str = decoded_line[6:] 
                            try:
                                data = json.loads(json_str)
                                
                                if data.get("error"):
                                    self.error_signal.emit(data.get("error"))
                                    return
                                    
                                if data.get("status") == "stream_finished":
                                    break
                                    
                                if ("url" in data or data.get("status") == "error") and "episode_number" in data:
                                    self.link_ready_signal.emit(data)
                            except json.JSONDecodeError:
                                continue
                                
            self.all_done_signal.emit()
            
        except Exception as e:
            self.error_signal.emit(f"Lỗi kết nối lấy link:\n{str(e)}")

# ==========================================
# THREAD CẦU CỨU LINK CHẾT TỰ ĐỘNG
# ==========================================
class RetryDeadLinkThread(QThread):
    new_link_signal = pyqtSignal(dict)
    
    def __init__(self, username, job_id, series_id, episode_number, auth_token):
        super().__init__()
        self.username = username
        self.job_id = job_id
        self.series_id = series_id
        self.episode_number = episode_number
        self.auth_token = auth_token
        
    def run(self):
        try:
            url = f"{SERVER_URL}/api/client/retry_dead_link"
            payload = {
                "username": self.username,
                "job_id": self.job_id,
                "series_id": self.series_id,
                "episode_number": self.episode_number
            }
            res = requests.post(url, json=payload, headers={"Authorization": f"Bearer {self.auth_token}"}, timeout=60)
            if res.status_code == 200:
                self.new_link_signal.emit(res.json())
            else:
                self.new_link_signal.emit({"status": "error", "episode_number": self.episode_number, "message": f"Server lỗi {res.status_code}"})
        except Exception as e:
            self.new_link_signal.emit({"status": "error", "episode_number": self.episode_number, "message": str(e)[:30]})

class SingleDriveDownloadThread(QThread):
    progress_signal = pyqtSignal(int, int, float)
    done_signal = pyqtSignal(int, str)
    error_signal = pyqtSignal(int, str)
    dead_link_signal = pyqtSignal(int)

    def __init__(self, ep_data, save_folder, auth_token, session):
        super().__init__()
        self.ep_data = ep_data
        self.save_folder = save_folder
        self.auth_token = auth_token
        self.session = session  

    def run(self):
        ep_num = self.ep_data.get("episode_number")
        try:
            ep_int = int(ep_num)
            file_name = self.ep_data.get("file_name", f"Tap_{ep_int:02d}.mp4")
        except:
            file_name = self.ep_data.get("file_name", f"Tap_{ep_num}.mp4")
            
        url = self.ep_data.get("drive_link")
        
        if not url or "error" in url.lower() or "status" in url.lower():
            self.dead_link_signal.emit(ep_num)
            return
            
        file_path = os.path.join(self.save_folder, file_name)
        part_path = file_path + ".part"  
        
        max_retries = 3
        for attempt in range(max_retries):
            if os.path.exists(file_path) and os.path.getsize(file_path) > 1024:
                self.done_signal.emit(ep_num, file_path)
                return

            try:
                if attempt > 0:
                    self.progress_signal.emit(ep_num, -2, float(attempt))
                    time.sleep(2)  
                
                self.progress_signal.emit(ep_num, 0, 0.0)
                
                resp = self.session.get(url, stream=True, timeout=30)
                resp.raise_for_status()
                
                content_type = resp.headers.get('Content-Type', '').lower()
                if 'video' not in content_type and 'application/octet-stream' not in content_type:
                    raise Exception(f"Bị chặn/Hết hạn (Content-Type: {content_type})")
                
                total = int(resp.headers.get('content-length', 0))
                downloaded = 0
                start_time = time.time()
                
                with open(part_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                pct = int(downloaded * 100 / total)
                                elapsed = time.time() - start_time
                                speed = (downloaded / 1024 / 1024) / elapsed if elapsed > 0 else 0
                                self.progress_signal.emit(ep_num, pct, speed)
                
                if os.path.exists(part_path):
                    os.rename(part_path, file_path)
                    self.done_signal.emit(ep_num, file_path)
                    return
                else:
                    raise Exception("Không ghi được file hệ thống")
                    
            except Exception as e:
                if os.path.exists(part_path):
                    try: os.remove(part_path)
                    except: pass
                
                if attempt == max_retries - 1:
                    self.dead_link_signal.emit(ep_num)
                    return

class DriveDownloadManager(QObject):
    progress_signal = pyqtSignal(int, int, float)  
    done_signal = pyqtSignal(int, str)              
    error_signal = pyqtSignal(int, str)             
    all_done_signal = pyqtSignal(int)               
    dead_link_signal = pyqtSignal(int)

    def __init__(self, episodes, save_folder, auth_token, parent=None, max_concurrent=None, expected_total=0):
        super().__init__(parent)
        self._pending = list(episodes)
        self.save_folder = save_folder
        self.auth_token = auth_token
        self._workers = []         
        self._success_count = 0
        self._finished_count = 0
        self.expected_total = expected_total  
        self.max_concurrent = max_concurrent or MAX_CONCURRENT_DOWNLOADS
        
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://hongguoduanju.com/",
            "Accept": "*/*",
            "Connection": "keep-alive"
        })

    def start(self):
        for _ in range(min(self.max_concurrent, len(self._pending))):
            self._launch_next()

    def add_and_run_episode(self, ep_data):
        self._pending.append(ep_data)
        if len([w for w in self._workers if w.isRunning()]) < self.max_concurrent:
            self._launch_next()

    def _launch_next(self):
        if not self._pending: return
        ep_data = self._pending.pop(0)
        worker = SingleDriveDownloadThread(ep_data, self.save_folder, self.auth_token, self.session)
        worker.progress_signal.connect(self.progress_signal.emit)
        worker.done_signal.connect(self._on_worker_done)
        worker.error_signal.connect(self._on_worker_error)
        worker.dead_link_signal.connect(self._on_worker_dead_link)
        self._workers.append(worker)
        worker.start()

    def _check_all_done(self):
        if self._finished_count >= self.expected_total:
            self.all_done_signal.emit(self._success_count)

    def _on_worker_done(self, ep_num, file_path):
        self._success_count += 1
        self._finished_count += 1
        self.done_signal.emit(ep_num, file_path)
        self._launch_next()
        self._check_all_done()

    def _on_worker_error(self, ep_num, error_msg):
        self._finished_count += 1
        self.error_signal.emit(ep_num, error_msg)
        self._launch_next()
        self._check_all_done()

    def _on_worker_dead_link(self, ep_num):
        self.dead_link_signal.emit(ep_num)
        self._launch_next() 

# ==========================================
# GIAO DIỆN CHÍNH: HONGGOU WIDGET
# ==========================================

# ==========================================
# STT BATCH THREAD — Tách sub từ file video
# ==========================================
class SttBatchThread(QThread):
    progress_signal = pyqtSignal(str)       # log message
    finished_signal = pyqtSignal(int, int)  # success, failed

    def __init__(self, file_paths, src_lang="zh-CN", out_lang="vi-VN", use_trans=True):
        super().__init__()
        self.file_paths = file_paths
        self.src_lang   = src_lang
        self.out_lang   = out_lang
        # KHÔNG dùng dịch của CapCut nữa -> chỉ tách sub tiếng GỐC.
        # Việc dịch sang tiếng Việt do Gemini đảm nhận ở bước sau.
        self.use_trans  = False
        self._stop      = False

    def stop(self):
        self._stop = True

    @staticmethod
    def _ms_to_srt(ms):
        ms = int(ms)
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms,    60_000)
        s, ms = divmod(ms,     1_000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def run(self):
        try:
            from capcut_tts_api import CapCutClient
            client = CapCutClient()
        except Exception as e:
            self.progress_signal.emit(f"❌ Không khởi tạo được CapCutClient: {e}")
            self.finished_signal.emit(0, len(self.file_paths))
            return

        SUCCEED = {"succeed", "success", "completed", "done"}
        FAIL    = {"failed",  "error",   "fail"}
        ok = failed = 0

        for i, fp in enumerate(self.file_paths):
            if self._stop:
                self.progress_signal.emit("🛑 Đã dừng bởi người dùng.")
                break
            if not os.path.exists(fp):
                self.progress_signal.emit(f"[{i+1}] ⏭ Bỏ qua (chưa có): {os.path.basename(fp)}")
                failed += 1; continue

            bname = os.path.basename(fp)
            out_srt = os.path.splitext(fp)[0] + ".srt"
            out_txt = os.path.splitext(fp)[0] + ".txt"

            self.progress_signal.emit(f"[{i+1}/{len(self.file_paths)}] ⬆️ Upload: {bname} ...")
            try:
                upload = client.upload_audio(fp)
                self.progress_signal.emit(f"[{i+1}] ✅ Upload xong ({upload.duration_ms}ms) · Đang nhận dạng...")

                stt_res = client.create_stt_task(
                    audio_vid=upload.vid, audio_md5=upload.md5,
                    duration_ms=upload.duration_ms or 10000,
                    language=self.src_lang,
                    translation_language=self.out_lang,
                    use_translation=self.use_trans)

                tasks = (stt_res.get("data") or {}).get("tasks") or []
                if not tasks:
                    raise RuntimeError(f"API không trả về task. Resp: {stt_res}")
                task_id, token = tasks[0]["id"], tasks[0]["token"]

                result = None; status = ""
                for attempt in range(90):
                    if self._stop: break
                    time.sleep(2)
                    q = client.query_stt_task(task_id, token)
                    qt = (q.get("data") or {}).get("tasks") or []
                    if not qt: continue
                    status = qt[0].get("status", "")
                    prog   = qt[0].get("progress", 0)
                    self.progress_signal.emit(f"[{i+1}] ⏳ {status} | {prog}%")
                    if status in SUCCEED: result = q; break
                    elif status in FAIL:  raise RuntimeError(f"STT thất bại: {status}")

                if result is None:
                    raise RuntimeError("Timeout hoặc bị dừng")

                subs = client.extract_subtitles(result)
                srt_lines = []
                for j, u in enumerate(subs.utterances, 1):
                    t = (u.translated_text if (self.use_trans
                         and hasattr(u, "translated_text") and u.translated_text)
                         else u.text)
                    srt_lines.append(
                        f"{j}\n{self._ms_to_srt(u.start_time)} --> {self._ms_to_srt(u.end_time)}\n{t}\n"
                    )
                with open(out_srt, "w", encoding="utf-8") as f: f.write("\n".join(srt_lines))
                with open(out_txt, "w", encoding="utf-8") as f: f.write(subs.full_text)
                self.progress_signal.emit(f"[{i+1}] 💾 Đã lưu: {os.path.basename(out_srt)}")
                ok += 1

            except Exception as e:
                self.progress_signal.emit(f"[{i+1}] ❌ Lỗi {bname}: {str(e)[:100]}")
                failed += 1

        self.finished_signal.emit(ok, failed)


# ==========================================
# DUB THREAD — Lồng tiếng từ SRT vào video
# ==========================================
class DubThread(QThread):
    progress_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(int, int)   # success, failed

    def __init__(self, tasks, voice_type="BV074_streaming", rate="1.0", mute_original=True):
        """
        tasks = list of {"video": path, "srt": path}
        mute_original: True = tắt hẳn tiếng gốc (chỉ còn tiếng lồng)
        """
        super().__init__()
        self.tasks      = tasks
        self.voice_type = voice_type
        self.rate       = rate
        self.mute_original = mute_original
        self._stop      = False

    def stop(self): self._stop = True

    @staticmethod
    def _ms_to_srt(ms):
        ms = int(ms)
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms,    60_000)
        s, ms = divmod(ms,     1_000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    @staticmethod
    def _parse_srt(srt_path):
        """Trả về list of (start_ms, end_ms, text)"""
        import re
        entries = []
        with open(srt_path, encoding="utf-8", errors="ignore") as f:
            raw = f.read()
        blocks = re.split(r"\n\s*\n", raw.strip())
        for block in blocks:
            lines = block.strip().splitlines()
            if len(lines) < 3: continue
            try:
                time_line = lines[1]
                m = re.match(
                    r"(\d+):(\d+):(\d+)[,\.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,\.](\d+)",
                    time_line)
                if not m: continue
                g = [int(x) for x in m.groups()]
                start_ms = g[0]*3600000 + g[1]*60000 + g[2]*1000 + g[3]
                end_ms   = g[4]*3600000 + g[5]*60000 + g[6]*1000 + g[7]
                text = " ".join(lines[2:]).strip()
                if text: entries.append((start_ms, end_ms, text))
            except Exception: pass
        return entries

    def run(self):
        import urllib.request, shutil, json, time as _time
        from pydub import AudioSegment
        from pydub.effects import speedup

        try:
            from capcut_tts_api import CapCutClient
            client = CapCutClient()
        except Exception as e:
            self.progress_signal.emit(f"❌ Không khởi tạo được CapCutClient: {e}")
            self.finished_signal.emit(0, len(self.tasks)); return

        ffmpeg = get_ffmpeg_path()
        if not ffmpeg:
            self.progress_signal.emit("❌ Không tìm thấy ffmpeg! Đặt ffmpeg.exe cùng thư mục.")
            self.finished_signal.emit(0, len(self.tasks)); return

        ok = failed = 0
        SUCCEED = {"succeed","success","completed","done","finish"}
        FAIL    = {"failed","error","fail"}
        is_neural = "Neural" in self.voice_type

        for idx, task in enumerate(self.tasks):
            if self._stop:
                self.progress_signal.emit("🛑 Đã dừng."); break

            video_path = task["video"]
            srt_path   = task["srt"]
            if not os.path.exists(video_path) or not os.path.exists(srt_path):
                self.progress_signal.emit(f"[{idx+1}] ⏭ Bỏ qua: thiếu file video hoặc SRT")
                failed += 1; continue

            bname = os.path.basename(video_path)
            self.progress_signal.emit(f"[{idx+1}/{len(self.tasks)}] 🎙 Bắt đầu lồng tiếng: {bname}")

            entries = self._parse_srt(srt_path)
            if not entries:
                self.progress_signal.emit(f"[{idx+1}] ⚠️ SRT rỗng: {os.path.basename(srt_path)}")
                failed += 1; continue

            temp_dir = tempfile.mkdtemp(prefix="dub_")
            try:
                # Tính tổng duration từ SRT cuối
                total_ms = entries[-1][1] + 500

                # TTS từng dòng
                combined = AudioSegment.silent(duration=total_ms)
                for i, (start_ms, end_ms, text) in enumerate(entries):
                    if self._stop: break
                    if not text.strip(): continue
                    target_dur = end_ms - start_ms
                    seg_path = os.path.join(temp_dir, f"seg_{i:04d}.mp3")
                    try:
                        if is_neural:
                            import asyncio, edge_tts
                            r = float(self.rate)
                            pct = int((r-1.0)*100)
                            rate_str = f"+{pct}%" if pct>=0 else f"{pct}%"
                            async def _run_edge():
                                comm = edge_tts.Communicate(text=text, voice=self.voice_type, rate=rate_str)
                                await comm.save(seg_path)
                            asyncio.run(_run_edge())
                        else:
                            create = client.create_tts_task(texts=text, voice=self.voice_type, rate=self.rate)
                            tasks_r = (create.get("data") or {}).get("tasks") or []
                            if not tasks_r: raise RuntimeError("Không có TTS task")
                            tid, tok = tasks_r[0]["id"], tasks_r[0]["token"]
                            url = None
                            for _ in range(60):
                                _time.sleep(2)
                                q = client.query_tts_task(tid, tok)
                                qt = (q.get("data") or {}).get("tasks") or []
                                if not qt: continue
                                st = qt[0].get("status","")
                                if st in SUCCEED:
                                    raw = qt[0].get("payload","{}")
                                    pl = json.loads(raw) if isinstance(raw,str) else raw
                                    subs2 = pl.get("audio_subtitles") or []
                                    if subs2: url = subs2[0].get("speech_url","")
                                    if not url:
                                        for k in ("audio_list","url_list"):
                                            for u in (pl.get(k) or []):
                                                url = (u.get("url") or u.get("speech_url")) if isinstance(u,dict) else str(u)
                                                if url: break
                                    break
                                elif st in FAIL: raise RuntimeError(f"TTS fail: {st}")
                            if not url: raise RuntimeError("Timeout TTS")
                            urllib.request.urlretrieve(url, seg_path)

                        # Chỉnh tốc độ cho khớp duration SRT.
                        # Decode qua ffmpeg trực tiếp (from_file thay from_mp3) để bớt phụ thuộc ffprobe.
                        try:
                            audio_seg = AudioSegment.from_file(seg_path, format="mp3")
                        except Exception:
                            # ffprobe/from_mp3 lỗi -> ép ffmpeg convert sang wav rồi đọc lại
                            wav_path = seg_path + ".wav"
                            _flags = 0x08000000 if os.name == "nt" else 0
                            subprocess.run([FFMPEG_PATH or "ffmpeg", "-y", "-i", seg_path, wav_path],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_flags)
                            audio_seg = AudioSegment.from_file(wav_path, format="wav")
                        cur = len(audio_seg)
                        if cur > target_dur and target_dur > 0:
                            factor = min(cur/target_dur, 2.5)
                            try:
                                audio_seg = speedup(audio_seg, playback_speed=factor)
                            except Exception:
                                pass  # không tăng tốc được thì để nguyên, vẫn có tiếng
                        elif cur < target_dur:
                            audio_seg += AudioSegment.silent(duration=target_dur-cur)

                        # Đặt vào đúng vị trí timeline
                        combined = combined.overlay(audio_seg, position=start_ms)
                        self.progress_signal.emit(f"[{idx+1}] 🔊 Dòng {i+1}/{len(entries)} xong")
                    except Exception as seg_e:
                        self.progress_signal.emit(f"[{idx+1}] ⚠️ Dòng {i+1}: {str(seg_e)[:60]}")

                if self._stop: break

                # Xuất file audio lồng tiếng
                dub_audio = os.path.join(temp_dir, "dub_final.mp3")
                combined.export(dub_audio, format="mp3")
                self.progress_signal.emit(f"[{idx+1}] 🎬 Đang mix vào video bằng ffmpeg...")

                # ffmpeg mix: giữ video gốc, thay/mix audio
                out_video = os.path.splitext(video_path)[0] + "_dubbed.mp4"
                import subprocess as _sp, sys as _sys
                si = None
                if _sys.platform == "win32":
                    si = _sp.STARTUPINFO()
                    si.dwFlags |= _sp.STARTF_USESHOWWINDOW

                # Tùy chọn: tắt HẲN tiếng gốc (chỉ còn tiếng lồng) hoặc mix nhẹ tiếng gốc làm nền
                if getattr(self, "mute_original", True):
                    # CHỈ lấy audio lồng tiếng, bỏ hoàn toàn tiếng gốc
                    cmd = [ffmpeg, "-y",
                           "-i", video_path,
                           "-i", dub_audio,
                           "-map", "0:v", "-map", "1:a",
                           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                           "-shortest",
                           out_video]
                else:
                    # Giữ 15% tiếng gốc làm nền + tiếng lồng
                    cmd = [ffmpeg, "-y",
                           "-i", video_path,
                           "-i", dub_audio,
                           "-filter_complex",
                           "[0:a]volume=0.15[orig];[1:a]volume=1.0[dub];[orig][dub]amix=inputs=2:duration=longest[aout]",
                           "-map", "0:v", "-map", "[aout]",
                           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                           out_video]
                res = _sp.run(cmd, startupinfo=si, stdout=_sp.DEVNULL, stderr=_sp.PIPE)
                if res.returncode == 0:
                    self.progress_signal.emit(f"[{idx+1}] ✅ Xong! → {os.path.basename(out_video)}")
                    ok += 1
                else:
                    err = res.stderr.decode("utf-8", errors="ignore")[-200:]
                    raise RuntimeError(f"ffmpeg lỗi: {err}")

            except Exception as e:
                self.progress_signal.emit(f"[{idx+1}] ❌ Lỗi {bname}: {str(e)[:120]}")
                failed += 1
            finally:
                try: shutil.rmtree(temp_dir)
                except: pass

        self.finished_signal.emit(ok, failed)

class HonggouWidget(QWidget):
    balance_changed = pyqtSignal(int)

    def __init__(self, username, parent=None):
        super().__init__(parent)
        self.username = username
        self.settings = QSettings("HongguoDownloader", "ClientApp")
        self.auth_token = self.settings.value("auth_token", "")
        
        self.current_series_id = ""
        self.current_job_id = ""
        self.current_episodes = []
        self.current_title = ""
        self.current_cover_url = ""
        self.monitor_thread = None
        self._active_threads = []
        
        default_dl = os.path.join(os.path.expanduser("~"), "Downloads")
        self.save_folder = self.settings.value(f"download_folder_{self.username}", default_dl)
        if not os.path.exists(self.save_folder):
            try: os.makedirs(self.save_folder)
            except: self.save_folder = default_dl
            
        self._setup_ui()
        self.load_hot_movies_shelf()

    def _keep_thread_alive(self, thread):
        self._active_threads.append(thread)
        thread.finished.connect(lambda: self._active_threads.remove(thread) if thread in self._active_threads else None)

    def _get_covers_dir(self):
        d = os.path.join(os.path.expanduser("~"), ".hongguo_covers")
        os.makedirs(d, exist_ok=True)
        return d
        
    def get_covers_dir(self):
        return self._get_covers_dir()

    def _get_history_file(self):
        return os.path.join(os.path.expanduser("~"), f".hongguo_scan_history_{self.username}.json")

    def _load_history(self):
        f = self._get_history_file()
        items = []
        if os.path.exists(f):
            try:
                with open(f, 'r', encoding='utf-8') as fh: items = json.load(fh)
            except Exception: items = []
        now = time.time()
        # Chỉ xóa các mục chưa tải (không có downloaded=True) sau 30 phút; mục đã tải giữ lại vĩnh viễn
        alive = [it for it in items if it.get('downloaded') or (now - it.get('ts', 0) < 1800)]
        if len(alive) != len(items):
            try:
                with open(f, 'w', encoding='utf-8') as fh: json.dump(alive, fh, ensure_ascii=False, indent=2)
            except Exception: pass
        return alive

    def _save_to_history(self, series_id, title, cover_url, total_eps=0, cover_bytes=None, downloaded=False):
        if not series_id: return
        series_id = str(series_id)
        if series_id in getattr(self, '_cached_history_ids', set()): return
        try:
            cover_path = os.path.join(self.get_covers_dir(), f"{series_id}.img")
            if cover_bytes and not os.path.exists(cover_path):
                with open(cover_path, 'wb') as f: f.write(cover_bytes)
        except Exception: pass
        
        items = self._load_history()
        items = [it for it in items if str(it.get('series_id')) != series_id]
        items.insert(0, {
            "series_id": series_id,
            "title": title if title else "Phim không rõ tên",
            "cover_url": cover_url,
            "total_episodes": total_eps,
            "ts": time.time(),
            "timestamp": datetime.now().strftime("%H:%M %d/%m/%Y"),
            "downloaded": downloaded
        })
        items = items[:20]
        try:
            with open(self._get_history_file(), 'w', encoding='utf-8') as fh: json.dump(items, fh, ensure_ascii=False, indent=2)
        except Exception: pass
        try: self._render_history_sidebar()
        except Exception: pass

    def _remove_from_history(self, series_id):
        try:
            items = self._load_history()
            new_items = [it for it in items if str(it.get('series_id')) != str(series_id)]
            if len(new_items) != len(items):
                with open(self._get_history_file(), 'w', encoding='utf-8') as fh: json.dump(new_items, fh, ensure_ascii=False, indent=2)
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

        self.lbl_downloaded_badge = QLabel("✅ BỘ NÀY ĐÃ TẢI RỒI")
        self.lbl_downloaded_badge.setStyleSheet("QLabel { background-color: #064e3b; color: #34d399; font-weight: bold; font-size: 13px; padding: 8px 14px; border-radius: 8px; border: 1px solid #10b981; }")
        self.lbl_downloaded_badge.hide()
        btn_back_layout.addWidget(self.lbl_downloaded_badge)

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
        self.table.setColumnWidth(0, 90); self.table.setColumnWidth(1, 50); self.table.setColumnWidth(3, 170)
        self.table.verticalHeader().setVisible(False); self.table.setShowGrid(False); self.table.setAlternatingRowColors(True); self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus); self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers); self.table.setIconSize(QSize(40, 50))  
        self.table.setStyleSheet("QTableWidget { background-color: #111827; alternate-background-color: #1f2937; color: #e2e8f0; border: 1px solid #374151; border-radius: 8px; outline: none; margin-top: 10px; font-size: 13px; } QHeaderView::section { background-color: #0f172a; color: #94a3b8; padding: 12px; font-weight: bold; border: none; border-bottom: 1px solid #374151; } QTableWidget::item { padding: 6px; border-bottom: 1px solid transparent; } QTableWidget::item:hover { background-color: #334155; } QTableWidget::indicator { width: 18px; height: 18px; border: 2px solid #475569; border-radius: 4px; } QTableWidget::indicator:checked { background-color: #10b981; border-color: #10b981; } QScrollBar:vertical { border: none; background: #111827; width: 8px; margin: 0px; } QScrollBar::handle:vertical { background: #374151; border-radius: 4px; min-height: 20px; } QScrollBar::handle:vertical:hover { background: #4b5563; }")
        detail_layout.addWidget(self.table)

        bottom_layout = QHBoxLayout()

        # ------------------- TÙY CHỌN GỘP FILE -------------------
        merge_layout = QVBoxLayout()
        self.merge_mode_combo = QComboBox()
        self.merge_mode_combo.addItems([
            "Không gộp (Từng tập rời)",
            "Gộp tất cả thành 1 file",
            "Gộp theo nhóm (Tách nhiều phần)"
        ])
        self.merge_mode_combo.setStyleSheet("QComboBox { background: #1f2937; color: #f8fafc; border: 1px solid #374151; border-radius: 6px; padding: 6px; font-weight: bold; }")

        self.chunk_spinbox = QSpinBox()
        self.chunk_spinbox.setRange(2, 200)
        self.chunk_spinbox.setValue(10)
        self.chunk_spinbox.setPrefix("Gộp ")
        self.chunk_spinbox.setSuffix(" tập / 1 file")
        self.chunk_spinbox.setStyleSheet("QSpinBox { background: #1f2937; color: #f8fafc; border: 1px solid #374151; border-radius: 6px; padding: 6px; font-weight: bold; }")
        self.chunk_spinbox.hide()

        self.merge_mode_combo.currentIndexChanged.connect(
            lambda idx: self.chunk_spinbox.show() if idx == 2 else self.chunk_spinbox.hide()
        )

        merge_layout.addWidget(self.merge_mode_combo)
        merge_layout.addWidget(self.chunk_spinbox)

        self.btn_select_all = QPushButton("☑ Chọn / Bỏ chọn tất cả")
        self.btn_select_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_select_all.setStyleSheet("QPushButton { padding: 14px; background-color: #4b5563; color: white; border-radius: 8px; font-weight: bold; font-size: 14px; margin-top: 10px; border: none;} QPushButton:hover { background-color: #64748b; }")
        self.btn_select_all.clicked.connect(self._toggle_select_all)
        self.btn_download = QPushButton("📥 Tải đã chọn")
        self.btn_download.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_download.setStyleSheet("QPushButton { padding: 14px 30px; background-color: #10b981; color: white; border-radius: 8px; font-weight: bold; font-size: 15px; margin-top: 10px; border: none; } QPushButton:hover { background-color: #059669; } QPushButton:disabled { background-color: #374151; color: #64748b; }")
        self.btn_download.setEnabled(False)
        self.btn_download.clicked.connect(self._download_selected)
        bottom_layout.addLayout(merge_layout)
        bottom_layout.addWidget(self.btn_select_all)
        bottom_layout.addWidget(self.btn_download)

        # ─── STT / Tách sub controls ───────────────────────────────────
        stt_ctrl = QHBoxLayout(); stt_ctrl.setSpacing(8)
        self.chk_auto_stt = QCheckBox("🔤 Tự động tách sub sau khi tải")
        self.chk_auto_stt.setStyleSheet("""
            QCheckBox { color: #f1f5f9; font-size: 12px; padding: 4px; font-weight: bold; }
            QCheckBox::indicator { width: 18px; height: 18px; border: 2px solid #7c3aed;
                border-radius: 4px; background: #1e293b; }
            QCheckBox::indicator:checked { background: #7c3aed; border-color: #7c3aed;
                image: none; }
            QCheckBox::indicator:checked::after { color: white; }
        """)
        stt_ctrl.addWidget(self.chk_auto_stt)

        stt_ctrl.addWidget(QLabel("  Tiếng gốc:"))
        self.cmb_stt_src = QComboBox()
        self.cmb_stt_src.addItems(["zh-CN","en-US","vi-VN","ja-JP","ko-KR","fr-FR"])
        self.cmb_stt_src.setToolTip("Ngôn ngữ gốc trong phim")
        self.cmb_stt_src.setStyleSheet("QComboBox { background:#1f2937; color:#f8fafc; border:1px solid #374151; border-radius:6px; padding:4px 8px; } QComboBox QAbstractItemView { background:#1e293b; color:#f8fafc; }")
        stt_ctrl.addWidget(self.cmb_stt_src)

        stt_ctrl.addWidget(QLabel("→ Dịch sang:"))
        self.cmb_stt_out = QComboBox()
        self.cmb_stt_out.addItems(["vi-VN","en-US","zh-CN","ja-JP","ko-KR"])
        self.cmb_stt_out.setToolTip("Ngôn ngữ phụ đề cần dịch sang")
        self.cmb_stt_out.setStyleSheet("QComboBox { background:#1f2937; color:#f8fafc; border:1px solid #374151; border-radius:6px; padding:4px 8px; } QComboBox QAbstractItemView { background:#1e293b; color:#f8fafc; }")
        stt_ctrl.addWidget(self.cmb_stt_out)

        self.btn_stt_now = QPushButton("🔤 Tách sub ngay")
        self.btn_stt_now.setEnabled(False)
        self.btn_stt_now.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stt_now.setStyleSheet("QPushButton { padding: 8px 18px; background-color: #7c3aed; color: white; border-radius: 8px; font-weight: bold; font-size: 13px; border: none; } QPushButton:hover { background-color: #6d28d9; } QPushButton:disabled { background-color: #374151; color: #64748b; }")
        self.btn_stt_now.clicked.connect(self._run_stt_on_downloaded)
        stt_ctrl.addWidget(self.btn_stt_now)
        stt_ctrl.addStretch()
        detail_layout.addLayout(stt_ctrl)

        # ─── Lồng tiếng controls ──────────────────────────────────────
        dub_ctrl = QHBoxLayout(); dub_ctrl.setSpacing(8)
        self.chk_auto_dub = QCheckBox("🎙 Lồng tiếng sau khi tách sub")
        self.chk_auto_dub.setStyleSheet("""
            QCheckBox { color: #fde68a; font-size: 12px; padding: 4px; font-weight: bold; }
            QCheckBox::indicator { width: 18px; height: 18px; border: 2px solid #f59e0b;
                border-radius: 4px; background: #1e293b; }
            QCheckBox::indicator:checked { background: #f59e0b; border-color: #f59e0b; }
        """)
        dub_ctrl.addWidget(self.chk_auto_dub)

        dub_ctrl.addWidget(QLabel("  Giọng:"))
        self.cmb_dub_voice = QComboBox()
        self._load_dub_voices()
        self.cmb_dub_voice.setToolTip("Giọng lồng tiếng (đọc từ Voice.json)")
        self.cmb_dub_voice.setStyleSheet("QComboBox { background:#1f2937; color:#fde68a; border:1px solid #f59e0b; border-radius:6px; padding:4px 8px; } QComboBox QAbstractItemView { background:#1e293b; color:#fde68a; }")
        dub_ctrl.addWidget(self.cmb_dub_voice)

        dub_ctrl.addWidget(QLabel("Tốc độ:"))
        self.spn_dub_rate = QDoubleSpinBox()
        self.spn_dub_rate.setRange(0.5, 2.0); self.spn_dub_rate.setSingleStep(0.1)
        self.spn_dub_rate.setValue(1.0); self.spn_dub_rate.setDecimals(1)
        self.spn_dub_rate.setFixedWidth(65)
        self.spn_dub_rate.setStyleSheet("QDoubleSpinBox { background:#1f2937; color:#fde68a; border:1px solid #f59e0b; border-radius:6px; padding:3px; }")
        dub_ctrl.addWidget(self.spn_dub_rate)

        self.chk_mute_original = QCheckBox("🔇 Tắt tiếng gốc")
        self.chk_mute_original.setChecked(True)
        self.chk_mute_original.setToolTip("Bật = chỉ còn tiếng lồng (bỏ hẳn tiếng Trung gốc)")
        self.chk_mute_original.setStyleSheet("""
            QCheckBox { color:#fde68a; font-weight:bold; font-size:13px; padding:2px; }
            QCheckBox::indicator { width:18px; height:18px; border:2px solid #f59e0b; border-radius:4px; background:#1f2937; }
            QCheckBox::indicator:checked { background:#f59e0b; }
        """)
        dub_ctrl.addWidget(self.chk_mute_original)

        self.btn_dub_now = QPushButton("🎙 Lồng tiếng ngay")
        self.btn_dub_now.setEnabled(False)
        self.btn_dub_now.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_dub_now.setStyleSheet("QPushButton { padding: 8px 18px; background-color: #b45309; color: white; border-radius: 8px; font-weight: bold; font-size: 13px; border: none; } QPushButton:hover { background-color: #92400e; } QPushButton:disabled { background-color: #374151; color: #64748b; }")
        self.btn_dub_now.clicked.connect(self._run_dub_on_downloaded)
        dub_ctrl.addWidget(self.btn_dub_now)
        dub_ctrl.addStretch()
        detail_layout.addLayout(dub_ctrl)
        # ─────────────────────────────────────────────────────────────

        self.txt_stt_log = QTextEdit()
        self.txt_stt_log.setReadOnly(True); self.txt_stt_log.setFixedHeight(110)
        self.txt_stt_log.setStyleSheet("QTextEdit { background: #0a0c14; color: #a3e635; font-family: Consolas; font-size: 9pt; border: 1px solid #374151; border-radius: 6px; padding: 4px; }")
        self.txt_stt_log.hide()
        detail_layout.addWidget(self.txt_stt_log)
        # ───────────────────────────────────────────────────────────────
        detail_layout.addLayout(bottom_layout)
        self.content_stack.addWidget(self.page_detail)
        self._render_history_sidebar()

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
        if not hasattr(self, 'history_list'): return
        history = self._load_history()
        downloaded_ids = {str(h.get('series_id')) for h in history}
        scanned = [s for s in self._load_history() if str(s.get('series_id')) not in downloaded_ids]
        
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
            url = item.data(Qt.ItemDataRole.UserRole) or ""
            sid = url.split("series_id=")[-1] if "series_id=" in url else ""
            if sid and hasattr(self, '_sidebar_icon_cache'): self._sidebar_icon_cache[sid] = icon

    def _on_history_item_clicked(self, item):
        url = item.data(Qt.ItemDataRole.UserRole)
        if not url: return
        if getattr(self, 'scan_thread', None) and self.scan_thread.isRunning(): return
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
        if getattr(self, 'monitor_thread', None): self.monitor_thread.stop()
        self.url_input.clear()
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

        if getattr(self, 'monitor_thread', None): self.monitor_thread.stop()
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
        
        if hasattr(self, 'lbl_downloaded_badge'):
            if str(self.current_series_id) in getattr(self, '_cached_history_ids', set()): self.lbl_downloaded_badge.show()
            else: self.lbl_downloaded_badge.hide()

        try:
            self._save_to_history(self.current_series_id, self.current_title, self.current_cover_url,
                                       total_eps=total_eps or len(self.current_episodes),
                                       cover_bytes=self.current_cover_bytes)
        except Exception: pass

        if status == "cache_hit":
            self.lbl_status.setText(f"✅ Đã tìm thấy phim trong bộ nhớ tạm! (Tổng: {total_eps} tập).")
            self.btn_download.setEnabled(True)
        else:
            self.lbl_status.setText(f"✅ Quét thành công (Tổng: {total_eps} tập). Bạn có thể chọn tập và tải ngay!")
            self.btn_download.setEnabled(True)

        self._render_table(total_eps, self.current_episodes)

        if status not in ["cache_hit", "completed"] and self.current_job_id:
            self.monitor_thread = JobStatusMonitorThread(self.current_job_id, self.auth_token)
            self._keep_thread_alive(self.monitor_thread)
            self.monitor_thread.update_signal.connect(self._on_monitor_update)
            self.monitor_thread.start()

    def _on_monitor_update(self, data):
        status = data.get("status")
        total_eps = data.get("total_episodes", self.table.rowCount())
        # Chốt chặn bảo vệ: Nếu Server trả về 0 tập do lỗi, giữ nguyên số hàng hiện tại
        if total_eps == 0 and self.table.rowCount() > 0:
            total_eps = self.table.rowCount()
            
        self.current_episodes = data.get("episodes", [])
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
            
            # Ép tên file hiển thị trên UI chuẩn có số 0
            file_name = f"Tap_{ep_num:02d}.mp4"
            
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
            
            file_item = QTableWidgetItem(file_name)
            file_item.setFlags(readonly_flags); file_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter); self.table.setItem(i, 2, file_item)

            safe_name = re.sub(r'[\\/*?:"<>|]', "", file_name)
            local_path = os.path.join(movie_folder, safe_name)
            
            if os.path.exists(local_path) and os.path.getsize(local_path) > 1024:
                link_item = QTableWidgetItem("💾 Đã có trên máy")
                link_item.setForeground(QColor("#c084fc"))
                _f = link_item.font(); _f.setBold(True); link_item.setFont(_f)
            elif ep_data and ep_data.get("drive_link"):
                link_item = QTableWidgetItem("☁️ Sẵn sàng (Link R2)")
                link_item.setForeground(QColor("#10b981"))
            else:
                link_item = QTableWidgetItem("⚡ Sẵn sàng (Link Gốc)")
                link_item.setForeground(QColor("#0ea5e9"))
                
            link_item.setFlags(readonly_flags); link_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter); self.table.setItem(i, 3, link_item)

    def _toggle_select_all(self):
        def _on_disk(i):
            st = self.table.item(i, 3)
            return bool(st and "Đã có trên máy" in st.text())

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

    # ==========================================
    # LOGIC TẢI KHỞI ĐỘNG
    # ==========================================
    def _download_selected(self):
        selected_eps_nums = []
        for i in range(self.table.rowCount()):
            item = self.table.item(i, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                selected_eps_nums.append(i + 1)

        if not selected_eps_nums:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn (tích) ít nhất 1 tập để tải!")
            return
            
        if self.current_series_id in getattr(self, '_cached_history_ids', set()):
            reply = QMessageBox.question(self, "Cảnh báo tải trùng", "Bộ phim này bạn ĐÃ TẢI VỀ máy trước đó rồi!\nBạn có chắc chắn muốn TẢI LẠI và BỊ TRỪ TIỀN không?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No: return

        # Dừng hẳn luồng theo dõi (Monitor) để nó không làm mới bảng và xóa mất tiến trình tải
        if getattr(self, 'monitor_thread', None):
            self.monitor_thread.stop()
            self.monitor_thread = None

        self.btn_download.setEnabled(False)
        self.btn_download.setText("⏳ Đang khởi chạy tải...")
        self.lbl_status.setText("⏳ Đang gửi yêu cầu giải mã hàng loạt...")

        folder_name = self.current_series_id if self.current_series_id else "Phim_Khong_Ro_ID"
        final_save_path = os.path.join(self.save_folder, folder_name)
        os.makedirs(final_save_path, exist_ok=True)

        self._save_to_history(self.current_series_id, self.current_title, self.current_cover_url,
                              total_eps=len(self.current_episodes) or getattr(self, 'current_total_eps', 0),
                              cover_bytes=getattr(self, 'current_cover_bytes', None),
                              downloaded=True)

        num_eps = len(selected_eps_nums)
        self._dl_finished = 0
        self.total_progress.setMaximum(num_eps); self.total_progress.setValue(0); self.total_progress.show()

        # Lưu danh sách file sẽ tải để phục vụ ghép sau
        self.downloaded_file_paths = []
        for ep_num in selected_eps_nums:
            fname = f"Tap_{int(ep_num):02d}.mp4"
            self.downloaded_file_paths.append(os.path.join(final_save_path, fname))

        try: max_threads = int(self.threads_combo.currentText())
        except Exception: max_threads = 3

        # Bộ đếm lượt xin lại link (Tránh treo app)
        self.server_retries = {} 

        self.download_manager = DriveDownloadManager(
            [], 
            final_save_path, 
            self.auth_token, 
            parent=self, 
            max_concurrent=max_threads, 
            expected_total=num_eps
        )
        self.download_manager.progress_signal.connect(self._on_download_progress)
        self.download_manager.done_signal.connect(self._on_episode_downloaded)
        self.download_manager.error_signal.connect(self._on_download_error)
        self.download_manager.dead_link_signal.connect(self._on_dead_link)
        self.download_manager.all_done_signal.connect(self._on_all_downloads_done)
        
        self._refresh_balance()
        
        payload = {
            "username": self.username,
            "num_episodes": num_eps,
            "series_id": self.current_series_id,
            "episodes": selected_eps_nums,
            "job_id": self.current_job_id
        }
        
        self.stream_thread = StreamDownloadThread(payload, self.auth_token)
        self._keep_thread_alive(self.stream_thread)
        self.stream_thread.link_ready_signal.connect(self._on_stream_link_ready)
        self.stream_thread.error_signal.connect(self._on_pay_error)
        self.stream_thread.start()

    def _on_stream_link_ready(self, data):
        ep_num = data.get("episode_number")
        url = data.get("url")
        
        # Nhận link thành công
        if ep_num and url:
            ep_item_data = {
                "episode_number": ep_num,
                "file_name": f"Tap_{int(ep_num):02d}.mp4",
                "drive_link": url,
                "series_id": self.current_series_id,
                "job_id": self.current_job_id
            }
            self.download_manager.add_and_run_episode(ep_item_data)
            
            if self.btn_download.text() == "⏳ Đang khởi chạy tải...":
                self.btn_download.setText("⏳ Đang lưu về máy...")
                self.lbl_status.setText("⏳ Đang kéo các tập phim về máy (bằng file .part)...")
                
        # Server báo lỗi giải mã -> Hoàn tiền luôn trên UI
        elif ep_num and data.get("status") == "error":
            self.download_manager._finished_count += 1
            self.download_manager.error_signal.emit(int(ep_num), data.get("message", "Lỗi giải mã API"))
            self.download_manager._check_all_done()
            self._refresh_balance()

    def _on_dead_link(self, ep_num):
        retry_count = self.server_retries.get(ep_num, 0)
        if retry_count >= 2:
            self.download_manager._finished_count += 1
            self.download_manager.error_signal.emit(ep_num, "Server không thể phục hồi link.")
            self.download_manager._check_all_done()
            return
            
        self.server_retries[ep_num] = retry_count + 1
        
        row = ep_num - 1
        if row < self.table.rowCount():
            status_item = QTableWidgetItem(f"🔄 Đang xin Server cấp lại link...")
            status_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            status_item.setForeground(QColor("#f59e0b"))
            self.table.setItem(row, 3, status_item)
            
        thread = RetryDeadLinkThread(self.username, self.current_job_id, self.current_series_id, ep_num, self.auth_token)
        self._keep_thread_alive(thread)
        thread.new_link_signal.connect(self._on_new_link_received)
        thread.start()

    def _on_new_link_received(self, data):
        ep_num = data.get("episode_number")
        if data.get("status") == "success":
            ep_item_data = {
                "episode_number": ep_num,
                "file_name": f"Tap_{int(ep_num):02d}.mp4",
                "drive_link": data["url"],
                "series_id": self.current_series_id,
                "job_id": self.current_job_id
            }
            self.download_manager.add_and_run_episode(ep_item_data)
        else:
            self.download_manager._finished_count += 1
            self.download_manager.error_signal.emit(ep_num, data.get("message", "Server từ chối cấp lại link"))
            self.download_manager._check_all_done()

    def _on_pay_error(self, error_msg):
        self.btn_download.setEnabled(True)
        self.btn_download.setText("📥 Tải đã chọn")
        self.lbl_status.setText("Trạng thái: Lỗi thanh toán/lấy link.")
        QMessageBox.critical(self, "Lỗi Xử Lý", error_msg)

    def _open_movie_folder(self):
        folder = os.path.join(self.save_folder, str(getattr(self, 'current_series_id', '') or ''))
        if not os.path.isdir(folder): folder = self.save_folder
        try: os.startfile(folder)
        except Exception as e: QMessageBox.warning(self, "Lỗi", f"Không mở được thư mục: {e}")

    def _bump_total_progress(self):
        if not hasattr(self, 'total_progress'): return
        self._dl_finished = getattr(self, '_dl_finished', 0) + 1
        self.total_progress.setValue(min(self._dl_finished, self.total_progress.maximum()))

    def _load_dub_voices(self):
        """Đọc HẾT giọng tiếng Việt từ Voice.json đổ vào combo. Thiếu file -> dùng vài giọng mặc định."""
        voices = []
        try:
            vpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Voice.json")
            with open(vpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            for v in data:
                if v.get("lan") == "vi" or v.get("lang") == "vi-VN":
                    name = v.get("display_name") or v.get("voice_type")
                    vt = v.get("voice_type")
                    if vt:
                        voices.append(f"{name} [{vt}]")
        except Exception:
            pass
        if not voices:
            voices = [
                "Cô Gái Hoạt Ngôn [BV074_streaming]",
                "Giọng Bé [BV074_streaming_dsp]",
                "Nhỏ Ngọt Ngào [BV421_vivn_streaming]",
                "Thanh Niên Tự Tin [BV075_streaming]",
            ]
        self.cmb_dub_voice.addItems(voices)
        for i in range(self.cmb_dub_voice.count()):
            if "BV074_streaming]" in self.cmb_dub_voice.itemText(i):
                self.cmb_dub_voice.setCurrentIndex(i); break

    def _refresh_balance(self):
        try:
            token = self.auth_token if hasattr(self, 'auth_token') else QSettings("HongguoDownloader", "ClientApp").value("auth_token", "")
            res = requests.get(f"{SERVER_URL}/api/client/balance/{self.username}", headers={"Authorization": f"Bearer {token}"}, timeout=5)
            if res.status_code == 200:
                balance = res.json().get("balance", 0)
                if hasattr(self, 'balance_changed') and self.balance_changed: self.balance_changed.emit(balance)
        except: pass

    def _on_download_progress(self, ep_num, percent, speed_mb):
        row = ep_num - 1
        if row < self.table.rowCount():
            if percent == -1:
                status_item = QTableWidgetItem(f"🔑 Đang xin Server giải mã...")
                status_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                status_item.setForeground(QColor("#f59e0b"))
                status_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            elif percent == -2:
                status_item = QTableWidgetItem(f"🔄 Thử lại lần {int(speed_mb)}...")
                status_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                status_item.setForeground(QColor("#f43f5e"))
                status_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            else:
                status_item = QTableWidgetItem(f"⬇️ {percent}% ({speed_mb:.1f} MB/s)")
                status_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                status_item.setForeground(QColor("#38bdf8"))
                status_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            
            self.table.setItem(row, 3, status_item)

    def _on_episode_downloaded(self, ep_num, file_path):
        row = ep_num - 1
        if row < self.table.rowCount():
            done_item = QTableWidgetItem("✔ ĐÃ XONG")
            done_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable); done_item.setForeground(QColor("#22d3ee")); done_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            _f = done_item.font(); _f.setBold(True); done_item.setFont(_f)
            self.table.setItem(row, 3, done_item)
        self._bump_total_progress()
        if hasattr(self, 'btn_stt_now'):
            self.btn_stt_now.setEnabled(True)

    def _on_download_error(self, ep_num, error_msg):
        row = ep_num - 1
        if row < self.table.rowCount():
            short_msg = error_msg[:25] + "..." if len(error_msg) > 25 else error_msg
            err_item = QTableWidgetItem(f"❌ {short_msg}")
            err_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable); err_item.setForeground(QColor("#ef4444")); err_item.setToolTip(str(error_msg)); err_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 3, err_item)
        self._bump_total_progress()

    def _on_all_downloads_done(self, total_downloaded):
        self._refresh_balance()

        mode = self.merge_mode_combo.currentIndex()
        files_to_merge = getattr(self, 'downloaded_file_paths', [])

        # Nếu không gộp hoặc không đủ file
        if mode == 0 or len(files_to_merge) <= 1:
            self.btn_download.setEnabled(True)
            self.btn_download.setText("📥 Tải đã chọn")
            self.lbl_status.setText(f"✅ Hoàn tất! Đã lưu {total_downloaded} tập phim lẻ.")
            if hasattr(self, 'lbl_downloaded_badge'): self.lbl_downloaded_badge.show()
            QMessageBox.information(self, "Thành công", f"Đã lưu thành công {total_downloaded} tập phim về máy bạn!")
            if hasattr(self, 'chk_auto_stt') and self.chk_auto_stt.isChecked():
                QTimer.singleShot(500, self._run_stt_on_downloaded)
            return

        # ==========================================
        # QUÁ TRÌNH GỘP FILE (MERGE)
        # ==========================================
        merge_tasks = []
        safe_title = re.sub(r'[\\/*?:"<>|]', "", self.current_title)

        if mode == 1:
            merge_tasks.append({
                "output_name": f"{safe_title} - Trọn Bộ.mp4",
                "files": files_to_merge
            })
        elif mode == 2:
            chunk_size = self.chunk_spinbox.value()
            for i in range(0, len(files_to_merge), chunk_size):
                chunk = files_to_merge[i:i + chunk_size]
                part_num = (i // chunk_size) + 1
                if len(chunk) > 1:
                    merge_tasks.append({
                        "output_name": f"{safe_title} - Phần {part_num}.mp4",
                        "files": chunk
                    })
                elif len(chunk) == 1:
                    out_path = os.path.join(self.save_folder, self.current_series_id, f"{safe_title} - Phần {part_num}.mp4")
                    try:
                        if os.path.exists(chunk[0]): shutil.move(chunk[0], out_path)
                    except: pass

        merge_tasks = [t for t in merge_tasks if len(t["files"]) > 1]

        if not merge_tasks:
            self.btn_download.setEnabled(True)
            self.btn_download.setText("📥 Tải đã chọn")
            self.lbl_status.setText("✅ Đã lưu và xử lý xong!")
            return

        folder_name = self.current_series_id if self.current_series_id else "Phim_Khong_Ro_ID"
        movie_folder = os.path.join(self.save_folder, folder_name)

        self.btn_download.setText("⚙️ Đang gộp file...")

        self.merge_thread = HonggouMergeThread(movie_folder, merge_tasks)
        self.merge_thread.progress_msg.connect(lambda msg: self.lbl_status.setText(msg))
        self.merge_thread.error_signal.connect(self._on_merge_error)
        self.merge_thread.finished_signal.connect(self._on_merge_finished)
        self._keep_thread_alive(self.merge_thread)
        self.merge_thread.start()

    def _on_merge_error(self, err_msg):
        self.btn_download.setEnabled(True)
        self.btn_download.setText("📥 Tải đã chọn")
        self.lbl_status.setText("❌ Gộp file thất bại.")
        QMessageBox.critical(self, "Lỗi gộp file", err_msg)

    def _on_merge_finished(self):
        self.btn_download.setEnabled(True)
        self.btn_download.setText("📥 Tải đã chọn")
        self.lbl_status.setText("✅ Đã tải và gộp thành công file!")
        if hasattr(self, 'lbl_downloaded_badge'): self.lbl_downloaded_badge.show()
        QMessageBox.information(
            self, "Thành công hoàn toàn",
            "Hệ thống đã tải và gộp thành công phim!\nCác tập lẻ đã được tự động xóa để tiết kiệm bộ nhớ."
        )
        if hasattr(self, 'chk_auto_stt') and self.chk_auto_stt.isChecked():
            # Tách sub từ file gộp (file mới nhất trong thư mục phim)
            folder = os.path.join(self.save_folder, self.current_series_id or "")
            if os.path.exists(folder):
                mp4s = sorted([os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".mp4")],
                              key=os.path.getmtime, reverse=True)
                if mp4s:
                    self._files_for_stt = mp4s
                    QTimer.singleShot(500, self._run_stt_on_downloaded)

    # ─────────────────────────────────────────────────────────────────
    #  STT / TÁCH SUB
    # ─────────────────────────────────────────────────────────────────
    def _run_dub_on_downloaded(self):
        """Lồng tiếng: ghép TTS vào video dựa trên SRT đã có."""
        files = getattr(self, '_files_for_stt', None) or getattr(self, 'downloaded_file_paths', [])
        files = [f for f in files if os.path.exists(f)]
        if not files:
            QMessageBox.warning(self, "Không có file", "Chưa có file video nào!"); return

        # Tìm file SRT tương ứng - ƯU TIÊN bản dịch _vi.srt (đã dịch Gemini)
        tasks = []
        for vf in files:
            base = os.path.splitext(vf)[0]
            vi_srt = base + "_vi.srt"
            srt_goc = base + ".srt"
            if os.path.exists(vi_srt):
                tasks.append({"video": vf, "srt": vi_srt})
            elif os.path.exists(srt_goc):
                # Chỉ còn srt gốc (chưa dịch) -> cảnh báo vì sẽ lồng tiếng gốc
                self.txt_stt_log.append(f"⚠️ Chưa có bản dịch _vi.srt cho {os.path.basename(vf)} — dùng srt gốc.")
                tasks.append({"video": vf, "srt": srt_goc})
            else:
                self.txt_stt_log.append(f"⏭ Bỏ qua (chưa có SRT): {os.path.basename(vf)}")

        if not tasks:
            QMessageBox.warning(self, "Không có SRT", "Chưa tìm thấy file .srt nào cạnh file video!\nHãy tách sub trước."); return

        # Lấy voice_type từ combo
        sel = self.cmb_dub_voice.currentText() if hasattr(self, 'cmb_dub_voice') else ""
        voice_type = "BV074_streaming"
        if "[" in sel and sel.endswith("]"):
            voice_type = sel[sel.rfind("[")+1:-1]
        rate = f"{self.spn_dub_rate.value():.1f}" if hasattr(self, 'spn_dub_rate') else "1.0"

        self.txt_stt_log.show()
        self.txt_stt_log.append(f"\n🎙 Bắt đầu lồng tiếng {len(tasks)} file | giọng: {voice_type} | rate: {rate}x")
        self.btn_dub_now.setEnabled(False); self.btn_dub_now.setText("⏳ Đang lồng tiếng...")
        self.lbl_status.setText(f"🎙 Đang lồng tiếng {len(tasks)} file...")

        mute_orig = self.chk_mute_original.isChecked() if hasattr(self, 'chk_mute_original') else True
        self._dub_thread = DubThread(tasks, voice_type=voice_type, rate=rate, mute_original=mute_orig)
        self._dub_thread.progress_signal.connect(self._on_stt_progress)   # dùng chung log box
        self._dub_thread.finished_signal.connect(self._on_dub_finished)
        self._keep_thread_alive(self._dub_thread)
        self._dub_thread.start()

    def _on_dub_finished(self, ok, failed):
        self.btn_dub_now.setEnabled(True); self.btn_dub_now.setText("🎙 Lồng tiếng ngay")
        summary = f"✅ Lồng tiếng xong: {ok} thành công, {failed} lỗi. File output: *_dubbed.mp4"
        self.lbl_status.setText(summary); self.txt_stt_log.append("\n" + summary)
        if ok > 0:
            QMessageBox.information(self, "Lồng tiếng hoàn tất",
                f"Đã lồng tiếng thành công {ok} video!\nFile đầu ra: *_dubbed.mp4 cạnh file gốc.")

    def _run_stt_on_downloaded(self):
        """Tách sub từ danh sách file đã tải."""
        # Ưu tiên _files_for_stt (từ merge), nếu không dùng downloaded_file_paths
        files = getattr(self, '_files_for_stt', None) or getattr(self, 'downloaded_file_paths', [])
        files = [f for f in files if os.path.exists(f)]
        if not files:
            QMessageBox.warning(self, "Không có file", "Chưa có file video nào để tách sub!"); return

        src  = self.cmb_stt_src.currentText() if hasattr(self, 'cmb_stt_src') else "zh-CN"
        out  = self.cmb_stt_out.currentText() if hasattr(self, 'cmb_stt_out') else "vi-VN"
        use_trans = src != out

        self.txt_stt_log.clear(); self.txt_stt_log.show()
        self.btn_stt_now.setEnabled(False); self.btn_stt_now.setText("⏳ Đang tách sub...")
        self.lbl_status.setText(f"🔤 Đang tách sub {len(files)} file...")
        self._stt_files = list(files)   # lưu để bước dịch Gemini dùng
        self._stt_out_lang = out

        self._stt_thread = SttBatchThread(files, src_lang=src, out_lang=out, use_trans=use_trans)
        self._stt_thread.progress_signal.connect(self._on_stt_progress)
        self._stt_thread.finished_signal.connect(self._on_stt_finished)
        self._keep_thread_alive(self._stt_thread)
        self._stt_thread.start()

    def _on_stt_progress(self, msg):
        self.txt_stt_log.append(msg)
        self.txt_stt_log.verticalScrollBar().setValue(self.txt_stt_log.verticalScrollBar().maximum())

    def _on_stt_finished(self, ok, failed):
        self.btn_stt_now.setEnabled(True); self.btn_stt_now.setText("🔤 Tách sub ngay")
        summary = f"✅ Tách sub xong: {ok} thành công, {failed} lỗi."
        self.lbl_status.setText(summary); self.txt_stt_log.append("\n" + summary)
        if ok <= 0:
            self._files_for_stt = None
            return
        if hasattr(self, 'btn_dub_now'): self.btn_dub_now.setEnabled(True)

        # BƯỚC DỊCH GEMINI: dịch các file .srt gốc -> _vi.srt trước khi lồng tiếng
        srt_files = []
        for vid in getattr(self, '_stt_files', []) or []:
            sp = os.path.splitext(vid)[0] + ".srt"
            if os.path.exists(sp):
                srt_files.append((vid, sp))

        if _GEMINI_AVAILABLE and GeminiTranslateThread and srt_files:
            if not os.path.exists(AUTH_FILE):
                QMessageBox.warning(self, "Chưa đăng nhập Gemini",
                    "Bạn cần bấm nút 'Đồng bộ Gemini' để đăng nhập trước khi dịch.")
                return
            self.txt_stt_log.append("\n🌐 Bắt đầu dịch bằng Gemini...")
            self._start_gemini_translate(srt_files)
        else:
            # Không có Gemini -> giữ hành vi cũ (chỉ tách sub)
            QMessageBox.information(self, "Tách sub hoàn tất",
                f"Đã tách sub {ok} file!\n(Chưa cấu hình Gemini nên không dịch.)")
        self._files_for_stt = None

    def _start_gemini_translate(self, srt_files):
        """Dịch danh sách (video, srt) bằng Gemini -> tạo _vi.srt, xong thì lồng tiếng."""
        settings = QSettings("HongguoDownloader", "ClientApp")
        preset = settings.value("trans_preset", list(PROMPT_PRESETS.keys())[0] if PROMPT_PRESETS else "")
        queue = [{"video": v, "srt": s} for (v, s) in srt_files]
        self._gemini_vi_map = {}   # video_path -> _vi.srt
        self._gemini_total = len(queue)
        self._gemini_done_count = 0

        self._gtrans_thread = GeminiTranslateThread(queue, preset, "Auto (Mặc định)", 100)
        self._gtrans_thread.log.connect(lambda m: self.txt_stt_log.append(m.strip()))
        def _on_item_done(idx, video_path, vi_path):
            self._gemini_vi_map[video_path] = vi_path
        def _on_item_failed(idx, msg):
            self.txt_stt_log.append(f"⚠️ Dịch lỗi 1 file: {msg}")
        self._gtrans_thread.item_done.connect(_on_item_done)
        self._gtrans_thread.item_failed.connect(_on_item_failed)
        self._gtrans_thread.all_done.connect(self._on_gemini_all_done)
        self._keep_thread_alive(self._gtrans_thread)
        self._gtrans_thread.start()

    def _on_gemini_all_done(self):
        n = len(getattr(self, '_gemini_vi_map', {}))
        self.txt_stt_log.append(f"\n✅ Dịch Gemini xong: {n} file.")
        # Sau khi dịch xong -> lồng tiếng (nếu bật auto dub)
        if hasattr(self, 'chk_auto_dub') and self.chk_auto_dub.isChecked():
            QTimer.singleShot(500, self._run_dub_on_downloaded)
        else:
            QMessageBox.information(self, "Dịch hoàn tất",
                f"Đã dịch {n} file sang tiếng Việt (_vi.srt).\nCó thể bấm Lồng tiếng.")

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
                resp = session.get(url, stream=True, timeout=30)

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
        self._is_force = bool(force)
        if force:
            # BẮT BUỘC: chặn dùng bản cũ. Chỉ có nút OK -> tải ngay. Đóng = thoát app.
            box = QMessageBox(self.parent)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Bắt buộc cập nhật")
            box.setText(f"Phiên bản mới v{version} là bản BẮT BUỘC.\n"
                        f"Bạn phải cập nhật để tiếp tục sử dụng.\n\n"
                        f"{changelog}\n\nNhấn OK để tải và cập nhật ngay.")
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
            box.exec()
            # Bấm OK (hoặc đóng) -> tải luôn, không cho thoát ra dùng bản cũ
            self._start_download(force=True)

    def _on_update_clicked(self):
        msg = f"Phiên bản mới: v{self._latest_version}\nHiện tại: v{APP_VERSION}\n\n"
        if self._changelog: msg += f"Thay đổi:\n{self._changelog}\n\n"
        msg += "Nhấn OK để tải bản mới.\nApp sẽ tự tắt → cập nhật → mở lại."
        if QMessageBox.question(self.parent, "Cập nhật phần mềm", msg, QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Ok: return
        self._start_download(force=False)

    def _start_download(self, force=False):
        self.progress = QProgressDialog("Đang tải phiên bản mới...", None, 0, 100, self.parent)
        self.progress.setWindowTitle("Cập nhật Hongguo Downloader")
        self.progress.setWindowModality(Qt.WindowModality.ApplicationModal if force else Qt.WindowModality.WindowModal)
        self.progress.setCancelButton(None); self.progress.setMinimumDuration(0); self.progress.setValue(0)
        # Bản bắt buộc: không cho tắt cửa sổ tải (chặn nút X)
        if force:
            self.progress.setWindowFlags(self.progress.windowFlags() & ~Qt.WindowType.WindowCloseButtonHint)
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
        # Bản BẮT BUỘC mà tải lỗi -> không cho dùng bản cũ, thoát app
        if getattr(self, "_is_force", False):
            QApplication.instance().quit()

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

        # Nút đồng bộ Gemini (đăng nhập 1 lần + chọn prompt dịch)
        self.btn_gemini = QPushButton()
        self.btn_gemini.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_gemini.clicked.connect(self._open_gemini_sync)
        self._refresh_gemini_btn()

        header_layout.addWidget(lbl_logo); header_layout.addStretch(); header_layout.addWidget(lbl_user_info)
        header_layout.addSpacing(20); header_layout.addWidget(self.lbl_balance)
        header_layout.addSpacing(15); header_layout.addWidget(self.btn_gemini)
        header_layout.addSpacing(20); header_layout.addWidget(btn_logout)

        self.updater = AutoUpdater(header_layout, parent_widget=self)
        main_layout.addWidget(header)

        self.honggou_tab = HonggouWidget(username)
        self.honggou_tab.balance_changed.connect(self._update_balance_display)
        main_layout.addWidget(self.honggou_tab)

        self.stack.addWidget(main_widget); self.stack.setCurrentWidget(main_widget)
        self._fetch_balance(username)
        self._hb_username = username
        self._hb_timer = QTimer(self); self._hb_timer.timeout.connect(self._send_heartbeat); self._hb_timer.start(20000); self._send_heartbeat()
    
    def _fetch_balance(self, username):
        try:
            token = self.honggou_tab.auth_token if hasattr(self, 'honggou_tab') else QSettings("HongguoDownloader", "ClientApp").value("auth_token", "")
            res = requests.get(f"{SERVER_URL}/api/client/balance/{username}", headers={"Authorization": f"Bearer {token}"}, timeout=5)
            if res.status_code == 200:
                balance = res.json().get("balance", 0)
                self._update_balance_display(balance)
            else:
                self._update_balance_display(0)
        except: 
            self._update_balance_display(0)

    def _update_balance_display(self, balance): 
        try:
            safe_bal = int(balance) if balance is not None else 0
            self.lbl_balance.setText(f"💰 Số dư: {safe_bal:,} đ".replace(",", "."))
        except:
            self.lbl_balance.setText("💰 Số dư: 0 đ")

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

    def _refresh_gemini_btn(self):
        """Cập nhật màu/chữ nút Gemini theo trạng thái đã login chưa."""
        try:
            logged = os.path.exists(AUTH_FILE)
        except Exception:
            logged = False
        if logged:
            self.btn_gemini.setText("🟢 Gemini")
            self.btn_gemini.setStyleSheet("QPushButton { padding: 8px 16px; background-color: #16a34a; color: white; border-radius: 6px; font-weight: bold; border: none; } QPushButton:hover { background-color: #15803d; }")
        else:
            self.btn_gemini.setText("🔑 Đồng bộ Gemini")
            self.btn_gemini.setStyleSheet("QPushButton { padding: 8px 16px; background-color: #7c3aed; color: white; border-radius: 6px; font-weight: bold; border: none; } QPushButton:hover { background-color: #6d28d9; }")

    def _open_gemini_sync(self):
        if not _GEMINI_AVAILABLE:
            QMessageBox.warning(self, "Thiếu module", "Không tìm thấy tab dịch (translate_tab.py). Hãy đặt file này cạnh app.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Đồng bộ Gemini")
        dlg.setMinimumWidth(460)
        dlg.setStyleSheet("QDialog { background:#0f172a; } QLabel { color:#e2e8f0; } QComboBox, QTextEdit { background:#1e293b; color:#e2e8f0; border:1px solid #334155; border-radius:6px; padding:6px; } QPushButton { padding:8px 14px; border:none; border-radius:6px; font-weight:bold; color:white; }")
        lay = QVBoxLayout(dlg)

        logged = os.path.exists(AUTH_FILE)
        lbl_status = QLabel("🟢 Đã đăng nhập Gemini" if logged else "🔴 Chưa đăng nhập Gemini")
        lay.addWidget(lbl_status)

        # Nút đăng nhập (mở Chrome đăng nhập 1 lần, lần sau khỏi cần)
        btn_login = QPushButton("🔑 Đăng nhập Gemini (1 lần)")
        btn_login.setStyleSheet("background:#7c3aed;")
        lay.addWidget(btn_login)

        # Chọn prompt dịch
        lay.addWidget(QLabel("Chọn prompt dịch:"))
        cb_preset = QComboBox()
        _gem_settings = QSettings("HongguoDownloader", "ClientApp")
        cb_preset.addItems(list(PROMPT_PRESETS.keys()))
        _default_preset = list(PROMPT_PRESETS.keys())[0] if PROMPT_PRESETS else ""
        saved = _gem_settings.value("trans_preset", _default_preset)
        if saved: cb_preset.setCurrentText(saved)
        lay.addWidget(cb_preset)

        # Xem nội dung prompt
        txt_preview = QTextEdit()
        txt_preview.setReadOnly(True)
        txt_preview.setFixedHeight(140)
        def _update_preview():
            txt_preview.setPlainText(PROMPT_PRESETS.get(cb_preset.currentText(), ""))
        cb_preset.currentTextChanged.connect(lambda _: _update_preview())
        _update_preview()
        lay.addWidget(QLabel("Nội dung prompt:"))
        lay.addWidget(txt_preview)

        log_box = QTextEdit(); log_box.setReadOnly(True); log_box.setFixedHeight(80)
        lay.addWidget(log_box)

        def _do_login():
            btn_login.setEnabled(False)
            log_box.append("⏳ Đang mở trình duyệt đăng nhập Gemini...")
            self._gemini_login_thread = GoogleManualLoginThread()
            self._gemini_login_thread.log.connect(lambda m: log_box.append(m.strip()))
            def _fin(ok):
                btn_login.setEnabled(True)
                lbl_status.setText("🟢 Đã đăng nhập Gemini" if ok else "🔴 Đăng nhập thất bại")
                self._refresh_gemini_btn()
            self._gemini_login_thread.finished_signal.connect(_fin)
            self._gemini_login_thread.start()
        btn_login.clicked.connect(_do_login)

        # Lưu preset khi đóng
        btn_save = QPushButton("💾 Lưu prompt & Đóng")
        btn_save.setStyleSheet("background:#16a34a;")
        def _save_close():
            _gem_settings.setValue("trans_preset", cb_preset.currentText())
            dlg.accept()
        btn_save.clicked.connect(_save_close)
        lay.addWidget(btn_save)

        dlg.exec()
        self._refresh_gemini_btn()

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
    # Style cho popup thông báo (QMessageBox) - nền tối, CHỮ TRẮNG rõ, nút sáng
    app.setStyleSheet("""
        QMessageBox { background-color: #1e293b; }
        QMessageBox QLabel { color: #f1f5f9; font-size: 13px; }
        QMessageBox QPushButton {
            background-color: #2563eb; color: white; border: none;
            border-radius: 6px; padding: 6px 18px; font-weight: bold; min-width: 70px;
        }
        QMessageBox QPushButton:hover { background-color: #1d4ed8; }
        QInputDialog { background-color: #1e293b; }
        QInputDialog QLabel { color: #f1f5f9; }
        QDialog { background-color: #1e293b; }
        QDialog QLabel { color: #f1f5f9; }
    """)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
