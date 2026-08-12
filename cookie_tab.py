import os, time
from http.cookiejar import MozillaCookieJar
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
    QLineEdit, QPushButton, QFileDialog, QFrame)
from PyQt6.QtCore import Qt, QSettings, QThread, pyqtSignal

# Màu chủ đạo cho từng platform (Không cần link test nữa)
PLATFORMS = {
    "youtube":      {"name": "YouTube",      "icon": "🔴", "color": "#FF0000"},
    "tiktok":       {"name": "TikTok",       "icon": "🎵", "color": "#00F2EA"},
    "douyin":       {"name": "Douyin",       "icon": "🎶", "color": "#FE2C55"},
    "bilibili":     {"name": "Bilibili",     "icon": "📺", "color": "#00A1D6"},
    "facebook":     {"name": "Facebook",     "icon": "🔵", "color": "#1877F2"},
    "x":            {"name": "X (Twitter)",  "icon": "✖", "color": "#FFFFFF"},
}

def get_cookie_file(platform):
    """Hàm tiện ích để các tab khác lấy đường dẫn cookie."""
    return QSettings("BoomStudio", "Cookies").value(f"{platform}_cookie", "")


class CookieCheckThread(QThread):
    """Thuật toán PRO: Đọc trực tiếp file thay vì ping mạng"""
    result = pyqtSignal(str, bool, str)  # platform, ok, message
    
    def __init__(self, platform, cookie_file):
        super().__init__()
        self.platform = platform
        self.cookie_file = cookie_file
    
    def run(self):
        if not self.cookie_file or not os.path.exists(self.cookie_file):
            self.result.emit(self.platform, False, "File không tồn tại")
            return
            
        try:
            # 1. Đọc file bằng thư viện chuẩn Netscape của Python
            jar = MozillaCookieJar(self.cookie_file)
            jar.load(ignore_discard=True, ignore_expires=True)
            
            # 2. Các "Chìa khóa" nhận diện tài khoản cho từng nền tảng
            auth_keys = {
                "youtube": ["SSID", "SAPISID", "LOGIN_INFO"],
                "tiktok": ["sessionid"],
                "douyin": ["sessionid", "sessionid_ss"],
                "bilibili": ["SESSDATA"],
                "facebook": ["c_user", "xs"],
                "x": ["auth_token"],
            }
            
            required = auth_keys.get(self.platform, [])
            current_time = int(time.time())
            
            found_valid = False
            err_msg = "Chưa đăng nhập (Thiếu key chứng thực)"
            
            # 3. Quét tìm chìa khóa trong file
            for cookie in jar:
                if cookie.name in required:
                    # Kiểm tra xem cookie đã hết hạn thời gian chưa
                    if cookie.expires and cookie.expires < current_time:
                        err_msg = f"Cookie '{cookie.name}' đã HẾT HẠN!"
                    else:
                        found_valid = True
                        break
                        
            if found_valid:
                self.result.emit(self.platform, True, "Cookie hợp lệ và đang sống!")
            else:
                self.result.emit(self.platform, False, err_msg)
                
        except Exception:
            self.result.emit(self.platform, False, "Sai định dạng chuẩn Netscape")


class PlatformRow(QFrame):
    """Một hàng quản lý cookie cho 1 nền tảng."""
    def __init__(self, platform_key, info, parent=None):
        super().__init__(parent)
        self.platform_key = platform_key
        self.info = info
        self.settings = QSettings("BoomStudio", "Cookies")
        self._check_thread = None
        
        color = info["color"]
        self.setStyleSheet(f"""
            PlatformRow {{
                background: #1e222d;
                border: 1px solid #2d3342;
                border-left: 4px solid {color};
                border-radius: 8px;
            }}
        """)
        self.setFixedHeight(70)
        
        lay = QHBoxLayout(self)
        lay.setContentsMargins(15, 8, 15, 8)
        lay.setSpacing(10)
        
        name_lbl = QLabel(f"{info['icon']}  {info['name']}")
        name_lbl.setFixedWidth(130)
        name_lbl.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 14px; border: none;")
        lay.addWidget(name_lbl)

        # Douyin dùng ĐĂNG NHẬP (lưu session JSON) thay cho dán file cookies.txt
        self.is_login_mode = (platform_key == "douyin")
        if self.is_login_mode:
            self._build_login_row(lay, color)
            return
        self._build_file_row(lay, color)

    # ---- Hàng kiểu ĐĂNG NHẬP (chỉ Douyin) ----
    def _build_login_row(self, lay, color):
        self._login_thread = None

        self.login_status = QLabel("Đang kiểm tra...")
        self.login_status.setStyleSheet("color:#aaa; font-size:12px; border:none;")
        lay.addWidget(self.login_status, 1)

        self.btn_login = QPushButton()
        self.btn_login.setFixedSize(150, 36)
        self.btn_login.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_login.clicked.connect(self._do_login)
        lay.addWidget(self.btn_login)

        self.btn_logout = QPushButton("🚪 Đăng xuất")
        self.btn_logout.setFixedSize(100, 36)
        self.btn_logout.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_logout.setStyleSheet(
            "QPushButton { background:#37202a; color:#ff9a9a; border-radius:6px; font-size:11px; }"
            "QPushButton:hover { background:#4a2730; }")
        self.btn_logout.clicked.connect(self._do_logout)
        lay.addWidget(self.btn_logout)

        self.status_dot = QLabel("⚫")
        self.status_dot.setFixedWidth(30)
        self.status_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_dot.setStyleSheet("font-size: 18px; border: none;")
        lay.addWidget(self.status_dot)

        self._refresh_login_state()

    def _douyin_logged_in(self):
        try:
            from douyin_tab import _douyin_logged_in as _chk
            return _chk()
        except Exception:
            return False

    def _refresh_login_state(self):
        ok = self._douyin_logged_in()
        if ok:
            self.login_status.setText("Đã đăng nhập Douyin")
            self.login_status.setStyleSheet("color:#4caf50; font-size:12px; border:none;")
            self.btn_login.setText("🔄 Đăng nhập lại")
            self.btn_login.setStyleSheet(
                "QPushButton { background:#2d3342; color:#ddd; border-radius:6px; font-size:11px; font-weight:bold; }"
                "QPushButton:hover { background:#3d4454; }")
            self.btn_logout.setVisible(True)
            self.status_dot.setText("🟢"); self.status_dot.setToolTip("Đã đăng nhập")
        else:
            self.login_status.setText("Chưa đăng nhập — bấm để đăng nhập Douyin")
            self.login_status.setStyleSheet("color:#aaa; font-size:12px; border:none;")
            self.btn_login.setText("🔑 Đăng nhập")
            self.btn_login.setStyleSheet(
                "QPushButton { background:#7c3aed; color:white; border-radius:6px; font-size:11px; font-weight:bold; }"
                "QPushButton:hover { background:#6d28d9; }")
            self.btn_logout.setVisible(False)
            self.status_dot.setText("⚫"); self.status_dot.setToolTip("Chưa đăng nhập")

    def _do_login(self):
        self.btn_login.setEnabled(False)
        self.btn_login.setText("⏳ Đang mở...")
        self.login_status.setText("Đang mở trình duyệt, hãy đăng nhập Douyin...")
        try:
            from douyin_tab import DouyinLoginThread
        except Exception as e:
            self.login_status.setText(f"Lỗi nạp module đăng nhập: {e}")
            self.btn_login.setEnabled(True)
            self._refresh_login_state()
            return
        self._login_thread = DouyinLoginThread()
        self._login_thread.log.connect(self._on_login_log)
        self._login_thread.finished_signal.connect(self._on_login_done)
        self._login_thread.start()

    def _on_login_log(self, msg):
        self.login_status.setText(msg.strip().splitlines()[-1] if msg.strip() else "...")

    def _on_login_done(self, ok):
        self.btn_login.setEnabled(True)
        self._refresh_login_state()

    def _do_logout(self):
        try:
            from douyin_tab import DOUYIN_AUTH_FILE
            if os.path.exists(DOUYIN_AUTH_FILE):
                os.remove(DOUYIN_AUTH_FILE)
        except Exception:
            pass
        self._refresh_login_state()

    # ---- Hàng kiểu DÁN FILE (các nền tảng còn lại) ----
    def _build_file_row(self, lay, color):
        self.path_input = QLineEdit(self.settings.value(f"{self.platform_key}_cookie", ""))
        self.path_input.setPlaceholderText("Chọn hoặc dán đường dẫn file cookies.txt...")
        self.path_input.setStyleSheet("""
            QLineEdit {
                background: #0f1219; color: #ffffff; padding: 8px;
                border: 1px solid #2d3342; border-radius: 6px; font-size: 12px;
            }
            QLineEdit:focus { border: 1px solid #ff9800; }
        """)
        self.path_input.textChanged.connect(self._save_path)
        lay.addWidget(self.path_input)
        
        btn_browse = QPushButton("📂")
        btn_browse.setFixedSize(36, 36)
        btn_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_browse.setStyleSheet("QPushButton { background: #2d3342; border-radius: 6px; font-size: 14px; } QPushButton:hover { background: #3d4454; }")
        btn_browse.clicked.connect(self._browse)
        lay.addWidget(btn_browse)
        
        self.btn_check = QPushButton("🔍 Check")
        self.btn_check.setFixedSize(80, 36)
        self.btn_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_check.setStyleSheet("QPushButton { background: #ff9800; color: white; font-weight: bold; border-radius: 6px; font-size: 11px; } QPushButton:hover { background: #ffb74d; }")
        self.btn_check.clicked.connect(self._check)
        lay.addWidget(self.btn_check)
        
        self.status_dot = QLabel("⚫")
        self.status_dot.setFixedWidth(30)
        self.status_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_dot.setStyleSheet("font-size: 18px; border: none;")
        lay.addWidget(self.status_dot)
        
        if self.path_input.text().strip():
            self.status_dot.setText("🟡")
            self.status_dot.setToolTip("Chưa kiểm tra")
    
    def _save_path(self, text):
        self.settings.setValue(f"{self.platform_key}_cookie", text.strip())
        if text.strip():
            self.status_dot.setText("🟡")
            self.status_dot.setToolTip("Chưa kiểm tra")
        else:
            self.status_dot.setText("⚫")
            self.status_dot.setToolTip("")
    
    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, f"Chọn Cookie {self.info['name']}", "", "Text (*.txt);;All (*)")
        if path:
            self.path_input.setText(path)
    
    def _check(self):
        path = self.path_input.text().strip()
        if not path:
            self.status_dot.setText("🔴")
            self.status_dot.setToolTip("Chưa chọn file")
            return
        
        self.btn_check.setEnabled(False)
        self.btn_check.setText("⏳...")
        self.status_dot.setText("🟡")
        self.status_dot.setToolTip("Đang kiểm tra...")
        
        self._check_thread = CookieCheckThread(self.platform_key, path)
        self._check_thread.result.connect(self._on_check_result)
        self._check_thread.start()
    
    def _on_check_result(self, platform, ok, msg):
        self.btn_check.setEnabled(True)
        self.btn_check.setText("🔍 Check")
        if ok:
            self.status_dot.setText("🟢")
            self.status_dot.setToolTip(f"✅ {msg}")
        else:
            self.status_dot.setText("🔴")
            self.status_dot.setToolTip(f"❌ {msg}")


class CookieWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        
        main = QVBoxLayout(self)
        main.setContentsMargins(20, 20, 20, 20)
        main.setSpacing(20)
        
        header = QLabel("🍪 QUẢN LÝ COOKIE TẬP TRUNG")
        header.setStyleSheet("color: #ff9800; font-size: 20px; font-weight: bold; letter-spacing: 2px;")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main.addWidget(header)
        
        desc = QLabel("Dán đường dẫn file cookies.txt cho mỗi nền tảng → nhấn Check để kiểm tra.\n"
                       "Riêng Douyin: bấm 🔑 Đăng nhập để lưu phiên (không cần file).\n"
                       "🟢 Hoạt động tốt   🟡 Chưa kiểm tra   🔴 Hết hạn / Lỗi   ⚫ Chưa thiết lập")
        desc.setStyleSheet("color: #888; font-size: 12px;")
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setWordWrap(True)
        main.addWidget(desc)
        
        self.rows = {}
        for key, info in PLATFORMS.items():
            row = PlatformRow(key, info)
            self.rows[key] = row
            main.addWidget(row)
        
        btn_row = QHBoxLayout()
        btn_check_all = QPushButton("🔍 CHECK TẤT CẢ")
        btn_check_all.setMinimumHeight(45)
        btn_check_all.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_check_all.setStyleSheet("""
            QPushButton {
                background: #ff9800; color: white; font-size: 14px;
                font-weight: bold; border-radius: 8px; letter-spacing: 1px;
            }
            QPushButton:hover { background: #ffb74d; }
        """)
        btn_check_all.clicked.connect(self._check_all)
        btn_row.addWidget(btn_check_all)
        
        btn_clear_all = QPushButton("🗑️ XÓA TẤT CẢ")
        btn_clear_all.setMinimumHeight(45)
        btn_clear_all.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_clear_all.setStyleSheet("""
            QPushButton {
                background: #f44336; color: white; font-size: 14px;
                font-weight: bold; border-radius: 8px; letter-spacing: 1px;
            }
            QPushButton:hover { background: #e57373; }
        """)
        btn_clear_all.clicked.connect(self._clear_all)
        btn_row.addWidget(btn_clear_all)
        
        main.addLayout(btn_row)
        
        guide = QLabel(
            "💡 Cách lấy Cookie:\n"
            "1. Cài extension \"Get cookies.txt LOCALLY\" trên Chrome/Edge\n"
            "2. Đăng nhập vào nền tảng (YouTube, TikTok, Douyin...)\n"
            "3. Nhấn icon extension → Export → Lưu file cookies.txt\n"
            "4. Dán đường dẫn file vào ô trên"
        )
        guide.setStyleSheet("color: #666; font-size: 11px; padding: 10px; background: #0f1219; border-radius: 8px;")
        guide.setWordWrap(True)
        main.addWidget(guide)
        
        main.addStretch()
    
    def _check_all(self):
        for row in self.rows.values():
            if getattr(row, "is_login_mode", False):
                continue
            if row.path_input.text().strip():
                row._check()
    
    def _clear_all(self):
        for row in self.rows.values():
            if getattr(row, "is_login_mode", False):
                continue
            row.path_input.clear()
