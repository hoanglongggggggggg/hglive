# -*- coding: utf-8 -*-
"""Tiện ích chung của HGLIVE: định dạng, tên file an toàn, tìm ffmpeg.

Gói này cố ý KHÔNG import hgdl — hglive/ là thư mục độc lập, chép ra máy khác vẫn
chạy được một mình. Vài hàm trùng tên với hgdl/util.py là chủ ý, không phải sót.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Windows: gọi ffmpeg mà không bật cửa sổ console (GUI không bị nháy đen).
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def app_dir() -> Path:
    """Thư mục "nhà": cạnh .exe khi đã đóng gói, cạnh gói hglive/ khi chạy source.

    Bản onefile giải nén vào thư mục tạm rồi xoá khi thoát — lấy __file__ làm gốc
    thì config và recordings/ sẽ bốc hơi sau mỗi lần chạy.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


_BAD_CHARS = re.compile(r'[\/:*?"<>|\r\n\t\x00-\x1f]+')
_WS = re.compile(r"\s+")
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(s: str, maxlen: int = 60, fallback: str = "live") -> str:
    """Tên file hợp lệ trên Windows: bỏ ký tự cấm, gộp khoảng trắng, cắt dài,
    không để lại dấu chấm/space cuối (Explorer không mở được)."""
    s = _WS.sub(" ", _BAD_CHARS.sub(" ", s or "")).strip()
    if s.upper().split(".")[0] in _RESERVED:
        s = "_" + s
    return s[:maxlen].strip().rstrip(". ") or fallback


def unique_path(p: Path) -> Path:
    """a.ts → a (2).ts → a (3).ts… — không bao giờ đè file đã ghi."""
    if not p.exists():
        return p
    for i in range(2, 1000):
        q = p.parent / f"{p.stem} ({i}){p.suffix}"
        if not q.exists():
            return q
    return p.parent / f"{p.stem} ({int(time.time())}){p.suffix}"


def human_size(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def human_speed(bps: float) -> str:
    return human_size(bps) + "/s" if bps and bps > 0 else "—"


def human_rate(kbits: float) -> str:
    """kbit/s → 613 kbps / 2.4 Mbps."""
    if not kbits or kbits <= 0:
        return "—"
    return f"{kbits/1000:.1f} Mbps" if kbits >= 1000 else f"{kbits:.0f} kbps"


def fmt_hms(seconds: float) -> str:
    """giây → mm:ss hoặc h:mm:ss. Ghi live tính bằng giờ nên luôn ưu tiên dạng dài."""
    s = int(seconds or 0)
    if s <= 0:
        return "00:00"
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def fmt_count(n) -> str:
    """12345 → 12.3K (kiểu thống kê Douyin). Nhận cả chuỗi vì API trả cả hai kiểu."""
    if n is None or n == "":
        return "—"
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n/1000:.1f}K".replace(".0K", "K")
    return f"{n/1_000_000:.1f}M".replace(".0M", "M")


def fmt_clock(ts: Optional[float] = None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


# ───────────────────────── ffmpeg ─────────────────────────
def which(name: str) -> Optional[str]:
    return shutil.which(name) or shutil.which(name + ".exe")


def ffmpeg_path() -> Optional[str]:
    return which("ffmpeg")


def ffprobe_path() -> Optional[str]:
    return which("ffprobe")


def has_ffmpeg() -> bool:
    return ffmpeg_path() is not None


def open_in_explorer(path) -> None:
    """Mở thư mục chứa file và chọn sẵn file đó (Windows), fallback mở thư mục."""
    p = Path(path)
    try:
        if sys.platform == "win32":
            if p.is_file():
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                p.mkdir(parents=True, exist_ok=True)
                os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)] if p.is_file() else ["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])
    except Exception:
        pass


class Ticker:
    """Tốc độ trung bình trượt (EMA) — mượt hơn chia tổng/thời gian, và không bị
    đứng hình khi CDN nhả dữ liệu theo cụm như live vẫn hay làm."""

    def __init__(self, halflife: float = 2.0):
        self.halflife = halflife
        self._t = time.monotonic()
        self._n = 0
        self.speed = 0.0

    def update(self, done: int) -> float:
        now = time.monotonic()
        dt = now - self._t
        if dt < 0.2:
            return self.speed
        inst = max(0, done - self._n) / dt
        alpha = 1 - 0.5 ** (dt / self.halflife)
        self.speed = inst if self.speed == 0 else self.speed + alpha * (inst - self.speed)
        self._t, self._n = now, done
        return self.speed
