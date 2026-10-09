# -*- coding: utf-8 -*-
"""Điểm vào chung cho HGLIVE — cùng khuôn với hgdl/app.py.

Nằm TRONG gói để launcher chỉ là vỏ vài dòng, không đẻ ra hai đường code song song.
Không có link trên dòng lệnh → mở GUI. Có link → chạy CLI.
"""
from __future__ import annotations

import sys


def _attach_console() -> None:
    """Bản .exe build windowed (không kèm console) để mở GUI cho sạch; khi người
    dùng gọi từ terminal kèm tham số thì bám vào console của tiến trình cha để in
    ra được — nhờ vậy MỘT file .exe phục vụ cả GUI lẫn CLI."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes
        if not ctypes.windll.kernel32.AttachConsole(-1):
            return
        for name in ("stdout", "stderr"):
            try:
                setattr(sys, name, open("CONOUT$", "w", encoding="utf-8",
                                        errors="replace", buffering=1))
            except Exception:
                pass
    except Exception:
        pass


def _utf8_console() -> None:
    """Console Windows mặc định cp1252 — không có dòng này thì in tên kênh tiếng
    Trung là crash ngay ở dòng log đầu tiên."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            if stream is not None:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    has_link = bool([a for a in argv if not a.startswith("-")])
    wants_cli = has_link or any(a in ("-h", "--help") for a in argv)

    if wants_cli:
        _attach_console()
    _utf8_console()

    if any(a in ("-h", "--help") for a in argv):
        from .cli import build_parser
        build_parser().print_help()
        return 0

    if not wants_cli or "--gui" in argv:
        try:
            from .gui import launch
        except ImportError as e:
            _attach_console()
            _utf8_console()
            print(f"Không mở được giao diện ({e}).\n"
                  f"Cài đặt:  pip install PySide6 requests\n"
                  f'Hoặc dùng dòng lệnh:  hglive "<link live>"')
            return 1
        return launch([a for a in argv if a != "--gui"])

    from .cli import run
    try:
        return run(argv)
    except KeyboardInterrupt:
        print("\nĐã dừng.")
        return 130
