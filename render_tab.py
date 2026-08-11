"""
═══════════════════════════════════════════════════════════
  RENDER TAB — Tab render/edit video cho Hongguo
  ─────────────────────────────────────────────────────────
  Tách từ workflow_tab (bỏ Dịch + TTS), chỉ giữ:
    • Grid tự nhận diện & ghép cặp video + srt (ưu tiên
      *_dubbed.mp4 + *_vi.srt, không có thì dùng gốc)
    • Panel Design: font/màu sub, khung mờ, overlay PNG,
      logo kênh, bộ lọc Bypass FX
    • Preview canvas (kéo thả chữ/logo, xem video)
    • Render hàng loạt bằng ffmpeg (GPU nếu có)
═══════════════════════════════════════════════════════════
"""
import os, sys, subprocess, re, shutil, time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea, QFileDialog, QTextEdit, QProgressBar,
    QComboBox, QLineEdit, QSpinBox, QMessageBox, QCheckBox, QSlider,
    QTabWidget, QDoubleSpinBox, QGridLayout,
    QGraphicsScene, QGraphicsView, QGraphicsTextItem, QGraphicsRectItem,
    QGraphicsPixmapItem, QGraphicsItem, QStyle, QApplication
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSettings, QUrl, QPointF, QRectF, QTimer
from PyQt6.QtGui import QCursor, QTextCursor, QFont, QPixmap, QPen, QBrush, QColor, QPainter
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem

# Tiện ích dùng chung (ffmpeg path, codec, cờ ẩn cửa sổ). Ưu tiên lấy từ
# shared_utils của app; nếu chạy lẻ không có thì tự fallback.
try:
    from shared_utils import (get_ffmpeg_path, get_optimal_ffmpeg_codec,
                              CREATE_NO_WINDOW)
except Exception:
    CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
    def get_ffmpeg_path(): return shutil.which("ffmpeg") or "ffmpeg"
    def get_optimal_ffmpeg_codec(): return "libx264"


FONTS_LIST = ["Arial", "Tahoma", "Verdana", "Times New Roman", "Segoe UI", "Impact", "Consolas", "Courier New"]

COLOR_PRESETS = {
    "Vàng (Yellow)":    {"ass": "&H0000FFFF", "qt": "#FFFF00"},
    "Trắng (White)":    {"ass": "&H00FFFFFF", "qt": "#FFFFFF"},
    "Đỏ (Red)":         {"ass": "&H000000FF", "qt": "#FF0000"},
    "Xanh lá (Green)":  {"ass": "&H0000FF00", "qt": "#00FF00"},
    "Xanh biển (Blue)": {"ass": "&H00FF0000", "qt": "#0000FF"},
    "Cam (Orange)":     {"ass": "&H0000A5FF", "qt": "#FFA500"},
    "Hồng (Pink)":      {"ass": "&H00FF00FF", "qt": "#FF00FF"},
    "Đen (Black)":      {"ass": "&H00000000", "qt": "#000000"},
}


def format_time(seconds):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"

def _escape_ffmpeg_path(path):
    p = path.replace('\\', '/')
    for ch in [":", "'", "[", "]", ",", ";"]: p = p.replace(ch, f"\\{ch}")
    return p

def _merge_srt_intervals(srt_path, gap=0.3, expand=0.5, max_intervals=150):
    try:
        with open(srt_path, "r", encoding="utf-8") as f: srt_text = f.read()
        times = re.findall(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})", srt_text)
        if not times: return []
        def to_sec(t):
            h, m, s = t.replace(",", ".").split(":")
            return float(h)*3600 + float(m)*60 + float(s)
        raw = [(max(0.0, to_sec(s) - expand), to_sec(e) + expand) for s, e in times]
        raw.sort(key=lambda x: x[0])

        def _merge(items, g):
            out = [items[0]]
            for s, e in items[1:]:
                if s <= out[-1][1] + g: out[-1] = (out[-1][0], max(out[-1][1], e))
                else: out.append((s, e))
            return out

        merged = _merge(raw, gap)
        g = gap
        while len(merged) > max_intervals and g < 20.0:
            g += 0.5
            merged = _merge(raw, g)
        return merged
    except Exception: return []

# ============================================================
# CÁC LUỒNG XỬ LÝ
# ============================================================

class SingleRenderThread(QThread):
    log = pyqtSignal(str); done = pyqtSignal(bool)
    def __init__(self, vp, vi_srt_path, tts_path, out_path, render_cfg):
        super().__init__(); self.vp = vp; self.sp = vi_srt_path; self.tts_path = tts_path; self.op = out_path; self.cfg = render_cfg; self._cancel = False
    def cancel(self): self._cancel = True
    
    def run(self):
        start_t = time.time() 
        self.log.emit(f"🎬 Bắt đầu Render & Ép phụ đề...\n")
        quality_text = self.cfg.get("render_quality", "⭐ Tốt (CRF 20)")
        self.log.emit(f"   📊 Chất lượng: {quality_text} | Codec: {get_optimal_ffmpeg_codec()}\n")
        codec = get_optimal_ffmpeg_codec()

        has_audio = False
        try:
            ffmpeg_bin = get_ffmpeg_path()
            probe = subprocess.run([ffmpeg_bin, "-i", self.vp], stderr=subprocess.PIPE, text=True, errors="ignore", creationflags=CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if "Audio:" in probe.stderr:
                has_audio = True
        except: pass
        
        vid_w = int(self.cfg["scene_w"])
        vid_h = int(self.cfg["scene_h"])
        
        m_l, m_r, m_v = max(0, int(self.cfg["margin_l"])), max(0, int(self.cfg["margin_r"])), max(0, int(self.cfg["margin_v"]))
        f_size = max(10, int(self.cfg["font_size"]))
        f_color = self.cfg.get("font_color", "&H0000FFFF")

        # Nền ô chữ (opaque box) sau chữ: BorderStyle=3 + BackColour là màu nền.
        # Nếu tắt -> viền thường (BorderStyle=1, outline đen).
        if self.cfg.get("subbox_en"):
            back = self.cfg.get("subbox_color", "&H80000000")  # mặc định đen mờ ~50%
            style = (f"FontName={self.cfg['font_name']},FontSize={f_size},PrimaryColour={f_color},"
                     f"OutlineColour={back},BackColour={back},BorderStyle=3,Outline=6,Shadow=0,"
                     f"Alignment=2,PlayResX={vid_w},PlayResY={vid_h},MarginL={m_l},MarginR={m_r},MarginV={m_v}")
        else:
            style = (f"FontName={self.cfg['font_name']},FontSize={f_size},PrimaryColour={f_color},"
                     f"OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,"
                     f"Alignment=2,PlayResX={vid_w},PlayResY={vid_h},MarginL={m_l},MarginR={m_r},MarginV={m_v}")

        basename = os.path.basename(self.vp)
        temp_srt = ""; escaped_srt = ""
        if self.sp and os.path.exists(self.sp):
            temp_srt = os.path.join(os.path.dirname(self.op), f"_temp_sub_{basename}.srt")
            try: shutil.copy2(self.sp, temp_srt); escaped_srt = _escape_ffmpeg_path(temp_srt)
            except Exception as e: self.log.emit(f"⚠️ Không thể copy SRT tạm: {e}\n"); escaped_srt = _escape_ffmpeg_path(self.sp)

        inputs = ["-hwaccel", "none", "-threads", "0", "-i", self.vp]
        filter_chains = []
        last_vid_out = "[0:v]"
        vid_filters = []
        
        if self.cfg.get("rotate_en"):
            vid_filters.append("rotate=1*PI/180:ow=iw:oh=ih")
            
        if self.cfg.get("blur_en") and self.cfg.get("blur_list"):
            enable_cmd = ""
            orig_srt = os.path.splitext(self.vp)[0] + ".srt"
            srt_for_blur = orig_srt if os.path.exists(orig_srt) else self.sp
            if srt_for_blur and os.path.exists(srt_for_blur):
                merged = _merge_srt_intervals(srt_for_blur, gap=0.3, expand=0.5)
                if merged:
                    covered = sum(e - s for s, e in merged)
                    span = merged[-1][1] - merged[0][0]
                    if span > 0 and covered / span > 0.85:
                        self.log.emit(f"   ℹ️ Thoại dày ({len(merged)} đoạn, phủ {covered/span*100:.0f}%) → làm mờ liên tục cho nhanh.\n")
                    else:
                        intervals = [f"between(t,{s:.3f},{e:.3f})" for s, e in merged]
                        enable_cmd = f":enable='{'+'.join(intervals)}'"
                        self.log.emit(f"   ℹ️ Vùng mờ: {len(merged)} đoạn theo phụ đề.\n")
                    
            for b in self.cfg["blur_list"]:
                bx = max(1, min(int(b['x']), vid_w - 3))
                by = max(1, min(int(b['y']), vid_h - 3))
                bw = max(2, min(int(b['w']), vid_w - bx - 1))
                bh = max(2, min(int(b['h']), vid_h - by - 1))
                vid_filters.append(f"delogo=x={bx}:y={by}:w={bw}:h={bh}{enable_cmd}")
            
        if self.cfg.get("flip"): vid_filters.append("hflip")
        if self.cfg.get("zoom"): vid_filters.append("crop=iw*0.96:ih*0.96,scale=trunc(iw/2)*2:trunc(ih/2)*2")
        if self.cfg.get("color"): vid_filters.append("eq=contrast=1.05:brightness=0.02:saturation=1.1")
        if self.cfg.get("noise"): vid_filters.append("noise=alls=1:allf=t")
        
        vid_filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
        
        if vid_filters: 
            filter_chains.append(f"[0:v] {','.join(vid_filters)} [v_base]")
            last_vid_out = "[v_base]"

        if self.cfg.get("frame_en") and self.cfg.get("frame_path") and os.path.exists(self.cfg.get("frame_path")):
            frame_idx = inputs.count("-i")
            inputs.extend(["-loop", "1", "-i", self.cfg["frame_path"]])
            filter_chains.append(f"[{frame_idx}:v] format=yuva420p,scale={vid_w}:{vid_h} [frame_s]")
            filter_chains.append(f"{last_vid_out}[frame_s] overlay=0:0:shortest=1 [v_framed]")
            last_vid_out = "[v_framed]"

        if self.cfg.get("logo_en") and self.cfg.get("logo_path") and os.path.exists(self.cfg.get("logo_path")):
            try:
                logo_idx = inputs.count("-i")
                inputs.extend(["-loop", "1", "-i", self.cfg["logo_path"]])
                lx, ly = int(self.cfg["logo_x"]), int(self.cfg["logo_y"]); logo_scale = self.cfg.get("logo_scale", 1.0)
                if abs(logo_scale - 1.0) > 0.01: 
                    filter_chains.append(f"[{logo_idx}:v] format=yuva420p,scale=iw*{logo_scale:.3f}:ih*{logo_scale:.3f} [logo_s]")
                else:
                    filter_chains.append(f"[{logo_idx}:v] format=yuva420p [logo_s]")
                filter_chains.append(f"{last_vid_out}[logo_s] overlay=x={lx}:y={ly}:shortest=1 [v_logo]")
                last_vid_out = "[v_logo]"
            except Exception as e:
                self.log.emit(f"⚠️ Lỗi chèn logo vào filter chain, bỏ qua logo: {e}\n")

        if escaped_srt and self.cfg.get("hardsub_en", True): 
            filter_chains.append(f"{last_vid_out} subtitles='{escaped_srt}':force_style='{style}' [vout]")
            last_vid_out = "[vout]"
            
        if self.cfg.get("speed"): 
            filter_chains.append(f"{last_vid_out} setpts=PTS/1.05 [v_speed]")
            last_vid_out = "[v_speed]"

        audio_map = ""
        if self.cfg.get("tts_en") and self.tts_path and os.path.exists(self.tts_path):
            tts_idx = inputs.count("-i")
            inputs.extend(["-i", self.tts_path])
            tts_v = self.cfg.get("tts_ai_vol", 150) / 100.0
            orig_v = self.cfg.get("tts_orig_vol", 15) / 100.0
            
            # ĐÃ FIX TRIỆT ĐỂ: Nếu có audio gốc VÀ Vol gốc > 0 thì mới mix, ngược lại bỏ qua hoàn toàn audio gốc để tránh lỗi file hỏng
            if has_audio and orig_v > 0:
                filter_chains.append(f"[0:a]aformat=channel_layouts=stereo,volume={orig_v:.2f}[a_orig];[{tts_idx}:a]aformat=channel_layouts=stereo,volume={tts_v:.2f}[a_tts];[a_orig][a_tts]amix=inputs=2:duration=first[a_mixed]")
                audio_map = "[a_mixed]"
            else:
                filter_chains.append(f"[{tts_idx}:a]aformat=channel_layouts=stereo,volume={tts_v:.2f}[a_mixed]")
                audio_map = "[a_mixed]"
        else:
            if has_audio:
                orig_v = self.cfg.get("tts_orig_vol", 15) / 100.0
                if orig_v > 0:
                    audio_map = "0:a"

        af_chain = []
        speed_val = 1.05 if self.cfg.get("speed") else 1.0
        pitch_val = 1.15 if self.cfg.get("pitch") else 1.0
        
        if self.cfg.get("pitch"):
            af_chain.append(f"aresample=48000,asetrate=48000*{pitch_val},aresample=48000,atempo={speed_val}/{pitch_val}")
        elif self.cfg.get("speed"):
            af_chain.append(f"atempo={speed_val}")
            
        if af_chain and audio_map: 
            pad = audio_map if audio_map.startswith("[") else f"[{audio_map}]"
            filter_chains.append(f"{pad} {','.join(af_chain)} [aout]")
            audio_map = "[aout]"
            
        cmd = ["ffmpeg", "-y"] + inputs; temp_filter = ""
        
        if filter_chains:
            basename = os.path.basename(self.vp)
            temp_filter = os.path.join(os.path.dirname(self.op), f"_temp_filter_{basename}.txt")
            with open(temp_filter, "w", encoding="utf-8") as f: f.write(";".join(filter_chains))
            
            vid_out_map = "0:v" if last_vid_out == "[0:v]" else last_vid_out
            cmd.extend(["-filter_complex_script", temp_filter, "-map", vid_out_map])
            
            if audio_map: cmd.extend(["-map", audio_map])
            
            quality_text = self.cfg.get("render_quality", "⭐ Tốt (CRF 20 - Đề xuất)")
            if "CRF 16" in quality_text:
                crf_val, preset_sw, preset_hw = 16, "slow", "quality"
            elif "CRF 20" in quality_text:
                crf_val, preset_sw, preset_hw = 20, "medium", "quality"
            elif "CRF 26" in quality_text:
                crf_val, preset_sw, preset_hw = 26, "medium", "speed"
            else:
                crf_val, preset_sw, preset_hw = 0, "fast", "speed"
            
            use_crf = crf_val > 0
            
            if "amf" in codec.lower() or "nvenc" in codec.lower() or "qsv" in codec.lower(): 
                cmd.extend(["-c:v", codec])
                if audio_map: cmd.extend(["-c:a", "aac", "-b:a", "192k"])
                
                if use_crf:
                    if "nvenc" in codec.lower():
                        # Dịch preset riêng cho NVIDIA để không báo lỗi
                        nv_preset = "hq" if preset_hw == "quality" else "fast"
                        cmd.extend(["-rc", "constqp", "-qp", str(crf_val), "-preset", nv_preset])
                    elif "amf" in codec.lower():
                        cmd.extend(["-rc", "cqp", "-qp_i", str(crf_val), "-qp_p", str(crf_val), "-quality", preset_hw])
                    elif "qsv" in codec.lower():
                        cmd.extend(["-global_quality", str(crf_val), "-preset", preset_hw])
                else:
                    if "nvenc" in codec.lower():
                        nv_preset = "hq" if preset_hw == "quality" else "fast"
                        cmd.extend(["-b:v", "1000k", "-preset", nv_preset])
                    else:
                        cmd.extend(["-b:v", "1000k", "-preset", preset_hw])
                cmd.extend(["-movflags", "+faststart", self.op])
            else: 
                cmd.extend(["-c:v", codec])
                if audio_map: cmd.extend(["-c:a", "aac", "-b:a", "192k"])
                if use_crf:
                    cmd.extend(["-crf", str(crf_val), "-preset", preset_sw])
                else:
                    cmd.extend(["-b:v", "1000k", "-preset", preset_sw])
                cmd.extend(["-movflags", "+faststart", self.op])
        else:
            cmd.extend(["-map", "0:v"])
            if audio_map: cmd.extend(["-map", audio_map, "-c", "copy"])
            else: cmd.extend(["-c:v", "copy"])
            cmd.extend(["-movflags", "+faststart", self.op])

        try:
            self.log.emit(f"  ⚙️ Đang xử lý FFmpeg, vui lòng đợi...\n")
            kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **kw)
            from collections import deque
            stderr_lines = deque(maxlen=40)
            last_report = time.time()
            for line in proc.stderr:
                stderr_lines.append(line)
                if "time=" in line and time.time() - last_report > 20:
                    m = re.search(r"time=(\d+:\d+:\d+)", line)
                    if m: self.log.emit(f"   ⏳ Render: {m.group(1)}\n")
                    last_report = time.time()
                if self._cancel:
                    try: proc.terminate()
                    except Exception: pass
                    break
            proc.wait() 
            elapsed = time.time() - start_t
            if self._cancel:
                self.log.emit(f"⛔ Đã hủy render.\n")
                try:
                    if os.path.exists(self.op): os.remove(self.op)
                except Exception: pass
                self.done.emit(False)
            elif proc.returncode == 0:
                self.log.emit(f"⏱️ Render hoàn thành trong: {format_time(elapsed)}\n")
                self.done.emit(True)
            else:
                tail = "".join(list(stderr_lines)[-15:])
                self.log.emit(f"❌ FFmpeg lỗi (code {proc.returncode})\n📋 Chi tiết:\n{tail}\n"); self.done.emit(False)
        except Exception as e:
            self.log.emit(f"❌ Lỗi: {e}\n"); self.done.emit(False)
        finally:
            for tmp in [temp_filter, temp_srt]:
                try:
                    if tmp and os.path.exists(tmp): os.remove(tmp)
                except Exception: pass

# ============================================================
# CÁC CLASS ĐỒ HỌA
# ============================================================
_HS = 8; _HH = _HS / 2
def _inset_corners(rect):
    r = rect; s = _HS
    return {"tl": QRectF(r.left(), r.top(), s, s), "tr": QRectF(r.right() - s, r.top(), s, s), "bl": QRectF(r.left(), r.bottom() - s, s, s), "br": QRectF(r.right() - s, r.bottom() - s, s, s)}

def _draw_handles(painter, handle_dict):
    painter.setPen(QPen(QColor(255, 255, 255, 200), 1)); painter.setBrush(QBrush(QColor(255, 255, 255, 200)))
    for hr in handle_dict.values(): painter.drawRect(hr)

class DraggableBlurBox(QGraphicsRectItem):
    _VIS = 12; _HIT = 24   
    def __init__(self, x, y, w, h):
        super().__init__(0, 0, w, h)
        self.setPos(x, y)
        self.setPen(QPen(QColor(255, 40, 40, 255), 2.5, Qt.PenStyle.DashLine))
        self.setBrush(QBrush(QColor(255, 0, 0, 40)))
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setZValue(1)
        self._resizing = False; self._handle = None; self._start_scene = QPointF(); self._start_pos = QPointF(); self._start_rect = QRectF()
    def _build_handles(self, rect, size):
        r = rect; hs = size / 2; cx, cy = r.center().x(), r.center().y()
        return {"tl": QRectF(r.left() - hs, r.top() - hs, size, size), "tm": QRectF(cx - hs, r.top() - hs, size, size), "tr": QRectF(r.right() - hs, r.top() - hs, size, size), "ml": QRectF(r.left() - hs, cy - hs, size, size), "mr": QRectF(r.right() - hs, cy - hs, size, size), "bl": QRectF(r.left() - hs, r.bottom() - hs, size, size), "bm": QRectF(cx - hs, r.bottom() - hs, size, size), "br": QRectF(r.right() - hs, r.bottom() - hs, size, size)}
    def boundingRect(self):
        br = super().boundingRect(); m = self._HIT / 2 + 2; return br.adjusted(-m, -m, m, m)
    def paint(self, painter, option, widget=None):
        painter.setPen(self.pen()); painter.setBrush(self.brush()); painter.drawRect(self.rect())
        if self.isSelected():
            vis = self._build_handles(self.rect(), self._VIS); painter.setPen(QPen(QColor(255, 255, 255, 220), 1)); painter.setBrush(QBrush(QColor(255, 80, 80, 220)))
            for hr in vis.values(): painter.drawRect(hr)
    def mousePressEvent(self, event):
        if self.isSelected() and event.button() == Qt.MouseButton.LeftButton:
            for name, hr in self._build_handles(self.rect(), self._HIT).items():
                if hr.contains(event.pos()):
                    self._resizing = True; self._handle = name; self._start_scene = event.scenePos(); self._start_pos = QPointF(self.pos()); self._start_rect = QRectF(self.rect()); event.accept(); return
        super().mousePressEvent(event)
    def mouseMoveEvent(self, event):
        if self._resizing:
            delta = event.scenePos() - self._start_scene; h = self._handle; ox, oy = self._start_pos.x(), self._start_pos.y(); ow, oh = self._start_rect.width(), self._start_rect.height(); nx, ny, nw, nh = ox, oy, ow, oh
            if "l" in h: nx = ox + delta.x(); nw = ow - delta.x()
            if "r" in h: nw = ow + delta.x()
            if "t" in h: ny = oy + delta.y(); nh = oh - delta.y()
            if "b" in h: nh = oh + delta.y()
            if nw < 20: nw = 20; nx = ox + ow - 20 if "l" in h else nx
            if nh < 20: nh = 20; ny = oy + oh - 20 if "t" in h else ny
            self.prepareGeometryChange(); self.setPos(nx, ny); self.setRect(0, 0, nw, nh); event.accept(); return
        super().mouseMoveEvent(event)
    def mouseReleaseEvent(self, event):
        if self._resizing: self._resizing = False; self._handle = None; event.accept(); return
        super().mouseReleaseEvent(event)

class ScalablePixmapItem(QGraphicsPixmapItem):
    def __init__(self):
        super().__init__(); self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable); self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache); self._resizing = False; self._handle = None; self._start_scene = QPointF(); self._start_scale = 1.0; self._start_diag = 1.0
    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        if self.isSelected() and not self.pixmap().isNull(): _draw_handles(painter, _inset_corners(super().boundingRect()))
    def _anchor_scene(self, handle):
        br = super().boundingRect(); opp = {"tl": br.bottomRight(), "tr": br.bottomLeft(), "bl": br.topRight(), "br": br.topLeft()}; return self.mapToScene(opp.get(handle, br.center()))
    def mousePressEvent(self, event):
        if self.isSelected() and event.button() == Qt.MouseButton.LeftButton:
            br = super().boundingRect()
            if not br.isEmpty():
                for name, hr in _inset_corners(br).items():
                    if hr.contains(event.pos()): self._resizing = True; self._handle = name; self._start_scene = event.scenePos(); self._start_scale = self.scale(); anchor = self._anchor_scene(name); self._start_diag = max(1.0, (self._start_scene - anchor).manhattanLength()); event.accept(); return
        super().mousePressEvent(event)
    def mouseMoveEvent(self, event):
        if self._resizing: anchor = self._anchor_scene(self._handle); cur_diag = max(1.0, (event.scenePos() - anchor).manhattanLength()); ratio = cur_diag / self._start_diag; new_scale = max(0.1, min(10.0, self._start_scale * ratio)); self.setScale(new_scale); event.accept(); return
        super().mouseMoveEvent(event)
    def mouseReleaseEvent(self, event):
        if self._resizing: self._resizing = False; event.accept(); return
        super().mouseReleaseEvent(event)

class ScalableTextItem(QGraphicsTextItem):
    def __init__(self, text=""):
        super().__init__(text); self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable); self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache); self._resizing = False; self._handle = None; self._start_scene = QPointF(); self._start_scale = 1.0; self._start_diag = 1.0
    def paint(self, painter, option, widget=None):
        option.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, option, widget)
        if self.isSelected():
            painter.setPen(QPen(QColor(255, 255, 255, 80), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(super().boundingRect())
            _draw_handles(painter, _inset_corners(super().boundingRect()))
    def _anchor_scene(self, handle):
        br = super().boundingRect(); opp = {"tl": br.bottomRight(), "tr": br.bottomLeft(), "bl": br.topRight(), "br": br.topLeft()}; return self.mapToScene(opp.get(handle, br.center()))
    def mousePressEvent(self, event):
        if self.isSelected() and event.button() == Qt.MouseButton.LeftButton:
            for name, hr in _inset_corners(super().boundingRect()).items():
                if hr.contains(event.pos()): self._resizing = True; self._handle = name; self._start_scene = event.scenePos(); self._start_scale = self.scale(); anchor = self._anchor_scene(name); self._start_diag = max(1.0, (self._start_scene - anchor).manhattanLength()); event.accept(); return
        super().mousePressEvent(event)
    def mouseMoveEvent(self, event):
        if self._resizing: anchor = self._anchor_scene(self._handle); cur_diag = max(1.0, (event.scenePos() - anchor).manhattanLength()); ratio = cur_diag / self._start_diag; new_scale = max(0.15, min(8.0, self._start_scale * ratio)); self.setScale(new_scale); event.accept(); return
        super().mouseMoveEvent(event)
    def mouseReleaseEvent(self, event):
        if self._resizing: self._resizing = False; event.accept(); return
        super().mouseReleaseEvent(event)

class PreviewGraphicsView(QGraphicsView):
    def __init__(self, scene, parent=None):
        super().__init__(scene, parent); self.setStyleSheet("background: #000; border-radius: 6px; border: none;"); self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); self.setRenderHint(QPainter.RenderHint.Antialiasing); self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform); self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
    def wheelEvent(self, event):
        item = self.scene().focusItem() or next(iter(self.scene().selectedItems()), None)
        if item and isinstance(item, (ScalablePixmapItem, ScalableTextItem)):
            factor = 1.1 if event.angleDelta().y() > 0 else 0.9; new_scale = max(0.15, min(8.0, item.scale() * factor)); item.setScale(new_scale); event.accept()
        elif item and isinstance(item, DraggableBlurBox):
            delta = 10 if event.angleDelta().y() > 0 else -10; r = item.rect(); nw = max(20, r.width() + delta); nh = max(20, r.height() + delta); item.prepareGeometryChange(); item.setRect(0, 0, nw, nh); event.accept()
        else: super().wheelEvent(event)
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.scene() and not self.scene().sceneRect().isEmpty(): self.fitInView(self.scene().sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

# ============================================================
# WIDGET & UI CẬP NHẬT
# ============================================================


# ============================================================
#  CARD MỖI TẬP TRONG GRID + THANH TIẾN ĐỘ
# ============================================================
class EpisodeCard(QFrame):
    """1 ô trong lưới: hiển thị 1 tập đã ghép cặp video + srt.
    Cho phép đổi lại video/srt nếu ghép sai, và bấm chọn để preview."""
    clicked = pyqtSignal(object)      # phát chính card khi được bấm

    def __init__(self, video_path, srt_path, parent=None):
        super().__init__(parent)
        self.video_path = video_path
        self.srt_path = srt_path
        self.selected = False
        self.setFixedHeight(96)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._build()
        self._apply_style()

    def _build(self):
        lay = QVBoxLayout(self); lay.setContentsMargins(8, 6, 8, 6); lay.setSpacing(2)
        self.lbl_name = QLabel(os.path.basename(self.video_path))
        self.lbl_name.setStyleSheet("color:#E5E6E8; font-weight:bold; font-size:11px; border:none;")
        self.lbl_name.setWordWrap(True)
        srt_txt = os.path.basename(self.srt_path) if self.srt_path else "⚠️ CHƯA CÓ SUB"
        srt_col = "#8A8D98" if self.srt_path else "#F87171"
        self.lbl_srt = QLabel("📄 " + srt_txt)
        self.lbl_srt.setStyleSheet(f"color:{srt_col}; font-size:10px; border:none;")
        self.lbl_badge = QLabel("chờ")
        self.lbl_badge.setStyleSheet("background:#2D303D; color:#8A8D98; font-size:9px; padding:1px 6px; border-radius:4px; border:none;")
        top = QHBoxLayout(); top.addWidget(self.lbl_name, stretch=1); top.addWidget(self.lbl_badge)
        lay.addLayout(top); lay.addWidget(self.lbl_srt)

    def _apply_style(self):
        if self.selected:
            self.setStyleSheet("QFrame { background:#232533; border:2px solid #10B981; border-radius:8px; }")
        else:
            self.setStyleSheet("QFrame { background:#1C1D27; border:1px solid #2D303D; border-radius:8px; } QFrame:hover { border:1px solid #7452FF; }")

    def set_selected(self, val):
        self.selected = val; self._apply_style()

    def set_status(self, status):
        colors = {
            "chờ": ("#2D303D", "#8A8D98"),
            "đang render": ("#3B2A1A", "#F37021"),
            "xong": ("#1B3320", "#10B981"),
            "lỗi": ("#3B1A1A", "#F87171"),
        }
        bg, fg = colors.get(status, ("#2D303D", "#8A8D98"))
        self.lbl_badge.setText(status)
        self.lbl_badge.setStyleSheet(f"background:{bg}; color:{fg}; font-size:9px; padding:1px 6px; border-radius:4px; border:none;")

    def mousePressEvent(self, e):
        self.clicked.emit(self)
        super().mousePressEvent(e)


class ProgressStep(QWidget):
    def __init__(self, name, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 2, 0, 2); lay.setSpacing(2)
        top = QHBoxLayout()
        self.dot = QLabel(); self.dot.setFixedSize(8, 8)
        self.dot.setStyleSheet("background:#4B5563; border-radius:4px;")
        self.lbl_name = QLabel(name); self.lbl_name.setStyleSheet("color:#8A8D98; font-size:11px;")
        self.lbl_status = QLabel("chờ"); self.lbl_status.setStyleSheet("color:#8A8D98; font-size:10px;")
        top.addWidget(self.dot); top.addWidget(self.lbl_name, stretch=1); top.addWidget(self.lbl_status)
        lay.addLayout(top)
        self.bar = QProgressBar(); self.bar.setFixedHeight(3); self.bar.setTextVisible(False)
        self.bar.setStyleSheet("QProgressBar { background:#2D303D; border:none; border-radius:1px; } QProgressBar::chunk { background:#10B981; border-radius:1px; }")
        lay.addWidget(self.bar)

    def set_status(self, status, progress=0):
        color = {"success": "#10B981", "processing": "#F37021"}.get(status, "#4B5563")
        self.dot.setStyleSheet(f"background:{color}; border-radius:4px;")
        self.lbl_status.setText({"success": "xong", "processing": "đang chạy"}.get(status, "chờ"))
        self.bar.setValue(int(progress))

# ============================================================
#  TAB RENDER CHÍNH
# ============================================================
class RenderWidget(QWidget):
    # Cặp file ưu tiên: *_dubbed.mp4 + *_vi.srt; không có thì dùng gốc.
    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = QSettings("HongguoDownloader", "RenderTab")
        self.cards = []                 # danh sách EpisodeCard trong grid
        self.selected_card = None
        self.blur_boxes = []
        self.sample_sub = None
        self.logo_item = None
        self._design_locked = None
        self._render_queue = []         # hàng đợi render (các card)
        self._render_running = False
        self._stopping = False
        self.render_thread = None

        self.setStyleSheet("""
            QWidget { background:#11121A; color:#E5E6E8; font-family:'Segoe UI',Arial,sans-serif; }
            QScrollArea { border:none; background:transparent; }
            QScrollBar:vertical { background:#11121A; width:8px; }
            QScrollBar::handle:vertical { background:#3B3E4D; border-radius:4px; }
            QPushButton { background:#2D303D; color:#E5E6E8; border-radius:6px; padding:7px; font-weight:bold; border:1px solid #3B3E4D; }
            QPushButton:hover { background:#3B3E4D; border:1px solid #7452FF; color:white; }
            QLineEdit, QSpinBox, QComboBox, QDoubleSpinBox { background:#11121A; border:1px solid #2D303D; padding:7px; color:white; border-radius:4px; font-weight:bold; }
            QComboBox QAbstractItemView { background:#1C1D27; border:1px solid #7452FF; selection-background-color:#2D303D; }
            QCheckBox { font-weight:bold; padding:3px; }
            QCheckBox::indicator { width:18px; height:18px; border-radius:4px; border:1px solid #3B3E4D; background:#11121A; }
            QCheckBox::indicator:checked { background:#10B981; border:1px solid #10B981; }
        """)

        main = QHBoxLayout(self); main.setContentsMargins(10, 10, 10, 10); main.setSpacing(10)

        # ---------- CỘT TRÁI: GRID GHÉP CẶP ----------
        left = QFrame(); left.setMinimumWidth(300); left.setMaximumWidth(340)
        left.setStyleSheet("background:#151821; border-radius:8px; border:1px solid #1F222D;")
        ll = QVBoxLayout(left); ll.setContentsMargins(10, 10, 10, 10)
        head_q = QHBoxLayout()
        head_q.addWidget(QLabel("🎞️ Hàng đợi Render", styleSheet="font-size:14px; font-weight:bold; color:#F37021; border:none;"))
        head_q.addStretch()
        self.btn_open_folder = QPushButton("📂 Mở thư mục đang render")
        self.btn_open_folder.setStyleSheet("QPushButton { background:#2D303D; color:#E5E6E8; padding:6px 10px; font-size:11px; border-radius:6px; border:1px solid #3B3E4D; } QPushButton:hover { background:#3B3E4D; border:1px solid #7452FF; color:white; }")
        self.btn_open_folder.clicked.connect(self._open_render_folder)
        head_q.addWidget(self.btn_open_folder)
        ll.addLayout(head_q)

        btnrow = QHBoxLayout()
        b_folder = QPushButton("📁 Chọn thư mục"); b_folder.clicked.connect(self._pick_folder)
        b_files = QPushButton("+ File"); b_files.clicked.connect(self._pick_files)
        b_clear = QPushButton("🗑️"); b_clear.setFixedWidth(40); b_clear.clicked.connect(self._clear_all)
        b_clear.setStyleSheet("background:#2D303D; color:#F87171; border:1px dashed #EF4444;")
        btnrow.addWidget(b_folder); btnrow.addWidget(b_files); btnrow.addWidget(b_clear)
        ll.addLayout(btnrow)

        self.scroll_grid = QScrollArea(); self.scroll_grid.setWidgetResizable(True)
        self.grid_host = QWidget(); self.grid_lay = QGridLayout(self.grid_host)
        self.grid_lay.setAlignment(Qt.AlignmentFlag.AlignTop); self.grid_lay.setSpacing(6)
        self.scroll_grid.setWidget(self.grid_host)
        ll.addWidget(self.scroll_grid, stretch=5)

        # sửa cặp khi chọn 1 card
        fix = QFrame(); fix.setStyleSheet("background:#0F1117; border-radius:6px; border:1px solid #1F222D;")
        fl = QVBoxLayout(fix); fl.setContentsMargins(8, 8, 8, 8); fl.setSpacing(4)
        fl.addWidget(QLabel("Sửa cặp đang chọn:", styleSheet="color:#8A8D98; font-size:10px; border:none;"))
        r1 = QHBoxLayout(); self.lbl_fix_v = QLabel("—"); self.lbl_fix_v.setStyleSheet("color:#E5E6E8; font-size:10px; border:none;")
        bv = QPushButton("Đổi video"); bv.setFixedWidth(80); bv.clicked.connect(self._change_video)
        r1.addWidget(self.lbl_fix_v, stretch=1); r1.addWidget(bv); fl.addLayout(r1)
        r2 = QHBoxLayout(); self.lbl_fix_s = QLabel("—"); self.lbl_fix_s.setStyleSheet("color:#8A8D98; font-size:10px; border:none;")
        bs = QPushButton("Đổi sub"); bs.setFixedWidth(80); bs.clicked.connect(self._change_srt)
        r2.addWidget(self.lbl_fix_s, stretch=1); r2.addWidget(bs); fl.addLayout(r2)
        ll.addWidget(fix)

        self.step_render = ProgressStep("Tiến độ render")
        ll.addWidget(self.step_render)
        self.txt_log = QTextEdit(); self.txt_log.setReadOnly(True); self.txt_log.document().setMaximumBlockCount(400)
        self.txt_log.setFixedHeight(90)
        self.txt_log.setStyleSheet("background:#0B0E14; color:#A7F3D0; font-family:Consolas; font-size:10px; padding:5px; border:1px solid #1F222D;")
        ll.addWidget(self.txt_log)
        main.addWidget(left)

        # ---------- CỘT GIỮA: PREVIEW ----------
        center = QFrame(); center.setStyleSheet("background:transparent; border:none;")
        cl = QVBoxLayout(center); cl.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(QLabel("🎬 Xem trước (bấm 1 tập bên trái · kéo chữ/logo · lăn chuột để zoom vật thể)",
                              styleSheet="color:#8A8D98; font-weight:bold; font-size:11px;"))
        head.addStretch()
        self.btn_reset = QPushButton("Reset vị trí"); self.btn_reset.setStyleSheet("background:#31265C; color:#7452FF; padding:4px;")
        self.btn_reset.clicked.connect(self._reset_pos); head.addWidget(self.btn_reset)
        cl.addLayout(head)

        self.scene = QGraphicsScene(self)
        self.video_item = QGraphicsVideoItem(); self.scene.addItem(self.video_item)
        self.media_player = QMediaPlayer(); self.audio_output = QAudioOutput()
        self.media_player.setAudioOutput(self.audio_output); self.media_player.setVideoOutput(self.video_item)
        self.video_item.nativeSizeChanged.connect(self._on_native_size)
        self.preview = PreviewGraphicsView(self.scene); cl.addWidget(self.preview, stretch=1)

        ctr = QHBoxLayout()
        self.btn_play = QPushButton("▶"); self.btn_play.setFixedWidth(70); self.btn_play.clicked.connect(self._toggle_play)
        self.lbl_time = QLabel("00:00 / 00:00"); self.lbl_time.setStyleSheet("color:#8A8D98; font-size:11px; padding:0 5px;")
        self.slider = QSlider(Qt.Orientation.Horizontal); self.slider.sliderMoved.connect(self.media_player.setPosition)
        self.media_player.positionChanged.connect(self._on_pos)
        self.media_player.durationChanged.connect(self._on_dur)
        ctr.addWidget(self.btn_play); ctr.addWidget(self.slider); ctr.addWidget(self.lbl_time)
        cl.addLayout(ctr)
        main.addWidget(center, stretch=6)

        # ---------- CỘT PHẢI: DESIGN ----------
        right = QFrame(); right.setMinimumWidth(300); right.setMaximumWidth(350)
        right.setStyleSheet("background:#151821; border-radius:8px; border:1px solid #1F222D;")
        rl = QVBoxLayout(right); rl.setContentsMargins(10, 10, 10, 10)
        rl.addWidget(QLabel("🎨 Thiết kế & Render", styleSheet="font-size:14px; font-weight:bold; color:#10B981; border:none;"))

        self.chk_hardsub = QCheckBox("Khắc Sub vào Video")
        self.chk_hardsub.setChecked(self.settings.value("hardsub_en", True, type=bool))
        self.chk_hardsub.setStyleSheet("color:#FBBF24; font-weight:bold;")
        rl.addWidget(self.chk_hardsub)

        ql = QHBoxLayout(); ql.addWidget(QLabel("Chất lượng:"))
        self.cb_quality = QComboBox()
        self.cb_quality.addItems([
            "🏆 Cao nhất (CRF 16 - Gần lossless)",
            "⭐ Tốt (CRF 20 - Đề xuất)",
            "👍 Vừa (CRF 26 - Cân bằng)",
            "⚡ Nhanh (1 Mbps - File nhỏ)",
        ])
        self.cb_quality.setCurrentText(self.settings.value("render_quality", "⭐ Tốt (CRF 20 - Đề xuất)"))
        ql.addWidget(self.cb_quality); rl.addLayout(ql)

        fb = QHBoxLayout(); fb.addWidget(QLabel("Font:"))
        self.cb_font = QComboBox(); self.cb_font.addItems(FONTS_LIST)
        self.cb_font.setCurrentText(self.settings.value("font_name", "Arial"))
        fb.addWidget(QLabel("Cỡ:")); self.spin_size = QSpinBox(); self.spin_size.setRange(10, 150)
        self.spin_size.setValue(int(self.settings.value("font_size", 24)))
        fb.addWidget(self.cb_font); fb.addWidget(self.spin_size); rl.addLayout(fb)

        cb = QHBoxLayout(); cb.addWidget(QLabel("Màu Sub:"))
        self.cb_color = QComboBox(); self.cb_color.addItems(list(COLOR_PRESETS.keys()))
        self.cb_color.setCurrentText(self.settings.value("font_color_name", "Trắng (White)"))
        cb.addWidget(self.cb_color); rl.addLayout(cb)

        # ── Nền ô chữ (dạng hộp màu sau chữ) ──
        box_row = QHBoxLayout()
        self.chk_subbox = QCheckBox("Nền ô chữ")
        self.chk_subbox.setChecked(self.settings.value("subbox_en", False, type=bool))
        self.chk_subbox.setStyleSheet("color:#93c5fd; font-weight:bold;")
        box_row.addWidget(self.chk_subbox)
        box_row.addWidget(QLabel("Màu nền:"))
        self.cb_subbox_color = QComboBox()
        self.cb_subbox_color.addItems(["Đen", "Xám đậm", "Xanh đen", "Trắng"])
        self.cb_subbox_color.setCurrentText(self.settings.value("subbox_color_name", "Đen"))
        box_row.addWidget(self.cb_subbox_color)
        rl.addLayout(box_row)

        op_row = QHBoxLayout()
        op_row.addWidget(QLabel("Độ mờ nền:"))
        self.spn_subbox_opacity = QSpinBox()
        self.spn_subbox_opacity.setRange(0, 100)
        self.spn_subbox_opacity.setValue(int(self.settings.value("subbox_opacity", 60)))
        self.spn_subbox_opacity.setSuffix(" %")
        self.spn_subbox_opacity.setToolTip("0% = trong suốt (không thấy nền), 100% = nền đặc kín.")
        op_row.addWidget(self.spn_subbox_opacity); op_row.addStretch()
        rl.addLayout(op_row)
        # đổi font/cỡ/màu -> cập nhật ngay ô chữ mẫu trên preview
        self.cb_font.currentTextChanged.connect(lambda *_: self._restyle_sample_sub())
        self.spin_size.valueChanged.connect(lambda *_: self._restyle_sample_sub())
        self.cb_color.currentTextChanged.connect(lambda *_: self._restyle_sample_sub())

        bl = QHBoxLayout()
        self.chk_blur = QCheckBox("Bật Khung Mờ"); self.chk_blur.setChecked(self.settings.value("bp_blur_en", False, type=bool))
        self.chk_blur.setStyleSheet("color:#F37021; font-weight:bold;")
        b_add = QPushButton("[+] Vùng che"); b_add.setStyleSheet("background:#2D303D; color:#10B981; padding:4px; font-size:10px;")
        b_add.clicked.connect(lambda: self._add_blur_box())
        b_clr = QPushButton("[-] Xóa"); b_clr.setStyleSheet("background:#2D303D; color:#EF4444; padding:4px; font-size:10px;")
        b_clr.clicked.connect(self._clear_blur_boxes)
        bl.addWidget(self.chk_blur); bl.addWidget(b_add); bl.addWidget(b_clr); rl.addLayout(bl)

        frl = QHBoxLayout()
        self.chk_frame = QCheckBox("Overlay PNG"); self.chk_frame.setChecked(self.settings.value("bp_frame_en", False, type=bool))
        self.chk_frame.setStyleSheet("color:#7452FF; font-weight:bold;")
        self.frame_input = QLineEdit(self.settings.value("frame_path", "")); self.frame_input.setPlaceholderText("Ảnh PNG...")
        bf = QPushButton("Chọn"); bf.setFixedWidth(55); bf.clicked.connect(self._select_frame)
        frl.addWidget(self.chk_frame); frl.addWidget(self.frame_input); frl.addWidget(bf); rl.addLayout(frl)

        lgl = QHBoxLayout()
        self.chk_logo = QCheckBox("Logo"); self.chk_logo.setChecked(self.settings.value("bp_logo_en", False, type=bool))
        self.logo_input = QLineEdit(self.settings.value("logo_path", ""))
        bg2 = QPushButton("Chọn"); bg2.setFixedWidth(55); bg2.clicked.connect(self._select_logo)
        lgl.addWidget(self.chk_logo); lgl.addWidget(self.logo_input); lgl.addWidget(bg2); rl.addLayout(lgl)

        # Hook hiển thị ảnh logo
        self.chk_logo.stateChanged.connect(lambda: self._update_logo_preview())
        self.logo_input.textChanged.connect(lambda: self._update_logo_preview())

        rl.addWidget(QLabel("🎛️ Bộ lọc Bypass FX:", styleSheet="font-weight:bold; margin-top:10px; color:#8A8D98;"))
        self.chk_flip = QCheckBox("Lật ngang"); self.chk_zoom = QCheckBox("Phóng to 4%")
        self.chk_color = QCheckBox("Kích màu sáng"); self.chk_noise = QCheckBox("Nhiễu hạt")
        self.chk_speed = QCheckBox("Tốc độ 1.05x"); self.chk_pitch = QCheckBox("Đổi Tone")
        self.chk_rotate = QCheckBox("Xoay 1°")
        for k, chk in (("bp_flip", self.chk_flip), ("bp_zoom", self.chk_zoom), ("bp_color", self.chk_color),
                       ("bp_noise", self.chk_noise), ("bp_speed", self.chk_speed), ("bp_pitch", self.chk_pitch),
                       ("bp_rotate", self.chk_rotate)):
            chk.setChecked(self.settings.value(k, False, type=bool))
        gb = QGridLayout()
        gb.addWidget(self.chk_flip, 0, 0); gb.addWidget(self.chk_zoom, 0, 1)
        gb.addWidget(self.chk_color, 1, 0); gb.addWidget(self.chk_noise, 1, 1)
        gb.addWidget(self.chk_speed, 2, 0); gb.addWidget(self.chk_pitch, 2, 1)
        gb.addWidget(self.chk_rotate, 3, 0)
        rl.addLayout(gb)
        rl.addStretch()

        # Nút Đồng bộ: chốt cấu hình canh chỉnh hiện tại (vị trí sub + ô che +
        # font/màu) làm chuẩn áp cho TẤT CẢ các tập khi render.
        self.btn_sync_design = QPushButton("🔄 Đồng bộ canh chỉnh cho tất cả file")
        self.btn_sync_design.setStyleSheet("QPushButton { background:#0891b2; color:white; padding:9px; font-size:12px; border-radius:8px; border:none; } QPushButton:hover { background:#0e7490; }")
        self.btn_sync_design.clicked.connect(self._sync_design_all)
        rl.addWidget(self.btn_sync_design)

        self.btn_run = QPushButton("🔥 RENDER TẤT CẢ (0)")
        self.btn_run.setStyleSheet("QPushButton { background:#F37021; color:white; padding:12px; font-size:14px; border-radius:8px; border:none; } QPushButton:hover { background:#e05f10; }")
        self.btn_run.clicked.connect(self._start_render_all)
        rl.addWidget(self.btn_run)

        self.btn_stop = QPushButton("⛔ DỪNG RENDER")
        self.btn_stop.setStyleSheet("QPushButton { background:#7F1D1D; color:white; padding:10px; font-size:13px; border-radius:8px; border:none; } QPushButton:hover { background:#991B1B; } QPushButton:disabled { background:#3B2020; color:#8A8D98; }")
        self.btn_stop.clicked.connect(self._stop_render)
        self.btn_stop.setEnabled(False)
        rl.addWidget(self.btn_stop)
        main.addWidget(right)

    # ============ LOG ============
    def _log(self, msg):
        self.txt_log.append(str(msg).strip())

    # ============ MỞ THƯ MỤC ĐANG RENDER ============
    def _open_render_folder(self):
        """Mở thư mục chứa file đang/đã render. Ưu tiên thư mục render gần
        nhất; nếu chưa render thì lấy thư mục của file đầu trong hàng đợi."""
        folder = getattr(self, "_last_render_dir", None)
        if not folder and self.cards:
            folder = os.path.dirname(self.cards[0].video_path)
        if not folder or not os.path.isdir(folder):
            QMessageBox.information(self, "Chưa có thư mục",
                "Chưa có file nào để mở. Hãy thêm video hoặc bắt đầu render trước.")
            return
        try:
            if os.name == "nt":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            QMessageBox.warning(self, "Lỗi", f"Không mở được thư mục:\n{e}")

    # ============ GHÉP CẶP VIDEO + SRT ============
    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Chọn thư mục chứa các tập")
        if not d:
            return
        pairs = self._auto_pair(d)
        if not pairs:
            QMessageBox.information(self, "Không thấy video", "Thư mục này không có file video (.mp4) nào.")
            return
        for vp, sp in pairs:
            self._add_card(vp, sp)
        self._relayout_grid()
        self._update_run_label()

    def _pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Chọn video", "", "Video (*.mp4 *.mkv *.mov *.avi)")
        for vp in files:
            sp = self._guess_srt_for(vp)
            self._add_card(vp, sp)
        if files:
            self._relayout_grid(); self._update_run_label()

    def _auto_pair(self, folder):
        """Quét folder, ghép cặp video+srt. Ưu tiên *_dubbed.mp4 + *_vi.srt;
        không có dubbed thì dùng video gốc; không có _vi.srt thì dùng .srt gốc.
        Gom theo 'stem gốc' của mỗi tập (bỏ hậu tố _dubbed)."""
        try:
            names = os.listdir(folder)
        except Exception:
            return []
        videos = [n for n in names if n.lower().endswith((".mp4", ".mkv", ".mov", ".avi"))]
        srt_set = set(n for n in names if n.lower().endswith(".srt"))

        # gom video theo stem gốc (bỏ _dubbed)
        groups = {}   # base_stem -> {"dubbed":..., "plain":...}
        for v in videos:
            stem = os.path.splitext(v)[0]
            if stem.endswith("_dubbed"):
                base = stem[:-len("_dubbed")]
                groups.setdefault(base, {})["dubbed"] = v
            else:
                groups.setdefault(stem, {})["plain"] = v

        pairs = []
        for base in sorted(groups.keys()):
            g = groups[base]
            video = g.get("dubbed") or g.get("plain")
            if not video:
                continue
            # chọn srt: ưu tiên <base>_vi.srt, rồi <base>.srt
            srt = None
            if f"{base}_vi.srt" in srt_set:
                srt = f"{base}_vi.srt"
            elif f"{base}.srt" in srt_set:
                srt = f"{base}.srt"
            vp = os.path.join(folder, video)
            sp = os.path.join(folder, srt) if srt else None
            pairs.append((vp, sp))
        return pairs

    def _guess_srt_for(self, video_path):
        """Đoán srt đi kèm 1 video lẻ (khi thêm bằng + File)."""
        stem = os.path.splitext(video_path)[0]
        if stem.endswith("_dubbed"):
            stem = stem[:-len("_dubbed")]
        for cand in (stem + "_vi.srt", stem + ".srt"):
            if os.path.exists(cand):
                return cand
        return None

    def _add_card(self, video_path, srt_path):
        # tránh trùng
        for c in self.cards:
            if c.video_path == video_path:
                return
        card = EpisodeCard(video_path, srt_path)
        card.clicked.connect(self._on_card_clicked)
        self.cards.append(card)

    def _relayout_grid(self):
        # xếp lại lưới 2 cột
        for i in reversed(range(self.grid_lay.count())):
            w = self.grid_lay.itemAt(i).widget()
            if w:
                self.grid_lay.removeWidget(w)
        for idx, card in enumerate(self.cards):
            self.grid_lay.addWidget(card, idx // 1, idx % 1)   # 1 cột cho dễ đọc tên

    def _clear_all(self):
        self.media_player.stop()
        for c in self.cards:
            c.setParent(None)
        self.cards = []; self.selected_card = None
        self.lbl_fix_v.setText("—"); self.lbl_fix_s.setText("—")
        self._update_run_label()

    def _on_card_clicked(self, card):
        for c in self.cards:
            c.set_selected(c is card)
        self.selected_card = card
        self.lbl_fix_v.setText(os.path.basename(card.video_path))
        self.lbl_fix_s.setText(os.path.basename(card.srt_path) if card.srt_path else "⚠️ chưa có sub")
        self._load_preview(card.video_path)

    def _change_video(self):
        if not self.selected_card:
            return
        fp, _ = QFileDialog.getOpenFileName(self, "Chọn video khác", "", "Video (*.mp4 *.mkv *.mov *.avi)")
        if fp:
            self.selected_card.video_path = fp
            self.selected_card.lbl_name.setText(os.path.basename(fp))
            self.lbl_fix_v.setText(os.path.basename(fp))
            self._load_preview(fp)

    def _change_srt(self):
        if not self.selected_card:
            return
        fp, _ = QFileDialog.getOpenFileName(self, "Chọn sub khác", "", "Phụ đề (*.srt)")
        if fp:
            self.selected_card.srt_path = fp
            self.selected_card.lbl_srt.setText("📄 " + os.path.basename(fp))
            self.selected_card.lbl_srt.setStyleSheet("color:#8A8D98; font-size:10px; border:none;")
            self.lbl_fix_s.setText(os.path.basename(fp))

    def _update_run_label(self):
        self.btn_run.setText(f"🔥 RENDER TẤT CẢ ({len(self.cards)})")

    # ============ PREVIEW ============
    def _load_preview(self, video_path):
        try:
            self.media_player.setSource(QUrl.fromLocalFile(video_path))
            self.media_player.pause()
        except Exception as e:
            self._log(f"⚠️ Không mở được preview: {e}")

    def _toggle_play(self):
        from PyQt6.QtMultimedia import QMediaPlayer as _QMP
        if self.media_player.playbackState() == _QMP.PlaybackState.PlayingState:
            self.media_player.pause(); self.btn_play.setText("▶")
        else:
            self.media_player.play(); self.btn_play.setText("⏸")

    def _on_pos(self, pos):
        self.slider.setValue(pos)
        dur = self.media_player.duration()
        self.lbl_time.setText(f"{format_time(pos/1000)} / {format_time(dur/1000)}")

    def _on_dur(self, dur):
        self.slider.setRange(0, dur)

    def _on_native_size(self, size):
        if size.width() > 0 and size.height() > 0:
            self.video_item.setSize(size)
            self.scene.setSceneRect(0, 0, size.width(), size.height())
            self.preview.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            self._ensure_sample_sub()   # hiện ô chữ mẫu để canh vị trí sub
            self._update_logo_preview() # hiện logo nếu có

    def _reset_pos(self):
        self.preview.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    # ============ KHUNG MỜ / FRAME / LOGO ============
    def _add_blur_box(self):
        # Lấy kích thước vùng làm việc: ưu tiên sceneRect, nếu chưa có (video
        # chưa load xong) thì lấy nativeSize của video, cuối cùng mặc định
        # 1080x1920 (dọc - hợp phim ngắn). Nhờ vậy ô che LUÔN hiện đủ to để
        # nhìn thấy và kéo, kể cả khi thêm trước lúc video sẵn sàng.
        rect = self.scene.sceneRect()
        W = rect.width(); H = rect.height()
        if W < 10 or H < 10:
            ns = self.video_item.nativeSize()
            if ns.width() > 10 and ns.height() > 10:
                W, H = ns.width(), ns.height()
            else:
                W, H = 1080, 1920
            # đảm bảo scene có kích thước để đặt item
            self.scene.setSceneRect(0, 0, W, H)
        w = max(120, W * 0.5); h = max(60, H * 0.10)
        # đặt ô che ở GIỮA màn hình cho dễ thấy
        x = (W - w) / 2; y = (H - h) / 2
        box = DraggableBlurBox(x, y, w, h)
        box.setSelected(True)          # chọn sẵn để thấy handle + kéo ngay
        self.scene.addItem(box); self.blur_boxes.append(box)
        # canh lại khung nhìn để chắc chắn ô nằm trong vùng thấy
        try:
            self.preview.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        except Exception:
            pass

    def _ensure_sample_sub(self):
        """Tạo (nếu chưa có) ô CHỮ MẪU sub tiếng Việt trên preview để canh vị
        trí. Chữ kéo được, phóng to được."""
        rect = self.scene.sceneRect()
        W = rect.width() or 1080; H = rect.height() or 1920
        if getattr(self, 'sample_sub', None) is None:
            self.sample_sub = ScalableTextItem("Chữ Dịch Tiếng Việt (kéo tôi đi đâu cũng được!)")
            self.sample_sub.setZValue(5)
            self.scene.addItem(self.sample_sub)
            self.sample_sub.setPos(W * 0.08, H * 0.80)
        self._restyle_sample_sub()

    def _restyle_sample_sub(self):
        """Áp font/cỡ/màu đang chọn lên ô chữ mẫu."""
        if getattr(self, 'sample_sub', None) is None:
            return
        try:
            font = QFont(self.cb_font.currentText(), int(self.spin_size.value()))
            font.setBold(True)
            self.sample_sub.setFont(font)
            qt_color = COLOR_PRESETS.get(self.cb_color.currentText(), {}).get("qt", "#FFFFFF")
            self.sample_sub.setDefaultTextColor(QColor(qt_color))
        except Exception:
            pass
            
    def _update_logo_preview(self):
        """Khởi tạo và hiển thị ảnh Logo lên màn hình Preview"""
        path = self.logo_input.text().strip()
        if not self.chk_logo.isChecked() or not os.path.exists(path):
            if getattr(self, 'logo_item', None):
                self.scene.removeItem(self.logo_item)
                self.logo_item = None
            return

        if getattr(self, 'logo_item', None) is None:
            self.logo_item = ScalablePixmapItem()
            self.logo_item.setZValue(6) # Đặt lớp trên cùng để dễ kéo
            self.scene.addItem(self.logo_item)
            
            # Căn góc logo lúc mới hiện
            scene_rect = self.scene.sceneRect()
            W = scene_rect.width() or 1080
            H = scene_rect.height() or 1920
            self.logo_item.setPos(W * 0.05, H * 0.05)

        pixmap = QPixmap(path)
        if not pixmap.isNull():
            self.logo_item.setPixmap(pixmap)

    def _clear_blur_boxes(self):
        for b in self.blur_boxes:
            try: self.scene.removeItem(b)
            except Exception: pass
        self.blur_boxes = []

    def _select_frame(self):
        fp, _ = QFileDialog.getOpenFileName(self, "Chọn ảnh Overlay PNG", "", "Ảnh (*.png)")
        if fp:
            self.frame_input.setText(fp)

    def _select_logo(self):
        fp, _ = QFileDialog.getOpenFileName(self, "Chọn Logo", "", "Ảnh (*.png *.jpg *.jpeg)")
        if fp:
            self.logo_input.setText(fp)

    # ============ THU THẬP CONFIG DESIGN ============
    def _collect_design(self):
        color_name = self.cb_color.currentText()
        color_ass = COLOR_PRESETS.get(color_name, {}).get("ass", "&H00FFFFFF")
        blur_list = []
        for b in self.blur_boxes:
            r = b.sceneBoundingRect()
            blur_list.append({"x": int(r.x()), "y": int(r.y()), "w": int(r.width()), "h": int(r.height())})
            
        scene = self.scene.sceneRect()
        SW = scene.width() or 1080; SH = scene.height() or 1920

        # Lấy thông số tọa độ + scale của Logo
        logo_pos = None
        if getattr(self, 'logo_item', None) is not None and self.chk_logo.isChecked():
            lr = self.logo_item.sceneBoundingRect()
            logo_pos = {
                "x": lr.x(), "y": lr.y(), "scale": self.logo_item.scale()
            }

        # Đọc VỊ TRÍ + CỠ chữ mẫu (nếu người dùng đã kéo canh) để render sub
        # đúng chỗ + đúng cỡ. Quy ước theo hệ toạ độ scene = kích thước video.
        sub_pos = None
        try:
            if getattr(self, 'sample_sub', None) is not None:
                r = self.sample_sub.sceneBoundingRect()   # đã tính cả scale
                # Lấy trực tiếp chiều cao pixel của ô chữ trên màn hình làm chuẩn.
                # Nhân 0.75 để bù trừ khoảng trắng (padding/line-height) mặc định của Qt
                eff_size = int(r.height() * 0.75)
                sub_pos = {
                    "cx": r.center().x(), "cy": r.center().y(),
                    "left": r.left(), "bottom": r.bottom(),
                    "SW": SW, "SH": SH, "eff_size": eff_size,
                }
        except Exception:
            sub_pos = None

        self.settings.setValue("font_name", self.cb_font.currentText())
        self.settings.setValue("font_size", self.spin_size.value())
        self.settings.setValue("font_color_name", color_name)
        self.settings.setValue("render_quality", self.cb_quality.currentText())
        self.settings.setValue("hardsub_en", self.chk_hardsub.isChecked())

        # Nền ô chữ -> mã màu ASS &HAABBGGRR (AA=alpha: 00 đặc, FF trong).
        subbox_en = self.chk_subbox.isChecked()
        _box_bgr = {"Đen": "000000", "Xám đậm": "202020", "Xanh đen": "301500", "Trắng": "FFFFFF"}
        bgr = _box_bgr.get(self.cb_subbox_color.currentText(), "000000")
        opac = int(self.spn_subbox_opacity.value())          # 0..100 (100 = đặc)
        alpha = int(round((100 - opac) * 255 / 100))         # ASS alpha: 0 đặc, 255 trong
        subbox_color = f"&H{alpha:02X}{bgr}"
        self.settings.setValue("subbox_en", subbox_en)
        self.settings.setValue("subbox_color_name", self.cb_subbox_color.currentText())
        self.settings.setValue("subbox_opacity", opac)
        return {
            "hardsub_en": self.chk_hardsub.isChecked(),
            "render_quality": self.cb_quality.currentText(),
            "font_name": self.cb_font.currentText(),
            "font_size": self.spin_size.value(),
            "font_color": color_ass,
            "sub_pos": sub_pos,
            "logo_pos": logo_pos,
            "SW": SW, "SH": SH,
            "subbox_en": subbox_en, "subbox_color": subbox_color,
            "bp_blur_en": self.chk_blur.isChecked(), "blur_list": blur_list,
            "bp_frame_en": self.chk_frame.isChecked(), "frame_path": self.frame_input.text().strip(),
            "bp_logo_en": self.chk_logo.isChecked(), "logo_path": self.logo_input.text().strip(),
            "bp_flip": self.chk_flip.isChecked(), "bp_zoom": self.chk_zoom.isChecked(),
            "bp_color": self.chk_color.isChecked(), "bp_noise": self.chk_noise.isChecked(),
            "bp_speed": self.chk_speed.isChecked(), "bp_pitch": self.chk_pitch.isChecked(),
            "bp_rotate": self.chk_rotate.isChecked(),
        }

    def _build_cfg(self, video_path, design):
        """Dò kích thước video rồi dựng cfg cho SingleRenderThread."""
        W, H = 1920, 1080
        try:
            probe = subprocess.run([get_ffmpeg_path(), "-i", video_path], stderr=subprocess.PIPE,
                                   text=True, errors="ignore",
                                   creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
            m = re.search(r"Video:.*?,.*? (\d+)x(\d+)", probe.stderr)
            if m:
                W, H = int(m.group(1)), int(m.group(2))
        except Exception:
            pass
        if design.get("bp_zoom"):
            W *= 0.96; H *= 0.96
            
        SW = design.get("SW", W); SH = design.get("SH", H)
        sy = H / SH; sx = W / SW

        # Vị trí + cỡ chữ: nếu người dùng đã kéo canh chữ mẫu (sub_pos) thì
        # dùng đúng vị trí/cỡ đó; nếu không thì mặc định giữa-dưới, cách đáy 8%.
        eff_font = int(design.get("font_size", 24))
        margin_v = int(H * 0.08)
        sp = design.get("sub_pos")
        if sp:
            try:
                # libass Alignment=2 neo ĐÁY dòng chữ cách đáy màn hình = MarginV.
                # Dùng đáy chữ (bottom) trên scene, quy về pixel video.
                margin_v = int(max(0, (SH - sp["bottom"]) * sy))
                eff_font = int(max(8, sp["eff_size"] * sy))
                self._log(
                    f"   🔧 [canh sub] scene={int(SW)}x{int(SH)} video={int(W)}x{int(H)} "
                    f"| đáy_chữ={int(sp['bottom'])} | margin_v={margin_v} "
                    f"| cỡ_gốc={design.get('font_size')} scale~{sp['eff_size']/max(1,design.get('font_size',24)):.2f} eff_font={eff_font}\n"
                )
            except Exception as _e:
                self._log(f"   ⚠️ [canh sub] lỗi tính vị trí: {_e}\n")
                
        # Quy đổi vị trí + Cỡ Logo từ Preview sang chuẩn FFmpeg
        lx, ly, lscale = 20, 20, 1.0
        lp = design.get("logo_pos")
        if lp:
            lx = int(max(0, lp["x"] * sx))
            ly = int(max(0, lp["y"] * sy))
            lscale = lp["scale"] * sx

        return {
            "scene_w": int(W), "scene_h": int(H),
            "blur_en": design["bp_blur_en"], "blur_list": design["blur_list"],
            "frame_en": design["bp_frame_en"], "frame_path": design["frame_path"],
            "logo_en": design["bp_logo_en"], "logo_path": design["logo_path"],
            "logo_x": lx, "logo_y": ly, "logo_scale": lscale,
            "flip": design["bp_flip"], "zoom": design["bp_zoom"], "color": design["bp_color"],
            "noise": design["bp_noise"], "speed": design["bp_speed"], "pitch": design["bp_pitch"],
            "rotate_en": design["bp_rotate"],
            "font_name": design["font_name"], "font_size": eff_font,
            "font_color": design["font_color"],
            "subbox_en": design.get("subbox_en", False),
            "subbox_color": design.get("subbox_color", "&H80000000"),
            "margin_l": 0, "margin_r": 0, "margin_v": margin_v,
            "hardsub_en": design["hardsub_en"],
            "render_quality": design["render_quality"],
            # KHÔNG bật tts_en -> SingleRenderThread bỏ qua phần audio TTS
        }

    # ============ RENDER HÀNG LOẠT ============
    def _sync_design_all(self):
        """Chốt cấu hình canh chỉnh hiện tại (vị trí sub + ô che + font/màu/FX)
        làm chuẩn dùng cho TẤT CẢ các tập khi render."""
        if not self.cards:
            QMessageBox.information(self, "Chưa có file", "Hãy thêm video vào hàng đợi trước.")
            return
        self._design_locked = self._collect_design()
        n_blur = len(self._design_locked.get("blur_list", []))
        self._log(f"🔄 Đã chốt canh chỉnh (vị trí sub + {n_blur} ô che) áp cho tất cả {len(self.cards)} tập.")
        QMessageBox.information(self, "Đã đồng bộ",
            f"Đã lưu canh chỉnh hiện tại làm chuẩn cho tất cả {len(self.cards)} tập.\n"
            f"Bấm 'RENDER TẤT CẢ' để render đồng loạt theo canh chỉnh này.")

    def _start_render_all(self):
        if self._render_running:
            QMessageBox.information(self, "Đang render", "Đang render, vui lòng đợi xong.")
            return
        if not self.cards:
            QMessageBox.information(self, "Chưa có video", "Hãy thêm video vào hàng đợi trước.")
            return
        # Ưu tiên cấu hình ĐÃ ĐỒNG BỘ (nếu bấm nút Đồng bộ trước đó); nếu chưa
        # đồng bộ thì lấy canh chỉnh hiện tại. Dù cách nào cũng áp CHUNG cho mọi tập.
        self._design = getattr(self, '_design_locked', None) or self._collect_design()
        self._render_queue = list(self.cards)
        self._render_running = True
        self._stopping = False
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._log(f"🚀 Bắt đầu render {len(self._render_queue)} tập...")
        self._render_next()

    def _stop_render(self):
        if not self._render_running:
            return
        self._stopping = True
        self._render_queue = []          # xóa các tập chưa render
        self.btn_stop.setEnabled(False)
        self._log("⛔ Đang dừng render... (đợi tập hiện tại thoát)")
        if self.render_thread and self.render_thread.isRunning():
            self.render_thread.cancel()  # hủy tập đang render (xóa file dở)

    def _render_next(self):
        if self._stopping or not self._render_queue:
            stopped = self._stopping
            self._render_running = False
            self._stopping = False
            self._render_queue = []
            self.btn_run.setEnabled(True)
            self.btn_stop.setEnabled(False)
            if stopped:
                self.step_render.set_status("error", 0)
                self._log("⛔ Đã dừng render.")
                QMessageBox.information(self, "Đã dừng", "Đã dừng render theo yêu cầu.")
            else:
                self.step_render.set_status("success", 100)
                self._log("🎉 Đã render xong tất cả!")
                QMessageBox.information(self, "Xong", "Đã render xong tất cả các tập!")
            return
        card = self._render_queue.pop(0)
        card.set_status("đang render")
        self.step_render.set_status("processing", 30)
        vp = card.video_path
        sp = card.srt_path
        out_dir = os.path.dirname(vp)
        self._last_render_dir = out_dir
        stem = os.path.splitext(os.path.basename(vp))[0]
        if stem.endswith("_dubbed"):
            stem = stem[:-len("_dubbed")]
        out_path = os.path.join(out_dir, f"{stem}_final.mp4")
        cfg = self._build_cfg(vp, self._design)
        self._log(f"🎬 Render: {os.path.basename(vp)}...")
        self.render_thread = SingleRenderThread(vp, sp, None, out_path, cfg)
        self.render_thread.log.connect(self._log)
        self.render_thread.done.connect(lambda ok, c=card: self._on_one_done(ok, c))
        self.render_thread.start()

    def _on_one_done(self, ok, card):
        if self._stopping and not ok:
            card.set_status("đã dừng")
        else:
            card.set_status("xong" if ok else "lỗi")
        self._render_next()
