# -*- coding: utf-8 -*-
import os
import sys
import time
import glob
import subprocess
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pyautogui
pyautogui.FAILSAFE = False
import pyperclip
import re
import shutil
import concurrent.futures
import threading

# Import thông tin định danh từ file config (CHỈ CÓ FILE CONFIG LÀ KHÁC NHAU GIỮA CÁC VPS)
from config import SERVER_URL, WORKER_SECRET, WORKER_ID

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ============================================================
# CỐ ĐỊNH TOÀN BỘ ĐƯỜNG DẪN VÀO THƯ MỤC CHỨA SCRIPT NÀY
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STATUS_FILE = os.path.join(BASE_DIR, "worker_status.txt")
CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.json")
TOKEN_FILE = os.path.join(BASE_DIR, "token.json")

# File ảnh nhận diện (Nằm chung thư mục)
IMG_O_NHAP = os.path.join(BASE_DIR, "o_nhap.png")
IMG_NUT_QUET = os.path.join(BASE_DIR, "nut_quet.png")
IMG_NUT_TAI = os.path.join(BASE_DIR, "nut_tai_xanh.png")

# File FFmpeg nằm chung thư mục
FFMPEG_PATH = os.path.join(BASE_DIR, "ffmpeg.exe")

# Thư mục tải phim sẽ tự động tạo ngay bên trong folder worker
DOWNLOAD_BASE_DIR = os.path.join(BASE_DIR, "phimtaive", "hongguo")

EXE_PATH = os.path.join(BASE_DIR, "ReupStudio.lnk")
MAX_WORKERS = 5   
POLL_INTERVAL = 2

# ============================================================
# HTTP SESSION BẤT TỬ CHỐNG RỚT MẠNG
# ============================================================
http = requests.Session()
retries = Retry(total=5, backoff_factor=2, status_forcelist=[500, 502, 503, 504], allowed_methods=["HEAD", "GET", "OPTIONS", "POST"])
adapter = HTTPAdapter(max_retries=retries)
http.mount("http://", adapter); http.mount("https://", adapter)
http.headers.update({"Authorization": WORKER_SECRET})

current_worker_action = "Đang khởi động..."
expected_total_global = 0
active_job_id = None

def update_local_status(text):
    global current_worker_action
    current_worker_action = text
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception: pass

def background_heartbeat():
    global current_worker_action
    while True:
        if active_job_id and active_job_id != "recovery":
            try:
                http.post(f"{SERVER_URL}/api/worker/update_scan", json={"job_id": active_job_id, "total_episodes": expected_total_global, "action": current_worker_action}, timeout=10)
            except Exception: pass
        time.sleep(10)

def scrape_series_info(url):
    import json
    info = {"total_episodes": 0, "cover_url": "", "title": ""}
    headers = {"User-Agent": "Mozilla/5.0"}
    for attempt in range(1, 4):
        if attempt > 1: time.sleep(3)
        try:
            resp = http.get(url, headers=headers, timeout=30); resp.raise_for_status(); html = resp.text
            match = re.search(r'window\._ROUTER_DATA\s*=\s*(\{.+?\})\s*;?\s*</script>', html, re.DOTALL)
            if match:
                raw = match.group(1); data = json.loads(raw); detail = data.get("loaderData", {}).get("detail_page", {}).get("seriesDetail", {})
                if detail:
                    info["title"] = detail.get("series_name", ""); info["cover_url"] = detail.get("series_cover", ""); right_text = detail.get("episode_right_text", "")
                    if right_text:
                        num_match = re.search(r'(\d+)', right_text)
                        if num_match: info["total_episodes"] = int(num_match.group(1))
                    if info["total_episodes"] == 0:
                        vid_list = detail.get("vid_list", [])
                        if isinstance(vid_list, list) and len(vid_list) > 0: info["total_episodes"] = len(vid_list)
                    if info["total_episodes"] == 0: info["total_episodes"] = detail.get("episode_cnt", 0)
                    if info["total_episodes"] > 0: return info

            if not info["title"]:
                title_match = re.search(r'"series_name"\s*:\s*"([^"]+)"', html)
                if title_match: info["title"] = title_match.group(1)
            if not info["cover_url"]:
                cover_match = re.search(r'"series_cover"\s*:\s*"([^"]+)"', html)
                if cover_match: info["cover_url"] = cover_match.group(1)
            if info["total_episodes"] == 0:
                ep_match = re.search(r'"episode_cnt"\s*:\s*(\d+)', html)
                if ep_match: info["total_episodes"] = int(ep_match.group(1))
            if info["total_episodes"] > 0: return info
        except Exception: pass
    return info

# ============================================================
# XỬ LÝ GOOGLE DRIVE
# ============================================================
SCOPES = ['https://www.googleapis.com/auth/drive']
DRIVE_PARENT_FOLDER_ID = "1QVP3Mh86LGLsEIQSyojZ6DBkjGUdWv3c"  
_drive_creds = None
_creds_lock = threading.Lock()

def _get_creds():
    global _drive_creds
    with _creds_lock:
        if _drive_creds is None and os.path.exists(TOKEN_FILE): _drive_creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if _drive_creds and not _drive_creds.valid:
            if _drive_creds.expired and _drive_creds.refresh_token: _drive_creds.refresh(Request())
            with open(TOKEN_FILE, 'w') as f: f.write(_drive_creds.to_json())
        if _drive_creds is None or not _drive_creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            _drive_creds = flow.run_local_server(port=0)
            with open(TOKEN_FILE, 'w') as f: f.write(_drive_creds.to_json())
        return _drive_creds

_thread_local = threading.local()

def get_drive_service():
    if not hasattr(_thread_local, 'service') or _thread_local.service is None:
        creds = _get_creds(); _thread_local.service = build('drive', 'v3', credentials=creds)
    return _thread_local.service

def create_drive_folder(folder_name, parent_id):
    service = get_drive_service()
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and '{parent_id}' in parents and trashed=false"
    results = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
    files = results.get('files', [])
    if files: return files[0]['id']
    metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
    folder = service.files().create(body=metadata, fields='id').execute()
    service.permissions().create(fileId=folder['id'], body={'type': 'anyone', 'role': 'reader'}).execute()
    return folder['id']

def upload_file_to_drive(file_path, file_name, folder_id):
    service = get_drive_service()
    metadata = {'name': file_name, 'parents': [folder_id]}
    media = MediaFileUpload(file_path, mimetype='video/mp4', resumable=True, chunksize=25 * 1024 * 1024)
    request = service.files().create(body=metadata, media_body=media, fields='id')
    response = None
    while response is None: status, response = request.next_chunk()
    service.permissions().create(fileId=response['id'], body={'type': 'anyone', 'role': 'reader'}).execute()
    return f"https://drive.google.com/file/d/{response['id']}/view"

def verify_drive_folder(folder_id):
    real_eps = set()
    try:
        service = get_drive_service()
        query = f"'{folder_id}' in parents and trashed=false and mimeType='video/mp4'"
        results = service.files().list(q=query, fields='files(name)', pageSize=500).execute()
        files = results.get('files', [])
        for f in files:
            ep = extract_episode_number(f['name'])
            if ep > 0: real_eps.add(ep)
    except Exception: pass
    return real_eps

# ============================================================
# CÁC HÀM XỬ LÝ LÕI
# ============================================================
def extract_episode_number(filename):
    match = re.search(r'Tập\s*(\d+)', filename, re.IGNORECASE)
    if match: return int(match.group(1))
    return 0

def remux_and_validate(input_path):
    output_path = input_path.replace(".mp4", "_remux.mp4")
    for attempt in range(1, 4):
        try:
            if not os.path.exists(input_path): return None
            file_size = os.path.getsize(input_path)
            if file_size < 500 * 1024:
                if attempt < 3: time.sleep(10); continue
                else: return None

            # Dùng FFMPEG nội bộ của thư mục
            cmd_check = [FFMPEG_PATH, "-v", "info", "-t", "20", "-i", input_path, "-vf", "blackdetect=d=2:pix_th=0.1", "-f", "null", "-"]
            result = subprocess.run(cmd_check, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
            output_log = result.stderr.lower()
            
            if "error while decoding" in output_log or "moov atom not found" in output_log:
                if attempt < 3: time.sleep(10); continue
                else: return None
                
            black_matches = re.findall(r'black_duration:([0-9.]+)', output_log)
            if black_matches and sum(float(t) for t in black_matches) > 5.0: return None 

            cmd_remux = [FFMPEG_PATH, "-y", "-i", input_path, "-c", "copy", "-movflags", "+faststart", output_path]
            remux_result = subprocess.run(cmd_remux, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
            
            if remux_result.returncode == 0:
                os.remove(input_path); os.rename(output_path, input_path); return input_path
            else:
                if os.path.exists(output_path): os.remove(output_path)
                if attempt < 3: time.sleep(10); continue
                return None
        except Exception:
            if os.path.exists(output_path): os.remove(output_path)
            if attempt < 3: time.sleep(10); continue
            return None
    return None

def find_and_click(image_path, label, timeout=120, interval=1.5, confidence=0.8):
    max_retries = int(timeout / interval)
    pyautogui.moveTo(pyautogui.size()[0] // 2, pyautogui.size()[1] // 2)
    for i in range(max_retries):
        try:
            btn = pyautogui.locateCenterOnScreen(image_path, confidence=confidence)
            if btn is not None:
                pyautogui.moveTo(btn.x, btn.y, duration=0.25); time.sleep(0.2); pyautogui.mouseDown(); time.sleep(0.1); pyautogui.mouseUp()
                return True
        except Exception: pass
        time.sleep(interval)
    return False

def kill_reupstudio(): os.system("taskkill /f /im ReupStudio.exe >nul 2>&1")
def is_reupstudio_running():
    try: return "ReupStudio.exe" in subprocess.check_output('tasklist /FI "IMAGENAME eq ReupStudio.exe"', shell=True).decode(errors='replace')
    except Exception: return False

def upload_single_episode(file_path, file_name, job_id, drive_folder_id, series_id=None):
    ep_num = extract_episode_number(file_name)
    if ep_num == 0: return

    if os.path.exists(file_path):
        remux_result = remux_and_validate(file_path)
        if remux_result is None: return

    for attempt in range(1, 4):
        try:
            if not os.path.exists(file_path): return
            drive_link = upload_file_to_drive(file_path, file_name, drive_folder_id)
            payload = {"job_id": job_id, "episode_number": ep_num, "drive_link": drive_link, "file_name": file_name}
            if series_id: payload["series_id"] = series_id
            http.post(f"{SERVER_URL}/api/worker/update_episode", json=payload, timeout=30)
            if os.path.exists(file_path): os.remove(file_path)
            return
        except Exception:
            if attempt < 3: time.sleep(attempt * 10); _thread_local.service = None

def watch_and_upload(job_id, watch_folder, drive_folder_id, expected_total=0, existing_eps=None, video_link="", series_id=None):
    global expected_total_global
    if existing_eps is None: existing_eps = []
    uploaded_files = set(); pending_files = {}; completed_eps = set(existing_eps) 
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
    futures = []; idle_time = 0; heartbeat_counter = 0

    while True:
        heartbeat_counter += 1
        active_uploads = len([f for f in futures if not f.done()])
        update_local_status(f"Đang canh tải về... (Upload: {active_uploads} luồng | Xong: {len(completed_eps)}/{expected_total_global})")

        if expected_total > 0 and len(completed_eps) >= expected_total and not pending_files and active_uploads == 0: break
        if not is_reupstudio_running() and not pending_files and active_uploads == 0: break

        if not pending_files and active_uploads == 0:
            idle_time += POLL_INTERVAL
            if idle_time >= 20: 
                update_local_status("Ping báo cáo Server để xin chốt đơn...")
                try:
                    res = http.post(f"{SERVER_URL}/api/worker/verify_total", json={"job_id": job_id, "series_id": series_id, "current_count": len(completed_eps)}, timeout=30)
                    if res.status_code == 200:
                        data = res.json(); action = data.get("action"); new_total = data.get("total", 0)
                        if new_total > 0 and new_total != expected_total: expected_total = new_total; expected_total_global = expected_total
                        if action == "done" or action == "accept": break
                        else: idle_time = 0 
                except Exception: idle_time = 0 
        else: idle_time = 0 

        current_mp4s = set()
        if os.path.exists(watch_folder):
            for f in os.listdir(watch_folder):
                full = os.path.join(watch_folder, f)
                if os.path.isfile(full) and f.lower().endswith('.mp4'): current_mp4s.add(f)

        new_files = current_mp4s - uploaded_files - set(pending_files.keys())
        for fname in new_files:
            fpath = os.path.join(watch_folder, fname)
            try: pending_files[fname] = {"size": os.path.getsize(fpath), "stable_count": 0}
            except OSError: pass

        STABLE_REQUIRED = 15  
        stable_files = []
        for fname in list(pending_files.keys()):
            fpath = os.path.join(watch_folder, fname)
            try:
                if not os.path.exists(fpath): del pending_files[fname]; continue
                cur_size = os.path.getsize(fpath); info = pending_files[fname]
                if cur_size > 0 and cur_size == info["size"]:
                    try:
                        with open(fpath, 'a'): pass
                        info["stable_count"] += 1
                        if info["stable_count"] >= STABLE_REQUIRED: stable_files.append(fname)
                    except PermissionError: info["stable_count"] = 0  
                    except OSError: pass
                else: info["size"] = cur_size; info["stable_count"] = 0  
            except OSError: del pending_files[fname]

        for fname in stable_files:
            del pending_files[fname]; uploaded_files.add(fname)
            fpath = os.path.join(watch_folder, fname); ep = extract_episode_number(fname)
            if ep in existing_eps:
                try: 
                    if os.path.exists(fpath): os.remove(fpath)
                except Exception: pass
                continue 
                
            completed_eps.add(ep)
            update_local_status(f"Đang chèn Metadata & Upload Tập {ep}...")
            future = executor.submit(upload_single_episode, fpath, fname, job_id, drive_folder_id, series_id)
            futures.append(future)

        time.sleep(POLL_INTERVAL)

    if futures:
        update_local_status("Chờ các luồng Upload cuối cùng kết thúc...")
        concurrent.futures.wait(futures)
    executor.shutdown(wait=False)
    return completed_eps

def start_reupstudio_download(job_id, video_link, target_folder):
    kill_reupstudio(); time.sleep(2)
    print(f"[{job_id}] ── Mở ReupStudio ──")
    os.startfile(EXE_PATH); time.sleep(12)

    print(f"[{job_id}] ── Dán link ──")
    pyperclip.copy(video_link)
    if not find_and_click(IMG_O_NHAP, "Ô nhập URL", timeout=30): return False
    time.sleep(0.5); pyautogui.hotkey('ctrl', 'a'); time.sleep(0.2); pyautogui.press('delete'); time.sleep(0.2); pyautogui.hotkey('ctrl', 'v'); time.sleep(1)

    print(f"[{job_id}] ── Bấm nút Quét ──")
    if not find_and_click(IMG_NUT_QUET, "Nút Quét", timeout=30, confidence=0.8): return False
    time.sleep(3)

    print(f"[{job_id}] ── Chờ quét xong ──")
    quet_xong = False
    pyautogui.moveTo(pyautogui.size()[0] // 2, pyautogui.size()[1] // 2) 
    
    for attempt in range(120): 
        if not is_reupstudio_running(): return False
        try:
            if pyautogui.locateCenterOnScreen(IMG_NUT_QUET, confidence=0.8) is not None: quet_xong = True; break
        except Exception: pass
        time.sleep(1)
        
    if not quet_xong: return False
    print("  ✅ Đã lấy xong danh sách!")

    print(f"[{job_id}] ── Bấm nút Tải ──")
    max_wait_time = 300; interval = 5; attempts = int(max_wait_time / interval); file_detected = False
    pyautogui.moveTo(pyautogui.size()[0] // 2, pyautogui.size()[1] // 2)

    for i in range(attempts):
        if not is_reupstudio_running(): return False
        if os.path.exists(target_folder) and len(os.listdir(target_folder)) > 0: file_detected = True; break
        try:
            btn = pyautogui.locateCenterOnScreen(IMG_NUT_TAI, confidence=0.8)
            if btn is not None:
                pyautogui.moveTo(btn.x, btn.y, duration=0.25); time.sleep(0.2); pyautogui.mouseDown(); time.sleep(0.1); pyautogui.mouseUp()
                pyautogui.moveTo(pyautogui.size()[0] // 2, pyautogui.size()[1] // 2)
        except Exception: pass
        time.sleep(interval)

    return file_detected

def process_video_download(job_id, series_id, video_link, existing_eps):
    global expected_total_global
    
    update_local_status("Bắt đầu nhận Phim mới, chuẩn bị dọn ổ...")
    os.makedirs(DOWNLOAD_BASE_DIR, exist_ok=True)
    target_folder = os.path.join(DOWNLOAD_BASE_DIR, series_id)
    if os.path.exists(target_folder):
        try: shutil.rmtree(target_folder)
        except Exception: pass

    update_local_status("Đang quét thông tin phim từ Web...")
    series_info = scrape_series_info(video_link)
    expected_total = series_info["total_episodes"]
    expected_total_global = expected_total 
    
    update_local_status("Đang tạo Folder chứa phim trên Google Drive...")
    try: drive_folder_id = create_drive_folder(series_id, DRIVE_PARENT_FOLDER_ID)
    except Exception as e: print(f"  ❌ Lỗi Drive: {e}"); return False

    if expected_total > 0 and len(existing_eps) >= expected_total:
        update_local_status("Server báo đủ tập, đang kiểm tra trực tiếp ổ Drive...")
        real_eps = verify_drive_folder(drive_folder_id)
        if real_eps:
            missing_eps = [ep for ep in range(1, expected_total + 1) if ep not in real_eps]
            if not missing_eps:
                update_local_status("Ổ Drive đã chứa đủ 100% phim, bỏ qua...")
                try: http.post(f"{SERVER_URL}/api/worker/complete_job", json={"job_id": job_id, "total_uploaded": len(real_eps), "expected_total": expected_total}, timeout=30)
                except Exception: pass
                return True
            else: existing_eps = list(real_eps)
        else:
            try: http.post(f"{SERVER_URL}/api/worker/complete_job", json={"job_id": job_id, "total_uploaded": len(existing_eps), "expected_total": expected_total}, timeout=30)
            except Exception: pass
            return True

    started = False
    for start_attempt in range(1, 4):
        if start_reupstudio_download(job_id, video_link, target_folder): started = True; break
        kill_reupstudio(); time.sleep(10)
    
    if not started: 
        kill_reupstudio()
        update_local_status("LỖI GIAO DIỆN (RDP KHÓA) - TỰ SÁT (MÃ 99) ĐỂ WATCHDOG CỨU")
        time.sleep(2)
        sys.exit(99)

    completed_eps = watch_and_upload(job_id, target_folder, drive_folder_id, expected_total, existing_eps, video_link, series_id)
    total_completed = len(completed_eps)

    update_local_status("Đang nghỉ mát 20 giây trước khi chốt đơn...")
    time.sleep(20); kill_reupstudio()

    if os.path.exists(target_folder):
        leftover_files = glob.glob(os.path.join(target_folder, "*.mp4"))
        if leftover_files:
            update_local_status("Đang quét X-Ray lại các file lỗi cứng đầu...")
            for fpath in leftover_files:
                fname = os.path.basename(fpath); ep = extract_episode_number(fname)
                remux_res = remux_and_validate(fpath)
                if remux_res:
                    try:
                        drive_link = upload_file_to_drive(fpath, fname, drive_folder_id)
                        payload = {"job_id": job_id, "episode_number": ep, "drive_link": drive_link, "file_name": fname, "series_id": series_id}
                        http.post(f"{SERVER_URL}/api/worker/update_episode", json=payload, timeout=30)
                        completed_eps.add(ep); total_completed = len(completed_eps)
                        if os.path.exists(fpath): os.remove(fpath) 
                    except Exception: pass
                else:
                    try: 
                        if os.path.exists(fpath): os.remove(fpath)
                    except Exception: pass

    if total_completed == 0 and len(existing_eps) == 0: return False

    update_local_status("Đang gửi báo cáo Hoàn thành về Server...")
    try: http.post(f"{SERVER_URL}/api/worker/complete_job", json={"job_id": job_id, "total_uploaded": total_completed, "expected_total": expected_total}, timeout=30)
    except Exception: pass

    try:
        if os.path.exists(target_folder): shutil.rmtree(target_folder)
    except Exception: pass
    return True

def recover_leftover_files():
    global active_job_id
    if not os.path.exists(DOWNLOAD_BASE_DIR): return
    
    active_job_id = "recovery"  
    update_local_status("Đang quét ổ cứng tìm file bỏ dở từ phiên trước...")
    
    for series_id in os.listdir(DOWNLOAD_BASE_DIR):
        series_folder = os.path.join(DOWNLOAD_BASE_DIR, series_id)
        if os.path.isdir(series_folder):
            mp4_files = glob.glob(os.path.join(series_folder, "*.mp4"))
            if mp4_files:
                update_local_status(f"Đang tái tạo Folder Drive phục hồi Phim {series_id}...")
                try:
                    drive_folder_id = create_drive_folder(series_id, DRIVE_PARENT_FOLDER_ID)
                    for fpath in mp4_files:
                        fname = os.path.basename(fpath); ep_num = extract_episode_number(fname)
                        if ep_num == 0: continue
                        update_local_status(f"Đang Upload phục hồi Tập {ep_num}...")
                        upload_single_episode(fpath, fname, job_id="recovery", drive_folder_id=drive_folder_id, series_id=series_id)
                except Exception: pass
                
                try:
                    if len(os.listdir(series_folder)) == 0: shutil.rmtree(series_folder)
                except Exception: pass
                
    active_job_id = None
    update_local_status("Đang khởi động xong, sẵn sàng chờ việc...")

def start_worker():
    global active_job_id, expected_total_global
    
    print("=" * 60)
    print("   NHÀ MÁY HẬU CẦN — REAL-TIME TELEMETRY (Nhịp tim 10s)")
    print("=" * 60)

    threading.Thread(target=background_heartbeat, daemon=True).start()

    if not os.path.exists(CREDENTIALS_FILE):
        print(f"❌ Thiếu {CREDENTIALS_FILE} (Vui lòng để trong thư mục: {BASE_DIR})!"); input("Nhấn Enter để thoát..."); return

    print("🔑 Kết nối Google Drive...")
    try: _get_creds(); print("✅ Google Drive sẵn sàng!")
    except Exception as e: print(f"⚠️ Lỗi Drive: {e}")

    recover_leftover_files()
    headers = {"Authorization": WORKER_SECRET}
    print("⏳ Đang chờ đơn từ Server...\n")
    update_local_status("Đang nghỉ ngơi, chờ việc...")

    while True:
        try:
            res = http.get(f"{SERVER_URL}/api/worker/get_job", headers=headers, params={"worker_id": WORKER_ID}, timeout=30)
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "has_job":
                    job = data["job"]; existing_eps = job.get("existing_episodes", [])
                    print(f"\n{'=' * 60}")
                    print(f"  🚀 ĐƠN MỚI | ID: {job['job_id']} | Series: {job['series_id']}")
                    print(f"{'=' * 60}")

                    active_job_id = job['job_id']
                    expected_total_global = len(existing_eps) 

                    try: process_video_download(job['job_id'], job['series_id'], job['link'], existing_eps)
                    except Exception as e:
                        update_local_status(f"LỖI: {e}. Đang tắt ReupStudio để tránh treo...")
                        kill_reupstudio(); time.sleep(5) 
                    finally:
                        active_job_id = None; update_local_status("Đang nghỉ ngơi, chờ việc...")
                
                elif data.get("status") == "pause":
                    update_local_status("PAUSED_BY_SERVER"); time.sleep(30)
                else: 
                    update_local_status("Đang nghỉ ngơi, chờ việc..."); time.sleep(5)
            else: time.sleep(5)

        except requests.exceptions.ConnectionError: time.sleep(10)
        except KeyboardInterrupt: kill_reupstudio(); break
        except Exception: time.sleep(5)

if __name__ == "__main__":
    start_worker()
