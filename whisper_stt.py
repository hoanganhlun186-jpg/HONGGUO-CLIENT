# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════
  whisper_stt.py — Tách phụ đề bằng faster-whisper (offline)
  ─────────────────────────────────────────────────────────
  Dùng cho VIDEO DÀI (nguyên tập drama) mà CapCut STT không
  làm được. Ưu điểm:
    • Không giới hạn thời lượng, không rate-limit, chạy offline
    • VAD (Silero) tích hợp: tự bỏ đoạn nhạc nền, chỉ nhận giọng
    • Tự dò GPU NVIDIA (cuda) → không có thì tự lùi về CPU

  Class WhisperSttThread khớp interface SttBatchThread:
    - __init__(files, src_lang, out_lang, use_trans, stt_workers, ...)
    - progress_signal = pyqtSignal(str)
    - finished_signal  = pyqtSignal(int, int)   # (ok, failed)
    - ghi .srt CẠNH video (base + ".srt") — bước dịch tự nhận.

  Cài đặt (1 lần):
    pip install faster-whisper
    # GPU (tùy chọn, nhanh hơn nhiều): cần CUDA 12 + cuDNN 9
═══════════════════════════════════════════════════════════
"""
import os, traceback, sys, zipfile, urllib.request
from PyQt6.QtCore import QThread, pyqtSignal

# ═══════════════════════════════════════════════════════════════════
#  GÓI WHISPER TẢI RIÊNG (không nhồi vào build .exe để tránh Nuitka crash)
#  Thư viện faster-whisper + av + ctranslate2 + onnxruntime được đóng gói
#  sẵn thành 1 file zip, up lên Google Drive. Khách bấm Whisper lần đầu
#  -> tự tải về, giải nén vào thư mục app, rồi import.
# ═══════════════════════════════════════════════════════════════════
# ⚠️ ĐỔI link này thành link tải TRỰC TIẾP của bạn trên Google Drive.
#    Cách lấy link trực tiếp: up file whisper_pack.zip lên Drive -> chia sẻ
#    "Bất kỳ ai có liên kết" -> lấy FILE_ID -> dùng dạng:
#    https://drive.google.com/uc?export=download&id=FILE_ID
WHISPER_PACK_URL = "https://drive.usercontent.google.com/download?id=1GFaQDQR10nzd5cLl5ZH4qT6I7bgEGyB0&export=download&confirm=t"

def _whisper_libs_dir():
    """Thư mục chứa thư viện Whisper đã giải nén (cạnh app)."""
    base = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
    return os.path.join(base, "whisper_libs")

def _try_import_fw():
    """Thử import faster_whisper. Trả về (WhisperModel, error_str)."""
    try:
        from faster_whisper import WhisperModel
        return WhisperModel, ""
    except Exception as e:
        import traceback as _tb
        return None, "".join(_tb.format_exception_only(type(e), e)).strip()

def download_whisper_pack(progress=None):
    """Tải gói whisper_pack.zip từ Drive về + giải nén. Trả về True nếu OK.
    progress: hàm nhận chuỗi để báo tiến độ (tùy chọn)."""
    def _log(m):
        if progress:
            progress(m)
    libs = _whisper_libs_dir()
    zip_path = os.path.join(os.path.dirname(libs), "whisper_pack.zip")
    try:
        _log("⏳ Đang tải gói Whisper (~100MB), lần đầu hơi lâu...")

        req = urllib.request.Request(WHISPER_PACK_URL,
              headers={"User-Agent": "Mozilla/5.0"})
        downloaded = 0
        with urllib.request.urlopen(req) as resp, open(zip_path, "wb") as f:
            while True:
                chunk = resp.read(512 * 1024)  # 512KB
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                _log(f"⏳ Đang tải... {downloaded // 1_000_000} MB")

        _log("📦 Đang giải nén gói Whisper...")
        os.makedirs(libs, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(libs)
        try:
            os.remove(zip_path)
        except Exception:
            pass
        _log("✅ Đã cài xong gói Whisper!")
        return True
    except Exception as e:
        _log(f"❌ Tải gói Whisper lỗi: {str(e)[:120]}")
        return False

# faster-whisper import mềm. Nếu thiếu, thử thêm thư mục whisper_libs (đã tải
# từ Drive) vào sys.path rồi import lại.
WhisperModel, _FW_IMPORT_ERROR = _try_import_fw()
if WhisperModel is None:
    _libs = _whisper_libs_dir()
    if os.path.isdir(_libs) and _libs not in sys.path:
        sys.path.insert(0, _libs)
        WhisperModel, _FW_IMPORT_ERROR = _try_import_fw()
_HAS_FW = WhisperModel is not None

# Map ngôn ngữ của app (zh-CN, en-US...) → mã Whisper (zh, en...)
_LANG_MAP = {
    "zh-CN": "zh", "zh": "zh",
    "en-US": "en", "en": "en",
    "ko-KR": "ko", "ko": "ko",
    "ja-JP": "ja", "ja": "ja",
    "vi-VN": "vi", "vi": "vi",
}

# Cache model theo (name, device, compute) — nạp 1 lần, tái dùng cho
# cả batch, tránh load lại mỗi video (rất tốn thời gian/VRAM).
_MODEL_CACHE = {}


def _pick_device():
    """Tự dò: có CUDA (GPU NVIDIA) thì dùng, không thì CPU.
    Trả về (device, compute_type)."""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            # float16 nhanh & nhẹ VRAM trên GPU
            return "cuda", "float16"
    except Exception:
        pass
    # CPU: int8 nhẹ RAM & nhanh hơn float32 đáng kể, độ chính xác gần như
    # không đổi cho tác vụ phụ đề.
    return "cpu", "int8"


def _get_model(model_name, progress=None):
    """Nạp (hoặc lấy từ cache) WhisperModel. Lần đầu sẽ TẢI model về máy
    (~small 480MB / medium 1.5GB / large-v3 3GB) rồi lưu lại dùng mãi."""
    device, compute = _pick_device()
    key = (model_name, device, compute)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key], device
    if progress:
        progress(f"⏳ Nạp model Whisper '{model_name}' ({device}, {compute})... "
                 f"(lần đầu sẽ tải model về máy, hãy đợi)")
    model = WhisperModel(model_name, device=device, compute_type=compute)
    _MODEL_CACHE[key] = model
    return model, device


def _fmt_ts(seconds):
    """Giây (float) → 'HH:MM:SS,mmm' cho srt."""
    if seconds < 0:
        seconds = 0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_srt(segments, srt_path):
    """Ghi list segment (mỗi cái có .start/.end/.text) ra file srt.
    Trả về số dòng thoại đã ghi."""
    n = 0
    lines = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        n += 1
        lines.append(str(n))
        lines.append(f"{_fmt_ts(seg.start)} --> {_fmt_ts(seg.end)}")
        lines.append(text)
        lines.append("")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return n


def transcribe_one(video_path, model_name="small", src_lang="zh",
                   progress=None):
    """Tách phụ đề 1 video → ghi <base>.srt cạnh video.
    Trả về (ok: bool, so_dong: int). so_dong=0 nghĩa tập không thoại."""
    model, device = _get_model(model_name, progress)
    lang = _LANG_MAP.get(src_lang, src_lang)  # 'zh-CN' -> 'zh'
    name = os.path.basename(video_path)
    if progress:
        progress(f"🎧 [{device}] Đang nghe: {name}")

    # vad_filter=True → dùng Silero VAD tự lọc đoạn không có giọng người
    # (nhạc nền, tiếng động), nên KHÔNG bị cắt nhầm giữa câu. Đây chính là
    # cái CapCut/silencedetect không làm được với drama có nhạc nền.
    segments, info = model.transcribe(
        video_path,
        language=lang,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        beam_size=5,
    )

    base, _ext = os.path.splitext(video_path)
    srt_path = base + ".srt"
    n = _write_srt(segments, srt_path)
    if progress:
        if n == 0:
            progress(f"🔇 {name}: không phát hiện thoại (tập không lời).")
        else:
            progress(f"✅ {name}: {n} dòng phụ đề → {os.path.basename(srt_path)}")
    return True, n


class WhisperSttThread(QThread):
    """Thread tách sub bằng Whisper — khớp interface SttBatchThread để
    _start_stt() dùng thay thế mà không sửa luồng dịch/lồng phía sau.

    Lưu ý: chạy TUẦN TỰ từng video (stt_workers bị bỏ qua) vì model dùng
    chung GPU/CPU — chạy song song không nhanh hơn mà dễ hết VRAM.
    """
    progress_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(int, int)   # (ok, failed)

    def __init__(self, files, src_lang="zh-CN", out_lang="vi-VN",
                 use_trans=False, stt_workers=1, model_name="small",
                 parent=None):
        super().__init__(parent)
        self.files = list(files)
        self.src_lang = src_lang
        self.model_name = model_name
        self._stop = False

    def stop(self):
        self._stop = True

    def _log(self, msg):
        self.progress_signal.emit(msg)

    def run(self):
        global WhisperModel, _HAS_FW, _FW_IMPORT_ERROR
        if not _HAS_FW:
            # Chưa có thư viện Whisper -> TỰ TẢI gói từ Google Drive rồi thử lại.
            self._log("⚙️ Whisper chưa sẵn sàng — đang tự tải bộ thư viện Whisper...")
            ok_dl = download_whisper_pack(progress=lambda m: self._log(m))
            if ok_dl:
                # Thêm thư mục vừa giải nén vào path và import lại
                _libs = _whisper_libs_dir()
                if _libs not in sys.path:
                    sys.path.insert(0, _libs)
                WhisperModel, _FW_IMPORT_ERROR = _try_import_fw()
                _HAS_FW = WhisperModel is not None
            if not _HAS_FW:
                self._log("❌ Vẫn chưa dùng được Whisper sau khi tải.")
                if _FW_IMPORT_ERROR:
                    self._log(f"   Lý do: {_FW_IMPORT_ERROR}")
                self._log("   ➤ Kiểm tra mạng, hoặc báo admin cập nhật link gói Whisper.")
                self.finished_signal.emit(0, len(self.files))
                return
            self._log("✅ Whisper đã sẵn sàng, bắt đầu tách sub...")

        ok = failed = 0
        total = len(self.files)
        for i, vp in enumerate(self.files, 1):
            if self._stop:
                self._log("⏹ Đã dừng theo yêu cầu.")
                break
            self._log(f"── [{i}/{total}] ──")
            try:
                success, _n = transcribe_one(
                    vp, model_name=self.model_name,
                    src_lang=self.src_lang, progress=self._log)
                if success:
                    ok += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                self._log(f"❌ Lỗi {os.path.basename(vp)}: {e}")
                self._log(traceback.format_exc(limit=2))
        self.finished_signal.emit(ok, failed)


if __name__ == "__main__":
    print("=" * 50)
    print("TEST TẢI WHISPER PACK TỪ GOOGLE DRIVE")
    print("=" * 50)
    ok = download_whisper_pack(progress=lambda m: print(m))
    if ok:
        print("\n✅ Tải và giải nén thành công!")
        # Thêm vào path và thử import
        _libs = _whisper_libs_dir()
        if _libs not in sys.path:
            sys.path.insert(0, _libs)
        model, err = _try_import_fw()
        if model:
            print("✅ Import faster_whisper thành công!")
        else:
            print(f"❌ Import thất bại: {err}")
    else:
        print("\n❌ Tải thất bại!")
