import os
import re
import subprocess
import tkinter as tk
from tkinter import messagebox

CLIENT_FILE = "honggou_tab.py"

def get_current_version():
    try:
        with open(CLIENT_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        match = re.search(r'APP_VERSION\s*=\s*"(\d+\.\d+\.\d+)"', content)
        return match.group(1) if match else "1.0.0"
    except Exception:
        return "1.0.0"

def bump_version(current_version):
    parts = [int(x) for x in current_version.split(".")]
    parts[2] += 1  # Tự động tăng số cuối lên 1 (VD: 1.0.0 -> 1.0.1)
    return f"{parts[0]}.{parts[1]}.{parts[2]}"

def update_file_version(new_version):
    with open(CLIENT_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    # Ghi đè số phiên bản mới vào thẳng file code
    content = re.sub(r'APP_VERSION\s*=\s*"\d+\.\d+\.\d+"',
                     f'APP_VERSION = "{new_version}"', content)
    with open(CLIENT_FILE, "w", encoding="utf-8") as f:
        f.write(content)

def _run(args):
    """Chạy lệnh git, trả về (thành công, output). Không raise để tự xử lý."""
    r = subprocess.run(args, capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0, out.strip()

def _current_branch():
    """Tự dò nhánh hiện tại (main hay master) thay vì cứng 'main'."""
    ok, out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    return out if ok and out else "main"

def publish():
    current = get_current_version()
    new_ver = bump_version(current)
    tag_name = f"v{new_ver}"
    branch = _current_branch()
    tag_created = False

    try:
        update_file_version(new_ver)

        # Đẩy code lên GitHub
        ok, out = _run(["git", "add", "."])
        if not ok:
            raise RuntimeError(f"git add lỗi:\n{out}")

        # Kiểm tra có gì để commit không (tránh 'nothing to commit' làm kẹt)
        ok_diff, _ = _run(["git", "diff", "--cached", "--quiet"])
        if ok_diff:
            raise RuntimeError("Không có thay đổi nào để phát hành.\n"
                               "Bạn cần sửa gì đó trong code trước khi publish.")

        ok, out = _run(["git", "commit", "-m", f"Release {tag_name}"])
        if not ok:
            raise RuntimeError(f"git commit lỗi:\n{out}")

        ok, out = _run(["git", "tag", tag_name])
        if not ok:
            raise RuntimeError(f"git tag lỗi (tag {tag_name} có thể đã tồn tại):\n{out}")
        tag_created = True

        ok, out = _run(["git", "push", "origin", branch])
        if not ok:
            raise RuntimeError(f"git push nhánh '{branch}' lỗi:\n{out}")

        ok, out = _run(["git", "push", "origin", tag_name])
        if not ok:
            raise RuntimeError(f"git push tag lỗi:\n{out}")

        messagebox.showinfo(
            "Thành công",
            f"Đã đẩy phiên bản {tag_name} lên GitHub (nhánh '{branch}')!\n\n"
            "Robot GitHub Actions đang build file .exe. "
            "Vài phút sau Server sẽ tự nhận bản cập nhật mới!")
        root.destroy()

    except Exception as e:
        # Dọn dẹp khi lỗi: xóa tag local vừa tạo để lần sau bấm lại không kẹt
        if tag_created:
            _run(["git", "tag", "-d", tag_name])
        # Trả lại version cũ trong file
        update_file_version(current)
        messagebox.showerror(
            "Lỗi",
            f"Có lỗi xảy ra:\n{str(e)}\n\n"
            "Đã hoàn tác (version cũ được giữ nguyên).\n"
            "Kiểm tra: đã 'git init' và 'git remote add origin' chưa?")

# Giao diện đơn giản bằng Tkinter
root = tk.Tk()
root.title("Auto Publish - BoomStudio")
root.geometry("350x150")
root.eval('tk::PlaceWindow . center')

tk.Label(root, text="Công Cụ Phát Hành Bản Cập Nhật",
         font=("Arial", 12, "bold")).pack(pady=10)
tk.Label(root, text=f"Phiên bản hiện tại: v{get_current_version()}",
         fg="blue").pack()

tk.Button(root, text="Nâng cấp & Phát hành ngay", bg="#10b981", fg="white",
          font=("Arial", 10, "bold"), command=publish).pack(pady=15)

root.mainloop()
