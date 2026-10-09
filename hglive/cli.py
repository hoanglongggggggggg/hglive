# -*- coding: utf-8 -*-
"""Dòng lệnh HGLIVE — dùng ĐÚNG engine với GUI, không có đường code riêng.

    py -3 hglive.py "<link>" --list          # xem phòng + các mức nét, không ghi
    py -3 hglive.py "<link>"                 # ghi tới khi tắt sóng
    py -3 hglive.py "<link>" -q ld -t 300    # ghi 5 phút mức ld
    py -3 hglive.py "<link>" --wait          # canh, chủ kênh lên sóng là ghi
    py -3 hglive.py "<link>" --url-only      # in URL luồng (dán vào VLC/ffmpeg)
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import cookies as CK
from . import lanes as LN
from . import live as LV
from . import recorder as RC
from .config import EXT_CHOICES, PROTO_CHOICES, QUALITY_CHOICES, Settings
from .util import fmt_hms, has_ffmpeg, human_size, human_speed


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hglive", description="HGLIVE — bắt luồng live Douyin.")
    p.add_argument("link", nargs="?",
                   help="link live.douyin.com/<rid>, link v.douyin.com, hoặc web_rid")
    p.add_argument("--gui", action="store_true", help="mở giao diện (mặc định khi không có link)")
    p.add_argument("--list", action="store_true", help="chỉ liệt kê phòng + mức nét")
    p.add_argument("--url-only", action="store_true", help="chỉ in URL luồng đã chọn")
    p.add_argument("-q", "--quality", default="",
                   choices=[k for k, _ in QUALITY_CHOICES] + [""],
                   help="best | worst | origin/uhd/hd/sd/ld/md/ao")
    p.add_argument("--proto", default="", choices=[k for k, _ in PROTO_CHOICES] + [""])
    p.add_argument("--ext", default="", choices=[k for k, _ in EXT_CHOICES] + [""])
    p.add_argument("-t", "--time", type=float, default=-1.0,
                   help="ghi bao nhiêu giây (mặc định: tới khi tắt sóng)")
    p.add_argument("--segment", type=int, default=-1, help="cắt file mỗi N phút")
    p.add_argument("--out", default="", help="thư mục ra")
    p.add_argument("--cookie", default="", help="file cookie (live chỉ cần ttwid)")
    p.add_argument("--proxy", default="", help="một proxy dùng cho tất cả")
    p.add_argument("--lanes", default="", metavar="MODE",
                   help="làn mạng: off | nordvpn | danh sách proxy ngăn bởi dấu phẩy "
                        "| đường dẫn file .txt mỗi dòng một proxy")
    p.add_argument("--lane-country", default="", help="nước cho làn NordVPN, vd JP")
    p.add_argument("--no-direct", action="store_true",
                   help="không dùng IP máy làm một làn (chỉ đi qua proxy)")
    p.add_argument("--wait", action="store_true", help="canh tới khi phòng lên sóng")
    p.add_argument("--every", type=int, default=-1, help="chu kỳ dò lại khi canh (giây)")
    p.add_argument("--no-resume", action="store_true",
                   help="luồng đứt là dừng luôn, không ghi tiếp")
    p.add_argument("--no-meta", action="store_true", help="không lưu .info.json / ảnh bìa")
    p.add_argument("-v", "--verbose", action="store_true", help="in cả dòng ffmpeg")
    return p


def _apply(cfg: Settings, a) -> Settings:
    if a.out:
        cfg.out_dir = a.out
    if a.cookie:
        cfg.cookie_path = a.cookie
    if a.proxy:
        cfg.proxy = a.proxy
    if a.quality:
        cfg.quality = a.quality
    if a.proto:
        cfg.proto = a.proto
    if a.ext:
        cfg.ext = a.ext
    if a.segment >= 0:
        cfg.segment_min = a.segment
    if a.every >= 0:
        cfg.poll_every = a.every
    if a.time >= 0:
        cfg.max_hours = a.time / 3600.0
    cfg.auto_record = True
    cfg.auto_resume = not a.no_resume
    cfg.save_meta = not a.no_meta
    cfg.log_verbose = a.verbose
    cfg.max_parallel = 1
    if a.lane_country:
        cfg.proxy_country = a.lane_country
    if a.no_direct:
        cfg.proxy_include_direct = False
    spec = (a.lanes or "").strip()
    if spec:
        if spec.lower() in ("off", "none", "0"):
            cfg.proxy_mode = "off"
        elif spec.lower() in ("nord", "nordvpn", "wg"):
            cfg.proxy_mode = "nordvpn"
        else:
            # danh sách proxy: đưa thẳng, hoặc trỏ vào file mỗi dòng một cái
            src = Path(spec)
            if src.is_file():
                urls = [l.strip() for l in
                        src.read_text(encoding="utf-8", errors="replace").splitlines()
                        if l.strip() and not l.strip().startswith("#")]
            else:
                urls = [u.strip() for u in spec.split(",") if u.strip()]
            cfg.proxy_mode = "list"
            cfg.proxy_list = urls
            cfg.proxy_lanes = len(urls)
    cfg.sanitize()
    return cfg


def run(argv: Optional[List[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    if not a.link:
        build_parser().print_help()
        return 2

    cfg = _apply(Settings.load(), a)
    ck = CK.load(cfg.cookie_path)

    # Ghi ra file/pipe thì \r không lùi con trỏ mà thành ký tự rác — chỉ vẽ dòng
    # tiến độ tại chỗ khi đang ở terminal thật.
    tty = bool(getattr(sys.stdout, "isatty", lambda: False)())

    def log(m=""):
        # Dòng tiến độ vẽ bằng \r; không xoá trước khi in thì log của recorder
        # (chạy ở luồng khác) đè lên giữa dòng, đọc ra chữ nghĩa lẫn lộn.
        print(("\r" + " " * 118 + "\r" if tty else "") + str(m), flush=True)

    try:
        rid = LV.resolve_rid(a.link)
    except LV.LiveError as e:
        log(f"✗ {e}")
        return 2
    log(f"web_rid : {rid}   (cookie: {CK.summary(ck)})")

    # ── làn mạng (dựng TRƯỚC khi dò, để chính lần dò đầu cũng đi qua làn) ──
    mgr = None
    if cfg.proxy_mode != "off":
        mgr = LN.LaneManager(cfg, log=log)
        mgr.start()
        for r in mgr.report():
            log(f"  {r['id']:9s} {'IP máy' if r['direct'] else (r['exit_ip'] or r['proxy'])}"
                f"  {r['country'] or ''}"
                + (f"  {r['latency_ms']:.0f}ms" if r["latency_ms"] else ""))
    probe_proxy = mgr.proxy_for(rid) if mgr else cfg.proxy

    def done(code: int) -> int:
        # Thoát kiểu nào cũng phải tắt wireproxy: bỏ sót là tiến trình con sống
        # tiếp sau khi lệnh đã kết thúc, và private key nằm lại trên đĩa.
        if mgr is not None:
            mgr.close()
        return code

    # ── dò một lần để in thông tin ──
    try:
        room = LV.room_info(rid, ck, probe_proxy)
    except LV.LiveError as e:
        log(f"✗ {e}")
        return done(3)
    log(f"phòng   : {room.get('id_str')}")
    log(f"          {LV.describe(room)}")

    qs = LV.qualities(room)
    if qs:
        log(f"\n{len(qs)} mức nét:")
        for q in qs:
            log(f"  {q['key']:7s} {q['res'] or '—':>9s}  {q['codec']:5s}  "
                f"{(q['name'] or ''):4s}  flv {'✓' if q['flv'] else '·'}"
                f"  hls {'✓' if q['hls'] else '·'}  lls {'✓' if q['lls'] else '·'}")
        log("  (vbitrate của API không đáng tin — xếp theo thang nét + độ phân giải)")
    if a.list:
        return done(0)

    if a.url_only:
        if not qs:
            log("✗ phòng không phát luồng nào")
            return done(4)
        q = LV.pick(qs, cfg.quality, cfg.proto)
        log("")
        print(q.get(cfg.proto) or q.get("flv") or "")
        return done(0)

    if not LV.is_live(room) and not a.wait:
        log("✗ phòng không phát. Thêm --wait để canh tới khi lên sóng.")
        return done(4)
    if not has_ffmpeg():
        log("✗ không tìm thấy ffmpeg trong PATH — https://www.gyan.dev/ffmpeg/builds/")
        return done(5)

    # ── ghi ──
    rec = RC.Recorder(cfg, ck, on_log=lambda m: log(m), lanes=mgr)
    t = rec.add(rid, armed=True)
    stopping = {"v": False}

    def bye(*_):
        if stopping["v"]:
            return
        stopping["v"] = True
        log("\n⏹ đang đóng file…")
        rec.stop(rid)
    try:
        signal.signal(signal.SIGINT, bye)
    except Exception:
        pass

    log("")
    last, last_state = "", ""
    while t.alive or (t._thread and t._thread.is_alive()):
        line = (f"  {RC.STATE_TEXT.get(t.state, t.state):10s} {fmt_hms(t.secs):>8s} "
                f"{human_size(t.bytes):>10s}  {human_speed(t.speed):>10s}  "
                f"{t.note or t.error or ''}")[:118]
        if tty:
            if line != last:
                print("\r" + line.ljust(118), end="", flush=True)
                last = line
        elif t.state != last_state:          # ra file/pipe: mỗi lần đổi trạng thái 1 dòng
            last_state = t.state
            print(line, flush=True)
        time.sleep(0.4)
        if not a.wait and t.state in (RC.ENDED, RC.WAITING) and t.sessions:
            rec.stop(rid)
            break
    if tty:
        print()

    if t.file and Path(t.file).exists():
        log(f"✓ {t.file}  ({human_size(t.bytes)}, {fmt_hms(t.secs)})")
    elif t.parts:
        log(f"✓ {len(t.parts)} miếng trong {Path(t.parts[0]).parent}  ({human_size(t.bytes)})")
    elif t.error:
        log(f"✗ {t.error}")
        return done(6)
    cfg.save()
    return done(0)
