# -*- coding: utf-8 -*-
"""Máy trạng thái ghi live: chờ sóng → ghi → đứt → nối lại.

Ghi live không giống tải video. Tải video là việc có đầu có cuối, biết trước tổng
dung lượng. Ghi live là một cái canh: phòng có thể chưa mở, mở rồi lại rớt mạng
giữa chừng, chủ kênh tắt rồi bật lại sau 30 giây. Nên mỗi phòng ở đây là MỘT luồng
riêng chạy vòng đời riêng, và "ffmpeg thoát" KHÔNG có nghĩa là xong việc.

Tiến độ đọc từ `ffmpeg -progress pipe:1` chứ không đoán theo kích thước file: đó là
con số ffmpeg tự khai (đã ghi được bao nhiêu giây, bao nhiêu byte), nên vẫn đúng cả
khi đang cắt file theo phút.

Cạm bẫy đã vấp và đã vá:
  · HLS bọc AAC kiểu ADTS mà MP4 không chứa được → thiếu `-bsf:a aac_adtstoasc` là
    ffmpeg "Error muxing a packet" rồi đẻ ra file 0.2 giây trông y như thành công.
  · Dừng bằng terminate() làm hỏng đuôi file MP4 → gửi 'q' qua stdin để ffmpeg tự
    đóng file tử tế, hết kiên nhẫn mới giết.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests

from . import live as LV
from .config import Settings
from .util import (NO_WINDOW, Ticker, ffmpeg_path, safe_name, unique_path)

# ───────────────────────── trạng thái ─────────────────────────
IDLE = "idle"          # vừa thêm, chưa dò lần nào
WAITING = "waiting"    # phòng chưa lên sóng — đang canh
LIVE = "live"          # phòng đang phát nhưng chưa cho ghi (tự động đang tắt)
QUEUED = "queued"      # tới lượt rồi nhưng hết suất ghi song song — đang xếp hàng
REC = "rec"            # đang ghi
PAUSED = "paused"      # người dùng tạm dừng GHI nhưng VẪN canh phòng
ENDED = "ended"        # phòng đã tắt sóng
STOPPED = "stopped"    # người dùng dừng hẳn, thôi canh
ERROR = "error"        # dò hỏng / ffmpeg hỏng

STATE_TEXT = {IDLE: "chờ dò", WAITING: "canh sóng", LIVE: "đang phát",
              QUEUED: "xếp hàng", REC: "ĐANG GHI", PAUSED: "tạm dừng",
              ENDED: "tắt sóng", STOPPED: "đã dừng", ERROR: "lỗi"}
ACTIVE_STATES = (IDLE, WAITING, LIVE, QUEUED, REC, PAUSED)

_KV = re.compile(r"^([a-z_]+)=\s*(.*)$")


@dataclass
class Task:
    """Một phòng đang được canh. Luồng nền ghi vào, GUI chỉ đọc."""
    rid: str
    state: str = IDLE
    # thông tin phòng (cập nhật mỗi lần dò)
    room_id: str = ""
    nick: str = ""
    title: str = ""
    viewers: str = ""
    cover: str = ""
    # luồng đang ghi
    quality: str = ""
    res: str = ""
    proto: str = ""
    lane: str = ""                   # làn mạng đang dùng (xem lanes.py)
    # tiến độ phiên hiện tại
    secs: float = 0.0
    bytes: int = 0
    kbits: float = 0.0
    file: Optional[Path] = None
    parts: List[Path] = field(default_factory=list)
    # tổng cả buổi (cộng dồn qua các lần nối lại)
    sessions: int = 0
    note: str = ""
    error: str = ""
    started_at: float = 0.0
    # nội bộ
    armed: bool = False              # người dùng bấm Ghi khi tự-động đang tắt
    _stop: bool = False
    _paused: bool = False
    _proc: Optional[subprocess.Popen] = None
    _thread: Optional[threading.Thread] = None
    _tick: Ticker = field(default_factory=Ticker)
    # Đánh thức vòng canh ngay lập tức khi người dùng bấm nút, thay vì để nó ngủ
    # cho hết chu kỳ dò rồi mới biết.
    _wake: threading.Event = field(default_factory=threading.Event)
    _off_secs: float = 0.0
    _off_bytes: int = 0

    @property
    def speed(self) -> float:
        return self._tick.speed

    @property
    def alive(self) -> bool:
        return self.state in ACTIVE_STATES

    def label(self) -> str:
        return self.nick or self.rid


LogFn = Callable[[str], None]
UpdFn = Callable[[Task], None]


class Recorder:
    """Quản lý nhiều phòng cùng lúc. An toàn để GUI gọi từ luồng chính."""

    def __init__(self, cfg: Settings, cookie: Optional[Dict[str, str]] = None,
                 on_update: Optional[UpdFn] = None, on_log: Optional[LogFn] = None,
                 lanes=None):
        self.cfg = cfg
        self.cookie = dict(cookie or {})
        self.on_update = on_update or (lambda t: None)
        self.on_log = on_log or (lambda m: None)
        self.lanes = lanes                      # LaneManager hoặc None
        self.tasks: Dict[str, Task] = {}
        self._lock = threading.Lock()
        self._rec_slots = threading.Semaphore(max(1, int(cfg.max_parallel)))

    # ───────────────────────── làn mạng ─────────────────────────
    def _proxy(self, rid: str) -> str:
        """Proxy của làn phòng này. Làn direct thì rơi về proxy chung (nếu có)."""
        if self.lanes is not None:
            try:
                p = self.lanes.proxy_for(rid)
                if p:
                    return p
            except Exception:
                pass
        return (self.cfg.proxy or "").strip()

    def _lane_name(self, rid: str) -> str:
        if self.lanes is None:
            return ""
        try:
            return self.lanes.lane_for(rid).id
        except Exception:
            return ""

    # ───────────────────────── điều khiển ─────────────────────────
    def add(self, rid: str, armed: bool = False) -> Task:
        """Thêm phòng vào danh sách canh. Thêm lại phòng đã dừng = bật lại nó."""
        with self._lock:
            t = self.tasks.get(rid)
            if t and t.alive:
                if armed:            # thêm lại phòng đang tạm dừng = bảo nó ghi tiếp
                    t.armed = True
                    t._paused = False
                    t._wake.set()
                return t
            t = Task(rid=rid, armed=armed)
            if self.tasks.get(rid):          # giữ lại thông tin phòng đã biết
                old = self.tasks[rid]
                t.nick, t.title, t.cover, t.room_id = old.nick, old.title, old.cover, old.room_id
            self.tasks[rid] = t
        t._thread = threading.Thread(target=self._run, args=(t,), daemon=True,
                                     name=f"live-{rid}")
        t._thread.start()
        return t

    def stop(self, rid: str) -> None:
        """Dừng HẲN: thôi ghi, thôi canh. Muốn ghi lại thì add() lần nữa."""
        t = self.tasks.get(rid)
        if not t:
            return
        t._stop = True
        t._wake.set()
        self._quit_proc(t)

    def pause(self, rid: str) -> None:
        """Tạm dừng GHI nhưng vẫn canh phòng — file đang ghi được đóng tử tế.

        Khác stop(): vòng canh còn sống, số liệu đã ghi được giữ nguyên, nên
        resume() ghi tiếp cộng dồn vào cùng một Task (file mới, tổng vẫn cộng).
        """
        t = self.tasks.get(rid)
        if not t or t._stop:
            return
        t._paused = True
        t.armed = False
        t._wake.set()
        self._quit_proc(t)

    def resume(self, rid: str) -> Optional[Task]:
        """Ghi tiếp. Phòng đã dừng hẳn thì dựng lại vòng canh từ đầu."""
        t = self.tasks.get(rid)
        alive_thread = bool(t and t._thread and t._thread.is_alive())
        if not t or t._stop or not alive_thread:
            return self.add(rid, armed=True)
        t._paused = False
        t.armed = True
        t._wake.set()
        return t

    def toggle(self, rid: str) -> None:
        """Một nút cho cả hai chiều — đúng cái nút trên mỗi dòng của bảng."""
        t = self.tasks.get(rid)
        if t and not t._paused and not t._stop and t.alive:
            self.pause(rid)
        else:
            self.resume(rid)

    def stop_all(self) -> None:
        for rid in list(self.tasks):
            self.stop(rid)

    def pause_all(self) -> None:
        for rid in list(self.tasks):
            self.pause(rid)

    def resume_all(self) -> None:
        for rid in list(self.tasks):
            self.resume(rid)

    def remove(self, rid: str) -> None:
        self.stop(rid)
        with self._lock:
            self.tasks.pop(rid, None)
        if self.lanes is not None:
            try:
                self.lanes.release(rid)
            except Exception:
                pass

    def arm(self, rid: str) -> None:
        """Bấm 'Ghi' thủ công khi chế độ tự động đang tắt."""
        t = self.tasks.get(rid)
        if t:
            t.armed = True
            t._stop = False

    def rids(self) -> List[str]:
        with self._lock:
            return list(self.tasks)

    # ───────────────────────── vòng đời một phòng ─────────────────────────
    def _run(self, t: Task) -> None:
        cfg = self.cfg
        while not t._stop:
            try:
                t.lane = self._lane_name(t.rid)
                room = LV.room_info(t.rid, self.cookie, self._proxy(t.rid))
                t.error = ""
            except LV.LiveError as e:
                t.error = str(e)
                t.state = ERROR
                t.note = "sẽ dò lại"
                self._emit(t)
                self.on_log(f"[{t.label()}] dò hỏng: {e}")
                if not self._nap(t, cfg.poll_every):
                    break
                continue

            self._absorb(t, room)

            # Tạm dừng: vẫn dò để bảng biết phòng còn sóng hay không, chỉ không ghi.
            if t._paused:
                t.state = PAUSED
                t.note = "tạm dừng — bấm Ghi tiếp để ghi lại"
                self._emit(t)
                if not self._nap(t, min(cfg.poll_every, 30)):
                    break
                continue

            if not LV.is_live(room):
                t.state = ENDED if t.sessions else WAITING
                t.note = "chờ chủ kênh lên sóng"
                self._emit(t)
                if not self._nap(t, cfg.poll_every):
                    break
                continue

            if not (cfg.auto_record or t.armed):
                t.state = LIVE
                t.note = "đang phát — bấm Ghi để bắt đầu"
                self._emit(t)
                if not self._nap(t, cfg.poll_every):
                    break
                continue

            self._record_once(t, room)

            if t._stop:
                break
            if t._paused:
                continue          # vòng sau rơi vào nhánh tạm dừng, KHÔNG thoát
            if not cfg.auto_resume:
                t.state = STOPPED
                t.note = "đã ghi xong một phiên"
                self._emit(t)
                break
            t.note = "luồng dứt — dò lại xem còn phát không"
            self._emit(t)
            if not self._nap(t, 5):
                break

        if not t.alive or t._stop:
            t.state = STOPPED if t._stop else t.state
        t.note = t.note or ""
        self._emit(t)

    def _nap(self, t: Task, seconds: float) -> bool:
        """Ngủ nhưng tỉnh NGAY khi người dùng bấm nút. False = phải thoát.

        Dùng Event chứ không phải vòng sleep 0.25s: bấm "Ghi tiếp" mà phải chờ tới
        nửa giây mới nhúc nhích thì cảm giác như nút không ăn.
        """
        t._wake.wait(max(0.5, seconds))
        t._wake.clear()
        return not t._stop

    def _absorb(self, t: Task, room: dict) -> None:
        t.room_id = room.get("id_str") or t.room_id
        t.nick = LV.nickname(room) or t.nick
        t.title = room.get("title") or t.title
        t.viewers = LV.viewers(room) or ""
        t.cover = LV.cover_url(room) or t.cover

    # ───────────────────────── một phiên ffmpeg ─────────────────────────
    def _record_once(self, t: Task, room: dict) -> None:
        cfg = self.cfg
        try:
            q = LV.pick(LV.qualities(room), cfg.quality, cfg.proto)
        except LV.LiveError as e:
            t.state = ERROR
            t.error = str(e)
            self._emit(t)
            self.on_log(f"[{t.label()}] {e}")
            self._nap(t, cfg.poll_every)
            return
        url = q.get(cfg.proto) or q.get("flv") or ""
        if not url:
            t.state = ERROR
            t.error = f"mức {q.get('key')} không có luồng {cfg.proto}"
            self._emit(t)
            return

        ff = ffmpeg_path()
        if not ff:
            t.state = ERROR
            t.error = "không tìm thấy ffmpeg trong PATH"
            self._emit(t)
            self.on_log("✗ Thiếu ffmpeg — cài rồi thêm vào PATH: https://www.gyan.dev/ffmpeg/builds/")
            t._stop = True
            return

        dst, listfile = self._plan_path(t, q)
        t.quality, t.res, t.proto = q.get("key", ""), q.get("res", ""), cfg.proto
        t.file = dst
        t.parts = []
        t._off_secs, t._off_bytes = t.secs, t.bytes
        t._tick = Ticker()
        t.sessions += 1
        if not t.started_at:
            t.started_at = time.time()
        # Hết suất ghi song song thì phòng này NẰM CHỜ ở đây. Trước đây vẫn hiện
        # "ĐANG GHI" với 00:00 / 0 B — nhìn y như treo mà thật ra chỉ đang xếp hàng.
        t.state = QUEUED
        t.note = f"chờ suất ghi (tối đa {cfg.max_parallel} phòng cùng lúc)"
        self._emit(t)

        if cfg.save_meta:
            self._write_meta(t, room, dst)

        # Ở chế độ cắt file, ffmpeg khai total_size=N/A (nó không biết tổng của cả
        # chuỗi miếng) — không có đường vòng này thì cột dung lượng đứng im ở 0 B.
        probe = None
        if listfile is not None:
            prefix = dst.name.split("%03d")[0]

            def probe():
                try:
                    return sum(f.stat().st_size for f in dst.parent.glob(prefix + "*")
                               if f.is_file())
                except Exception:
                    return t.bytes - t._off_bytes

        with self._rec_slots:
            if t._stop:
                return
            t.state = REC
            # ffmpeg im lặng vài giây đầu (đang bắt tay CDN + gom keyframe) — không
            # nói gì thì bảng hiện 00:00 / 0 B trông như treo.
            t.note = "đang kết nối…"
            self._emit(t)
            proxy = self._proxy(t.rid)
            self.on_log(f"[{t.label()}] ghi {LV.label(q)} qua {cfg.proto.upper()}"
                        + (f" · làn {t.lane}" if t.lane and t.lane != "direct" else "")
                        + f" → {dst.name}")
            if self.lanes is not None:
                self.lanes.mark_busy(t.rid, True)
            try:
                rc = self._pump(t, self._cmd(ff, url, dst, listfile, proxy), probe)
            finally:
                if self.lanes is not None:
                    self.lanes.mark_busy(t.rid, False)

        if listfile and listfile.exists():
            try:
                t.parts = [dst.parent / ln.strip() for ln in
                           listfile.read_text(encoding="utf-8", errors="replace").splitlines()
                           if ln.strip()]
            except Exception:
                pass

        # Chạm trần thời lượng thì DỪNG HẲN, đừng để auto_resume mở phiên mới —
        # không thì "ghi 15 phút" biến thành vô số file 15 phút nối nhau.
        cap = self.cfg.max_hours * 3600
        hit_cap = cap > 0 and (t.secs - t._off_secs) >= cap - 2

        if t._stop:
            t.state = STOPPED
            t.note = f"đã dừng — giữ lại {self._size_note(t)}"
        elif t._paused:
            t.state = PAUSED
            t.note = f"tạm dừng — giữ lại {self._size_note(t)}"
        elif hit_cap:
            t._stop = True
            t.state = STOPPED
            t.note = f"đủ trần thời lượng — {self._size_note(t)}"
        elif rc not in (0, 255):
            t.note = f"ffmpeg thoát mã {rc}"
            if t.error:
                self.on_log(f"[{t.label()}] {t.error}")
        self._emit(t)

    def _cmd(self, ff: str, url: str, dst: Path, listfile: Optional[Path],
             proxy: str = "") -> List[str]:
        cfg = self.cfg
        # KHÔNG dùng -nostdin: stdin phải mở để còn gửi 'q' cho ffmpeg đóng file tử tế.
        cmd = [ff, "-hide_banner", "-loglevel", "warning", "-y", "-user_agent", LV.UA]
        if proxy:
            cmd += ["-http_proxy", proxy]
        # reconnect: CDN live hay ngắt vài giây rồi có lại — để ffmpeg tự nối, đỡ
        # phải dựng lại cả tiến trình và mất đoạn giữa.
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-rw_timeout", "15000000", "-i", url]
        if cfg.max_hours > 0:
            cmd += ["-t", str(int(cfg.max_hours * 3600))]
        cmd += ["-c", "copy"]
        if dst.suffix.lower() == ".mp4":
            # HLS gói AAC kiểu ADTS, MP4 không chứa được. Với FLV thì bsf này vô hại.
            cmd += ["-bsf:a", "aac_adtstoasc"]
        if listfile is not None:                       # cắt file theo phút
            cmd += ["-f", "segment", "-segment_time", str(int(cfg.segment_min * 60)),
                    "-reset_timestamps", "1", "-segment_list", str(listfile),
                    "-segment_list_type", "flat"]
            if dst.suffix.lower() == ".mp4":
                cmd += ["-segment_format", "mp4", "-segment_format_options",
                        "movflags=+frag_keyframe+empty_moov+default_base_moof"]
        elif dst.suffix.lower() == ".mp4":
            # Mất điện giữa chừng mà file thường thì mất trắng; fragmented vẫn phát được.
            cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof"]
        cmd += ["-progress", "pipe:1", "-nostats", str(dst)]
        return cmd

    def _pump(self, t: Task, cmd: List[str],
              size_probe: Optional[Callable[[], int]] = None) -> int:
        """Chạy ffmpeg, đọc -progress để cập nhật tiến độ, gom stderr để báo lỗi."""
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                 errors="replace", bufsize=1, creationflags=NO_WINDOW)
        except Exception as e:
            t.state = ERROR
            t.error = f"không chạy được ffmpeg: {str(e)[:100]}"
            self._emit(t)
            return -1
        t._proc = p

        tail: List[str] = []

        def drain_err():
            for line in p.stderr or []:
                line = line.strip()
                if not line:
                    continue
                tail.append(line)
                del tail[:-6]
                if self.cfg.log_verbose:
                    self.on_log(f"[{t.label()}] ffmpeg: {line[:160]}")
        threading.Thread(target=drain_err, daemon=True).start()

        # total_size của muxer segment quay về 0 mỗi lần cắt file mới -> cộng dồn tay,
        # không thì thanh dung lượng tụt về 0 giữa buổi ghi.
        seg_base = 0
        prev = 0
        last_emit = 0.0
        for raw in (p.stdout or []):
            m = _KV.match(raw.strip())
            if not m:
                continue
            k, v = m.group(1), m.group(2)
            if k == "out_time_us" and v.isdigit():
                t.secs = t._off_secs + int(v) / 1_000_000
            elif k == "total_size":
                if v.isdigit():
                    cur = int(v)
                    if cur < prev:      # muxer segment quay số về 0 mỗi lần cắt
                        seg_base += prev
                    prev = cur
                    t.bytes = t._off_bytes + seg_base + cur
                elif size_probe is not None:
                    t.bytes = t._off_bytes + size_probe()
                else:
                    continue
                t._tick.update(t.bytes)
            elif k == "bitrate":
                mm = re.match(r"([\d.]+)kbits/s", v)
                t.kbits = float(mm.group(1)) if mm else t.kbits
            elif k == "progress":
                if t.note == "đang kết nối…":
                    t.note = ""
                now = time.monotonic()
                if now - last_emit > 0.4 or v == "end":
                    last_emit = now
                    self._emit(t)
        rc = p.wait()
        t._proc = None
        if tail and rc not in (0, 255):
            t.error = tail[-1][:200]
        return rc

    def _quit_proc(self, t: Task) -> None:
        """Gửi 'q' cho ffmpeg để nó đóng file tử tế; cứng đầu mới giết."""
        p = t._proc
        if not p or p.poll() is not None:
            return
        try:
            if p.stdin:
                p.stdin.write("q\n")
                p.stdin.flush()
        except Exception:
            pass
        try:
            p.wait(timeout=8)
            return
        except Exception:
            pass
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    # ───────────────────────── đường dẫn & meta ─────────────────────────
    def _plan_path(self, t: Task, q: dict):
        cfg = self.cfg
        now = time.localtime()
        stem = safe_name(cfg.name_tpl.format(
            nick=safe_name(t.nick or t.rid, 40), rid=t.rid, room=t.room_id or "",
            title=safe_name(t.title or "", 40),
            date=time.strftime("%Y%m%d", now), time=time.strftime("%H%M%S", now),
            quality=q.get("key", ""), res=q.get("res", "")), 90)
        base = Path(cfg.out_dir)
        if cfg.room_subdir:
            base = base / safe_name(t.nick or t.rid, 50)
        base.mkdir(parents=True, exist_ok=True)
        if cfg.segment_min > 0:
            # muxer segment tự điền %03d; -segment_list cho biết đã đẻ ra file nào.
            return base / f"{stem}_p%03d.{cfg.ext}", base / f"{stem}.parts.txt"
        return unique_path(base / f"{stem}.{cfg.ext}"), None

    def _write_meta(self, t: Task, room: dict, dst: Path) -> None:
        stem = dst.name.split("_p%03d")[0].rsplit(".", 1)[0]
        try:
            info = LV.meta(room)
            info["web_rid"] = t.rid
            (dst.parent / f"{stem}.info.json").write_text(
                json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        url = t.cover
        if not url:
            return
        try:
            r = requests.get(url, headers={"User-Agent": LV.UA}, timeout=20)
            if r.ok and r.content:
                ext = ".jpg" if "jpeg" in r.headers.get("Content-Type", "") else ".webp"
                (dst.parent / f"{stem}{ext}").write_bytes(r.content)
        except Exception:
            pass

    def _size_note(self, t: Task) -> str:
        from .util import human_size
        return human_size(t.bytes)

    def _emit(self, t: Task) -> None:
        try:
            self.on_update(t)
        except Exception:
            pass
