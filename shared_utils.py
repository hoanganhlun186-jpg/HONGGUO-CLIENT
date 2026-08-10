import os, sys, threading, subprocess, re, shutil
import concurrent.futures
try:
    import requests
except ImportError:
    requests = None
from PyQt6.QtCore import QObject, pyqtSignal

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ====================================================================
# 0. ĐỊNH VỊ CÔNG CỤ NGOÀI (ffmpeg/ffprobe/ffplay/aria2c)
#    Thứ tự ưu tiên: bundle PyInstaller (_MEIPASS) -> cạnh file .exe
#    -> thư mục hiện tại -> PATH hệ thống. Nhờ vậy máy khách chỉ cần
#    có ffmpeg.exe nằm cạnh app là chạy, KHÔNG phụ thuộc PATH máy Anh.
# ====================================================================
def _tool_dirs():
    """Danh sách thư mục có thể chứa .exe kèm theo, theo thứ tự ưu tiên."""
    dirs = []
    # 1) Thư mục bundle tạm khi chạy .exe onefile (PyInstaller)
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        dirs.append(mei)
    # 2) Thư mục chứa file .exe thật (khi đóng gói) hoặc file script (khi chạy .py)
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(sys.executable))
    else:
        dirs.append(os.path.dirname(os.path.abspath(__file__)))
    # 3) Thư mục làm việc hiện tại
    dirs.append(os.path.abspath("."))
    # Khử trùng lặp, giữ thứ tự
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen:
            seen.add(d); out.append(d)
    return out

def find_tool(name):
    """
    Tìm 1 công cụ (vd 'ffmpeg', 'ffplay', 'ffprobe', 'aria2c').
    Trả về đường dẫn tuyệt đối nếu thấy file kèm theo, hoặc kết quả
    shutil.which nếu có trong PATH, hoặc None nếu không tìm thấy.
    """
    exe = name if name.lower().endswith(".exe") or os.name != "nt" else name + ".exe"
    for d in _tool_dirs():
        cand = os.path.join(d, exe)
        if os.path.exists(cand):
            return cand
    return shutil.which(name) or shutil.which(exe)

def get_ffmpeg_path():
    """Đường dẫn ffmpeg (kèm app ưu tiên trước PATH). Fallback 'ffmpeg'."""
    return find_tool("ffmpeg") or "ffmpeg"

def get_ffprobe_path():
    return find_tool("ffprobe") or "ffprobe"

def get_ffplay_path():
    """Đường dẫn ffplay, hoặc None nếu không tìm thấy."""
    return find_tool("ffplay")


# ====================================================================
# 1. THUMBNAIL CACHING & ASYNC LOADER (Tối ưu hóa CPU & RAM)
# ====================================================================
_image_cache = {}
_thumb_executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

class AsyncImageLoader(QObject):
    image_loaded = pyqtSignal(str, bytes)
    
    def __init__(self, url, vid_id):
        super().__init__()
        self.url = url
        self.vid_id = vid_id
        
    def start(self):
        if not self.url: 
            return
        if self.vid_id in _image_cache:
            self.image_loaded.emit(self.vid_id, _image_cache[self.vid_id])
            return
            
        def fetch_image():
            try:
                if requests is None: return
                resp = requests.get(self.url, timeout=5)
                if resp.status_code == 200:
                    _image_cache[self.vid_id] = resp.content
                    self.image_loaded.emit(self.vid_id, resp.content)
            except: 
                pass
                
        _thumb_executor.submit(fetch_image)

# ====================================================================
# 2. SINGLETON AI MODELS (Đảm bảo chỉ Load Model vào RAM 1 lần duy nhất)
# ====================================================================
_funasr_model = None
_whisper_model = None
_model_lock = threading.Lock()

def get_funasr_model(cb=None):
    global _funasr_model
    with _model_lock:
        if _funasr_model is None:
            if cb: cb("⚡ Đang nạp model FunASR (Paraformer Tiếng Trung) vào RAM...\n")
            from funasr import AutoModel
            
            # ĐÃ FIX: Thêm punc_model để AI biết ngắt câu và trả về timecode
            _funasr_model = AutoModel(
                model="paraformer-zh", 
                vad_model="fsmn-vad", 
                punc_model="ct-punc",
                trust_remote_code=True,
                disable_update=True
            )
        return _funasr_model

def get_whisper_model(cb=None):
    global _whisper_model
    with _model_lock:
        if _whisper_model is None:
            if cb: cb("🧠 Đang nạp model Whisper (Đa ngôn ngữ) vào RAM...\n")
            from faster_whisper import WhisperModel
            try: 
                _whisper_model = WhisperModel("small", device="cuda", compute_type="float16")
            except Exception: 
                _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
        return _whisper_model

def ms_to_srt_time(ms):
    ms = max(0, int(ms))
    h = ms // 3600000
    m = (ms % 3600000) // 60000
    s = (ms % 60000) // 1000
    milli = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"

# ====================================================================
# 3. ĐƯỜNG ỐNG XỬ LÝ: VIDEO -> AUDIO -> AI -> FILE .SRT
# ====================================================================
def generate_srt_pipeline(video_path, engine_mode=1, cb=None):
    if engine_mode == 0 or not os.path.exists(video_path): 
        return False
        
    wav_path = os.path.splitext(video_path)[0] + ".wav"
    srt_path = os.path.splitext(video_path)[0] + ".srt"

    if cb: cb("🎤 Đang tách âm thanh khỏi video...\n")
    cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", wav_path]
    kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
    subprocess.run(cmd, capture_output=True, **kw)

    try:
        if not os.path.exists(wav_path): 
            if cb: cb("❌ Không thể trích xuất âm thanh từ video.\n")
            return False
            
        lines = []
        idx = 1
        
        # ---------------------------------------------------------
        # CHẾ ĐỘ 1: TIẾNG TRUNG (FUNASR PARAFORMER)
        # ---------------------------------------------------------
        if engine_mode == 1: 
            if cb: cb("🤖 AI FunASR đang quét Timecode thực tế...\n")
            model = get_funasr_model(cb)
            
            # Gọi hàm với cờ sentence_timestamp=True
            result = model.generate(input=wav_path, batch_size_s=300, sentence_timestamp=True)
            
            if result and isinstance(result, list) and len(result) > 0:
                rec = result[0]
                
                # CÁCH 2 MỚI: ƯU TIÊN cắt theo ký tự và dấu câu để tránh gộp dòng
                if "timestamp" in rec and rec["timestamp"]:
                    raw_text = rec.get("text", "")
                    clean_text = re.sub(r'<\|.*?\|>', '', raw_text)
                    timestamps = rec.get("timestamp", [])
                    
                    punctuation = ['。', '？', '！', '，', '；', '：', '.', '?', '!', ',', ';']
                    current_sentence = ""
                    start_time = None
                    end_time = None
                    
                    ts_idx = 0
                    for char in clean_text:
                        if ts_idx < len(timestamps):
                            ts = timestamps[ts_idx]
                            if start_time is None: start_time = ts[0]
                            end_time = ts[1]
                            ts_idx += 1
                            
                        current_sentence += char
                        
                        # Cắt câu tại dấu chấm/phẩy hoặc max 30 chữ
                        if char in punctuation or len(current_sentence) > 30:
                            if current_sentence.strip() and start_time is not None and end_time is not None:
                                lines.extend([str(idx), f"{ms_to_srt_time(start_time)} --> {ms_to_srt_time(end_time)}", current_sentence.strip(), ""])
                                idx += 1
                            current_sentence = ""
                            start_time = None
                    
                    if current_sentence.strip() and start_time is not None and end_time is not None:
                        lines.extend([str(idx), f"{ms_to_srt_time(start_time)} --> {ms_to_srt_time(end_time)}", current_sentence.strip(), ""])
                        idx += 1
                        
                # CÁCH 1 CŨ: Lấy từ sentence_info (Chuyển xuống làm dự phòng)
                elif "sentence_info" in rec and rec["sentence_info"]:
                    for seg in rec["sentence_info"]:
                        text = re.sub(r'<\|.*?\|>', '', seg.get("text", "")).strip()
                        if not text: continue
                        start = int(seg.get("start", 0))
                        end = int(seg.get("end", 0))
                        lines.extend([str(idx), f"{ms_to_srt_time(start)} --> {ms_to_srt_time(end)}", text, ""])
                        idx += 1
                        
                else:
                    if cb: cb("❌ Thư viện FunASR trên máy bạn bị lỗi lấy Timecode. Hãy chuyển sang dùng Whisper!\n")

        # ---------------------------------------------------------
        # CHẾ ĐỘ 2: ĐA NGÔN NGỮ (WHISPER)
        # ---------------------------------------------------------
        elif engine_mode == 2:
            if cb: cb("🤖 AI Whisper đang quét Timecode...\n")
            model = get_whisper_model(cb)
            segments, info = model.transcribe(wav_path, beam_size=5, vad_filter=True, word_timestamps=False)
            for seg in segments:
                text = seg.text.strip()
                if not text: continue
                lines.extend([str(idx), f"{ms_to_srt_time(seg.start * 1000)} --> {ms_to_srt_time(seg.end * 1000)}", text, ""])
                idx += 1

        # ---------------------------------------------------------
        # TẠO FILE SRT
        # ---------------------------------------------------------
        if lines:
            with open(srt_path, "w", encoding="utf-8") as f: 
                f.write("\n".join(lines))
            if cb: cb(f"📝 Đã tạo thành công file Phụ đề: {os.path.basename(srt_path)}\n")
            return True
        else:
            if cb: cb(f"⚠️ Không có file SRT nào được tạo.\n")
            return False
            
    except Exception as e:
        if cb: cb(f"❌ Lỗi xử lý AI: {e}\n")
        return False
    finally:
        if os.path.exists(wav_path): 
            os.remove(wav_path)

# ====================================================================
# 4. MẮT THẦN DÒ CARD ĐỒ HỌA (Dùng cho Workflow Render)
# ====================================================================
def get_optimal_ffmpeg_codec():
    """Tự động dò tìm Card đồ họa trên Windows để trả về Codec nén video siêu tốc."""
    try:
        cmd_output = subprocess.check_output("wmic path win32_VideoController get name", shell=True, text=True)
        vga_name = cmd_output.lower()
        if "nvidia" in vga_name: return "h264_nvenc"
        elif "amd" in vga_name or "radeon" in vga_name: return "h264_amf"
        elif "intel" in vga_name: return "h264_qsv"
    except Exception: pass
    return "libx264"
