# -*- coding: utf-8 -*-
"""
deepseek_translate.py
======================
Module dịch phụ đề (.srt) Trung -> Việt bằng DeepSeek V4 Pro, có GIỮ NGỮ CẢNH
xuyên suốt cả bộ phim (glossary + hồ sơ nhân vật + ngữ cảnh gần nhất), dựa
trên system prompt trong file `rule_translate.txt` (bản Việt hóa Trung->Việt).

Cách dùng nhanh:
    from deepseek_translate import DeepSeekTranslator

    tr = DeepSeekTranslator(api_key="sk-xxxx")
    vi_path = tr.translate_srt_file(
        srt_path="Tap_01.srt",
        genre="Cổ trang",
        target_style="Tự nhiên, dễ nghe",
        output_requirements="Phụ đề phim, câu ngắn gọn dễ đọc",
    )
    print("Đã lưu:", vi_path)

Tích hợp vào app PyQt hiện tại: chạy translate_srt_file() bên trong 1
QThread (giống DubThread/GeminiTranslateThread đang có), emit progress_signal
sau mỗi khối để cập nhật thanh tiến trình.
"""

import os
import re
import json
import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional

import urllib.request
import urllib.error


# ─────────────────────────────────────────────────────────────────────────
# CẤU HÌNH
# ─────────────────────────────────────────────────────────────────────────

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-pro"          # đổi thành "deepseek-v4-flash" nếu muốn rẻ/nhanh hơn

# Số dòng phụ đề mỗi khối gửi đi 1 lần gọi API.
# DeepSeek V4 Pro cho phép output tối đa 384.000 token, nên chunk 300-500 dòng
# vẫn rất an toàn (~5-9k token output, chưa tới 3% giới hạn thật).
# Chunk to hơn còn tiết kiệm chi phí đáng kể vì rule_translate.txt (~4.1k token
# cố định) chỉ bị gửi lại 1 lần mỗi khối thay vì lặp lại quá nhiều lần.
LINES_PER_CHUNK = 400

# Số câu dịch gần nhất giữ lại làm PREVIOUS_CONTEXT cho khối kế tiếp.
PREVIOUS_CONTEXT_LINES = 6

# Số lần thử lại nếu API lỗi / rate-limit.
MAX_RETRIES = 3
RETRY_DELAY_SEC = 4


# ─────────────────────────────────────────────────────────────────────────
# PARSE / GHÉP LẠI FILE .SRT
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class SrtBlock:
    idx: str            # số thứ tự phụ đề gốc trong file (giữ nguyên)
    time_range: str      # "00:00:01,000 --> 00:00:03,000"
    text: str            # nội dung gốc (có thể nhiều dòng)


def parse_srt(srt_text: str) -> List[SrtBlock]:
    """Parse nội dung .srt thành danh sách SrtBlock, giữ nguyên ID & mốc thời gian."""
    blocks = []
    raw_blocks = re.split(r"\r?\n\r?\n", srt_text.strip())
    for rb in raw_blocks:
        lines = [l for l in rb.strip().splitlines() if l.strip() != ""]
        if len(lines) < 2:
            continue
        idx = lines[0].strip()
        time_range = lines[1].strip()
        text = "\n".join(lines[2:]).strip()
        if not re.match(r"^\d+$", idx) or "-->" not in time_range:
            # Không đúng định dạng SRT chuẩn -> bỏ qua an toàn
            continue
        blocks.append(SrtBlock(idx=idx, time_range=time_range, text=text))
    return blocks


def rebuild_srt(blocks: List[SrtBlock]) -> str:
    """Ghép lại danh sách SrtBlock (đã dịch) thành nội dung .srt hoàn chỉnh."""
    out = []
    for b in blocks:
        out.append(f"{b.idx}\n{b.time_range}\n{b.text}\n")
    return "\n".join(out) + "\n"


# ─────────────────────────────────────────────────────────────────────────
# TRẠNG THÁI NGỮ CẢNH CHO CẢ SERIES (LƯU FILE JSON, DÙNG XUYÊN SUỐT NHIỀU TẬP)
# ─────────────────────────────────────────────────────────────────────────
#
# QUAN TRỌNG: glossary và character_profiles ban đầu chỉ là "điểm khởi đầu"
# (phân tích từ 1 vài tập đầu hoặc toàn bộ script nếu có sẵn). Sau MỖI TẬP,
# hàm update_context_after_episode() sẽ tự cập nhật lại 2 trường này +
# plot_summary_so_far, để phản ánh đúng diễn biến/thay đổi quan hệ nhân vật
# tính đến thời điểm hiện tại — KHÔNG dùng cố định 1 bản phân tích từ đầu
# cho toàn bộ series, vì cốt truyện luôn phát triển (nhân vật mới, twist,
# quan hệ đổi chiều...).

@dataclass
class SeriesContext:
    series_id: str = "default"
    genre: str = "Phụ đề phim"
    target_style: str = "Tự nhiên, dễ nghe"
    output_requirements: str = "Phụ đề phim, câu ngắn gọn dễ đọc"
    glossary: Dict[str, str] = field(default_factory=dict)       # giữ lại để tương thích ngược, không dùng khi build prompt nữa
    character_profiles: str = ""    # lưu đoạn phân tích thể loại + xưng hô (y hệt "clean_ctx" bên Gemini)
    plot_summary_so_far: str = ""   # tóm tắt diễn biến tính đến tập gần nhất
    previous_context: str = ""      # vài câu dịch gần nhất (trong-tập, nối khối)
    last_episode_done: int = 0      # tập cuối cùng đã xử lý xong

    def update_previous_context(self, translated_lines: List[str]) -> None:
        """Giữ lại N câu dịch cuối cùng để nối mạch giữa các KHỐI trong cùng 1 tập."""
        tail = translated_lines[-PREVIOUS_CONTEXT_LINES:]
        self.previous_context = "\n".join(tail)

    def merge_glossary(self, new_terms: Dict[str, str]) -> None:
        """Thêm thuật ngữ mới, KHÔNG ghi đè thuật ngữ đã khóa sẵn (giữ nhất quán)."""
        for zh, vi in (new_terms or {}).items():
            if zh not in self.glossary:
                self.glossary[zh] = vi

    # ---- lưu / đọc để dùng xuyên suốt nhiều tập, nhiều lần chạy app ----
    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.__dict__, f, ensure_ascii=False, indent=2)

    @classmethod
    def load_or_create(cls, path: str, **defaults) -> "SeriesContext":
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls(**data)
        return cls(**defaults)


# ─────────────────────────────────────────────────────────────────────────
# GỌI API DEEPSEEK (OpenAI-compatible)
# ─────────────────────────────────────────────────────────────────────────

class DeepSeekAPIError(Exception):
    pass


def _call_deepseek(api_key: str, system_prompt: str, user_prompt: str,
                    max_tokens: int = 4000, temperature: float = 0.3) -> str:
    """Gọi endpoint chat/completions của DeepSeek (tương thích OpenAI)."""
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_BASE_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            last_err = f"HTTP {e.code}: {err_body[:300]}"
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(RETRY_DELAY_SEC * attempt)  # backoff tăng dần
                continue
            raise DeepSeekAPIError(last_err)
        except Exception as e:
            last_err = str(e)
            time.sleep(RETRY_DELAY_SEC * attempt)
            continue

    raise DeepSeekAPIError(f"Gọi DeepSeek thất bại sau {MAX_RETRIES} lần: {last_err}")


# ─────────────────────────────────────────────────────────────────────────
# BƯỚC 1: PHÂN TÍCH BỐI CẢNH — Y HỆT CÁCH GEMINI ĐANG LÀM (GỌI 1 LẦN)
# ─────────────────────────────────────────────────────────────────────────
# Dùng ĐÚNG nguyên văn câu hỏi mà GeminiTranslateThread._translate_smart()
# đang gửi, lấy mẫu 150 dòng đầu (đúng số dòng Gemini đang lấy), để 2 engine
# cho ra bối cảnh cùng "chất lượng" và cùng cách suy luận, không lệch nhau.

CONTEXT_SAMPLE_LINES = 150   # số dòng lấy mẫu để phân tích - KHỚP với Gemini


def _build_context_prompt(sample_text: str) -> str:
    """Nguyên văn prompt phân tích bối cảnh, copy y hệt từ GeminiTranslateThread."""
    return (
        "Đọc kịch bản sau và hãy đóng vai nhà phê bình phim để trả lời NGẮN GỌN 2 câu hỏi:\n"
        "1. Phim này thuộc thể loại gì?\n"
        "2. Nhận diện các nhân vật xuất hiện và cách họ xưng hô với nhau sao cho chuẩn "
        "tiếng Việt nhất (Ví dụ: Lâm Xung (y/hắn), đại ca - đệ, hoàng thượng - thần thiếp, "
        "anh - em...)?\n\n"
        "TUYỆT ĐỐI KHÔNG DỊCH VĂN BẢN. CHỈ TRẢ VỀ TÓM TẮT ĐÁNH GIÁ (Khoảng 4-5 dòng).\n"
        f"Văn bản trích xuất:\n{sample_text}"
    )


def analyze_movie_context(api_key: str, full_script_text: str,
                           ctx: SeriesContext) -> None:
    """Gọi DeepSeek 1 lần với mẫu 150 dòng đầu (y hệt Gemini), lấy về đoạn tóm
    tắt thể loại + xưng hô, lưu vào ctx.character_profiles để dùng lại cho
    MỌI khối dịch phía sau — cùng cơ chế "phân tích 1 lần, dịch xuyên suốt"
    như Gemini đang làm."""
    sample_lines = full_script_text.splitlines()[:CONTEXT_SAMPLE_LINES]
    sample_text = "\n".join(sample_lines)
    context_prompt = _build_context_prompt(sample_text)

    try:
        raw = _call_deepseek(
            api_key=api_key,
            system_prompt="Bạn là nhà phê bình phim chuyên nghiệp, trả lời đúng theo yêu cầu, không dịch văn bản.",
            user_prompt=context_prompt,
            max_tokens=500,
            temperature=0.3,
        )
    except DeepSeekAPIError:
        ctx.character_profiles = "Không thể phân tích bối cảnh. Hệ thống sẽ dịch theo mặc định."
        return

    clean_ctx = re.sub(r'```[a-zA-Z]*\n?', '', raw).replace('```', '')
    ctx.character_profiles = clean_ctx.strip()


# ─────────────────────────────────────────────────────────────────────────
# BƯỚC 2: DỊCH TỪNG KHỐI, DÙNG LẠI NGỮ CẢNH ĐÃ LƯU
# ─────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────
# BƯỚC 2: DỊCH TỪNG KHỐI — BUILD PROMPT + XỬ LÝ PHẢN HỒI Y HỆT GEMINI
# ─────────────────────────────────────────────────────────────────────────
# Toàn bộ phần dưới đây port lại chính xác logic trong
# GeminiTranslateThread._translate_smart() (translate_tab.py), chỉ đổi nơi
# gọi từ trình duyệt Gemini sang gọi thẳng API DeepSeek. Không dùng file
# rule_translate.txt hay hệ placeholder riêng nào nữa — 2 engine giờ dùng
# chung 1 bộ quy tắc, chỉ khác kênh gọi.

# Các câu mở đầu thừa mà model hay tự thêm dù đã dặn không làm - cắt bỏ y hệt
# Gemini đang làm ("Phẫu thuật sub").
_FORBIDDEN_STARTS = ("dạ,", "dạ ", "vâng", "đây là bản", "bản dịch",
                     "dưới đây là", "chắc chắn", "tất nhiên", "theo yêu cầu")

# Tỷ lệ ký tự Hán/Trung còn sót tối đa cho phép trước khi coi là "AI lười dịch"
# và bắt dịch lại - khớp ngưỡng 3% Gemini đang dùng.
_CJK_LEFTOVER_MAX_RATIO = 0.03

MAX_CHUNK_RETRIES = 5


def _build_strict_rules(n_lines: int, context_text: str) -> str:
    """Nguyên văn 5 quy tắc cứng, copy y hệt từ GeminiTranslateThread."""
    return f"""QUY TẮC TUYỆT ĐỐI (VI PHẠM SẼ LỖI PHẦN MỀM):
1. BẮT BUỘC trả về ĐÚNG {n_lines} dòng. Không gộp, không tách.
2. KHÔNG giải thích, KHÔNG CHÀO HỎI. KHÔNG dùng thẻ markdown. CHỈ TRẢ VỀ NỘI DUNG DỊCH.
3. BẮT BUỘC SỬ DỤNG TIẾNG VIỆT CÓ DẤU CHUẨN CHÍNH TẢ (Ví dụ: "Không", tuyệt đối không viết "Khong"). Đảm bảo giữ nguyên các dấu thanh của tiếng Việt.
4. DỊCH SẠCH 100%, KHÔNG ĐỂ SÓT LẠI KÝ TỰ HÁN/TRUNG QUỐC.
5. ÁP DỤNG BỐI CẢNH VÀ XƯNG HÔ SAU ĐÂY VÀO BẢN DỊCH:
---
{context_text}
---"""


def _clean_ai_response(raw: str) -> List[str]:
    """Dọn phản hồi y hệt logic 'Phẫu thuật sub' bên Gemini: bỏ code fence,
    bỏ dấu *, cắt câu chào hỏi thừa ở đầu, dồn khoảng trắng/từ lặp, lọc bỏ
    dòng "rác" AI tự chèn thêm (số đếm trần trụi / dòng timestamp tự bịa)."""
    res_clean = re.sub(r'```[a-zA-Z]*\n?', '', raw)
    res_clean = res_clean.replace('```', '').replace('*', '')
    temp_lines_raw = [l.strip() for l in res_clean.split('\n') if l.strip()]

    while temp_lines_raw:
        first_line = temp_lines_raw[0].strip().lower()
        if first_line.startswith(_FORBIDDEN_STARTS):
            temp_lines_raw.pop(0)
        else:
            break

    temp_lines = []
    _timestamp_line_re = re.compile(r'^\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}$')
    for line in temp_lines_raw:
        stripped = line.strip()
        # Lọc bỏ dòng "rác" AI đôi khi tự chèn thêm dù bị cấm: số thứ tự trần
        # trụi (VD "1", "2") hoặc dòng timestamp tự bịa - không phải bản dịch
        # thật, giữ lại sẽ làm lệch vị trí ghép vào block gốc.
        if stripped.isdigit():
            continue
        if _timestamp_line_re.match(stripped):
            continue
        clean_line = re.sub(r'(\b\w+\b)(?:\s+\1){2,}', r'\1', line, flags=re.IGNORECASE)
        clean_line = re.sub(r' +', ' ', clean_line)
        temp_lines.append(clean_line)
    return temp_lines


def _cjk_leftover_ratio(lines: List[str]) -> float:
    joined = " ".join(lines)
    total_chars = len(re.sub(r"\s", "", joined))
    if total_chars == 0:
        return 0.0
    cjk_count = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7a3]", joined))
    return cjk_count / total_chars


def translate_chunk(api_key: str, ctx: SeriesContext,
                     blocks: List[SrtBlock], log_callback=None) -> List[str]:
    """Dịch 1 khối SrtBlock. Build prompt + retry + kiểm tra chất lượng y hệt
    GeminiTranslateThread._translate_smart(), chỉ đổi kênh gọi sang DeepSeek."""
    def _log(msg):
        if log_callback:
            log_callback(msg)

    context_text = ctx.character_profiles or "(chưa phân tích được bối cảnh)"
    if ctx.previous_context:
        context_text += f"\n\nCâu dịch ngay trước đó (để nối mạch tự nhiên):\n{ctx.previous_context}"

    remaining = list(blocks)
    batch_size = len(remaining)
    retry_count = 0
    result_lines: List[str] = []

    while remaining and retry_count < MAX_CHUNK_RETRIES:
        current_batch = remaining[:batch_size]
        lines_to_translate = [b.text for b in current_batch]
        text_payload = "\n".join(lines_to_translate)

        strict_rules = _build_strict_rules(len(lines_to_translate), context_text)
        system_prompt = f"{ctx.genre}\n\n{strict_rules}"
        user_prompt = f"Dịch {len(lines_to_translate)} dòng sau:\n{text_payload}"

        try:
            raw = _call_deepseek(
                api_key=api_key,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=16000,
                temperature=0.3,
            )
        except DeepSeekAPIError as e:
            _log(f"⚠️ Lỗi gọi DeepSeek: {e}\n")
            retry_count += 1
            time.sleep(RETRY_DELAY_SEC)
            continue

        temp_lines = _clean_ai_response(raw)

        if not temp_lines:
            _log("⚠️ AI không trả về dòng nào hợp lệ. Thử lại...\n")
            retry_count += 1
            continue

        ratio = _cjk_leftover_ratio(temp_lines)
        if ratio > _CJK_LEFTOVER_MAX_RATIO:
            _log(f"⚠️ AI lười dịch, sót chữ Hán ({ratio*100:.1f}% > 3%). Ép dịch lại!\n")
            if retry_count >= 2 and batch_size > 20:
                batch_size = max(1, batch_size // 2)
                _log(f"✂️ Tự động chia nhỏ: {batch_size} câu để AI bớt lười...\n")
            retry_count += 1
            continue

        if len(temp_lines) < len(current_batch):
            _log(f"⚠️ AI dịch thiếu ({len(temp_lines)}/{len(current_batch)} dòng). Chia nhỏ dịch lại...\n")
            if batch_size > 15:
                batch_size = max(1, batch_size // 2)
            retry_count += 1
            continue

        # Đủ dòng, sạch chữ Hán -> chấp nhận, tiến sang phần còn lại
        result_lines.extend(temp_lines[:len(current_batch)])
        remaining = remaining[len(current_batch):]
        batch_size = len(remaining)
        retry_count = 0  # reset đếm lỗi khi 1 batch đã qua thành công

    # Còn sót block chưa dịch được sau khi hết lượt retry -> giữ nguyên bản gốc
    # (an toàn, không làm crash tiến trình, tương tự Gemini fallback).
    while len(result_lines) < len(blocks):
        result_lines.append(blocks[len(result_lines)].text)

    return result_lines


# ─────────────────────────────────────────────────────────────────────────
# HÀM CHÍNH: DỊCH — CÓ 2 CHẾ ĐỘ TÙY KHÁCH CHỌN TẢI LẺ HAY TRỌN BỘ
# ─────────────────────────────────────────────────────────────────────────
#
# - MODE "each"  (khách tải lẻ từng tập): mỗi tập tự phân tích ngữ cảnh
#   RIÊNG (chỉ dựa trên nội dung tập đó, vì các tập khác chưa chắc đã có
#   sẵn) rồi dịch theo khối trong phạm vi tập đó.
#
# - MODE "full"  (khách chọn tải trọn bộ): phân tích ngữ cảnh 1 LẦN DUY
#   NHẤT dựa trên TOÀN BỘ script của mọi tập đã tải (nên biết trước hết
#   plot twist, nhân vật mới xuất hiện muộn, quan hệ đổi chiều...), sau đó
#   dịch tuần tự qua các tập bằng đúng 1 ctx dùng chung — previous_context
#   cũng chảy liên tục từ tập trước sang tập sau, không bị ngắt quãng.

class DeepSeekTranslator:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("Thiếu DeepSeek API key.")
        self.api_key = api_key

    # ---- dùng nội bộ: dịch 1 danh sách block đã có sẵn ctx, cập nhật progress ----
    def _translate_blocks(self, ctx: SeriesContext, blocks: List[SrtBlock],
                           done_offset: int, total: int, progress_callback) -> int:
        done = done_offset
        for start in range(0, len(blocks), LINES_PER_CHUNK):
            chunk = blocks[start:start + LINES_PER_CHUNK]
            translated_texts = translate_chunk(self.api_key, ctx, chunk)

            for b, vi_text in zip(chunk, translated_texts):
                b.text = vi_text

            ctx.update_previous_context(translated_texts)

            done += len(chunk)
            if progress_callback:
                progress_callback(done, total)
        return done

    # ── MODE "each": KHÁCH TẢI LẺ -> mỗi tập dịch độc lập, tự phân tích riêng ──
    def translate_episode(
        self,
        srt_path: str,
        genre: str = "Phụ đề phim",
        target_style: str = "Tự nhiên, dễ nghe",
        output_requirements: str = "Phụ đề phim, câu ngắn gọn dễ đọc",
        output_path: Optional[str] = None,
        progress_callback=None,
    ) -> str:
        """Dịch 1 tập ĐỘC LẬP — dùng khi khách chỉ tải lẻ 1-vài tập, không có
        đủ dữ liệu các tập khác để phân tích ngữ cảnh chung."""
        with open(srt_path, "r", encoding="utf-8") as f:
            srt_text = f.read()

        blocks = parse_srt(srt_text)
        if not blocks:
            raise ValueError(f"Không parse được block phụ đề nào trong {srt_path}")

        ctx = SeriesContext(
            genre=genre, target_style=target_style,
            output_requirements=output_requirements,
        )

        full_script_text = "\n".join(b.text for b in blocks)
        analyze_movie_context(self.api_key, full_script_text, ctx)

        self._translate_blocks(ctx, blocks, done_offset=0, total=len(blocks),
                                progress_callback=progress_callback)

        if not output_path:
            base, _ext = os.path.splitext(srt_path)
            output_path = f"{base}_vi.srt"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(rebuild_srt(blocks))

        return output_path

    # ── MODE "full": KHÁCH CHỌN TRỌN BỘ -> phân tích 1 lần, dịch xuyên suốt ──
    def translate_full_series(
        self,
        srt_paths: List[str],   # đã sắp xếp đúng thứ tự Tập 1, Tập 2, ...
        genre: str = "Phụ đề phim",
        target_style: str = "Tự nhiên, dễ nghe",
        output_requirements: str = "Phụ đề phim, câu ngắn gọn dễ đọc",
        output_suffix: str = "_vi",
        progress_callback=None,   # callback(done, total) tính theo TỔNG số dòng cả series
    ) -> List[str]:
        """
        Dịch NGUYÊN 1 series đã tải trọn bộ. Phân tích ngữ cảnh 1 LẦN dựa
        trên toàn bộ nội dung mọi tập -> model biết trước hết plot, tên
        nhân vật xuất hiện muộn, quan hệ thay đổi... rồi dịch tuần tự qua
        từng tập, ngữ cảnh (glossary/hồ sơ nhân vật/previous_context) chảy
        liên tục xuyên suốt cả series, không bị reset giữa các tập.
        Trả về danh sách đường dẫn file .srt tiếng Việt theo đúng thứ tự tập.
        """
        episodes_blocks: List[List[SrtBlock]] = []
        for p in srt_paths:
            with open(p, "r", encoding="utf-8") as f:
                blocks = parse_srt(f.read())
            if not blocks:
                raise ValueError(f"Không parse được block phụ đề nào trong {p}")
            episodes_blocks.append(blocks)

        ctx = SeriesContext(
            genre=genre, target_style=target_style,
            output_requirements=output_requirements,
        )

        # ---- BƯỚC 1: phân tích ngữ cảnh 1 LẦN cho TOÀN BỘ series ----
        full_script_text = "\n\n".join(
            f"[Tập {i+1}]\n" + "\n".join(b.text for b in blocks)
            for i, blocks in enumerate(episodes_blocks)
        )
        analyze_movie_context(self.api_key, full_script_text, ctx)

        # ---- BƯỚC 2: dịch tuần tự từng tập, DÙNG CHUNG 1 ctx xuyên suốt ----
        total_lines = sum(len(b) for b in episodes_blocks)
        done = 0
        output_paths = []
        for srt_path, blocks in zip(srt_paths, episodes_blocks):
            done = self._translate_blocks(ctx, blocks, done_offset=done,
                                           total=total_lines, progress_callback=progress_callback)

            base, _ext = os.path.splitext(srt_path)
            out_path = f"{base}{output_suffix}.srt"
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(rebuild_srt(blocks))
            output_paths.append(out_path)

        return output_paths

    # ── Hàm điều phối: tự chọn mode theo lựa chọn của khách trên UI ──
    def translate(
        self,
        srt_paths: List[str],
        download_mode: str,          # "each" (tải lẻ) hoặc "full" (trọn bộ)
        **kwargs,
    ) -> List[str]:
        if download_mode == "full" and len(srt_paths) > 1:
            return self.translate_full_series(srt_paths, **kwargs)

        # "each", hoặc "full" nhưng chỉ có 1 tập -> xử lý như tải lẻ cho đơn giản
        progress_callback = kwargs.pop("progress_callback", None)
        output_suffix = kwargs.pop("output_suffix", "_vi")
        outputs = []
        for p in srt_paths:
            base, _ext = os.path.splitext(p)
            outputs.append(
                self.translate_episode(
                    p, output_path=f"{base}{output_suffix}.srt",
                    progress_callback=progress_callback, **kwargs
                )
            )
        return outputs


# ─────────────────────────────────────────────────────────────────────────
# CHẠY THỬ TRỰC TIẾP:
#   python deepseek_translate.py each  Tap_01.srt sk-xxxx
#   python deepseek_translate.py full  Tap_01.srt Tap_02.srt Tap_03.srt sk-xxxx
# ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 4:
        print("Cách dùng:")
        print("  Tải lẻ 1 tập : python deepseek_translate.py each <file.srt> <api_key>")
        print("  Trọn bộ nhiều tập: python deepseek_translate.py full <file1.srt> <file2.srt> ... <api_key>")
        sys.exit(1)

    mode = sys.argv[1]              # "each" hoặc "full"
    key = sys.argv[-1]               # api key luôn là tham số cuối cùng
    srt_files = sys.argv[2:-1]       # phần giữa là danh sách file .srt

    if mode not in ("each", "full"):
        print(f"Mode không hợp lệ: {mode!r} (chỉ nhận 'each' hoặc 'full')")
        sys.exit(1)

    translator = DeepSeekTranslator(api_key=key)

    def _print_progress(done, total):
        print(f"  Đã dịch {done}/{total} dòng...")

    download_mode = "full" if mode == "full" else "each"
    outputs = translator.translate(
        srt_paths=srt_files,
        download_mode=download_mode,
        progress_callback=_print_progress,
    )

    print("\nXong! Các file tiếng Việt:")
    for o in outputs:
        print(" -", o)

