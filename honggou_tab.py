import sys
import time
import json
import requests
import os
import re
import tempfile
import subprocess
from urllib.parse import urlparse, parse_qs, unquote
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, 
    QTableWidget, QTableWidgetItem, QLabel, QMessageBox, 
    QHeaderView, QListWidget, QListWidgetItem, QApplication, QMainWindow, QStackedWidget,
    QFileDialog, QProgressDialog
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QSettings
from PyQt6.QtGui import QIcon, QPixmap, QImage, QFont, QColor

# ==========================================
# CẤU HÌNH SERVER & PHIÊN BẢN
# ==========================================
APP_VERSION = "1.0.7"  # ← ĐỔI MỖI LẦN BUILD PHIÊN BẢN MỚI
SERVER_URL = "http://163.61.182.119:8000"
MAX_CONCURRENT_DOWNLOADS = 3  # Số luồng tải song song từ Google Drive

# ==========================================
# THREAD 1: TẢI DANH SÁCH PHIM HOT NGẦM (CUỐN CHIẾU)
# ==========================================
class HotMoviesLoadThread(QThread):
    item_loaded_signal = pyqtSignal(dict) # Bắn tín hiệu TỪNG PHIM MỘT
    finished_signal = pyqtSignal()
    
    # [CẬP NHẬT]: Thêm biến genre để nhận lệnh lọc từ Tab Thể Loại
    def __init__(self, genre=None):
        super().__init__()
        self.genre = genre
        
    def run(self):
        try:
            url = f"{SERVER_URL}/api/client/hot_movies"
            params = {}
            if self.genre:
                params["genre"] = self.genre # Đính kèm thể loại để Server lọc
                
            res = requests.get(url, params=params, timeout=10)
            if res.status_code == 200:
                movies = res.json()
                for m in movies:
                    # --- GIẢI MÃ UNICODE CHO TÊN PHIM VÀ ẢNH ---
                    title = m.get("title", "")
                    if isinstance(title, str) and "\\u" in title:
                        try:
                            title = title.encode('utf-8').decode('unicode_escape')
                            m["title"] = title
                        except: pass
                    
                    cover_url = m.get("cover_url", "")
                    if isinstance(cover_url, str) and "\\u" in cover_url:
                        try:
                            cover_url = cover_url.encode('utf-8').decode('unicode_escape')
                        except: pass
                    
                    if cover_url:
                        cover_url = cover_url.replace("\\/", "/").replace("\\", "")
                        if cover_url.startswith("//"):
                            cover_url = "https:" + cover_url
                            
                        # --- CƠ CHẾ RETRY TẢI ẢNH TỐI ĐA 3 LẦN ---
                        for attempt in range(3):
                            try:
                                headers = {
                                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                    "Referer": "https://hongguoduanju.com/"
                                }
                                img_res = requests.get(cover_url, headers=headers, timeout=5)
                                
                                if img_res.status_code == 200:
                                    m["img_data"] = img_res.content
                                    break
                            except Exception:
                                if attempt < 2:
                                    time.sleep(0.5)
                                else:
                                    pass
                    
                    # QUAN TRỌNG: Tải xong phim nào là ném luôn ra giao diện phim đó
                    self.item_loaded_signal.emit(m)
                    
            self.finished_signal.emit()
        except Exception:
            self.finished_signal.emit()

# ==========================================
# THREAD 2: MÁY KHÁCH TỰ QUÉT LINK (PLAYWRIGHT NGẦM)
# ==========================================
class HonggouScanThread(QThread):
    scan_result = pyqtSignal(dict)
    error_signal = pyqtSignal(str)
    url_resolved_signal = pyqtSignal(str) 

    def __init__(self, url, auth_token=""):
        super().__init__()
        self.url = url
        self.auth_token = auth_token  # 🔒 JWT token

    def _resolve_to_detail_url(self, url):
        if "hongguoduanju.com/detail" in url or "hongguoduanju.com/player" in url:
            return url

        if re.search(r'novelquickapp\.com/s/', url):
            try:
                headers = {"User-Agent": "Mozilla/5.0"}
                resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
                url = resp.url
            except Exception:
                pass 

        decoded = url
        for _ in range(4):
            new_decoded = unquote(decoded)
            if new_decoded == decoded:
                break
            decoded = new_decoded

        match = re.search(r'"video_series_id"\s*:\s*"(\d+)"', decoded)
        if match:
            return f"https://hongguoduanju.com/detail?series_id={match.group(1)}"

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
                    if vid:
                        return f"https://hongguoduanju.com/detail?series_id={vid}"
        except Exception:
            pass

        match = re.search(r'video_series_id[=%22":]+(\d{15,25})', decoded)
        if match:
            return f"https://hongguoduanju.com/detail?series_id={match.group(1)}"

        return url  

    def run(self):
        try:
            self.url = self._resolve_to_detail_url(self.url)
            self.url_resolved_signal.emit(self.url) 

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            resp = requests.get(self.url, headers=headers, timeout=30)
            resp.raise_for_status()
            html = resp.text

            detail = None
            json_match = re.search(r'window\._ROUTER_DATA\s*=\s*(\{.+?\})\s*;?\s*</script>', html, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(1))
                    detail = data.get("loaderData", {}).get("detail_page", {}).get("seriesDetail", None)
                except json.JSONDecodeError:
                    pass

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
                if num_match:
                    total_episodes = int(num_match.group(1))
            if total_episodes == 0:
                vid_list = detail.get("vid_list", [])
                if isinstance(vid_list, list) and len(vid_list) > 0:
                    total_episodes = len(vid_list)
            if total_episodes == 0:
                total_episodes = int(detail.get("episode_cnt", 0))

            payload = {
                "url": self.url,
                "series_id": series_id,
                "expected_total": total_episodes,
                "title": title,
                "cover_url": cover_url
            }
            res = requests.post(f"{SERVER_URL}/api/client/add_job", json=payload, headers={"Authorization": f"Bearer {self.auth_token}"}, timeout=10)
            
            if res.status_code == 200:
                data = res.json()
                data["title"] = title
                data["cover_url"] = cover_url
                data["total_episodes"] = total_episodes
                self.scan_result.emit(data)
            else:
                self.error_signal.emit("Lỗi kết nối đến máy chủ điều phối!")

        except requests.exceptions.RequestException as e:
            self.error_signal.emit(f"Lỗi kết nối web: {str(e)}")
        except Exception as e:
            self.error_signal.emit(f"Lỗi quét link: {str(e)}")

# ==========================================
# THREAD 3: THEO DÕI TRẠNG THÁI REAL-TIME
# ==========================================
class JobStatusMonitorThread(QThread):
    update_signal = pyqtSignal(dict)

    def __init__(self, job_id, auth_token=""):
        super().__init__()
        self.job_id = job_id
        self.auth_token = auth_token  # 🔒 JWT token
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
            except Exception:
                pass
            time.sleep(3)

    def stop(self):
        self.running = False

# ==========================================
# THREAD 4a: LUỒNG TẢI 1 FILE MP4 TỪ GOOGLE DRIVE (Giữ nguyên)
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
        match = re.search(r'/d/([a-zA-Z0-9_-]+)', drive_link)
        return match.group(1) if match else None

    def run(self):
        ep_num = self.ep_data["episode_number"]
        file_name = self.ep_data.get("file_name", f"Tap_{ep_num}.mp4")
        drive_link = self.ep_data["drive_link"]
        save_path = os.path.join(self.save_folder, file_name)

        file_id = self._extract_file_id(drive_link)
        if not file_id:
            self.error_signal.emit(ep_num, "Link Drive không hợp lệ")
            return

        try:
            url = f"https://drive.google.com/uc?export=download&id={file_id}"
            session = requests.Session()

            resp = session.get(url, stream=True, timeout=30)

            for key, value in resp.cookies.items():
                if key.startswith('download_warning'):
                    url = f"https://drive.google.com/uc?export=download&confirm={value}&id={file_id}"
                    resp = session.get(url, stream=True, timeout=30)
                    break

            content_type = resp.headers.get('Content-Type', '')
            if 'text/html' in content_type:
                url = f"https://drive.google.com/uc?export=download&confirm=t&id={file_id}"
                resp = session.get(url, stream=True, timeout=30)

            total_size = int(resp.headers.get('content-length', 0))
            downloaded = 0
            chunk_size = 1024 * 1024
            start_time = time.time()

            with open(save_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        elapsed = time.time() - start_time
                        speed = (downloaded / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                        percent = int(downloaded * 100 / total_size) if total_size > 0 else 0
                        self.progress_signal.emit(ep_num, percent, speed)

            self.done_signal.emit(ep_num, save_path)
        except Exception as e:
            self.error_signal.emit(ep_num, str(e))

# ==========================================
# THREAD 4b: QUẢN LÝ TẢI SONG SONG NHIỀU LUỒNG (Giữ nguyên)
# ==========================================
class DriveDownloadThread(QThread):
    progress_signal = pyqtSignal(int, int, float)  
    done_signal = pyqtSignal(int, str)              
    error_signal = pyqtSignal(int, str)             
    all_done_signal = pyqtSignal(int)               

    def __init__(self, episodes, save_folder):
        super().__init__()
        self.episodes = list(episodes)
        self.save_folder = save_folder
        self._workers = []         
        self._pending = []         
        self._success_count = 0
        self._finished_count = 0
        self._total = len(episodes)

    def run(self):
        self._pending = list(self.episodes)
        self._workers = []
        self._success_count = 0
        self._finished_count = 0

        for _ in range(min(MAX_CONCURRENT_DOWNLOADS, len(self._pending))):
            self._launch_next()

        while self._finished_count < self._total:
            time.sleep(0.2)

        self.all_done_signal.emit(self._success_count)

    def _launch_next(self):
        if not self._pending:
            return
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

    def _on_worker_error(self, ep_num, error_msg):
        self._finished_count += 1
        self.error_signal.emit(ep_num, error_msg)
        self._launch_next()

# ==========================================
# MODULE CHÍNH: HONGGOU WIDGET
# ==========================================
class HonggouWidget(QWidget):
    def __init__(self, username):
        super().__init__()
        self.username = username
        self.current_job_id = None
        self.current_series_id = "" 
        self.current_episodes = []
        self.monitor_thread = None
        self.is_first_movie = True 
        self.settings = QSettings("AnhStudio", "HongguoApp")
        
        self.auth_token = self.settings.value("auth_token", "")  # 🔒 Lấy JWT token
        
        default_folder = os.path.join(os.path.expanduser("~"), "Desktop", "AnhStudio_Downloads")
        self.save_folder = self.settings.value(f"download_folder_{username}", default_folder)
        os.makedirs(self.save_folder, exist_ok=True)
        self._setup_ui()
        self.load_hot_movies_shelf()

    def _setup_ui(self):
        master_layout = QVBoxLayout(self)
        master_layout.setSpacing(15)
        master_layout.setContentsMargins(20, 20, 20, 20)

        # --- THANH TÌM KIẾM ---
        top_bar = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Dán link phim Hongguo vào đây rồi nhấn Enter (VD: https://hongguoduanju.com/...)")
        self.url_input.setStyleSheet("""
            QLineEdit {
                padding: 12px; font-size: 14px; border-radius: 8px; 
                border: 1px solid #374151; background: #1f2937; color: #f8fafc;
            }
            QLineEdit:focus { border: 1px solid #3b82f6; background: #1e293b; }
        """)
        self.url_input.returnPressed.connect(self._scan)
        
        self.btn_scan = QPushButton("🔍 Quét Phim")
        self.btn_scan.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_scan.setStyleSheet("""
            QPushButton {
                padding: 12px 24px; font-size: 14px; background-color: #2563eb; 
                color: white; border-radius: 8px; font-weight: bold; border: none;
            }
            QPushButton:hover { background-color: #1d4ed8; }
            QPushButton:disabled { background-color: #374151; color: #64748b; }
        """)
        self.btn_scan.clicked.connect(self._scan)
        
        top_bar.addWidget(self.url_input)
        top_bar.addWidget(self.btn_scan)
        master_layout.addLayout(top_bar)

        # --- THANH THƯ MỤC TẢI ---
        folder_bar = QHBoxLayout()
        self.lbl_folder = QLabel(f"📂 Lưu vào: {self.save_folder}")
        self.lbl_folder.setStyleSheet("color: #94a3b8; font-size: 12px; padding: 4px;")
        
        btn_change_folder = QPushButton("Đổi thư mục")
        btn_change_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_change_folder.setStyleSheet("""
            QPushButton { padding: 6px 14px; font-size: 12px; background-color: transparent; color: #3b82f6; border: 1px solid #374151; border-radius: 6px; }
            QPushButton:hover { background-color: #1e293b; border: 1px solid #3b82f6; }
        """)
        btn_change_folder.clicked.connect(self._change_folder)
        
        folder_bar.addWidget(self.lbl_folder)
        folder_bar.addStretch()
        folder_bar.addWidget(btn_change_folder)
        master_layout.addLayout(folder_bar)

        self.content_stack = QStackedWidget()
        master_layout.addWidget(self.content_stack)

        # ==========================================
        # TRANG 1: KỆ PHIM HOT & TAB THỂ LOẠI
        # ==========================================
        self.page_grid = QWidget()
        grid_layout = QVBoxLayout(self.page_grid)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        
        # --- TAB THỂ LOẠI (MỚI) ---
        self.genre_container = QWidget()
        genre_layout = QHBoxLayout(self.genre_container)
        genre_layout.setContentsMargins(0, 5, 0, 10)
        genre_layout.setSpacing(10)
        
        self.genre_buttons = []
        
        # Danh sách các Tab cần hiện (Bạn có thể thêm bớt thoải mái)
        genres = [
            ("🔥 Phổ Biến", None), 
            ("💖 Ngọt Sủng", "Ngọt sủng"), 
            ("⚔️ Chiến Thần", "Chiến thần"), 
            ("👔 Tổng Tài", "Tổng tài"), 
            ("🌀 Xuyên Không", "Xuyên không"), 
            ("🔄 Trọng Sinh", "Trọng sinh"), 
            ("🏯 Cổ Trang", "Cổ trang"),
            ("😂 Hài Hước", "Hài hước")
        ]
        
        for name, tag in genres:
            btn = QPushButton(name)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("genre_tag", tag)
            btn.clicked.connect(lambda checked, b=btn: self._on_genre_clicked(b))
            genre_layout.addWidget(btn)
            self.genre_buttons.append(btn)
            
        genre_layout.addStretch() # Dồn toàn bộ nút sang bên trái
        grid_layout.addWidget(self.genre_container)
        
        # Cập nhật style mặc định (Tab Phổ Biến sáng lên)
        self._update_genre_styles(self.genre_buttons[0])

        self.hot_list = QListWidget()
        self.hot_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.hot_list.setIconSize(QSize(160, 220))
        self.hot_list.setGridSize(QSize(180, 280))
        self.hot_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.hot_list.setWordWrap(True)
        self.hot_list.setStyleSheet("""
            QListWidget { background-color: transparent; border: none; outline: none; }
            QListWidget::item { color: #e2e8f0; font-weight: bold; font-size: 13px; padding-top: 5px; border-radius: 10px; }
            QListWidget::item:hover { background-color: #1e293b; }
            QScrollBar:vertical { border: none; background: #111827; width: 8px; margin: 0px; }
            QScrollBar::handle:vertical { background: #374151; border-radius: 4px; min-height: 20px; }
            QScrollBar::handle:vertical:hover { background: #4b5563; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { border: none; background: none; height: 0px; }
        """)
        self.hot_list.itemClicked.connect(self._on_hot_movie_clicked)
        grid_layout.addWidget(self.hot_list)
        
        self.content_stack.addWidget(self.page_grid)

        # --- TRANG 2: BẢNG CHI TIẾT ---
        self.page_detail = QWidget()
        detail_layout = QVBoxLayout(self.page_detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)

        self.btn_back = QPushButton("⬅ Quay lại danh sách phim")
        self.btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_back.setStyleSheet("""
            QPushButton { padding: 8px 15px; background-color: transparent; color: #94a3b8; border: 1px solid #374151; border-radius: 6px; font-weight: bold; text-align: left; }
            QPushButton:hover { background-color: #1e293b; color: #f8fafc; border: 1px solid #4b5563; }
        """)
        self.btn_back.clicked.connect(self._go_back)
        
        btn_back_layout = QHBoxLayout()
        btn_back_layout.addWidget(self.btn_back)
        btn_back_layout.addStretch()
        detail_layout.addLayout(btn_back_layout)

        self.lbl_status = QLabel("Trạng thái: Sẵn sàng phục vụ...")
        self.lbl_status.setStyleSheet("color: #10b981; font-size: 14px; font-weight: bold; margin-top: 10px;")
        detail_layout.addWidget(self.lbl_status)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Chọn Tập", "Tên File", "Trạng Thái Link"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False) 
        self.table.setShowGrid(False) 
        self.table.setAlternatingRowColors(True) 
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus) 
        
        self.table.setStyleSheet("""
            QTableWidget { 
                background-color: #111827; 
                alternate-background-color: #1f2937; 
                color: #e2e8f0; 
                border: 1px solid #374151; 
                border-radius: 8px; 
                outline: none; 
                margin-top: 10px;
                font-size: 13px;
            }
            QHeaderView::section { 
                background-color: #0f172a; 
                color: #94a3b8; 
                padding: 12px; 
                font-weight: bold; 
                border: none; 
                border-bottom: 1px solid #374151;
            }
            QTableWidget::item { 
                padding: 6px; 
                border-bottom: 1px solid transparent; 
            }
            QTableWidget::item:hover {
                background-color: #334155;
            }
            QTableWidget::indicator { 
                width: 18px; 
                height: 18px; 
                border: 2px solid #475569;
                border-radius: 4px;
            }
            QTableWidget::indicator:checked {
                background-color: #10b981;
                border-color: #10b981;
            }
            QScrollBar:vertical { border: none; background: #111827; width: 8px; margin: 0px; }
            QScrollBar::handle:vertical { background: #374151; border-radius: 4px; min-height: 20px; }
            QScrollBar::handle:vertical:hover { background: #4b5563; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { border: none; background: none; }
        """)
        detail_layout.addWidget(self.table)

        bottom_layout = QHBoxLayout()
        
        self.btn_select_all = QPushButton("☑ Chọn / Bỏ chọn tất cả")
        self.btn_select_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_select_all.setStyleSheet("""
            QPushButton { padding: 14px; background-color: #4b5563; color: white; border-radius: 8px; font-weight: bold; font-size: 14px; margin-top: 10px; border: none;}
            QPushButton:hover { background-color: #64748b; }
        """)
        self.btn_select_all.clicked.connect(self._toggle_select_all)
        
        self.btn_download = QPushButton("📥 Tải đã chọn")
        self.btn_download.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_download.setStyleSheet("""
            QPushButton { padding: 14px 30px; background-color: #10b981; color: white; border-radius: 8px; font-weight: bold; font-size: 15px; margin-top: 10px; border: none; }
            QPushButton:hover { background-color: #059669; }
            QPushButton:disabled { background-color: #374151; color: #64748b; }
        """)
        self.btn_download.setEnabled(False)
        self.btn_download.clicked.connect(self._download_selected)
        
        bottom_layout.addWidget(self.btn_select_all)
        bottom_layout.addWidget(self.btn_download)
        detail_layout.addLayout(bottom_layout)

        self.content_stack.addWidget(self.page_detail)

    # --- HÀM STYLE & SỰ KIỆN TAB THỂ LOẠI ---
    def _update_genre_styles(self, active_btn):
        for btn in self.genre_buttons:
            if btn == active_btn:
                # NÚT ĐANG CHỌN (Màu Vàng Nổi Bật)
                btn.setStyleSheet("""
                    QPushButton { background-color: #f59e0b; color: #ffffff; font-weight: bold; font-size: 14px; border-radius: 16px; padding: 8px 20px; border: none; }
                    QPushButton:hover { background-color: #d97706; }
                """)
            else:
                # NÚT CHƯA CHỌN (Màu Xanh Cyan Sáng mượt mà)
                btn.setStyleSheet("""
                    QPushButton { background-color: #0ea5e9; color: #ffffff; font-weight: bold; font-size: 13px; border-radius: 16px; padding: 8px 18px; border: none; }
                    QPushButton:hover { background-color: #38bdf8; }
                """)

    def _on_genre_clicked(self, clicked_btn):
        # 1. Đổi màu nút
        self._update_genre_styles(clicked_btn)
        # 2. Lấy thể loại và nạp lại phim
        genre_tag = clicked_btn.property("genre_tag")
        self.load_hot_movies_shelf(genre_tag)

    def load_hot_movies_shelf(self, genre=None):
        self.hot_list.clear()
        
        msg = "⏳ Đang kết nối máy chủ để tải kệ phim...\nVui lòng chờ trong giây lát."
        if genre:
            msg = f"⏳ Đang lọc phim thể loại [{genre}]...\nVui lòng chờ trong giây lát."
            
        loading_item = QListWidgetItem(msg)
        loading_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        loading_item.setFlags(Qt.ItemFlag.NoItemFlags) 
        self.hot_list.addItem(loading_item)
        
        self.is_first_movie = True 
        
        # Bắn lệnh quét ngầm kèm filter thể loại
        self.hot_thread = HotMoviesLoadThread(genre)
        self.hot_thread.item_loaded_signal.connect(self._render_single_hot_movie)
        self.hot_thread.start()

    def _render_single_hot_movie(self, m):
        if self.is_first_movie:
            self.hot_list.clear()
            self.is_first_movie = False

        item = QListWidgetItem()
        title = m.get("title")
        if not title:
            title = "Phim Hot Gợi Ý"
            
        eps = m.get("total_episodes", 0)
        item.setText(f"{title}\n({eps} Tập)")
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        
        img_data = m.get("img_data")
        if img_data:
            pixmap = QPixmap()
            success = pixmap.loadFromData(img_data)
            if success and not pixmap.isNull():
                pixmap = pixmap.scaled(
                    160, 220, 
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding, 
                    Qt.TransformationMode.SmoothTransformation
                )
                item.setIcon(QIcon(pixmap))
        
        item.setData(Qt.ItemDataRole.UserRole, m.get("url", "")) 
        self.hot_list.addItem(item)

    def _on_hot_movie_clicked(self, item):
        url = item.data(Qt.ItemDataRole.UserRole)
        if url:
            self.url_input.setText(url)
            self._scan()

    def _go_back(self):
        if self.monitor_thread:
            self.monitor_thread.stop()
        self.content_stack.setCurrentWidget(self.page_grid)
        self.url_input.clear()

    def _normalize_url(self, raw_url):
        if "hongguoduanju.com/detail" in raw_url or "hongguoduanju.com/player" in raw_url:
            return raw_url

        video_series_id = None

        decoded = raw_url
        for _ in range(4):
            new_decoded = unquote(decoded)
            if new_decoded == decoded:
                break
            decoded = new_decoded

        match = re.search(r'"video_series_id"\s*:\s*"(\d+)"', decoded)
        if match:
            video_series_id = match.group(1)

        if not video_series_id:
            try:
                parsed = urlparse(raw_url)
                params = parse_qs(parsed.query)
                
                zlink = params.get("zlink", [None])[0]
                if zlink:
                    zlink_decoded = unquote(zlink)
                    zlink_parsed = urlparse(zlink_decoded)
                    zlink_params = parse_qs(zlink_parsed.query)
                    
                    scheme_params_raw = zlink_params.get("schemeParams", [None])[0]
                    if scheme_params_raw:
                        scheme_decoded = unquote(scheme_params_raw)
                        try:
                            scheme_json = json.loads(scheme_decoded)
                            video_series_id = str(scheme_json.get("video_series_id", ""))
                        except json.JSONDecodeError:
                            pass
                
                if not video_series_id:
                    scheme_params_raw = params.get("schemeParams", [None])[0]
                    if scheme_params_raw:
                        scheme_decoded = unquote(scheme_params_raw)
                        try:
                            scheme_json = json.loads(scheme_decoded)
                            video_series_id = str(scheme_json.get("video_series_id", ""))
                        except json.JSONDecodeError:
                            pass
            except Exception:
                pass

        if not video_series_id:
            match = re.search(r'video_series_id[=%22":]+(\d{15,25})', decoded)
            if match:
                video_series_id = match.group(1)

        if video_series_id:
            return f"https://hongguoduanju.com/detail?series_id={video_series_id}"
        
        return raw_url

    def _extract_url_from_text(self, text):
        match = re.search(r'(https?://\S+)', text)
        return match.group(1) if match else text

    def _scan(self):
        raw_text = self.url_input.text().strip()
        if not raw_text:
            QMessageBox.warning(self, "Lỗi", "Vui lòng nhập hoặc dán link phim!")
            return
            
        if self.monitor_thread:
            self.monitor_thread.stop()

        raw_url = self._extract_url_from_text(raw_text)

        url = self._normalize_url(raw_url)
        if url != raw_text:
            self.url_input.setText(url) 

        self.content_stack.setCurrentWidget(self.page_detail)
        self.btn_scan.setEnabled(False)
        self.btn_scan.setText("⏳ Đang quét ngầm...")
        self.lbl_status.setText("Trạng thái: Đang kết nối bóc tách dữ liệu...")
        self.table.setRowCount(0)

        self.scan_thread = HonggouScanThread(url, self.auth_token)
        self.scan_thread.scan_result.connect(self._on_scan_result)
        self.scan_thread.error_signal.connect(self._on_scan_error)
        self.scan_thread.url_resolved_signal.connect(self._on_url_resolved)
        self.scan_thread.start()

    def _on_scan_result(self, data):
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("🔍 Quét Phim")
        
        status = data.get("status")
        self.current_job_id = data.get("job_id")
        self.current_series_id = str(data.get("series_id", "")) 
        total_eps = data.get("total_episodes", 0)
        self.current_episodes = data.get("episodes", [])
        
        if status == "cache_hit":
            self.lbl_status.setText(f"✅ Phim đã có sẵn trên Server! (Tổng: {total_eps} tập). Bạn có thể chọn tập và tải ngay.")
            self.btn_download.setEnabled(True)
        elif status == "retrying":
            self.lbl_status.setText(f"⚠️ Worker đang tiến hành tải bổ sung.")
            self.btn_download.setEnabled(True) 
        elif status == "processing":
            self.lbl_status.setText("⏳ Worker đang tiến hành tải. Link sẽ tự động cập nhật khi có!")
            self.btn_download.setEnabled(True)
        else:
            self.lbl_status.setText(f"🕒 Đã lên đơn! Chờ Worker nhận việc tải {total_eps} tập.")
            self.btn_download.setEnabled(False)

        self._render_table(total_eps, self.current_episodes)

        if status not in ["cache_hit", "completed"] and self.current_job_id:
            self.monitor_thread = JobStatusMonitorThread(self.current_job_id, self.auth_token)
            self.monitor_thread.update_signal.connect(self._on_monitor_update)
            self.monitor_thread.start()

    def _on_monitor_update(self, data):
        status = data.get("status")
        total_eps = data.get("total_episodes", self.table.rowCount())
        self.current_episodes = data.get("episodes", [])

        if status == "completed":
            self.lbl_status.setText(f"✅ Quá trình tải đã hoàn tất! Phim đã có sẵn (Tổng: {total_eps} tập).")
            self.btn_download.setEnabled(True)
        elif status == "partial":
            self.lbl_status.setText(f"⚠️ Worker đã tải xong một phần. (Hiện có: {len(self.current_episodes)} tập).")
            self.btn_download.setEnabled(True)
        elif status == "processing":
            self.lbl_status.setText(f"⏳ Đang tải... Đã lên Drive {len(self.current_episodes)}/{total_eps} tập.")
            self.btn_download.setEnabled(True)

        self._render_table(total_eps, self.current_episodes)

    def _render_table(self, total_eps, episodes):
        checked_eps = set()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                checked_eps.add(row)

        if self.table.rowCount() != total_eps:
            self.table.setRowCount(total_eps)
            
        for i in range(total_eps):
            ep_num = i + 1
            ep_data = next((e for e in episodes if e.get("episode_number") == ep_num), None)
            
            ep_item = QTableWidgetItem(f" Tập {ep_num}")
            ep_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            ep_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            
            if i in checked_eps:
                ep_item.setCheckState(Qt.CheckState.Checked)
            else:
                ep_item.setCheckState(Qt.CheckState.Unchecked)
                
            self.table.setItem(i, 0, ep_item)
            self.table.setRowHeight(i, 45) 
            
            if ep_data and ep_data.get("drive_link"):
                file_item = QTableWidgetItem(ep_data.get("file_name", f"Tap_{ep_num}.mp4"))
                file_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(i, 1, file_item)
                
                link_item = QTableWidgetItem("✅ Sẵn sàng")
                link_item.setForeground(QColor("#10b981")) 
                link_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(i, 2, link_item)
            else:
                file_item = QTableWidgetItem("---")
                file_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(i, 1, file_item)
                
                wait_item = QTableWidgetItem("⏳ Đang đợi")
                wait_item.setForeground(QColor("#f59e0b")) 
                wait_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(i, 2, wait_item)

    def _toggle_select_all(self):
        all_checked = True
        for i in range(self.table.rowCount()):
            item = self.table.item(i, 0)
            if item and item.checkState() != Qt.CheckState.Checked:
                all_checked = False
                break
                
        new_state = Qt.CheckState.Unchecked if all_checked else Qt.CheckState.Checked
        for i in range(self.table.rowCount()):
            item = self.table.item(i, 0)
            if item:
                item.setCheckState(new_state)

    def _download_selected(self):
        selected_eps = []
        for i in range(self.table.rowCount()):
            item = self.table.item(i, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                ep_num = i + 1
                ep_data = next((e for e in self.current_episodes if e.get("episode_number") == ep_num), None)
                if ep_data and ep_data.get("drive_link"):
                    selected_eps.append(ep_data)

        if not selected_eps:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn (tích) ít nhất 1 tập đã tải xong!")
            return
            
        num_eps = len(selected_eps)
        
        folder_name = self.current_series_id if self.current_series_id else "Phim_Khong_Ro_ID"
        final_save_path = os.path.join(self.save_folder, folder_name)
        os.makedirs(final_save_path, exist_ok=True)

        try:
            res = requests.post(f"{SERVER_URL}/api/client/pay_for_download", json={"username": self.username, "num_episodes": num_eps}, headers=self._auth_headers(), timeout=10)
            data = res.json()
            
            if data.get("status") == "success":
                self.btn_download.setEnabled(False)
                self.btn_download.setText("⏳ Đang tải xuống...")
                self.lbl_status.setText(f"⏳ Đang tải {num_eps} tập về máy...")
                
                self._refresh_balance()
                
                self.download_thread = DriveDownloadThread(selected_eps, final_save_path)
                self.download_thread.progress_signal.connect(self._on_download_progress)
                self.download_thread.done_signal.connect(self._on_episode_downloaded)
                self.download_thread.error_signal.connect(self._on_download_error)
                self.download_thread.all_done_signal.connect(self._on_all_downloads_done)
                self.download_thread.start()
            else:
                QMessageBox.critical(self, "Không đủ số dư", data.get("message", "Vui lòng nạp thêm tiền!"))
        except Exception as e:
            QMessageBox.critical(self, "Lỗi mạng", f"Không thể kết nối đến Server: {e}")

    def _refresh_balance(self):
        try:
            res = requests.get(f"{SERVER_URL}/api/client/balance/{self.username}", headers=self._auth_headers(), timeout=5)
            if res.status_code == 200:
                balance = res.json().get("balance", 0)
                if hasattr(self, 'balance_changed') and self.balance_changed:
                    self.balance_changed(balance)
        except Exception:
            pass

    def _on_download_progress(self, ep_num, percent, speed_mb):
        row = ep_num - 1
        if row < self.table.rowCount():
            status_item = QTableWidgetItem(f"⬇️ {percent}% ({speed_mb:.1f} MB/s)")
            status_item.setForeground(QColor("#38bdf8")) 
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 2, status_item)

    def _on_episode_downloaded(self, ep_num, file_path):
        row = ep_num - 1
        if row < self.table.rowCount():
            done_item = QTableWidgetItem("✅ Đã xong")
            done_item.setForeground(QColor("#10b981"))
            done_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 2, done_item)

    def _on_download_error(self, ep_num, error_msg):
        row = ep_num - 1
        if row < self.table.rowCount():
            err_item = QTableWidgetItem(f"❌ Lỗi: {error_msg[:30]}")
            err_item.setForeground(QColor("#ef4444"))
            err_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 2, err_item)

    def _on_all_downloads_done(self, total_downloaded):
        self.btn_download.setEnabled(True)
        self.btn_download.setText("📥 Tải đã chọn")
        self.lbl_status.setText(f"✅ Hoàn tất! Đã tải {total_downloaded} tập về máy.")
        self._refresh_balance()
        QMessageBox.information(self, "Thành công", f"Đã tải xong {total_downloaded} tập MP4 về máy bạn!")

    def _change_folder(self):
        new_folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu phim", self.save_folder)
        if new_folder:
            self.save_folder = new_folder
            self.settings.setValue(f"download_folder_{self.username}", new_folder)
            self.lbl_folder.setText(f"📂 Lưu vào: {self.save_folder}")

    def _auth_headers(self):
        """🔒 Header xác thực JWT cho mọi request tới Server."""
        return {"Authorization": f"Bearer {self.auth_token}"}

    def _on_scan_error(self, error_msg):
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("🔍 Quét Phim")
        self.lbl_status.setText("Trạng thái: Sẵn sàng phục vụ...")
        QMessageBox.critical(self, "Lỗi Quét Phim", error_msg)

    def _on_url_resolved(self, resolved_url):
        self.url_input.setText(resolved_url)

# ==========================================
# AUTO-UPDATER: KIỂM TRA & CẬP NHẬT PHIÊN BẢN MỚI TỪ GITHUB
# ==========================================
def _compare_versions(current: str, latest: str) -> bool:
    try:
        cur = [int(x) for x in current.split(".")]
        lat = [int(x) for x in latest.split(".")]
        return lat > cur
    except Exception:
        return False

def _get_exe_path() -> str:
    if getattr(sys, 'frozen', False):
        return sys.executable
    else:
        return os.path.abspath(sys.argv[0])

class UpdateCheckThread(QThread):
    """Chạy ngầm khi app khởi động, kiểm tra có bản mới không."""
    update_available = pyqtSignal(str, str, str, bool)  # version, url, changelog, force
    no_update = pyqtSignal()

    def run(self):
        try:
            res = requests.get(
                f"{SERVER_URL}/api/client/check_update",
                params={"current_version": APP_VERSION},
                timeout=10
            )
            if res.status_code == 200:
                data = res.json()
                latest = data.get("latest_version", APP_VERSION)
                if _compare_versions(APP_VERSION, latest):
                    self.update_available.emit(
                        latest,
                        data.get("download_url", ""),
                        data.get("changelog", ""),
                        data.get("force_update", False)
                    )
                else:
                    self.no_update.emit()
            else:
                self.no_update.emit()
        except Exception:
            pass

class DownloadUpdateThread(QThread):
    """Tải file .exe mới từ GitHub về thư mục tạm, báo tiến trình %."""
    progress_signal = pyqtSignal(int)
    done_signal = pyqtSignal(str)
    error_signal = pyqtSignal(str)

    def __init__(self, download_url: str):
        super().__init__()
        self.download_url = download_url

    def run(self):
        try:
            # TẢI TRỰC TIẾP TỪ GITHUB - KHÔNG CẦN VƯỢT RÀO COOKIE NỮA
            resp = requests.get(self.download_url, stream=True, timeout=30)
            resp.raise_for_status()

            total_size = int(resp.headers.get('content-length', 0))
            downloaded = 0
            temp_path = os.path.join(tempfile.gettempdir(), "AnhStudio_Update.exe")

            with open(temp_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            self.progress_signal.emit(int(downloaded * 100 / total_size))

            self.done_signal.emit(temp_path)
        except Exception as e:
            self.error_signal.emit(str(e))

def _apply_update_and_restart(new_exe_path: str):
    """Ghi file .bat thay thế exe cũ rồi restart."""
    current_exe = _get_exe_path()

    if sys.platform == "win32":
        bat_path = os.path.join(tempfile.gettempdir(), "anhstudio_update.bat")
        bat_content = f'''@echo off
chcp 65001 >nul
echo AnhStudio - Dang cap nhat...

set /a count=0
:WAIT_LOOP
tasklist /FI "PID eq {os.getpid()}" 2>NUL | find /I "{os.getpid()}" >NUL
if errorlevel 1 goto DO_UPDATE
timeout /t 1 /nobreak >nul
set /a count+=1
if %count% GEQ 30 goto DO_UPDATE
goto WAIT_LOOP

:DO_UPDATE
if exist "{current_exe}" copy /Y "{current_exe}" "{current_exe}.bak" >nul 2>&1
copy /Y "{new_exe_path}" "{current_exe}"
if errorlevel 1 (
    if exist "{current_exe}.bak" copy /Y "{current_exe}.bak" "{current_exe}" >nul 2>&1
    pause
    goto END
)
del /f /q "{current_exe}.bak" >nul 2>&1
del /f /q "{new_exe_path}" >nul 2>&1
start "" "{current_exe}"
:END
del /f /q "%~f0" >nul 2>&1
exit
'''
        with open(bat_path, 'w', encoding='utf-8') as f:
            f.write(bat_content)
        subprocess.Popen(
            ['cmd', '/c', 'start', '', bat_path],
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )
    else:
        sh_path = os.path.join(tempfile.gettempdir(), "anhstudio_update.sh")
        sh_content = f'''#!/bin/bash
for i in $(seq 1 30); do kill -0 {os.getpid()} 2>/dev/null || break; sleep 1; done
cp "{current_exe}" "{current_exe}.bak" 2>/dev/null
cp "{new_exe_path}" "{current_exe}" && chmod +x "{current_exe}"
rm -f "{current_exe}.bak" "{new_exe_path}"
"{current_exe}" &
rm -f "$0"
'''
        with open(sh_path, 'w') as f:
            f.write(sh_content)
        os.chmod(sh_path, 0o755)
        subprocess.Popen(['bash', sh_path])

    QApplication.instance().quit()

class AutoUpdater:
    """Gắn vào header_layout, tự kiểm tra & ÉP cập nhật."""
    def __init__(self, header_layout: QHBoxLayout, parent_widget=None):
        self.parent = parent_widget
        self._download_url = ""
        self._latest_version = ""
        self._changelog = ""

        self.btn_update = QPushButton()
        self.btn_update.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_update.setVisible(False)
        self.btn_update.clicked.connect(lambda: self._on_update_clicked(is_forced=False))
        self.btn_update.setStyleSheet("""
            QPushButton { padding: 8px 16px; background-color: #f59e0b; color: #000; border-radius: 6px; font-weight: bold; font-size: 13px; border: none; }
            QPushButton:hover { background-color: #d97706; }
        """)

        logout_index = header_layout.count() - 1
        header_layout.insertWidget(logout_index, self.btn_update)
        header_layout.insertSpacing(logout_index + 1, 10)

        self._check_thread = UpdateCheckThread()
        self._check_thread.update_available.connect(self._on_update_found)
        self._check_thread.start()

    def _on_update_found(self, version, url, changelog, force):
        self._latest_version = version
        self._download_url = url
        self._changelog = changelog
        self.btn_update.setText(f"🔄 Cập nhật v{version}")
        self.btn_update.setVisible(True)
        
        # NẾU CÓ CỜ BẮT BUỘC TỪ SERVER -> ÉP KHÁCH CẬP NHẬT
        if force:
            QMessageBox.critical(
                self.parent, 
                "BẮT BUỘC CẬP NHẬT",
                f"Phát hiện phiên bản mới: v{version}\nĐây là bản cập nhật bắt buộc để tối ưu hệ thống.\n\nPhần mềm sẽ tiến hành tải và cài đặt ngay lập tức!\n\nChi tiết thay đổi: {changelog}"
            )
            self._on_update_clicked(is_forced=True)

    def _on_update_clicked(self, is_forced=False):
        # Nếu không bắt buộc thì mới hỏi ý kiến
        if not is_forced:
            msg = f"Phiên bản mới: v{self._latest_version}\nHiện tại: v{APP_VERSION}\n\n"
            if self._changelog:
                msg += f"Thay đổi:\n{self._changelog}\n\n"
            msg += "Nhấn OK để tải bản mới.\nApp sẽ tự tắt → cập nhật → mở lại."

            if QMessageBox.question(self.parent, "Cập nhật phần mềm", msg,
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
            ) != QMessageBox.StandardButton.Ok:
                return

        # Khóa nút Hủy, ép chạy tiến trình tải
        self.progress = QProgressDialog("Đang tải phiên bản mới...", None, 0, 100, self.parent)
        self.progress.setWindowTitle("Cập nhật AnhStudio")
        self.progress.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress.setCancelButton(None) 
        self.progress.setMinimumDuration(0)
        self.progress.setValue(0)
        self.progress.setStyleSheet("""
            QProgressDialog { background: #1e293b; color: white; }
            QProgressBar { border: 1px solid #374151; border-radius: 6px; background: #111827; text-align: center; color: white; }
            QProgressBar::chunk { background-color: #10b981; border-radius: 5px; }
        """)
        self.progress.show()

        self.btn_update.setEnabled(False)
        self.btn_update.setText("⏳ Đang tải...")

        self._dl_thread = DownloadUpdateThread(self._download_url)
        self._dl_thread.progress_signal.connect(lambda p: (self.progress.setValue(p), self.progress.setLabelText(f"Đang tải... {p}%")))
        self._dl_thread.done_signal.connect(self._on_dl_done)
        self._dl_thread.error_signal.connect(self._on_dl_error)
        self._dl_thread.start()

    def _on_dl_done(self, new_exe_path):
        self.progress.close()
        QMessageBox.information(self.parent, "Sẵn sàng",
            "Tải xong bản mới!\nApp sẽ tự đóng, cập nhật, và mở lại.\nNhấn OK.")
        _apply_update_and_restart(new_exe_path)

    def _on_dl_error(self, error_msg):
        self.progress.close()
        self.btn_update.setEnabled(True)
        self.btn_update.setText(f"🔄 Cập nhật v{self._latest_version}")
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

        self.settings = QSettings("AnhStudio", "HongguoApp")

        login_box = QWidget()
        login_box.setFixedWidth(400)
        login_box.setStyleSheet("background-color: #1e293b; border-radius: 12px; border: 1px solid #334155;")
        box_layout = QVBoxLayout(login_box)
        box_layout.setContentsMargins(30, 40, 30, 40)
        box_layout.setSpacing(15)

        title = QLabel("ĐĂNG NHẬP HỆ THỐNG")
        title.setFont(QFont("Arial", 18, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color: #38bdf8; border: none; margin-bottom: 15px;")
        box_layout.addWidget(title)

        self.inp_user = QLineEdit()
        self.inp_user.setPlaceholderText("Tên đăng nhập")
        self.inp_user.setStyleSheet("padding: 14px; border-radius: 8px; border: 1px solid #475569; background: #0f172a;")
        box_layout.addWidget(self.inp_user)

        self.inp_pass = QLineEdit()
        self.inp_pass.setPlaceholderText("Mật khẩu")
        self.inp_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.inp_pass.setStyleSheet("padding: 14px; border-radius: 8px; border: 1px solid #475569; background: #0f172a;")
        box_layout.addWidget(self.inp_pass)

        saved_user = self.settings.value("username", "")
        saved_pwd = self.settings.value("password", "")
        if saved_user:
            self.inp_user.setText(saved_user)
            self.inp_pass.setText(saved_pwd)

        self.btn_login = QPushButton("Đăng Nhập")
        self.btn_login.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_login.setStyleSheet("""
            QPushButton { padding: 14px; background-color: #2563eb; color: white; border-radius: 8px; font-weight: bold; font-size: 14px; margin-top: 10px; border: none;}
            QPushButton:hover { background-color: #1d4ed8; }
        """)
        self.btn_login.clicked.connect(self._handle_login)
        box_layout.addWidget(self.btn_login)

        self.btn_register = QPushButton("Tạo Tài Khoản Mới")
        self.btn_register.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_register.setStyleSheet("""
            QPushButton { padding: 14px; background-color: #10b981; color: white; border-radius: 8px; font-weight: bold; font-size: 14px; border: none;}
            QPushButton:hover { background-color: #059669; }
        """)
        self.btn_register.clicked.connect(self._handle_register)
        box_layout.addWidget(self.btn_register)

        layout.addWidget(login_box)

    def _handle_login(self):
        user = self.inp_user.text().strip()
        pwd = self.inp_pass.text().strip()
        if not user or not pwd:
            QMessageBox.warning(self, "Lỗi", "Vui lòng nhập đủ thông tin!")
            return

        self.btn_login.setText("Đang kết nối...")
        self.btn_login.setEnabled(False)

        try:
            res = requests.post(f"{SERVER_URL}/api/login", json={"username": user, "password": pwd, "hwid": "may_khach_01"}, timeout=10)
            data = res.json()
            
            if data.get("status") == "success":
                self.settings.setValue("username", user)
                self.settings.setValue("password", pwd)
                self.settings.setValue("auth_token", data.get("token", ""))  # 🔒 Lưu JWT token
                
                self.login_success.emit(user, data.get("expiry", "Vô thời hạn"))
            else:
                QMessageBox.critical(self, "Lỗi", data.get("message", "Đăng nhập thất bại"))
        except Exception as e:
            QMessageBox.critical(self, "Lỗi mạng", f"Không thể kết nối đến Server:\n{e}")
        
        self.btn_login.setText("Đăng Nhập")
        self.btn_login.setEnabled(True)

    def _handle_register(self):
        user = self.inp_user.text().strip()
        pwd = self.inp_pass.text().strip()
        if not user or not pwd:
            QMessageBox.warning(self, "Lỗi", "Vui lòng nhập Tên đăng nhập và Mật khẩu bạn muốn tạo vào 2 ô trên, sau đó bấm Đăng Ký!")
            return

        self.btn_register.setText("Đang xử lý...")
        self.btn_register.setEnabled(False)

        try:
            res = requests.post(f"{SERVER_URL}/api/register", json={"username": user, "password": pwd, "zalo": ""}, timeout=10)
            data = res.json()
            
            if data.get("status") == "success":
                QMessageBox.information(self, "Thành công", data.get("message", "Đăng ký thành công!"))
            else:
                QMessageBox.critical(self, "Lỗi", data.get("message", "Đăng ký thất bại"))
        except Exception as e:
            QMessageBox.critical(self, "Lỗi mạng", f"Không thể kết nối đến Server:\n{e}")
        
        self.btn_register.setText("Tạo Tài Khoản Mới")
        self.btn_register.setEnabled(True)

# ==========================================
# CỬA SỔ CHÍNH
# ==========================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"AnhStudio Client v{APP_VERSION} - Hongguo Downloader")
        self.resize(1050, 780)
        self.setStyleSheet("background-color: #0f172a;")

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.login_screen = LoginScreen()
        self.login_screen.login_success.connect(self.show_main_app)
        self.stack.addWidget(self.login_screen)

    def show_main_app(self, username, expiry):
        main_widget = QWidget()
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(65)
        header.setStyleSheet("background-color: #1e293b; border-bottom: 1px solid #334155;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(25, 0, 25, 0)

        lbl_logo = QLabel("👑 AnhStudio Tool")
        lbl_logo.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        lbl_logo.setStyleSheet("color: #38bdf8;")
        
        lbl_user_info = QLabel(f"👤 Khách hàng: <b>{username}</b>  |  ⏳ Hạn VIP: {expiry}")
        lbl_user_info.setStyleSheet("color: #cbd5e1; font-size: 14px;")

        self.lbl_balance = QLabel("💰 Số dư: --- đ")
        self.lbl_balance.setStyleSheet("color: #10b981; font-size: 14px; font-weight: bold;")

        btn_logout = QPushButton("🚪 Đăng Xuất")
        btn_logout.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_logout.setStyleSheet("""
            QPushButton { padding: 8px 16px; background-color: #ef4444; color: white; border-radius: 6px; font-weight: bold; border: none; }
            QPushButton:hover { background-color: #dc2626; }
        """)
        btn_logout.clicked.connect(self.logout)

        header_layout.addWidget(lbl_logo)
        header_layout.addStretch()
        header_layout.addWidget(lbl_user_info)
        header_layout.addSpacing(20)
        header_layout.addWidget(self.lbl_balance)
        header_layout.addSpacing(20)
        header_layout.addWidget(btn_logout)

        # === AUTO-UPDATER: Tự kiểm tra & hiện nút cập nhật trên header ===
        self.updater = AutoUpdater(header_layout, parent_widget=self)

        main_layout.addWidget(header)

        self.honggou_tab = HonggouWidget(username)
        self.honggou_tab.balance_changed = self._update_balance_display
        main_layout.addWidget(self.honggou_tab)

        self.stack.addWidget(main_widget)
        self.stack.setCurrentWidget(main_widget)
        
        self._fetch_balance(username)
    
    def _fetch_balance(self, username):
        try:
            token = QSettings("AnhStudio", "HongguoApp").value("auth_token", "")
            res = requests.get(f"{SERVER_URL}/api/client/balance/{username}", headers={"Authorization": f"Bearer {token}"}, timeout=5)
            if res.status_code == 200:
                balance = res.json().get("balance", 0)
                self._update_balance_display(balance)
        except Exception:
            pass

    def _update_balance_display(self, balance):
        self.lbl_balance.setText(f"💰 Số dư: {balance:,} đ".replace(",", "."))

    def logout(self):
        reply = QMessageBox.question(self, "Đăng xuất", "Bạn có chắc chắn muốn đăng xuất không?\n(Sẽ xóa thông tin tài khoản đã ghi nhớ)", 
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            settings = QSettings("AnhStudio", "HongguoApp")
            settings.remove("username")
            settings.remove("password")
            
            self.login_screen.inp_user.clear()
            self.login_screen.inp_pass.clear() 
            
            if hasattr(self, 'honggou_tab') and self.honggou_tab.monitor_thread:
                self.honggou_tab.monitor_thread.stop()
            self.stack.setCurrentWidget(self.login_screen)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
