# -*- coding: utf-8 -*-
"""Nguyên liệu dựng proxy NordVPN WireGuard bằng wireproxy — không cần app NordVPN.

Chuyển thể từ module proxy NordVPN của translation_gateway sang bản ĐỒNG BỘ,
không asyncio: HGLIVE chạy bằng thread chứ không có event loop.

Cách hoạt động: lấy NordLynx private key từ token tài khoản → hỏi danh sách server
WireGuard → dựng file cấu hình → chạy `wireproxy` như một tiến trình con, nó mở
cổng SOCKS5 + HTTP ở localhost và tuồn mọi thứ qua đường hầm WireGuard. Mỗi tiến
trình = một IP ra riêng.

An toàn: CHỈ bind vào 127.0.0.1. Gateway từng để 0.0.0.0 và biến máy thành open
proxy công cộng — bị quét và lạm dụng trong vài giờ. Ở đây không có đường nào để
đổi thành 0.0.0.0, kể cả qua biến môi trường.
"""
from __future__ import annotations

import base64
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

from .util import NO_WINDOW, app_dir

API_URL = "https://api.nordvpn.com/v1"
NORD_DNS = "1.1.1.1, 8.8.8.8"
WIREPROXY_VERSION = "1.1.2"
BIND_ADDR = "127.0.0.1"          # cố định, không cho cấu hình — xem docstring

LogFn = Optional[Callable[[str], None]]


def _log(cb: LogFn, msg: str) -> None:
    if cb:
        cb(msg)


# ───────────────────────── tìm token ─────────────────────────
# Token là bí mật: các hàm dưới đây trả về GIÁ TRỊ nhưng chỉ log NGUỒN. Không chỗ
# nào trong tool được in token ra màn hình hay nhật ký.
TOKEN_KEYS = ("NORDVPN_TOKEN", "NORD_TOKEN", "NORDVPN_ACCESS_TOKEN")

# Nơi ngó qua khi người dùng không tự chỉ: cạnh tool. Muốn dùng chung token với
# một dự án khác thì trỏ ô "File token" vào file .env của dự án đó.
KNOWN_TOKEN_FILES = (".nordvpn_token", ".env")


def _token_from_text(text: str) -> str:
    """Bóc token từ nội dung .env (KEY=value) hoặc từ file chỉ có mỗi token."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            if k.strip().upper() in TOKEN_KEYS:
                v = v.strip().strip('"').strip("'")
                if v:
                    return v
    # file trần chỉ chứa token
    bare = (text or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{32,128}", bare):
        return bare
    return ""


def read_token_file(path: str) -> str:
    try:
        p = Path((path or "").strip().strip('"'))
        if p.is_dir():
            p = p / ".env"
        if not p.is_file() or p.stat().st_size > 1_000_000:
            return ""
        return _token_from_text(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return ""


def discover_token(extra_path: str = "") -> Tuple[str, str]:
    """Tìm token theo thứ tự: file người dùng chỉ → biến môi trường → chỗ quen.

    Trả (token, mô tả nguồn). Mô tả nguồn ĐƯỢC PHÉP hiện lên UI; token thì không.
    """
    if extra_path:
        tok = read_token_file(extra_path)
        if tok:
            return tok, f"file bạn chỉ: {Path(extra_path).name}"
    for key in TOKEN_KEYS:
        v = (os.environ.get(key) or "").strip()
        if v:
            return v, f"biến môi trường {key}"
    for cand in (str(app_dir() / name) for name in KNOWN_TOKEN_FILES):
        tok = read_token_file(cand)
        if tok:
            return tok, cand
    return "", ""


def mask(token: str) -> str:
    """Dạng an toàn để hiện lên UI — đủ để nhận ra là token nào, không đủ để dùng."""
    t = (token or "").strip()
    if not t:
        return "—"
    return f"{t[:4]}…{t[-4:]} ({len(t)} ký tự)" if len(t) > 12 else f"({len(t)} ký tự)"


# ───────────────────────── API NordVPN ─────────────────────────
def fetch_countries() -> List[dict]:
    try:
        r = requests.get(f"{API_URL}/servers/countries", timeout=30)
        r.raise_for_status()
        out = [{"id": c["id"], "code": c["code"], "name": c["name"]} for c in r.json()]
        return sorted(out, key=lambda x: x["name"])
    except Exception:
        return []


def country_id_for(code_or_name: str) -> Optional[int]:
    target = (code_or_name or "").strip().lower()
    if not target:
        return None
    for c in fetch_countries():
        if c["code"].lower() == target or c["name"].lower() == target:
            return c["id"]
    return None


def fetch_private_key(token: str) -> Optional[str]:
    """Token tài khoản → NordLynx private key. Token sai/hết hạn -> None."""
    auth = base64.b64encode(f"token:{(token or '').strip()}".encode()).decode()
    try:
        r = requests.get(f"{API_URL}/users/services/credentials",
                         headers={"Authorization": f"Basic {auth}"}, timeout=30)
        if r.status_code == 401:
            return None
        r.raise_for_status()
        return r.json().get("nordlynx_private_key") or None
    except Exception:
        return None


def fetch_wg_servers(country_id: Optional[int] = None, limit: int = 500) -> List[dict]:
    """Server WireGuard UDP, sắp theo tải NordVPN báo (nhẹ trước)."""
    url = (f"{API_URL}/servers?filters[servers_technologies][identifier]=wireguard_udp"
           f"&limit={limit}")
    if country_id:
        url += f"&filters[country_id]={country_id}"
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        servers = r.json()
    except Exception:
        return []

    out: List[dict] = []
    for s in servers:
        tech = next((t for t in s.get("technologies", [])
                     if t.get("identifier") == "wireguard_udp"), None)
        if not tech:
            continue
        meta = next((m for m in tech.get("metadata", []) if m.get("name") == "public_key"), None)
        pubkey = meta.get("value") if meta else None
        locs = s.get("locations", [])
        cc = locs[0].get("country", {}).get("code") if locs else ""
        if pubkey and s.get("station"):
            out.append({"name": s.get("name") or s.get("hostname") or s.get("station"),
                        "ip": s["station"], "pubkey": pubkey, "country": cc,
                        "load": s.get("load", 100)})
    out.sort(key=lambda x: x.get("load", 100))
    return out


# ───────────────────────── binary wireproxy ─────────────────────────
def find_wireproxy(extra: str = "") -> Optional[str]:
    """Tìm wireproxy: đường dẫn người dùng chỉ → cạnh tool → PATH. Không thấy thì
    người gọi tự tải bằng download_wireproxy()."""
    name = "wireproxy.exe" if platform.system() == "Windows" else "wireproxy"
    if extra:
        p = Path(extra.strip().strip('"'))
        if p.is_dir():
            p = p / name
        if p.exists():
            return str(p.resolve())
    root = app_dir()
    for c in (root / name, root / "resources" / name, Path(".") / name):
        if c.exists():
            return str(c.resolve())
    return shutil.which(name)


def download_wireproxy(log: LogFn = None) -> str:
    system = platform.system()
    if system == "Windows":
        url = (f"https://github.com/windtf/wireproxy/releases/download/"
               f"v{WIREPROXY_VERSION}/wireproxy_windows_amd64.tar.gz")
        binary = "wireproxy.exe"
    elif system == "Linux":
        url = (f"https://github.com/windtf/wireproxy/releases/download/"
               f"v{WIREPROXY_VERSION}/wireproxy_linux_amd64.tar.gz")
        binary = "wireproxy"
    else:
        raise RuntimeError(f"Hệ điều hành chưa hỗ trợ: {system}")

    _log(log, f"  tải wireproxy v{WIREPROXY_VERSION}…")
    dest_dir = app_dir()
    archive = dest_dir / "wireproxy_dl.tar.gz"
    r = requests.get(url, stream=True, timeout=180)
    r.raise_for_status()
    with open(archive, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extract(tar.getmember(binary), str(dest_dir))
    try:
        archive.unlink()
    except Exception:
        pass
    path = dest_dir / binary
    if system == "Linux":
        os.chmod(path, 0o755)
    _log(log, f"  wireproxy đã cài ở {path}")
    return str(path.resolve())


def ensure_wireproxy(extra: str = "", log: LogFn = None) -> str:
    return find_wireproxy(extra) or download_wireproxy(log)


# ───────────────────────── cổng ─────────────────────────
def is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            # Thử bind ĐÚNG địa chỉ wireproxy sẽ bind, để "rảnh" ở đây nghĩa là
            # bind thật cũng thành công.
            s.bind((BIND_ADDR, port))
            return True
        except OSError:
            return False


def allocate_port_pair(start_socks: int = 1080, start_http: int = 8282) -> Tuple[int, int]:
    socks = start_socks
    while not is_port_free(socks):
        socks += 1
    http = start_http
    while http == socks or not is_port_free(http):
        http += 1
    return socks, http


def build_config(private_key: str, server: dict, socks_port: int, http_port: int) -> str:
    return "\n".join([
        "[Interface]", f"PrivateKey = {private_key}", "Address = 10.5.0.2/16",
        f"DNS = {NORD_DNS}", "",
        "[Peer]", f"PublicKey = {server['pubkey']}", f"Endpoint = {server['ip']}:51820",
        "AllowedIPs = 0.0.0.0/0", "PersistentKeepalive = 25", "",
        "[Socks5]", f"BindAddress = {BIND_ADDR}:{socks_port}", "",
        "[http]", f"BindAddress = {BIND_ADDR}:{http_port}",
    ])


# ───────────────────────── đo đường ─────────────────────────
def socks5_trace(socks_port: int, timeout: float = 6.0) -> Optional[dict]:
    """SOCKS5 → TLS → Cloudflare /cdn-cgi/trace. Trả {"ip","country"} hoặc None.

    Vừa là phép kiểm "proxy sống chưa", vừa cho biết IP ra thật sự là gì.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((BIND_ADDR, socks_port))
        sock.sendall(b"\x05\x01\x00")
        resp = sock.recv(2)
        if len(resp) < 2 or resp[0] != 5 or resp[1] != 0:
            sock.close()
            return None
        sock.sendall(b"\x05\x01\x00\x01\x01\x01\x01\x01\x01\xbb")   # CONNECT 1.1.1.1:443
        resp = sock.recv(10)
        if len(resp) < 4 or resp[0] != 5 or resp[1] != 0:
            sock.close()
            return None
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ss = ctx.wrap_socket(sock, server_hostname="one.one.one.one")
        ss.settimeout(timeout)
        ss.sendall(b"GET /cdn-cgi/trace HTTP/1.1\r\nHost: one.one.one.one\r\n"
                   b"User-Agent: hglive-lane\r\nConnection: close\r\n\r\n")
        chunks = []
        while True:
            c = ss.recv(4096)
            if not c:
                break
            chunks.append(c)
        ss.close()
        text = b"".join(chunks).decode("utf-8", errors="ignore")
        ip = loc = None
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("ip="):
                ip = line.split("=", 1)[1]
            elif line.startswith("loc="):
                loc = line.split("=", 1)[1]
        return {"ip": ip, "country": loc} if ip else None
    except Exception:
        return None


def measure_latency(socks_port: int, samples: int = 3, timeout: float = 6.0) -> Optional[dict]:
    best: Optional[float] = None
    ip = country = None
    for _ in range(max(1, samples)):
        t0 = time.perf_counter()
        res = socks5_trace(socks_port, timeout)
        if not res:
            continue
        ms = (time.perf_counter() - t0) * 1000.0
        ip, country = res["ip"], res.get("country")
        if best is None or ms < best:
            best = ms
    if best is None or not ip:
        return None
    return {"ms": round(best, 1), "ip": ip, "country": country}


# ───────────────────────── giám sát tiến trình ─────────────────────────
class ProxyProcess:
    """Trông một tiến trình wireproxy = một lối ra WireGuard."""

    def __init__(self, slot_id: int, private_key: str, server: dict,
                 socks_port: int, http_port: int, binary_path: str):
        self.slot_id = slot_id
        self.private_key = private_key
        self.server = server
        self.socks_port = socks_port
        self.http_port = http_port
        self.binary_path = binary_path
        self.process: Optional[subprocess.Popen] = None
        self.temp_dir: Optional[Path] = None
        self._log_fh = None

    def start(self, log: LogFn = None) -> bool:
        self.temp_dir = app_dir() / f".wireproxy_{self.slot_id}"
        # cleanup() chỉ chạy khi tắt êm; bị kill cứng thì wireproxy.conf — có
        # PRIVATE KEY dạng chữ thường — nằm lại trên đĩa vô thời hạn. Xoá thư mục
        # của CHÍNH slot này trước khi dựng lại (dù sao cũng sắp ghi đè).
        if self.temp_dir.is_dir():
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        conf = self.temp_dir / "wireproxy.conf"
        conf.write_text(build_config(self.private_key, self.server,
                                     self.socks_port, self.http_port), encoding="utf-8")
        try:
            os.chmod(conf, 0o600)         # vô hại trên Windows, đúng trên Linux
        except Exception:
            pass

        try:
            self._log_fh = open(self.temp_dir / "wireproxy.log", "w", encoding="utf-8")
            self.process = subprocess.Popen(
                [self.binary_path, "-c", str(conf)],
                stdout=self._log_fh, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        except Exception as e:
            _log(log, f"  [làn {self.slot_id}] không chạy được wireproxy: {str(e)[:90]}")
            self.cleanup()
            return False

        # Đo thực tế 2026-08-29: server NordVPN nào sống thì bắt tay xong trong ~2-4s,
        # còn phần lớn thì im lặng mãi (thử 4 nước, chỉ 1 con phản hồi). Nên đừng ngồi
        # đủ 12 giây với con chết — thấy log kêu "Handshake did not complete" là bỏ
        # ngay, để cùng ngần ấy thời gian thử được gấp đôi số server.
        for i in range(6):
            time.sleep(2)
            if self.process.poll() is not None:
                _log(log, f"  [làn {self.slot_id}] wireproxy chết ngay khi khởi động")
                self.cleanup()
                return False
            if socks5_trace(self.socks_port):
                return True
            if i >= 2 and "Handshake did not complete" in self._log_text():
                _log(log, f"  [làn {self.slot_id}] {self.server.get('name', '?')}: "
                          "không bắt tay được — bỏ, thử server khác")
                self.stop()          # PHẢI stop chứ không cleanup: tiến trình còn sống
                return False
        _log(log, f"  [làn {self.slot_id}] wireproxy chạy nhưng chưa thông sau 12s"
                  + (" — " + self.why_stuck() if self.why_stuck() else ""))
        return True

    def _log_text(self) -> str:
        try:
            if self._log_fh:
                self._log_fh.flush()
            return (self.temp_dir / "wireproxy.log").read_text(encoding="utf-8",
                                                               errors="replace")
        except Exception:
            return ""

    def why_stuck(self) -> str:
        """Đọc log wireproxy để đoán nguyên nhân. Rỗng = không đoán được."""
        txt = self._log_text()
        if "Handshake did not complete" in txt:
            return ("server này không trả lời bắt tay WireGuard (phần lớn server "
                    "NordVPN im lặng từ đường VN) — thử server khác")
        if "Failed to send handshake" in txt or "no route to host" in txt.lower():
            return "không gửi được gói ra endpoint — kiểm tra tường lửa/định tuyến"
        return ""

    def stop(self) -> None:
        p = self.process
        if p:
            try:
                if platform.system() == "Windows" and p.pid:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                                   capture_output=True, timeout=6, creationflags=NO_WINDOW)
                else:
                    p.kill()
            except Exception:
                pass
            try:
                p.wait(timeout=4)
            except Exception:
                pass
            self.process = None
        self.cleanup()

    def cleanup(self) -> None:
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None
        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = None


def sweep_stale_dirs() -> int:
    """Xoá thư mục .wireproxy_* còn sót từ lần chạy trước bị kill cứng.

    Không quét là private key nằm lại trên đĩa mãi — gateway từng để sót hai thư
    mục suốt ba tuần. Chạy lúc khởi động, TRƯỚC khi dựng làn mới.
    """
    n = 0
    try:
        for d in app_dir().glob(".wireproxy_*"):
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                n += 1
    except Exception:
        pass
    return n
