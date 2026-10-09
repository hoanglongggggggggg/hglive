# -*- coding: utf-8 -*-
"""HGLIVE — bắt luồng LIVE của Douyin (thư viện + CLI + GUI).

Thư mục độc lập: không import gì từ hgdl/, chép nguyên hglive/ + hglive.py sang
máy khác là chạy. Chỉ cần `pip install requests PySide6` và ffmpeg trong PATH.

Vì sao tách hẳn khỏi HGDL: nhánh live dùng endpoint khác (webcast), cách xác thực
khác (chỉ cần cookie ttwid, KHÔNG cần ký a_bogus, KHÔNG cần đăng nhập), và vòng đời
việc cũng khác — tải video là "có giới hạn, xong là hết", còn ghi live là "chờ →
ghi → đứt → ghi tiếp", chạy hàng giờ. Nhét chung vào HGDL chỉ làm rối cả hai.

  live.py      dò phòng, bóc danh sách luồng            (thuần mạng, không trạng thái)
  recorder.py  máy trạng thái ghi: chờ sóng → ghi → nối lại
  lanes.py     làn mạng: trải nhiều phòng ra nhiều lối ra, xoay IP khi rảnh
  nordvpn.py   dựng proxy WireGuard bằng wireproxy (không cần app NordVPN)
  gui.py       giao diện PySide6
  cli.py       dòng lệnh
"""

__version__ = "1.0.0"
__all__ = ["live", "recorder", "lanes", "nordvpn", "config", "cookies", "util"]
