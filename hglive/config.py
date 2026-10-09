# -*- coding: utf-8 -*-
"""Cấu hình HGLIVE — lưu JSON cạnh tool, hỏng file cũng không làm chết app."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field, fields
from pathlib import Path
from typing import Any, List

from .util import app_dir

ROOT = app_dir()
CONFIG_PATH = ROOT / "hglive.config.json"

QUALITY_CHOICES = [
    ("best", "Nét nhất (mức cao nhất phòng đang phát)"),
    ("origin", "origin — nguyên gốc / 高清"),
    ("uhd", "uhd — 蓝光"),
    ("hd", "hd — 超清"),
    ("sd", "sd — 高清"),
    ("ld", "ld — 标清"),
    ("md", "md — 流畅"),
    ("worst", "Nhẹ nhất (tiết kiệm ổ đĩa)"),
    ("ao", "Chỉ tiếng (audio-only)"),
]

PROTO_CHOICES = [
    ("flv", "FLV — độ trễ thấp, mặc định"),
    ("hls", "HLS — chịu mạng chập hơn, trễ ~10-30s"),
]

PROXY_MODE_CHOICES = [
    ("off", "Tắt — mọi phòng đi chung IP máy"),
    ("list", "Danh sách proxy — mỗi proxy một làn"),
    ("nordvpn", "NordVPN WireGuard — tự dựng làn bằng wireproxy"),
]
PROXY_MODES = {m for m, _ in PROXY_MODE_CHOICES}

EXT_CHOICES = [
    ("ts", "TS — Ctrl+C hay mất điện vẫn phát được (khuyên dùng)"),
    ("mp4", "MP4 — tiện chia sẻ (đã ép fragmented cho an toàn)"),
    ("flv", "FLV — giữ nguyên gói gốc, không remux"),
]


@dataclass
class Settings:
    # ── nơi lưu ──
    out_dir: str = str(ROOT / "recordings")
    cookie_path: str = ""            # chỉ cần ttwid; để trống thì tool tự xin
    proxy: str = ""                  # http://user:pass@host:port

    # ── chọn luồng ──
    quality: str = "best"
    proto: str = "flv"
    ext: str = "ts"

    # ── hành vi ghi ──
    auto_record: bool = True         # thấy phòng lên sóng là ghi ngay, không cần bấm
    auto_resume: bool = True         # luồng đứt giữa buổi mà phòng còn phát -> nối tiếp
    poll_every: int = 45             # chu kỳ dò lại khi đang chờ sóng (giây)
    segment_min: int = 0             # cắt file mỗi N phút (0 = một file liền mạch)
    max_hours: float = 0.0           # trần thời lượng mỗi lần ghi (0 = không giới hạn)
    max_parallel: int = 3            # số phòng được GHI cùng lúc (chờ sóng thì không tính)
    resume_on_start: bool = True     # mở app là canh lại đúng danh sách phòng lần trước

    # ── đặt tên / tổ chức ──
    name_tpl: str = "{nick}_{date}_{time}_{quality}"
    room_subdir: bool = True         # mỗi phòng một thư mục riêng
    save_meta: bool = True           # ghi kèm .info.json + ảnh bìa

    # ── làn mạng / proxy (xem lanes.py) ──
    proxy_mode: str = "off"          # off | list | nordvpn
    proxy_lanes: int = 2             # số làn proxy muốn dựng (chưa kể direct)
    proxy_include_direct: bool = True   # có dùng cả IP máy làm một làn không
    proxy_rooms_per_lane: int = 2    # trần số phòng bám dính mỗi làn
    proxy_list: List[str] = field(default_factory=list)   # chế độ list
    nordvpn_token: str = ""          # chế độ nordvpn — LƯU CHỮ THƯỜNG trong config
    nordvpn_token_file: str = ""     # để trống thì tự dò (biến môi trường, .nordvpn_token/.env cạnh tool)
    proxy_country: str = ""          # "" = bất kỳ nước nào; hoặc "JP" / "Singapore"
    proxy_max_latency_ms: float = 600
    proxy_probe_candidates: int = 10  # thử mấy server mới lấy con nhanh nhất
                                     # (đo thật: chỉ ~1/4 server NordVPN chịu bắt tay)
    proxy_socks_start: int = 1080
    proxy_http_start: int = 8282
    wireproxy_path: str = ""         # để trống = tự tìm rồi tự tải
    rotate_min_hours: float = 2.0
    rotate_max_hours: float = 3.0
    rotate_min_interval_min: float = 20.0

    # ── giao diện ──
    log_verbose: bool = False
    window: List[int] = field(default_factory=lambda: [1120, 720])
    watch: List[str] = field(default_factory=list)   # web_rid đang theo dõi, mở lại còn

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Settings":
        s = cls()
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return s
        known = {f.name for f in fields(cls)}
        for k, v in (raw or {}).items():
            if k in known:
                try:
                    setattr(s, k, v)
                except Exception:
                    pass
        s.sanitize()
        return s

    def save(self, path: Path = CONFIG_PATH) -> None:
        try:
            Path(path).write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except Exception:
            pass

    def sanitize(self) -> None:
        # Dò dày quá là tự chuốc risk-control; 15s đã là dày hơn người xem thật.
        self.poll_every = max(15, min(3600, int(self.poll_every or 45)))
        self.segment_min = max(0, min(720, int(self.segment_min or 0)))
        self.max_hours = max(0.0, min(48.0, float(self.max_hours or 0)))
        self.max_parallel = max(1, min(12, int(self.max_parallel or 1)))
        if self.quality not in {q for q, _ in QUALITY_CHOICES}:
            self.quality = "best"
        if self.proto not in {p for p, _ in PROTO_CHOICES}:
            self.proto = "flv"
        if self.ext not in {e for e, _ in EXT_CHOICES}:
            self.ext = "ts"
        if not self.out_dir:
            self.out_dir = str(ROOT / "recordings")
        if not self.name_tpl.strip():
            self.name_tpl = "{nick}_{date}_{time}_{quality}"
        self.watch = [str(r) for r in (self.watch or []) if str(r).strip()][:200]

        if self.proxy_mode not in PROXY_MODES:
            self.proxy_mode = "off"
        self.proxy_lanes = max(0, min(16, int(self.proxy_lanes or 0)))
        self.proxy_rooms_per_lane = max(1, min(20, int(self.proxy_rooms_per_lane or 1)))
        self.proxy_list = [str(u).strip() for u in (self.proxy_list or []) if str(u).strip()][:32]
        self.proxy_max_latency_ms = max(50.0, min(5000.0, float(self.proxy_max_latency_ms or 600)))
        self.proxy_probe_candidates = max(1, min(40, int(self.proxy_probe_candidates or 10)))
        self.proxy_socks_start = max(1024, min(65000, int(self.proxy_socks_start or 1080)))
        self.proxy_http_start = max(1024, min(65000, int(self.proxy_http_start or 8282)))
        self.rotate_min_hours = max(0.1, min(72.0, float(self.rotate_min_hours or 2)))
        self.rotate_max_hours = max(self.rotate_min_hours, min(96.0, float(self.rotate_max_hours or 3)))
        self.rotate_min_interval_min = max(1.0, min(600.0, float(self.rotate_min_interval_min or 20)))
        # Bật proxy mà không có làn nào thì coi như tắt — đỡ phải kiểm hai điều kiện
        # ở mọi chỗ dùng.
        if self.proxy_mode != "off" and self.proxy_lanes <= 0:
            self.proxy_mode = "off"

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)
