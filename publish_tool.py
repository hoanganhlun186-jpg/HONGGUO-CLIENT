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
    parts[2] += 1 # Tự động tăng số cuối lên 1 (VD: 1.0.0 -> 1.0.1)
    return f"{parts[0]}.{parts[1]}.{parts[2]}"

def update_file_version(new_version):
    with open(CLIENT_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    # Ghi đè số phiên bản mới vào thẳng file code
    content = re.sub(r'APP_VERSION\s*=\s*"\d+\.\d+\.\d+"', f'APP_VERSION = "{new_version}"', content)
    with open(CLIENT_FILE, "w", encoding="utf-8") as f:
        f.write(content)

def publish():
    current = get_current_version()
    new_ver = bump_version(current)
    tag_name = f"v{new_ver}"
    
    try:
        update_file_version(new_ver)
        
        # Các lệnh Git đẩy code lên GitHub
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(["git", "commit", "-m", f"Release {tag_name}"], check=True)
        subprocess.run(["git", "tag", tag_name], check=True)
        
        # Lưu ý: Cần đổi nhánh 'main' thành 'master' nếu kho của bạn dùng master
        subprocess.run(["git", "push", "origin", "main"], check=True)
        subprocess.run(["git", "push", "origin", tag_name], check=True)
        
        messagebox.showinfo("Thành công", f"Đã đẩy phiên bản {tag_name} lên GitHub!\n\nRobot GitHub Actions đang tiến hành build file .exe. Vài phút sau Server sẽ tự nhận bản cập nhật mới!")
        root.destroy()
    except Exception as e:
        messagebox.showerror("Lỗi", f"Có lỗi xảy ra:\n{str(e)}\n\nHãy chắc chắn bạn đã chạy lệnh 'git init' và 'git remote add origin' rồi nhé!")
        
        # Trả lại version cũ nếu lỗi
        update_file_version(current)

# Giao diện đơn giản bằng Tkinter
root = tk.Tk()
root.title("Auto Publish - AnhStudio")
root.geometry("350x150")
root.eval('tk::PlaceWindow . center')

tk.Label(root, text="Công Cụ Phát Hành Bản Cập Nhật", font=("Arial", 12, "bold")).pack(pady=10)
tk.Label(root, text=f"Phiên bản hiện tại: v{get_current_version()}", fg="blue").pack()

tk.Button(root, text="Nâng cấp & Phát hành ngay", bg="#10b981", fg="white", font=("Arial", 10, "bold"), command=publish).pack(pady=15)

root.mainloop()
