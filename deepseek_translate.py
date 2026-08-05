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
RULE_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "rule_translate.txt")

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
    glossary: Dict[str, str] = field(default_factory=dict)       # {"师尊": "sư tôn", ...}
    character_profiles: str = ""                                   # cập nhật dần qua từng tập
    plot_summary_so_far: str = ""                                   # tóm tắt diễn biến tính đến tập gần nhất
    previous_context: str = ""                                      # vài câu dịch gần nhất (trong-tập, nối khối)
    last_episode_done: int = 0                                      # tập cuối cùng đã xử lý xong

    # ---- dùng khi build prompt cho 1 khối dịch ----
    def glossary_as_text(self) -> str:
        if not self.glossary:
            return "(chưa có thuật ngữ nào được khóa — tự chọn cách dịch nhất quán)"
        return "\n".join(f"* {zh} → {vi}" for zh, vi in self.glossary.items())

    def character_profiles_as_text(self) -> str:
        base = self.character_profiles or "(chưa có hồ sơ nhân vật)"
        if self.plot_summary_so_far:
            base += f"\n\nDiễn biến câu chuyện tính đến tập gần nhất:\n{self.plot_summary_so_far}"
        return base

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
# BƯỚC 1: PHÂN TÍCH NGỮ CẢNH TOÀN BỘ PHIM (GỌI 1 LẦN DUY NHẤT)
# ─────────────────────────────────────────────────────────────────────────

_ANALYZE_SYSTEM_PROMPT = """Bạn là trợ lý phân tích kịch bản phim tiếng Trung.
Nhiệm vụ: đọc toàn bộ lời thoại được cung cấp và trả về DUY NHẤT 1 JSON object
với 2 khóa:
- "glossary": object ánh xạ thuật ngữ/danh xưng đặc thù Hán-Việt sang bản dịch
  tiếng Việt cố định (ví dụ {"师尊": "sư tôn", "宗门": "tông môn"}). Chỉ liệt kê
  thuật ngữ lặp lại nhiều lần hoặc quan trọng, không cần liệt kê từ thông dụng.
- "character_profiles": chuỗi text mô tả ngắn gọn từng nhân vật xuất hiện
  (tên, giới tính, tuổi tác ước lượng, địa vị, quan hệ với các nhân vật khác,
  cách xưng hô giữa họ). Viết bằng tiếng Việt, súc tích, dùng gạch đầu dòng.

CHỈ trả về JSON hợp lệ, không thêm giải thích, không thêm markdown code fence."""


def analyze_movie_context(api_key: str, full_script_text: str,
                           ctx: SeriesContext) -> None:
    """
    Gọi DeepSeek 1 lần với toàn bộ kịch bản (tận dụng cửa sổ ngữ cảnh 1M token)
    để trích xuất glossary + hồ sơ nhân vật, rồi lưu thẳng vào `ctx`.
    Đây chính là bước "lưu ngữ cảnh cho nguyên bộ phim" — chỉ làm 1 lần,
    kết quả được tái sử dụng cho MỌI khối dịch phía sau.
    """
    raw = _call_deepseek(
        api_key=api_key,
        system_prompt=_ANALYZE_SYSTEM_PROMPT,
        user_prompt=full_script_text,
        max_tokens=3000,
        temperature=0.1,
    )

    # DeepSeek đôi khi vẫn bọc kết quả trong ```json ... ``` dù đã dặn không làm vậy
    cleaned = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()

    try:
        parsed = json.loads(cleaned)
        ctx.merge_glossary(parsed.get("glossary", {}) or {})
        ctx.character_profiles = (parsed.get("character_profiles", "") or "").strip()
    except json.JSONDecodeError:
        # Nếu model trả sai định dạng, không chặn tiến trình — dịch vẫn chạy
        # được, chỉ là thiếu phần glossary/hồ sơ nhân vật tự động.
        ctx.character_profiles = ctx.character_profiles or "(không phân tích được tự động)"


# ─────────────────────────────────────────────────────────────────────────
# BƯỚC 2: DỊCH TỪNG KHỐI, DÙNG LẠI NGỮ CẢNH ĐÃ LƯU
# ─────────────────────────────────────────────────────────────────────────

def _load_rule_prompt() -> str:
    with open(RULE_PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _fill_rule_prompt(rule_template: str, ctx: SeriesContext,
                       source_text: str, raw_translation: str = "") -> str:
    """Điền các placeholder {{...}} trong rule_translate.txt bằng dữ liệu thật."""
    filled = rule_template
    filled = filled.replace("{{SOURCE_TEXT}}", source_text)
    filled = filled.replace("{{RAW_TRANSLATION}}", raw_translation or "(chưa có bản dịch thô)")
    filled = filled.replace("{{GENRE}}", ctx.genre)
    filled = filled.replace("{{TARGET_STYLE}}", ctx.target_style)
    filled = filled.replace("{{GLOSSARY}}", ctx.glossary_as_text())
    filled = filled.replace("{{CHARACTER_PROFILES}}", ctx.character_profiles_as_text())
    filled = filled.replace("{{PREVIOUS_CONTEXT}}", ctx.previous_context or "(đây là khối đầu tiên của phim)")
    filled = filled.replace("{{OUTPUT_REQUIREMENTS}}", ctx.output_requirements)
    return filled


def translate_chunk(api_key: str, ctx: SeriesContext,
                     rule_template: str, blocks: List[SrtBlock]) -> List[str]:
    """
    Dịch 1 khối gồm nhiều SrtBlock. Trả về danh sách text tiếng Việt theo
    ĐÚNG thứ tự block đầu vào (dùng số dòng để model không lẫn lộn).
    """
    numbered_source = "\n".join(f"[{i+1}] {b.text}" for i, b in enumerate(blocks))
    system_prompt = _fill_rule_prompt(rule_template, ctx, source_text=numbered_source)

    # Yêu cầu model trả về đúng format [n] để mình parse lại theo thứ tự,
    # tránh trường hợp model gộp/tách câu làm lệch số dòng.
    user_prompt = (
        "Dịch các dòng phụ đề sau sang tiếng Việt theo đúng system prompt đã cho.\n"
        "Trả về ĐÚNG số dòng, mỗi dòng theo format: [số] bản dịch\n"
        "KHÔNG thêm giải thích, không gộp dòng, không đổi thứ tự.\n\n"
        f"{numbered_source}"
    )

    raw = _call_deepseek(
        api_key=api_key,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=16000,   # đủ dư cho chunk tới ~500 dòng (~8-9k token output thực tế)
        temperature=0.3,
    )

    # Parse lại theo format "[n] nội dung"
    result_map: Dict[int, str] = {}
    for line in raw.splitlines():
        m = re.match(r"^\[(\d+)\]\s*(.*)$", line.strip())
        if m:
            result_map[int(m.group(1))] = m.group(2).strip()

    translated = []
    for i in range(len(blocks)):
        translated.append(result_map.get(i + 1, blocks[i].text))  # fallback: giữ nguyên gốc nếu thiếu dòng

    return translated


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
        self.rule_template = _load_rule_prompt()

    # ---- dùng nội bộ: dịch 1 danh sách block đã có sẵn ctx, cập nhật progress ----
    def _translate_blocks(self, ctx: SeriesContext, blocks: List[SrtBlock],
                           done_offset: int, total: int, progress_callback) -> int:
        done = done_offset
        for start in range(0, len(blocks), LINES_PER_CHUNK):
            chunk = blocks[start:start + LINES_PER_CHUNK]
            translated_texts = translate_chunk(self.api_key, ctx, self.rule_template, chunk)

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

