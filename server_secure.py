# -*- coding: utf-8 -*-
# ==========================================
# 🔒 SERVER ANHSTUDIO — PHIÊN BẢN BẢO MẬT
# ==========================================
# Thay đổi so với bản cũ:
#   1. Admin Dashboard có trang đăng nhập, tất cả API admin cần token
#   2. Client API dùng JWT token sau khi login (Bearer token)
#   3. Mật khẩu hash bằng bcrypt, tự động migrate user cũ
#   4. Rate limiting cho login/register
#   5. CORS khóa chặt, Worker secret đọc từ biến môi trường
#
# Cài thêm: pip install PyJWT bcrypt
# Biến môi trường (tuỳ chọn, có giá trị mặc định):
#   ADMIN_PASSWORD, JWT_SECRET, WORKER_SECRET, DB_PASSWORD
# ==========================================

from fastapi import FastAPI, Request, HTTPException, Query, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import datetime
import time
import re
import json
import secrets
import mysql.connector
from mysql.connector import pooling, Error, IntegrityError
import uvicorn
import asyncio
from contextlib import asynccontextmanager
import httpx
import concurrent.futures
import os
from collections import defaultdict
from googleapiclient.discovery import build

# ==========================================
# 🔑 CẤU HÌNH BẢO MẬT — ĐỌC TỪ BIẾN MÔI TRƯỜNG
# ==========================================
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "AnhStudio@Admin2024!CHANGE_ME")
JWT_SECRET = os.environ.get("JWT_SECRET") or secrets.token_hex(32)
ADMIN_SESSION_SECRET = os.environ.get("ADMIN_SESSION_SECRET") or secrets.token_hex(16)
WORKER_SECRET = os.environ.get("WORKER_SECRET", "anhstudio_secret_key")

# JWT Token hết hạn sau 24 giờ
JWT_EXPIRY_HOURS = 24

# ==========================================
# 🛡️ RATE LIMITER — CHỐNG BRUTE-FORCE
# ==========================================
class RateLimiter:
    def __init__(self):
        self._requests = defaultdict(list)

    def is_allowed(self, key: str, max_requests: int, window_seconds: int) -> bool:
        now = time.time()
        self._requests[key] = [t for t in self._requests[key] if t > now - window_seconds]
        if len(self._requests[key]) >= max_requests:
            return False
        self._requests[key].append(now)
        return True

    def cleanup(self):
        """Xóa các key cũ hơn 10 phút để tránh rò rỉ RAM."""
        now = time.time()
        stale = [k for k, v in self._requests.items() if not v or v[-1] < now - 600]
        for k in stale:
            del self._requests[k]

rate_limiter = RateLimiter()

# ==========================================
# 🔐 HELPER: MẬT KHẨU (BCRYPT)
# ==========================================
import bcrypt as _bcrypt

def hash_password(plain: str) -> str:
    return _bcrypt.hashpw(plain.encode('utf-8'), _bcrypt.gensalt()).decode('utf-8')

def verify_password(plain: str, stored: str) -> tuple:
    """Trả về (match: bool, needs_migration: bool).
    Nếu stored chưa phải bcrypt hash → so sánh plaintext, báo cần migrate."""
    if stored.startswith("$2b$") or stored.startswith("$2a$"):
        return _bcrypt.checkpw(plain.encode('utf-8'), stored.encode('utf-8')), False
    else:
        # Plaintext cũ — so sánh trực tiếp, báo cần migrate
        return (plain == stored), True

# ==========================================
# 🔐 HELPER: JWT TOKEN
# ==========================================
import jwt as _jwt

def create_client_token(username: str, platform: str) -> str:
    payload = {
        "sub": username,
        "platform": platform,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=JWT_EXPIRY_HOURS),
        "iat": datetime.datetime.utcnow(),
        "type": "client"
    }
    return _jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def create_admin_token() -> str:
    payload = {
        "role": "admin",
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=12),
        "iat": datetime.datetime.utcnow(),
        "type": "admin"
    }
    return _jwt.encode(payload, ADMIN_SESSION_SECRET, algorithm="HS256")

def decode_client_token(token: str) -> dict:
    return _jwt.decode(token, JWT_SECRET, algorithms=["HS256"])

def decode_admin_token(token: str) -> dict:
    return _jwt.decode(token, ADMIN_SESSION_SECRET, algorithms=["HS256"])

# ==========================================
# 🔐 FASTAPI DEPENDENCIES — XÁC THỰC
# ==========================================
async def require_client(request: Request) -> dict:
    """Bắt buộc mọi endpoint client phải có JWT token hợp lệ."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Thiếu token xác thực. Vui lòng đăng nhập lại.")
    token = auth[7:]
    try:
        payload = decode_client_token(token)
        if payload.get("type") != "client":
            raise HTTPException(status_code=401, detail="Token không hợp lệ.")
        return payload
    except _jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Phiên đăng nhập đã hết hạn. Vui lòng đăng nhập lại.")
    except Exception:
        raise HTTPException(status_code=401, detail="Token không hợp lệ.")

async def require_admin(request: Request):
    """Bắt buộc mọi endpoint admin phải có admin token."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Admin "):
        token = auth[6:]
        try:
            payload = decode_admin_token(token)
            if payload.get("role") == "admin":
                return True
        except Exception:
            pass
    raise HTTPException(status_code=401, detail="Bạn cần đăng nhập Admin.")

async def require_worker(request: Request):
    """Xác thực Worker bằng secret key."""
    if request.headers.get("Authorization") != WORKER_SECRET:
        raise HTTPException(status_code=401, detail="Worker không được xác thực.")
    return True

def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

# ==========================================
# ⚡ BỘ TỪ ĐIỂN DỊCH THỂ LOẠI TỰ ĐỘNG
# ==========================================
GENRE_DICT = {
    "现代": "Hiện đại", "都市日常": "Đô thị", "都市": "Đô thị", "古代": "Cổ đại", "乡村": "Nông thôn",
    "年代": "Niên đại", "架空": "Giả tưởng", "职场": "Công sở", "民国": "Dân quốc",
    "校园": "Vườn trường", "宫廷": "Cung đình", "荒岛": "Đảo hoang", "古风": "Cổ phong",
    "爽文": "Sảng văn", "成长": "Trưởng thành", "脑洞": "Độc lạ", "奇幻": "Kỳ ảo",
    "玄幻": "Huyền huyễn", "古言": "Cổ ngôn", "战神": "Chiến thần", "宫斗": "Cung đấu",
    "宅斗": "Trạch đấu", "仙侠": "Tiên hiệp", "权谋": "Quyền mưu", "种田": "Điền văn",
    "爱情": "Tình yêu", "悬疑": "Hồi hộp", "喜剧": "Hài hước", "青春": "Thanh xuân",
    "虐恋": "Ngược luyến", "灵异": "Linh dị", "家国情怀": "Tình quốc gia", "法律": "Pháp luật",
    "刑侦": "Hình sự", "抗战": "Kháng chiến", "武侠": "Võ hiệp", "传奇": "Truyền kỳ",
    "求生": "Sinh tồn", "动作": "Hành động", "科幻": "Viễn tưởng", "恐怖": "Kinh dị", "商战": "Thương chiến",
    "打脸": "Vả mặt", "虐渣": "Ngược tra", "反击": "Phản công", "大男主": "Nam chủ",
    "大女主": "Nữ chủ", "马甲文": "Giấu nghề", "马甲": "Giấu nghề", "重生逆袭": "Trọng sinh nghịch tập",
    "重生": "Trọng sinh", "穿越": "Xuyên không", "系统": "Hệ thống", "先婚后爱": "Cưới trước yêu sau",
    "闪婚": "Cưới chớp nhoáng", "家长里短": "Gia đình", "小人物": "Nhân vật nhỏ", "破镜重圆": "Gương vỡ lại lành",
    "神豪": "Thần hào", "豪门": "Hào môn", "黑化归来": "Hắc hóa trở về", "回归": "Trở về", "异能": "Dị năng",
    "传承": "Truyền thừa", "觉醒": "Giác ngộ", "医生": "Bác sĩ", "强强": "Cường cường",
    "替身": "Thế thân", "逆袭": "Nghịch tập", "翻身": "Lật kèo", "甜宠": "Ngọt sủng",
    "宠妻": "Sủng thê", "护妻": "Bảo vệ vợ", "娱乐圈": "Giới giải trí", "神医": "Thần y",
    "青梅竹马": "Thanh mai trúc mã", "姐弟恋": "Tình chị em", "玄学": "Huyền học",
    "娇妻": "Kiều thê", "傲娇": "Ngạo kiều", "精英": "Tinh anh", "一见钟情": "Nhất kiến chung tình",
    "日久生情": "Lâu ngày sinh tình", "萌宝": "Manh bảo", "带娃": "Mang thai",
    "扮猪吃虎": "Giả heo ăn hổ", "反派": "Phản diện", "黑化": "Hắc hóa", "萌宠": "Manh sủng",
    "双向救赎": "Song hướng cứu rỗi", "白月光": "Bạch nguyệt quang", "灵魂互换": "Hoán đổi linh hồn",
    "病娇": "Bệnh kiều", "暴富": "Đổi đời", "黑道": "Hắc đạo", "丧尸": "Zombie",
    "特种兵": "Đặc chủng binh", "婆媳": "Mẹ chồng nàng dâu", "反转": "Cú lừa (Plot twist)",
    "复仇": "Báo thù", "赘婿": "Ở rể", "真相大白": "Sự thật", "男频": "Nam tần",
    "女频": "Nữ tần", "短剧": "Phim ngắn"
}

def extract_and_translate_genres(html):
    genres = []
    json_match = re.search(r'window\._ROUTER_DATA\s*=\s*(\{.+?\})\s*;?\s*</script>', html, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            tags = data.get("loaderData", {}).get("detail_page", {}).get("seriesDetail", {}).get("tags", [])
            for tag in tags:
                name = tag.get("name", "") if isinstance(tag, dict) else str(tag)
                if name: genres.append(name)
        except: pass
    if not genres:
        matches = re.findall(r'<span[^>]*class="[^"]*pc-tag-text[^"]*"[^>]*>([^<]+)</span>', html)
        if matches: genres = matches
    translated = []
    for g in genres:
        g = g.strip()
        g = re.sub(r'<[^>]+>', '', g).replace('>', '')
        if g in GENRE_DICT: translated.append(GENRE_DICT[g])
        else:
            for cn_key, vn_val in GENRE_DICT.items():
                if cn_key in g:
                    translated.append(vn_val)
                    break
    return ", ".join(list(dict.fromkeys(translated)))

# ==========================================
# CẤU HÌNH CHUNG
# ==========================================
APP_VERSION = "1.0.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SQLITE_PATH = os.path.join(BASE_DIR, "database_users.db")
DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive']
DRIVE_PARENT_FOLDER_ID = '1QVP3Mh86LGLsEIQSyojZ6DBkjGUdWv3c'
_drive_checked = False

MAX_HOT_MOVIES = 30
WORKER_HEARTBEATS = {}
WATCHDOG_COMMANDS = {}
WORKER_TIMEOUT = 120
UPDATE_INFO_FILE = os.path.join(BASE_DIR, "update_info.json")

def _load_update_info():
    if os.path.exists(UPDATE_INFO_FILE):
        try:
            with open(UPDATE_INFO_FILE, "r") as f:
                return json.load(f)
        except: pass
    return {"latest_version": APP_VERSION, "download_url": "", "changelog": "", "force_update": False}

def _save_update_info(info):
    with open(UPDATE_INFO_FILE, "w") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

# ==========================================
# GOOGLE DRIVE HELPERS (giữ nguyên)
# ==========================================
def get_drive_service():
    global _drive_checked
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    token_path = os.path.join(BASE_DIR, 'token.json')
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, DRIVE_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, 'w') as f:
            f.write(creds.to_json())
    if not creds or not creds.valid:
        if not _drive_checked: _drive_checked = True
        return None
    return build('drive', 'v3', credentials=creds)

def verify_drive_episodes(series_id):
    real_eps = set()
    try:
        service = get_drive_service()
        if not service: return real_eps
        query = f"'{DRIVE_PARENT_FOLDER_ID}' in parents and name = '{series_id}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        result = service.files().list(q=query, fields='files(id)').execute()
        folders = result.get('files', [])
        if not folders: return real_eps
        folder_id = folders[0]['id']
        query = f"'{folder_id}' in parents and trashed = false and mimeType = 'video/mp4'"
        result = service.files().list(q=query, fields='files(name)', pageSize=500).execute()
        files = result.get('files', [])
        for f in files:
            match = re.search(r'(?:Tap|tập|EP|ep|_)\s*(\d+)', f['name'], re.IGNORECASE)
            if match: real_eps.add(int(match.group(1)))
            else:
                nums = re.findall(r'(\d+)', f['name'])
                if nums: real_eps.add(int(nums[-1]))
    except Exception: pass
    return real_eps

def get_real_web_total(html):
    web_total = 0
    try:
        json_match = re.search(r'window\._ROUTER_DATA\s*=\s*(\{.+?\})\s*;?\s*</script>', html, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group(1))
            series_detail = data.get("loaderData", {}).get("detail_page", {}).get("seriesDetail", {})
            if series_detail:
                right_text = series_detail.get("episode_right_text", "")
                if right_text:
                    num_match = re.search(r'(\d+)', right_text)
                    if num_match: web_total = int(num_match.group(1))
                if web_total == 0:
                    vid_list = series_detail.get("vid_list", [])
                    if isinstance(vid_list, list) and len(vid_list) > 0: web_total = len(vid_list)
        if web_total == 0:
            ep_match = re.search(r'"episode_cnt"\s*:\s*(\d+)', html)
            if ep_match: web_total = int(ep_match.group(1))
    except: pass
    return web_total

# ==========================================
# HOT MOVIES HELPERS (giữ nguyên)
# ==========================================
def _extract_series_from_json(data, movies, seen):
    if len(movies) >= MAX_HOT_MOVIES: return
    if isinstance(data, dict):
        sid = data.get('series_id')
        sname = data.get('series_name')
        if sid and sname and isinstance(sname, str):
            sid = str(sid).strip()
            sname = sname.strip()
            if sid.isdigit() and len(sid) >= 10 and sid not in seen:
                title = sname.replace("\\", "")
                cover = data.get('series_cover') or data.get('cover_url') or data.get('horizontal_cover') or ''
                if cover and not str(cover).startswith('http') and not str(cover).startswith('//'): cover = ''
                ep_cnt = 0
                raw_cnt = data.get('episode_cnt')
                if raw_cnt:
                    try: ep_cnt = int(raw_cnt)
                    except: pass
                if ep_cnt == 0:
                    right_text = data.get('episode_right_text', '')
                    if right_text:
                        num_m = re.search(r'(\d+)', str(right_text))
                        if num_m: ep_cnt = int(num_m.group(1))
                if ep_cnt == 0:
                    vid_list = data.get('vid_list')
                    if isinstance(vid_list, list) and vid_list: ep_cnt = len(vid_list)
                if title and ep_cnt > 0:
                    movies.append({"url": f"https://hongguoduanju.com/detail?series_id={sid}", "series_id": sid, "title": title, "cover_url": cover, "total_episodes": ep_cnt, "genres": ""})
                    seen.add(sid)
        for v in data.values():
            if len(movies) >= MAX_HOT_MOVIES: return
            _extract_series_from_json(v, movies, seen)
    elif isinstance(data, list):
        for item in data:
            if len(movies) >= MAX_HOT_MOVIES: return
            _extract_series_from_json(item, movies, seen)

def _get_completed_series_ids():
    completed = set()
    try:
        conn = get_mysql_connection()
        if conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT series_id FROM jobs WHERE status = 'completed' AND total_episodes > 0")
            for row in cursor.fetchall(): completed.add(str(row['series_id']))
            cursor.close(); conn.close()
    except: pass
    return completed

async def _async_fetch_hot_movies():
    headers = {"User-Agent": "Mozilla/5.0"}
    movies = []
    completed_ids = _get_completed_series_ids()
    seen = set(completed_ids)
    urls_to_scrape = ["https://hongguoduanju.com/", "https://hongguoduanju.com/category"]
    async with httpx.AsyncClient(timeout=30.0) as client:
        for url in urls_to_scrape:
            if len(movies) >= MAX_HOT_MOVIES: break
            try:
                resp = await client.get(url, headers=headers)
                html = resp.text
            except: continue
            json_match = re.search(r'window\._ROUTER_DATA\s*=\s*(\{.+?\})\s*;?\s*</script>', html, re.DOTALL)
            if json_match:
                try: data = json.loads(json_match.group(1)); _extract_series_from_json(data, movies, seen)
                except: pass
    return movies

# ==========================================
# BACKGROUND TASKS
# ==========================================
async def cleanup_zombie_tasks():
    while True:
        try:
            conn = get_mysql_connection()
            if conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE jobs SET status = 'pending', worker_id = NULL WHERE status = 'processing' AND updated_at < (NOW() - INTERVAL 2 MINUTE)")
                conn.commit(); conn.close()
        except: pass
        # Dọn rate limiter mỗi 5 phút
        rate_limiter.cleanup()
        await asyncio.sleep(60)

@asynccontextmanager
async def lifespan(app: FastAPI):
    task2 = asyncio.create_task(cleanup_zombie_tasks())
    yield
    task2.cancel()

# ==========================================
# KHỞI TẠO FASTAPI APP
# ==========================================
app = FastAPI(title="AnhStudio SaaS Server (Secured)", lifespan=lifespan)

# 🔒 CORS: Chỉ cho phép same-origin (admin dashboard)
# Desktop app (PyQt6) KHÔNG dùng browser nên không bị ảnh hưởng bởi CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],  # Không cho phép cross-origin từ bất kỳ domain nào
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

# ==========================================
# SQLITE DATABASE (USERS) — giữ nguyên schema
# ==========================================
def get_db():
    conn = sqlite3.connect(SQLITE_PATH); conn.row_factory = sqlite3.Row; return conn

def init_db():
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password TEXT, zalo TEXT, hwid TEXT, expiry_date TIMESTAMP, balance_hongguo INTEGER DEFAULT 0, platform TEXT DEFAULT 'honggou')")
    for col, col_type, col_default in [("balance_hongguo", "INTEGER", "0"), ("platform", "TEXT", "'honggou'")]:
        try: cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type} DEFAULT {col_default}")
        except: pass
    conn.commit(); conn.close()
init_db()

# ==========================================
# MYSQL DATABASE (JOBS) — giữ nguyên schema
# ==========================================
DB_HOST = "localhost"
DB_USER = "root"
DB_PASSWORD = os.environ.get("MYSQL_PASSWORD", "AnhStudio123!")
DB_NAME = "phim_database"
mysql_pool = None

def init_mysql_db():
    global mysql_pool
    try:
        temp_conn = mysql.connector.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD, use_pure=True, auth_plugin='mysql_native_password')
        cursor = temp_conn.cursor()
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME}")
        cursor.close(); temp_conn.close()
        mysql_pool = pooling.MySQLConnectionPool(
            pool_name="anhstudio_pool", pool_size=32, pool_reset_session=True,
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD, database=DB_NAME,
            use_pure=True, auth_plugin='mysql_native_password'
        )
        conn = get_mysql_connection()
        if conn:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE IF NOT EXISTS jobs (job_id VARCHAR(100) PRIMARY KEY, series_id VARCHAR(100) UNIQUE, total_episodes INT DEFAULT 0, status VARCHAR(50) DEFAULT 'pending', original_url TEXT NULL, worker_id VARCHAR(50) NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)")
            cursor.execute("CREATE TABLE IF NOT EXISTS job_episodes (id INT AUTO_INCREMENT PRIMARY KEY, job_id VARCHAR(100), episode_number INT NOT NULL, drive_link TEXT NOT NULL, FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE)")
            cursor.execute("CREATE TABLE IF NOT EXISTS worker_blacklist (worker_id VARCHAR(50) PRIMARY KEY, blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            for query in ["ALTER TABLE job_episodes ADD COLUMN file_name TEXT", "ALTER TABLE job_episodes ADD COLUMN file_size BIGINT DEFAULT 0", "ALTER TABLE jobs ADD COLUMN original_url TEXT", "ALTER TABLE jobs ADD COLUMN title TEXT NULL", "ALTER TABLE jobs ADD COLUMN cover_url TEXT NULL", "ALTER TABLE jobs ADD COLUMN worker_id VARCHAR(50) NULL", "ALTER TABLE jobs ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP", "ALTER TABLE jobs ADD COLUMN genres TEXT NULL"]:
                try: cursor.execute(query)
                except Error: pass
            conn.commit(); cursor.close(); conn.close()
    except Exception as e: print("Lỗi Init DB:", e)

def get_mysql_connection():
    try:
        if mysql_pool: return mysql_pool.get_connection()
    except Error: return None
    return None

init_mysql_db()

# ==========================================
# PYDANTIC MODELS
# ==========================================
class RegisterReq(BaseModel): username: str; password: str; zalo: str = ""; platform: str = "honggou"
class LoginReq(BaseModel): username: str; password: str; hwid: str = ""; platform: str = "honggou"
class AddJobReq(BaseModel): url: str; series_id: str; expected_total: int = 0; title: str = ""; cover_url: str = ""
class PayDownloadReq(BaseModel): username: str; num_episodes: int
class ScanUpdate(BaseModel): job_id: str; total_episodes: int; action: str = ""
class EpisodeUpdate(BaseModel): job_id: str; episode_number: int; drive_link: str; file_name: str; series_id: str = None
class CompleteJobReq(BaseModel): job_id: str; total_uploaded: int = 0; expected_total: int = 0
class VerifyTotalReq(BaseModel): job_id: str; series_id: str; current_count: int
class TopupReq(BaseModel): amount: int
class AddVipReq(BaseModel): days: int
class AdminLoginReq(BaseModel): password: str
class PublishUpdateReq(BaseModel): latest_version: str; download_url: str; changelog: str = ""; force_update: bool = False

# ==========================================
# 🔒 XỬ LÝ ĐĂNG KÝ — MẬT KHẨU ĐƯỢC HASH
# ==========================================
def process_register(req: RegisterReq, platform: str):
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE username = ? AND platform = ?", (req.username, platform))
    if cursor.fetchone(): conn.close(); return {"status": "error", "message": "Tên đăng nhập đã tồn tại!"}
    expiry = datetime.datetime.now()
    hashed_pwd = hash_password(req.password)  # 🔒 HASH MẬT KHẨU
    cursor.execute("INSERT INTO users (username, password, zalo, hwid, expiry_date, balance_hongguo, platform) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (req.username, hashed_pwd, req.zalo, "", expiry.strftime("%Y-%m-%d %H:%M:%S"), 0, platform))
    conn.commit(); conn.close()
    return {"status": "success", "message": "Đăng ký thành công!"}

# ==========================================
# 🔒 XỬ LÝ ĐĂNG NHẬP — TRẢ JWT TOKEN, TỰ MIGRATE
# ==========================================
def process_login(req: LoginReq, platform: str):
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ? AND platform = ?", (req.username, platform))
    user = cursor.fetchone()
    if not user: conn.close(); return {"status": "error", "message": "Tài khoản không tồn tại!"}

    # 🔒 Kiểm tra mật khẩu (hỗ trợ cả bcrypt hash và plaintext cũ)
    pwd_match, needs_migration = verify_password(req.password, user["password"])
    if not pwd_match:
        conn.close()
        return {"status": "error", "message": "Sai mật khẩu!"}

    # 🔒 Tự động migrate password cũ (plaintext) sang bcrypt hash
    if needs_migration:
        new_hash = hash_password(req.password)
        cursor.execute("UPDATE users SET password = ? WHERE username = ? AND platform = ?", (new_hash, req.username, platform))
        conn.commit()

    # Kiểm tra HWID
    stored_hwid = user["hwid"] or ""
    if stored_hwid and stored_hwid != req.hwid:
        conn.close()
        return {"status": "error", "message": "Tài khoản đã được kích hoạt trên máy khác!"}

    # Kiểm tra hạn VIP
    expiry_str = user["expiry_date"] or ""
    if expiry_str:
        try:
            if datetime.datetime.now() > datetime.datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S"):
                conn.close()
                return {"status": "expired", "message": "Tài khoản hết hạn!", "expiry": expiry_str}
        except: pass

    # Lưu HWID nếu lần đầu đăng nhập trên máy mới
    if not stored_hwid and req.hwid:
        cursor.execute("UPDATE users SET hwid = ? WHERE username = ? AND platform = ?", (req.hwid, req.username, platform))
        conn.commit()

    conn.close()

    # 🔒 TẠO JWT TOKEN
    token = create_client_token(req.username, platform)
    return {"status": "success", "expiry": expiry_str, "token": token}

# ==========================================
# PUBLIC ENDPOINTS — KHÔNG CẦN XÁC THỰC
# ==========================================
@app.get("/")
def health_check():
    return {"status": "ok"}

@app.post("/api/register")
def register_launcher(req: RegisterReq, request: Request):
    ip = get_client_ip(request)
    if not rate_limiter.is_allowed(f"register:{ip}", 5, 60):
        raise HTTPException(429, "Bạn đăng ký quá nhiều lần. Vui lòng chờ 1 phút.")
    return process_register(req, req.platform)

@app.post("/api/login")
def login_launcher(req: LoginReq, request: Request):
    ip = get_client_ip(request)
    if not rate_limiter.is_allowed(f"login:{ip}", 10, 60):
        raise HTTPException(429, "Quá nhiều lần đăng nhập. Vui lòng chờ 1 phút.")
    return process_login(req, req.platform)

@app.post("/api/honggou/register")
def register_honggou(req: RegisterReq, request: Request):
    ip = get_client_ip(request)
    if not rate_limiter.is_allowed(f"register:{ip}", 5, 60):
        raise HTTPException(429, "Quá nhiều lần đăng ký.")
    return process_register(req, "honggou")

@app.post("/api/honggou/login")
def login_honggou(req: LoginReq, request: Request):
    ip = get_client_ip(request)
    if not rate_limiter.is_allowed(f"login:{ip}", 10, 60):
        raise HTTPException(429, "Quá nhiều lần đăng nhập.")
    return process_login(req, "honggou")

@app.post("/api/douyin/register")
def register_douyin(req: RegisterReq, request: Request):
    ip = get_client_ip(request)
    if not rate_limiter.is_allowed(f"register:{ip}", 5, 60):
        raise HTTPException(429, "Quá nhiều lần đăng ký.")
    return process_register(req, "douyin")

@app.post("/api/douyin/login")
def login_douyin(req: LoginReq, request: Request):
    ip = get_client_ip(request)
    if not rate_limiter.is_allowed(f"login:{ip}", 10, 60):
        raise HTTPException(429, "Quá nhiều lần đăng nhập.")
    return process_login(req, "douyin")

@app.post("/api/allinone/register")
def register_allinone(req: RegisterReq, request: Request):
    ip = get_client_ip(request)
    if not rate_limiter.is_allowed(f"register:{ip}", 5, 60):
        raise HTTPException(429, "Quá nhiều lần đăng ký.")
    return process_register(req, "allinone")

@app.post("/api/allinone/login")
def login_allinone(req: LoginReq, request: Request):
    ip = get_client_ip(request)
    if not rate_limiter.is_allowed(f"login:{ip}", 10, 60):
        raise HTTPException(429, "Quá nhiều lần đăng nhập.")
    return process_login(req, "allinone")

# Check update — public (app khách gọi kiểm tra bản mới)
@app.get("/api/client/check_update")
def check_update(current_version: str = ""):
    info = _load_update_info()
    return {
        "latest_version": info.get("latest_version", APP_VERSION),
        "download_url": info.get("download_url", ""),
        "changelog": info.get("changelog", ""),
        "force_update": info.get("force_update", False)
    }

# Hot movies — public (chỉ xem, không cần auth)
@app.get("/api/client/hot_movies")
def get_hot_movies(genre: str = None):
    conn = get_mysql_connection()
    if not conn: return []
    cursor = conn.cursor(dictionary=True); ready_movies = []
    query = "SELECT series_id, original_url, title, cover_url, total_episodes, genres FROM jobs WHERE status = 'completed' AND total_episodes > 0 AND title IS NOT NULL AND title != '' AND cover_url IS NOT NULL AND cover_url != ''"
    params = []
    if genre: query += " AND genres LIKE %s"; params.append(f"%{genre}%")
    query += " ORDER BY updated_at DESC LIMIT 50"
    cursor.execute(query, tuple(params))
    old_jobs = cursor.fetchall()
    for old in old_jobs:
        url = old.get('original_url') or f"https://hongguoduanju.com/detail?series_id={old['series_id']}"
        ready_movies.append({"url": url, "series_id": old['series_id'], "title": old.get('title'), "cover_url": old.get('cover_url'), "total_episodes": old['total_episodes'], "genres": old.get('genres', '')})
    cursor.close(); conn.close()
    return ready_movies

# ==========================================
# 🔒 CLIENT ENDPOINTS — CẦN JWT TOKEN
# ==========================================
@app.get("/api/client/balance/{username}")
def get_client_balance(username: str, user: dict = Depends(require_client)):
    # 🔒 Chỉ cho phép xem số dư của chính mình
    if user["sub"] != username:
        raise HTTPException(403, "Bạn không có quyền xem số dư người khác.")
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT balance_hongguo FROM users WHERE username = ? AND platform = 'honggou'", (username,))
    row = cursor.fetchone(); conn.close()
    return {"status": "success", "balance": row["balance_hongguo"]} if row else {"status": "error", "balance": 0}

@app.post("/api/client/pay_for_download")
def pay_for_download(req: PayDownloadReq, user: dict = Depends(require_client)):
    # 🔒 Chỉ cho phép trừ tiền của chính mình
    if user["sub"] != req.username:
        raise HTTPException(403, "Bạn không có quyền thao tác tài khoản người khác.")
    cost = req.num_episodes * 20
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT balance_hongguo FROM users WHERE username = ? AND platform = 'honggou'", (req.username,))
    row = cursor.fetchone()
    if not row: conn.close(); return {"status": "error", "message": "Tài khoản không tồn tại!"}
    if row["balance_hongguo"] < cost: conn.close(); return {"status": "error", "message": f"Không đủ số dư Hongguo! Cần {cost}đ."}
    cursor.execute("UPDATE users SET balance_hongguo = balance_hongguo - ? WHERE username = ? AND platform = 'honggou'", (cost, req.username))
    conn.commit(); conn.close()
    return {"status": "success", "message": "Thanh toán thành công"}

@app.post("/api/client/add_job")
def client_add_job(req: AddJobReq, user: dict = Depends(require_client)):
    conn = get_mysql_connection(); cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM jobs WHERE series_id = %s", (req.series_id,))
    existing_job = cursor.fetchone()
    if existing_job:
        job_id = existing_job['job_id']
        cursor.execute("SELECT episode_number, drive_link, file_name FROM job_episodes WHERE job_id = %s ORDER BY episode_number ASC", (job_id,))
        episodes = cursor.fetchall()
        if req.expected_total > len(episodes) and existing_job['status'] not in ['pending', 'processing']:
            cursor.execute("UPDATE jobs SET status = 'pending', total_episodes = %s WHERE job_id = %s", (req.expected_total, job_id))
            conn.commit(); conn.close(); return {"status": "retrying", "job_id": job_id, "series_id": req.series_id, "episodes": episodes}
        if existing_job['status'] == 'completed': conn.close(); return {"status": "cache_hit", "job_id": job_id, "series_id": req.series_id, "episodes": episodes}
        elif existing_job['status'] == 'partial':
            cursor.execute("UPDATE jobs SET status = 'pending' WHERE job_id = %s", (job_id,)); conn.commit(); conn.close()
            return {"status": "retrying", "job_id": job_id, "series_id": req.series_id, "episodes": episodes}
        else: conn.close(); return {"status": "processing", "job_id": job_id, "series_id": req.series_id, "episodes": episodes}
    job_id = f"HG_{int(time.time())}"
    try: cursor.execute("INSERT INTO jobs (job_id, series_id, original_url, title, cover_url, total_episodes, status) VALUES (%s, %s, %s, %s, %s, %s, %s)", (job_id, req.series_id, req.url, req.title, req.cover_url, req.expected_total, "pending")); conn.commit()
    except: conn.rollback()
    finally: conn.close()
    return {"status": "cache_miss", "job_id": job_id, "series_id": req.series_id}

@app.get("/api/client/job_status/{job_id}")
def get_job_status(job_id: str, user: dict = Depends(require_client)):
    conn = get_mysql_connection()
    if not conn: return {"status": "error"}
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,))
    job_info = cursor.fetchone()
    if not job_info: return {"status": "waiting"}
    cursor.execute("SELECT episode_number, drive_link, file_name FROM job_episodes WHERE job_id = %s", (job_id,))
    episodes_data = cursor.fetchall(); conn.close()
    return {"status": job_info["status"], "total_episodes": job_info["total_episodes"], "episodes": episodes_data}

# ==========================================
# 🔒 WORKER ENDPOINTS — CẦN WORKER SECRET
# ==========================================
@app.get("/api/worker/watchdog_ping")
def watchdog_ping(request: Request, worker_id: str = "Unknown", _=Depends(require_worker)):
    command = WATCHDOG_COMMANDS.pop(worker_id, None)
    return {"status": "ok", "command": command}

@app.get("/api/worker/get_job")
def worker_get_job(request: Request, worker_id: str = "Unknown", _=Depends(require_worker)):
    WORKER_HEARTBEATS[worker_id] = {"time": time.time(), "action": "Vừa nhận được Job mới..."}
    conn = get_mysql_connection(); cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT worker_id FROM worker_blacklist WHERE worker_id = %s", (worker_id,))
    if cursor.fetchone(): conn.close(); return {"status": "pause", "message": "Worker bi chan boi Admin."}
    cursor.execute("START TRANSACTION;")
    cursor.execute("SELECT job_id, series_id, original_url FROM jobs WHERE status = 'pending' LIMIT 1 FOR UPDATE SKIP LOCKED")
    job = cursor.fetchone()
    if job:
        cursor.execute("UPDATE jobs SET status = 'processing', worker_id = %s, updated_at = NOW() WHERE job_id = %s", (worker_id, job['job_id']))
        cursor.execute("SELECT episode_number FROM job_episodes WHERE job_id = %s", (job['job_id'],))
        existing_eps = [r['episode_number'] for r in cursor.fetchall()]
        conn.commit(); conn.close()
        final_url = job.get('original_url') or f"https://hongguoduanju.com/{job['series_id']}"
        return {"status": "has_job", "job": {"job_id": job["job_id"], "series_id": job["series_id"], "link": final_url, "existing_episodes": existing_eps}}
    conn.commit(); conn.close(); return {"status": "no_job"}

@app.post("/api/worker/update_scan")
def update_scan(data: ScanUpdate, _=Depends(require_worker)):
    conn = get_mysql_connection(); cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT worker_id FROM jobs WHERE job_id = %s", (data.job_id,))
    job = cursor.fetchone()
    if job and job.get('worker_id'):
        WORKER_HEARTBEATS[job['worker_id']] = {"time": time.time(), "action": data.action}
    cursor.execute("UPDATE jobs SET total_episodes = %s, updated_at = NOW() WHERE job_id = %s", (data.total_episodes, data.job_id))
    conn.commit(); conn.close(); return {"status": "ok"}

@app.post("/api/worker/update_episode")
def update_episode(data: EpisodeUpdate, _=Depends(require_worker)):
    conn = get_mysql_connection(); cursor = conn.cursor(dictionary=True)
    real_job_id = data.job_id
    if real_job_id == "recovery" and data.series_id:
        cursor.execute("SELECT job_id FROM jobs WHERE series_id = %s ORDER BY job_id DESC LIMIT 1", (data.series_id,))
        row = cursor.fetchone()
        if row: real_job_id = row['job_id']
        else:
            real_job_id = f"HG_RECOVERY_{data.series_id}"
            cursor.execute("INSERT IGNORE INTO jobs (job_id, series_id, total_episodes, status) VALUES (%s, %s, 0, 'completed')", (real_job_id, data.series_id))
    cursor.execute("SELECT worker_id FROM jobs WHERE job_id = %s", (real_job_id,))
    job_row = cursor.fetchone()
    if job_row and job_row.get('worker_id'):
        WORKER_HEARTBEATS[job_row['worker_id']] = {"time": time.time(), "action": f"Vừa Upload xong Tập {data.episode_number}..."}
    cursor.execute("SELECT id FROM job_episodes WHERE job_id = %s AND episode_number = %s", (real_job_id, data.episode_number))
    if cursor.fetchone(): cursor.execute("UPDATE job_episodes SET drive_link = %s, file_name = %s WHERE job_id = %s AND episode_number = %s", (data.drive_link, data.file_name, real_job_id, data.episode_number))
    else: cursor.execute("INSERT INTO job_episodes (job_id, episode_number, drive_link, file_name) VALUES (%s, %s, %s, %s)", (real_job_id, data.episode_number, data.drive_link, data.file_name))
    cursor.execute("UPDATE jobs SET updated_at = NOW() WHERE job_id = %s", (real_job_id,))
    conn.commit(); cursor.close(); conn.close(); return {"status": "ok"}

@app.post("/api/worker/complete_job")
def worker_complete_job(req: CompleteJobReq, _=Depends(require_worker)):
    conn = get_mysql_connection(); cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT COUNT(*) as cnt FROM job_episodes WHERE job_id = %s AND drive_link IS NOT NULL AND drive_link != ''", (req.job_id,))
    actual_in_db = cursor.fetchone()['cnt']
    cursor.execute("SELECT total_episodes FROM jobs WHERE job_id = %s", (req.job_id,))
    job = cursor.fetchone()
    expected = job['total_episodes'] if job else 0
    new_status = "completed" if expected > 0 and actual_in_db >= expected else "partial"
    cursor.execute("UPDATE jobs SET status = %s, worker_id = NULL, updated_at = NOW() WHERE job_id = %s", (new_status, req.job_id))
    conn.commit(); conn.close(); return {"status": "ok", "job_status": new_status}

@app.post("/api/worker/verify_total")
async def worker_verify_total(data: VerifyTotalReq, _=Depends(require_worker)):
    web_total = 0
    try:
        url = f"https://hongguoduanju.com/detail?series_id={data.series_id}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        web_total = get_real_web_total(resp.text)
    except: pass
    conn = get_mysql_connection()
    if web_total > 0:
        if conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE jobs SET total_episodes = %s, updated_at = NOW() WHERE job_id = %s", (web_total, data.job_id))
            conn.commit(); conn.close()
        if data.current_count >= web_total: return {"action": "done", "total": web_total}
        else: return {"action": "continue", "total": web_total}
    else:
        if conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE jobs SET total_episodes = %s, updated_at = NOW() WHERE job_id = %s", (data.current_count, data.job_id))
            conn.commit(); conn.close()
        return {"action": "accept", "total": data.current_count}

# ==========================================
# 🔒 ADMIN LOGIN ENDPOINT
# ==========================================
@app.post("/api/admin/login")
def admin_login(req: AdminLoginReq, request: Request):
    ip = get_client_ip(request)
    if not rate_limiter.is_allowed(f"admin_login:{ip}", 5, 300):
        raise HTTPException(429, "Quá nhiều lần thử. Chờ 5 phút.")
    if req.password == ADMIN_PASSWORD:
        token = create_admin_token()
        return {"status": "success", "token": token}
    raise HTTPException(401, "Sai mật khẩu Admin.")

# ==========================================
# 🔒 ADMIN ENDPOINTS — CẦN ADMIN TOKEN
# ==========================================
@app.get("/api/admin/stats")
def get_admin_stats(_=Depends(require_admin)):
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT expiry_date, balance_hongguo, platform FROM users")
    users = cursor.fetchall(); conn.close()
    total_balance = sum((u["balance_hongguo"] or 0) for u in users)
    now = datetime.datetime.now(); vip_count = 0; normal_count = 0
    for u in users:
        is_vip = False
        if u["expiry_date"]:
            try:
                if datetime.datetime.strptime(u["expiry_date"], "%Y-%m-%d %H:%M:%S") > now: is_vip = True
            except: pass
        if is_vip: vip_count += 1
        else: normal_count += 1
    return {"total_users": len(users), "honggou_users": sum(1 for u in users if u["platform"] == "honggou"), "douyin_users": sum(1 for u in users if u["platform"] == "douyin"), "allinone_users": sum(1 for u in users if u["platform"] == "allinone"), "vip_count": vip_count, "normal_count": normal_count, "total_balance_hongguo": total_balance}

@app.get("/api/admin/users")
def get_all_users(platform: str = Query(default=None), _=Depends(require_admin)):
    conn = get_db(); cursor = conn.cursor()
    if platform: cursor.execute("SELECT username, zalo, hwid, expiry_date, balance_hongguo, platform FROM users WHERE platform = ? ORDER BY expiry_date DESC", (platform,))
    else: cursor.execute("SELECT username, zalo, hwid, expiry_date, balance_hongguo, platform FROM users ORDER BY expiry_date DESC")
    # 🔒 KHÔNG TRẢ VỀ PASSWORD nữa — chỉ trả các field cần thiết
    users = [dict(row) for row in cursor.fetchall()]; conn.close(); return users

@app.post("/api/admin/users/{username}/add_balance_hongguo")
def admin_add_balance_hongguo(username: str, req: TopupReq, _=Depends(require_admin)):
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("UPDATE users SET balance_hongguo = balance_hongguo + ? WHERE username = ? AND platform = 'honggou'", (req.amount, username))
    conn.commit(); conn.close(); return {"status": "success"}

@app.post("/api/admin/users/{username}/add_vip")
def admin_add_vip(username: str, req: AddVipReq, platform: str = Query(default="honggou"), _=Depends(require_admin)):
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT expiry_date FROM users WHERE username = ? AND platform = ?", (username, platform))
    user = cursor.fetchone()
    if user and user["expiry_date"]:
        try:
            current = datetime.datetime.strptime(user["expiry_date"], "%Y-%m-%d %H:%M:%S")
            if current < datetime.datetime.now(): current = datetime.datetime.now()
            cursor.execute("UPDATE users SET expiry_date = ? WHERE username = ? AND platform = ?", ((current + datetime.timedelta(days=req.days)).strftime("%Y-%m-%d %H:%M:%S"), username, platform))
            conn.commit()
        except Exception: pass
    conn.close(); return {"status": "success"}

@app.post("/api/admin/users/{username}/reset_hwid")
def reset_hwid(username: str, platform: str = Query(default="honggou"), _=Depends(require_admin)):
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("UPDATE users SET hwid = '' WHERE username = ? AND platform = ?", (username, platform))
    conn.commit(); conn.close(); return {"status": "success"}

@app.post("/api/admin/users/{username}/reset_password")
def reset_password(username: str, platform: str = Query(default="honggou"), _=Depends(require_admin)):
    """🔒 Admin reset password — đặt lại mật khẩu mặc định '123456' (đã hash)."""
    conn = get_db(); cursor = conn.cursor()
    new_hash = hash_password("123456")
    cursor.execute("UPDATE users SET password = ? WHERE username = ? AND platform = ?", (new_hash, username, platform))
    conn.commit(); conn.close()
    return {"status": "success", "message": f"Đã reset mật khẩu [{username}] về '123456'."}

@app.post("/api/admin/users/{username}/delete")
def delete_user(username: str, platform: str = Query(default="honggou"), _=Depends(require_admin)):
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE username = ? AND platform = ?", (username, platform))
    conn.commit(); conn.close(); return {"status": "success"}

@app.get("/api/admin/workers")
def admin_get_workers(_=Depends(require_admin)):
    now = time.time(); workers = {}
    for wid, wdata in WORKER_HEARTBEATS.items():
        if isinstance(wdata, dict): last_seen = wdata["time"]; action = wdata.get("action", "")
        else: last_seen = wdata; action = ""
        workers[wid] = {"worker_id": wid, "last_seen": last_seen, "ago_seconds": int(now - last_seen), "job": None, "status": "idle", "blocked": False, "action": action}
    conn = get_mysql_connection()
    if conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT j.worker_id, j.job_id, j.series_id, j.title, j.total_episodes, COUNT(e.id) as has_link FROM jobs j LEFT JOIN job_episodes e ON j.job_id = e.job_id AND e.drive_link IS NOT NULL AND e.drive_link != '' WHERE j.status = 'processing' AND j.worker_id IS NOT NULL GROUP BY j.job_id")
        for row in cursor.fetchall():
            wid = row['worker_id']
            if wid not in workers: workers[wid] = {"worker_id": wid, "last_seen": 0, "ago_seconds": 999999, "job": None, "status": "offline", "blocked": False, "action": ""}
            workers[wid]["job"] = {"job_id": row["job_id"], "series_id": row["series_id"], "title": row["title"] or row["series_id"], "progress": f"{row['has_link']}/{row['total_episodes']}"}
        cursor.execute("SELECT worker_id FROM worker_blacklist")
        for row in cursor.fetchall():
            wid = row['worker_id']
            if wid not in workers: workers[wid] = {"worker_id": wid, "last_seen": 0, "ago_seconds": 999999, "job": None, "status": "blocked", "blocked": True, "action": ""}
            else: workers[wid]["blocked"] = True
        cursor.close(); conn.close()
    for w in workers.values():
        if w["blocked"]: w["status"] = "blocked"
        elif w["job"]: w["status"] = "working" if w["ago_seconds"] < WORKER_TIMEOUT else "stuck"
        elif w["ago_seconds"] < WORKER_TIMEOUT: w["status"] = "idle"
        else: w["status"] = "offline"
    return sorted(workers.values(), key=lambda x: x["ago_seconds"])

@app.post("/api/admin/workers/{worker_id}/stop")
def admin_stop_worker(worker_id: str, _=Depends(require_admin)):
    conn = get_mysql_connection()
    if not conn: return {"status": "error"}
    cursor = conn.cursor()
    cursor.execute("INSERT IGNORE INTO worker_blacklist (worker_id) VALUES (%s)", (worker_id,))
    cursor.execute("UPDATE jobs SET status = 'pending', worker_id = NULL, updated_at = NOW() WHERE worker_id = %s AND status = 'processing'", (worker_id,))
    reclaimed = cursor.rowcount; conn.commit(); conn.close()
    return {"status": "ok", "message": f"Da dung {worker_id}. Thu hoi {reclaimed} job."}

@app.post("/api/admin/workers/{worker_id}/activate")
def admin_activate_worker(worker_id: str, _=Depends(require_admin)):
    conn = get_mysql_connection()
    if not conn: return {"status": "error"}
    cursor = conn.cursor()
    cursor.execute("DELETE FROM worker_blacklist WHERE worker_id = %s", (worker_id,))
    conn.commit(); conn.close(); return {"status": "ok"}

@app.post("/api/admin/workers/{worker_id}/reset")
def admin_reset_worker(worker_id: str, _=Depends(require_admin)):
    WATCHDOG_COMMANDS[worker_id] = "reset"
    conn = get_mysql_connection()
    if conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE jobs SET status = 'pending', worker_id = NULL, updated_at = NOW() WHERE worker_id = %s AND status = 'processing'", (worker_id,))
        conn.commit(); conn.close()
    return {"status": "ok"}

@app.post("/api/admin/publish_update")
def admin_publish_update(req: PublishUpdateReq, _=Depends(require_admin)):
    info = {
        "latest_version": req.latest_version,
        "download_url": req.download_url,
        "changelog": req.changelog,
        "force_update": req.force_update
    }
    _save_update_info(info)
    return {"status": "success", "message": f"Đã phát hành v{req.latest_version}!"}

@app.get("/api/admin/honggou_movies")
def admin_honggou_movies(_=Depends(require_admin)):
    conn = get_mysql_connection()
    if not conn: return []
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT j.job_id, j.series_id, j.total_episodes, j.status, j.worker_id, j.updated_at, j.title, j.genres, COUNT(e.id) as db_episode_count, SUM(CASE WHEN e.drive_link IS NOT NULL AND e.drive_link != '' THEN 1 ELSE 0 END) as db_has_link FROM jobs j LEFT JOIN job_episodes e ON j.job_id = e.job_id GROUP BY j.job_id ORDER BY j.updated_at DESC")
    movies = cursor.fetchall()
    for m in movies:
        if m.get('updated_at'): m['updated_at'] = str(m['updated_at'])
    cursor.close(); conn.close(); return movies

@app.post("/api/admin/delete_pending_movies")
def admin_delete_pending_movies(_=Depends(require_admin)):
    conn = get_mysql_connection()
    if not conn: return {"status": "error"}
    cursor = conn.cursor()
    try: cursor.execute("DELETE FROM jobs WHERE status = 'pending'"); conn.commit(); return {"status": "success", "message": f"Đã xóa {cursor.rowcount} phim!"}
    except Exception: conn.rollback(); return {"status": "error"}
    finally: cursor.close(); conn.close()

@app.post("/api/admin/delete_movie/{job_id}")
def admin_delete_movie(job_id: str, _=Depends(require_admin)):
    conn = get_mysql_connection()
    if not conn: return {"status": "error"}
    cursor = conn.cursor()
    try: cursor.execute("DELETE FROM jobs WHERE job_id = %s", (job_id,)); conn.commit(); return {"status": "success", "message": "Đã xóa phim!"}
    except Exception as e: conn.rollback(); return {"status": "error", "message": str(e)}
    finally: cursor.close(); conn.close()

@app.post("/api/admin/purge_fake_movies")
def admin_purge_fake_movies(_=Depends(require_admin)):
    conn = get_mysql_connection()
    if not conn: return {"status": "error"}
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT j.job_id, j.series_id, j.total_episodes, COUNT(e.id) as has_link FROM jobs j LEFT JOIN job_episodes e ON j.job_id = e.job_id AND e.drive_link IS NOT NULL AND e.drive_link != '' WHERE j.total_episodes > 0 GROUP BY j.job_id HAVING has_link >= j.total_episodes")
    movies = cursor.fetchall(); deleted_count = 0; details = []
    for m in movies:
        sid = m['series_id']; total = m['total_episodes']
        drive_eps = verify_drive_episodes(sid)
        drive_count = len(drive_eps) if drive_eps else 0
        if drive_count < total:
            cursor.execute("DELETE FROM jobs WHERE job_id = %s", (m['job_id'],))
            deleted_count += 1; details.append(f"Xóa {sid} (DB: {total}, Drive: {drive_count})")
    conn.commit(); cursor.close(); conn.close()
    if not details: details.append("Không phát hiện phim ảo!")
    return {"status": "success", "deleted": deleted_count, "details": details}

@app.post("/api/admin/fetch_hot_movies")
async def admin_fetch_hot_movies(_=Depends(require_admin)):
    conn = get_mysql_connection()
    if not conn: return {"status": "error", "message": "Lỗi DB"}
    try:
        hot_movies = await _async_fetch_hot_movies()
        if not hot_movies: return {"status": "error", "message": "Không tìm thấy phim hot."}
        cursor = conn.cursor(dictionary=True); added_count = 0
        for movie in hot_movies:
            cursor.execute("SELECT job_id, status FROM jobs WHERE series_id = %s", (movie["series_id"],))
            existing_job = cursor.fetchone()
            if not existing_job:
                job_id = f"HG_HOT_{int(time.time())}_{movie['series_id']}"
                cursor.execute("INSERT INTO jobs (job_id, series_id, original_url, title, cover_url, total_episodes, status) VALUES (%s, %s, %s, %s, %s, %s, %s)", (job_id, movie["series_id"], movie["url"], movie["title"], movie["cover_url"], movie["total_episodes"], "pending"))
                added_count += 1
            elif existing_job['status'] == 'partial':
                cursor.execute("UPDATE jobs SET status = 'pending', worker_id = NULL, updated_at = NOW() WHERE job_id = %s", (existing_job['job_id'],)); added_count += 1
        conn.commit(); cursor.close(); return {"status": "success", "message": f"Đã cào {added_count} phim hot!"}
    except Exception as e: return {"status": "error", "message": str(e)}
    finally: conn.close()

@app.post("/api/admin/heal_metadata")
async def admin_heal_metadata(_=Depends(require_admin)):
    conn = get_mysql_connection()
    if not conn: return {"status": "error"}
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT job_id, series_id, title, cover_url, genres FROM jobs")
    jobs = cursor.fetchall(); healed_count = 0
    async with httpx.AsyncClient(timeout=15.0) as client:
        for job in jobs:
            try:
                url = f"https://hongguoduanju.com/detail?series_id={job['series_id']}"
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                html = resp.text; real_title = ""; real_cover = ""
                json_match = re.search(r'window\._ROUTER_DATA\s*=\s*(\{.+?\})\s*;?\s*</script>', html, re.DOTALL)
                if json_match:
                    try: data = json.loads(json_match.group(1)); detail = data.get("loaderData", {}).get("detail_page", {}).get("seriesDetail", {}); real_title = detail.get("series_name", ""); real_cover = detail.get("series_cover", "") or detail.get("cover_url", "")
                    except: pass
                if not real_title:
                    title_match = re.search(r'<h1[^>]*class="[^"]*title[^"]*"[^>]*>([^<]+)</h1>', html)
                    if title_match: real_title = title_match.group(1).strip()
                if not real_cover:
                    cover_match = re.search(r'<img[^>]*class="arco-image-img"[^>]*src="([^"]+)"', html)
                    if cover_match: real_cover = cover_match.group(1)
                real_genres = extract_and_translate_genres(html)
                final_title = real_title if real_title else job['title']
                final_cover = real_cover if real_cover else job['cover_url']
                final_genres = real_genres
                if not final_genres:
                    old_g = str(job.get('genres') or ""); clean_g = re.sub(r'<[^>]+>', '', old_g).replace('>', '').replace('Chưa có', '')
                    clean_g = re.sub(r'[\u4e00-\u9fff]+', '', clean_g)
                    parts = [p.strip() for p in clean_g.split(',') if p.strip()]; final_genres = ", ".join(parts)
                if not final_genres: final_genres = ""
                cursor.execute("UPDATE jobs SET title = %s, cover_url = %s, genres = %s WHERE job_id = %s", (final_title, final_cover, final_genres, job['job_id'])); healed_count += 1
            except Exception: pass
    conn.commit(); cursor.close(); conn.close()
    return {"status": "ok", "message": f"Đã quét {healed_count} phim!"}

@app.post("/api/admin/repair_movies")
async def admin_repair_movies(_=Depends(require_admin)):
    conn = get_mysql_connection()
    if not conn: return {"status": "error"}
    cursor = conn.cursor(dictionary=True); fixed = 0; details = []
    cursor.execute("DELETE e1 FROM job_episodes e1 INNER JOIN job_episodes e2 ON e1.job_id = e2.job_id AND e1.episode_number = e2.episode_number AND e1.id < e2.id")
    cursor.execute("DELETE FROM job_episodes WHERE drive_link IS NULL OR drive_link = ''")
    cursor.execute("DELETE e FROM job_episodes e INNER JOIN jobs j ON e.job_id = j.job_id WHERE j.total_episodes > 0 AND e.episode_number > j.total_episodes")
    cursor.execute("SELECT job_id, series_id, total_episodes, original_url FROM jobs WHERE total_episodes >= 100 OR total_episodes = 0")
    suspect_movies = cursor.fetchall()
    async with httpx.AsyncClient(timeout=10.0) as client:
        for m in suspect_movies:
            try:
                url = m.get("original_url") or f"https://hongguoduanju.com/detail?series_id={m['series_id']}"
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                real_total = get_real_web_total(resp.text)
                if real_total > 0 and real_total != m['total_episodes']: cursor.execute("UPDATE jobs SET total_episodes = %s WHERE job_id = %s", (real_total, m['job_id']))
            except: pass
    cursor.execute("SELECT j.job_id, j.series_id, j.total_episodes, COUNT(e.id) as has_link FROM jobs j LEFT JOIN job_episodes e ON j.job_id = e.job_id AND e.drive_link IS NOT NULL AND e.drive_link != '' WHERE j.status = 'completed' AND j.total_episodes > 0 GROUP BY j.job_id HAVING has_link < j.total_episodes")
    for m in cursor.fetchall():
        cursor.execute("UPDATE jobs SET status = 'pending', worker_id = NULL, updated_at = NOW() WHERE job_id = %s", (m['job_id'],)); fixed += 1
    cursor.execute("UPDATE jobs SET status = 'pending', worker_id = NULL, updated_at = NOW() WHERE status = 'completed' AND total_episodes = 0")
    conn.commit(); conn.close()
    return {"status": "ok", "fixed": fixed, "details": details}

@app.post("/api/admin/check_fix_all")
async def admin_check_fix_all(_=Depends(require_admin)):
    conn = get_mysql_connection()
    if not conn: return {"status": "error"}
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT j.job_id, j.series_id, j.total_episodes, j.status, j.original_url, COUNT(e.id) as has_link FROM jobs j LEFT JOIN job_episodes e ON j.job_id = e.job_id AND e.drive_link IS NOT NULL AND e.drive_link != '' GROUP BY j.job_id")
    all_movies = cursor.fetchall(); fixed = 0; details = []; total_checked = len(all_movies)
    cursor.execute("DELETE e1 FROM job_episodes e1 INNER JOIN job_episodes e2 ON e1.job_id = e2.job_id AND e1.episode_number = e2.episode_number AND e1.id < e2.id")
    cursor.execute("DELETE FROM job_episodes WHERE drive_link IS NULL OR drive_link = ''")
    cursor.execute("DELETE e FROM job_episodes e INNER JOIN jobs j ON e.job_id = j.job_id WHERE j.total_episodes > 0 AND e.episode_number > j.total_episodes")
    async with httpx.AsyncClient(timeout=15.0) as client:
        for m in all_movies:
            sid = m['series_id']; needs_fix = False; fix_reasons = []; real_total = m['total_episodes']
            try:
                url = m.get("original_url") or f"https://hongguoduanju.com/detail?series_id={sid}"
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                web_total = get_real_web_total(resp.text)
                if web_total > 0 and web_total != m['total_episodes']:
                    cursor.execute("UPDATE jobs SET total_episodes = %s WHERE job_id = %s", (web_total, m['job_id']))
                    fix_reasons.append(f"tổng {m['total_episodes']}→{web_total}"); real_total = web_total; needs_fix = True
            except: pass
            has_link = m['has_link'] or 0
            if real_total > 0 and has_link < real_total: fix_reasons.append(f"DB {has_link}/{real_total}"); needs_fix = True
            drive_eps = verify_drive_episodes(sid)
            if drive_eps is not None and len(drive_eps) > 0 and real_total > 0:
                drive_missing = len([ep for ep in range(1, real_total + 1) if ep not in drive_eps])
                if drive_missing > 0: fix_reasons.append(f"Drive thiếu {drive_missing}"); needs_fix = True
            if needs_fix and m['status'] != 'processing':
                cursor.execute("UPDATE jobs SET status = 'pending', worker_id = NULL, updated_at = NOW() WHERE job_id = %s", (m['job_id'],))
                fixed += 1; details.append(f"{sid}: {', '.join(fix_reasons)}")
            elif not needs_fix and has_link >= real_total and real_total > 0:
                details.append(f"OK {sid}: {has_link}/{real_total}")
    conn.commit(); conn.close()
    return {"status": "ok", "total_checked": total_checked, "fixed": fixed, "details": details}

@app.post("/api/admin/fix_movie/{job_id}")
def admin_fix_movie(job_id: str, web_total: int = 0, _=Depends(require_admin)):
    conn = get_mysql_connection()
    if not conn: return {"status": "error"}
    cursor = conn.cursor()
    if web_total > 0: cursor.execute("UPDATE jobs SET total_episodes = %s WHERE job_id = %s", (web_total, job_id))
    cursor.execute("UPDATE jobs SET status = 'pending', worker_id = NULL, updated_at = NOW() WHERE job_id = %s", (job_id,))
    cursor.execute("DELETE e1 FROM job_episodes e1 INNER JOIN job_episodes e2 ON e1.job_id = e2.job_id AND e1.episode_number = e2.episode_number AND e1.id < e2.id WHERE e1.job_id = %s", (job_id,))
    if web_total > 0: cursor.execute("DELETE FROM job_episodes WHERE job_id = %s AND episode_number > %s", (job_id, web_total))
    conn.commit(); conn.close()
    return {"status": "ok", "message": "Đã sửa!"}

@app.post("/api/admin/sync_drive_to_db/{job_id}")
def sync_drive_to_db(job_id: str, _=Depends(require_admin)):
    conn = get_mysql_connection()
    if not conn: return {"status": "error"}
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT series_id, total_episodes FROM jobs WHERE job_id = %s", (job_id,))
    job = cursor.fetchone()
    if not job: conn.close(); return {"status": "error", "message": "Không tìm thấy Job"}
    series_id = job['series_id']
    cursor.execute("SELECT episode_number FROM job_episodes WHERE job_id = %s AND drive_link IS NOT NULL AND drive_link != ''", (job_id,))
    db_eps = {row['episode_number'] for row in cursor.fetchall()}
    service = get_drive_service()
    if not service: conn.close(); return {"status": "error", "message": "Lỗi token Drive"}
    synced_count = 0; details = []
    try:
        query = f"'{DRIVE_PARENT_FOLDER_ID}' in parents and name = '{series_id}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        result = service.files().list(q=query, fields='files(id)').execute()
        folders = result.get('files', [])
        if folders:
            folder_id = folders[0]['id']
            query = f"'{folder_id}' in parents and trashed = false and mimeType = 'video/mp4'"
            result = service.files().list(q=query, fields='files(id, name, webViewLink)', pageSize=500).execute()
            files = result.get('files', [])
            for f in files:
                file_name = f.get('name', ''); drive_link = f.get('webViewLink', '')
                if not drive_link: continue
                ep_num = 0
                match = re.search(r'(?:Tap|tập|EP|ep|_)\s*(\d+)', file_name, re.IGNORECASE)
                if match: ep_num = int(match.group(1))
                else:
                    nums = re.findall(r'(\d+)', file_name)
                    if nums: ep_num = int(nums[-1])
                if ep_num > 0 and ep_num not in db_eps:
                    cursor.execute("SELECT id FROM job_episodes WHERE job_id = %s AND episode_number = %s", (job_id, ep_num))
                    if cursor.fetchone(): cursor.execute("UPDATE job_episodes SET drive_link = %s, file_name = %s WHERE job_id = %s AND episode_number = %s", (drive_link, file_name, job_id, ep_num))
                    else: cursor.execute("INSERT INTO job_episodes (job_id, episode_number, drive_link, file_name) VALUES (%s, %s, %s, %s)", (job_id, ep_num, drive_link, file_name))
                    db_eps.add(ep_num); synced_count += 1; details.append(f"Tập {ep_num}")
            if job['total_episodes'] > 0 and len(db_eps) >= job['total_episodes']:
                cursor.execute("UPDATE jobs SET status = 'completed', worker_id = NULL, updated_at = NOW() WHERE job_id = %s", (job_id,))
            else:
                cursor.execute("UPDATE jobs SET updated_at = NOW() WHERE job_id = %s", (job_id,))
            conn.commit()
    except Exception as e: conn.close(); return {"status": "error", "message": str(e)}
    conn.close()
    return {"status": "success", "message": f"Đồng bộ {synced_count} tập!", "synced_eps": details}

@app.post("/api/admin/sync_all_drive_to_db")
def admin_sync_all_drive(_=Depends(require_admin)):
    conn = get_mysql_connection()
    if not conn: return {"status": "error"}
    cursor = conn.cursor(dictionary=True)
    service = get_drive_service()
    cursor.execute("SELECT j.job_id, j.series_id, j.total_episodes, COUNT(e.id) as has_link FROM jobs j LEFT JOIN job_episodes e ON j.job_id = e.job_id AND e.drive_link IS NOT NULL AND e.drive_link != '' WHERE j.total_episodes > 0 GROUP BY j.job_id")
    jobs = cursor.fetchall(); synced_files = 0; auto_completed = 0
    for job in jobs:
        jid = job['job_id']; sid = job['series_id']; tot = job['total_episodes']; hl = job['has_link']
        if hl >= tot:
            cursor.execute("SELECT status FROM jobs WHERE job_id = %s", (jid,))
            row = cursor.fetchone()
            if row and row['status'] != 'completed':
                cursor.execute("UPDATE jobs SET status = 'completed', worker_id = NULL, updated_at = NOW() WHERE job_id = %s", (jid,)); auto_completed += 1
            continue
        if service:
            cursor.execute("SELECT episode_number FROM job_episodes WHERE job_id = %s AND drive_link IS NOT NULL AND drive_link != ''", (jid,))
            db_eps = {r['episode_number'] for r in cursor.fetchall()}
            try:
                q1 = f"'{DRIVE_PARENT_FOLDER_ID}' in parents and name = '{sid}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
                res1 = service.files().list(q=q1, fields='files(id)').execute()
                folders = res1.get('files', [])
                if folders:
                    folder_id = folders[0]['id']
                    q2 = f"'{folder_id}' in parents and trashed = false and mimeType = 'video/mp4'"
                    res2 = service.files().list(q=q2, fields='files(id, name, webViewLink)', pageSize=500).execute()
                    files = res2.get('files', [])
                    for f in files:
                        fname = f.get('name', ''); dlink = f.get('webViewLink', '')
                        if not dlink: continue
                        ep_num = 0
                        match = re.search(r'(?:Tap|tập|EP|ep|_)\s*(\d+)', fname, re.IGNORECASE)
                        if match: ep_num = int(match.group(1))
                        else:
                            nums = re.findall(r'(\d+)', fname)
                            if nums: ep_num = int(nums[-1])
                        if ep_num > 0 and ep_num not in db_eps:
                            cursor.execute("SELECT id FROM job_episodes WHERE job_id = %s AND episode_number = %s", (jid, ep_num))
                            if cursor.fetchone(): cursor.execute("UPDATE job_episodes SET drive_link = %s, file_name = %s WHERE job_id = %s AND episode_number = %s", (dlink, fname, jid, ep_num))
                            else: cursor.execute("INSERT INTO job_episodes (job_id, episode_number, drive_link, file_name) VALUES (%s, %s, %s, %s)", (jid, ep_num, dlink, fname))
                            db_eps.add(ep_num); synced_files += 1
                    if len(db_eps) >= tot:
                        cursor.execute("UPDATE jobs SET status = 'completed', worker_id = NULL, updated_at = NOW() WHERE job_id = %s", (jid,)); auto_completed += 1
            except Exception: pass
    conn.commit(); conn.close()
    return {"status": "ok", "message": f"Kéo {synced_files} link. Hoàn thành {auto_completed} phim!"}

@app.get("/api/admin/verify_movie/{job_id}")
async def admin_verify_movie(job_id: str, _=Depends(require_admin)):
    conn = get_mysql_connection()
    if not conn: return {"error": "DB lỗi"}
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,))
    job = cursor.fetchone()
    if not job: conn.close(); return {"error": "Không tìm thấy job"}
    result = {"job_id": job_id, "series_id": job["series_id"], "status": job["status"], "total_episodes_db": job["total_episodes"]}
    cursor.execute("SELECT episode_number FROM job_episodes WHERE job_id = %s AND drive_link IS NOT NULL AND drive_link != ''", (job_id,))
    db_eps = [r['episode_number'] for r in cursor.fetchall()]; result["db_episodes"] = len(db_eps)
    cursor.execute("SELECT episode_number, COUNT(*) as cnt FROM job_episodes WHERE job_id = %s GROUP BY episode_number HAVING cnt > 1", (job_id,))
    result["db_duplicates"] = [{"ep": d['episode_number'], "count": d['cnt']} for d in cursor.fetchall()]
    conn.close()
    drive_eps = verify_drive_episodes(job["series_id"]); result["drive_episodes"] = len(drive_eps)
    web_total = 0; web_title = ""
    try:
        url = job.get("original_url") or f"https://hongguoduanju.com/detail?series_id={job['series_id']}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            html = resp.text
            json_match = re.search(r'window\._ROUTER_DATA\s*=\s*(\{.+?\})\s*;?\s*</script>', html, re.DOTALL)
            if json_match:
                try: data = json.loads(json_match.group(1)); detail = data.get("loaderData", {}).get("detail_page", {}).get("seriesDetail", {}); web_title = detail.get("series_name", "")
                except: pass
            if not web_title:
                title_match = re.search(r'<h1[^>]*class="[^"]*title[^"]*"[^>]*>([^<]+)</h1>', html)
                if title_match: web_title = title_match.group(1).strip()
            web_total = get_real_web_total(html)
    except Exception as e: web_title = f"Lỗi: {e}"
    result["web_total"] = web_total; result["web_title"] = web_title
    expected = max(result["total_episodes_db"], web_total)
    if expected > 0:
        result["missing_db"] = sorted([ep for ep in range(1, expected + 1) if ep not in db_eps])[:30]
        result["missing_drive"] = sorted([ep for ep in range(1, expected + 1) if ep not in drive_eps])[:30]
        result["missing_db_count"] = len([ep for ep in range(1, expected + 1) if ep not in db_eps])
        result["missing_drive_count"] = len([ep for ep in range(1, expected + 1) if ep not in drive_eps])
        result["extra_db"] = sorted([ep for ep in db_eps if ep > expected])
        result["extra_drive"] = sorted([ep for ep in drive_eps if ep > expected])
    return result

# ==========================================
# 🔒 TRANG ADMIN DASHBOARD — CÓ TRANG ĐĂNG NHẬP
# ==========================================
@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard():
    return """
    <!DOCTYPE html>
    <html lang="vi">
    <head>
        <meta charset="UTF-8">
        <title>SaaS Admin - AnhStudio</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
        <style>
            .tab-btn.active { background-color: #f3f4f6; color: #1f2937; border-bottom: 2px solid #3b82f6; }
            body { background-color: #f0f2f5; }
            .platform-badge { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 9999px; font-size: 11px; font-weight: 700; }
            .badge-honggou { background: #fef3c7; color: #92400e; border: 1px solid #fde68a; }
            .badge-douyin { background: #fce7f3; color: #9d174d; border: 1px solid #fbcfe8; }
            .badge-allinone { background: #e0e7ff; color: #3730a3; border: 1px solid #c7d2fe; }
        </style>
    </head>
    <body class="text-gray-800 font-sans pb-10">

        <!-- 🔒 TRANG ĐĂNG NHẬP ADMIN -->
        <div id="login-overlay" class="fixed inset-0 bg-gray-900 flex items-center justify-center z-50">
            <div class="bg-white rounded-2xl shadow-2xl p-10 w-96">
                <h1 class="text-2xl font-bold text-center mb-6 text-gray-700"><i class="fas fa-lock text-blue-500 mr-2"></i>Admin Login</h1>
                <input type="password" id="admin-password" placeholder="Mật khẩu Admin" class="w-full p-3 border border-gray-300 rounded-lg mb-4 focus:border-blue-500 focus:outline-none" onkeydown="if(event.key==='Enter') adminLogin()">
                <button onclick="adminLogin()" class="w-full bg-blue-600 hover:bg-blue-700 text-white font-bold py-3 rounded-lg transition-all">Đăng Nhập</button>
                <p id="login-error" class="text-red-500 text-center mt-3 text-sm hidden">Sai mật khẩu!</p>
            </div>
        </div>

        <!-- NỘI DUNG CHÍNH (ẩN CHO ĐẾN KHI ĐĂNG NHẬP) -->
        <div id="main-content" class="hidden">
        <div class="bg-white shadow-sm p-4 mb-4 flex justify-between items-center">
            <h1 class="text-xl font-bold text-gray-700">
                <i class="fas fa-server text-blue-500 mr-2"></i> Hệ Thống Quản Lý AnhStudio
            </h1>
            <div class="space-x-2">
                <button onclick="switchTab('tab-overview', this)" class="tab-btn active px-4 py-2 text-sm font-bold text-gray-500 transition-all"><i class="fas fa-chart-pie mr-1"></i> Tổng Quan</button>
                <button onclick="switchTab('tab-honggou', this)" class="tab-btn px-4 py-2 text-sm font-bold text-gray-500 transition-all"><i class="fas fa-film mr-1"></i> KH Honggou</button>
                <button onclick="switchTab('tab-douyin', this)" class="tab-btn px-4 py-2 text-sm font-bold text-gray-500 transition-all"><i class="fab fa-tiktok mr-1"></i> KH Douyin</button>
                <button onclick="switchTab('tab-allinone', this)" class="tab-btn px-4 py-2 text-sm font-bold text-gray-500 transition-all"><i class="fas fa-layer-group mr-1"></i> KH All In One</button>
                <button onclick="switchTab('tab-movies', this)" class="tab-btn px-4 py-2 text-sm font-bold text-gray-500 transition-all"><i class="fas fa-database mr-1"></i> Kho Phim</button>
                <button onclick="switchTab('tab-workers', this)" class="tab-btn px-4 py-2 text-sm font-bold text-gray-500 transition-all"><i class="fas fa-robot mr-1"></i> Worker</button>
                <button onclick="adminLogout()" class="px-4 py-2 text-sm font-bold text-red-500 hover:bg-red-50 rounded transition-all"><i class="fas fa-sign-out-alt mr-1"></i> Thoát</button>
            </div>
        </div>

        <div id="tab-overview" class="tab-content max-w-7xl mx-auto space-y-4 px-4">
            <h2 class="text-lg font-bold text-gray-700 mb-2 border-b pb-2"><i class="fas fa-tachometer-alt text-blue-500 mr-2"></i> Báo Cáo Dữ Liệu Thực Tế</h2>
            <div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-4">
                <div class="bg-gradient-to-r from-gray-700 to-gray-900 text-white p-4 rounded-lg shadow-sm border border-gray-600"><div class="text-xs font-semibold mb-1 text-gray-300"><i class="fas fa-users mr-1"></i> TỔNG USER</div><div class="text-3xl font-bold" id="card-total-users">0</div></div>
                <div class="bg-gradient-to-r from-amber-400 to-orange-500 text-white p-4 rounded-lg shadow-sm border border-orange-400"><div class="text-xs font-semibold mb-1 text-orange-100"><i class="fas fa-film mr-1"></i> HONGGOU</div><div class="text-3xl font-bold" id="card-honggou-users">0</div></div>
                <div class="bg-gradient-to-r from-pink-500 to-rose-600 text-white p-4 rounded-lg shadow-sm border border-rose-500"><div class="text-xs font-semibold mb-1 text-pink-200"><i class="fab fa-tiktok mr-1"></i> DOUYIN</div><div class="text-3xl font-bold" id="card-douyin-users">0</div></div>
                <div class="bg-gradient-to-r from-indigo-500 to-purple-600 text-white p-4 rounded-lg shadow-sm border border-indigo-500"><div class="text-xs font-semibold mb-1 text-indigo-200"><i class="fas fa-layer-group mr-1"></i> ALL IN ONE</div><div class="text-3xl font-bold" id="card-allinone-users">0</div></div>
                <div class="bg-gradient-to-r from-purple-500 to-indigo-600 text-white p-4 rounded-lg shadow-sm border border-indigo-500"><div class="text-xs font-semibold mb-1 text-indigo-200"><i class="fas fa-crown mr-1"></i> TK VIP</div><div class="text-3xl font-bold" id="card-vip-users">0</div></div>
                <div class="bg-gradient-to-r from-emerald-400 to-teal-500 text-white p-4 rounded-lg shadow-sm border border-teal-400"><div class="text-xs font-semibold mb-1 text-teal-100"><i class="fas fa-user mr-1"></i> TK THƯỜNG</div><div class="text-3xl font-bold" id="card-normal-users">0</div></div>
                <div class="bg-gradient-to-r from-sky-400 to-blue-500 text-white p-4 rounded-lg shadow-sm border border-blue-400"><div class="text-xs font-semibold mb-1 text-blue-100"><i class="fas fa-wallet mr-1"></i> SỐ DƯ HONGGOU</div><div class="text-2xl font-bold truncate" id="card-balance">0 đ</div></div>
            </div>
            <div class="mt-6 bg-white p-6 rounded-lg shadow-sm border border-gray-200 text-center text-gray-500">
                <button onclick="loadOverviewStats()" class="mt-2 bg-gray-100 hover:bg-gray-200 text-gray-700 px-4 py-2 rounded-lg text-sm font-bold border border-gray-300"><i class="fas fa-sync-alt"></i> Tải lại số liệu</button>
            </div>
        </div>

        <div id="tab-honggou" class="tab-content hidden max-w-7xl mx-auto space-y-4 px-4"><div class="bg-white p-4 rounded-lg shadow-sm border border-gray-100"><div class="flex justify-between items-center mb-4 border-b pb-2"><h2 class="text-lg font-bold text-gray-700"><span class="platform-badge badge-honggou mr-2"><i class="fas fa-film"></i> HONGGOU</span> Khách Hàng</h2><button onclick="loadHonggouUsers()" class="bg-amber-500 hover:bg-amber-600 text-white px-4 py-1.5 rounded shadow text-sm font-bold"><i class="fas fa-sync-alt mr-2"></i> Làm mới</button></div><div class="overflow-x-auto"><table class="w-full text-left border-collapse text-sm"><thead><tr class="bg-gray-50 text-gray-500 uppercase text-xs border-b"><th class="p-3">Khách hàng</th><th class="p-3 text-center">Gói VIP</th><th class="p-3 text-center">Số Dư</th><th class="p-3 text-center">Khóa Máy</th><th class="p-3 text-center">Hành Động</th></tr></thead><tbody id="honggouTableBody"></tbody></table></div></div></div>

        <div id="tab-douyin" class="tab-content hidden max-w-7xl mx-auto space-y-4 px-4"><div class="bg-white p-4 rounded-lg shadow-sm border border-gray-100"><div class="flex justify-between items-center mb-4 border-b pb-2"><h2 class="text-lg font-bold text-gray-700"><span class="platform-badge badge-douyin mr-2"><i class="fab fa-tiktok"></i> DOUYIN</span> Khách Hàng</h2><button onclick="loadDouyinUsers()" class="bg-pink-500 hover:bg-pink-600 text-white px-4 py-1.5 rounded shadow text-sm font-bold"><i class="fas fa-sync-alt mr-2"></i> Làm mới</button></div><div class="overflow-x-auto"><table class="w-full text-left border-collapse text-sm"><thead><tr class="bg-gray-50 text-gray-500 uppercase text-xs border-b"><th class="p-3">Khách hàng</th><th class="p-3 text-center">Gói VIP</th><th class="p-3 text-center">Khóa Máy</th><th class="p-3 text-center">Hành Động</th></tr></thead><tbody id="douyinTableBody"></tbody></table></div></div></div>

        <div id="tab-allinone" class="tab-content hidden max-w-7xl mx-auto space-y-4 px-4"><div class="bg-white p-4 rounded-lg shadow-sm border border-gray-100"><div class="flex justify-between items-center mb-4 border-b pb-2"><h2 class="text-lg font-bold text-gray-700"><span class="platform-badge badge-allinone mr-2"><i class="fas fa-layer-group"></i> ALL IN ONE</span> Khách Hàng</h2><button onclick="loadAllInOneUsers()" class="bg-indigo-500 hover:bg-indigo-600 text-white px-4 py-1.5 rounded shadow text-sm font-bold"><i class="fas fa-sync-alt mr-2"></i> Làm mới</button></div><div class="overflow-x-auto"><table class="w-full text-left border-collapse text-sm"><thead><tr class="bg-gray-50 text-gray-500 uppercase text-xs border-b"><th class="p-3">Khách hàng</th><th class="p-3 text-center">Gói VIP</th><th class="p-3 text-center">Khóa Máy</th><th class="p-3 text-center">Hành Động</th></tr></thead><tbody id="allinoneTableBody"></tbody></table></div></div></div>

        <div id="tab-movies" class="tab-content hidden max-w-7xl mx-auto space-y-4 px-4">
            <div class="grid grid-cols-4 gap-4 mb-4">
                <div class="bg-green-50 p-3 rounded-lg border border-green-200 text-center shadow-sm"><div class="text-xs text-green-600 font-bold uppercase mb-1">Hoàn thành</div><div class="text-2xl font-bold text-green-700" id="count-completed">0</div></div>
                <div class="bg-blue-50 p-3 rounded-lg border border-blue-200 text-center shadow-sm"><div class="text-xs text-blue-600 font-bold uppercase mb-1">Chờ xử lý</div><div class="text-2xl font-bold text-blue-700" id="count-pending">0</div></div>
                <div class="bg-yellow-50 p-3 rounded-lg border border-yellow-200 text-center shadow-sm"><div class="text-xs text-yellow-600 font-bold uppercase mb-1">Đang tải</div><div class="text-2xl font-bold text-yellow-700" id="count-processing">0</div></div>
                <div class="bg-red-50 p-3 rounded-lg border border-red-200 text-center shadow-sm"><div class="text-xs text-red-600 font-bold uppercase mb-1">Thiếu tập</div><div class="text-2xl font-bold text-red-700" id="count-partial">0</div></div>
            </div>
            <div class="bg-white p-4 rounded-lg shadow-sm border border-gray-100">
                <div class="flex justify-between items-center mb-4 border-b pb-2">
                    <h2 class="text-lg font-bold text-gray-700"><i class="fas fa-database text-blue-500 mr-2"></i> Kho Phim</h2>
                    <div class="flex flex-wrap gap-2 justify-end">
                        <button onclick="loadMovies()" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-1.5 rounded shadow text-sm font-bold"><i class="fas fa-sync-alt mr-1"></i> Làm mới</button>
                        <button onclick="fetchHotMovies()" class="bg-rose-500 hover:bg-rose-600 text-white px-4 py-1.5 rounded shadow text-sm font-bold"><i class="fas fa-fire mr-1"></i> Cào 30 Phim Hot</button>
                        <button onclick="healMetadata()" class="bg-indigo-500 hover:bg-indigo-600 text-white px-4 py-1.5 rounded shadow text-sm font-bold"><i class="fas fa-magic mr-1"></i> Chữa Lành Metadata</button>
                        <button onclick="repairMovies()" class="bg-orange-500 hover:bg-orange-600 text-white px-4 py-1.5 rounded shadow text-sm font-bold"><i class="fas fa-wrench mr-1"></i> Sửa thiếu tập</button>
                        <button onclick="checkFixAll()" class="bg-purple-500 hover:bg-purple-600 text-white px-4 py-1.5 rounded shadow text-sm font-bold"><i class="fas fa-check-double mr-1"></i> Check + Sửa tất cả</button>
                        <button onclick="purgeFakeMovies()" class="bg-fuchsia-600 hover:bg-fuchsia-700 text-white px-4 py-1.5 rounded shadow text-sm font-bold"><i class="fas fa-skull-crossbones mr-1"></i> Xóa Phim Ảo</button>
                        <button onclick="syncAllDrive()" class="bg-teal-500 hover:bg-teal-600 text-white px-4 py-1.5 rounded shadow text-sm font-bold"><i class="fas fa-cloud-download-alt mr-1"></i> Đồng bộ Drive</button>
                        <button onclick="deletePendingMovies()" class="bg-red-600 hover:bg-red-700 text-white px-4 py-1.5 rounded shadow text-sm font-bold"><i class="fas fa-trash-alt mr-1"></i> Xóa hết Pending</button>
                    </div>
                </div>
                <div class="overflow-x-auto mt-2"><table class="w-full text-left border-collapse text-sm"><thead><tr class="bg-gray-50 text-gray-500 uppercase text-xs border-b"><th class="p-3">Series ID / Tên Phim</th><th class="p-3">Thể loại</th><th class="p-3 text-center">Trạng Thái</th><th class="p-3 text-center">Worker</th><th class="p-3 text-center">Tổng</th><th class="p-3 text-center">Có Link</th><th class="p-3 text-center">Cập nhật</th><th class="p-3 text-center">Xác thực</th></tr></thead><tbody id="moviesTableBody"></tbody></table></div>
                <div id="verifyResult" class="mt-4 hidden bg-gray-50 p-4 rounded-lg border border-gray-200 text-sm"></div>
            </div>
        </div>

        <div id="tab-workers" class="tab-content hidden max-w-7xl mx-auto space-y-4 px-4"><div class="bg-white p-4 rounded-lg shadow-sm border border-gray-100"><div class="flex justify-between items-center mb-4 border-b pb-2"><h2 class="text-lg font-bold text-gray-700"><i class="fas fa-robot text-blue-500 mr-2"></i> Quản Lý Worker</h2><button onclick="loadWorkers()" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-1.5 rounded shadow text-sm font-bold"><i class="fas fa-sync-alt mr-1"></i> Làm mới</button></div><div id="workerStats" class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4"></div><div class="overflow-x-auto"><table class="w-full text-left border-collapse text-sm"><thead><tr class="bg-gray-50 text-gray-500 uppercase text-xs border-b"><th class="p-3">Worker ID</th><th class="p-3 text-center">Trạng thái</th><th class="p-3">Chi tiết</th><th class="p-3 text-center">Heartbeat</th><th class="p-3 text-center">Hành động</th></tr></thead><tbody id="workerTableBody"></tbody></table></div><div id="workerEmpty" class="hidden text-center text-gray-400 py-8"><i class="fas fa-robot text-4xl mb-2"></i><p>Chưa có Worker kết nối.</p></div></div></div>

        </div><!-- /main-content -->

        <script>
            // ==========================================
            // 🔒 QUẢN LÝ ADMIN TOKEN
            // ==========================================
            let adminToken = sessionStorage.getItem('adminToken') || '';

            // Kiểm tra token cũ còn hợp lệ không
            if (adminToken) { checkAdminSession(); }

            async function adminLogin() {
                const pwd = document.getElementById('admin-password').value;
                try {
                    const res = await fetch('/api/admin/login', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({password: pwd})
                    });
                    if (res.ok) {
                        const data = await res.json();
                        adminToken = data.token;
                        sessionStorage.setItem('adminToken', adminToken);
                        document.getElementById('login-overlay').classList.add('hidden');
                        document.getElementById('main-content').classList.remove('hidden');
                        loadOverviewStats(); loadHonggouUsers(); loadDouyinUsers(); loadAllInOneUsers();
                    } else {
                        document.getElementById('login-error').classList.remove('hidden');
                    }
                } catch(e) { alert('Lỗi kết nối server!'); }
            }

            function adminLogout() {
                sessionStorage.removeItem('adminToken');
                adminToken = '';
                document.getElementById('main-content').classList.add('hidden');
                document.getElementById('login-overlay').classList.remove('hidden');
                document.getElementById('admin-password').value = '';
            }

            async function checkAdminSession() {
                try {
                    const res = await adminFetch('/api/admin/stats');
                    if (res.ok) {
                        document.getElementById('login-overlay').classList.add('hidden');
                        document.getElementById('main-content').classList.remove('hidden');
                        loadOverviewStats(); loadHonggouUsers(); loadDouyinUsers(); loadAllInOneUsers();
                    } else { adminLogout(); }
                } catch(e) { adminLogout(); }
            }

            // 🔒 Wrapper: Tự động gắn Admin token vào mọi request
            function adminFetch(url, options = {}) {
                if (!options.headers) options.headers = {};
                options.headers['Authorization'] = 'Admin ' + adminToken;
                return fetch(url, options).then(res => {
                    if (res.status === 401) { adminLogout(); throw new Error('Hết phiên đăng nhập'); }
                    return res;
                });
            }

            function switchTab(tabId, btnElement) {
                document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
                document.getElementById(tabId).classList.remove('hidden');
                document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active', 'bg-gray-100'));
                btnElement.classList.add('active', 'bg-gray-100');
                if(tabId === 'tab-movies') loadMovies();
                if(tabId === 'tab-workers') loadWorkers();
            }

            async function loadOverviewStats() {
                try {
                    const res = await adminFetch('/api/admin/stats');
                    const data = await res.json();
                    document.getElementById('card-total-users').innerText = data.total_users;
                    document.getElementById('card-honggou-users').innerText = data.honggou_users;
                    document.getElementById('card-douyin-users').innerText = data.douyin_users;
                    document.getElementById('card-allinone-users').innerText = data.allinone_users;
                    document.getElementById('card-vip-users').innerText = data.vip_count;
                    document.getElementById('card-normal-users').innerText = data.normal_count;
                    document.getElementById('card-balance').innerText = data.total_balance_hongguo.toLocaleString('vi-VN') + ' đ';
                } catch(e) {}
            }

            function renderUserRow(u, platform) {
                let hwidBadge = (u.hwid && u.hwid.trim() !== "") ? '<button onclick="resetHwid(\\'' + u.username + '\\', \\'' + platform + '\\')" class="bg-green-100 text-green-600 px-3 py-1 rounded-full text-xs font-bold border border-green-200">Đã khóa</button>' : '<span class="bg-gray-100 text-gray-500 px-3 py-1 rounded-full text-xs font-bold border border-green-200">Trống</span>';
                let balanceCol = platform === 'honggou' ? '<td class="p-3 text-center"><div class="font-bold text-emerald-500 mb-1">' + (u.balance_hongguo || 0).toLocaleString('vi-VN') + ' đ</div><button onclick="addBalanceHongguo(\\'' + u.username + '\\')" class="bg-emerald-100 hover:bg-emerald-200 text-emerald-600 border border-emerald-200 px-2 py-0.5 rounded text-xs font-bold">Nạp Tiền</button></td>' : '';
                return '<tr class="border-b border-gray-100 hover:bg-gray-50"><td class="p-3 font-bold text-gray-700">' + u.username + '</td><td class="p-3 text-center"><div class="text-pink-500 font-semibold text-xs mb-1">' + (u.expiry_date || 'Chưa VIP') + '</div><button onclick="addVip(\\'' + u.username + '\\', \\'' + platform + '\\')" class="bg-purple-100 hover:bg-purple-200 text-purple-600 border border-purple-200 px-2 py-0.5 rounded text-xs font-bold">Cộng VIP</button></td>' + balanceCol + '<td class="p-3 text-center">' + hwidBadge + '</td><td class="p-3 text-center"><button onclick="resetPwd(\\'' + u.username + '\\', \\'' + platform + '\\')" class="text-blue-500 hover:text-blue-700 bg-blue-50 p-1.5 rounded border border-blue-100 mr-1" title="Reset MK"><i class="fas fa-key"></i></button><button onclick="deleteUser(\\'' + u.username + '\\', \\'' + platform + '\\')" class="text-red-500 hover:text-red-700 bg-red-50 p-1.5 rounded border border-red-100" title="Xóa"><i class="fas fa-trash"></i></button></td></tr>';
            }

            async function loadHonggouUsers() { try { const res = await adminFetch('/api/admin/users?platform=honggou'); const users = await res.json(); document.getElementById('honggouTableBody').innerHTML = users.map(u => renderUserRow(u, 'honggou')).join(''); } catch(e) {} }
            async function loadDouyinUsers() { try { const res = await adminFetch('/api/admin/users?platform=douyin'); const users = await res.json(); document.getElementById('douyinTableBody').innerHTML = users.map(u => renderUserRow(u, 'douyin')).join(''); } catch(e) {} }
            async function loadAllInOneUsers() { try { const res = await adminFetch('/api/admin/users?platform=allinone'); const users = await res.json(); document.getElementById('allinoneTableBody').innerHTML = users.map(u => renderUserRow(u, 'allinone')).join(''); } catch(e) {} }

            async function addBalanceHongguo(username) { let amount = prompt('Nạp tiền cho [' + username + ']:'); if(amount && !isNaN(amount)) { await adminFetch('/api/admin/users/' + username + '/add_balance_hongguo', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ amount: parseInt(amount) }) }); loadHonggouUsers(); loadOverviewStats(); } }
            async function addVip(username, platform) { let days = prompt('Cộng ngày VIP cho [' + username + ']:', '30'); if(days && !isNaN(days)) { await adminFetch('/api/admin/users/' + username + '/add_vip?platform=' + platform, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ days: parseInt(days) }) }); if(platform==='honggou') loadHonggouUsers(); else if(platform==='douyin') loadDouyinUsers(); else loadAllInOneUsers(); loadOverviewStats(); } }
            async function resetHwid(username, platform) { if(confirm('Mở khóa HWID cho [' + username + ']?')) { await adminFetch('/api/admin/users/' + username + '/reset_hwid?platform=' + platform, { method: 'POST' }); if(platform==='honggou') loadHonggouUsers(); else if(platform==='douyin') loadDouyinUsers(); else loadAllInOneUsers(); } }
            async function resetPwd(username, platform) { if(confirm('Reset mật khẩu [' + username + '] về "123456"?')) { const res = await adminFetch('/api/admin/users/' + username + '/reset_password?platform=' + platform, { method: 'POST' }); const d = await res.json(); alert(d.message); } }
            async function deleteUser(username, platform) { if(confirm('Xóa vĩnh viễn [' + username + ']?')) { await adminFetch('/api/admin/users/' + username + '/delete?platform=' + platform, { method: 'POST' }); if(platform==='honggou') loadHonggouUsers(); else if(platform==='douyin') loadDouyinUsers(); else loadAllInOneUsers(); loadOverviewStats(); } }

            async function loadMovies() {
                try {
                    const res = await adminFetch('/api/admin/honggou_movies'); const movies = await res.json();
                    let html = ''; const sc = {'completed': 'bg-green-100 text-green-700', 'processing': 'bg-yellow-100 text-yellow-700', 'pending': 'bg-blue-100 text-blue-700', 'partial': 'bg-red-100 text-red-700'};
                    const stMap = {'completed': 'Hoàn thành', 'processing': 'Đang tải', 'pending': 'Chờ xử lý', 'partial': 'Thiếu tập'};
                    let cC=0, cPe=0, cPr=0, cPa=0;
                    movies.forEach(m => {
                        if(m.status==='completed') cC++; else if(m.status==='pending') cPe++; else if(m.status==='processing') cPr++; else if(m.status==='partial') cPa++;
                        const sClass = sc[m.status] || 'bg-gray-100 text-gray-700'; const ds = stMap[m.status] || m.status;
                        const hl = m.db_has_link||0; const tot = m.total_episodes||0; const ok = (hl>=tot && tot>0);
                        const pct = tot>0 ? Math.min(100, Math.round((hl/tot)*100)) : 0;
                        const barC = ok ? '#10b981' : '#f59e0b'; const txtC = ok ? 'text-green-600' : 'text-orange-500';
                        const titleD = m.title ? m.title : '<span class="text-red-400 italic text-xs">Thiếu tên</span>';
                        const genreD = m.genres ? m.genres : '<span class="text-gray-400 italic text-xs">Chưa có</span>';
                        html += '<tr class="border-b border-gray-100 hover:bg-gray-50"><td class="p-3 font-mono text-xs font-bold">' + m.series_id + '<br><span class="text-gray-500 font-sans text-sm">' + titleD + '</span></td><td class="p-3 text-xs text-blue-600 font-medium max-w-[150px] truncate">' + genreD + '</td><td class="p-3 text-center"><span class="px-2 py-1 rounded-full text-xs font-bold ' + sClass + '">' + ds + '</span></td><td class="p-3 text-center text-xs">' + (m.worker_id||'—') + '</td><td class="p-3 text-center font-bold">' + tot + '</td><td class="p-3 text-center"><div class="flex items-center gap-2 justify-center"><div class="w-20 bg-gray-200 rounded-full h-2"><div class="h-2 rounded-full" style="width:' + pct + '%;background:' + barC + '"></div></div><span class="font-bold ' + txtC + '">' + hl + '/' + tot + '</span></div></td><td class="p-3 text-center text-xs text-gray-500">' + (m.updated_at||'—') + '</td><td class="p-3 text-center"><button onclick="verifyMovie(\\'' + m.job_id + '\\', \\'' + m.series_id + '\\')" class="bg-indigo-100 hover:bg-indigo-200 text-indigo-600 px-3 py-1 rounded text-xs font-bold">Check</button></td></tr>';
                    });
                    document.getElementById('count-completed').innerText = cC; document.getElementById('count-pending').innerText = cPe;
                    document.getElementById('count-processing').innerText = cPr; document.getElementById('count-partial').innerText = cPa;
                    document.getElementById('moviesTableBody').innerHTML = html;
                } catch(e) {}
            }

            async function fetchHotMovies() { if(!confirm('Cào 30 phim hot?')) return; const r = document.getElementById('verifyResult'); r.classList.remove('hidden'); r.innerHTML = '<p class="text-rose-500 font-bold"><i class="fas fa-spinner fa-spin mr-2"></i> Đang cào...</p>'; try { const res = await adminFetch('/api/admin/fetch_hot_movies', { method: 'POST' }); const d = await res.json(); r.innerHTML = d.status==='success' ? '<p class="text-green-600 font-bold">' + d.message + '</p>' : '<p class="text-red-500 font-bold">' + d.message + '</p>'; loadMovies(); } catch(e) { r.innerHTML = '<p class="text-red-500">Lỗi: ' + e + '</p>'; } }
            async function deletePendingMovies() { if(!confirm('Xóa TẤT CẢ phim Pending?')) return; try { const res = await adminFetch('/api/admin/delete_pending_movies', { method: 'POST' }); const d = await res.json(); alert(d.message); loadMovies(); } catch(e) {} }
            async function healMetadata() { if(!confirm('Quét metadata cho tất cả phim?')) return; const r = document.getElementById('verifyResult'); r.classList.remove('hidden'); r.innerHTML = '<p class="text-indigo-500 font-bold"><i class="fas fa-spinner fa-spin mr-2"></i> Đang quét...</p>'; try { const res = await adminFetch('/api/admin/heal_metadata', { method: 'POST' }); const d = await res.json(); r.innerHTML = d.status==='ok' ? '<p class="text-green-600 font-bold">' + d.message + '</p>' : '<p class="text-red-500 font-bold">' + d.message + '</p>'; loadMovies(); } catch(e) {} }
            async function syncAllDrive() { if(!confirm('Đồng bộ Drive?')) return; const r = document.getElementById('verifyResult'); r.classList.remove('hidden'); r.innerHTML = '<p class="text-teal-500 font-bold"><i class="fas fa-spinner fa-spin mr-2"></i> Đang đồng bộ...</p>'; try { const res = await adminFetch('/api/admin/sync_all_drive_to_db', { method: 'POST' }); const d = await res.json(); r.innerHTML = d.status==='ok' ? '<p class="text-green-600 font-bold">' + d.message + '</p>' : '<p class="text-red-500 font-bold">' + d.message + '</p>'; loadMovies(); } catch(e) {} }
            async function checkFixAll() { if(!confirm('Check + Sửa tất cả?')) return; const r = document.getElementById('verifyResult'); r.classList.remove('hidden'); r.innerHTML = '<p class="text-purple-500 font-bold"><i class="fas fa-spinner fa-spin mr-2"></i> Đang quét...</p>'; try { const res = await adminFetch('/api/admin/check_fix_all', { method: 'POST' }); const d = await res.json(); let h = '<h3 class="font-bold text-gray-700 mb-3">Kết quả</h3><p>Tổng: ' + d.total_checked + ' | Sửa: ' + d.fixed + '</p><div class="space-y-1 max-h-60 overflow-y-auto mt-2">'; (d.details||[]).forEach(l => { h += '<p class="text-sm">' + l + '</p>'; }); h += '</div>'; r.innerHTML = h; loadMovies(); } catch(e) {} }
            async function purgeFakeMovies() { if(!confirm('Xóa phim ảo?')) return; const r = document.getElementById('verifyResult'); r.classList.remove('hidden'); r.innerHTML = '<p class="text-fuchsia-600 font-bold"><i class="fas fa-spinner fa-spin mr-2"></i> Đang quét Drive...</p>'; try { const res = await adminFetch('/api/admin/purge_fake_movies', { method: 'POST' }); const d = await res.json(); let h = '<p class="font-bold text-red-500 mb-2">Xóa: ' + d.deleted + ' phim</p><div class="space-y-1 max-h-60 overflow-y-auto text-sm">'; (d.details||[]).forEach(l => { h += '<p>' + l + '</p>'; }); h += '</div>'; r.innerHTML = h; loadMovies(); } catch(e) {} }
            async function repairMovies() { if(!confirm('Sửa phim thiếu tập?')) return; try { const res = await adminFetch('/api/admin/repair_movies', { method: 'POST' }); const d = await res.json(); alert('Đã sửa ' + d.fixed + ' phim.'); loadMovies(); } catch(e) {} }

            async function verifyMovie(jobId, sid) {
                const r = document.getElementById('verifyResult'); r.classList.remove('hidden');
                r.innerHTML = '<p class="text-blue-500 font-bold"><i class="fas fa-spinner fa-spin mr-2"></i> Đang quét...</p>';
                try {
                    const res = await adminFetch('/api/admin/verify_movie/' + jobId); const d = await res.json();
                    let dup = '';
                    if (d.db_duplicates && d.db_duplicates.length > 0) { let dt = d.db_duplicates.map(x => 'Tập ' + x.ep + ' (' + x.count + 'x)').join(', '); dup = '<div class="mt-2 p-2 bg-red-50 border border-red-200 rounded"><b class="text-red-600">TRÙNG:</b> ' + dt + '</div>'; }
                    let mdb = (d.missing_db_count > 0) ? '<span class="text-red-500 font-bold">Thiếu ' + d.missing_db_count + ': [' + d.missing_db.join(',') + ']</span>' : '<span class="text-green-500 font-bold">Đủ</span>';
                    let mdr = (d.missing_drive_count > 0) ? '<span class="text-red-500 font-bold">Thiếu ' + d.missing_drive_count + ': [' + d.missing_drive.join(',') + ']</span>' : '<span class="text-green-500 font-bold">Đủ</span>';
                    let needFix = (d.missing_db_count > 0 || d.missing_drive_count > 0 || (d.web_total > 0 && d.web_total != d.total_episodes_db));
                    let wt = d.web_total || '?'; let title = d.web_title || sid; let fixBtn = '';
                    if (needFix) {
                        let mf = wt !== '?' ? wt : d.db_episodes;
                        fixBtn = '<div class="mt-4 p-3 bg-red-50 border border-red-200 rounded-lg flex flex-wrap items-center gap-3"><input type="number" id="mt_' + jobId + '" value="' + mf + '" class="border border-red-300 rounded px-3 py-1.5 w-24 text-center font-bold"><button onclick="fixMovie(\\'' + jobId + '\\', document.getElementById(\\'mt_' + jobId + '\\').value)" class="bg-red-500 hover:bg-red-600 text-white px-4 py-1.5 rounded font-bold text-sm">Sửa</button><button onclick="syncDriveToDb(\\'' + jobId + '\\')" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-1.5 rounded font-bold text-sm">Kéo Drive</button><button onclick="deleteSingleMovie(\\'' + jobId + '\\')" class="bg-gray-800 hover:bg-gray-900 text-white px-4 py-1.5 rounded font-bold text-sm">Xóa</button></div>';
                    } else { fixBtn = '<div class="mt-3 text-green-600 font-bold bg-green-50 p-2 border border-green-200 rounded flex justify-between items-center"><span>OK!</span><button onclick="deleteSingleMovie(\\'' + jobId + '\\')" class="bg-gray-800 hover:bg-gray-900 text-white px-4 py-1.5 rounded font-bold text-sm">Xóa</button></div>'; }
                    r.innerHTML = '<h3 class="font-bold text-gray-700 mb-3">' + title + '</h3><div class="grid grid-cols-3 gap-4 mb-3"><div class="bg-white p-3 rounded border"><div class="text-xs text-gray-500">Web</div><div class="text-2xl font-bold text-blue-600">' + wt + '</div></div><div class="bg-white p-3 rounded border"><div class="text-xs text-gray-500">DB</div><div class="text-2xl font-bold text-purple-600">' + d.db_episodes + '</div></div><div class="bg-white p-3 rounded border"><div class="text-xs text-gray-500">Drive</div><div class="text-2xl font-bold text-green-600">' + d.drive_episodes + '</div></div></div><p><b>DB:</b> ' + mdb + '</p><p><b>Drive:</b> ' + mdr + '</p>' + dup + fixBtn;
                } catch(e) {}
            }

            async function fixMovie(jobId, webTotal) { try { const res = await adminFetch('/api/admin/fix_movie/' + jobId + '?web_total=' + (webTotal||0), { method: 'POST' }); const d = await res.json(); alert(d.message); loadMovies(); document.getElementById('verifyResult').classList.add('hidden'); } catch(e) {} }
            async function syncDriveToDb(jobId) { if(!confirm('Kéo link từ Drive?')) return; const r = document.getElementById('verifyResult'); r.innerHTML = '<p class="text-blue-500 font-bold"><i class="fas fa-spinner fa-spin mr-2"></i> Đang kéo...</p>'; try { const res = await adminFetch('/api/admin/sync_drive_to_db/' + jobId, { method: 'POST' }); const d = await res.json(); alert(d.message); loadMovies(); r.classList.add('hidden'); } catch(e) {} }
            async function deleteSingleMovie(jobId) { if(!confirm('Xóa vĩnh viễn phim này?')) return; try { const res = await adminFetch('/api/admin/delete_movie/' + jobId, { method: 'POST' }); const d = await res.json(); alert(d.message); if(d.status==='success') { document.getElementById('verifyResult').classList.add('hidden'); loadMovies(); } } catch(e) {} }

            async function loadWorkers() {
                try {
                    const res = await adminFetch('/api/admin/workers'); const workers = await res.json();
                    if (workers.length === 0) { document.getElementById('workerTableBody').innerHTML = ''; document.getElementById('workerEmpty').classList.remove('hidden'); document.getElementById('workerStats').innerHTML = ''; return; }
                    document.getElementById('workerEmpty').classList.add('hidden');
                    let cW=0, cI=0, cB=0, cO=0;
                    workers.forEach(w => { if(w.status==='working') cW++; else if(w.status==='idle') cI++; else if(w.status==='blocked') cB++; else cO++; });
                    document.getElementById('workerStats').innerHTML = '<div class="bg-green-50 p-3 rounded border border-green-200 text-center"><div class="text-xs text-green-600 font-bold">LÀM VIỆC</div><div class="text-2xl font-bold text-green-700">' + cW + '</div></div><div class="bg-yellow-50 p-3 rounded border border-yellow-200 text-center"><div class="text-xs text-yellow-600 font-bold">RẢNH</div><div class="text-2xl font-bold text-yellow-700">' + cI + '</div></div><div class="bg-red-50 p-3 rounded border border-red-200 text-center"><div class="text-xs text-red-600 font-bold">DỪNG</div><div class="text-2xl font-bold text-red-700">' + cB + '</div></div><div class="bg-gray-50 p-3 rounded border border-gray-200 text-center"><div class="text-xs text-gray-600 font-bold">OFFLINE</div><div class="text-2xl font-bold text-gray-700">' + cO + '</div></div>';
                    const sm = { 'working': '<span class="px-2 py-1 rounded-full text-xs font-bold bg-green-100 text-green-700">Đang làm</span>', 'idle': '<span class="px-2 py-1 rounded-full text-xs font-bold bg-yellow-100 text-yellow-700">Rảnh</span>', 'blocked': '<span class="px-2 py-1 rounded-full text-xs font-bold bg-red-100 text-red-700">Dừng</span>', 'stuck': '<span class="px-2 py-1 rounded-full text-xs font-bold bg-orange-100 text-orange-700">Kẹt</span>', 'offline': '<span class="px-2 py-1 rounded-full text-xs font-bold bg-gray-100 text-gray-500">Offline</span>' };
                    let html = '';
                    workers.forEach(w => {
                        let ago = w.ago_seconds; let at = '—';
                        if (w.last_seen > 0) { if(ago<60) at=ago+' giây'; else if(ago<3600) at=Math.floor(ago/60)+' phút'; else at=Math.floor(ago/3600)+' giờ'; }
                        let aH = w.action ? '<br><span class="text-xs text-blue-600 font-medium italic bg-blue-50 px-2 py-0.5 rounded border border-blue-200 mt-1 inline-block">' + w.action + '</span>' : '';
                        let jI = w.job ? '<span class="font-bold">' + w.job.title + '</span><br><span class="text-xs text-gray-500">' + w.job.progress + '</span>' + aH : (w.action ? aH : '—');
                        let aBtn = w.blocked ? '<button onclick="activateWorker(\\'' + w.worker_id + '\\')" class="bg-green-500 hover:bg-green-600 text-white px-3 py-1 rounded text-xs font-bold">Kích hoạt</button>' : '<button onclick="stopWorker(\\'' + w.worker_id + '\\')" class="bg-red-500 hover:bg-red-600 text-white px-3 py-1 rounded text-xs font-bold">Dừng</button>';
                        aBtn += ' <button onclick="resetWorker(\\'' + w.worker_id + '\\')" class="bg-orange-500 hover:bg-orange-600 text-white px-3 py-1 rounded text-xs font-bold">Reset</button>';
                        html += '<tr class="border-b border-gray-100 hover:bg-gray-50"><td class="p-3 font-bold text-gray-700"><i class="fas fa-robot mr-2 text-blue-400"></i>' + w.worker_id + '</td><td class="p-3 text-center">' + (sm[w.status]||sm['offline']) + '</td><td class="p-3">' + jI + '</td><td class="p-3 text-center text-xs text-gray-500">' + at + '</td><td class="p-3 text-center">' + aBtn + '</td></tr>';
                    });
                    document.getElementById('workerTableBody').innerHTML = html;
                } catch(e) {}
            }

            async function stopWorker(wid) { if(!confirm('Dừng worker [' + wid + ']?')) return; try { await adminFetch('/api/admin/workers/' + wid + '/stop', { method: 'POST' }); loadWorkers(); } catch(e) {} }
            async function activateWorker(wid) { if(!confirm('Kích hoạt [' + wid + ']?')) return; try { await adminFetch('/api/admin/workers/' + wid + '/activate', { method: 'POST' }); loadWorkers(); } catch(e) {} }
            async function resetWorker(wid) { if(!confirm('Reset worker [' + wid + ']?')) return; try { await adminFetch('/api/admin/workers/' + wid + '/reset', { method: 'POST' }); alert('Đã gửi lệnh Reset!'); loadWorkers(); } catch(e) {} }

            setInterval(() => { if(!document.getElementById('tab-workers').classList.contains('hidden')){ loadWorkers(); } }, 10000);
        </script>
    </body>
    </html>
    """

# ==========================================
# KHỞI ĐỘNG SERVER
# ==========================================
if __name__ == "__main__":
    print("=" * 60)
    print("🔒 SERVER ANHSTUDIO (BẢO MẬT) ĐÃ SẴN SÀNG")
    print(f"   Admin Dashboard: http://0.0.0.0:8000/admin")
    print(f"   Admin Password:  {ADMIN_PASSWORD[:4]}{'*' * (len(ADMIN_PASSWORD)-4)}")
    print(f"   JWT Secret:      {JWT_SECRET[:8]}...")
    print(f"   Worker Secret:   {WORKER_SECRET[:8]}...")
    print("=" * 60)
    uvicorn.run("server_secure:app", host="0.0.0.0", port=8000, workers=1)
