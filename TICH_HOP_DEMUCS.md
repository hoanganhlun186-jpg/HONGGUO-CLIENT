# Hướng dẫn tích hợp demucs_manager.py vào honggou_tab.py

## Chỉ cần sửa 3 chỗ:

---

### Chỗ 1 — Import ở đầu file (sau dòng import có sẵn)

```python
# Thêm vào sau các import hiện có
try:
    from demucs_manager import ensure_demucs_installed_ui, get_demucs_python
    _DEMUCS_MANAGER_OK = True
except ImportError:
    _DEMUCS_MANAGER_OK = False
```

---

### Chỗ 2 — Hàm `_run_bgm_only` (dòng ~3780)

Tìm đoạn:
```python
def _run_bgm_only(self):
    """Tách nhạc nền độc lập cho các video đã tải."""
    files = getattr(self, '_files_for_stt', None) or ...
    ...
```

Thêm check demucs VÀO ĐẦU hàm, trước mọi thứ khác:
```python
def _run_bgm_only(self):
    """Tách nhạc nền độc lập cho các video đã tải."""
    
    # ── Check & cài demucs nếu chưa có ──────────────────────────────
    if _DEMUCS_MANAGER_OK:
        def _after_install():
            self._run_bgm_only_core()
        ensure_demucs_installed_ui(self, _after_install)
        return
    # Fallback nếu không có demucs_manager: chạy thẳng như cũ
    self._run_bgm_only_core()
```

Sau đó đổi tên phần còn lại của hàm thành `_run_bgm_only_core`:
```python
def _run_bgm_only_core(self):
    """Phần thực thi thật — chỉ gọi khi demucs đã có sẵn."""
    files = getattr(self, '_files_for_stt', None) or getattr(self, 'downloaded_file_paths', [])
    # ... toàn bộ code cũ giữ nguyên ...
```

---

### Chỗ 3 — Trong `_run_bgm_only_core`, sửa cmd_demucs (dòng ~3841)

Tìm:
```python
cmd_demucs = [
    sys.executable, "-m", "demucs.separate",
    ...
]
```

Sửa thành:
```python
_py = get_demucs_python() if _DEMUCS_MANAGER_OK else sys.executable
cmd_demucs = [
    _py, "-m", "demucs.separate",
    ...
]
```

---

### Chỗ tương tự trong luồng lồng tiếng (dòng ~1450)

Tìm:
```python
cmd_demucs = [
    _sys.executable, "-m", "demucs.separate",
    ...
]
```

Sửa thành:
```python
_py = get_demucs_python() if _DEMUCS_MANAGER_OK else _sys.executable
cmd_demucs = [
    _py, "-m", "demucs.separate",
    ...
]
```

---

## build.yml — KHÔNG cần thêm gì

Không cần thêm `pip install demucs` vào build.yml nữa.
Không cần thêm `--include-package=demucs` vào Nuitka.
File exe vẫn nhẹ như cũ!

Chỉ cần thêm vào Nuitka:
```
--include-module=demucs_manager
```

---

## Kết quả

- **Khách không dùng tách nhạc:** không tốn gì, không ảnh hưởng gì ✅  
- **Khách dùng lần đầu:** popup hỏi → bấm Cài ngay → tự tải ~500MB → xong ✅  
- **Khách dùng lần sau:** vào thẳng, không hỏi lại ✅  
- **File exe vẫn nhẹ như cũ** ✅  
- **Auto-update không thay đổi** ✅  
