# -*- coding: utf-8 -*-
"""Làn mạng (lane): trải việc ghi ra nhiều lối đi khác nhau thay vì dồn một chỗ.

Chuyển thể ý tưởng từ `proxy/lane_manager.py` của translation_gateway, nhưng đổi
mục đích: bên đó chia làn để MỖI TÀI KHOẢN luôn thấy một IP ổn định; ở đây chia
làn để BĂNG THÔNG không dồn hết vào một đường — ghi 5 phòng cùng lúc qua một IP
thì CDN bóp, còn qua 5 lối thì mỗi lối đi đường riêng.

Một *làn* là một lối ra mạng:
  · `direct`  — IP của chính máy, không proxy.
  · `proxy-N` — chế độ `list`: một proxy HTTP người dùng đưa vào.
                chế độ `nordvpn`: một tiến trình wireproxy riêng (một exit
                WireGuard của NordVPN), mở cổng HTTP cố định ở 127.0.0.1.

Mỗi phòng bám DÍNH một làn: gọi API và kéo luồng đều qua đúng lối đó, nên phía
Douyin thấy một phòng luôn đến từ một IP.

KHÁC CỐT TỬ so với gateway: ở đó xoay IP lúc nào cũng được vì mỗi request chỉ
sống vài giây. Ở đây một lần ghi kéo dài hàng giờ — xoay giữa chừng là đứt luồng.
Nên làn ĐANG GHI thì không bao giờ bị xoay; lệnh xoay xếp lại chờ tới lúc rảnh.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Callable, Dict, List, Optional

from . import nordvpn as NV

LogFn = Optional[Callable[[str], None]]

MODE_OFF = "off"
MODE_LIST = "list"
MODE_NORD = "nordvpn"
DIRECT = "direct"


class Lane:
    def __init__(self, lane_id: str, index: int, proxy_url: Optional[str] = None,
                 socks_port: Optional[int] = None, http_port: Optional[int] = None):
        self.id = lane_id
        self.index = index
        self.proxy_url = proxy_url          # None = đi thẳng
        self.socks_port = socks_port
        self.http_port = http_port
        self.proc: Optional[NV.ProxyProcess] = None
        self.server: Optional[dict] = None
        self.exit_ip: Optional[str] = None
        self.country: Optional[str] = None
        self.latency_ms: Optional[float] = None
        self.bound: List[str] = []          # rid bám dính
        self.busy = 0                       # số phòng ĐANG GHI trên làn này
        self.created_at = time.time()
        self.rotate_after = 0.0
        self.last_rotated_at = 0.0
        self._rotate_reason: Optional[str] = None

    @property
    def is_direct(self) -> bool:
        return self.proxy_url is None

    def request_rotation(self, reason: str) -> None:
        if not self.is_direct and self.proc is not None:
            self._rotate_reason = reason

    def label(self) -> str:
        if self.is_direct:
            return "direct"
        where = self.country or ""
        return f"{self.id}{' ' + where if where else ''}"

    def report(self) -> dict:
        return {"id": self.id, "direct": self.is_direct, "proxy": self.proxy_url,
                "exit_ip": self.exit_ip, "country": self.country,
                "latency_ms": self.latency_ms,
                "server": (self.server or {}).get("name") if self.server else None,
                "bound": list(self.bound), "busy": self.busy}


class LaneManager:
    """Dựng làn, chia phòng vào làn, xoay IP khi làn rảnh.

    An toàn với đa luồng: recorder gọi từ nhiều thread ghi khác nhau.
    """

    def __init__(self, cfg, log: LogFn = None):
        self.cfg = cfg
        self.log = log or (lambda m: None)
        self.lanes: List[Lane] = []
        self.by_room: Dict[str, str] = {}      # rid -> lane_id
        self._lock = threading.RLock()
        self._private_key: Optional[str] = None
        self._binary: Optional[str] = None
        self._servers: List[dict] = []
        self._used_servers: set = set()   # endpoint IP làn khác đang dùng
        self._used_exits: set = set()     # IP RA đã bị chiếm
        self._bad_servers: set = set()    # server đã thử mà không bắt tay được
        self._closed = False
        self._rot_thread: Optional[threading.Thread] = None
        self.status = "chưa bật"
        self.token_source = ""
        self.ready = False

    # ───────────────────────── vòng đời ─────────────────────────
    def start(self) -> None:
        """Dựng làn. CHẶN luồng gọi (vài chục giây với nordvpn) — gọi trong thread."""
        cfg = self.cfg
        with self._lock:
            self.lanes = []
            self.by_room = {}
            self._used_servers = set()
            self._used_exits = set()
        mode = (cfg.proxy_mode or MODE_OFF).strip()

        if mode == MODE_OFF or cfg.proxy_lanes <= 0:
            self._add_direct()
            self.status = "tắt — chỉ đi thẳng"
            self.ready = True
            return

        if cfg.proxy_include_direct or mode == MODE_OFF:
            self._add_direct()

        if mode == MODE_LIST:
            self._start_list()
        elif mode == MODE_NORD:
            self._start_nordvpn()
        else:
            self.status = f"chế độ lạ: {mode}"

        if not any(not l.is_direct for l in self.lanes):
            if not self.lanes:
                self._add_direct()
            self.status = (self.status or "") + " — không dựng được làn proxy nào, đi thẳng"
        self.ready = True
        self.log(f"⚙ làn mạng: {self.summary()}")

    def _add_direct(self) -> Lane:
        lane = Lane(DIRECT, 0)
        with self._lock:
            self.lanes.append(lane)
        return lane

    def _start_list(self) -> None:
        urls = [u.strip() for u in (self.cfg.proxy_list or []) if u.strip()]
        urls = urls[: max(1, int(self.cfg.proxy_lanes))]
        if not urls:
            self.status = "danh sách proxy rỗng"
            return
        for i, url in enumerate(urls, 1):
            lane = Lane(f"proxy-{i}", i, proxy_url=url)
            with self._lock:
                self.lanes.append(lane)
        # Kiểm từng proxy: hỏng thì báo ngay chứ đừng để tới lúc ghi mới lộ.
        for lane in [l for l in self.lanes if not l.is_direct]:
            info = self._probe_http(lane.proxy_url)
            if info:
                lane.exit_ip, lane.country = info.get("ip"), info.get("country")
                lane.latency_ms = info.get("ms")
            else:
                lane.latency_ms = None
                self.log(f"  ⚠ {lane.id} không thông — vẫn giữ, sẽ thử lại khi dùng")
        ok = sum(1 for l in self.lanes if not l.is_direct and l.latency_ms is not None)
        self.status = f"danh sách: {ok}/{len(urls)} proxy thông"

    def _start_nordvpn(self) -> None:
        cfg = self.cfg
        token = (cfg.nordvpn_token or "").strip()
        src = "ô cấu hình"
        if not token:
            # Không có thì đi tìm: file người dùng chỉ → biến môi trường → file cạnh
            # tool (.nordvpn_token / .env). Chỉ log NGUỒN, tuyệt đối không log token.
            token, src = NV.discover_token(cfg.nordvpn_token_file)
        if not token:
            self.status = "thiếu token NordVPN"
            self.log("  ⚠ chế độ NordVPN nhưng không tìm ra token ở đâu — chạy đi thẳng")
            return
        self.token_source = src
        self.log(f"  token NordVPN lấy từ {src} ({NV.mask(token)})")
        n = NV.sweep_stale_dirs()
        if n:
            self.log(f"  dọn {n} thư mục wireproxy còn sót (có private key trong đó)")
        self.status = "đang lấy khoá NordLynx…"
        self._private_key = NV.fetch_private_key(token)
        if not self._private_key:
            self.status = "token NordVPN sai hoặc hết hạn"
            self.log("  ✗ " + self.status)
            return
        try:
            self._binary = NV.ensure_wireproxy(cfg.wireproxy_path, self.log)
        except Exception as e:
            self.status = f"không có wireproxy: {str(e)[:70]}"
            self.log("  ✗ " + self.status)
            return
        cid = NV.country_id_for(cfg.proxy_country) if cfg.proxy_country else None
        self._servers = NV.fetch_wg_servers(cid, 500)
        if not self._servers:
            self.status = "NordVPN không trả server WireGuard nào"
            self.log("  ✗ " + self.status)
            return

        socks, http = cfg.proxy_socks_start, cfg.proxy_http_start
        want = max(1, int(cfg.proxy_lanes))
        up = 0
        for i in range(1, want + 1):
            if self._closed:
                break
            self.status = f"đang dựng làn {i}/{want}…"
            sp, hp = NV.allocate_port_pair(socks, http)
            socks, http = sp + 1, hp + 1
            lane = Lane(f"proxy-{i}", i, proxy_url=f"http://127.0.0.1:{hp}",
                        socks_port=sp, http_port=hp)
            if self._establish(lane):
                lane.last_rotated_at = time.time()
                lane.rotate_after = time.time() + self._rotate_delay()
                with self._lock:
                    self.lanes.append(lane)
                up += 1
                self.log(f"  ✓ {lane.id}: {lane.exit_ip} ({lane.country}) "
                         f"{lane.latency_ms:.0f}ms qua {(lane.server or {}).get('name')}")
            else:
                self.log(f"  ✗ {lane.id} không dựng được — bỏ qua")
        self.status = f"NordVPN: {up}/{want} làn"
        if up:
            self._rot_thread = threading.Thread(target=self._rotation_loop, daemon=True,
                                                name="lane-rotate")
            self._rot_thread.start()

    def close(self) -> None:
        self._closed = True
        with self._lock:
            lanes = list(self.lanes)
        for lane in lanes:
            if lane.proc:
                try:
                    lane.proc.stop()
                except Exception:
                    pass
                lane.proc = None
        NV.sweep_stale_dirs()

    # ───────────────────────── dựng / xoay một làn ─────────────────────────
    def _rotate_delay(self) -> float:
        lo = float(self.cfg.rotate_min_hours)
        hi = max(float(self.cfg.rotate_max_hours), lo)
        return random.uniform(lo, hi) * 3600.0

    def _candidates(self, exclude: set) -> List[dict]:
        """exclude = ENDPOINT IP của server (s["ip"]), KHÔNG phải IP ra.

        Chỗ này từng so nhầm hai loại IP với nhau: bộ loại trừ chứa IP RA
        (45.86.210.7) còn danh sách so bằng IP ENDPOINT (89.187.175.97) — không bao
        giờ khớp, nên hai làn cùng vớ một server và chung một IP ra, mất sạch ý nghĩa
        của việc chia làn.
        """
        pool = [s for s in self._servers if s["ip"] not in exclude]
        if not pool:
            pool = list(self._servers)
        # _servers đã sắp theo tải; lấy một cửa sổ đầu rồi xáo để không phải lúc
        # nào cũng đâm vào đúng server nhẹ nhất (ai cũng làm thế thì nó hết nhẹ).
        window = pool[: max(int(self.cfg.proxy_probe_candidates) * 5, 20)]
        random.shuffle(window)
        return window

    def _establish(self, lane: Lane, previous_ip: Optional[str] = None) -> bool:
        """Thử vài server, giữ lại cái có ms thấp nhất đạt ngưỡng.

        Né server mà làn khác đang dùng (theo endpoint), và né cả IP RA đã có làn
        khác chiếm — hai server khác nhau vẫn có thể đổ ra cùng một IP.
        """
        exclude = set(self._used_servers)
        if lane.server:
            exclude.discard(lane.server.get("ip"))
        if previous_ip:
            exclude.add(previous_ip)
        cands = [c for c in self._candidates(exclude)
                 if c["ip"] not in self._bad_servers]
        if not cands:
            cands = self._candidates(exclude)
        if not cands:
            return False

        proc = NV.ProxyProcess(lane.index, self._private_key, cands[0],
                               lane.socks_port, lane.http_port, self._binary)
        best: Optional[tuple] = None
        established = False
        for server in cands[: max(1, int(self.cfg.proxy_probe_candidates))]:
            if self._closed:
                break
            proc.server = server
            try:
                if not proc.start(self.log):
                    continue
            except Exception:
                continue
            info = NV.measure_latency(lane.socks_port, 3)
            if not info:
                self._bad_servers.add(server["ip"])
                proc.stop()
                continue
            if info.get("ip") and info["ip"] in self._used_exits:
                # Server khác nhưng đổ ra đúng IP làn khác đang dùng -> vô nghĩa.
                self.log(f"  ↷ {lane.id}: {server['name']} ra trùng IP "
                         f"{info['ip']} — bỏ, thử server khác")
                proc.stop()
                continue
            if best is None or info["ms"] < best[1]["ms"]:
                best = (server, info)
            if info["ms"] <= float(self.cfg.proxy_max_latency_ms):
                established = True
                break
            proc.stop()             # quá chậm, thử server khác

        if not established:
            if best is None:
                return False
            proc.server = best[0]   # không con nào đạt ngưỡng -> lấy con nhanh nhất
            if not proc.start(self.log):
                return False
            info = NV.measure_latency(lane.socks_port, 2) or best[1]
        else:
            info = NV.measure_latency(lane.socks_port, 1) or best[1]

        if lane.exit_ip:
            self._used_exits.discard(lane.exit_ip)
        if lane.server:
            self._used_servers.discard(lane.server.get("ip"))
        lane.proc = proc
        lane.server = proc.server
        lane.exit_ip = info.get("ip")
        lane.country = info.get("country")
        lane.latency_ms = info.get("ms")
        if lane.exit_ip:
            self._used_exits.add(lane.exit_ip)
        if lane.server:
            self._used_servers.add(lane.server.get("ip"))
        return True

    def _rotation_loop(self) -> None:
        min_gap = float(self.cfg.rotate_min_interval_min) * 60.0
        while not self._closed:
            time.sleep(20)
            if self._closed:
                break
            now = time.time()
            with self._lock:
                lanes = [l for l in self.lanes if l.proc]
            for lane in lanes:
                if self._closed:
                    break
                due = lane._rotate_reason or (lane.rotate_after and now >= lane.rotate_after)
                if not due:
                    continue
                if now - lane.last_rotated_at < min_gap:
                    continue
                if lane.busy:
                    # Đang ghi thì KHÔNG đụng vào — xoay là đứt file. Để lệnh xoay
                    # nằm đó, vòng sau rảnh sẽ làm.
                    continue
                self._rotate(lane)

    def _rotate(self, lane: Lane) -> None:
        reason = lane._rotate_reason or "tới hẹn"
        old_ip, old = lane.exit_ip, lane.proc
        lane._rotate_reason = None
        self.log(f"  ↻ xoay {lane.id} ({reason})…")
        if old:
            old.stop()
            lane.proc = None
        if self._establish(lane, previous_ip=old_ip):
            lane.last_rotated_at = time.time()
            lane.rotate_after = time.time() + self._rotate_delay()
            self.log(f"  ↻ {lane.id}: {old_ip} → {lane.exit_ip} ({lane.country})")
        else:
            lane.rotate_after = time.time() + 600     # thất bại: thử lại sau 10 phút
            self.log(f"  ⚠ xoay {lane.id} hỏng — giữ nguyên, thử lại sau")

    def rotate_now(self, lane_id: str, reason: str = "người dùng yêu cầu") -> bool:
        with self._lock:
            lane = next((l for l in self.lanes if l.id == lane_id), None)
        if not lane or lane.is_direct or not lane.proc:
            return False
        lane.request_rotation(reason)
        lane.last_rotated_at = 0.0          # bỏ qua khoảng cách tối thiểu
        return True

    # ───────────────────────── chia phòng vào làn ─────────────────────────
    def lane_for(self, rid: str) -> Lane:
        """Làn của phòng này (bám dính). Chưa có thì chọn làn rảnh nhất."""
        with self._lock:
            if not self.lanes:
                self._add_direct()
            lid = self.by_room.get(rid)
            if lid:
                lane = next((l for l in self.lanes if l.id == lid), None)
                if lane:
                    return lane
            cap = max(1, int(self.cfg.proxy_rooms_per_lane))
            under = [l for l in self.lanes if len(l.bound) < cap]
            pool = under or self.lanes
            # Ưu tiên làn đang ghi ít nhất — băng thông mới là thứ đang thiếu;
            # số phòng bám dính chỉ dùng để phá hoà.
            lane = min(pool, key=lambda l: (l.busy, len(l.bound), l.index))
            lane.bound.append(rid)
            self.by_room[rid] = lane.id
            return lane

    def release(self, rid: str) -> None:
        with self._lock:
            lid = self.by_room.pop(rid, None)
            if not lid:
                return
            lane = next((l for l in self.lanes if l.id == lid), None)
            if lane and rid in lane.bound:
                lane.bound.remove(rid)

    def mark_busy(self, rid: str, busy: bool) -> None:
        with self._lock:
            lid = self.by_room.get(rid)
            lane = next((l for l in self.lanes if l.id == lid), None) if lid else None
            if not lane:
                return
            lane.busy = max(0, lane.busy + (1 if busy else -1))

    def proxy_for(self, rid: str) -> str:
        """Chuỗi proxy để đưa cho requests/ffmpeg. Rỗng = đi thẳng."""
        return self.lane_for(rid).proxy_url or ""

    # ───────────────────────── báo cáo ─────────────────────────
    def _probe_http(self, proxy_url: str) -> Optional[dict]:
        import requests
        t0 = time.perf_counter()
        try:
            r = requests.get("https://one.one.one.one/cdn-cgi/trace",
                             proxies={"http": proxy_url, "https": proxy_url}, timeout=12)
            r.raise_for_status()
        except Exception:
            return None
        ms = (time.perf_counter() - t0) * 1000.0
        ip = loc = None
        for line in r.text.splitlines():
            if line.startswith("ip="):
                ip = line.split("=", 1)[1]
            elif line.startswith("loc="):
                loc = line.split("=", 1)[1]
        return {"ms": round(ms, 1), "ip": ip, "country": loc} if ip else None

    def summary(self) -> str:
        with self._lock:
            n = len(self.lanes)
            prox = sum(1 for l in self.lanes if not l.is_direct)
        if prox == 0:
            return "đi thẳng (1 làn)"
        return f"{n} làn ({prox} proxy + {'1 direct' if n > prox else '0 direct'}) · {self.status}"

    def report(self) -> List[dict]:
        with self._lock:
            return [l.report() for l in self.lanes]
