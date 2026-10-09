# -*- coding: utf-8 -*-
"""HGLIVE — bắt luồng LIVE của Douyin.

    py -3 hglive.py                       → mở giao diện
    py -3 hglive.py "<link live>"         → ghi bằng dòng lệnh
    py -3 hglive.py "<link live>" --list  → chỉ xem phòng + các mức nét
    py -3 hglive.py --help                → tất cả tuỳ chọn

Cần: python 3.9+, `pip install requests PySide6`, và ffmpeg trong PATH.
Toàn bộ logic nằm ở gói hglive/ — file này chỉ là vỏ.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from hglive.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
