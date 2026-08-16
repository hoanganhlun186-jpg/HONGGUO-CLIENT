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

# [CHỐNG CRASH TẬN GỐC] Đã bỏ khóa cứng CUDA_VISIBLE_DEVICES để cho phép tự động nhận diện GPU qua tiến trình con (Subprocess) an toàn.
# os.environ["CUDA_VISIBLE_DEVICES"] = ""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, 
    QTableWidget, QTableWidgetItem, QLabel, QMessageBox, 
    QHeaderView, QListWidget, QListWidgetItem, QApplication, QMainWindow, QStackedWidget,
    QFileDialog, QProgressDialog, QProgressBar, QComboBox, QSpinBox,
    QCheckBox, QTextEdit, QTabWidget, QSplitter, QAbstractItemView,
    QTreeWidget, QTreeWidgetItem, QDoubleSpinBox, QScrollArea, QFrame, QSizePolicy, QDialog
)
from PyQt6.QtCore import Qt, QThread, QObject, pyqtSignal, pyqtSlot, QSize, QSettings, QTimer, QMetaObject
from PyQt6.QtGui import QIcon, QPixmap, QImage, QFont, QColor

# Tab Render (chỉnh sửa & khắc sub/hiệu ứng lên video). Import an toàn: nếu
# thiếu file render_tab.py hoặc lỗi import thì app VẪN chạy bình thường, chỉ
# không hiện tab Render (RenderWidget = None).
try:
    from render_tab import RenderWidget
except Exception as _re_err:
    RenderWidget = None
    print(f"[WARN] Không nạp được tab Render: {_re_err}")

# Ẩn cửa sổ đen (console) khi pydub/ffmpeg chạy subprocess trên Windows.
# QUAN TRỌNG: patch này PHẢI chạy TRƯỚC khi import pydub bên dưới, vì pydub
# tự làm "from subprocess import Popen" ngay lúc import -> nếu patch sau,
# pydub vẫn giữ tham chiếu Popen GỐC (chưa ẩn cửa sổ) -> cmd đen flash lên
# mỗi khi lồng tiếng gọi ffmpeg ghép audio qua pydub.
def _patch_subprocess_no_window():
    if os.name != "nt":
        return
    try:
        import subprocess as _subp
        _CNW = 0x08000000  # CREATE_NO_WINDOW
        _orig_popen = _subp.Popen
        class _QuietPopen(_orig_popen):
            def __init__(self, *args, **kwargs):
                if "creationflags" not in kwargs:
                    kwargs["creationflags"] = _CNW
                else:
                    kwargs["creationflags"] |= _CNW
                super().__init__(*args, **kwargs)
        _subp.Popen = _QuietPopen
    except Exception:
        pass

_patch_subprocess_no_window()

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
        # An toàn 2 lớp: dù thứ tự import có bị đảo lại (vd import pydub ở module
        # khác từ trước), vẫn ép pydub.utils dùng đúng Popen đã patch ẩn cửa sổ.
        try:
            import pydub.utils as _pydub_utils
            import subprocess as _subp2
            _pydub_utils.Popen = _subp2.Popen
        except Exception:
            pass
    except Exception:
        pass
    return fp

FFMPEG_PATH = _setup_ffmpeg_pydub()

def _get_capcut_device():
    s = QSettings("BoomStudio", "ClientApp")
    did = s.value("capcut_device_id", "")
    if not did:
        did = "".join(str(uuid.uuid4().int)[:20])
        s.setValue("capcut_device_id", did)
    return {"device_id": did, "iid": did, "tdid": did}

# Đồng bộ Gemini: tái dùng login + prompt presets từ tab dịch (nếu có)
try:
    from translate_tab import GoogleManualLoginThread, PROMPT_PRESETS, AUTH_FILE, GeminiTranslateThread, DeepSeekTranslateThread
    _GEMINI_AVAILABLE = True
except Exception:
    GoogleManualLoginThread = None
    PROMPT_PRESETS = {}
    AUTH_FILE = "gemini_auth.json"
    GeminiTranslateThread = None
    DeepSeekTranslateThread = None
    _GEMINI_AVAILABLE = False

# Quản lý cài đặt Demucs tự động (lazy install)
try:
    from demucs_manager import ensure_demucs_installed_ui, get_demucs_python
    _DEMUCS_MANAGER_OK = True
except Exception:
    _DEMUCS_MANAGER_OK = False

# "Cổng" giới hạn số Demucs chạy song song DÙNG CHUNG TOÀN APP - khác Lock()
# cứng ở chỗ giới hạn (limit) có thể ĐỔI ĐƯỢC lúc đang chạy (khách chỉnh ô
# "Tách nhạc song song" 1-5), áp dụng cho MỌI nơi gọi Demucs (DubThread nhiều
# video CapCut, BgmPrecomputeThread chạy nền, BgmStandaloneThread...).
# Mặc định = 1 (an toàn nhất, giống Lock() cũ). Khách tự chọn cao hơn (kể cả
# khi dùng GPU) thì tự chịu rủi ro tràn VRAM nếu máy yếu.
import threading as _threading_global

class _DemucsConcurrencyGate:
    def __init__(self, limit=1):
        self._cond = _threading_global.Condition()
        self._active = 0
        self.limit = max(1, int(limit))

    def acquire(self):
        with self._cond:
            while self._active >= self.limit:
                self._cond.wait()
            self._active += 1

    def release(self):
        with self._cond:
            self._active -= 1
            self._cond.notify()

    def __enter__(self):
        self.acquire(); return self

    def __exit__(self, *exc):
        self.release()

_GLOBAL_DEMUCS_GATE = _DemucsConcurrencyGate(limit=1)

def _get_vocals_cache_path(video_path):
    """Đường dẫn file cache lưu vocals.wav đã tách sẵn cho 1 video, để
    BgmPrecomputeThread tách trước (chạy ngầm song song lúc dịch) và
    DubThread dùng lại khi tới lượt lồng tiếng - khỏi tách lại từ đầu."""
    base = os.path.splitext(video_path)[0]
    return base + ".vocals_cache.wav"

def _separate_vocals_demucs(video_path, use_gpu, dest_vocals_path, progress_cb=None):
    """Tách vocals (giọng gốc, không nhạc nền) cho 1 video bằng Demucs, lưu
    kết quả vào dest_vocals_path. Trả về True/False. Tự xin phép qua
    _GLOBAL_DEMUCS_GATE (giới hạn số Demucs chạy song song theo cấu hình
    khách chọn). Dùng chung cho BgmPrecomputeThread và BgmStandaloneThread."""
    import subprocess as _sp, sys as _sys, tempfile, shutil
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return False
    si = None
    if _sys.platform == "win32":
        si = _sp.STARTUPINFO()
        si.dwFlags |= _sp.STARTF_USESHOWWINDOW
    temp_dir = tempfile.mkdtemp(prefix="bgm_sep_")
    try:
        with _GLOBAL_DEMUCS_GATE:
            if progress_cb:
                progress_cb(f"🎵 Đang tách: {os.path.basename(video_path)}...")
            raw_wav = os.path.join(temp_dir, "orig_audio.wav")
            _sp.run([ffmpeg, "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", raw_wav],
                    startupinfo=si, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)

            demucs_out = os.path.join(temp_dir, "demucs_out")
            model_name = "mdx_extra"
            _demucs_py = _resolve_demucs_python()
            env = _clean_subprocess_env(_demucs_py)
            env["KMP_DUPLICATE_LIB_OK"] = "TRUE"

            device_chain = (["cuda", "cpu"] if use_gpu else ["cpu"])
            stem_name = os.path.splitext(os.path.basename(raw_wav))[0]
            vocals_path = os.path.join(demucs_out, model_name, stem_name, "vocals.wav")

            ok_sep = False
            for device in device_chain:
                env2 = dict(env)
                if device == "cuda":
                    env2.pop("CUDA_VISIBLE_DEVICES", None)
                else:
                    env2["CUDA_VISIBLE_DEVICES"] = "-1"
                    n_threads = str(max(1, int(os.cpu_count() * 0.3)))
                    for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
                        env2[k] = n_threads

                cmd = [_demucs_py, "-m", "demucs.separate", "-n", model_name,
                       "--two-stems", "vocals", "-d", device, "--out", demucs_out, raw_wav]
                res = _sp.run(cmd, env=env2, startupinfo=si, stdout=_sp.PIPE, stderr=_sp.PIPE)
                if res.returncode == 0 and os.path.exists(vocals_path):
                    ok_sep = True
                    break
                else:
                    try:
                        if os.path.isdir(demucs_out):
                            shutil.rmtree(demucs_out, ignore_errors=True)
                    except Exception:
                        pass

            if ok_sep:
                shutil.copy2(vocals_path, dest_vocals_path)
                return True
            return False
    except Exception:
        return False
    finally:
        try: shutil.rmtree(temp_dir)
        except Exception: pass

def _resolve_demucs_python():
    """Trả về đường dẫn python.exe THẬT SỰ có torch/demucs đã cài, luôn xác
    minh file tồn tại trước khi dùng - không tin mù quáng vào việc import
    demucs_manager có thành công hay không (module có thể fail-import trong
    1 số bản build Nuitka vì lý do khác, nhưng python_portable vẫn có sẵn
    trên máy). Nếu mọi cách đều không tìm thấy, fallback về sys.executable
    (Python của chính app - sẽ không có torch, chỉ dùng khi thực sự bó tay)."""
    candidates = []
    if _DEMUCS_MANAGER_OK:
        try:
            candidates.append(get_demucs_python())
        except Exception:
            pass
    # Fallback cứng: đường dẫn portable python chuẩn, phòng khi
    # demucs_manager không import được nhưng python_portable vẫn có sẵn.
    appdata = os.getenv('APPDATA', '')
    if appdata:
        candidates.append(os.path.join(appdata, 'BoomStudio', 'python_portable', 'python.exe'))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return sys.executable


def _clean_subprocess_env(python_exe_path):
    """Trả về dict env() an toàn để gọi python_portable làm subprocess từ
    bên trong app Nuitka. QUAN TRỌNG: app Nuitka tự thêm thư mục giải nén
    tạm của chính nó vào đầu PATH -> nếu copy nguyên PATH đó cho tiến trình
    con, python_portable có thể nạp NHẦM DLL của app cha (vd python3xx.dll,
    hoặc DLL CUDA/torch khác version) thay vì DLL đúng trong thư mục của
    chính nó -> import torch chết lặng lẽ, không báo lỗi rõ, GPU luôn bị
    detect sai "không có". Fix: đưa thư mục chứa python_portable lên ĐẦU
    PATH để nó luôn ưu tiên nạp đúng DLL của chính mình trước."""
    env = os.environ.copy()
    env.pop("CUDA_VISIBLE_DEVICES", None)
    py_dir = os.path.dirname(python_exe_path)
    if py_dir and os.path.isdir(py_dir):
        old_path = env.get("PATH", "")
        env["PATH"] = py_dir + os.pathsep + old_path
    return env

# ==========================================
# GIỌNG EDGE TTS (Online, miễn phí) - port nguyên từ workflow_tab.py
# Mỗi giọng: (voice_id Edge, pitch, rate offset %) - dùng cùng 1 giọng gốc
# NamMinh/HoaiMy nhưng chỉnh pitch/rate khác nhau để tạo nhiều "chất giọng".
# ==========================================
EDGE_TTS_VOICES = {
    "VN - Nam Minh (Nam - Truyền cảm)":      ("vi-VN-NamMinhNeural", "+0Hz",  "+0%"),
    "VN - Đạo Hữu (Nam - Recap/Tu Tiên)":    ("vi-VN-NamMinhNeural", "+15Hz", "+15%"),
    "VN - Hùng Dũng (Nam - Trầm/Lạnh đạm)": ("vi-VN-NamMinhNeural", "-15Hz", "+0%"),
    "VN - Bá Vương (Nam - Oai phong)":       ("vi-VN-NamMinhNeural", "-25Hz", "+0%"),
    "VN - Thiếu Niên (Nam - Trẻ/Vui)":      ("vi-VN-NamMinhNeural", "+25Hz", "+20%"),
    "VN - Hoài My (Nữ - Nhẹ nhàng)":        ("vi-VN-HoaiMyNeural", "+0Hz",  "+0%"),
    "VN - Vy Vy (Nữ - Review Phim)":         ("vi-VN-HoaiMyNeural", "+15Hz", "+30%"),
    "VN - Hạ Mây (Nữ - Tâm sự/Chữa lành)": ("vi-VN-HoaiMyNeural", "-5Hz",  "+0%"),
    "VN - Băng Nhi (Nữ - Lạnh/Kiêu)":       ("vi-VN-HoaiMyNeural", "-20Hz", "+0%"),
    "VN - Tiểu Yến (Nữ - Trẻ con/Cute)":    ("vi-VN-HoaiMyNeural", "+30Hz", "+25%"),
    "VN - Tố Nương (Nữ - Cổ trang/Đài)":    ("vi-VN-HoaiMyNeural", "+8Hz",  "+0%"),
}

def _clamp_edge_pitch(pitch_str):
    """Giới hạn pitch trong khoảng an toàn Edge TTS chấp nhận, y hệt
    validate_edge_tts_kwargs bên workflow_tab.py."""
    try:
        val = int(str(pitch_str).replace('Hz', '').replace('+', ''))
        val = max(-200, min(200, val))
        return f"{val:+d}Hz" if val != 0 else None
    except Exception:
        return None

def _clamp_edge_rate(rate_pct):
    """Giới hạn rate % trong khoảng an toàn Edge TTS chấp nhận."""
    try:
        val = int(rate_pct)
        val = max(-50, min(100, val))
        return f"{val:+d}%" if val != 0 else None
    except Exception:
        return None

# ==========================================
# CẤU HÌNH SERVER & PHIÊN BẢN
# ==========================================
APP_VERSION = "1.0.63"
SERVER_URL = "http://163.61.182.119:8000"
GITHUB_REPO = "anhstudiovn/hongguo-downloader"  # đổi thành repo thật của bạn

# ── PEKKA TTS ────────────────────────────────────────────────────────────────
# Tích hợp giọng Pekka. Khách tự nhập api_key trong app.
PEKKA_TTS_URL = "https://voice.getpekka.com/api/v1/tts/sync"
PEKKA_PREFIX = "pekka:"

def _pekka_synthesize(text, voice_code, api_key, out_path, speed_rate="1.0", log=None):
    import requests, time

    def _log(m):
        if log: log(m)

    # Thẻ bài (Headers) dùng chung cho cả lúc gửi Text và lúc tải Audio
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    body = {
        "text": text,
        "voiceId": voice_code,
        "speed": float(speed_rate)
    }

    # Vòng lặp chống lỗi: Thử tối đa 10 lần nếu bị chặn do quá tải API (Rate Limit 429)
    for attempt in range(1, 11):
        try:
            # 1. Gọi API tạo audio (Dùng endpoint /sync cho văn bản ngắn)
            r = requests.post("https://voice.getpekka.com/api/v1/tts/sync", json=body, headers=headers, timeout=30)
            
            if r.status_code == 429:
                wait_time = attempt * 2
                _log(f"⏳ Quá tải API Pekka, chờ {wait_time}s rồi thử lại...")
                time.sleep(wait_time)
                continue
                
            data = r.json()
            
            if r.status_code != 200 or "url" not in data:
                _log(f"❌ Pekka API Error: {data.get('error', r.status_code)}")
                return False

            # Lấy URL kết quả
            audio_link = data["url"]
            if audio_link.startswith("/"):
                audio_link = "https://voice.getpekka.com" + audio_link

            # 2. Thực hiện lệnh GET để tải file mp3 về (Mang theo header có API Key để không bị 403)
            dl_req = requests.get(audio_link, headers=headers, timeout=30)
            
            if dl_req.status_code == 200:
                with open(out_path, 'wb') as f:
                    f.write(dl_req.content)
                return True
            else:
                _log(f"❌ Pekka: Không thể tải file (Lỗi {dl_req.status_code})")
                return False
            
        except Exception as e:
            _log(f"⚠️ Lỗi mạng (Thử lại lần {attempt}/10): {str(e)[:40]}")
            time.sleep(2)
            
    _log("❌ Đã thử 10 lần nhưng vẫn thất bại do máy chủ Pekka.")
    return False

# ==========================================
# FFMPEG HELPER VÀ LUỒNG GỘP FILE (MERGE)
# ==========================================
def get_ffmpeg_path():
    if sys.platform == "win32":
        if os.path.exists("ffmpeg.exe"): 
            return os.path.abspath("ffmpeg.exe")
    else:
        if os.path.exists("ffmpeg"): 
            return os.path.abspath("ffmpeg")
    
    ffmpeg_system = shutil.which("ffmpeg")
    return ffmpeg_system if ffmpeg_system else None

def get_ffprobe_path():
    """Tìm ffprobe. Nếu không có, trả None (sẽ fallback dùng ffmpeg để dò)."""
    cands = []
    if sys.platform == "win32":
        cands = ["ffprobe.exe", os.path.join("bin", "ffprobe.exe")]
    else:
        cands = ["ffprobe", os.path.join("bin", "ffprobe")]
    for c in cands:
        if os.path.exists(c):
            return os.path.abspath(c)
    return shutil.which("ffprobe")

def _run_hidden(cmd):
    """Chạy subprocess, ẩn cửa sổ console trên Windows, trả (returncode, stdout, stderr)."""
    si = None
    flags = 0
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        flags = 0x08000000  # CREATE_NO_WINDOW
    try:
        res = subprocess.run(cmd, startupinfo=si, creationflags=flags,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return (res.returncode,
                res.stdout.decode("utf-8", errors="ignore"),
                res.stderr.decode("utf-8", errors="ignore"))
    except Exception as e:
        return (-1, "", str(e))

def probe_stream(file_path, ffprobe_path=None):
    info = {"vcodec": "", "acodec": "", "sample_rate": "", "channels": "", "has_audio": False}
    ffprobe = ffprobe_path or get_ffprobe_path()
    if ffprobe:
        cmd = [ffprobe, "-v", "error", "-show_entries",
               "stream=codec_type,codec_name,sample_rate,channels",
               "-of", "json", file_path]
        rc, out, err = _run_hidden(cmd)
        if rc == 0 and out.strip():
            try:
                data = json.loads(out)
                for st in data.get("streams", []):
                    ct = st.get("codec_type", "")
                    if ct == "video" and not info["vcodec"]:
                        info["vcodec"] = st.get("codec_name", "")
                    elif ct == "audio" and not info["acodec"]:
                        info["acodec"] = st.get("codec_name", "")
                        info["sample_rate"] = str(st.get("sample_rate", ""))
                        info["channels"] = str(st.get("channels", ""))
                        info["has_audio"] = True
                return info
            except Exception:
                pass
    ff = get_ffmpeg_path()
    if ff:
        rc, out, err = _run_hidden([ff, "-i", file_path])
        text = err or out
        mv = re.search(r"Video:\s*([a-zA-Z0-9_]+)", text)
        if mv:
            info["vcodec"] = mv.group(1)
        # Bắt codec + Hz + channel trong 1 lần, từng nhóm optional riêng
        ma_codec = re.search(r"Audio:\s*([a-zA-Z0-9_.]+)", text)
        ma_hz    = re.search(r"(\d+)\s*Hz", text)
        # Bắt channel sau Hz (stereo/mono/N channels) — phải search sau Hz để tránh nhầm số Hz
        ma_ch    = re.search(r"Hz\s*,\s*(mono|stereo|\d+)\s*(?:channels?)?", text, re.IGNORECASE)
        if ma_codec:
            info["acodec"] = ma_codec.group(1).split(".")[0]  # chuẩn hoá mp4a.40.2 → mp4a
            info["has_audio"] = True
            if ma_hz:
                info["sample_rate"] = ma_hz.group(1)
            if ma_ch:
                ch = ma_ch.group(1).lower()
                info["channels"] = "1" if ch == "mono" else ("2" if ch == "stereo" else ch)
    return info

def _streams_uniform(infos):
    if not infos:
        return (False, False, False, "")
    vcodecs = {i["vcodec"] for i in infos}
    acodecs = {i["acodec"] for i in infos if i["has_audio"]}
    srates  = {i["sample_rate"] for i in infos if i["has_audio"]}
    chans   = {i["channels"] for i in infos if i["has_audio"]}
    all_have_audio = all(i["has_audio"] for i in infos)
    any_have_audio = any(i["has_audio"] for i in infos)
    # uniform: chỉ cần video đồng nhất + audio đồng nhất (bỏ qua tập probe fail)
    # Nếu phần lớn có audio thì coi như uniform audio (tránh encode lại không cần)
    uniform = (len(vcodecs) == 1 and len(acodecs) <= 1 and
               len(srates) <= 1 and len(chans) <= 1 and
               (all_have_audio or not any_have_audio))
    all_aac = acodecs == {"aac"} and any_have_audio
    vcodec = next(iter(vcodecs)) if len(vcodecs) == 1 else ""
    return (uniform, all_have_audio, all_aac, vcodec)

class HonggouMergeThread(QThread):
    progress_msg = pyqtSignal(str)
    finished_signal = pyqtSignal()
    error_signal = pyqtSignal(str)

    def __init__(self, movie_folder, merge_tasks):
        super().__init__()
        self.movie_folder = movie_folder
        self.merge_tasks = merge_tasks

    def _bsf_for_vcodec(self, vcodec):
        vc = (vcodec or "").lower()
        if vc in ("hevc", "h265"):
            return "hevc_mp4toannexb"
        return "h264_mp4toannexb"

    def _verify_output(self, ffmpeg_path, ffprobe_path, final_output, any_have_audio):
        """
        Verify file output 100%:
        1. Có audio stream không (nếu input có audio)
        2. Decode toàn bộ video + audio không bị lỗi/dựt
        3. Duration hợp lý (> 1 giây)
        """
        # Check 1: audio stream
        out_info = probe_stream(final_output, ffprobe_path)
        if any_have_audio and not out_info["has_audio"]:
            return False, "File output không có tiếng"

        # Check 2: kích thước
        if os.path.getsize(final_output) < 1024:
            return False, "File output quá nhỏ"

        # Check 3: decode toàn bộ file — phát hiện dựt/lỗi frame
        si = None
        cf = 0
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            cf = 0x08000000
        verify_cmd = [
            ffmpeg_path, '-v', 'error',
            '-i', final_output,
            '-f', 'null', '-'
        ]
        res = subprocess.run(verify_cmd, startupinfo=si, creationflags=cf,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        err_output = res.stderr.decode('utf-8', errors='ignore')

        # Lọc các lỗi nghiêm trọng (bỏ qua warning nhỏ)
        serious_errors = [
            line for line in err_output.splitlines()
            if any(kw in line.lower() for kw in [
                'invalid data', 'corrupt', 'error', 'no such', 'moov atom'
            ]) and 'warning' not in line.lower()
              and 'deprecated' not in line.lower()
              and 'pts' not in line.lower()
        ]
        if serious_errors:
            return False, f"File bị lỗi: {serious_errors[0][:120]}"

        return True, "OK"

    def run(self):
        ffmpeg_path = get_ffmpeg_path()
        if not ffmpeg_path:
            self.error_signal.emit("Không tìm thấy phần mềm FFmpeg để gộp file! Vui lòng tải ffmpeg.exe đặt cùng thư mục app.")
            return
        ffprobe_path = get_ffprobe_path()

        startupinfo = None
        creationflags = 0
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = 0x08000000

        def _run(cmd):
            return subprocess.run(cmd, startupinfo=startupinfo, creationflags=creationflags,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        total_tasks = len(self.merge_tasks)
        # Đảm bảo thư mục đích tồn tại trước khi ghi merge_list / file gộp.
        # Tránh lỗi "No such file or directory: ...merge_list_0.txt" khi thư mục
        # phim chưa được tạo hoặc bị xóa giữa chừng.
        try:
            os.makedirs(self.movie_folder, exist_ok=True)
        except Exception as _mk_e:
            self.error_signal.emit(f"Không tạo được thư mục ghép '{self.movie_folder}': {_mk_e}")
            return
        for i, task in enumerate(self.merge_tasks):
            out_name = task["output_name"]
            files_to_merge = task["files"]

            if len(files_to_merge) <= 1:
                continue

            final_output = os.path.join(self.movie_folder, out_name)

            self.progress_msg.emit(f"🔎 Kiểm tra thông số phần {i+1}/{total_tasks}...")
            infos = [probe_stream(fp, ffprobe_path) for fp in files_to_merge]
            uniform, all_have_audio, all_aac, common_vcodec = _streams_uniform(infos)
            # any_have_audio: dùng cho encode path — nếu ít nhất 1 tập có audio thì giữ audio
            # (tránh trường hợp 1 tập probe fail → all_have_audio=False → ffmpeg chạy -an mất tiếng)
            any_have_audio = any(i["has_audio"] for i in infos)
            need_encode = (not uniform)

            success = False
            temp_files = []
            list_txt_path = os.path.join(self.movie_folder, f"merge_list_{i}.txt")

            try:
                if not need_encode:
                    self.progress_msg.emit(f"⚡ Ghép nhanh (giữ nguyên chất lượng) phần {i+1}/{total_tasks}...")
                    ts_files = []
                    for j, fp in enumerate(files_to_merge):
                        self.progress_msg.emit(f"⚙️ Chuẩn hoá tập {j+1}/{len(files_to_merge)} (Phần {i+1})...")
                        ts_path = fp + ".ts"
                        bsf = self._bsf_for_vcodec(infos[j].get("vcodec") or common_vcodec)
                        fi = infos[j]
                        # Luôn encode audio → AAC để đảm bảo không mất tiếng khi ghép .ts
                        if fi.get("has_audio"):
                            ts_cmd = [ffmpeg_path, '-y', '-fflags', '+genpts', '-i', fp,
                                      '-c:v', 'copy', '-bsf:v', bsf,
                                      '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2',
                                      '-f', 'mpegts', ts_path]
                        else:
                            ts_cmd = [ffmpeg_path, '-y', '-fflags', '+genpts', '-i', fp,
                                      '-c:v', 'copy', '-bsf:v', bsf,
                                      '-an', '-f', 'mpegts', ts_path]
                        res_ts = _run(ts_cmd)
                        if res_ts.returncode != 0:
                            # Fallback: không bsf
                            if fi.get("has_audio"):
                                ts_cmd2 = [ffmpeg_path, '-y', '-fflags', '+genpts', '-i', fp,
                                           '-c:v', 'copy',
                                           '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2',
                                           '-f', 'mpegts', ts_path]
                            else:
                                ts_cmd2 = [ffmpeg_path, '-y', '-fflags', '+genpts', '-i', fp,
                                           '-c:v', 'copy', '-an', '-f', 'mpegts', ts_path]
                            res_ts = _run(ts_cmd2)
                            if res_ts.returncode != 0:
                                raise Exception("remux .ts lỗi: " + res_ts.stderr.decode('utf-8', errors='ignore')[-160:])
                        ts_files.append(ts_path)
                    temp_files = list(ts_files)

                    with open(list_txt_path, 'w', encoding='utf-8') as f:
                        for tp in ts_files:
                            f.write(f"file '{tp.replace(os.sep, '/')}'\n")
                    temp_files.append(list_txt_path)

                    self.progress_msg.emit(f"⚡ Ráp mạch phim phần {i+1}/{total_tasks}: {out_name}...")
                    cmd = [ffmpeg_path, '-y', '-f', 'concat', '-safe', '0', '-i', list_txt_path,
                           '-c', 'copy',
                           '-bsf:a', 'aac_adtstoasc',  # luôn dùng vì đã encode AAC ở bước .ts
                           '-movflags', '+faststart', final_output]
                    result = _run(cmd)
                    if result.returncode == 0 and os.path.exists(final_output):
                        self.progress_msg.emit(f"🔍 Đang kiểm tra chất lượng file...")
                        ok_verify, reason = self._verify_output(ffmpeg_path, ffprobe_path, final_output, any_have_audio)
                        if not ok_verify:
                            self.progress_msg.emit(f"↩️ Ghép nhanh lỗi ({reason}), chuyển sang encode lại...")
                            try:
                                os.remove(final_output)
                            except Exception:
                                pass
                            need_encode = True
                        else:
                            success = True
                    else:
                        self.progress_msg.emit(f"↩️ Ghép nhanh lỗi, chuyển sang chế độ an toàn (encode lại)...")
                        need_encode = True

                if need_encode:
                    self.progress_msg.emit(f"🛡 Ghép an toàn (các tập lệch thông số) phần {i+1}/{total_tasks} — encode lại, hơi lâu...")
                    with open(list_txt_path, 'w', encoding='utf-8') as f:
                        for fp in files_to_merge:
                            f.write(f"file '{fp.replace(os.sep, '/')}'\n")
                    temp_files = [list_txt_path]

                    cmd = [ffmpeg_path, '-y', '-f', 'concat', '-safe', '0', '-i', list_txt_path]
                    cmd += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p']
                    if any_have_audio:
                        # Dùng any_have_audio (không phải all_have_audio) để tránh mất tiếng
                        # khi probe 1 tập fail nhưng thực tế vẫn có audio
                        cmd += ['-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2']
                    else:
                        cmd += ['-an']
                    cmd += ['-vsync', 'cfr', '-movflags', '+faststart', final_output]

                    result = _run(cmd)
                    if result.returncode == 0 and os.path.exists(final_output):
                        self.progress_msg.emit(f"🔍 Đang kiểm tra chất lượng file (encode lại)...")
                        ok_verify, reason = self._verify_output(ffmpeg_path, ffprobe_path, final_output, any_have_audio)
                        if not ok_verify:
                            raise Exception(f"Encode lại xong nhưng file lỗi: {reason}")
                        success = True
                    else:
                        err = result.stderr.decode('utf-8', errors='ignore')[-200:]
                        raise Exception(f"encode lại lỗi. Code {result.returncode}: {err}")

            except Exception as e:
                for t in temp_files:
                    try:
                        if os.path.exists(t): os.remove(t)
                    except Exception: pass
                self.error_signal.emit(f"Lỗi ghép phần {i+1} ({out_name}): {str(e)[:180]}")
                return

            for t in temp_files:
                try:
                    if os.path.exists(t): os.remove(t)
                except Exception: pass

            if success:
                for fp in files_to_merge:
                    try:
                        if os.path.exists(fp): os.remove(fp)
                    except Exception: pass
            else:
                self.error_signal.emit(f"FFmpeg thất bại ở file {out_name}.")
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

            res = requests.get(url, params=params, timeout=20)
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

    def __init__(self, jobs, covers_dir, auth_token=""):
        super().__init__()
        self.jobs = jobs  
        self.covers_dir = covers_dir
        self.auth_token = auth_token

    def run(self):
        for row, sid, url in self.jobs:
            content = None
            if sid and self.auth_token:
                try:
                    rs = requests.get(
                        f"{SERVER_URL}/api/client/cover/{sid}",
                        headers={"Authorization": f"Bearer {self.auth_token}"},
                        timeout=20
                    )
                    if rs.status_code == 200 and rs.content and len(rs.content) > 500:
                        content = rs.content
                except Exception:
                    pass
            if content:
                try:
                    with open(os.path.join(self.covers_dir, f"{sid}.img"), 'wb') as f: f.write(content)
                except Exception: pass
                self.cover_ready.emit(row, content)

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
            else: self.error_signal.emit("Máy chủ đang quá tải. Vui lòng chờ 1-2 phút rồi bấm lại nhé!")
        except Exception:
            self.error_signal.emit("Máy chủ đang quá tải. Vui lòng chờ 1-2 phút rồi bấm lại nhé!")

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
            html = None
            last_err = None
            for attempt in range(1, 4):
                try:
                    resp = requests.get(self.url, headers=headers, timeout=30)
                    resp.raise_for_status()
                    html = resp.text
                    break
                except Exception as e:
                    last_err = e
                    if attempt < 3:
                        time.sleep(attempt * 2)
            if html is None:
                self.error_signal.emit(
                    f"Web nguồn đang lỗi (đã thử 3 lần): {str(last_err)}\n"
                    f"Web hongguoduanju có thể đang bận. Hãy thử lại sau ít phút.")
                return

            detail = None
            parse_stage = "init"

            json_match = re.search(r'window\._ROUTER_DATA\s*=\s*(\{.+\})\s*;?\s*</script>', html, re.DOTALL)
            if not json_match:
                # phòng khi đổi tên biến / bỏ tiền tố window.
                json_match = re.search(r'_ROUTER_DATA\s*=\s*(\{.+\})\s*;?\s*</script>', html, re.DOTALL)

            if json_match:
                raw = json_match.group(1)
                # cắt về JSON cân bằng dấu ngoặc thay vì non-greedy (tránh dừng ở } lồng nhau đầu tiên)
                depth, end, in_str, esc = 0, None, False, False
                for i, ch in enumerate(raw):
                    if in_str:
                        if esc: esc = False
                        elif ch == '\\': esc = True
                        elif ch == '"': in_str = False
                        continue
                    if ch == '"': in_str = True
                    elif ch == '{': depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                if end:
                    raw = raw[:end]
                try:
                    data = json.loads(raw)
                    detail = (data.get("loaderData", {}) or {}).get("detail_page", {}) or {}
                    detail = detail.get("seriesDetail", {}) or {}
                    parse_stage = "ok" if detail else "path_miss"
                except json.JSONDecodeError as e:
                    parse_stage = f"json_err: {e}"
            else:
                low = html.lower()
                if any(k in low for k in ("captcha", "verify", "验证", "滑块")):
                    parse_stage = "blocked_captcha"
                elif any(k in low for k in ("login", "sign in", "登录")):
                    parse_stage = "need_login"
                elif len(html) < 2000:
                    parse_stage = f"html_too_short(len={len(html)})"
                else:
                    parse_stage = "no_router_data"

            if not detail:
                snippet = (html[:400].replace("\r", " ").replace("\n", " ") if html else "")
                self.error_signal.emit(
                    f"Không bóc tách được dữ liệu [{parse_stage}]. Vui lòng kiểm tra lại link.\n"
                    f"---\nHTML mở đầu: {snippet}")
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
            
            # Auto-retry khi server quá tải (503/502/504) - tối đa 3 lần
            _RETRY_CODES = {502, 503, 504}
            res = None
            for _attempt in range(1, 4):
                res = requests.post(f"{SERVER_URL}/api/client/add_job", json=payload,
                                    headers={"Authorization": f"Bearer {self.auth_token}"}, timeout=15)
                if res.status_code not in _RETRY_CODES:
                    break
                if _attempt < 3:
                    time.sleep(_attempt * 3)  # 3s, 6s

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
                if res.status_code in _RETRY_CODES:
                    self.error_signal.emit("Máy chủ đang quá tải. Vui lòng chờ 1-2 phút rồi bấm lại nhé!")
                else:
                    self.error_signal.emit(f"Máy chủ từ chối yêu cầu (Mã {res.status_code}). Vui lòng thử lại sau ít phút.")

        except requests.exceptions.RequestException as e: 
            self.error_signal.emit("Máy chủ đang quá tải. Vui lòng chờ 1-2 phút rồi bấm lại nhé!")
        except Exception:
            self.error_signal.emit("Máy chủ đang quá tải. Vui lòng chờ 1-2 phút rồi bấm lại nhé!")

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
            self.error_signal.emit("Máy chủ đang quá tải. Vui lòng chờ 1-2 phút rồi bấm lại nhé!")

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

class BulkQuoteThread(QThread):
    result_signal = pyqtSignal(dict)
    error_signal = pyqtSignal(str)
    def __init__(self, mode, username, auth_token, num_series=0, exclude_ids=None, series_ids=None):
        super().__init__()
        self.mode = mode  
        self.username = username
        self.auth_token = auth_token
        self.num_series = num_series
        self.exclude_ids = exclude_ids or []
        self.series_ids = series_ids or []
    def run(self):
        try:
            hdr = {"Authorization": f"Bearer {self.auth_token}"}
            if self.mode == 'random':
                res = requests.post(f"{SERVER_URL}/api/client/bulk/random_quote",
                    json={"username": self.username, "num_series": self.num_series, "exclude_series_ids": self.exclude_ids},
                    headers=hdr, timeout=30)
            else:
                res = requests.post(f"{SERVER_URL}/api/client/bulk/pick_quote",
                    json={"username": self.username, "series_ids": self.series_ids},
                    headers=hdr, timeout=30)
            data = res.json()
            if res.status_code == 200 and data.get("status") == "success":
                self.result_signal.emit(data)
            else:
                self.error_signal.emit(data.get("message") or f"Lỗi {res.status_code}")
        except Exception as e:
            self.error_signal.emit(str(e))

class BulkConfirmThread(QThread):
    result_signal = pyqtSignal(dict)
    error_signal = pyqtSignal(str)
    def __init__(self, username, token, auth_token):
        super().__init__()
        self.username = username; self.token = token; self.auth_token = auth_token
    def run(self):
        try:
            res = requests.post(f"{SERVER_URL}/api/client/bulk/confirm",
                json={"username": self.username, "token": self.token},
                headers={"Authorization": f"Bearer {self.auth_token}"}, timeout=90)
            data = res.json()
            if res.status_code == 200 and data.get("status") == "success":
                self.result_signal.emit(data)
            else:
                self.error_signal.emit(data.get("message") or f"Lỗi {res.status_code}")
        except Exception as e:
            self.error_signal.emit(str(e))

# ==========================================
# THREAD TẢI VÀ GIẢI MÃ (DECRYPTION)
# ==========================================
class SingleDriveDownloadThread(QThread):
    progress_signal = pyqtSignal(int, int, float)
    done_signal = pyqtSignal(int, str)
    error_signal = pyqtSignal(int, str)
    dead_link_signal = pyqtSignal(int)

    def __init__(self, ep_data, save_folder, auth_token, session, concurrency=1):
        super().__init__()
        self.ep_data = ep_data
        self.save_folder = save_folder
        self.auth_token = auth_token
        self.session = session
        self.concurrency = max(1, concurrency)

    NUM_PARTS = 4

    def _dl_headers(self, needs_decrypt=False):
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Connection": "keep-alive",
        }

    def _download_multipart(self, url, dest_path, needs_decrypt):
        from concurrent.futures import ThreadPoolExecutor

        headers = self._dl_headers(needs_decrypt)
        ep_num = self.ep_data.get("episode_number")

        try:
            with requests.get(url, headers=headers, stream=True, timeout=(15, 30), allow_redirects=True) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0))
                accept_ranges = r.headers.get("Accept-Ranges", "").lower()
        except Exception:
            return None

        if total <= 0 or "bytes" not in accept_ranges:
            return None

        MAX_TOTAL_CONN = 16
        parts = max(1, min(self.NUM_PARTS, MAX_TOTAL_CONN // self.concurrency))
        if total < 2 * 1024 * 1024:
            parts = 1

        part_size = total // parts
        ranges = []
        for i in range(parts):
            start = i * part_size
            end = (start + part_size - 1) if i < parts - 1 else (total - 1)
            ranges.append((start, end))

        tmp_files = [dest_path + f".p{i}" for i in range(parts)]
        downloaded_per = [0] * parts
        import threading as _th
        lock = _th.Lock()
        start_time = time.time()
        last_emit = [0.0]

        def _emit_progress():
            done = sum(downloaded_per)
            now = time.time()
            if now - last_emit[0] >= 0.25:
                last_emit[0] = now
                pct = int(done * 100 / total)
                if pct >= 100:
                    pct = 98
                elapsed = now - start_time
                speed = (done / 1024 / 1024) / elapsed if elapsed > 0 else 0
                self.progress_signal.emit(ep_num, pct, speed)

        def _worker(idx, start, end):
            expected = end - start + 1
            last_reason = "?"
            for tries in range(3):
                got = 0
                try:
                    h = dict(headers)
                    h["Range"] = f"bytes={start}-{end}"
                    with requests.get(url, headers=h, stream=True, timeout=(15, 120)) as r:
                        if r.status_code != 206:
                            last_reason = f"status {r.status_code} (cần 206)"
                            raise Exception(last_reason)
                        with open(tmp_files[idx], "wb") as f:
                            for chunk in r.iter_content(chunk_size=1 << 20):
                                if chunk:
                                    f.write(chunk)
                                    got += len(chunk)
                                    with lock:
                                        downloaded_per[idx] += len(chunk)
                                        _emit_progress()
                    if got != expected:
                        last_reason = f"nhận {got}/{expected} byte"
                        raise Exception(last_reason)
                    return None
                except Exception as ex:
                    last_reason = str(ex)
                    with lock:
                        downloaded_per[idx] -= got
                        if downloaded_per[idx] < 0:
                            downloaded_per[idx] = 0
                    try:
                        if os.path.exists(tmp_files[idx]): os.remove(tmp_files[idx])
                    except: pass
                    if tries < 2:
                        time.sleep(1.5)
            return f"mảnh {idx}: {last_reason}"

        reasons = []
        with ThreadPoolExecutor(max_workers=parts) as ex:
            futs = [ex.submit(_worker, i, s, e) for i, (s, e) in enumerate(ranges)]
            for fu in futs:
                res = fu.result()
                if res:
                    reasons.append(res)

        if reasons:
            for tf in tmp_files:
                try:
                    if os.path.exists(tf): os.remove(tf)
                except: pass
            raise Exception("Tải mảnh lỗi -> " + " | ".join(reasons))

        with open(dest_path, "wb") as out:
            for tf in tmp_files:
                with open(tf, "rb") as pf:
                    while True:
                        buf = pf.read(1 << 20)
                        if not buf:
                            break
                        out.write(buf)
                try: os.remove(tf)
                except: pass

        if os.path.getsize(dest_path) != total:
            try: os.remove(dest_path)
            except: pass
            raise Exception("Ghép mảnh sai dung lượng")

        if not self._looks_like_mp4(dest_path):
            try: os.remove(dest_path)
            except: pass
            raise Exception("File tải về không hợp lệ (thiếu moov)")
        return True

    @staticmethod
    def _looks_like_mp4(path):
        try:
            with open(path, "rb") as f:
                head = f.read(64 * 1024)
                if b"ftyp" not in head:
                    return False
                f.seek(max(0, os.path.getsize(path) - 2 * 1024 * 1024))
                tail = f.read()
            return (b"moov" in head) or (b"moov" in tail)
        except Exception:
            return False

    def _download_single(self, url, dest_path, needs_decrypt):
        headers = self._dl_headers(needs_decrypt)
        ep_num = self.ep_data.get("episode_number")
        with requests.get(url, headers=headers, stream=True, timeout=(15, 120)) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0
            start_time = time.time()
            last_emit = 0.0
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            now = time.time()
                            if now - last_emit >= 0.25:
                                last_emit = now
                                pct = int(downloaded * 100 / total)
                                if pct >= 100: pct = 98
                                elapsed = now - start_time
                                speed = (downloaded / 1024 / 1024) / elapsed if elapsed > 0 else 0
                                self.progress_signal.emit(ep_num, pct, speed)
        return True

    def run(self):
        ep_num = self.ep_data.get("episode_number")
        try:
            ep_int = int(ep_num)
            file_name = self.ep_data.get("file_name", f"Tap_{ep_int:02d}.mp4")
        except:
            file_name = self.ep_data.get("file_name", f"Tap_{ep_num}.mp4")
            
        url = self.ep_data.get("drive_link")
        aes_key = self.ep_data.get("aes_key", "")
        needs_decrypt = bool(aes_key)
        
        if not url or "error" in url.lower() or "status" in url.lower():
            self.dead_link_signal.emit(ep_num)
            return
            
        file_path = os.path.join(self.save_folder, file_name)
        
        if needs_decrypt:
            enc_path = file_path.replace(".mp4", ".enc.mp4")
            part_path = enc_path + ".part"
        else:
            enc_path = None
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

                ok = None
                try:
                    ok = self._download_multipart(url, part_path, needs_decrypt)
                except Exception as multi_err:
                    ok = None

                if ok is None:
                    self._download_single(url, part_path, needs_decrypt)

                if os.path.exists(part_path):
                    if needs_decrypt:
                        os.rename(part_path, enc_path)
                        self.progress_signal.emit(ep_num, 99, 0.0)  
                        
                        try:
                            from cenc_decrypt import decrypt_file
                            decrypt_file(enc_path, aes_key, file_path)
                            if os.path.exists(enc_path):
                                os.remove(enc_path)
                            self.done_signal.emit(ep_num, file_path)
                            return
                        except Exception as dec_e:
                            raise Exception(f"Lỗi bẻ khóa video: {str(dec_e)}")
                    else:
                        os.rename(part_path, file_path)
                        self.done_signal.emit(ep_num, file_path)
                        return
                else:
                    raise Exception("Không ghi được file hệ thống")
                    
            except Exception as e:
                if os.path.exists(part_path):
                    try: os.remove(part_path)
                    except: pass
                if enc_path and os.path.exists(enc_path):
                    try: os.remove(enc_path)
                    except: pass
                
                if attempt == max_retries - 1:
                    try:
                        print(f"[TẢI LỖI] Tập {ep_num}: {e}")
                    except: pass
                    if "Lỗi bẻ khóa video" in str(e):
                        self.error_signal.emit(ep_num, str(e))
                        return
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
        self._paused = False       # cờ tạm dừng: True = không tung worker mới
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

    def pause(self):
        """Tạm dừng: ngừng tung tập mới. Các tập đang tải vẫn chạy cho xong."""
        self._paused = True

    def resume(self):
        """Tiếp tục: tung lại các tập còn trong hàng đợi."""
        if not self._paused:
            return
        self._paused = False
        running = len([w for w in self._workers if w.isRunning()])
        for _ in range(max(0, self.max_concurrent - running)):
            if self._pending:
                self._launch_next()

    def is_paused(self):
        return self._paused

    def add_and_run_episode(self, ep_data):
        self._pending.append(ep_data)
        if self._paused:
            return  # đang tạm dừng thì chỉ xếp hàng, không tải
        if len([w for w in self._workers if w.isRunning()]) < self.max_concurrent:
            self._launch_next()

    def _launch_next(self):
        if self._paused: return
        if not self._pending: return
        ep_data = self._pending.pop(0)
        worker = SingleDriveDownloadThread(ep_data, self.save_folder, self.auth_token, self.session, concurrency=self.max_concurrent)
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
# STT BATCH THREAD — Tách sub từ file video
# ==========================================
class SttBatchThread(QThread):
    progress_signal = pyqtSignal(str)       
    pct_signal = pyqtSignal(int)            # % tổng cả mẻ tách sub (done/total)
    finished_signal = pyqtSignal(int, int)  

    def __init__(self, file_paths, src_lang="zh-CN", out_lang="vi-VN", use_trans=True, stt_workers=3):
        super().__init__()
        self.file_paths = file_paths
        self.src_lang   = src_lang
        self.out_lang   = out_lang
        self.use_trans  = False
        self.stt_workers = max(1, int(stt_workers))
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
            # Không truyền device cố định nữa - dùng CapCutClient() trần y
            # hệt tool CapCut TTS/STT gốc, tránh 1 device_id bị dùng lặp đi
            # lặp lại quá nhiều lần có thể khiến server giảm ưu tiên xử lý.
            client = CapCutClient()
        except Exception as e:
            self.progress_signal.emit(f"❌ Không khởi tạo được CapCutClient: {e}")
            self.finished_signal.emit(0, len(self.file_paths))
            return

        SUCCEED = {"succeed", "success", "completed", "done"}
        FAIL    = {"failed",  "error",   "fail"}

        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _process_one(i, fp):
            if not os.path.exists(fp):
                self.progress_signal.emit(f"[{i+1}] ⏭ Bỏ qua (chưa có): {os.path.basename(fp)}")
                return False

            bname = os.path.basename(fp)
            out_srt = os.path.splitext(fp)[0] + ".srt"
            out_txt = os.path.splitext(fp)[0] + ".txt"

            last_err = ""
            MAX_RETRY = 5
            MAX_POLL = 70  
            for attempt in range(1, MAX_RETRY + 1):  
                if self._stop:
                    return False
                try:
                    self.progress_signal.emit(f"[{i+1}/{len(self.file_paths)}] ⬆️ Upload: {bname} ..." +
                                              (f" (lần {attempt})" if attempt > 1 else ""))
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
                    for _poll in range(MAX_POLL):
                        if self._stop: break
                        time.sleep(2)
                        q = client.query_stt_task(task_id, token)
                        qt = (q.get("data") or {}).get("tasks") or []
                        if not qt: continue
                        status = qt[0].get("status", "")
                        pct = qt[0].get("progress", "")
                        if pct: self.progress_signal.emit(f"[{i+1}] ⏳ {status} | {pct}%")
                        if status in SUCCEED:
                            result = qt[0]; break
                        elif status in FAIL:
                            raise RuntimeError(f"STT fail: {status}")
                    if result is None:
                        raise RuntimeError(f"Timeout nhận dạng STT")

                    subs = client.extract_subtitles({"data": {"tasks": [result]}})
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
                    return True  
                except Exception as e:
                    last_err = str(e)[:100]
                    if attempt < MAX_RETRY:
                        self.progress_signal.emit(f"[{i+1}] 🔄 Lỗi tách sub, thử lại lần {attempt+1}/{MAX_RETRY}: {last_err}")
                        time.sleep(attempt * 3)
            if not self._stop:
                self.progress_signal.emit(f"[{i+1}] ❌ Tách sub lỗi sau {MAX_RETRY} lần: {last_err}")
            return False

        ok = failed = 0
        self.progress_signal.emit(f"🚀 Tách sub song song {self.stt_workers} luồng ({len(self.file_paths)} tập)...")
        self.pct_signal.emit(0)
        _total = max(1, len(self.file_paths))
        with ThreadPoolExecutor(max_workers=self.stt_workers) as ex:
            futs = {ex.submit(_process_one, i, fp): i for i, fp in enumerate(self.file_paths)}
            for fut in as_completed(futs):
                if self._stop:
                    self.progress_signal.emit("🛑 Đã dừng bởi người dùng.")
                try:
                    if fut.result(): ok += 1
                    else: failed += 1
                except Exception as e:
                    failed += 1
                    self.progress_signal.emit(f"⚠️ Lỗi luồng: {str(e)[:80]}")
                self.pct_signal.emit(int((ok + failed) / _total * 100))

        self.finished_signal.emit(ok, failed)

# ==========================================
# BGM PRECOMPUTE THREAD — Tách nhạc nền NGẦM, chạy SONG SONG với bước dịch,
# lưu kết quả vào cache để DubThread dùng lại sau (khỏi tách lại từ đầu lúc
# lồng tiếng, tiết kiệm phần lớn thời gian chờ ở bước lồng tiếng).
# ==========================================
class BgmPrecomputeThread(QThread):
    progress_signal = pyqtSignal(str)
    finished_signal = pyqtSignal()

    def __init__(self, video_paths, use_gpu=False):
        super().__init__()
        self.video_paths = list(video_paths)
        self.use_gpu = use_gpu
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        todo = [v for v in self.video_paths
                if os.path.exists(v) and not os.path.exists(_get_vocals_cache_path(v))]

        def _one(video_path):
            if self._stop:
                return
            cache_path = _get_vocals_cache_path(video_path)
            bname = os.path.basename(video_path)
            try:
                ok = _separate_vocals_demucs(
                    video_path, self.use_gpu, cache_path,
                    progress_cb=lambda m: self.progress_signal.emit(f"[Tách nền ngầm] {m}")
                )
            except Exception:
                ok = False
            if ok:
                self.progress_signal.emit(f"⚡ [Tách nền ngầm] Xong: {bname}")
            else:
                self.progress_signal.emit(f"⚠️ [Tách nền ngầm] Lỗi, bỏ qua: {bname} (sẽ tự tách lại lúc lồng tiếng)")

        # Nộp TẤT CẢ video cùng lúc vào pool 5 luồng - số Demucs THẬT SỰ chạy
        # song song do _GLOBAL_DEMUCS_GATE quyết định (khách chỉnh ô "Tách
        # song song" 1-5), ở đây chỉ là số luồng SẴN SÀNG chờ tới lượt.
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = [ex.submit(_one, v) for v in todo]
            for _ in as_completed(futs):
                pass

        self.finished_signal.emit()

# ==========================================
# BGM STANDALONE THREAD — Xuất video RIÊNG chỉ có giọng gốc, KHÔNG nhạc nền.
# Dùng khi khách tick "Tách nhạc nền" nhưng KHÔNG tick "Lồng tiếng" - tức chỉ
# muốn bản không nhạc nền, không cần lồng tiếng gì cả. Chạy nhiều video song
# song theo số khách chọn ở ô "Tách song song" (qua _GLOBAL_DEMUCS_GATE).
# ==========================================
class BgmStandaloneThread(QThread):
    progress_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(int, int)

    def __init__(self, video_paths, use_gpu=False, del_original=False):
        super().__init__()
        self.video_paths = list(video_paths)
        self.use_gpu = use_gpu
        self.del_original = del_original
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import subprocess as _sp, sys as _sys

        ffmpeg = get_ffmpeg_path()
        if not ffmpeg:
            self.progress_signal.emit("❌ Không tìm thấy ffmpeg!")
            self.finished_signal.emit(0, len(self.video_paths)); return

        si = None
        if _sys.platform == "win32":
            si = _sp.STARTUPINFO()
            si.dwFlags |= _sp.STARTF_USESHOWWINDOW

        ok_lock = _threading_global.Lock()
        ok = failed = 0

        def _tally(result):
            nonlocal ok, failed
            if result is None:
                return
            with ok_lock:
                if result: ok += 1
                else: failed += 1

        def _one(idx, video_path):
            if self._stop:
                return None
            if not os.path.exists(video_path):
                self.progress_signal.emit(f"[{idx+1}] ⏭ Bỏ qua: thiếu file")
                return False

            bname = os.path.basename(video_path)
            temp_vocals = video_path + ".tmp_vocals.wav"
            try:
                ok_sep = _separate_vocals_demucs(
                    video_path, self.use_gpu, temp_vocals,
                    progress_cb=lambda m: self.progress_signal.emit(f"[{idx+1}] {m}")
                )
                if not ok_sep:
                    self.progress_signal.emit(f"[{idx+1}] ❌ Lỗi tách nhạc nền: {bname}")
                    return False

                out_video = os.path.splitext(video_path)[0] + "_vocals.mp4"
                res = _sp.run(
                    [ffmpeg, "-y", "-i", video_path, "-i", temp_vocals,
                     "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                     "-c:a", "aac", "-b:a", "192k", "-shortest", out_video],
                    startupinfo=si, stdout=_sp.DEVNULL, stderr=_sp.PIPE
                )
                if res.returncode != 0 or not os.path.exists(out_video):
                    self.progress_signal.emit(f"[{idx+1}] ❌ Lỗi ghép video: {bname}")
                    return False

                if self.del_original:
                    try:
                        os.remove(video_path)
                        os.rename(out_video, video_path)
                        self.progress_signal.emit(f"[{idx+1}] ✅ Xong! Đã ghi đè: {bname} (không nhạc nền)")
                    except Exception as e:
                        self.progress_signal.emit(f"[{idx+1}] ⚠️ Xong nhưng lỗi ghi đè: {str(e)[:100]}")
                else:
                    self.progress_signal.emit(f"[{idx+1}] ✅ Xong! → {os.path.basename(out_video)}")
                return True
            except Exception as e:
                self.progress_signal.emit(f"[{idx+1}] ❌ Lỗi {bname}: {str(e)[:120]}")
                return False
            finally:
                try:
                    if os.path.exists(temp_vocals): os.remove(temp_vocals)
                except Exception:
                    pass

        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {ex.submit(_one, idx, v): idx for idx, v in enumerate(self.video_paths)}
            for fut in as_completed(futs):
                _tally(fut.result())

        self.finished_signal.emit(ok, failed)

# ==========================================
# DUB THREAD — Lồng tiếng từ SRT vào video
# ==========================================
class DubThread(QThread):
    progress_signal = pyqtSignal(str)
    pct_signal = pyqtSignal(str, int)        # (video_path, % thật 0..100) của 1 tập
    finished_signal = pyqtSignal(int, int)   

    def __init__(self, tasks, voice_type="BV074_streaming", rate="1.0", pitch="+0Hz", mute_original=True, orig_volume=15, remove_bgm=False, use_gpu=False, tts_workers=4, pekka_api_key=""):
        super().__init__()
        self.tasks      = tasks
        self.voice_type = voice_type
        self.rate       = rate
        self.pitch      = pitch
        self.pekka_api_key = pekka_api_key   # Đổi thành Pekka
        self.tts_workers = max(1, int(tts_workers))
        self.mute_original = mute_original
        self.remove_bgm = remove_bgm
        self.use_gpu = use_gpu
        try:
            self.orig_volume = max(0, min(100, int(orig_volume)))
        except Exception:
            self.orig_volume = 15
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
        import urllib.request, shutil, json, threading
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from pydub import AudioSegment
        from pydub.effects import speedup

        is_neural = "Neural" in self.voice_type
        is_pekka = self.voice_type.startswith(PEKKA_PREFIX)   # Sửa is_vbee thành is_pekka

        # QUAN TRỌNG: chỉ khởi tạo CapCutClient khi THẬT SỰ cần dùng giọng
        # CapCut. Nếu khách chọn giọng 🌐 Edge TTS thì không cần CapCut chút
        # nào - Edge TTS phải chạy độc lập, không phụ thuộc CapCut có đăng
        # nhập/còn hạn hay không.
        client = None
        if not is_neural and not is_pekka:
            try:
                from capcut_tts_api import CapCutClient
                # Không truyền device cố định nữa - dùng CapCutClient() trần
                # y hệt tool CapCut TTS/STT gốc, tránh 1 device_id bị dùng
                # lặp đi lặp lại quá nhiều lần có thể khiến server giảm ưu
                # tiên xử lý (nghi vấn chính gây lồng tiếng chậm hơn tool cũ).
                client = CapCutClient()
            except Exception as e:
                self.progress_signal.emit(f"❌ Không khởi tạo được CapCutClient: {e}")
                self.finished_signal.emit(0, len(self.tasks)); return

        ffmpeg = get_ffmpeg_path()
        if not ffmpeg:
            self.progress_signal.emit("❌ Không tìm thấy ffmpeg! Đặt ffmpeg.exe cùng thư mục.")
            self.finished_signal.emit(0, len(self.tasks)); return

        # [SỬA LỖI] Ép thư viện pydub phải sử dụng đúng ffmpeg.exe đang có
        AudioSegment.converter = ffmpeg

        ok = failed = 0
        ok_lock = threading.Lock()
        SUCCEED = {"succeed","success","completed","done","finish"}
        FAIL    = {"failed","error","fail"}

        def _process_one_task(idx, task):
            """Xử lý lồng tiếng cho ĐÚNG 1 video. Trả về True/False (thành
            công/thất bại). Tách thành hàm riêng để có thể gọi tuần tự (Edge
            TTS) hoặc song song nhiều video cùng lúc (CapCut, xem dưới)."""
            if self._stop:
                return None  # bị hủy giữa chừng, không tính thành công/thất bại

            video_path = task["video"]
            srt_path   = task["srt"]
            if not os.path.exists(video_path) or not os.path.exists(srt_path):
                self.progress_signal.emit(f"[{idx+1}] ⏭ Bỏ qua: thiếu file video hoặc SRT")
                return False

            bname = os.path.basename(video_path)
            self.progress_signal.emit(f"[{idx+1}/{len(self.tasks)}] 🎙 Bắt đầu lồng tiếng: {bname}")
            self.pct_signal.emit(video_path, 0)

            entries = self._parse_srt(srt_path)
            if not entries:
                self.progress_signal.emit(f"[{idx+1}] ⚠️ SRT rỗng: {os.path.basename(srt_path)}")
                return False

            temp_dir = tempfile.mkdtemp(prefix="dub_")
            _vocals_from_cache = False  # khai báo sớm nhất có thể, tránh NameError
            # trong finally nếu lỗi xảy ra trước khi tới đoạn tách nhạc nền bên dưới.
            try:
                total_ms = entries[-1][1] + 500

                combined = AudioSegment.silent(duration=total_ms)
                from concurrent.futures import ThreadPoolExecutor, as_completed

                def _make_one_segment(i, start_ms, end_ms, text):
                    if not text.strip():
                        return (i, start_ms, None)
                    target_dur = end_ms - start_ms
                    seg_path = os.path.join(temp_dir, f"seg_{i:04d}.mp3")
                    last_err = ""
                    for attempt in range(1, 4):  
                        if self._stop:
                            return (i, start_ms, None)
                        try:
                            if is_neural:
                                import asyncio, edge_tts
                                r = float(self.rate)
                                pct = int((r-1.0)*100)
                                _rate_clamped = _clamp_edge_rate(pct)
                                _pitch_clamped = _clamp_edge_pitch(self.pitch)
                                _edge_kwargs = {}
                                if _rate_clamped: _edge_kwargs['rate'] = _rate_clamped
                                if _pitch_clamped: _edge_kwargs['pitch'] = _pitch_clamped
                                async def _run_edge():
                                    comm = edge_tts.Communicate(text=text, voice=self.voice_type, **_edge_kwargs)
                                    await comm.save(seg_path)
                                asyncio.run(_run_edge())
                            elif is_pekka:
                                # Giọng Pekka: voice_type dạng "pekka:<voiceId>"
                                voice_code = self.voice_type[len(PEKKA_PREFIX):]
                                ok_pekka = _pekka_synthesize(
                                    text, voice_code,
                                    self.pekka_api_key,
                                    seg_path,
                                    speed_rate=self.rate,
                                    log=lambda m: self.progress_signal.emit(f"[{idx+1}] Dòng {i+1}: {m}"))
                                if not ok_pekka:
                                    raise RuntimeError("Pekka tổng hợp thất bại")
                            else:
                                # Port Y HỆT logic từ capcut_widget.py (_tts_get_url) đã
                                # xác nhận chạy nhanh - đảm bảo 100% giống hệt, không còn
                                # nghi ngờ có khác biệt tinh vi nào. Thêm log chi tiết từng
                                # lần poll để so sánh trực tiếp thời gian với tool gốc.
                                self.progress_signal.emit(f"[{idx+1}] Dòng {i+1}: Gửi request...")
                                create = client.create_tts_task(texts=text, voice=self.voice_type, rate=self.rate)
                                tasks_r = (create.get("data") or {}).get("tasks") or []
                                if not tasks_r: raise RuntimeError("Không có TTS task")
                                tid, tok = tasks_r[0]["id"], tasks_r[0]["token"]
                                self.progress_signal.emit(f"[{idx+1}] Dòng {i+1}: task_id={tid}")

                                url = None
                                st = ""
                                for _poll_attempt in range(60):
                                    time.sleep(2)  # y hệt tool cũ (2s/lần) để so sánh công bằng
                                    q = client.query_tts_task(tid, tok)
                                    qt = (q.get("data") or {}).get("tasks") or []
                                    if not qt: continue
                                    st = qt[0].get("status", "")
                                    prog = qt[0].get("progress", 0)
                                    self.progress_signal.emit(f"[{idx+1}] Dòng {i+1}: Poll {_poll_attempt+1} | status={st!r} | progress={prog}%")
                                    if st in SUCCEED:
                                        raw = qt[0].get("payload", "{}")
                                        pl = json.loads(raw) if isinstance(raw, str) else raw
                                        subs2 = pl.get("audio_subtitles") or []
                                        if subs2:
                                            url = subs2[0].get("speech_url", "")
                                            if url: break
                                        for k in ("audio_list", "url_list"):
                                            for u in (pl.get(k) or []):
                                                url = (u.get("url") or u.get("audio_url") or u.get("speech_url")) if isinstance(u, dict) else str(u)
                                                if url: break
                                            if url: break
                                        if not url:
                                            raise RuntimeError("Task succeed nhưng không tìm thấy URL")
                                        break
                                    elif st in FAIL:
                                        raise RuntimeError(f"TTS fail: {st}")
                                if not url:
                                    raise RuntimeError(f"Timeout 120s. Status cuối: {st!r}")
                                urllib.request.urlretrieve(url, seg_path)

                            try:
                                audio_seg = AudioSegment.from_file(seg_path, format="mp3")
                            except Exception:
                                wav_path = seg_path + ".wav"
                                _flags = 0x08000000 if os.name == "nt" else 0
                                subprocess.run([ffmpeg, "-y", "-i", seg_path, wav_path],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_flags)
                                audio_seg = AudioSegment.from_file(wav_path, format="wav")
                            cur = len(audio_seg)
                            # CHỐNG ÉP CHỮ: câu đọc dài hơn ô SRT thì tua nhanh
                            # lại cho khớp, NHƯNG chỉ tối đa 1.3× (trước là 1.5×).
                            # Khách đã có thể tự tăng tốc độ đọc (rate) bên TTS,
                            # nên tua thêm quá nhiều ở đây làm giọng bị dí/méo.
                            # Câu nào vượt 1.3× thì chỉ tua tới 1.3× rồi để tràn
                            # nhẹ sang khoảng lặng dòng sau (nghe tự nhiên hơn ép kịch).
                            MAX_SPEEDUP = 1.3
                            if cur > target_dur and target_dur > 0:
                                factor = min(cur/target_dur, MAX_SPEEDUP)
                                if factor > 1.01:
                                    try: audio_seg = speedup(audio_seg, playback_speed=factor)
                                    except Exception: pass
                            elif cur < target_dur:
                                audio_seg += AudioSegment.silent(duration=target_dur-cur)
                            return (i, start_ms, audio_seg)  
                        except Exception as seg_e:
                            last_err = str(seg_e)[:60]
                            if attempt < 3:
                                self.progress_signal.emit(f"[{idx+1}] 🔄 Dòng {i+1} lỗi, thử lại lần {attempt+1}/3...")
                                time.sleep(attempt * 2)  
                    self.progress_signal.emit(f"[{idx+1}] ❌ Dòng {i+1} lỗi sau 3 lần: {last_err}")
                    return (i, start_ms, None)
                
                # Edge TTS: dùng đúng số luồng khách chỉnh ở ô "Luồng" (miễn phí,
                # ít giới hạn). CapCut: LUÔN khóa cứng 4 luồng bất kể khách chỉnh
                # gì trong ô "Luồng" - CapCut có giới hạn API riêng, tăng luồng
                # cao dễ bị lỗi/limit tài khoản.
                # Cả Edge TTS lẫn CapCut đều dùng chung số luồng khách chỉnh ở
                # ô "Luồng". Trước đây CapCut bị khóa cứng 4 luồng để an toàn,
                # nhưng thực tế tool CapCut TTS/STT gốc chạy nhiều luồng hơn
                # vẫn ổn định và nhanh hơn hẳn - bỏ khóa cứng, để khách tự
                # điều chỉnh theo tài khoản/giới hạn API thực tế của họ.
                luong_tts = self.tts_workers
                done_cnt = 0
                segments_to_mix = [] 
                
                with ThreadPoolExecutor(max_workers=luong_tts) as ex:
                    futs = [ex.submit(_make_one_segment, i, s, e, t)
                            for i, (s, e, t) in enumerate(entries)]
                    for fut in as_completed(futs):
                        if self._stop: break
                        i, start_ms, seg = fut.result()
                        done_cnt += 1
                        if seg is not None:
                            segments_to_mix.append((start_ms, seg))
                        self.progress_signal.emit(f"[{idx+1}] 🔊 {done_cnt}/{len(entries)} dòng")
                        if entries:
                            self.pct_signal.emit(video_path, int(done_cnt / len(entries) * 70))

                if self._stop:
                    return None
                # Mix theo ĐÚNG mốc thời gian phụ đề: overlay từng câu vào
                # position=start_ms trên nền im lặng dài bằng cả video. Cách này
                # giữ tiếng khớp sub tuyệt đối, kể cả khi 1 câu đọc dài tràn qua
                # khung của nó (câu sau vẫn vào đúng mốc của nó, không bị đẩy).
                #
                # KHÔNG dùng cách nối tiếp (combined += seg) vì khi 1 câu tiếng
                # dài hơn khung sub, current_pos bị đẩy vượt mốc câu kế -> không
                # chèn khoảng lặng -> toàn bộ phần sau chạy sớm dần: "tiếng đi
                # trước, chữ đi sau", lệch tích lũy càng về cuối càng nặng.
                combined = AudioSegment.silent(duration=total_ms)
                segments_to_mix.sort(key=lambda x: x[0])
                for start_ms, seg in segments_to_mix:
                    if self._stop: break
                    combined = combined.overlay(seg, position=start_ms)

                if self._stop:
                    return None

                dub_audio = os.path.join(temp_dir, "dub_final.mp3")
                combined.export(dub_audio, format="mp3")
                self.pct_signal.emit(video_path, 72)
                self.progress_signal.emit(f"[{idx+1}] 🎬 Đang mix vào video bằng ffmpeg...")

                out_video = os.path.splitext(video_path)[0] + "_dubbed.mp4"
                import subprocess as _sp, sys as _sys
                si = None
                if _sys.platform == "win32":
                    si = _sp.STARTUPINFO()
                    si.dwFlags |= _sp.STARTF_USESHOWWINDOW

                # --- TÁCH NHẠC NỀN AN TOÀN BẰNG SUBPROCESS DEMUCS ---------------
                vocals_audio = None
                if getattr(self, "remove_bgm", False):
                    _cache_path = _get_vocals_cache_path(video_path)
                    if os.path.exists(_cache_path):
                        # Đã có sẵn từ BgmPrecomputeThread (tách ngầm song song
                        # lúc đang dịch) -> dùng luôn, khỏi tách lại từ đầu,
                        # tiết kiệm gần như toàn bộ thời gian chờ Demucs ở đây.
                        vocals_audio = _cache_path
                        _vocals_from_cache = True
                        self.progress_signal.emit(f"[{idx+1}] ⚡ Dùng vocals đã tách sẵn (cache), bỏ qua bước tách...")
                    else:
                      with _GLOBAL_DEMUCS_GATE:  # khóa GPU dùng chung toàn app, tránh mọi nơi tranh chấp GPU cùng lúc
                        try:
                            self.progress_signal.emit(f"[{idx+1}] 🎵 Đang tách thoại gốc bằng Demucs (Tiến trình độc lập)...")
                        
                            raw_wav = os.path.join(temp_dir, "orig_audio.wav")
                            _sp.run(
                                [ffmpeg, "-y", "-i", video_path, "-vn",
                                 "-acodec", "pcm_s16le", raw_wav],
                                startupinfo=si, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL
                            )

                            demucs_out = os.path.join(temp_dir, "demucs_out")
                            # Dùng mdx_extra (KHÔNG có hậu tố _q) vì bản _q cần gói "diffq"
                            # để giải nén weights, mà diffq không có wheel cho Python 3.11 trên
                            # Windows -> pip phải tự biên dịch, cần Visual Studio Build Tools.
                            # mdx_extra chất lượng tương đương, không cần diffq gì cả.
                            model_name = "mdx_extra"
                            _demucs_py = _resolve_demucs_python()
                            stem_name = os.path.splitext(os.path.basename(raw_wav))[0]
                            vocals_path = os.path.join(demucs_out, model_name, stem_name, "vocals.wav")

                            # Danh sách thiết bị sẽ thử theo thứ tự. Nếu khách muốn GPU và
                            # máy đã detect có card thật -> thử "cuda" trước, LỖI thì TỰ RỚT
                            # về "cpu" (fallback thật sự, không crash). Máy không GPU -> chỉ chạy CPU.
                            _want_gpu = getattr(self, "use_gpu", False) and getattr(self, "_has_real_gpu", False)
                            _device_chain = (["cuda", "cpu"] if _want_gpu else ["cpu"])

                            vocals_audio = None
                            _last_err = ""
                            for _device in _device_chain:
                                env = _clean_subprocess_env(_demucs_py)  # tránh xung đột DLL với app Nuitka
                                env["KMP_DUPLICATE_LIB_OK"] = "TRUE"
                                if _device == "cuda":
                                    env.pop("CUDA_VISIBLE_DEVICES", None)
                                    _dev_label = "GPU"
                                else:
                                    # ép torch không thấy GPU + giới hạn luồng CPU cho an toàn máy khách
                                    env["CUDA_VISIBLE_DEVICES"] = "-1"
                                    _n_threads = str(max(1, int(os.cpu_count() * 0.3)))
                                    env["OMP_NUM_THREADS"] = _n_threads
                                    env["MKL_NUM_THREADS"] = _n_threads
                                    env["NUMEXPR_NUM_THREADS"] = _n_threads
                                    env["OPENBLAS_NUM_THREADS"] = _n_threads
                                    _dev_label = "CPU (30% Công suất - An toàn)"

                                if _device == "cuda":
                                    self.progress_signal.emit(f"[{idx+1}] 🚀 Đang thử tách bằng GPU...")

                                cmd_demucs = [
                                    _demucs_py, "-m", "demucs.separate",
                                    "-n", model_name,
                                    "--two-stems", "vocals",
                                    "-d", _device,
                                    "--out", demucs_out,
                                    raw_wav
                                ]
                                res_d = _sp.run(cmd_demucs, env=env, startupinfo=si,
                                                stdout=_sp.PIPE, stderr=_sp.PIPE)

                                if res_d.returncode == 0 and os.path.exists(vocals_path):
                                    vocals_audio = vocals_path
                                    self.progress_signal.emit(f"[{idx+1}] ✅ Tách thoại xong! ({_dev_label})")
                                    break
                                else:
                                    _last_err = res_d.stderr.decode("utf-8", errors="ignore")[-200:]
                                    if _device == "cuda":
                                        # GPU lỗi (hết VRAM, driver cũ, torch không thấy cuda...) -> báo & rớt về CPU
                                        self.progress_signal.emit(
                                            f"[{idx+1}] ⚠️ GPU tách lỗi, tự chuyển sang CPU..."
                                        )
                                    # xóa output dở của lần GPU để lần CPU chạy sạch
                                    try:
                                        if os.path.isdir(demucs_out):
                                            import shutil as _shutil
                                            _shutil.rmtree(demucs_out, ignore_errors=True)
                                    except Exception:
                                        pass

                            if not vocals_audio:
                                raise RuntimeError(_last_err or "Không tạo được vocals.wav")
                        except ImportError:
                            self.progress_signal.emit(f"[{idx+1}] ⚠️ Chưa cài demucs! Bỏ qua tách nhạc.")
                        except Exception as bgm_e:
                            import traceback
                            full_err = traceback.format_exc()
                            # Log full lỗi ra file để debug
                            try:
                                log_path = os.path.join(
                                    os.environ.get("APPDATA", os.path.expanduser("~")),
                                    "BoomStudio", "demucs_error.log"
                                )
                                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                                with open(log_path, "w", encoding="utf-8") as _lf:
                                    _lf.write(full_err)
                            except Exception:
                                pass
                            short = str(bgm_e)[:200]
                            self.progress_signal.emit(
                                f"[{idx+1}] ⚠️ Lỗi Demucs: {short}\n"
                                r"  Chi tiết lỗi đã lưu tại: AppData\Roaming\BoomStudio\demucs_error.log"
                            )

                # ── MIX & GHÉP VÀO VIDEO ──────────────────────────────────────
                if vocals_audio:
                    # Bước 1: Mix vocals.wav vào video gốc → video_vocals.mp4
                    self.pct_signal.emit(video_path, 90)
                    ov = getattr(self, "orig_volume", 15) / 100.0
                    video_vocals = os.path.join(temp_dir, "video_vocals.mp4")
                    self.progress_signal.emit(f"[{idx+1}] 🎵 Đang ghép nhạc nền đã tách vào video...")
                    cmd_vocals = [ffmpeg, "-y",
                                  "-i", video_path,
                                  "-i", vocals_audio,
                                  "-map", "0:v", "-map", "1:a",
                                  "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                                  "-shortest",
                                  video_vocals]
                    res_v = _sp.run(cmd_vocals, startupinfo=si, stdout=_sp.DEVNULL, stderr=_sp.PIPE)
                    if res_v.returncode != 0:
                        err = res_v.stderr.decode("utf-8", errors="ignore")[-200:]
                        raise RuntimeError(f"ffmpeg ghép vocals lỗi: {err}")

                    # Bước 2: Mix dub_audio vào video_vocals.mp4 → out_video
                    self.progress_signal.emit(f"[{idx+1}] 🎙 Đang ghép tiếng lồng vào video...")
                    if getattr(self, "mute_original", True):
                        cmd = [ffmpeg, "-y",
                               "-i", video_vocals,
                               "-i", dub_audio,
                               "-map", "0:v", "-map", "1:a",
                               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                               "-shortest",
                               out_video]
                    else:
                        cmd = [ffmpeg, "-y",
                               "-i", video_vocals,
                               "-i", dub_audio,
                               "-filter_complex",
                               f"[0:a]volume={ov:.3f}[orig];[1:a]volume=1.0[dub];[orig][dub]amix=inputs=2:duration=longest:normalize=0[aout]",
                               "-map", "0:v", "-map", "[aout]",
                               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                               out_video]

                elif getattr(self, "mute_original", True):
                    # Không tách nhạc nền → lồng tiếng bình thường
                    cmd = [ffmpeg, "-y",
                           "-i", video_path,
                           "-i", dub_audio,
                           "-map", "0:v", "-map", "1:a",
                           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                           "-shortest",
                           out_video]
                else:
                    ov = getattr(self, "orig_volume", 15) / 100.0
                    cmd = [ffmpeg, "-y",
                           "-i", video_path,
                           "-i", dub_audio,
                           "-filter_complex",
                           f"[0:a]volume={ov:.3f}[orig];[1:a]volume=1.0[dub];[orig][dub]amix=inputs=2:duration=longest:normalize=0[aout]",
                           "-map", "0:v", "-map", "[aout]",
                           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                           out_video]

                if not vocals_audio:
                    self.pct_signal.emit(video_path, 90)
                    res = _sp.run(cmd, startupinfo=si, stdout=_sp.DEVNULL, stderr=_sp.PIPE)
                    if res.returncode != 0:
                        err = res.stderr.decode("utf-8", errors="ignore")[-200:]
                        raise RuntimeError(f"ffmpeg lỗi: {err}")
                else:
                    res = _sp.run(cmd, startupinfo=si, stdout=_sp.DEVNULL, stderr=_sp.PIPE)
                    if res.returncode != 0:
                        err = res.stderr.decode("utf-8", errors="ignore")[-200:]
                        raise RuntimeError(f"ffmpeg ghép lồng tiếng lỗi: {err}")

                self.pct_signal.emit(video_path, 100)
                self.progress_signal.emit(f"[{idx+1}] ✅ Xong! → {os.path.basename(out_video)}")
                return True

            except Exception as e:
                self.progress_signal.emit(f"[{idx+1}] ❌ Lỗi {bname}: {str(e)[:120]}")
                return False
            finally:
                try: shutil.rmtree(temp_dir)
                except: pass
                # Dọn file cache vocals đã dùng xong (dù thành công hay lỗi),
                # tránh để rác .vocals_cache.wav nằm lại cạnh video mãi mãi.
                if _vocals_from_cache:
                    try:
                        _cp = _get_vocals_cache_path(video_path)
                        if os.path.exists(_cp): os.remove(_cp)
                    except Exception:
                        pass

        # ── ĐIỀU PHỐI: Edge TTS chạy TUẦN TỰ từng video (bản thân nó đã đa
        # luồng ở mức từng câu thoại rồi, không cần thêm lớp song song ở đây).
        # CapCut chạy SONG SONG NHIỀU VIDEO cùng lúc (mặc định 4) để tăng tốc
        # tổng thể, vì mỗi video chỉ dùng 4 luồng/segment (khóa cứng phía trên)
        # -> gộp lại vẫn nằm trong giới hạn an toàn cho API CapCut.
        is_neural_outer = "Neural" in self.voice_type

        def _tally(result):
            nonlocal ok, failed
            if result is None:
                return  # bị hủy giữa chừng, không tính
            with ok_lock:
                if result: ok += 1
                else: failed += 1

        if is_neural_outer:
            for idx, task in enumerate(self.tasks):
                if self._stop:
                    self.progress_signal.emit("🛑 Đã dừng."); break
                _tally(_process_one_task(idx, task))
        else:
            VIDEO_PARALLEL_CAPCUT = 4
            with ThreadPoolExecutor(max_workers=VIDEO_PARALLEL_CAPCUT) as vid_ex:
                futs = {vid_ex.submit(_process_one_task, idx, task): idx
                        for idx, task in enumerate(self.tasks)}
                for fut in as_completed(futs):
                    _tally(fut.result())

        self.finished_signal.emit(ok, failed)

class HonggouWidget(QWidget):
    balance_changed = pyqtSignal(int)
    refresh_stats_signal = pyqtSignal()
    quota_used_signal = pyqtSignal()

    def __init__(self, username, expiry="", vip_unlocked=False, parent=None):
        super().__init__(parent)
        self.username = username
        self.expiry = expiry
        self.vip_unlocked = bool(vip_unlocked)
        self.settings = QSettings("BoomStudio", "ClientApp")
        self.auth_token = self.settings.value("auth_token", "")
        
        self.current_series_id = ""
        # Trạng thái tải hàng loạt nhiều tab (xếp hàng lần lượt)
        self._batch_running = False
        self._batch_advancing = False
        self._batch_tab_queue = []
        self._batch_total = 0
        self._batch_done = 0
        self.current_job_id = ""
        self.current_episodes = []
        self.current_title = ""
        self.current_cover_url = ""
        self.monitor_thread = None
        self._active_threads = []
        self.current_quota = 20
        self._has_real_gpu = False
        # Cập nhật tooltip sau khi detect xong
        QTimer.singleShot(3000, self._update_gpu_tooltip)
        
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

    def _is_vip(self):
        return bool(getattr(self, 'vip_unlocked', False))

    def _refresh_vip_status(self):
        try:
            res = requests.get(f"{SERVER_URL}/api/client/vip_status",
                               headers={"Authorization": f"Bearer {self.auth_token}"}, timeout=5)
            if res.status_code == 200:
                self.vip_unlocked = bool(res.json().get("vip_unlocked", False))
        except Exception:
            pass
        return self.vip_unlocked

    def _build_poster_grid(self, series, checkable=False):
        lw = QListWidget()
        lw.setViewMode(QListWidget.ViewMode.IconMode)
        lw.setIconSize(QSize(150, 200))
        lw.setResizeMode(QListWidget.ResizeMode.Adjust)
        lw.setMovement(QListWidget.Movement.Static)
        lw.setDragEnabled(False)
        lw.setDragDropMode(QListWidget.DragDropMode.NoDragDrop)
        lw.setSpacing(10)
        lw.setWordWrap(True)
        lw.setStyleSheet("QListWidget { background:#0f172a; border:none; } QListWidget::item { color:#e2e8f0; }")
        missing = []
        for s in series:
            sid = str(s.get("series_id"))
            title = s.get("title", "?"); eps = s.get("total_episodes", 0)
            it = QListWidgetItem(f"{title}\n({eps} tập)")
            it.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            it.setSizeHint(QSize(170, 260))
            it.setData(Qt.ItemDataRole.UserRole, {"series_id": sid, "title": title, "total_episodes": eps})
            if checkable:
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(Qt.CheckState.Checked if sid in getattr(self, '_pick_selected', {}) else Qt.CheckState.Unchecked)
            lw.addItem(it)
            row = lw.count() - 1
            done = False
            try:
                cpath = os.path.join(self._get_covers_dir(), f"{sid}.img")
                if os.path.exists(cpath):
                    with open(cpath, 'rb') as f: img = f.read()
                    pm = QPixmap()
                    if pm.loadFromData(img) and not pm.isNull():
                        pm = pm.scaled(150, 200, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
                        it.setIcon(QIcon(pm)); done = True
            except Exception: pass
            if not done and sid:
                missing.append((row, sid, s.get("cover_url") or ""))
        thread = None
        if missing:
            thread = HistoryCoverThread(missing, self._get_covers_dir(), self.auth_token)
            def _on_ready(r, content, _lw=lw):
                try:
                    item = _lw.item(r)
                    if not item: return
                    pm = QPixmap()
                    if pm.loadFromData(content) and not pm.isNull():
                        pm = pm.scaled(150, 200, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
                        item.setIcon(QIcon(pm))
                except Exception: pass
            thread.cover_ready.connect(_on_ready)
            self._keep_thread_alive(thread)
            thread.start()
        return lw, thread

    # ================= MUA TRỌN BỘ =================
    def _open_bulk_menu(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("📦 Mua Trọn Bộ")
        dlg.setMinimumWidth(420)
        dlg.setStyleSheet("QDialog { background-color: #0f172a; }")
        lay = QVBoxLayout(dlg)
        tt = QLabel("Chọn cách mua trọn bộ phim:")
        tt.setStyleSheet("color:#e2e8f0; font-size:14px; padding:6px;")
        lay.addWidget(tt)

        b1 = QPushButton("🎲 Ngẫu Nhiên  —  2.000đ / bộ")
        b1.setStyleSheet("QPushButton { padding:14px; background:#7c3aed; color:white; font-weight:bold; font-size:14px; border:none; border-radius:10px; } QPushButton:hover { background:#6d28d9; }")
        b1.clicked.connect(lambda: (dlg.accept(), self._bulk_random_choose()))
        lay.addWidget(b1)

        b2 = QPushButton("✅ Tự Chọn  —  2.500đ / bộ")
        b2.setStyleSheet("QPushButton { padding:14px; background:#0ea5e9; color:white; font-weight:bold; font-size:14px; border:none; border-radius:10px; } QPushButton:hover { background:#0284c7; }")
        b2.clicked.connect(lambda: (dlg.accept(), self._bulk_pick_open()))
        lay.addWidget(b2)
        dlg.exec()

    def _bulk_random_choose(self):
        dlg = QDialog(self); dlg.setWindowTitle("🎲 Mua Ngẫu Nhiên"); dlg.setMinimumWidth(360)
        dlg.setStyleSheet("QDialog { background-color: #0f172a; }")
        lay = QVBoxLayout(dlg)
        lab = QLabel("Chọn số bộ muốn mua (2.000đ/bộ):")
        lab.setStyleSheet("color:#e2e8f0; font-size:14px; padding:4px;")
        lay.addWidget(lab)
        combo = QComboBox()
        for n in range(10, 101, 10):
            combo.addItem(f"{n} bộ  —  {n*2000:,}đ", n)
        combo.setStyleSheet("QComboBox { padding:10px; font-size:14px; background:#1e293b; color:#e2e8f0; border:1px solid #334155; border-radius:8px; }")
        lay.addWidget(combo)
        row = QHBoxLayout()
        cancel = QPushButton("Hủy"); cancel.clicked.connect(dlg.reject)
        cancel.setStyleSheet("QPushButton { padding:10px 18px; background:transparent; color:#94a3b8; border:1px solid #374151; border-radius:8px; }")
        ok = QPushButton("Bốc phim →"); 
        ok.setStyleSheet("QPushButton { padding:10px 18px; background:#7c3aed; color:white; font-weight:bold; border:none; border-radius:8px; }")
        ok.clicked.connect(dlg.accept)
        row.addStretch(); row.addWidget(cancel); row.addWidget(ok); lay.addLayout(row)
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        num = combo.currentData()

        excl = []
        try:
            for it in self._load_history():
                if it.get('downloaded'): excl.append(str(it.get('series_id')))
        except Exception: pass

        self.btn_bulk.setEnabled(False); self.btn_bulk.setText("⏳ Đang bốc...")
        self.bulk_quote_thread = BulkQuoteThread('random', self.username, self.auth_token, num_series=num, exclude_ids=excl)
        self._keep_thread_alive(self.bulk_quote_thread)
        self.bulk_quote_thread.result_signal.connect(self._on_bulk_quote)
        self.bulk_quote_thread.error_signal.connect(self._on_bulk_error)
        self.bulk_quote_thread.start()

    def _on_bulk_error(self, msg):
        self.btn_bulk.setEnabled(True); self.btn_bulk.setText("📦 Mua Trọn Bộ")
        QMessageBox.warning(self, "Không thực hiện được", msg)

    def _on_bulk_quote(self, data):
        self.btn_bulk.setEnabled(True); self.btn_bulk.setText("📦 Mua Trọn Bộ")
        token = data.get("token"); series = data.get("series", [])
        cost = data.get("cost", 0); num = data.get("num_series", len(series))

        dlg = QDialog(self); dlg.setWindowTitle("Xác nhận mua"); dlg.setMinimumSize(720, 640)
        dlg.setStyleSheet("QDialog { background-color: #0f172a; }")
        lay = QVBoxLayout(dlg)
        head = QLabel(f"Đã chọn <b>{num}</b> bộ")
        head.setStyleSheet("color:#e2e8f0; font-size:15px; padding:4px;")
        lay.addWidget(head)
        grid, _ = self._build_poster_grid(series, checkable=False)
        lay.addWidget(grid, 1)
        money = QLabel(f"💰 Thành tiền: <span style='color:#f59e0b; font-size:22px;'><b>{cost:,}đ</b></span>")
        money.setStyleSheet("color:#e2e8f0; font-size:15px; padding:8px;")
        lay.addWidget(money)
        warn = QLabel("⚠️ Bấm \"Xác nhận & Tải\" sẽ trừ đúng số tiền trên.")
        warn.setStyleSheet("color:#fca5a5; font-size:12px; padding:0 8px 8px 8px;"); warn.setWordWrap(True)
        lay.addWidget(warn)
        row = QHBoxLayout()
        cancel = QPushButton("Hủy"); cancel.clicked.connect(dlg.reject)
        cancel.setStyleSheet("QPushButton { padding:10px 20px; background:transparent; color:#94a3b8; border:1px solid #374151; border-radius:8px; }")
        ok = QPushButton(f"✅ Xác nhận & Tải ({cost:,}đ)"); ok.clicked.connect(dlg.accept)
        ok.setStyleSheet("QPushButton { padding:10px 20px; background:#16a34a; color:white; font-weight:bold; border:none; border-radius:8px; }")
        row.addStretch(); row.addWidget(cancel); row.addWidget(ok); lay.addLayout(row)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._bulk_confirm(token)

    def _bulk_confirm(self, token):
        self.bulk_confirm_thread = BulkConfirmThread(self.username, token, self.auth_token)
        self._keep_thread_alive(self.bulk_confirm_thread)
        self.bulk_confirm_thread.result_signal.connect(self._on_bulk_confirmed)
        self.bulk_confirm_thread.error_signal.connect(self._on_bulk_error)
        self.bulk_confirm_thread.start()

    def _on_bulk_confirmed(self, data):
        result = data.get("series", [])
        if not result:
            QMessageBox.warning(self, "Lỗi", data.get("message") or "Không lấy được link tải."); return
        try: self._refresh_balance()
        except Exception: pass

        self._bulk_queue = list(result)
        self._bulk_done_count = 0
        self._bulk_total = len(result)
        self._bulk_total_eps = sum(len(s.get("episodes", [])) for s in result)
        self._bulk_eps_done = 0

        self._bulk_dlg = QDialog(self)
        self._bulk_dlg.setWindowTitle("📥 Đang tải trọn bộ")
        self._bulk_dlg.setMinimumWidth(460)
        self._bulk_dlg.setStyleSheet("QDialog { background-color: #0f172a; }")
        v = QVBoxLayout(self._bulk_dlg)
        self._bulk_lbl_series = QLabel("Chuẩn bị...")
        self._bulk_lbl_series.setStyleSheet("color:#e2e8f0; font-size:14px; font-weight:bold; padding:4px;")
        self._bulk_lbl_series.setWordWrap(True)
        v.addWidget(self._bulk_lbl_series)
        self._bulk_bar_series = QProgressBar(); self._bulk_bar_series.setStyleSheet("QProgressBar { border:1px solid #334155; border-radius:6px; background:#1e293b; height:20px; text-align:center; color:#e2e8f0; } QProgressBar::chunk { background:#16a34a; border-radius:5px; }")
        v.addWidget(self._bulk_bar_series)
        self._bulk_lbl_overall = QLabel("")
        self._bulk_lbl_overall.setStyleSheet("color:#94a3b8; font-size:12px; padding:4px;")
        v.addWidget(self._bulk_lbl_overall)
        self._bulk_bar_overall = QProgressBar(); self._bulk_bar_overall.setStyleSheet("QProgressBar { border:1px solid #334155; border-radius:6px; background:#1e293b; height:16px; text-align:center; color:#e2e8f0; } QProgressBar::chunk { background:#7c3aed; border-radius:5px; }")
        self._bulk_bar_overall.setMaximum(max(1, self._bulk_total_eps))
        v.addWidget(self._bulk_bar_overall)
        btn_hide = QPushButton("Ẩn (tải tiếp ở nền)")
        btn_hide.setStyleSheet("QPushButton { padding:8px; background:transparent; color:#94a3b8; border:1px solid #374151; border-radius:8px; }")
        btn_hide.clicked.connect(self._bulk_dlg.hide)
        v.addWidget(btn_hide)
        self._bulk_dlg.show()

        self._start_next_bulk_series()

    def _start_next_bulk_series(self):
        if not getattr(self, '_bulk_queue', None):
            try:
                if getattr(self, '_bulk_dlg', None): self._bulk_dlg.close()
                QMessageBox.information(self, "Hoàn tất", f"Đã tải xong {self._bulk_done_count}/{self._bulk_total} bộ ({self._bulk_eps_done} tập).")
            except Exception: pass
            return
        s = self._bulk_queue.pop(0)
        sid = str(s.get("series_id")); title = s.get("title") or sid
        eps = s.get("episodes", [])
        if not eps:
            self._bulk_done_count += 1
            self._start_next_bulk_series(); return
        sub = os.path.join(self.save_folder, sid)
        try: os.makedirs(sub, exist_ok=True)
        except Exception: pass

        eps = [{
            "episode_number": e.get("episode_number"),
            "drive_link": e.get("url") or e.get("drive_link"),
            "aes_key": e.get("aes_key", ""),
            "source": e.get("source", ""),
            "file_name": e.get("file_name"),
        } for e in eps]

        cur_idx = self._bulk_done_count + 1
        try:
            self._bulk_lbl_series.setText(f"Bộ {cur_idx}/{self._bulk_total}: {title}")
            self._bulk_bar_series.setMaximum(len(eps)); self._bulk_bar_series.setValue(0)
            self._bulk_series_eps_done = 0
            self._bulk_lbl_overall.setText(f"Tổng: {self._bulk_eps_done}/{self._bulk_total_eps} tập")
        except Exception: pass

        th = DriveDownloadManager(eps, sub, self.auth_token, parent=self, max_concurrent=5, expected_total=len(eps))
        def _on_ep_done(ep_num, file_path):
            try:
                self._bulk_series_eps_done += 1
                self._bulk_eps_done += 1
                self._bulk_bar_series.setValue(self._bulk_series_eps_done)
                self._bulk_bar_overall.setValue(self._bulk_eps_done)
                self._bulk_lbl_overall.setText(f"Tổng: {self._bulk_eps_done}/{self._bulk_total_eps} tập")
            except Exception: pass
        def _series_done(_succ=0):
            self._bulk_done_count += 1
            try: self._save_to_history(sid, title, "", total_eps=s.get("total_episodes", len(eps)), downloaded=True)
            except Exception: pass
            self._start_next_bulk_series()
        th.done_signal.connect(_on_ep_done)
        th.all_done_signal.connect(_series_done)
        self._current_bulk_thread = th
        th.start()

    def _bulk_pick_open(self):
        self._pick_selected = {}
        self._pick_page = 1; self._pick_keyword = ""
        try: self.pick_search.clear()
        except Exception: pass
        self._pick_total_lbl.setText("Chọn theo mức 10, 20, 30... bộ")
        try: self._pick_ok.setEnabled(False)
        except Exception: pass
        self.content_stack.setCurrentWidget(self.page_pick)
        self._load_pick_page()

    def _do_pick_search(self):
        self._pick_keyword = self.pick_search.text().strip()
        self._pick_page = 1
        self._load_pick_page()

    def _pick_change_page(self, delta):
        self._pick_page = max(1, self._pick_page + delta)
        self._load_pick_page()

    def _bulk_pick_submit_page(self):
        n = len(self._pick_selected)
        if n == 0:
            QMessageBox.information(self, "Chưa chọn", "Bạn chưa chọn bộ nào."); return
        if n % 10 != 0:
            thieu = 10 - (n % 10)
            QMessageBox.information(self, "Chưa đủ mức", f"Gói tự chọn phải theo mức 10, 20, 30... bộ.\nBạn đang chọn {n} bộ — hãy chọn thêm {thieu} bộ (đủ {n+thieu}) hoặc bỏ bớt về {n - (n%10)} bộ.")
            return
        ids = list(self._pick_selected.keys())
        self.content_stack.setCurrentWidget(self.page_grid)
        self.bulk_quote_thread = BulkQuoteThread('pick', self.username, self.auth_token, series_ids=ids)
        self._keep_thread_alive(self.bulk_quote_thread)
        self.bulk_quote_thread.result_signal.connect(self._on_bulk_quote)
        self.bulk_quote_thread.error_signal.connect(self._on_bulk_error)
        self.bulk_quote_thread.start()

    def _load_pick_page(self):
        try:
            res = requests.get(f"{SERVER_URL}/api/client/catalog",
                params={"username": self.username, "keyword": self._pick_keyword, "page": self._pick_page, "page_size": 40},
                headers={"Authorization": f"Bearer {self.auth_token}"}, timeout=20)
            data = res.json()
        except Exception as e:
            QMessageBox.warning(self, "Máy chủ đang bận",
                                "Máy chủ đang quá tải.\nVui lòng chờ 1-2 phút rồi bấm lại nhé!"); return
        if data.get("status") != "success":
            QMessageBox.warning(self, "Lỗi", data.get("message", "Có lỗi xảy ra")); return
        series = data.get("series", [])
        total = data.get("total", 0); ps = data.get("page_size", 40)
        pages = max(1, (total + ps - 1) // ps)
        self.pick_page_lbl.setText(f"Trang {self._pick_page}/{pages} ({total} bộ)")

        self._pick_list.blockSignals(True)
        self._pick_list.clear()
        pick_missing = []
        for s in series:
            sid = str(s.get("series_id"))
            it = QListWidgetItem(f"{s.get('title','?')}\n({s.get('total_episodes',0)} tập)")
            it.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            it.setSizeHint(QSize(170, 270))
            it.setData(Qt.ItemDataRole.UserRole, {"series_id": sid, "title": s.get("title"), "total_episodes": s.get("total_episodes", 0)})
            self._pick_list.addItem(it)
            if sid in self._pick_selected:
                it.setSelected(True)
            row = self._pick_list.count() - 1
            done = False
            try:
                cpath = os.path.join(self._get_covers_dir(), f"{sid}.img")
                if os.path.exists(cpath):
                    with open(cpath, 'rb') as f: img = f.read()
                    pm = QPixmap()
                    if pm.loadFromData(img) and not pm.isNull():
                        pm = pm.scaled(150, 200, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
                        it.setIcon(QIcon(pm)); done = True
            except Exception: pass
            if not done and sid:
                pick_missing.append((row, sid, s.get("cover_url") or ""))
        self._pick_list.blockSignals(False)
        if pick_missing:
            self._pick_cover_thread = HistoryCoverThread(pick_missing, self._get_covers_dir(), self.auth_token)
            def _pk_ready(r, content):
                try:
                    item = self._pick_list.item(r)
                    if not item: return
                    pm = QPixmap()
                    if pm.loadFromData(content) and not pm.isNull():
                        pm = pm.scaled(150, 200, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
                        item.setIcon(QIcon(pm))
                except Exception: pass
            self._pick_cover_thread.cover_ready.connect(_pk_ready)
            self._keep_thread_alive(self._pick_cover_thread)
            self._pick_cover_thread.start()

    def _on_pick_item_clicked(self, item):
        meta = item.data(Qt.ItemDataRole.UserRole)
        if not meta: return
        sid = meta["series_id"]
        if sid in self._pick_selected:
            self._pick_selected.pop(sid, None)
            item.setSelected(False)
        else:
            self._pick_selected[sid] = meta
            item.setSelected(True)
        self._update_pick_total()

    def _update_pick_total(self):
        n = len(self._pick_selected); cost = n * 2500
        if n == 0:
            self._pick_total_lbl.setText("Chưa chọn bộ nào")
            self._pick_ok.setEnabled(False)
        elif n % 10 != 0:
            thieu = 10 - (n % 10)
            self._pick_total_lbl.setText(f"Đã chọn {n} bộ — cần chọn thêm {thieu} bộ (đủ {n + thieu}) — {cost:,}đ")
            self._pick_ok.setEnabled(False)
        else:
            self._pick_total_lbl.setText(f"✅ Đã chọn {n} bộ — {cost:,}đ")
            self._pick_ok.setEnabled(True)

    def _bulk_pick_submit(self, dlg):
        if not self._pick_selected:
            QMessageBox.information(self, "Chưa chọn", "Bạn chưa chọn bộ nào."); return
        ids = list(self._pick_selected.keys())
        dlg.accept()
        self.bulk_quote_thread = BulkQuoteThread('pick', self.username, self.auth_token, series_ids=ids)
        self._keep_thread_alive(self.bulk_quote_thread)
        self.bulk_quote_thread.result_signal.connect(self._on_bulk_quote)
        self.bulk_quote_thread.error_signal.connect(self._on_bulk_error)
        self.bulk_quote_thread.start()

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

    def _clear_all_history(self):
        items = self._load_history()
        if not items:
            QMessageBox.information(self, "Lịch sử trống", "Chưa có lịch sử tải nào để xóa.")
            return
        reply = QMessageBox.question(
            self, "Xóa toàn bộ lịch sử",
            f"Xóa hết {len(items)} mục trong lịch sử tải?\n\n"
            "(Chỉ xóa danh sách lịch sử — KHÔNG xóa file phim đã tải về máy.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            f = self._get_history_file()
            if os.path.exists(f):
                os.remove(f)
        except Exception as e:
            QMessageBox.warning(self, "Lỗi", f"Không xóa được file lịch sử: {e}")
            return
        # reset cache + vẽ lại sidebar
        self._cached_history_ids = set()
        self._history_sig = None
        try:
            self._render_history_sidebar()
        except Exception:
            if hasattr(self, 'history_list'):
                self.history_list.clear()
        self.lbl_status.setText("🗑 Đã xóa toàn bộ lịch sử tải.")

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

        # Nút "+ Bộ mới": mở 1 tab trống để dán link bộ khác và quét riêng.
        self.btn_new_tab = QPushButton("➕ Bộ mới")
        self.btn_new_tab.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_new_tab.setToolTip("Mở 1 tab mới để quét thêm 1 bộ phim khác (mỗi bộ 1 tab riêng).")
        self.btn_new_tab.setStyleSheet("QPushButton { padding: 12px 18px; font-size: 14px; background-color: #334155; color: #e2e8f0; border-radius: 8px; font-weight: bold; border: none; } QPushButton:hover { background-color: #475569; }")
        self.btn_new_tab.clicked.connect(lambda: self._new_series_tab())

        # Nút "Tải hàng loạt": tải lần lượt tất cả các tab (xong bộ này sang bộ kế).
        self.btn_batch_dl = QPushButton("📥 Tải hàng loạt")
        self.btn_batch_dl.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_batch_dl.setToolTip("Tải TẤT CẢ các tab, lần lượt: xong bộ này tự sang bộ kế tiếp.")
        self.btn_batch_dl.setStyleSheet("QPushButton { padding: 12px 18px; font-size: 14px; background-color: #7c3aed; color: white; border-radius: 8px; font-weight: bold; border: none; } QPushButton:hover { background-color: #6d28d9; } QPushButton:disabled { background-color: #374151; color: #64748b; }")
        self.btn_batch_dl.clicked.connect(self._start_batch_download)

        # Nút "Đồng bộ": copy cấu hình (tách sub/dịch/lồng...) của tab hiện tại
        # sang TẤT CẢ các tab, để không phải chỉnh lại từng bộ.
        self.btn_sync_cfg = QPushButton("🔄 Đồng bộ")
        self.btn_sync_cfg.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sync_cfg.setToolTip("Áp cấu hình (tách sub/dịch/lồng tiếng/tách nhạc...) của tab hiện tại cho TẤT CẢ các bộ.")
        self.btn_sync_cfg.setStyleSheet("QPushButton { padding: 12px 16px; font-size: 14px; background-color: #0891b2; color: white; border-radius: 8px; font-weight: bold; border: none; } QPushButton:hover { background-color: #0e7490; }")
        self.btn_sync_cfg.clicked.connect(self._sync_config_to_all_tabs)

        top_bar.addWidget(self.url_input); top_bar.addWidget(self.btn_scan)
        top_bar.addWidget(self.btn_new_tab); top_bar.addWidget(self.btn_sync_cfg); top_bar.addWidget(self.btn_batch_dl)
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
        hp_header = QHBoxLayout()
        hp_header.setContentsMargins(0, 0, 0, 0)
        lbl_hp = QLabel("🕒 Lịch Sử Tải")
        lbl_hp.setStyleSheet("color: #f59e0b; font-size: 15px; font-weight: bold; padding: 4px 2px;")
        hp_header.addWidget(lbl_hp)
        hp_header.addStretch()
        self.btn_clear_history = QPushButton("🗑 Xóa")
        self.btn_clear_history.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear_history.setToolTip("Xóa toàn bộ lịch sử tải (không xóa file phim đã tải về máy)")
        self.btn_clear_history.setStyleSheet("QPushButton { padding: 4px 10px; font-size: 12px; background-color: transparent; color: #f87171; border: 1px solid #7f1d1d; border-radius: 6px; font-weight: bold; } QPushButton:hover { background-color: #7f1d1d; color: white; }")
        self.btn_clear_history.clicked.connect(self._clear_all_history)
        hp_header.addWidget(self.btn_clear_history)
        hp_layout.addLayout(hp_header)
        self.history_list = QListWidget()
        self.history_list.setIconSize(QSize(52, 70))
        self.history_list.setDragEnabled(False)
        self.history_list.setDragDropMode(QListWidget.DragDropMode.NoDragDrop)
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
        genres = [("🔥 Tất Cả", None), ("👍 BXH Đề Cử", "BXH Đề Cử"), ("📈 BXH Lượt Xem", "BXH Lượt Xem"), ("🆕 BXH Phim Mới", "BXH Phim Mới"), ("🐼 BXH Hoạt Hình", "BXH Hoạt Hình"), ("📅 Lịch Phim", "Lịch Phim")]
        for name, tag in genres:
            btn = QPushButton(name)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("genre_tag", tag)
            btn.clicked.connect(lambda checked, b=btn: self._on_genre_clicked(b))
            genre_layout.addWidget(btn)
            self.genre_buttons.append(btn)

        self.btn_bulk = QPushButton("📦 Mua Trọn Bộ")
        self.btn_bulk.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_bulk.setStyleSheet("QPushButton { background-color: #7c3aed; color: #ffffff; font-weight: bold; font-size: 13px; border-radius: 16px; padding: 8px 18px; border: none; } QPushButton:hover { background-color: #6d28d9; }")
        self.btn_bulk.clicked.connect(self._open_bulk_menu)
        genre_layout.addWidget(self.btn_bulk)

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
        self.hot_list.setMovement(QListWidget.Movement.Static)
        self.hot_list.setDragEnabled(False)
        self.hot_list.setDragDropMode(QListWidget.DragDropMode.NoDragDrop)
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

        self.chk_auto_cover = QCheckBox("🖼️ Tự động tải ảnh bìa")
        self.chk_auto_cover.setChecked(False)
        self.chk_auto_cover.setToolTip("Bật = tự động lưu ảnh bìa phim vào thư mục phim mỗi khi bắt đầu tải.")
        self.chk_auto_cover.setStyleSheet("""
            QCheckBox { color: #fcd34d; font-weight: bold; font-size: 13px; padding: 2px; }
            QCheckBox::indicator { width: 18px; height: 18px; border: 2px solid #d97706; border-radius: 4px; background: #1f2937; }
            QCheckBox::indicator:checked { background: #d97706; }
        """)
        try: self.chk_auto_cover.setChecked(self.settings.value("auto_cover", "false") == "true")
        except Exception: pass
        self.chk_auto_cover.stateChanged.connect(
            lambda v: self.settings.setValue("auto_cover", "true" if v else "false")
        )
        btn_back_layout.addWidget(self.chk_auto_cover)

        detail_layout.addLayout(btn_back_layout)

        self.lbl_status = QLabel("Trạng thái: Sẵn sàng phục vụ...")
        self.lbl_status.setStyleSheet("color: #10b981; font-size: 14px; font-weight: bold; margin-top: 10px;")
        detail_layout.addWidget(self.lbl_status)

        self.total_progress = QProgressBar()
        self.total_progress.setFormat("Đã tải %v/%m tập  (%p%)")
        self.total_progress.setStyleSheet("QProgressBar { background: #1f2937; border: 1px solid #374151; border-radius: 8px; color: #f8fafc; font-weight: bold; text-align: center; min-height: 22px; } QProgressBar::chunk { background-color: #10b981; border-radius: 7px; }")
        self.total_progress.hide()
        detail_layout.addWidget(self.total_progress)

        self.table = self._make_episode_table()

        # ── Bọc bảng chọn-tập vào QTabWidget: mỗi bộ phim = 1 tab riêng ──
        # self.table LUÔN trỏ tới bảng của tab đang mở, nên toàn bộ code cũ
        # dùng self.table không phải sửa. Mỗi tab lưu series_id/title/episodes
        # riêng trong dict self._tab_data[table].
        self.series_tabs = QTabWidget()
        self.series_tabs.setTabsClosable(True)
        self.series_tabs.setStyleSheet("""
            QTabWidget::pane { border: 1px solid #374151; border-radius: 8px; background: #111827; top: -1px; }
            QTabBar::tab { background: #1f2937; color: #94a3b8; padding: 7px 16px; font-weight: bold;
                border: 1px solid #374151; border-bottom: none; margin-right: 2px; border-top-left-radius: 6px; border-top-right-radius: 6px; }
            QTabBar::tab:selected { background: #111827; color: #38bdf8; }
        """)
        self.series_tabs.tabCloseRequested.connect(self._close_series_tab)
        # LƯU Ý: KHÔNG nối currentChanged ở đây - vì các widget cấu hình
        # (chk_auto_stt...) chưa được tạo. Nối ở CUỐI __init__ (xem
        # _wire_series_tab_signal) để tránh chạy _apply_config lên widget
        # chưa tồn tại.
        # dict: table_widget -> {series_id, title, episodes, cover_url, config}
        self._tab_data = {}
        self.series_tabs.addTab(self.table, "Phim 1")
        self._tab_data[self.table] = {"series_id": "", "title": "Phim 1", "episodes": []}
        detail_layout.addWidget(self.series_tabs)

        bottom_layout = QHBoxLayout()

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

        self.btn_pause = QPushButton("⏸ Tạm dừng")
        self.btn_pause.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pause.setStyleSheet("QPushButton { padding: 14px 24px; background-color: #f59e0b; color: white; border-radius: 8px; font-weight: bold; font-size: 15px; margin-top: 10px; border: none; } QPushButton:hover { background-color: #d97706; } QPushButton:disabled { background-color: #374151; color: #64748b; }")
        self.btn_pause.setEnabled(False)
        self.btn_pause.hide()  # chỉ hiện khi đang tải
        self.btn_pause.clicked.connect(self._toggle_pause_download)

        dl_row = QHBoxLayout(); dl_row.setSpacing(8)
        dl_row.addWidget(self.btn_download, 1)
        dl_row.addWidget(self.btn_pause)

        bottom_layout.addLayout(merge_layout)
        bottom_layout.addWidget(self.btn_select_all)
        bottom_layout.addLayout(dl_row)

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

        lbl_stt_src = QLabel("  Tiếng gốc:")
        lbl_stt_src.setStyleSheet("color: #e2e8f0; font-weight: bold;")
        stt_ctrl.addWidget(lbl_stt_src)
        self.cmb_stt_src = QComboBox()
        self.cmb_stt_src.addItems(["zh-CN","en-US","vi-VN","ja-JP","ko-KR","fr-FR"])
        self.cmb_stt_src.setToolTip("Ngôn ngữ gốc trong phim")
        self.cmb_stt_src.setStyleSheet("QComboBox { background:#1f2937; color:#f8fafc; border:1px solid #374151; border-radius:6px; padding:4px 8px; } QComboBox QAbstractItemView { background:#1e293b; color:#f8fafc; }")
        stt_ctrl.addWidget(self.cmb_stt_src)

        lbl_stt_out = QLabel("→ Dịch sang:")
        lbl_stt_out.setStyleSheet("color: #e2e8f0; font-weight: bold;")
        stt_ctrl.addWidget(lbl_stt_out)
        self.cmb_stt_out = QComboBox()
        self.cmb_stt_out.addItems(["vi-VN","en-US","zh-CN","ja-JP","ko-KR"])
        self.cmb_stt_out.setToolTip("Ngôn ngữ phụ đề cần dịch sang")
        self.cmb_stt_out.setStyleSheet("QComboBox { background:#1f2937; color:#f8fafc; border:1px solid #374151; border-radius:6px; padding:4px 8px; } QComboBox QAbstractItemView { background:#1e293b; color:#f8fafc; }")
        stt_ctrl.addWidget(self.cmb_stt_out)

        self.chk_do_translate = QCheckBox("🌐 Tự động dịch sau khi tách sub")
        self.chk_do_translate.setChecked(False)
        self.chk_do_translate.setToolTip("Tích vào mới tự động dịch sub sang tiếng Việt")
        self.chk_do_translate.setStyleSheet("""
            QCheckBox { color: #86efac; font-size: 12px; padding: 4px; font-weight: bold; }
            QCheckBox::indicator { width: 18px; height: 18px; border: 2px solid #16a34a;
                border-radius: 4px; background: #1e293b; }
            QCheckBox::indicator:checked { background: #16a34a; border-color: #16a34a; }
        """)
        stt_ctrl.addWidget(self.chk_do_translate)

        # ── Chọn Engine dịch: Gemini (trình duyệt, free) hoặc DeepSeek (API) ──
        self.cb_translate_engine = QComboBox()
        self.cb_translate_engine.addItems(["🌐 Gemini", "🚀 DeepSeek V4 Pro"])
        self.cb_translate_engine.setToolTip("Chọn công cụ dịch: Gemini (miễn phí, qua trình duyệt) hoặc DeepSeek (API key riêng, nhanh & rẻ)")
        self.cb_translate_engine.setStyleSheet("QComboBox { background:#1f2937; color:#f8fafc; border:1px solid #374151; border-radius:6px; padding:4px 8px; } QComboBox QAbstractItemView { background:#1e293b; color:#f8fafc; }")
        _saved_engine = QSettings("BoomStudio", "ClientApp").value("trans_engine_main", "🌐 Gemini")
        self.cb_translate_engine.setCurrentText(_saved_engine)
        self.cb_translate_engine.currentTextChanged.connect(self._on_translate_engine_changed)
        stt_ctrl.addWidget(self.cb_translate_engine)

        # ── Hiện trình duyệt khi dịch (để soi Gemini chạy) - chỉ dùng để xem/debug ──
        self.chk_show_browser = QCheckBox("👁 Hiện trình duyệt khi dịch")
        self.chk_show_browser.setToolTip(
            "Bật = Chrome hiện lên cho bạn xem Gemini gõ prompt & dịch (chậm hơn, chỉ nên bật khi soi 1 tập).\n"
            "Tắt = chạy ngầm bình thường (nhanh hơn, dùng khi dịch hàng loạt)."
        )
        self.chk_show_browser.setStyleSheet("""
            QCheckBox { color: #fcd34d; font-size: 12px; padding: 4px; font-weight: bold; }
            QCheckBox::indicator { width: 18px; height: 18px; border: 2px solid #f59e0b;
                border-radius: 4px; background: #1e293b; }
            QCheckBox::indicator:checked { background: #f59e0b; border-color: #f59e0b; }
        """)
        try:
            self.chk_show_browser.setChecked(
                QSettings("BoomStudio", "ClientApp").value("show_browser_translate", "false") == "true")
        except Exception:
            pass
        self.chk_show_browser.stateChanged.connect(
            lambda v: QSettings("BoomStudio", "ClientApp").setValue(
                "show_browser_translate", "true" if v else "false"))
        stt_ctrl.addWidget(self.chk_show_browser)

        # ── Số tập dịch SONG SONG bằng Gemini (mỗi tập 1 Chrome ẩn riêng) ──
        stt_ctrl.addWidget(QLabel("Tập song song:", styleSheet="color:#8A8D98; font-size:11px;"))
        self.spn_trans_workers = QSpinBox()
        self.spn_trans_workers.setRange(1, 4)
        self.spn_trans_workers.setValue(2)   # mặc định 2 cho an toàn (RAM + tránh Google nghi)
        self.spn_trans_workers.setFixedWidth(55)
        self.spn_trans_workers.setToolTip(
            "Số tập dịch CÙNG LÚC bằng Gemini (mỗi tập mở 1 Chrome ẩn riêng).\n"
            "• 2 = an toàn cho phần lớn máy.\n"
            "• 3-4 = nhanh hơn nhưng ngốn RAM/CPU và cùng 1 tài khoản Gemini\n"
            "  bắn nhiều phiên dễ bị Google chèn captcha. Chỉ tăng nếu máy khỏe.\n"
            "(Chỉ áp dụng cho engine Gemini.)"
        )
        self.spn_trans_workers.setStyleSheet("QSpinBox { background:#1f2937; color:#fde68a; border:1px solid #f59e0b; border-radius:6px; padding:3px; }")
        try:
            self.spn_trans_workers.setValue(int(QSettings("BoomStudio", "ClientApp").value("trans_workers", 2)))
        except Exception:
            pass
        self.spn_trans_workers.valueChanged.connect(
            lambda v: QSettings("BoomStudio", "ClientApp").setValue("trans_workers", int(v)))
        stt_ctrl.addWidget(self.spn_trans_workers)

        self.txt_ds_key_main = QLineEdit()
        self.txt_ds_key_main.setPlaceholderText("DeepSeek API Key (sk-...)")
        self.txt_ds_key_main.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_ds_key_main.setFixedWidth(160)
        self.txt_ds_key_main.setStyleSheet("QLineEdit { background:#1f2937; color:#f8fafc; border:1px solid #374151; border-radius:6px; padding:4px 8px; }")
        self.txt_ds_key_main.setText(QSettings("BoomStudio", "ClientApp").value("deepseek_api_key", ""))
        self.txt_ds_key_main.textChanged.connect(
            lambda t: QSettings("BoomStudio", "ClientApp").setValue("deepseek_api_key", t)
        )
        stt_ctrl.addWidget(self.txt_ds_key_main)
        self._on_translate_engine_changed(self.cb_translate_engine.currentText())  # set ẩn/hiện ban đầu

        self.btn_stt_now = QPushButton("🔤 Tách sub ngay")
        self.btn_stt_now.setEnabled(False)
        self.btn_stt_now.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stt_now.setStyleSheet("QPushButton { padding: 8px 18px; background-color: #7c3aed; color: white; border-radius: 8px; font-weight: bold; font-size: 13px; border: none; } QPushButton:hover { background-color: #6d28d9; } QPushButton:disabled { background-color: #374151; color: #64748b; }")
        self.btn_stt_now.clicked.connect(self._run_stt_on_downloaded)
        stt_ctrl.addWidget(self.btn_stt_now)
        stt_ctrl.addStretch()
        detail_layout.addLayout(stt_ctrl)

        dub_ctrl = QHBoxLayout(); dub_ctrl.setSpacing(8)  # HÀNG 1: control cốt lõi
        dub_ctrl2 = QHBoxLayout(); dub_ctrl2.setSpacing(8)  # HÀNG 2: tinh chỉnh âm thanh
        self.chk_auto_dub = QCheckBox("🎙 Lồng tiếng sau khi tách sub")
        self.chk_auto_dub.setStyleSheet("""
            QCheckBox { color: #fde68a; font-size: 12px; padding: 4px; font-weight: bold; }
            QCheckBox::indicator { width: 18px; height: 18px; border: 2px solid #f59e0b;
                border-radius: 4px; background: #1e293b; }
            QCheckBox::indicator:checked { background: #f59e0b; border-color: #f59e0b; }
        """)
        dub_ctrl.addWidget(self.chk_auto_dub)

        lbl_giong = QLabel("  Giọng:")
        lbl_giong.setStyleSheet("color: #fde68a; font-weight: bold;")
        dub_ctrl.addWidget(lbl_giong)
        self.cmb_dub_voice = QComboBox()
        self._load_dub_voices()
        self.cmb_dub_voice.setToolTip("Giọng lồng tiếng (đọc từ Voice.json)")
        self.cmb_dub_voice.setStyleSheet("QComboBox { background:#1f2937; color:#fde68a; border:1px solid #f59e0b; border-radius:6px; padding:4px 8px; } QComboBox QAbstractItemView { background:#1e293b; color:#fde68a; }")
        dub_ctrl.addWidget(self.cmb_dub_voice)

        lbl_toc_do = QLabel("Tốc độ:")
        lbl_toc_do.setStyleSheet("color: #fde68a; font-weight: bold;")
        dub_ctrl.addWidget(lbl_toc_do)
        self.spn_dub_rate = QDoubleSpinBox()
        self.spn_dub_rate.setRange(0.5, 2.0); self.spn_dub_rate.setSingleStep(0.1)
        self.spn_dub_rate.setValue(1.0); self.spn_dub_rate.setDecimals(1)
        self.spn_dub_rate.setFixedWidth(65)
        self.spn_dub_rate.setStyleSheet("QDoubleSpinBox { background:#1f2937; color:#fde68a; border:1px solid #f59e0b; border-radius:6px; padding:3px; }")
        dub_ctrl.addWidget(self.spn_dub_rate)

        lbl_luong = QLabel("Luồng:")
        lbl_luong.setStyleSheet("color: #fde68a; font-weight: bold;")
        dub_ctrl.addWidget(lbl_luong)
        self.spn_tts_workers = QSpinBox()
        # Edge TTS chịu được nhiều luồng song song hơn CapCut (giới hạn API riêng),
        # nhưng để 1 ô chung cho gọn - khách tự cân chỉnh theo engine đang chọn.
        self.spn_tts_workers.setRange(1, 100)
        self.spn_tts_workers.setSingleStep(5)
        try:
            _saved_workers = int(self.settings.value("tts_workers", 10))
        except Exception:
            _saved_workers = 10
        self.spn_tts_workers.setValue(_saved_workers)
        self.spn_tts_workers.setFixedWidth(65)
        self.spn_tts_workers.setToolTip(
            "Số luồng lồng tiếng chạy song song.\n"
            "Edge TTS: có thể để cao (20-50) vì miễn phí, ít giới hạn.\n"
            "CapCut: nên để thấp (4-10) để tránh lỗi giới hạn API."
        )
        self.spn_tts_workers.setStyleSheet("QSpinBox { background:#1f2937; color:#fde68a; border:1px solid #f59e0b; border-radius:6px; padding:3px; }")
        self.spn_tts_workers.valueChanged.connect(
            lambda v: self.settings.setValue("tts_workers", v)
        )
        dub_ctrl.addWidget(self.spn_tts_workers)

        # Ô "Luồng" CHỈ áp dụng cho Edge TTS - giọng CapCut luôn khóa cứng 4
        # luồng trong code, nên làm mờ ô này khi khách chọn giọng CapCut để
        # tránh hiểu nhầm là chỉnh được cho cả CapCut.
        self.cmb_dub_voice.currentTextChanged.connect(self._on_dub_voice_changed)
        self._on_dub_voice_changed(self.cmb_dub_voice.currentText())

        self.btn_dub_now = QPushButton("🎙 Lồng tiếng ngay")
        self.btn_dub_now.setEnabled(False)
        self.btn_dub_now.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_dub_now.setStyleSheet("QPushButton { padding: 8px 18px; background-color: #b45309; color: white; border-radius: 8px; font-weight: bold; font-size: 13px; border: none; } QPushButton:hover { background-color: #92400e; } QPushButton:disabled { background-color: #374151; color: #64748b; }")
        self.btn_dub_now.clicked.connect(self._run_dub_on_downloaded)
        dub_ctrl.addWidget(self.btn_dub_now)

        lbl_tieng_goc2 = QLabel("🔊 Tiếng gốc:")
        lbl_tieng_goc2.setStyleSheet("color: #fde68a; font-weight: bold;")
        dub_ctrl2.addWidget(lbl_tieng_goc2)
        self.spn_orig_volume = QSpinBox()
        self.spn_orig_volume.setRange(0, 100)
        self.spn_orig_volume.setSingleStep(5)
        self.spn_orig_volume.setValue(15)   # mặc định 15% như logic cũ
        self.spn_orig_volume.setSuffix("%")
        self.spn_orig_volume.setFixedWidth(70)
        self.spn_orig_volume.setToolTip(
            "Âm lượng tiếng gốc (tiếng Trung) khi LỒNG TIẾNG và KHÔNG tắt tiếng gốc.\n"
            "Trộn tiếng gốc nền dưới tiếng Việt lồng vào.\n"
            "• 15% = tiếng gốc nhỏ phía sau (mặc định)\n"
            "• 0% = gần như câm tiếng gốc\n"
            "(Không ảnh hưởng khi ghép video thường hoặc khi đã tick 'Tắt tiếng gốc'.)")
        self.spn_orig_volume.setStyleSheet("QSpinBox { background:#1f2937; color:#fde68a; border:1px solid #f59e0b; border-radius:6px; padding:3px; }")
        dub_ctrl2.addWidget(self.spn_orig_volume)

        self.chk_mute_original = QCheckBox("🔇 Tắt tiếng gốc")
        self.chk_mute_original.setChecked(True)
        self.chk_mute_original.setToolTip("Bật = chỉ còn tiếng lồng (bỏ hẳn tiếng Trung gốc)")
        self.chk_mute_original.setStyleSheet("""
            QCheckBox { color:#fde68a; font-weight:bold; font-size:13px; padding:2px; }
            QCheckBox::indicator { width:18px; height:18px; border:2px solid #f59e0b; border-radius:4px; background:#1f2937; }
            QCheckBox::indicator:checked { background:#f59e0b; }
        """)
        dub_ctrl2.addWidget(self.chk_mute_original)

        self.chk_remove_bgm = QCheckBox("🎵 Tách nhạc nền")
        self.chk_remove_bgm.setChecked(False)
        self.chk_remove_bgm.setToolTip(
            "Bật = dùng Demucs AI tách nhạc nền ra khỏi audio gốc trước khi mix.\n"
            "Giữ lại: thoại tiếng Trung + hiệu ứng âm thanh (SFX) + tiếng Việt lồng.\n"
            "Bỏ đi: nhạc nền (soundtrack/BGM).\n"
            "⚠️ Cần cài: pip install demucs\n"
            "⚠️ Lần đầu chạy sẽ tải model ~300MB, xử lý lâu hơn bình thường.")
        self.chk_remove_bgm.setStyleSheet("""
            QCheckBox { color: #6ee7b7; font-weight: bold; font-size: 13px; padding: 2px; }
            QCheckBox::indicator { width: 18px; height: 18px; border: 2px solid #10b981;
                border-radius: 4px; background: #1f2937; }
            QCheckBox::indicator:checked { background: #10b981; }
        """)
        self.chk_remove_bgm.stateChanged.connect(self._on_chk_remove_bgm_changed)
        dub_ctrl2.addWidget(self.chk_remove_bgm)

        lbl_tach_ss = QLabel("Tách song song:")
        lbl_tach_ss.setStyleSheet("color: #6ee7b7; font-weight: bold;")
        dub_ctrl2.addWidget(lbl_tach_ss)
        self.spn_bgm_parallel = QSpinBox()
        self.spn_bgm_parallel.setRange(1, 5)
        try:
            _saved_bgm_parallel = int(self.settings.value("bgm_parallel", 1))
        except Exception:
            _saved_bgm_parallel = 1
        self.spn_bgm_parallel.setValue(max(1, min(5, _saved_bgm_parallel)))
        _GLOBAL_DEMUCS_GATE.limit = self.spn_bgm_parallel.value()
        self.spn_bgm_parallel.setFixedWidth(50)
        self.spn_bgm_parallel.setToolTip(
            "Số video tách nhạc nền (Demucs) chạy song song cùng lúc.\n"
            "1 = an toàn nhất (mặc định).\n"
            "⚠️ Chọn cao hơn với GPU có thể tràn VRAM tùy máy - tự cân nhắc theo cấu hình card."
        )
        self.spn_bgm_parallel.setStyleSheet("QSpinBox { background:#1f2937; color:#6ee7b7; border:1px solid #10b981; border-radius:6px; padding:3px; }")
        def _on_bgm_parallel_changed(v):
            _GLOBAL_DEMUCS_GATE.limit = v
            self.settings.setValue("bgm_parallel", v)
        self.spn_bgm_parallel.valueChanged.connect(_on_bgm_parallel_changed)
        dub_ctrl2.addWidget(self.spn_bgm_parallel)

        self.chk_del_original = QCheckBox("🗑 Xóa file gốc")
        self.chk_del_original.setChecked(False) 
        self.chk_del_original.setToolTip("Bật = sau khi xong, xóa video gốc + sub tiếng Trung,\nchỉ giữ bản lồng tiếng + phụ đề Việt (đổi tên sạch: Tap_01.mp4 + Tap_01.srt)")
        self.chk_del_original.setStyleSheet("""
            QCheckBox { color:#fca5a5; font-weight:bold; font-size:13px; padding:2px; }
            QCheckBox::indicator { width:18px; height:18px; border:2px solid #ef4444; border-radius:4px; background:#1f2937; }
            QCheckBox::indicator:checked { background:#ef4444; }
        """)
        dub_ctrl2.addWidget(self.chk_del_original)
        dub_ctrl2.addStretch()

        dub_ctrl.addStretch()
        detail_layout.addLayout(dub_ctrl)
        detail_layout.addLayout(dub_ctrl2)

        # ── HÀNG TÁCH NHẠC NỀN ĐỘC LẬP ─────────────────────────────
        bgm_ctrl = QHBoxLayout(); bgm_ctrl.setSpacing(8)

        self.btn_bgm_only = QPushButton("🎵 Tách nhạc nền ngay")
        self.btn_bgm_only.setEnabled(False)
        self.btn_bgm_only.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_bgm_only.setToolTip(
            "Chỉ tách nhạc nền khỏi các video đã tải, KHÔNG lồng tiếng.\n"
            "Giữ lại: thoại tiếng Trung + SFX (đã ghép sẵn vào video).\n"
            "Output: Tap_01_vocals.mp4 cạnh file gốc (hoặc ghi đè nếu tick 'Xóa file gốc').\n"
            "Tự động dùng CPU ở chế độ an toàn (30%) để tránh văng máy.")
        self.btn_bgm_only.setStyleSheet(
            "QPushButton { padding: 8px 18px; background-color: #065f46; color: #6ee7b7; "
            "border-radius: 8px; font-weight: bold; font-size: 13px; border: 2px solid #10b981; } "
            "QPushButton:hover { background-color: #047857; } "
            "QPushButton:disabled { background-color: #374151; color: #64748b; border-color: #374151; }")
        self.btn_bgm_only.clicked.connect(self._run_bgm_only)
        bgm_ctrl.addWidget(self.btn_bgm_only)

        self.chk_bgm_del_original = QCheckBox("🗑 Xóa file gốc sau tách")
        self.chk_bgm_del_original.setChecked(False)
        self.chk_bgm_del_original.setToolTip("Xóa video gốc, đổi tên _vocals.mp4 → tên gốc")
        self.chk_bgm_del_original.setStyleSheet("""
            QCheckBox { color:#fca5a5; font-weight:bold; font-size:12px; padding:2px; }
            QCheckBox::indicator { width:16px; height:16px; border:2px solid #ef4444;
                border-radius:4px; background:#1f2937; }
            QCheckBox::indicator:checked { background:#ef4444; }
        """)
        bgm_ctrl.addWidget(self.chk_bgm_del_original)

        self.chk_use_gpu = QCheckBox("🚀 Tách bằng Card Đồ Họa (GPU)")
        self.chk_use_gpu.setEnabled(False)
        self.chk_use_gpu.setToolTip("Bật 'Tách nhạc nền' để tự kiểm tra GPU. Máy không có GPU NVIDIA sẽ dùng CPU (chậm hơn nhưng vẫn chạy).")
        self.chk_use_gpu.setStyleSheet("""
            QCheckBox { color: #38bdf8; font-weight: bold; font-size: 12px; padding: 2px; }
            QCheckBox::indicator { width: 16px; height: 16px; border: 2px solid #0284c7; border-radius: 4px; background: #1f2937; }
            QCheckBox::indicator:checked { background: #0284c7; }
        """)
        self.chk_use_gpu.clicked.connect(self._on_gpu_checkbox_clicked)
        bgm_ctrl.addWidget(self.chk_use_gpu)

        bgm_ctrl.addStretch()
        detail_layout.addLayout(bgm_ctrl)
        
        # Tự động detect GPU khi khởi động
        # (Đã bỏ dò GPU lúc mở app — 'import torch' rất nặng làm lag cả máy.
        #  Giờ chỉ dò khi khách bật 'Tách nhạc nền', và dò nhẹ bằng nvidia-smi.)

        self.txt_stt_log = QTextEdit()
        self.txt_stt_log.setReadOnly(True); self.txt_stt_log.setFixedHeight(110)
        self.txt_stt_log.setStyleSheet("QTextEdit { background: #0a0c14; color: #a3e635; font-family: Consolas; font-size: 9pt; border: 1px solid #374151; border-radius: 6px; padding: 4px; }")
        self.txt_stt_log.hide()
        detail_layout.addWidget(self.txt_stt_log)
        detail_layout.addLayout(bottom_layout)
        self.content_stack.addWidget(self.page_detail)

        self.page_pick = QWidget()
        pick_layout = QVBoxLayout(self.page_pick)
        pick_layout.setContentsMargins(0, 0, 0, 0); pick_layout.setSpacing(8)
        ptop = QHBoxLayout()
        self.pick_btn_back = QPushButton("← Quay lại")
        self.pick_btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pick_btn_back.setStyleSheet("QPushButton { padding:8px 16px; background:#1e293b; color:#e2e8f0; border:1px solid #334155; border-radius:8px; font-weight:bold; } QPushButton:hover { background:#334155; }")
        self.pick_btn_back.clicked.connect(lambda: self.content_stack.setCurrentWidget(self.page_grid))
        ptop.addWidget(self.pick_btn_back)
        ttl = QLabel("✅ Tự Chọn Phim (2.500đ/bộ · chọn theo mức 10 bộ)")
        ttl.setStyleSheet("color:#e2e8f0; font-size:16px; font-weight:bold; padding:0 8px;")
        ptop.addWidget(ttl)
        self.pick_search = QLineEdit(); self.pick_search.setPlaceholderText("🔍 Tìm tên phim...")
        self.pick_search.setStyleSheet("QLineEdit { padding:9px; background:#1e293b; color:#e2e8f0; border:1px solid #334155; border-radius:8px; }")
        ptop.addWidget(self.pick_search, 1)
        pick_layout.addLayout(ptop)
        self._pick_list = QListWidget()
        self._pick_list.setViewMode(QListWidget.ViewMode.IconMode)
        self._pick_list.setIconSize(QSize(160, 220))
        self._pick_list.setGridSize(QSize(180, 285))
        self._pick_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._pick_list.setMovement(QListWidget.Movement.Static)
        self._pick_list.setDragEnabled(False)
        self._pick_list.setDragDropMode(QListWidget.DragDropMode.NoDragDrop)
        self._pick_list.setWordWrap(True)
        self._pick_list.setStyleSheet("QListWidget { background:transparent; border:none; outline:none; } QListWidget::item { color:#e2e8f0; font-weight:bold; font-size:13px; padding-top:5px; border-radius:10px; border:3px solid transparent; } QListWidget::item:hover { background:#1e293b; } QListWidget::item:selected { background:#0e7490; border:3px solid #22d3ee; color:#ffffff; } QScrollBar:vertical { border:none; background:#111827; width:8px; } QScrollBar::handle:vertical { background:#374151; border-radius:4px; min-height:20px; }")
        self._pick_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self._pick_list.itemClicked.connect(self._on_pick_item_clicked)
        pick_layout.addWidget(self._pick_list, 1)
        pbot = QHBoxLayout()
        self.pick_btn_prev = QPushButton("‹ Trước"); self.pick_btn_next = QPushButton("Sau ›")
        for b in (self.pick_btn_prev, self.pick_btn_next):
            b.setStyleSheet("QPushButton { padding:6px 14px; background:#1e293b; color:#e2e8f0; border:1px solid #334155; border-radius:6px; }")
        self.pick_page_lbl = QLabel("Trang 1")
        self.pick_page_lbl.setStyleSheet("color:#94a3b8; font-weight:bold;")
        pbot.addWidget(self.pick_btn_prev); pbot.addWidget(self.pick_page_lbl); pbot.addWidget(self.pick_btn_next)
        pbot.addStretch()
        self._pick_total_lbl = QLabel("Chưa chọn bộ nào")
        self._pick_total_lbl.setStyleSheet("color:#f59e0b; font-size:16px; font-weight:bold; padding:0 12px;")
        pbot.addWidget(self._pick_total_lbl)
        self._pick_ok = QPushButton("🛒 Mua ngay")
        self._pick_ok.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pick_ok.setStyleSheet("QPushButton { padding:10px 24px; background:#16a34a; color:white; font-weight:bold; border:none; border-radius:8px; font-size:14px; } QPushButton:hover { background:#15803d; }")
        self._pick_ok.clicked.connect(self._bulk_pick_submit_page)
        pbot.addWidget(self._pick_ok)
        pick_layout.addLayout(pbot)
        self.content_stack.addWidget(self.page_pick)
        self._pick_search_timer = QTimer(self); self._pick_search_timer.setSingleShot(True)
        self._pick_search_timer.timeout.connect(self._do_pick_search)
        self.pick_search.textChanged.connect(lambda: self._pick_search_timer.start(300))
        self.pick_btn_prev.clicked.connect(lambda: self._pick_change_page(-1))
        self.pick_btn_next.clicked.connect(lambda: self._pick_change_page(1))

        self._render_history_sidebar()

        self._history_timer = QTimer(self)
        self._history_timer.timeout.connect(self._render_history_sidebar)
        self._history_timer.start(60000)

        # Mọi widget cấu hình đã dựng xong -> giờ mới an toàn để:
        # 1) chụp cấu hình mặc định làm config cho tab đầu tiên
        # 2) nối signal đổi tab (lưu/khôi phục cấu hình theo tab)
        try:
            if hasattr(self, 'table') and self.table in self._tab_data:
                self._tab_data[self.table]["config"] = self._snapshot_config()
            self.series_tabs.currentChanged.connect(self._on_series_tab_changed)
        except Exception as _e:
            print(f"[WARN] Nối signal tab lỗi: {_e}")

    def _on_chk_remove_bgm_changed(self, state):
        """Khi tick vào ô Tách nhạc nền: check demucs, hỏi cài nếu chưa có."""
        if state == 2:  # Checked
            # Dò GPU LƯỜI: chỉ dò lần đầu khi khách thật sự bật tách nhạc nền,
            # thay vì dò lúc mở app (import torch rất nặng, làm lag cả máy).
            if not getattr(self, "_gpu_detected_once", False):
                self._gpu_detected_once = True
                self._detect_bgm_device()

            if not _DEMUCS_MANAGER_OK:
                # demucs_manager không load được → cho tick bình thường
                return
            # Bỏ tick tạm, hiện loading cursor, check trong thread riêng
            self.chk_remove_bgm.blockSignals(True)
            self.chk_remove_bgm.setChecked(False)
            self.chk_remove_bgm.blockSignals(False)
            self.chk_remove_bgm.setEnabled(False)
            self.chk_remove_bgm.setText("🎵 Đang kiểm tra...")

            class _CheckThread(QThread):
                result = pyqtSignal(bool)
                def run(self):
                    from demucs_manager import is_demucs_ready
                    self.result.emit(is_demucs_ready())

            def _on_check_done(ready):
                self.chk_remove_bgm.setEnabled(True)
                self.chk_remove_bgm.setText("🎵 Tách nhạc nền")
                if ready:
                    # Đã cài rồi → tick luôn
                    self.chk_remove_bgm.blockSignals(True)
                    self.chk_remove_bgm.setChecked(True)
                    self.chk_remove_bgm.blockSignals(False)
                else:
                    # Chưa cài → hiện popup hỏi cài
                    def _after_install():
                        self.chk_remove_bgm.setChecked(True)
                    ensure_demucs_installed_ui(self, _after_install)

            self._bgm_check_thread = _CheckThread()
            self._bgm_check_thread.result.connect(_on_check_done)
            self._bgm_check_thread.start()

    # ── LOGIC DÒ GPU AN TOÀN QUA SUBPROCESS ─────────────────────────
    def _update_gpu_tooltip(self):
        """Cập nhật tooltip GPU sau khi detect xong."""
        if getattr(self, '_has_real_gpu', False):
            vram = getattr(self, '_gpu_vram_gb', 0)
            self.chk_use_gpu.setToolTip(
                f"✅ Phát hiện GPU NVIDIA ({vram:.1f}GB VRAM)\n"
                "GPU giúp tách nhanh gấp 10 lần so với CPU."
            )
        else:
            self.chk_use_gpu.setToolTip(
                "❌ Không phát hiện GPU NVIDIA (CUDA) trên máy này.\n"
                "Chương trình sẽ tự dùng CPU — chậm hơn nhưng vẫn hoạt động tốt."
            )

    def _on_gpu_checkbox_clicked(self, checked):
        if checked and not getattr(self, '_has_real_gpu', False):
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "Không có GPU NVIDIA",
                "Máy bạn không có Card Đồ Họa NVIDIA (CUDA) hợp lệ.\n\n"
                "✅ Đừng lo — chương trình sẽ tự động dùng CPU để tách nhạc,\n"
                "kết quả vẫn tốt, chỉ chậm hơn GPU một chút."
            )
            self.chk_use_gpu.setChecked(False)
            return
        if checked and getattr(self, '_has_real_gpu', False) and not getattr(self, '_gpu_is_good', False):
            vram = getattr(self, '_gpu_vram_gb', 0.0)
            reply = QMessageBox.warning(
                self, "Cảnh báo VRAM thấp",
                f"GPU của bạn chỉ có {vram:.1f}GB VRAM, thấp hơn mức khuyến nghị "
                f"({self.MIN_GPU_VRAM_GB_FOR_AUTO:.0f}GB) để chạy tách nhạc ổn định.\n\n"
                "Máy có thể bị treo, tràn bộ nhớ hoặc tự tắt nguồn giữa chừng.\n"
                "Bạn có chắc chắn muốn bật GPU?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                self.chk_use_gpu.setChecked(False)

    # Ngưỡng VRAM tối thiểu để coi là GPU "đủ xịn" cho phép tự bật GPU.
    # Dưới mức này (GPU tích hợp, card cũ VRAM thấp) sẽ dễ bị tràn bộ nhớ / quá tải
    # khi chạy Demucs -> ép về CPU (throttled) cho an toàn với máy khách.
    MIN_GPU_VRAM_GB_FOR_AUTO = 4.0

    @pyqtSlot()
    def _apply_gpu_detect_result(self):
        """Được gọi trên main thread sau khi detect GPU xong (qua invokeMethod).
        FIX: QTimer.singleShot từ background thread KHÔNG chạy trên main thread
        -> callback bị Qt bỏ qua lặng lẽ -> checkbox kẹt disabled mãi."""
        if not hasattr(self, 'chk_use_gpu'):
            return
        self.chk_use_gpu.setEnabled(True)
        self._update_gpu_tooltip()
        if getattr(self, '_gpu_is_good', False):
            self.chk_use_gpu.setChecked(True)
            self.chk_use_gpu.setText(
                f"🚀 Tách bằng GPU ({self._gpu_name}, {self._gpu_vram_gb:.1f}GB VRAM) - Nhanh hơn CPU ~10 lần"
            )
        elif getattr(self, '_has_real_gpu', False):
            self.chk_use_gpu.setChecked(False)
            self.chk_use_gpu.setText(
                f"⚠️ GPU yếu ({self._gpu_name}, {self._gpu_vram_gb:.1f}GB VRAM) - Có thể bật nhưng cẩn thận VRAM"
            )
        else:
            self.chk_use_gpu.setChecked(False)
            self.chk_use_gpu.setText("🖥 Tách bằng CPU (Không tìm thấy GPU NVIDIA)")

    def _detect_bgm_device(self):
        """Dò GPU ngầm bằng Subprocess để không làm sập App chính.
        FIX: dùng QMetaObject.invokeMethod thay QTimer.singleShot để update UI
        từ thread phụ an toàn - tránh checkbox kẹt disabled dù detect GPU thành công."""
        import threading, subprocess, sys

        def _check():
            self._has_real_gpu = False
            self._gpu_name = ""
            self._gpu_vram_gb = 0.0
            _debug_info = ""

            si = None
            if sys.platform == "win32":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            # BƯỚC 1 (nhẹ): nvidia-smi — chạy tích tắc, không nạp torch.
            # Không có NVIDIA thì DỪNG luôn, khỏi đụng 'import torch' (rất nặng).
            try:
                smi = subprocess.run(
                    ["nvidia-smi", "--query-gpu=name,memory.total",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, startupinfo=si, timeout=8)
                if smi.returncode == 0 and smi.stdout.strip():
                    line = smi.stdout.strip().splitlines()[0]
                    name, mem = [x.strip() for x in line.split(",")[:2]]
                    self._has_real_gpu = True
                    self._gpu_name = name
                    try:
                        self._gpu_vram_gb = float(mem) / 1024.0  # MiB -> GiB
                    except Exception:
                        self._gpu_vram_gb = 0.0
                else:
                    # nvidia-smi có nhưng không GPU NVIDIA -> chắc chắn không có
                    self._has_real_gpu = False
            except FileNotFoundError:
                # Không có nvidia-smi = gần như chắc chắn không phải máy NVIDIA.
                # Vẫn thử torch 1 lần phòng driver lạ, nhưng đa số sẽ ra 'không GPU'.
                self._has_real_gpu = False
            except Exception as e:
                _debug_info += f"nvidia-smi lỗi: {e}\n"

            # BƯỚC 2 (nặng, chỉ khi cần): chỉ đụng torch khi nvidia-smi báo CÓ
            # GPU nhưng chưa lấy được dung lượng VRAM (hiếm). Không có NVIDIA thì
            # KHÔNG bao giờ nạp torch -> mở app không còn lag.
            need_torch = self._has_real_gpu and self._gpu_vram_gb <= 0.0
            if need_torch:
                try:
                    probe = (
                        "import torch\n"
                        "if torch.cuda.is_available():\n"
                        "    p = torch.cuda.get_device_properties(0)\n"
                        "    print('1|%s|%.2f' % (p.name, p.total_memory / (1024**3)))\n"
                        "else:\n"
                        "    print('0||0')\n"
                    )
                    _probe_py = _resolve_demucs_python()
                    env = _clean_subprocess_env(_probe_py)
                    cmd = [_probe_py, "-c", probe]
                    res = subprocess.run(cmd, env=env, capture_output=True, text=True, startupinfo=si, timeout=30)
                    parts = res.stdout.strip().split('|')
                    if len(parts) == 3 and parts[0] == '1':
                        self._has_real_gpu = True
                        self._gpu_name = parts[1]
                        self._gpu_vram_gb = float(parts[2])
                    else:
                        _debug_info += (
                            f"probe_py: {_probe_py}\nreturncode: {res.returncode}\n"
                            f"stdout: {res.stdout!r}\nstderr: {res.stderr[-1500:]!r}\n"
                        )
                except Exception as e:
                    _debug_info += f"Exception torch detect: {e}\n"

            try:
                if _debug_info:
                    appdata = os.getenv('APPDATA', '')
                    if appdata:
                        log_path = os.path.join(appdata, 'BoomStudio', 'gpu_detect_debug.log')
                        os.makedirs(os.path.dirname(log_path), exist_ok=True)
                        with open(log_path, 'w', encoding='utf-8') as f:
                            f.write(_debug_info)
            except Exception:
                pass
            self._gpu_is_good = self._has_real_gpu and self._gpu_vram_gb >= self.MIN_GPU_VRAM_GB_FOR_AUTO
            QMetaObject.invokeMethod(self, "_apply_gpu_detect_result",
                                     Qt.ConnectionType.QueuedConnection)

        threading.Thread(target=_check, daemon=True).start()


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
            try: self.hot_thread.finished_signal.disconnect()
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
                self.history_cover_thread = HistoryCoverThread(missing_covers, covers_dir, self.auth_token)
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
        self.hot_thread.finished_signal.connect(self._on_hot_movies_finished)
        self._keep_thread_alive(self.hot_thread)
        self.hot_thread.start()

    def _on_hot_movies_finished(self):
        # Cùng lý do như trên: chỉ xử lý nếu đúng là luồng hiện tại báo xong,
        # bỏ qua tín hiệu "finished" rớt muộn từ 1 luồng đã bị thay thế.
        if self.sender() is not getattr(self, 'hot_thread', None):
            return
        self.loading_bar.hide()
        # Nếu không có phim nào load được (server lỗi/timeout/danh mục rỗng),
        # dòng "Đang lọc phim..." sẽ bị kẹt mãi mãi nếu không xử lý ở đây.
        if getattr(self, 'is_first_movie', False):
            self.hot_list.clear()
            self.is_first_movie = False
            empty = QListWidgetItem("😢 Không tải được danh sách phim.\nCó thể do mất mạng hoặc server đang bận.\nHãy thử bấm lại danh mục này.")
            empty.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.hot_list.addItem(empty)

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
            self.sidebar_cover_thread = HistoryCoverThread(missing, covers_dir, self.auth_token)
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
        self._from_shelf = True  # phim từ lịch sử: cho phép cả khách test
        self._scan()

    def _render_single_hot_movie(self, m):
        # Chặn tín hiệu "rớt muộn" từ 1 luồng tải CŨ (VD: bấm danh mục 2 lần liên
        # tiếp trong lúc lần 1 chưa kịp trả lời) - chỉ nhận tín hiệu từ luồng
        # đang là self.hot_thread hiện tại, bỏ qua mọi tín hiệu từ luồng đã cũ.
        if self.sender() is not getattr(self, 'hot_thread', None):
            return
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
        self._from_shelf = True  # phim từ kho/kết quả search đã lọc: cho phép cả khách test
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

    def _start_batch_download(self):
        """Tải lần lượt TẤT CẢ các tab: xong bộ này tự sang bộ kế tiếp."""
        if getattr(self, '_batch_running', False):
            QMessageBox.information(self, "Đang chạy", "Đang tải hàng loạt, vui lòng đợi.")
            return
        # gom các tab đã quét được (có series_id + có tập trong bảng)
        self._save_current_tab_state()
        tabs_to_run = []
        for i in range(self.series_tabs.count()):
            w = self.series_tabs.widget(i)
            d = self._tab_data.get(w, {})
            if d.get("series_id") and w.rowCount() > 0:
                tabs_to_run.append(i)
        if not tabs_to_run:
            QMessageBox.information(self, "Chưa có bộ nào",
                "Hãy quét ít nhất 1 bộ (dán link rồi bấm Quét) trước khi tải hàng loạt.")
            return
        self._batch_tab_queue = tabs_to_run
        self._batch_total = len(tabs_to_run)
        self._batch_done = 0
        self._batch_running = True
        self.btn_batch_dl.setEnabled(False)
        self.btn_batch_dl.setText(f"📥 Đang tải (0/{self._batch_total})")
        if hasattr(self, 'txt_stt_log'):
            self.txt_stt_log.show()
            self.txt_stt_log.append(f"📋 Tải hàng loạt {self._batch_total} bộ (lần lượt)...")
        self._batch_run_next_tab()

    def _batch_run_next_tab(self):
        if not self._batch_tab_queue:
            self._batch_running = False
            self.btn_batch_dl.setEnabled(True)
            self.btn_batch_dl.setText("📥 Tải hàng loạt")
            if hasattr(self, 'txt_stt_log'):
                self.txt_stt_log.append(f"🎉 Đã tải xong toàn bộ {self._batch_total} bộ!")
            QMessageBox.information(self, "Xong", f"Đã tải xong {self._batch_total} bộ trong hàng đợi!")
            return
        idx = self._batch_tab_queue.pop(0)
        self.btn_batch_dl.setText(f"📥 Đang tải ({self._batch_done}/{self._batch_total})")
        # chuyển sang tab đó (kích hoạt _on_series_tab_changed -> self.table + config đúng)
        self.series_tabs.setCurrentIndex(idx)
        # nạp đúng cấu hình riêng của tab này (phòng khi signal chưa kịp chạy)
        d = self._tab_data.get(self.table, {})
        if d.get("config"):
            self._apply_config(d["config"])
        if hasattr(self, 'txt_stt_log'):
            self.txt_stt_log.append(f"▶️ [{self._batch_done + 1}/{self._batch_total}] Tải bộ: {self.current_title}")
        # chọn tất cả tập rồi tải
        t = self.table
        for r in range(t.rowCount()):
            it = t.item(r, 0)
            if it and (it.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                it.setCheckState(Qt.CheckState.Checked)
        QTimer.singleShot(400, self._download_selected)

    def _batch_tab_finished(self):
        """Gọi khi 1 bộ (1 tab) đã xử lý xong -> sang tab kế tiếp.
        Chống gọi trùng bằng cờ _batch_advancing."""
        if not getattr(self, '_batch_running', False):
            return
        if getattr(self, '_batch_advancing', False):
            return
        self._batch_advancing = True
        self._batch_done += 1
        if hasattr(self, 'txt_stt_log'):
            self.txt_stt_log.append(f"✔️ Xong bộ {self._batch_done}/{self._batch_total}. Sang bộ kế...")
        def _go():
            self._batch_advancing = False
            self._batch_run_next_tab()
        QTimer.singleShot(2000, _go)

    def _config_widgets(self):
        """Danh sách (tên, widget, kiểu) các widget cấu hình cần nhớ theo tab.
        kiểu: 'chk' (checkbox), 'cbo' (combo - lưu index), 'int'/'dbl' (spin)."""
        specs = [
            ("chk_auto_cover", "chk"), ("chk_auto_stt", "chk"), ("chk_do_translate", "chk"),
            ("chk_auto_dub", "chk"), ("chk_remove_bgm", "chk"), ("chk_bgm_del_original", "chk"),
            ("chk_del_original", "chk"), ("chk_mute_original", "chk"), ("chk_show_browser", "chk"),
            ("chk_use_gpu", "chk"),
            ("cb_translate_engine", "cbo"), ("merge_mode_combo", "cbo"), ("cmb_dub_voice", "cbo"),
            ("chunk_spinbox", "int"), ("spn_bgm_parallel", "int"), ("spn_orig_volume", "int"),
            ("spn_trans_workers", "int"), ("spn_tts_workers", "int"),
            ("spn_dub_rate", "dbl"),
        ]
        out = []
        for name, kind in specs:
            w = getattr(self, name, None)
            if w is not None:
                out.append((name, w, kind))
        return out

    def _snapshot_config(self):
        """Chụp cấu hình hiện tại của khu điều khiển thành dict."""
        cfg = {}
        for name, w, kind in self._config_widgets():
            try:
                if kind == "chk":
                    cfg[name] = w.isChecked()
                elif kind == "cbo":
                    cfg[name] = w.currentIndex()
                else:
                    cfg[name] = w.value()
            except Exception:
                pass
        return cfg

    def _apply_config(self, cfg):
        """Áp 1 dict cấu hình lên khu điều khiển (dùng khi đổi tab / tải hàng loạt)."""
        if not cfg:
            return
        for name, w, kind in self._config_widgets():
            if name not in cfg:
                continue
            try:
                w.blockSignals(True)
                if kind == "chk":
                    w.setChecked(bool(cfg[name]))
                elif kind == "cbo":
                    w.setCurrentIndex(int(cfg[name]))
                else:
                    w.setValue(cfg[name])
            except Exception:
                pass
            finally:
                w.blockSignals(False)

    def _save_current_tab_config(self):
        """Lưu cấu hình hiện tại vào tab đang mở."""
        t = getattr(self, 'table', None)
        if t is not None and t in getattr(self, '_tab_data', {}):
            self._tab_data[t]["config"] = self._snapshot_config()

    def _sync_config_to_all_tabs(self):
        """Nút Đồng bộ: copy cấu hình tab hiện tại sang TẤT CẢ các tab khác."""
        cur = self._snapshot_config()
        n = 0
        for i in range(self.series_tabs.count()):
            w = self.series_tabs.widget(i)
            if w in self._tab_data:
                self._tab_data[w]["config"] = dict(cur)
                n += 1
        if hasattr(self, 'txt_stt_log'):
            self.txt_stt_log.show()
            self.txt_stt_log.append(f"🔄 Đã đồng bộ cấu hình hiện tại sang {n} tab.")
        QMessageBox.information(self, "Đã đồng bộ",
            f"Đã áp cấu hình của tab hiện tại cho tất cả {n} bộ.")

    def _dl_table(self):
        """Trả về bảng của bộ ĐANG TẢI (nếu đang tải) để cập nhật trạng thái
        đúng bộ, kể cả khi người dùng đã chuyển sang xem tab khác. Nếu không
        có phiên tải nào, trả về bảng đang xem."""
        t = getattr(self, '_active_dl_table', None)
        if t is not None:
            try:
                # đảm bảo bảng còn tồn tại trong các tab
                if t in getattr(self, '_tab_data', {}):
                    return t
            except Exception:
                pass
        return self.table

    def _make_episode_table(self):
        """Tạo 1 bảng chọn-tập chuẩn (dùng cho mỗi tab bộ phim)."""
        t = QTableWidget(); t.setColumnCount(4)
        t.setHorizontalHeaderLabels(["Chọn Tập", "", "Tên File", "Trạng Thái Link"])
        t.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        t.setColumnWidth(0, 90); t.setColumnWidth(1, 50); t.setColumnWidth(3, 170)
        t.verticalHeader().setVisible(False); t.setShowGrid(False); t.setAlternatingRowColors(True)
        t.setFocusPolicy(Qt.FocusPolicy.NoFocus); t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        t.setIconSize(QSize(40, 50))
        t.setStyleSheet("QTableWidget { background-color: #111827; alternate-background-color: #1f2937; color: #e2e8f0; border: none; outline: none; font-size: 13px; } QHeaderView::section { background-color: #0f172a; color: #94a3b8; padding: 12px; font-weight: bold; border: none; border-bottom: 1px solid #374151; } QTableWidget::item { padding: 6px; border-bottom: 1px solid transparent; } QTableWidget::item:hover { background-color: #334155; } QTableWidget::indicator { width: 18px; height: 18px; border: 2px solid #475569; border-radius: 4px; } QTableWidget::indicator:checked { background-color: #10b981; border-color: #10b981; } QScrollBar:vertical { border: none; background: #111827; width: 8px; margin: 0px; } QScrollBar::handle:vertical { background: #374151; border-radius: 4px; min-height: 20px; } QScrollBar::handle:vertical:hover { background: #4b5563; }")
        return t

    def _save_current_tab_state(self):
        """Lưu series_id/title/episodes hiện tại vào tab đang mở (trước khi đổi tab)."""
        t = getattr(self, 'table', None)
        if t is None or t not in getattr(self, '_tab_data', {}):
            return
        self._tab_data[t].update({
            "series_id": getattr(self, 'current_series_id', ''),
            "title": getattr(self, 'current_title', ''),
            "episodes": getattr(self, 'current_episodes', []),
            "cover_url": getattr(self, 'current_cover_url', ''),
            "total_eps": getattr(self, 'current_total_eps', 0),
            "job_id": getattr(self, 'current_job_id', None),
        })

    def _on_series_tab_changed(self, index):
        """Khi người dùng chuyển tab: trỏ self.table sang bảng của tab mới và
        khôi phục series_id/title/episodes + CẤU HÌNH của tab đó."""
        # lưu tab cũ trước (state + config)
        self._save_current_tab_state()
        if hasattr(self, '_config_widgets'):
            self._save_current_tab_config()
        w = self.series_tabs.widget(index)
        if w is None:
            return
        self.table = w
        d = self._tab_data.get(w, {})
        self.current_series_id = d.get("series_id", "")
        self.current_title = d.get("title", "")
        self.current_episodes = d.get("episodes", [])
        self.current_cover_url = d.get("cover_url", "")
        self.current_total_eps = d.get("total_eps", 0)
        self.current_job_id = d.get("job_id", None)
        # nạp cấu hình riêng của tab này (nếu đã lưu)
        if hasattr(self, '_apply_config') and d.get("config"):
            self._apply_config(d["config"])

    def _close_series_tab(self, index):
        """Đóng 1 tab bộ phim. Luôn giữ ít nhất 1 tab."""
        if self.series_tabs.count() <= 1:
            QMessageBox.information(self, "Không thể đóng", "Phải còn ít nhất 1 tab.")
            return
        w = self.series_tabs.widget(index)
        if w in self._tab_data:
            del self._tab_data[w]
        self.series_tabs.removeTab(index)
        # cập nhật self.table theo tab hiện tại
        self._on_series_tab_changed(self.series_tabs.currentIndex())

    def _new_series_tab(self, title="Phim mới"):
        """Tạo 1 tab bộ phim mới (bảng rỗng) và chuyển sang nó."""
        self._save_current_tab_state()
        if hasattr(self, '_config_widgets'):
            self._save_current_tab_config()
        t = self._make_episode_table()
        # tab mới thừa hưởng cấu hình hiện tại làm mặc định (chỉnh riêng sau)
        init_cfg = self._snapshot_config() if hasattr(self, '_snapshot_config') else {}
        self._tab_data[t] = {"series_id": "", "title": title, "episodes": [], "config": init_cfg}
        idx = self.series_tabs.addTab(t, title)
        self.series_tabs.setCurrentIndex(idx)   # -> kích hoạt _on_series_tab_changed
        return t

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

        # Cờ: phim được chọn từ trong kho/lịch sử/kết quả search (đã lọc), luôn cho phép
        from_shelf = getattr(self, '_from_shelf', False)
        self._from_shelf = False  # reset ngay sau khi đọc

        # Khách chưa mở khóa VIP (test): chỉ được dùng phim có sẵn trong kho, không cho quét link phim ngoài
        if not from_shelf and not self._is_vip():
            # Hỏi lại server phòng khi admin vừa mở khóa
            self._refresh_vip_status()
        if not from_shelf and not self._is_vip():
            QMessageBox.information(
                self, "Tính năng chưa được mở khóa",
                "🔒 Tính năng này chưa được mở khóa.\n\n"
                "Vui lòng liên hệ Admin để được hỗ trợ."
            )
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
            try: self.hot_thread.finished_signal.disconnect()
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

        # Khách chưa VIP (test): chỉ hiển thị phim đã có trong kho (DB/R2), lọc bỏ phim nguồn web
        is_test = not self._is_vip()
        if is_test and results:
            results = [m for m in results if m.get("is_local")]

        if not results:
            if is_test:
                msg = "🔒 Tính năng này chưa được mở khóa.\nVui lòng liên hệ Admin để được hỗ trợ."
            else:
                msg = "❌ Không tìm thấy bộ phim nào phù hợp."
            empty_item = QListWidgetItem(msg)
            empty_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter); empty_item.setFlags(Qt.ItemFlag.NoItemFlags) 
            self.hot_list.addItem(empty_item); return

        self._search_missing_covers = []   
        for m in results:
            item = QListWidgetItem()
            title = m.get("title", "Không rõ tên")
            eps = m.get("total_episodes", 0)
            series_id = str(m.get("series_id", ""))
            downloaded_tag = ""
            if series_id in getattr(self, '_cached_history_ids', set()):
                downloaded_tag = "\n[💾 Đã tải]"
            
            item.setText(f"{title}\n({eps} Tập){downloaded_tag}")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setData(Qt.ItemDataRole.UserRole, f"https://hongguoduanju.com/detail?series_id={series_id}") 
            self.hot_list.addItem(item)
            row = self.hot_list.count() - 1

            co_bia = False
            if series_id:
                try:
                    cpath = os.path.join(self._get_covers_dir(), f"{series_id}.img")
                    if os.path.exists(cpath):
                        with open(cpath, 'rb') as f: img = f.read()
                        pm = QPixmap()
                        if pm.loadFromData(img) and not pm.isNull():
                            pm = pm.scaled(160, 220, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
                            item.setIcon(QIcon(pm)); co_bia = True
                except Exception: pass
            if not co_bia:
                cover_url = m.get("cover_url") or ""
                if series_id:
                        self._search_missing_covers.append((row, series_id, cover_url))

        if self._search_missing_covers:
            try:
                if getattr(self, 'search_cover_thread', None) and self.search_cover_thread.isRunning():
                    return
                self.search_cover_thread = HistoryCoverThread(self._search_missing_covers, self._get_covers_dir(), self.auth_token)
                self.search_cover_thread.cover_ready.connect(self._on_search_cover_ready)
                self._keep_thread_alive(self.search_cover_thread)
                self.search_cover_thread.start()
            except Exception: pass

    def _on_search_cover_ready(self, row, content):
        try:
            item = self.hot_list.item(row)
            if not item: return
            pm = QPixmap()
            if pm.loadFromData(content) and not pm.isNull():
                pm = pm.scaled(160, 220, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
                item.setIcon(QIcon(pm))
        except Exception: pass

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

        # Đặt tên tab hiện tại theo tên phim + lưu state vào tab đó
        try:
            if hasattr(self, 'series_tabs') and hasattr(self, 'table'):
                idx = self.series_tabs.indexOf(self.table)
                if idx >= 0:
                    short = (self.current_title or "Phim")[:14]
                    self.series_tabs.setTabText(idx, short)
                self._save_current_tab_state()
        except Exception:
            pass
        
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
            self.lbl_status.setText(f"✅ Tìm thấy phim! ({total_eps} tập) — Chọn tập và bấm Tải ngay.")
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
                link_item = QTableWidgetItem("☁️ Sẵn sàng tải")
                link_item.setForeground(QColor("#10b981"))
            else:
                link_item = QTableWidgetItem("☁️ Sẵn sàng tải")
                link_item.setForeground(QColor("#10b981"))
                
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

    def _toggle_pause_download(self):
        mgr = getattr(self, 'download_manager', None)
        if not mgr:
            return
        if mgr.is_paused():
            mgr.resume()
            self.btn_pause.setText("⏸ Tạm dừng")
            self.btn_pause.setStyleSheet("QPushButton { padding: 14px 24px; background-color: #f59e0b; color: white; border-radius: 8px; font-weight: bold; font-size: 15px; margin-top: 10px; border: none; } QPushButton:hover { background-color: #d97706; }")
            self.lbl_status.setText("▶️ Đã tiếp tục tải...")
        else:
            mgr.pause()
            self.btn_pause.setText("▶ Tiếp tục")
            self.btn_pause.setStyleSheet("QPushButton { padding: 14px 24px; background-color: #22c55e; color: white; border-radius: 8px; font-weight: bold; font-size: 15px; margin-top: 10px; border: none; } QPushButton:hover { background-color: #16a34a; }")
            self.lbl_status.setText("⏸ Đã tạm dừng — các tập đang tải sẽ chạy nốt, tập còn lại chờ tiếp tục.")

    def _download_selected(self):
        selected_eps_nums = []
        for i in range(self.table.rowCount()):
            item = self.table.item(i, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                selected_eps_nums.append(i + 1)

        if not selected_eps_nums:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn (tích) ít nhất 1 tập để tải!")
            return
            
        if getattr(self, 'current_quota', 20) <= 0:
            QMessageBox.warning(self, "Hết lượt tải", "Bạn đã dùng hết 20 lượt tải miễn phí hôm nay!\nHãy chờ qua 0h đêm để hồi lượt, hoặc dùng chức năng 'Mua Trọn Bộ'.")
            return

        if self.current_series_id in getattr(self, '_cached_history_ids', set()):
            reply = QMessageBox.question(self, "Cảnh báo tải trùng", "Bộ phim này bạn ĐÃ TẢI VỀ máy trước đó rồi!\nBạn có chắc chắn muốn TẢI LẠI và BỊ TRỪ 1 LƯỢT không?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No: return

        # KHÔNG trừ lượt ở đây nữa. Trước kia trừ ngay lúc bấm nút -> nếu server
        # trả "Lỗi API"/lỗi lấy link thì khách MẤT LƯỢT mà không nhận được link nào.
        # Giờ chỉ đánh dấu "phiên này chưa trừ", và chỉ trừ đúng 1 lần khi có ÍT
        # NHẤT 1 tập trả về link thật (xem _on_stream_link_ready).
        self._quota_charged_this_session = False

        # KHÓA bảng của bộ ĐANG TẢI: từ đây mọi cập nhật trạng thái tải ghi vào
        # đúng bảng này (self._active_dl_table), KHÔNG theo tab đang xem. Nhờ vậy
        # người dùng chuyển tab xem bộ khác trong lúc tải cũng không bị loạn.
        self._active_dl_table = self.table
        self._active_dl_series_id = self.current_series_id

        if getattr(self, 'chk_auto_cover', None) and self.chk_auto_cover.isChecked():
            self._save_cover_image(silent=True)

        if getattr(self, 'monitor_thread', None):
            self.monitor_thread.stop()
            self.monitor_thread = None

        self.btn_download.setEnabled(False)
        self.btn_download.setText("⏳ Đang khởi chạy tải...")
        self.btn_pause.setText("⏸ Tạm dừng")
        self.btn_pause.setEnabled(True)
        self.btn_pause.show()
        self.lbl_status.setText("⏳ Đang xử lý...")

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

        self.downloaded_file_paths = []
        for ep_num in selected_eps_nums:
            fname = f"Tap_{int(ep_num):02d}.mp4"
            self.downloaded_file_paths.append(os.path.join(final_save_path, fname))

        try: max_threads = int(self.threads_combo.currentText())
        except Exception: max_threads = 3

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
        
        if ep_num and url:
            # Chỉ trừ lượt khi THỰC SỰ có link tải về (tập đầu tiên hợp lệ). Trừ
            # đúng 1 lần cho cả phiên tải, dù bộ có nhiều tập. Nếu server lỗi và
            # không tập nào ra link, cờ này vẫn False -> khách không bị trừ oan.
            if not getattr(self, '_quota_charged_this_session', False):
                self._quota_charged_this_session = True
                self.quota_used_signal.emit()

            ep_item_data = {
                "episode_number": ep_num,
                "file_name": f"Tap_{int(ep_num):02d}.mp4",
                "drive_link": url,
                "aes_key": data.get("aes_key", ""),
                "source": data.get("source", ""),
                "series_id": self.current_series_id,
                "job_id": self.current_job_id
            }
            self.download_manager.add_and_run_episode(ep_item_data)
            
            if self.btn_download.text() == "⏳ Đang khởi chạy tải...":
                self.btn_download.setText("⏳ Đang lưu về máy...")
                self.lbl_status.setText("⏳ Đang tải và bẻ khóa các tập phim về máy...")
                
        elif ep_num and data.get("status") == "error":
            self.download_manager._finished_count += 1
            self.download_manager.error_signal.emit(int(ep_num), data.get("message", "Tải thất bại"))
            self.download_manager._check_all_done()
            self._refresh_balance()

    def _on_dead_link(self, ep_num):
        retry_count = self.server_retries.get(ep_num, 0)
        if retry_count >= 2:
            self.download_manager._finished_count += 1
            self.download_manager.error_signal.emit(ep_num, "Tải thất bại.")
            self.download_manager._check_all_done()
            return
            
        self.server_retries[ep_num] = retry_count + 1
        
        row = ep_num - 1
        if row < self._dl_table().rowCount():
            status_item = QTableWidgetItem(f"🔄 Đang thử lại...")
            status_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            status_item.setForeground(QColor("#f59e0b"))
            self._dl_table().setItem(row, 3, status_item)
            
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
                "aes_key": data.get("aes_key", ""),
                "source": data.get("source", "momigo_raw"),
                "series_id": self.current_series_id,
                "job_id": self.current_job_id
            }
            self.download_manager.add_and_run_episode(ep_item_data)
        else:
            self.download_manager._finished_count += 1
            self.download_manager.error_signal.emit(ep_num, data.get("message", "Tải thất bại"))
            self.download_manager._check_all_done()

    def _hide_pause_btn(self):
        if hasattr(self, 'btn_pause'):
            self.btn_pause.setEnabled(False)
            self.btn_pause.hide()
            self.btn_pause.setText("⏸ Tạm dừng")

    def _on_pay_error(self, error_msg):
        self.btn_download.setEnabled(True)
        self.btn_download.setText("📥 Tải đã chọn")
        self._hide_pause_btn()
        self.lbl_status.setText("Trạng thái: Lỗi lấy link.")
        QMessageBox.critical(self, "Lỗi Xử Lý", error_msg)

    def _open_movie_folder(self):
        folder = os.path.join(self.save_folder, str(getattr(self, 'current_series_id', '') or ''))
        if not os.path.isdir(folder): folder = self.save_folder
        try: os.startfile(folder)
        except Exception as e: QMessageBox.warning(self, "Lỗi", f"Không mở được thư mục: {e}")

    def _detect_image_ext(self, content_type, content):
        """Xác định đúng đuôi file ảnh dựa vào Content-Type thật + magic bytes,
        KHÔNG đoán mò theo URL (URL có thể không có đuôi rõ ràng hoặc sai)."""
        ct = (content_type or '').lower()
        if 'png' in ct: return '.png'
        if 'webp' in ct: return '.webp'
        if 'gif' in ct: return '.gif'
        if 'jpeg' in ct or 'jpg' in ct: return '.jpg'
        if content[:8] == b'\x89PNG\r\n\x1a\n': return '.png'
        if content[:4] == b'RIFF' and content[8:12] == b'WEBP': return '.webp'
        if content[:6] in (b'GIF87a', b'GIF89a'): return '.gif'
        if content[:3] == b'\xff\xd8\xff': return '.jpg'
        return '.jpg'  # fallback cuối cùng

    def _looks_like_video(self, content_type, content):
        """Một số web phim (VD hongguoduanju) dùng 'bìa động' dạng video ngắn
        thay vì ảnh tĩnh cho series_cover. Phát hiện qua Content-Type thật
        hoặc magic bytes container video (mp4/mov/webm), không tin đuôi URL."""
        ct = (content_type or '').lower()
        if ct.startswith('video/'):
            return True
        if len(content) > 12:
            if content[4:8] == b'ftyp':            # MP4 / MOV container
                return True
            if content[:4] == b'\x1a\x45\xdf\xa3':   # WebM / Matroska
                return True
        return False

    def _save_cover_image(self, silent=False):
        cover_url = (getattr(self, 'current_cover_url', '') or '').strip()
        if not cover_url:
            if not silent:
                QMessageBox.warning(self, "Chưa có ảnh bìa",
                    "Chưa quét phim nào hoặc phim này không có ảnh bìa.\n"
                    "Hãy quét 1 phim trước khi tải ảnh bìa.")
            return
        if cover_url.startswith('//'):
            cover_url = 'https:' + cover_url

        folder = os.path.join(self.save_folder, str(getattr(self, 'current_series_id', '') or ''))
        os.makedirs(folder, exist_ok=True)

        safe_title = re.sub(r'[\\/:*?"<>|]', '_', getattr(self, 'current_title', '') or 'poster').strip() or 'poster'

        # KHÔNG đoán đuôi file từ chuỗi URL (dễ sai nếu URL không có đuôi rõ
        # ràng hoặc dùng link ký số phức tạp) - tải về trước, rồi xác định
        # đúng định dạng THẬT dựa vào Content-Type / magic bytes của dữ liệu
        # tải được, tránh lưu nhầm .jpg cho ảnh WEBP/PNG/... hoặc video khác.
        if silent:
            for _ext_check in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
                if os.path.exists(os.path.join(folder, f"{safe_title}{_ext_check}")):
                    return  # Đã có sẵn, khỏi tải lại

        try:
            resp = requests.get(cover_url, timeout=15)
            resp.raise_for_status()
            content = resp.content
            content_type = resp.headers.get('Content-Type', '')

            if self._looks_like_video(content_type, content):
                # "Bìa động" dạng video (VD MP4 ngắn) chứ không phải ảnh tĩnh
                # - trích 1 khung hình bằng ffmpeg làm ảnh bìa TĨNH thật, đúng
                # ý người dùng ("tải ảnh bìa" chứ không phải tải video).
                import tempfile as _tempfile, subprocess as _subprocess
                tmp_video = os.path.join(_tempfile.gettempdir(), f"_cover_tmp_{int(time.time())}.mp4")
                with open(tmp_video, 'wb') as f:
                    f.write(content)

                dest_path = os.path.join(folder, f"{safe_title}.jpg")
                ffmpeg = get_ffmpeg_path()
                si = None
                if sys.platform == "win32":
                    si = _subprocess.STARTUPINFO()
                    si.dwFlags |= _subprocess.STARTF_USESHOWWINDOW
                _subprocess.run(
                    [ffmpeg, "-y", "-i", tmp_video, "-vframes", "1", "-q:v", "2", dest_path],
                    startupinfo=si, stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL
                )
                try: os.remove(tmp_video)
                except Exception: pass

                if not os.path.exists(dest_path):
                    raise RuntimeError("Không trích được khung hình từ bìa động (video).")
            else:
                ext = self._detect_image_ext(content_type, content)
                dest_path = os.path.join(folder, f"{safe_title}{ext}")
                with open(dest_path, 'wb') as f:
                    f.write(content)

            if not silent:
                QMessageBox.information(self, "Đã lưu ảnh bìa",
                    f"Đã lưu ảnh bìa vào:\n{dest_path}")
                try: os.startfile(folder)
                except Exception: pass
        except Exception as e:
            if not silent:
                QMessageBox.warning(self, "Máy chủ đang bận",
                                    "Máy chủ đang quá tải.\nVui lòng chờ 1-2 phút rồi thử lại nhé!")
            # silent mode: lỗi thì bỏ qua, không làm gián đoạn tiến trình tải phim

    def _bump_total_progress(self):
        if not hasattr(self, 'total_progress'): return
        self._dl_finished = getattr(self, '_dl_finished', 0) + 1
        self.total_progress.setValue(min(self._dl_finished, self.total_progress.maximum()))

    def _load_dub_voices(self):
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
        # Đã bỏ giọng Edge TTS (🌐) theo yêu cầu — chỉ giữ giọng CapCut.
        for i in range(self.cmb_dub_voice.count()):
            if "BV074_streaming]" in self.cmb_dub_voice.itemText(i):
                self.cmb_dub_voice.setCurrentIndex(i); break

    def _on_dub_voice_changed(self, sel_text):
        """Ô 'Luồng' áp dụng cho CẢ Edge TTS lẫn CapCut - trước đây CapCut bị
        khóa cứng 4 luồng, nhưng đã bỏ khóa vì thực tế chạy nhiều luồng hơn
        vẫn ổn (dựa trên tool CapCut TTS/STT gốc). Chỉ đổi tooltip gợi ý mức
        an toàn khác nhau tùy loại giọng, không còn khóa/làm mờ ô nữa."""
        if not hasattr(self, 'spn_tts_workers'):
            return
        is_edge = sel_text.startswith("🌐 ")
        self.spn_tts_workers.setEnabled(True)
        if is_edge:
            self.spn_tts_workers.setToolTip(
                "Số luồng Edge TTS chạy song song (miễn phí, có thể để cao 20-50)."
            )
        else:
            self.spn_tts_workers.setToolTip(
                "Số luồng CapCut chạy song song (API riêng, có giới hạn theo\n"
                "tài khoản/thiết bị). Nếu thấy CHẬM HƠN khi tăng luồng, thử\n"
                "giảm về 1-2 - một số tài khoản CapCut bị nghẽn phía server\n"
                "khi gửi nhiều request đồng thời, chạy tuần tự lại nhanh hơn."
            )

    def _refresh_balance(self):
        if hasattr(self, 'refresh_stats_signal'): 
            self.refresh_stats_signal.emit()

    def _on_download_progress(self, ep_num, percent, speed_mb):
        row = ep_num - 1
        if row < self._dl_table().rowCount():
            if percent == -1:
                status_item = QTableWidgetItem(f"⏳ Đang xử lý...")
                status_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                status_item.setForeground(QColor("#f59e0b"))
                status_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            elif percent == -2:
                status_item = QTableWidgetItem(f"🔄 Thử lại lần {int(speed_mb)}...")
                status_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                status_item.setForeground(QColor("#f43f5e"))
                status_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            elif percent == 99 and speed_mb == 0.0:
                status_item = QTableWidgetItem(f"⏳ Đang xử lý...")
                status_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                status_item.setForeground(QColor("#a855f7"))
                status_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            else:
                status_item = QTableWidgetItem(f"⬇️ {percent}% ({speed_mb:.1f} MB/s)")
                status_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                status_item.setForeground(QColor("#38bdf8"))
                status_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            
            self._dl_table().setItem(row, 3, status_item)

    def _on_episode_downloaded(self, ep_num, file_path):
        row = ep_num - 1

        # Kiểm tra audio ngay sau khi tải xong
        info = probe_stream(file_path, get_ffprobe_path())
        if not info["has_audio"]:
            # Mất tiếng — hiển thị cảnh báo đỏ trên bảng
            if row < self._dl_table().rowCount():
                warn_item = QTableWidgetItem("⚠️ MẤT TIẾNG")
                warn_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                warn_item.setForeground(QColor("#f97316"))
                warn_item.setToolTip("Tập này tải về bị mất tiếng. Thử tải lại tập này.")
                _f = warn_item.font(); _f.setBold(True); warn_item.setFont(_f)
                self._dl_table().setItem(row, 3, warn_item)
            self._bump_total_progress()
            return

        if row < self._dl_table().rowCount():
            done_item = QTableWidgetItem("✔ ĐÃ XONG")
            done_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable); done_item.setForeground(QColor("#22d3ee")); done_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            _f = done_item.font(); _f.setBold(True); done_item.setFont(_f)
            self._dl_table().setItem(row, 3, done_item)
        self._bump_total_progress()
        if hasattr(self, 'btn_stt_now'):
            self.btn_stt_now.setEnabled(True)

    def _on_download_error(self, ep_num, error_msg):
        row = ep_num - 1
        if row < self._dl_table().rowCount():
            short_msg = error_msg[:25] + "..." if len(error_msg) > 25 else error_msg
            err_item = QTableWidgetItem(f"❌ {short_msg}")
            err_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable); err_item.setForeground(QColor("#ef4444")); err_item.setToolTip(str(error_msg)); err_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            self._dl_table().setItem(row, 3, err_item)
        self._bump_total_progress()

    def _on_all_downloads_done(self, total_downloaded):
        self._refresh_balance()
        self._hide_pause_btn()

        mode = self.merge_mode_combo.currentIndex()
        files_to_merge = getattr(self, 'downloaded_file_paths', [])
        auto_stt = hasattr(self, 'chk_auto_stt') and self.chk_auto_stt.isChecked()

        # Nếu tick "Tách nhạc nền" -> xử lý theo 2 trường hợp:
        # 1) CÓ tick "Lồng tiếng": tách NGẦM ngay, lưu cache, DubThread dùng
        #    lại sau (tiết kiệm thời gian chờ Demucs lúc lồng tiếng thật).
        # 2) KHÔNG tick "Lồng tiếng": không có DubThread nào chạy để dùng
        #    cache -> tự XUẤT LUÔN video riêng chỉ có giọng gốc, không nhạc
        #    nền (giống hệt nút "🎵 Tách nhạc nền ngay" thủ công, nhưng chạy
        #    tự động, và chạy song song nhiều video theo ô "Tách song song").
        if hasattr(self, 'chk_remove_bgm') and self.chk_remove_bgm.isChecked() and files_to_merge:
            use_gpu_pre = self.chk_use_gpu.isChecked() if hasattr(self, 'chk_use_gpu') else False
            will_dub = hasattr(self, 'chk_auto_dub') and self.chk_auto_dub.isChecked()

            if will_dub:
                self._bgm_precompute_thread = BgmPrecomputeThread(list(files_to_merge), use_gpu=use_gpu_pre)
                if hasattr(self, 'txt_stt_log'):
                    self._bgm_precompute_thread.progress_signal.connect(
                        lambda m: self.txt_stt_log.append(m.strip())
                    )
                self._keep_thread_alive(self._bgm_precompute_thread)
                self._bgm_precompute_thread.start()
            else:
                del_orig_bgm = self.chk_bgm_del_original.isChecked() if hasattr(self, 'chk_bgm_del_original') else False
                self._bgm_standalone_thread = BgmStandaloneThread(
                    list(files_to_merge), use_gpu=use_gpu_pre, del_original=del_orig_bgm
                )
                if hasattr(self, 'txt_stt_log'):
                    self.txt_stt_log.show()
                    self.txt_stt_log.append(f"🎵 Không lồng tiếng -> tự xuất {len(files_to_merge)} video không nhạc nền...")
                    self._bgm_standalone_thread.progress_signal.connect(
                        lambda m: self.txt_stt_log.append(m.strip())
                    )
                self._keep_thread_alive(self._bgm_standalone_thread)
                self._bgm_standalone_thread.start()

        if auto_stt and files_to_merge:
            self.btn_download.setEnabled(True)
            self.btn_download.setText("📥 Tải đã chọn")
            if hasattr(self, 'lbl_downloaded_badge'): self.lbl_downloaded_badge.show()
            self.lbl_status.setText(f"✅ Đã tải {total_downloaded} tập. Tự động tách sub (ghép ở bước cuối)...")
            self._files_for_stt = list(files_to_merge)
            QTimer.singleShot(500, self._run_stt_on_downloaded)
            return

        if mode == 0 or len(files_to_merge) <= 1:
            self.btn_download.setEnabled(True)
            self.btn_download.setText("📥 Tải đã chọn")
            self.lbl_status.setText(f"✅ Hoàn tất! Đã lưu {total_downloaded} tập phim lẻ.")
            if hasattr(self, 'lbl_downloaded_badge'): self.lbl_downloaded_badge.show()
            if getattr(self, '_batch_running', False):
                self._batch_tab_finished()
            else:
                QMessageBox.information(self, "Thành công", f"Đã lưu thành công {total_downloaded} tập phim về máy bạn!")
            return

        self._do_merge(mode, files_to_merge)

    def _do_merge(self, mode, files_to_merge, after_dub=False):
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

        # QUAN TRỌNG: suy ra thư mục ghép từ VỊ TRÍ THẬT của file cần ghép,
        # KHÔNG lấy theo self.current_series_id (series đang mở trên UI). Vì
        # dịch/lồng tiếng chạy lâu; nếu giữa chừng bạn quét/mở phim khác thì
        # current_series_id đã đổi -> ghép nhầm sang thư mục phim mới (chưa
        # tồn tại -> lỗi "No such file: merge_list_0.txt", hoặc ghép sai chỗ).
        movie_folder = None
        try:
            _first_files = merge_tasks[0].get("files") or []
            if _first_files:
                movie_folder = os.path.dirname(os.path.abspath(_first_files[0]))
        except Exception:
            movie_folder = None
        if not movie_folder or not os.path.isdir(movie_folder):
            # Dự phòng: quay về cách cũ nếu không suy ra được từ file.
            folder_name = self.current_series_id if self.current_series_id else "Phim_Khong_Ro_ID"
            movie_folder = os.path.join(self.save_folder, folder_name)

        self._merge_srt_files(merge_tasks, movie_folder, after_dub=after_dub)

        # Lưu lại để dọn dẹp SAU KHI ghép xong thành công (xem _on_merge_finished).
        # Không dọn ở đây vì lúc này file .mp4 gộp CHƯA tồn tại - dọn sớm sẽ mất
        # dữ liệu nguồn nếu HonggouMergeThread lỡ lỗi giữa chừng.
        self._last_merge_tasks = merge_tasks
        self._last_merge_folder = movie_folder

        self.btn_download.setText("⚙️ Đang gộp file...")

        self.merge_thread = HonggouMergeThread(movie_folder, merge_tasks)
        self.merge_thread.progress_msg.connect(lambda msg: self.lbl_status.setText(msg))
        self.merge_thread.error_signal.connect(self._on_merge_error)
        self.merge_thread.finished_signal.connect(self._on_merge_finished)
        self._keep_thread_alive(self.merge_thread)
        self.merge_thread.start()

    def _merge_srt_files(self, merge_tasks, movie_folder, after_dub=False):
        import re as _re

        def _srt_to_ms(t):
            h, m, s, ms = map(int, _re.split(r'[:,]', t.strip()))
            return h*3600000 + m*60000 + s*1000 + ms

        def _ms_to_srt_ts(ms):
            ms = max(0, int(ms))
            h, ms = divmod(ms, 3600000)
            m, ms = divmod(ms, 60000)
            s, ms = divmod(ms, 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        def _parse_srt_blocks(path):
            try:
                with open(path, encoding='utf-8', errors='ignore') as f:
                    raw = f.read()
            except Exception:
                return []
            blocks = []
            for block in _re.split(r'\n\s*\n', raw.strip()):
                lines = block.strip().splitlines()
                if len(lines) < 3:
                    continue
                try:
                    time_line = lines[1]
                    m = _re.match(
                        r'(\d+:\d+:\d+[,\.]\d+)\s*-->\s*(\d+:\d+:\d+[,\.]\d+)', time_line)
                    if not m:
                        continue
                    start_ms = _srt_to_ms(m.group(1))
                    end_ms   = _srt_to_ms(m.group(2))
                    text = '\n'.join(lines[2:]).strip()
                    if text:
                        blocks.append((start_ms, end_ms, text))
                except Exception:
                    continue
            return blocks

        def _get_video_duration_ms(video_path):
            import subprocess as _sp
            ffmpeg = get_ffmpeg_path()
            si = None
            if os.name == 'nt':
                si = _sp.STARTUPINFO()
                si.dwFlags |= _sp.STARTF_USESHOWWINDOW

            # Ứng viên ffprobe: cạnh ffmpeg (.exe / không đuôi), rồi trên PATH.
            probes = []
            if ffmpeg:
                d = os.path.dirname(ffmpeg)
                probes += [os.path.join(d, 'ffprobe.exe'), os.path.join(d, 'ffprobe')]
            probes += ['ffprobe.exe', 'ffprobe']  # dựa vào PATH

            for ffprobe in probes:
                # Bỏ qua path tuyệt đối không tồn tại (nhưng vẫn thử tên trần
                # vì nó có thể nằm trên PATH).
                if os.path.isabs(ffprobe) and not os.path.exists(ffprobe):
                    continue
                try:
                    # Đọc ĐỘ DÀI LUỒNG VIDEO (stream=duration của v:0), KHÔNG
                    # phải format=duration của container. Khi ghép concat, mỗi
                    # tập chiếm đúng độ dài video stream; format=duration hay dài
                    # hơn (audio priming/audio dài hơn video) -> canh sub theo nó
                    # sẽ dồn lệch, sub tới muộn dần về cuối.
                    r = _sp.run(
                        [ffprobe, '-v', 'error', '-select_streams', 'v:0',
                         '-show_entries', 'stream=duration',
                         '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
                        stdout=_sp.PIPE, stderr=_sp.PIPE, startupinfo=si)
                    txt = r.stdout.decode(errors='ignore').strip()
                    if txt and txt.upper() != 'N/A':
                        return int(round(float(txt) * 1000))
                    # Một số container không ghi stream duration -> quay lại
                    # format=duration cho tập này (hiếm; chấp nhận sai số nhỏ).
                    r2 = _sp.run(
                        [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                         '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
                        stdout=_sp.PIPE, stderr=_sp.PIPE, startupinfo=si)
                    txt2 = r2.stdout.decode(errors='ignore').strip()
                    if txt2 and txt2.upper() != 'N/A':
                        return int(round(float(txt2) * 1000))
                except Exception:
                    continue

            # Fallback cuối: đọc duration từ chính ffmpeg (không cần ffprobe).
            if ffmpeg:
                try:
                    r = _sp.run([ffmpeg, '-i', video_path],
                                stdout=_sp.PIPE, stderr=_sp.PIPE, startupinfo=si)
                    err = r.stderr.decode(errors='ignore')
                    m = _re.search(r'Duration:\s*(\d+):(\d+):(\d+)\.(\d+)', err)
                    if m:
                        h, mn, s, cs = m.groups()
                        return ((int(h)*3600 + int(mn)*60 + int(s)) * 1000
                                + int(cs.ljust(3, '0')[:3]))
                except Exception:
                    pass
            return None

        for task in merge_tasks:
            video_files = task["files"]
            out_name_noext = os.path.splitext(task["output_name"])[0]

            for suffix, srt_suffix in [("_vi.srt", "_vi.srt"), (".srt", ".srt")]:
                # QUAN TRỌNG: phải đi theo ĐÚNG thứ tự video_files (chính là thứ
                # tự concat của ffmpeg). Với mỗi tập, đo duration THẬT của video
                # rồi cộng offset = tổng duration các tập TRƯỚC nó. Không dùng
                # end-time của câu sub cuối để suy offset (bỏ mất khoảng lặng
                # cuối tập -> lệch dồn càng về sau càng nặng).
                per_ep = []  # (offset_ms_của_tập_này, srt_path)
                offset_ms = 0
                any_srt = False
                missing_dur = False
                for vf in video_files:
                    base = os.path.splitext(vf)[0]
                    # Khi ghép SAU LỒNG TIẾNG, video là *_dubbed.mp4 nhưng file
                    # sub đặt theo tên GỐC (Tap_01_vi.srt, không phải
                    # Tap_01_dubbed_vi.srt). Bỏ hậu tố _dubbed để tìm đúng sub.
                    sub_base = base[:-len("_dubbed")] if base.endswith("_dubbed") else base
                    srt_path = sub_base + suffix

                    # Offset của tập = tổng ĐỘ DÀI VIDEO STREAM các tập trước.
                    # PHẢI dùng độ dài video stream, KHÔNG dùng format=duration
                    # của container: khi concat, mỗi tập chiếm đúng độ dài luồng
                    # video, trong khi format=duration hay dài hơn (audio priming
                    # / audio dài hơn video) -> nếu cộng theo container, offset dư
                    # ra mỗi tập, dồn qua trăm tập thành sub tới muộn dần.
                    dur = _get_video_duration_ms(vf)

                    per_ep.append((offset_ms, srt_path if os.path.exists(srt_path) else None))
                    if os.path.exists(srt_path):
                        any_srt = True

                    if dur:
                        offset_ms += dur
                    else:
                        missing_dur = True
                        blocks_tmp = _parse_srt_blocks(srt_path) if os.path.exists(srt_path) else []
                        if blocks_tmp:
                            offset_ms += blocks_tmp[-1][1] + 1000

                if not any_srt:
                    continue

                if missing_dur and hasattr(self, 'txt_stt_log'):
                    self.txt_stt_log.append(
                        "⚠️ Có tập không đọc được thời lượng video (ffprobe lỗi) — "
                        "time sub trọn bộ có thể lệch. Kiểm tra ffprobe.exe cạnh ffmpeg."
                    )

                combined_blocks = []
                for ep_offset, sp in per_ep:
                    if not sp:
                        continue
                    for (s, e, t) in _parse_srt_blocks(sp):
                        combined_blocks.append((s + ep_offset, e + ep_offset, t))

                if not combined_blocks:
                    continue

                out_srt = os.path.join(movie_folder, out_name_noext + suffix)
                try:
                    os.makedirs(movie_folder, exist_ok=True)
                    lines_out = []
                    for idx, (s, e, t) in enumerate(combined_blocks, 1):
                        lines_out.append(f"{idx}\n{_ms_to_srt_ts(s)} --> {_ms_to_srt_ts(e)}\n{t}\n")
                    with open(out_srt, 'w', encoding='utf-8') as f:
                        f.write('\n'.join(lines_out))
                    if hasattr(self, 'txt_stt_log'):
                        self.txt_stt_log.append(f"📝 Đã gộp sub: {os.path.basename(out_srt)} ({len(combined_blocks)} dòng)")
                except Exception as ex:
                    if hasattr(self, 'txt_stt_log'):
                        self.txt_stt_log.append(f"⚠️ Gộp sub lỗi: {ex}")

    def _on_merge_error(self, err_msg):
        self.btn_download.setEnabled(True)
        self.btn_download.setText("📥 Tải đã chọn")
        self.lbl_status.setText("❌ Gộp file thất bại.")
        QMessageBox.critical(self, "Lỗi gộp file", err_msg)

    def _on_merge_finished(self):
        self.btn_download.setEnabled(True)
        self.btn_download.setText("📥 Tải đã chọn")

        n_cleaned = self._cleanup_after_merge()

        self.lbl_status.setText("🎉 Hoàn tất! Đã lồng tiếng và ghép file xong.")
        if hasattr(self, 'lbl_downloaded_badge'): self.lbl_downloaded_badge.show()
        if hasattr(self, 'txt_stt_log'):
            self.txt_stt_log.append("🎉 XONG TOÀN BỘ: tách sub → dịch → lồng tiếng → ghép file!")
            if n_cleaned:
                self.txt_stt_log.append(f"🗑 Đã dọn {n_cleaned} file rác, chỉ giữ lại .srt + .mp4 hoàn chỉnh.")
        if getattr(self, '_batch_running', False):
            self._batch_tab_finished()

    def _cleanup_after_merge(self):
        """Dọn sạch mọi file lẻ từng tập (video gốc, *_dubbed.mp4, .srt, _vi.srt,
        .txt) SAU KHI đã ghép thành công thành 1 file trọn bộ - chỉ chạy nếu
        checkbox 'Xóa file gốc' đang bật. Chỉ xóa nguồn của những task mà file
        gộp đầu ra THẬT SỰ đã được tạo ra (an toàn, không mất dữ liệu nếu 1 task
        trong nhiều task bị lỗi giữa chừng)."""
        if not (hasattr(self, 'chk_del_original') and self.chk_del_original.isChecked()):
            return 0
        tasks = getattr(self, '_last_merge_tasks', None)
        folder = getattr(self, '_last_merge_folder', None)
        if not tasks or not folder:
            return 0

        removed = 0
        for task in tasks:
            out_path = os.path.join(folder, task["output_name"])
            if not os.path.exists(out_path):
                # File gộp đầu ra chưa có -> task này lỗi/chưa xong, KHÔNG đụng
                # tới file nguồn của nó để tránh mất dữ liệu.
                continue
            for vf in task["files"]:
                base = os.path.splitext(vf)[0]
                # Nếu là file *_dubbed.mp4 (trường hợp ghép sau khi lồng tiếng),
                # bỏ hậu tố "_dubbed" để tìm đúng *_vi.srt / video gốc đi kèm
                # (đặt tên theo video GỐC, không theo tên file _dubbed).
                if base.endswith("_dubbed"):
                    base = base[:-len("_dubbed")]
                # Xóa đủ mọi file liên quan tới tập này: video gốc, *_dubbed.mp4
                # (nếu vf khác base+.mp4), sub gốc, sub dịch, .txt - dọn dẹp độc
                # lập, không phụ thuộc bước xóa trước đó (nếu có) đã chạy hay chưa.
                targets = {vf, base + ".mp4", base + ".srt", base + "_vi.srt", base + ".txt"}
                for f in targets:
                    try:
                        if os.path.exists(f):
                            os.remove(f)
                            removed += 1
                    except Exception:
                        pass

        self._last_merge_tasks = None
        self._last_merge_folder = None
        return removed

    # ─────────────────────────────────────────────────────────────────
    #  STT / TÁCH SUB
    # ─────────────────────────────────────────────────────────────────
    def _run_dub_on_downloaded(self):
        files = getattr(self, '_files_for_stt', None) or getattr(self, 'downloaded_file_paths', [])
        files = [f for f in files if os.path.exists(f)]
        if not files:
            QMessageBox.warning(self, "Không có file", "Chưa có file video nào!"); return

        tasks = []
        for vf in files:
            base = os.path.splitext(vf)[0]
            vi_srt = base + "_vi.srt"
            srt_goc = base + ".srt"
            if os.path.exists(vi_srt):
                tasks.append({"video": vf, "srt": vi_srt})
            elif os.path.exists(srt_goc):
                self.txt_stt_log.append(f"⚠️ Chưa có bản dịch _vi.srt cho {os.path.basename(vf)} — dùng srt gốc.")
                tasks.append({"video": vf, "srt": srt_goc})
            else:
                self.txt_stt_log.append(f"⏭ Bỏ qua (chưa có SRT): {os.path.basename(vf)}")

        if not tasks:
            QMessageBox.warning(self, "Không có SRT", "Chưa tìm thấy file .srt nào cạnh file video!\nHãy tách sub trước."); return

        sel = self.cmb_dub_voice.currentText() if hasattr(self, 'cmb_dub_voice') else ""
        voice_type = "BV074_streaming"
        if "[" in sel and sel.endswith("]"):
            voice_type = sel[sel.rfind("[")+1:-1]
        rate = f"{self.spn_dub_rate.value():.1f}" if hasattr(self, 'spn_dub_rate') else "1.0"

        # Nếu là giọng Edge TTS (có prefix 🌐) -> tra ra pitch riêng của giọng
        # đó trong EDGE_TTS_VOICES để giọng nghe khác biệt nhau thật sự
        # (không phải chỉ đổi tên mà âm sắc giống hệt do thiếu pitch).
        pitch = "+0Hz"
        if sel.startswith("🌐 "):
            label = sel[2:sel.rfind("[")].strip()
            preset = EDGE_TTS_VOICES.get(label)
            if preset:
                pitch = preset[1]

        self.txt_stt_log.show()
        self.txt_stt_log.append(f"\n🎙 Bắt đầu lồng tiếng {len(tasks)} file | giọng: {voice_type} | rate: {rate}x")
        self.btn_dub_now.setEnabled(False); self.btn_dub_now.setText("⏳ Đang lồng tiếng...")
        self.lbl_status.setText(f"🎙 Đang lồng tiếng {len(tasks)} file...")

        mute_orig = self.chk_mute_original.isChecked() if hasattr(self, 'chk_mute_original') else True
        orig_vol = self.spn_orig_volume.value() if hasattr(self, 'spn_orig_volume') else 15
        remove_bgm = self.chk_remove_bgm.isChecked() if hasattr(self, 'chk_remove_bgm') else False
        use_gpu = self.chk_use_gpu.isChecked() if hasattr(self, 'chk_use_gpu') else False

        tts_workers = self.spn_tts_workers.value() if hasattr(self, 'spn_tts_workers') else 4
        self._dub_thread = DubThread(tasks, voice_type=voice_type, rate=rate, pitch=pitch, mute_original=mute_orig, orig_volume=orig_vol, remove_bgm=remove_bgm, use_gpu=use_gpu, tts_workers=tts_workers)
        self._dub_thread.progress_signal.connect(self._on_stt_progress)   
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
        self._stt_files = list(files)   
        self._stt_out_lang = out

        self._stt_thread = SttBatchThread(files, src_lang=src, out_lang=out, use_trans=use_trans, stt_workers=3)
        self._stt_thread.progress_signal.connect(self._on_stt_progress)
        self._stt_thread.finished_signal.connect(self._on_stt_finished)
        self._keep_thread_alive(self._stt_thread)
        self._stt_thread.start()

    def _on_stt_progress(self, msg):
        self.txt_stt_log.append(msg)
        self.txt_stt_log.verticalScrollBar().setValue(self.txt_stt_log.verticalScrollBar().maximum())

    def _maybe_merge_after_stt(self, files):
        mode = getattr(self, '_merge_mode_after', 0)
        files = [f for f in (files or []) if os.path.exists(f)]
        if mode == 0 or len(files) <= 1:
            # Không ghép -> đây là điểm KẾT THÚC chuỗi (chỉ tách sub, không lồng).
            # Nếu đang tải hàng loạt thì sang bộ kế tiếp.
            if getattr(self, '_batch_running', False):
                self._batch_tab_finished()
            return
        self.txt_stt_log.append(f"🔗 Đang ghép {len(files)} tập theo chế độ đã chọn...")
        self._do_merge(mode, files)

    def _on_stt_finished(self, ok, failed):
        self.btn_stt_now.setEnabled(True); self.btn_stt_now.setText("🔤 Tách sub ngay")
        summary = f"✅ Tách sub xong: {ok} thành công, {failed} lỗi."
        self.lbl_status.setText(summary); self.txt_stt_log.append("\n" + summary)
        if ok <= 0:
            self._files_for_stt = None
            return
        if hasattr(self, 'btn_dub_now'): self.btn_dub_now.setEnabled(True)
        if hasattr(self, 'btn_bgm_only'): self.btn_bgm_only.setEnabled(True)

        srt_files = []
        for vid in getattr(self, '_stt_files', []) or []:
            sp = os.path.splitext(vid)[0] + ".srt"
            if os.path.exists(sp):
                srt_files.append((vid, sp))

        do_translate = hasattr(self, 'chk_do_translate') and self.chk_do_translate.isChecked()
        stt_files_list = list(getattr(self, '_stt_files', []) or [])
        use_deepseek_engine = hasattr(self, 'cb_translate_engine') and self.cb_translate_engine.currentText().startswith("🚀")

        if not do_translate:
            QMessageBox.information(self, "Tách sub hoàn tất",
                f"Đã tách sub {ok} file!\n(Chỉ lưu sub tiếng gốc — chưa bật dịch tự động.)")
            self._maybe_merge_after_stt(stt_files_list)
        elif use_deepseek_engine and DeepSeekTranslateThread and srt_files:
            # Dùng DeepSeek: không cần đăng nhập Gemini/AUTH_FILE, chỉ cần API key
            # (việc kiểm tra thiếu key đã nằm trong _start_gemini_translate).
            self.txt_stt_log.append("\n🚀 Bắt đầu dịch bằng DeepSeek V4 Pro...")
            self._start_gemini_translate(srt_files)
        elif _GEMINI_AVAILABLE and GeminiTranslateThread and srt_files:
            if not os.path.exists(AUTH_FILE):
                QMessageBox.warning(self, "Chưa đăng nhập Gemini",
                    "Bạn cần bấm nút 'Đồng bộ Gemini' để đăng nhập trước khi dịch.")
                self._maybe_merge_after_stt(stt_files_list)
                self._files_for_stt = None
                return
            self.txt_stt_log.append("\n🌐 Bắt đầu dịch bằng Gemini...")
            self._start_gemini_translate(srt_files)
        else:
            QMessageBox.information(self, "Tách sub hoàn tất",
                f"Đã tách sub {ok} file!\n(Chưa cấu hình engine dịch nên không dịch.)")
            self._maybe_merge_after_stt(stt_files_list)
        self._files_for_stt = None

    def _on_translate_engine_changed(self, text):
        QSettings("BoomStudio", "ClientApp").setValue("trans_engine_main", text)
        self.txt_ds_key_main.setVisible(text.startswith("🚀"))

    def _start_gemini_translate(self, srt_files):
        use_deepseek = self.cb_translate_engine.currentText().startswith("🚀") if hasattr(self, 'cb_translate_engine') else False

        if use_deepseek:
            api_key = self.txt_ds_key_main.text().strip()
            if not api_key:
                self.txt_stt_log.append("❌ Chưa nhập DeepSeek API key! Vào ô cạnh combo Engine để dán key.")
                return
            if DeepSeekTranslateThread is None:
                self.txt_stt_log.append("❌ Không tìm thấy module DeepSeek (deepseek_translate.py). Hãy đặt file này cạnh app.")
                return

            queue = [{"video": v, "srt": s} for (v, s) in srt_files]
            self._gemini_vi_map = {}
            self._gemini_total = len(queue)
            self._gemini_done_count = 0
            self._dub_queue = []
            self._dub_running = False
            self._auto_dub_on = hasattr(self, 'chk_auto_dub') and self.chk_auto_dub.isChecked()

            _total_eps = getattr(self, 'current_total_eps', 0)
            _is_full_series = (_total_eps > 0 and len(queue) >= _total_eps)
            self._gtrans_thread = DeepSeekTranslateThread(queue, api_key=api_key, full_series_mode=_is_full_series)
            self._gtrans_thread.log.connect(lambda m: self.txt_stt_log.append(m.strip()))

            def _on_item_done(idx, video_path, vi_path):
                self._gemini_vi_map[video_path] = vi_path
                if self._auto_dub_on and os.path.exists(vi_path):
                    self.txt_stt_log.append(f"✅ Dịch xong {os.path.basename(video_path)} → xếp hàng lồng tiếng.")
                    self._dub_queue.append(video_path)
                    self._pump_dub_queue()

            def _on_item_failed(idx, msg):
                self.txt_stt_log.append(f"⚠️ Dịch lỗi 1 file: {msg}")

            self._gtrans_thread.item_done.connect(_on_item_done)
            self._gtrans_thread.item_failed.connect(_on_item_failed)
            self._gtrans_thread.all_done.connect(self._on_gemini_all_done)
            self._keep_thread_alive(self._gtrans_thread)
            self._gtrans_thread.start()
            return

        settings = QSettings("BoomStudio", "ClientApp")
        preset = settings.value("trans_preset", list(PROMPT_PRESETS.keys())[0] if PROMPT_PRESETS else "")
        _CUSTOM_KEY = "✏️ Tự nhập prompt"
        if preset == _CUSTOM_KEY:
            custom_text = settings.value("trans_custom_prompt", "").strip()
            if not custom_text:
                self.txt_stt_log.append("⚠️ Chưa nhập prompt tùy chỉnh! Vào 'Đồng bộ Gemini' → 'Tự nhập prompt' để điền.")
                return
            PROMPT_PRESETS[_CUSTOM_KEY] = custom_text
        queue = [{"video": v, "srt": s} for (v, s) in srt_files]
        self._gemini_vi_map = {}
        self._gemini_total = len(queue)
        self._gemini_done_count = 0
        self._dub_queue = []            
        self._dub_running = False      
        self._auto_dub_on = hasattr(self, 'chk_auto_dub') and self.chk_auto_dub.isChecked()

        _show_browser = hasattr(self, 'chk_show_browser') and self.chk_show_browser.isChecked()
        _trans_workers = self.spn_trans_workers.value() if hasattr(self, 'spn_trans_workers') else 2
        # An toàn: khi bật "hiện trình duyệt" mà chạy nhiều tập song song sẽ có
        # nhiều cửa sổ Chrome bật cùng lúc, rất rối -> ép về 1 tập để dễ xem.
        if _show_browser and _trans_workers > 1:
            _trans_workers = 1
            self.txt_stt_log.append("👁 Đang bật 'Hiện trình duyệt' → tạm chạy 1 tập/lượt cho dễ xem.")
        self._gtrans_thread = GeminiTranslateThread(queue, preset, "Auto (Mặc định)", 80,
                                                    translate_workers=_trans_workers, show_browser=_show_browser)
        self._gtrans_thread.log.connect(lambda m: self.txt_stt_log.append(m.strip()))
        def _on_item_done(idx, video_path, vi_path):
            self._gemini_vi_map[video_path] = vi_path
            if self._auto_dub_on and os.path.exists(vi_path):
                self.txt_stt_log.append(f"✅ Dịch xong {os.path.basename(video_path)} → xếp hàng lồng tiếng.")
                self._dub_queue.append(video_path)
                self._pump_dub_queue()
        def _on_item_failed(idx, msg):
            self.txt_stt_log.append(f"⚠️ Dịch lỗi 1 file: {msg}")
        self._gtrans_thread.item_done.connect(_on_item_done)
        self._gtrans_thread.item_failed.connect(_on_item_failed)
        self._gtrans_thread.all_done.connect(self._on_gemini_all_done)
        self._keep_thread_alive(self._gtrans_thread)
        self._gtrans_thread.start()

    def _pump_dub_queue(self):
        if self._dub_running or not self._dub_queue:
            return
        video_path = self._dub_queue.pop(0)
        vi_srt = os.path.splitext(video_path)[0] + "_vi.srt"
        if not os.path.exists(vi_srt):
            QTimer.singleShot(100, self._pump_dub_queue)
            return
        self._dub_running = True
        voice_type_sel = self.cmb_dub_voice.currentText()
        voice_type = voice_type_sel[voice_type_sel.rfind("[")+1:-1] if "[" in voice_type_sel else "BV074_streaming"
        rate = str(self.spn_dub_rate.value()) if hasattr(self, 'spn_dub_rate') else "1.0"
        # Tra pitch riêng nếu là giọng Edge TTS - đồng bộ với logic ở _run_dub_on_downloaded
        pitch = "+0Hz"
        if voice_type_sel.startswith("🌐 "):
            _label = voice_type_sel[2:voice_type_sel.rfind("[")].strip()
            _preset = EDGE_TTS_VOICES.get(_label)
            if _preset:
                pitch = _preset[1]
        mute_orig = self.chk_mute_original.isChecked() if hasattr(self, 'chk_mute_original') else True
        orig_vol = self.spn_orig_volume.value() if hasattr(self, 'spn_orig_volume') else 15
        remove_bgm = self.chk_remove_bgm.isChecked() if hasattr(self, 'chk_remove_bgm') else False
        use_gpu = self.chk_use_gpu.isChecked() if hasattr(self, 'chk_use_gpu') else False
        tts_workers = self.spn_tts_workers.value() if hasattr(self, 'spn_tts_workers') else 4
        self.txt_stt_log.append(f"🎙 Lồng tiếng: {os.path.basename(video_path)}...")

        self._roll_dub_thread = DubThread([{"video": video_path, "srt": vi_srt}],
                                          voice_type=voice_type, rate=rate, pitch=pitch, mute_original=mute_orig,
                                          orig_volume=orig_vol, remove_bgm=remove_bgm, use_gpu=use_gpu, tts_workers=tts_workers)
        self._roll_dub_thread.progress_signal.connect(lambda m: self.txt_stt_log.append(m.strip()))
        def _one_done(ok, failed):
            self._dub_running = False
            if getattr(self, '_merge_mode_after', 0) == 0:
                self._cleanup_one_episode(video_path, vi_srt)
            if self._dub_queue:
                self._pump_dub_queue()  
            else:
                trans_done = not (hasattr(self, '_gtrans_thread') and self._gtrans_thread.isRunning())
                if trans_done:
                    self._merge_after_dub()
        self._roll_dub_thread.finished_signal.connect(_one_done)
        self._keep_thread_alive(self._roll_dub_thread)
        self._roll_dub_thread.start()

    # ── TÁCH NHẠC NỀN ĐỘC LẬP BẰNG SUBPROCESS AN TOÀN ───────────────────
    def _run_bgm_only(self):
        """Wrapper: kiểm tra Demucs đã cài chưa (lazy-install), quản lý trạng thái
        nút bấm, rồi mới gọi _run_bgm_only_core() thực hiện tách nhạc."""
        if _DEMUCS_MANAGER_OK:
            from demucs_manager import is_demucs_ready
            if not is_demucs_ready():
                def _after_install():
                    self._run_bgm_only()  # cài xong -> tự chạy lại
                ensure_demucs_installed_ui(self, _after_install)
                return

        self.btn_bgm_only.setEnabled(False)
        self.btn_bgm_only.setText("⏳ Đang tách nhạc nền...")
        self._run_bgm_only_core()

    def _run_bgm_only_core(self):
        """Tách nhạc nền độc lập cho các video đã tải."""
        files = getattr(self, '_files_for_stt', None) or getattr(self, 'downloaded_file_paths', [])
        files = [f for f in files if os.path.exists(f)]
        if not files:
            QMessageBox.warning(self, "Không có file", "Chưa có file video nào!"); return

        del_orig = self.chk_bgm_del_original.isChecked() if hasattr(self, 'chk_bgm_del_original') else False
        use_gpu = self.chk_use_gpu.isChecked() if hasattr(self, 'chk_use_gpu') else False
        
        self.txt_stt_log.show()
        self.txt_stt_log.append(f"\n🎵 Bắt đầu tách nhạc nền {len(files)} file...")

        import threading
        def _worker():
            ffmpeg = get_ffmpeg_path()
            if not ffmpeg:
                self.txt_stt_log.append("❌ Không tìm thấy ffmpeg!"); return

            # Setup môi trường xử lý
            import os, subprocess, sys, tempfile, shutil
            _demucs_py_env = _resolve_demucs_python()  # xác định trước để build env đúng, tránh xung đột DLL
            env = _clean_subprocess_env(_demucs_py_env)

            if use_gpu:
                env.pop("CUDA_VISIBLE_DEVICES", None) # Mở khóa GPU cho subprocess
                device = "cuda"
                gpu_label = "GPU"
            else:
                env["KMP_DUPLICATE_LIB_OK"] = "TRUE"  # Chống xung đột thư viện CPU
                _n_threads = str(max(1, int(os.cpu_count() * 0.3)))
                env["OMP_NUM_THREADS"] = _n_threads # Ép chạy 30% luồng cho an toàn
                env["MKL_NUM_THREADS"] = _n_threads
                env["NUMEXPR_NUM_THREADS"] = _n_threads
                env["OPENBLAS_NUM_THREADS"] = _n_threads
                device = "cpu"
                gpu_label = "CPU (30% Công suất - An toàn)"

            # Dùng mdx_extra (KHÔNG _q) vì bản _q cần gói "diffq" - gói này không có
            # wheel cho Python 3.11 Windows nên pip phải tự biên dịch, cần cài thêm
            # Visual Studio Build Tools trên máy khách. mdx_extra không cần diffq.
            model_name = "mdx_extra"
            ok = failed = 0
            
            for idx, video_path in enumerate(files):
                bname = os.path.basename(video_path)
                self.txt_stt_log.append(f"[{idx+1}/{len(files)}] 🎵 {bname} ({gpu_label})...")
                tmp = tempfile.mkdtemp(prefix="bgm_only_")
                try:
                    si = None
                    if sys.platform == "win32":
                        si = subprocess.STARTUPINFO()
                        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW

                    raw_wav = os.path.join(tmp, "orig.wav")
                    subprocess.run([ffmpeg, "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", raw_wav],
                                   startupinfo=si, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    demucs_out = os.path.join(tmp, "out")
                    
                    # Gọi Demucs bằng subprocess. KHÔNG import trực tiếp trong app
                    _demucs_py = _demucs_py_env
                    cmd_demucs = [
                        _demucs_py, "-m", "demucs.separate",
                        "-n", model_name,
                        "--two-stems", "vocals",
                        "-d", device,
                        "--out", demucs_out,
                        raw_wav
                    ]
                    
                    res = subprocess.run(cmd_demucs, env=env, startupinfo=si, 
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    
                    if res.returncode != 0:
                        raise RuntimeError(res.stderr.decode("utf-8", errors="ignore")[-200:])

                    stem = os.path.splitext(os.path.basename(raw_wav))[0]
                    vocals_path = os.path.join(demucs_out, model_name, stem, "vocals.wav")
                    
                    if not os.path.exists(vocals_path):
                        raise FileNotFoundError("Không tìm thấy kết quả tách nhạc.")

                    out_video = os.path.splitext(video_path)[0] + "_vocals.mp4"
                    subprocess.run([ffmpeg, "-y", "-i", video_path, "-i", vocals_path,
                                    "-map", "0:v", "-map", "1:a",
                                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                                    "-shortest", out_video],
                                   startupinfo=si, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    if del_orig and os.path.exists(out_video):
                        os.remove(video_path)
                        os.rename(out_video, video_path)
                        self.txt_stt_log.append(f"[{idx+1}] ✅ Xong! Đã ghi đè: {bname}")
                    else:
                        self.txt_stt_log.append(f"[{idx+1}] ✅ Xong! → {os.path.basename(out_video)}")
                    ok += 1
                except Exception as e:
                    self.txt_stt_log.append(f"[{idx+1}] ❌ Lỗi: {str(e)[:100]}")
                    failed += 1
                finally:
                    try: shutil.rmtree(tmp)
                    except: pass

            # Update UI khi xong
            def _done():
                summary = f"🎵 Tách nhạc nền xong: {ok} thành công, {failed} lỗi."
                self.txt_stt_log.append("\n" + summary)
                self.lbl_status.setText(summary)
                self.btn_bgm_only.setEnabled(True)
                self.btn_bgm_only.setText("🎵 Tách nhạc nền ngay")

            QTimer.singleShot(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _cleanup_one_episode(self, video_path, vi_srt):
        if not (hasattr(self, 'chk_del_original') and self.chk_del_original.isChecked()):
            return
        try:
            base = os.path.splitext(video_path)[0]      
            dubbed = base + "_dubbed.mp4"
            srt_goc = base + ".srt"
            txt_goc = base + ".txt"
            if not os.path.exists(dubbed):
                return  
            for f in (video_path, srt_goc, txt_goc):
                try:
                    if os.path.exists(f): os.remove(f)
                except Exception: pass
            try:
                if os.path.exists(video_path):  
                    pass
                else:
                    os.rename(dubbed, video_path)   
            except Exception: pass
            try:
                if os.path.exists(vi_srt):
                    os.rename(vi_srt, srt_goc)
            except Exception: pass
            self.txt_stt_log.append(f"🗑 Đã dọn file gốc, giữ bản Việt: {os.path.basename(video_path)}")
        except Exception as e:
            self.txt_stt_log.append(f"⚠️ Dọn file lỗi: {str(e)[:50]}")

    def _merge_after_dub(self):
        mode = getattr(self, '_merge_mode_after', 0)
        if mode == 0:
            self.txt_stt_log.append("🎉 Hoàn tất tất cả: tách sub → dịch → lồng tiếng (từng tập rời)!")
            self.lbl_status.setText("🎉 Hoàn tất! Các tập đã lồng tiếng (rời).")
            if getattr(self, '_batch_running', False):
                self._batch_tab_finished()
            return

        dubbed = sorted(getattr(self, '_gemini_vi_map', {}).keys())

        # ── RETRY tối đa 3 LẦN các tập THIẾU bản _dubbed.mp4 TRƯỚC KHI GHÉP ──
        # Quan trọng với chế độ ghép trọn bộ: nếu 1 tập lồng lỗi (VD hết ổ
        # đĩa tạm thời, lỗi mạng) mà cứ ghép luôn thì bản trọn bộ sẽ THIẾU
        # HẲN tập đó. Nên gom các tập chưa có _dubbed.mp4, đẩy lại hàng đợi
        # lồng tiếng. Mỗi tập thử lại tối đa 3 lần (đếm bằng _dub_retry_count)
        # để không lặp vô hạn nếu tập đó hỏng thật.
        MAX_DUB_RETRY = 3
        if not hasattr(self, '_dub_retry_count'):
            self._dub_retry_count = {}
        missing = []
        for v in dubbed:
            df = os.path.splitext(v)[0] + "_dubbed.mp4"
            vi_srt = os.path.splitext(v)[0] + "_vi.srt"
            if (not os.path.exists(df)) and os.path.exists(vi_srt) \
                    and self._dub_retry_count.get(v, 0) < MAX_DUB_RETRY:
                missing.append(v)

        if missing:
            for v in missing:
                self._dub_retry_count[v] = self._dub_retry_count.get(v, 0) + 1
                self._dub_queue.append(v)
            _lan = self._dub_retry_count[missing[0]]
            self.txt_stt_log.append(
                f"🔁 Có {len(missing)} tập lồng tiếng lỗi → thử lồng lại (lần {_lan}/{MAX_DUB_RETRY}) trước khi ghép: "
                + ", ".join(os.path.basename(x) for x in missing))
            self._pump_dub_queue()   # lồng lại; xong sẽ tự quay lại _merge_after_dub
            return

        dub_files = []
        still_missing = []
        for v in dubbed:
            df = os.path.splitext(v)[0] + "_dubbed.mp4"
            if os.path.exists(df):
                dub_files.append(df)
            else:
                still_missing.append(os.path.basename(v))

        if still_missing:
            # Vẫn còn tập chưa lồng được sau khi đã retry -> KHÔNG ghép trọn bộ
            # (tránh bản ghép thiếu tập), KHÔNG xóa gốc (để bạn lồng lại tay).
            self.txt_stt_log.append(
                f"⚠️ Còn {len(still_missing)} tập chưa lồng được sau {MAX_DUB_RETRY} lần thử: "
                + ", ".join(still_missing)
                + ".\n   → TẠM DỪNG ghép trọn bộ để tránh thiếu tập. File gốc các tập này được GIỮ LẠI, "
                  "bạn hãy lồng tiếng lại thủ công rồi ghép sau.")
            self.lbl_status.setText(f"⚠️ Chưa ghép: còn {len(still_missing)} tập lồng lỗi. Đã giữ file gốc.")
            return

        if len(dub_files) <= 1:
            self.txt_stt_log.append("🎉 Hoàn tất! (Không đủ file để ghép.)")
            return
        if hasattr(self, 'chk_del_original') and self.chk_del_original.isChecked():
            for v in dubbed:
                b = os.path.splitext(v)[0]
                for f in (v, b + ".srt", b + ".txt"):  
                    try:
                        if os.path.exists(f): os.remove(f)
                    except Exception: pass
            self.txt_stt_log.append("🗑 Đã dọn file gốc trước khi ghép.")
        self.txt_stt_log.append(f"🔗 Đang ghép {len(dub_files)} tập đã lồng tiếng thành {'trọn bộ' if mode==1 else 'từng phần'}...")
        self._do_merge(mode, dub_files, after_dub=True)

    def _on_gemini_all_done(self):
        n = len(getattr(self, '_gemini_vi_map', {}))
        _engine_name = "DeepSeek" if (
            DeepSeekTranslateThread is not None
            and isinstance(getattr(self, '_gtrans_thread', None), DeepSeekTranslateThread)
        ) else "Gemini"

        # Nếu không dịch được file nào (vd: API hết tiền, lỗi kết nối...)
        # → hiện cảnh báo thay vì popup "Dịch hoàn tất" giả gây nhầm lẫn
        if n == 0:
            self.txt_stt_log.append(f"\n⚠️ {_engine_name}: Không dịch được file nào. Kiểm tra API key / số dư tài khoản.")
            QMessageBox.warning(self, f"Dịch thất bại",
                f"{_engine_name} không dịch được file nào.\n\n"
                f"Nguyên nhân thường gặp:\n"
                f"• Hết số dư tài khoản (HTTP 402)\n"
                f"• API key sai hoặc hết hạn\n"
                f"• Mất kết nối mạng\n\n"
                f"Kiểm tra log bên dưới để xem chi tiết lỗi.")
            return

        self.txt_stt_log.append(f"\n✅ Dịch {_engine_name} xong toàn bộ: {n} file.")
        if self._auto_dub_on:
            self._pump_dub_queue()
            if not self._dub_queue and not self._dub_running:
                self._merge_after_dub()
        else:
            QMessageBox.information(self, "Dịch hoàn tất",
                f"Đã dịch {n} file sang tiếng Việt (_vi.srt).\nCó thể bấm Lồng tiếng.")
            self._maybe_merge_after_stt(list(getattr(self, '_gemini_vi_map', {}).keys()))

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
            # STANDALONE: bản cập nhật là .zip cả thư mục (không phải 1 .exe).
            temp_path = os.path.join(tempfile.gettempdir(), "BoomStudio_Update.zip")

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

def _apply_update_and_restart(new_zip_path: str):
    """STANDALONE: giải nén .zip cập nhật rồi thay TOÀN BỘ thư mục app.
    Bản build .zip có 1 thư mục con 'BoomStudio' bên trong -> giải nén ra temp,
    copy đè nội dung thư mục đó vào thư mục app đang chạy, rồi khởi động lại."""
    current_exe = _get_exe_path()
    app_dir = os.path.dirname(current_exe)
    extract_dir = os.path.join(tempfile.gettempdir(), "BoomStudio_Update_extract")
    bat_path = os.path.join(tempfile.gettempdir(), "boomstudio_update.bat")

    # PowerShell giải nén; robocopy /MIR đồng bộ (thay cả thư mục, giữ file mới).
    # Nội dung zip: <extract>\BoomStudio\*  -> nguồn copy là thư mục con đó.
    bat_content = f'''@echo off
timeout /t 3 /nobreak >nul
rmdir /s /q "{extract_dir}" >nul 2>&1
powershell -NoProfile -Command "Expand-Archive -LiteralPath '{new_zip_path}' -DestinationPath '{extract_dir}' -Force"
set SRC="{extract_dir}\\BoomStudio"
if not exist %SRC% set SRC="{extract_dir}"
set RETRY=0
:COPY_LOOP
if %RETRY% GEQ 20 goto COPY_DONE
robocopy %SRC% "{app_dir}" /E /R:2 /W:1 >nul
if %ERRORLEVEL% LSS 8 goto COPY_DONE
set /a RETRY+=1
timeout /t 1 /nobreak >nul
goto COPY_LOOP
:COPY_DONE
timeout /t 2 /nobreak >nul
start "" "{current_exe}"
rmdir /s /q "{extract_dir}" >nul 2>&1
del /f /q "{new_zip_path}" >nul 2>&1
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
            box = QMessageBox(self.parent)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Bắt buộc cập nhật")
            box.setText(f"Phiên bản mới v{version} là bản BẮT BUỘC.\n"
                        f"Bạn phải cập nhật để tiếp tục sử dụng.\n\n"
                        f"{changelog}\n\nNhấn OK để tải và cập nhật ngay.")
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
            box.exec()
            self._start_download(force=True)

    def _on_update_clicked(self):
        msg = f"Phiên bản mới: v{self._latest_version}\nHiện tại: v{APP_VERSION}\n\n"
        if self._changelog: msg += f"Thay đổi:\n{self._changelog}\n\n"
        msg += "Nhấn OK để tải bản mới.\nApp sẽ tự tắt → cập nhật → mở lại."
        if QMessageBox.question(self.parent, "Cập nhật phần mềm", msg, QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Ok: return
        self._start_download(force=False)

    def _start_download(self, force=False):
        self.progress = QProgressDialog("Đang tải phiên bản mới...", None, 0, 100, self.parent)
        self.progress.setWindowTitle("Cập nhật BOOM STUDIO")
        self.progress.setWindowModality(Qt.WindowModality.ApplicationModal if force else Qt.WindowModality.WindowModal)
        self.progress.setCancelButton(None); self.progress.setMinimumDuration(0); self.progress.setValue(0)
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

    def _on_dl_done(self, new_zip_path):
        self.progress.close()
        QMessageBox.information(self.parent, "Sẵn sàng", "Tải xong bản mới!\nApp sẽ tự đóng, cập nhật, và mở lại.\nNhấn OK.")
        _apply_update_and_restart(new_zip_path)

    def _on_dl_error(self, error_msg):
        self.progress.close(); self.btn_update.setEnabled(True); self.btn_update.setText(f"🔄 Cập nhật v{self._latest_version}")
        QMessageBox.critical(self.parent, "Lỗi cập nhật", f"Không thể tải bản mới:\n{error_msg}")
        if getattr(self, "_is_force", False):
            QApplication.instance().quit()

# ==========================================
# MÀN HÌNH ĐĂNG NHẬP
# ==========================================
class LoginScreen(QWidget):
    login_success = pyqtSignal(str, str, bool)

    def __init__(self):
        super().__init__()
        self.setStyleSheet("background-color: #0f172a; color: white;")
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.settings = QSettings("BoomStudio", "ClientApp")

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

        lbl_note = QLabel("Tài khoản do Admin cấp. Vui lòng liên hệ Admin để được cấp tài khoản.")
        lbl_note.setWordWrap(True)
        lbl_note.setStyleSheet("color: #64748b; font-size: 12px; margin-top: 10px;")
        lbl_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box_layout.addWidget(lbl_note)
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
                self.login_success.emit(user, data.get("expiry", "Vô thời hạn"), bool(data.get("vip_unlocked", False)))
            else: QMessageBox.critical(self, "Lỗi", data.get("message", "Đăng nhập thất bại"))
        except Exception:
            QMessageBox.critical(self, "Máy chủ đang bận",
                                 "Máy chủ đang quá tải.\nVui lòng chờ 1-2 phút rồi bấm Đăng Nhập lại nhé!")
        self.btn_login.setText("Đăng Nhập"); self.btn_login.setEnabled(True)

    def _handle_register(self):
        # Đã bỏ tự đăng ký: tài khoản do Admin cấp.
        QMessageBox.information(self, "Thông báo", "Hệ thống không mở đăng ký. Vui lòng liên hệ Admin để được cấp tài khoản.")
        return

# ==========================================
# CỬA SỔ CHÍNH
# ==========================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"BOOM STUDIO v{APP_VERSION}")
        # Icon cửa sổ / taskbar (icon.ico đặt cạnh app; bỏ qua nếu không có)
        try:
            _ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
            if not os.path.exists(_ico) and getattr(sys, "frozen", False):
                _ico = os.path.join(os.path.dirname(sys.executable), "icon.ico")
            if os.path.exists(_ico):
                self.setWindowIcon(QIcon(_ico))
        except Exception:
            pass
        self.resize(1050, 780); self.setStyleSheet("background-color: #0f172a;")
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.login_screen = LoginScreen()
        self.login_screen.login_success.connect(self.show_main_app)
        self.stack.addWidget(self.login_screen)

    def show_main_app(self, username, expiry, vip_unlocked=False):
        main_widget = QWidget(); main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(0, 0, 0, 0); main_layout.setSpacing(0)

        header = QWidget(); header.setFixedHeight(65)
        header.setStyleSheet("background-color: #1e293b; border-bottom: 1px solid #334155;")
        header_layout = QHBoxLayout(header); header_layout.setContentsMargins(25, 0, 25, 0)

        lbl_logo = QLabel("⚡ BOOM STUDIO")
        lbl_logo.setFont(QFont("Arial", 16, QFont.Weight.Bold)); lbl_logo.setStyleSheet("color: #38bdf8;")
        vip_tag = "🔓 Đã kích hoạt" if vip_unlocked else "🔒 Chưa kích hoạt"
        lbl_user_info = QLabel(f"👤 Khách hàng: <b>{username}</b>  |  ⏳ Hạn: {expiry}  |  {vip_tag}")
        lbl_user_info.setStyleSheet("color: #cbd5e1; font-size: 14px;")

        self.lbl_balance = QLabel("💰 Số dư: --- đ")
        self.lbl_balance.setStyleSheet("color: #10b981; font-size: 14px; font-weight: bold;")
        
        self.lbl_quota = QLabel("🎯 Lượt tải: --/20")
        self.lbl_quota.setStyleSheet("color: #a855f7; font-size: 14px; font-weight: bold;")

        btn_logout = QPushButton("🚪 Đăng Xuất")
        btn_logout.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_logout.setStyleSheet("QPushButton { padding: 8px 16px; background-color: #ef4444; color: white; border-radius: 6px; font-weight: bold; border: none; } QPushButton:hover { background-color: #dc2626; }")
        btn_logout.clicked.connect(self.logout)

        self.btn_gemini = QPushButton()
        self.btn_gemini.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_gemini.clicked.connect(self._open_gemini_sync)
        self._refresh_gemini_btn()

        header_layout.addWidget(lbl_logo); header_layout.addStretch(); header_layout.addWidget(lbl_user_info)
        header_layout.addSpacing(20); header_layout.addWidget(self.lbl_balance)
        header_layout.addSpacing(20); header_layout.addWidget(self.lbl_quota)
        header_layout.addSpacing(15); header_layout.addWidget(self.btn_gemini)
        header_layout.addSpacing(20); header_layout.addWidget(btn_logout)

        self.updater = AutoUpdater(header_layout, parent_widget=self)
        main_layout.addWidget(header)

        self.honggou_tab = HonggouWidget(username, expiry=expiry, vip_unlocked=vip_unlocked)
        self.honggou_tab.balance_changed.connect(self._update_balance_display)
        self.honggou_tab.refresh_stats_signal.connect(lambda: self._fetch_balance(username))
        self.honggou_tab.quota_used_signal.connect(self._deduct_quota)

        # ── Bọc Hongguo + Render thành các TAB trong cùng cửa sổ ──────────
        self.main_tabs = QTabWidget()
        self.main_tabs.setStyleSheet("""
            QTabWidget::pane { border: none; background: #0f172a; }
            QTabBar::tab {
                background: #1e293b; color: #94a3b8;
                padding: 9px 22px; font-weight: bold; font-size: 13px;
                border: 1px solid #334155; border-bottom: none;
                margin-right: 2px;
            }
            QTabBar::tab:selected { background: #0f172a; color: #38bdf8; }
            QTabBar::tab:hover { color: #e2e8f0; }
        """)
        self.main_tabs.addTab(self.honggou_tab, "👑  Hongguo VIP")

        # ── Thêm các tab nền tảng (YouTube, TikTok, Douyin, Bilibili, Facebook, X, Render, Cookie) ──
        _platform_tabs = [
            ("youtube_tab",  "YouTubeWidget",  "🔴  YouTube"),
            ("tiktok_tab",   "TikTokWidget",   "🎵  TikTok"),
            ("douyin_tab",   "DouyinWidget",   "🎶  Douyin"),
            ("bilibili_tab", "BilibiliWidget", "📺  Bilibili"),
            ("facebook_tab", "FacebookWidget", "🔵  Facebook"),
            ("x_tab",        "XWidget",        "✖  X (Twitter)"),
        ]
        for _mod_name, _cls_name, _tab_label in _platform_tabs:
            try:
                import importlib
                _mod = importlib.import_module(_mod_name)
                _cls = getattr(_mod, _cls_name)
                _widget = _cls()
                self.main_tabs.addTab(_widget, _tab_label)
                setattr(self, f"_tab_{_mod_name}", _widget)
            except Exception as _tab_e:
                print(f"[WARN] Không nạp được tab {_tab_label}: {_tab_e}")

        # Render Video nằm sau X, trước Cookie
        if RenderWidget is not None:
            try:
                self.render_tab = RenderWidget()
                self.main_tabs.addTab(self.render_tab, "🎨  Render Video")
            except Exception as _rw_e:
                print(f"[WARN] Không tạo được tab Render: {_rw_e}")

        # Cookie cuối cùng
        try:
            import importlib
            _mod = importlib.import_module("cookie_tab")
            _widget = _mod.CookieWidget()
            self.main_tabs.addTab(_widget, "🍪  Cookie")
            self._tab_cookie_tab = _widget
        except Exception as _tab_e:
            print(f"[WARN] Không nạp được tab Cookie: {_tab_e}")

        main_layout.addWidget(self.main_tabs)

        self.stack.addWidget(main_widget); self.stack.setCurrentWidget(main_widget)
        self._fetch_balance(username)
        self._refresh_quota(username)
        self._hb_username = username
        self._hb_timer = QTimer(self); self._hb_timer.timeout.connect(self._send_heartbeat); self._hb_timer.start(20000); self._send_heartbeat()
    
    def _fetch_balance(self, username):
        try:
            token = self.honggou_tab.auth_token if hasattr(self, 'honggou_tab') else QSettings("BoomStudio", "ClientApp").value("auth_token", "")
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
        username = getattr(self, '_hb_username', '')
        if username:
            self._refresh_quota(username)

        try:
            token = QSettings("BoomStudio", "ClientApp").value("auth_token", "")
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

        btn_login = QPushButton("🔑 Đăng nhập Gemini (1 lần)")
        btn_login.setStyleSheet("background:#7c3aed;")
        lay.addWidget(btn_login)

        _gem_settings = QSettings("BoomStudio", "ClientApp")
        CUSTOM_KEY = "✏️ Tự nhập prompt"

        lay.addWidget(QLabel("Chọn prompt dịch:"))
        cb_preset = QComboBox()
        preset_keys = list(PROMPT_PRESETS.keys()) + [CUSTOM_KEY]
        cb_preset.addItems(preset_keys)
        _default_preset = preset_keys[0] if preset_keys else CUSTOM_KEY
        saved_preset = _gem_settings.value("trans_preset", _default_preset)
        if saved_preset in preset_keys:
            cb_preset.setCurrentText(saved_preset)
        else:
            cb_preset.setCurrentText(CUSTOM_KEY)
        lay.addWidget(cb_preset)

        lbl_prompt = QLabel("Nội dung prompt:")
        lay.addWidget(lbl_prompt)

        txt_preview = QTextEdit()
        txt_preview.setFixedHeight(160)
        lay.addWidget(txt_preview)

        saved_custom = _gem_settings.value("trans_custom_prompt", "")

        def _update_preview():
            sel = cb_preset.currentText()
            if sel == CUSTOM_KEY:
                lbl_prompt.setText("✏️ Nhập prompt của bạn (tự do):")
                txt_preview.setReadOnly(False)
                txt_preview.setStyleSheet(
                    "QTextEdit { background:#1e293b; color:#fde68a; border:2px solid #f59e0b; "
                    "border-radius:6px; padding:6px; }")
                txt_preview.setPlaceholderText(
                    "Nhập prompt tùy ý của bạn vào đây...\n"
                    "Ví dụ: Dịch sang tiếng Việt tự nhiên, giữ nguyên tên nhân vật...")
                if txt_preview.toPlainText() == "" or txt_preview.isReadOnly():
                    txt_preview.setPlainText(saved_custom)
            else:
                lbl_prompt.setText("Nội dung prompt:")
                txt_preview.setReadOnly(True)
                txt_preview.setStyleSheet(
                    "QTextEdit { background:#1e293b; color:#e2e8f0; border:1px solid #334155; "
                    "border-radius:6px; padding:6px; }")
                txt_preview.setPlainText(PROMPT_PRESETS.get(sel, ""))

        cb_preset.currentTextChanged.connect(lambda _: _update_preview())
        _update_preview()

        log_box = QTextEdit(); log_box.setReadOnly(True); log_box.setFixedHeight(70)
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

        btn_save = QPushButton("💾 Lưu prompt & Đóng")
        btn_save.setStyleSheet("background:#16a34a;")
        def _save_close():
            sel = cb_preset.currentText()
            _gem_settings.setValue("trans_preset", sel)
            if sel == CUSTOM_KEY:
                _gem_settings.setValue("trans_custom_prompt", txt_preview.toPlainText().strip())
            dlg.accept()
        btn_save.clicked.connect(_save_close)
        lay.addWidget(btn_save)

        dlg.exec()
        self._refresh_gemini_btn()

    def _refresh_quota(self, username):
        settings = QSettings("BoomStudio", "ClientApp")
        today = datetime.now().strftime("%Y-%m-%d")
        saved_date = settings.value(f"quota_date_{username}", "")
        
        if saved_date != today:
            settings.setValue(f"quota_date_{username}", today)
            settings.setValue(f"quota_left_{username}", 20)
            quota = 20
        else:
            try: quota = int(settings.value(f"quota_left_{username}", 20))
            except: quota = 20
            
        self.lbl_quota.setText(f"🎯 Lượt tải: {quota}/20")
        self.honggou_tab.current_quota = quota

    def _deduct_quota(self):
        username = getattr(self, '_hb_username', '')
        if not username: return
        
        self._refresh_quota(username)
        
        settings = QSettings("BoomStudio", "ClientApp")
        try: quota = int(settings.value(f"quota_left_{username}", 20))
        except: quota = 20
        
        if quota > 0:
            quota -= 1
            settings.setValue(f"quota_left_{username}", quota)
            self.lbl_quota.setText(f"🎯 Lượt tải: {quota}/20")
            self.honggou_tab.current_quota = quota

    def logout(self):
        reply = QMessageBox.question(self, "Đăng xuất", "Bạn có chắc chắn muốn đăng xuất không?\n(Sẽ xóa thông tin tài khoản đã ghi nhớ)", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            if hasattr(self, '_hb_timer'): self._hb_timer.stop()
            settings = QSettings("BoomStudio", "ClientApp")
            settings.remove("username"); settings.remove("password")
            self.login_screen.inp_user.clear(); self.login_screen.inp_pass.clear() 
            if hasattr(self, 'honggou_tab') and self.honggou_tab.monitor_thread: self.honggou_tab.monitor_thread.stop()
            self.stack.setCurrentWidget(self.login_screen)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
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
