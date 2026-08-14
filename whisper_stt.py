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
import os, traceback
from PyQt6.QtCore import QThread, pyqtSignal

# faster-whisper import mềm — thiếu thì báo lỗi rõ ràng lúc chạy,
# không làm sập cả app khi mới mở.
try:
    from faster_whisper import WhisperModel
    _HAS_FW = True
    _FW_IMPORT_ERROR = ""
except Exception as _e:
    WhisperModel = None
    _HAS_FW = False
    # Lưu lỗi THẬT (thường là ctranslate2 thiếu DLL), không nuốt mất —
    # để lúc chạy in ra biết đúng bệnh: thiếu thư viện hay thiếu DLL.
    import traceback as _tb
    _FW_IMPORT_ERROR = "".join(_tb.format_exception_only(type(_e), _e)).strip()

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
        if not _HAS_FW:
            if _FW_IMPORT_ERROR:
                # Thư viện CÓ trong gói nhưng import lỗi (hay gặp: ctranslate2
                # thiếu DLL runtime). In lý do thật để sửa đúng chỗ.
                self._log("❌ Whisper không dùng được — thư viện có nhưng nạp lỗi.")
                self._log(f"   Lý do: {_FW_IMPORT_ERROR}")
                self._log("   (Nếu là ctranslate2/DLL: thiếu Visual C++ Runtime "
                          "hoặc build thiếu DLL — không phải chưa cài.)")
            else:
                self._log("❌ Chưa cài faster-whisper. Chạy:  pip install faster-whisper")
            self.finished_signal.emit(0, len(self.files))
            return

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
