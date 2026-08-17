import os, sys, re, json, threading, subprocess, concurrent.futures, time
import logging
from datetime import datetime
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QListWidget, QListWidgetItem, QTextEdit, QFileDialog, QProgressBar, QFrame, QSplitter, QAbstractItemView, QSizePolicy, QComboBox, QCheckBox)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QSettings, QSize
from PyQt6.QtGui import QTextCursor, QPixmap, QColor, QIcon
from shared_utils import AsyncImageLoader, CREATE_NO_WINDOW, get_ytdlp_path
from cookie_tab import get_cookie_file

def _sanitize(name): 
    return re.sub(r'[<>:"/\\|?*\n\r\t]', '_', name).strip()[:60]

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
                background: #ff0000; border-color: #ff0000;
                image: none;
            }
            QCheckBox::indicator:hover {
                border-color: #ff0000;
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
            
        # --- Thông tin video & Thanh tiến trình ---
        info_lay = QVBoxLayout()
        info_lay.setSpacing(4)
        
        self.title_lbl = QLabel(vid_data.get('desc') or "Không có tiêu đề")
        self.title_lbl.setWordWrap(True)
        self.title_lbl.setStyleSheet("color: #e0e0e0; font-size: 13px; font-weight: 500;")
        self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        info_lay.addWidget(self.title_lbl)
        
        author = vid_data.get('author', 'YouTubeChannel')
        status_text = f"👤 {author}   |   🆔 {vid_data.get('id')}"
        self.id_lbl = QLabel(status_text)
        self.id_lbl.setStyleSheet("color: #666666; font-size: 11px;")
        info_lay.addWidget(self.id_lbl)
        
        self.pbar = QProgressBar()
        self.pbar.setFixedHeight(4)
        self.pbar.setTextVisible(False)
        self.pbar.setStyleSheet("""
            QProgressBar { background: transparent; border: none; }
            QProgressBar::chunk { background: #ff0000; border-radius: 2px; }
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
# SCAN THREAD — Quét danh sách video YouTube 
# ============================================================
class YouTubeScanThread(QThread):
    log = pyqtSignal(str)
    user_log = pyqtSignal(str)
    video_found = pyqtSignal(dict)
    finished_signal = pyqtSignal(int)
    
    def __init__(self, url, cookie_file):
        super().__init__()
        self.url = url
        self.cookie_file = cookie_file
        self._cancel = False
        
    def cancel(self):
        self._cancel = True
        
    def run(self):
        url = self.url if self.url.startswith("http") else "https://" + self.url
        self.log.emit(f"🚀 QUÉT YOUTUBE: {url}\n")
        self.user_log.emit(f"🔍 Đang quét YouTube...\n")
        res = []
        cmd = [get_ytdlp_path(), "--flat-playlist", "--dump-json", "--no-warnings"]
        
        if self.cookie_file and os.path.exists(self.cookie_file):
            cmd.extend(["--cookies", self.cookie_file])
        cmd.append(url)
        
        kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **kw)
        
        for line in proc.stdout:
            if self._cancel:
                proc.terminate()
                break
            try:
                it = json.loads(line.strip())
                vid_id = str(it.get("id") or "")
                if vid_id and vid_id not in [x["id"] for x in res]:
                    author = str(it.get("uploader") or it.get("channel") or "YouTubeChannel")
                    thumbs = it.get("thumbnails", [])
                    cover = thumbs[-1]["url"] if thumbs else ""
                    v = {
                        "id": vid_id,
                        "url": f"https://www.youtube.com/watch?v={vid_id}",
                        "platform": "youtube",
                        "desc": it.get("title",""),
                        "author": author,
                        "cover_url": cover
                    }
                    res.append(v)
                    self.video_found.emit(v)
                    self.log.emit(f"🔎 Quét thấy: {vid_id}\n")
                    if len(res) % 5 == 0: self.user_log.emit(f"📦 Đã tìm thấy {len(res)} video...\n")
            except:
                continue
                
        proc.wait(timeout=15)
        self.log.emit(f"🏁 TỔNG KẾT: {len(res)} video.\n")
        self.user_log.emit(f"🏁 Hoàn tất — Tổng cộng {len(res)} video\n")
        self.finished_signal.emit(len(res))

# ============================================================
# DOWNLOAD THREAD — Tải video YouTube 
# ============================================================
class YouTubeDownloadThread(QThread):
    log = pyqtSignal(str)
    user_log = pyqtSignal(str)
    total_progress = pyqtSignal(int, int)
    card_progress = pyqtSignal(str, int)
    
    def __init__(self, videos, save_dir, cookie_file, resolution_mode, thread_count=3):
        super().__init__()
        self.videos = videos
        self.save_dir = save_dir
        self.cookie_file = cookie_file
        self.resolution_mode = resolution_mode
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
        self.log.emit(f"\n📥 BẮT ĐẦU TẢI {total} VIDEO YOUTUBE (Độ phân giải: {self.resolution_mode})...\n")
        self.user_log.emit(f"📥 Bắt đầu tải {total} video ({self.resolution_mode}, {self.thread_count} luồng)...\n")
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.thread_count) as ex:
            futs = {ex.submit(self._dl_worker, v, i, total): v for i, v in enumerate(self.videos, 1)}
            concurrent.futures.wait(futs)
        self.log.emit(f"🎉 HOÀN TẤT TẢI: {self.success_count}/{total} video.\n")
        self.user_log.emit(f"🎉 Hoàn tất: {self.success_count}/{total} tải thành công\n")

    def _format_for_res(self):
        # Ánh xạ chế độ độ phân giải -> chuỗi format của yt-dlp.
        # QUAN TRỌNG: ưu tiên codec H.264 (avc1) + AAC (m4a) để file mp4
        # xem trước / render / import vào editor được ở MỌI nơi.
        # YouTube 'bestvideo' thường là VP9/AV1 + Opus -> nhét vào .mp4 sẽ
        # tạo file mp4 "giả", Windows và nhiều editor không đọc được.
        # Chuỗi format: ưu tiên avc1+m4a; nếu không có mới lùi về best thường.
        def _f(h):
            return (
                f"bestvideo[height<={h}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
                f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
                f"best[height<={h}][ext=mp4]/"
                f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
            )
        m = {
            "4K":    _f(2160),
            "2K":    _f(1440),
            "1080p": _f(1080),
            "720p":  _f(720),
            "480p":  _f(480),
        }
        return m.get(self.resolution_mode,
                     "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo+bestaudio/best")

    def _dl_worker(self, vid, idx, tot):
        self.pause_event.wait()
        if self._cancel:
            return False

        vid_id = str(vid.get("id", ""))
        desc = _sanitize(vid.get("desc", "") or "")
        author = _sanitize(vid.get("author", "YouTubeChannel") or "YouTubeChannel")
        user_dir = os.path.join(self.save_dir, "YouTubeDownload", author)
        os.makedirs(user_dir, exist_ok=True)

        base_name = f"{desc} [{vid_id}]" if desc else f"{vid_id}"
        outtmpl = os.path.join(user_dir, f"{base_name}.%(ext)s")

        self.log.emit(f"[{idx}/{tot}] ⬇️ Bắt đầu tải: {vid_id}\n")
        self.card_progress.emit(vid_id, -1)

        cmd = [
            get_ytdlp_path(),
            "-f", self._format_for_res(),
            "--merge-output-format", "mp4",
            # Ép MỌI video về H.264 + AAC (mp4 chuẩn) để xem trước / render /
            # import editor được 100%, kể cả video gốc là VP9/AV1/Opus.
            "--recode-video", "mp4",
            "--postprocessor-args", "VideoConvertor:-c:v libx264 -preset veryfast -crf 20 -c:a aac -b:a 192k -movflags +faststart",
            "-o", outtmpl,
            "--no-warnings", "--no-playlist",
            "--newline",
            "--progress-template", "download:PCT %(progress._percent_str)s",
        ]
        if self.cookie_file and os.path.exists(self.cookie_file):
            cmd += ["--cookies", self.cookie_file]
        cmd.append(vid["url"])

        success = False
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            if self._cancel:
                break
            self.pause_event.wait()
            try:
                kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", **kw
                )
                last_err_line = ""
                for line in proc.stdout:
                    if self._cancel:
                        proc.terminate()
                        break
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("PCT"):
                        try:
                            pct = int(float(line.replace("PCT", "").replace("%", "").strip()))
                            self.card_progress.emit(vid_id, pct)
                        except ValueError:
                            pass
                    elif "ERROR" in line.upper():
                        last_err_line = line
                        self.log.emit(f"   {line}\n")
                proc.wait()
                if proc.returncode == 0:
                    success = True
                    self.card_progress.emit(vid_id, 100)
                    break
                else:
                    if attempt < max_retries:
                        self.log.emit(f"⚠️ [{idx}] Lỗi tải (thử lại {attempt}/{max_retries}) | {last_err_line[:120]}\n")
                        time.sleep(2)
                    else:
                        self.log.emit(f"❌ [{idx}] Thất bại hoàn toàn: {vid_id} | {last_err_line[:200]}\n")
            except Exception as e:
                err_msg = str(e).replace("\n", " ").strip()
                if attempt < max_retries:
                    self.log.emit(f"⚠️ [{idx}] Lỗi tải (thử lại {attempt}/{max_retries}) | {err_msg[:120]}\n")
                    time.sleep(2)
                else:
                    self.log.emit(f"❌ [{idx}] Thất bại hoàn toàn: {vid_id} | {err_msg}\n")

        with self.lock:
            self.done_count += 1
            if success:
                self.success_count += 1
                self.log.emit(f"✅ [XONG] {vid_id} -> {self.success_count}/{tot}\n")
                self.user_log.emit(f"✅ Tải xong ({self.success_count}/{tot}): {vid_id}\n")
            else:
                self.log.emit(f"❌ [BỎ QUA] {vid_id} do lỗi.\n")
                self.user_log.emit(f"❌ Lỗi tải: {vid_id}\n")
            self.total_progress.emit(self.done_count, tot)

        return success

# ============================================================
# YOUTUBE WIDGET — Giao diện đồng bộ
# ============================================================
class YouTubeWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scanned = []
        self.settings = QSettings("AnhStudio", "YouTube")
        
        # ==============================================================
        # 1. CẤU HÌNH NHẬN DIỆN THƯƠNG HIỆU
        # ==============================================================
        THEME_COLOR = "#ff0000"  
        HOVER_COLOR = "#cc0000"
        PLACEHOLDER_TEXT = "🔗 Dán link YouTube kênh, playlist, hoặc video lẻ vào đây..."
        PLATFORM_NAME = "YouTube"
        
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
        
        # --- Chỉnh sửa: Thêm Box chọn độ phân giải và Nút tải xuống ---
        dl_layout = QHBoxLayout()
        
        self.cb_resolution = QComboBox()
        self.cb_resolution.addItems(["Tự động (Cao nhất)", "4K (2160p)", "2K (1440p)", "Full HD (1080p)", "HD (720p)", "SD (480p)"])
        self.cb_resolution.setMinimumHeight(45)
        self.cb_resolution.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cb_resolution.setStyleSheet(f"""
            QComboBox {{ 
                background-color: #0f0f0f; border: 1px solid #2a2a2a; 
                border-radius: 8px; color: #fff; padding-left: 10px; font-weight: bold;
            }}
            QComboBox:focus {{ border: 1px solid {THEME_COLOR}; }}
            QComboBox::drop-down {{ border: none; }}
            QComboBox QAbstractItemView {{
                background-color: #0f0f0f; color: #fff; 
                selection-background-color: {THEME_COLOR}; border: 1px solid #2a2a2a;
            }}
        """)
        dl_layout.addWidget(self.cb_resolution, stretch=1)
        
        
        
        from PyQt6.QtWidgets import QSpinBox
        lbl_thread = QLabel("Luồng:")
        lbl_thread.setStyleSheet("color: #888; font-size: 11px; font-weight: bold;")
        dl_layout.addWidget(lbl_thread)
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 10)
        self.spin_threads.setValue(3)
        self.spin_threads.setFixedSize(55, 35)
        self.spin_threads.setToolTip("Số luồng tải đồng thời (1-10)")
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
        dl_layout.addWidget(self.btn_dl_sel, stretch=2)
        
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
        self.lbl_queue_count.setStyleSheet(f"color: {THEME_COLOR}; border: 1px solid {THEME_COLOR}; background: rgba(255,0,0,0.1); padding: 3px 15px; border-radius: 6px; font-weight: bold; font-size: 12px;")
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
        author = _sanitize(vid_data.get("author", "YouTubeChannel"))
        user_dir = os.path.join(save_dir, "YouTubeDownload", author)
        if not os.path.isdir(user_dir): return False
        for f in os.listdir(user_dir):
            if vid_id in f and (f.endswith(".mp4") or f.endswith(".mkv") or f.endswith(".webm")): 
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
        
        # Banner đang xử lý
        self.status_banner.setText("⏳ Đang phân tích dữ liệu, vui lòng đợi...")
        self.status_banner.setStyleSheet("background-color: rgba(255, 0, 0, 0.1); border: 1px solid #ff0000; border-radius: 8px; padding: 15px; font-size: 14px; font-weight: bold; color: #ff0000;")
        
        self._scan_thread = YouTubeScanThread(url, get_cookie_file("youtube"))
        self._scan_thread.log.connect(self._write_hidden_log) # Bắt log kỹ thuật ẩn
        self._scan_thread.user_log.connect(self._user_log)
        self._scan_thread.video_found.connect(self._add_video_card)
        self._scan_thread.finished_signal.connect(self._on_scan_finished)
        self._scan_thread.start()
        
    def _on_scan_finished(self, count):
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("Quét Dữ Liệu")
        if count > 0:
            self.status_banner.setText(f"✅ Quét thành công: Phát hiện {count} video")
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
        
        # Xác định độ phân giải được chọn
        res_text = self.cb_resolution.currentText()
        res_mode = "Auto"
        if "4K" in res_text: res_mode = "4K"
        elif "2K" in res_text: res_mode = "2K"
        elif "1080p" in res_text: res_mode = "1080p"
        elif "720p" in res_text: res_mode = "720p"
        elif "480p" in res_text: res_mode = "480p"
        
        self._dl_thread = YouTubeDownloadThread(vids, self.dir_input.text().strip(), get_cookie_file("youtube"), res_mode, self.spin_threads.value())
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
