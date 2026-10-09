# -*- coding: utf-8 -*-
"""Nhận cookie ở MỌI định dạng thường gặp và quy về một dict duy nhất.

Chấp nhận: Netscape cookies.txt · JSON export (Cookie-Editor / EditThisCookie)
· header thô "a=1; b=2" · nội dung dán trực tiếp (không phải đường dẫn file).
Cookie KHÔNG bao giờ được ghi vào log.

Bản của HGLIVE: giống hgdl/cookies.py (cố ý chép để hglive/ đứng độc lập) nhưng
summary() báo theo nhu cầu của LIVE — thứ duy nhất bắt buộc là ttwid, còn
sessionid thì hoàn toàn không cần.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

# Cookie chứng minh phiên đăng nhập — có mới truy cập được nội dung đã mua.
LOGIN_KEYS = ("sessionid", "sessionid_ss", "sid_tt", "passport_csrf_token")


def parse(raw: str) -> Dict[str, str]:
    """Chuỗi cookie (bất kỳ định dạng nào) → dict {name: value}."""
    raw = (raw or "").strip()
    if not raw:
        return {}

    # 1) JSON export
    if raw[:1] in "[{":
        try:
            j = json.loads(raw)
            items = j["cookies"] if isinstance(j, dict) and "cookies" in j else j
            if isinstance(items, dict):          # {"name": "value", ...}
                return {str(k): str(v) for k, v in items.items()}
            out = {}
            for c in items:
                if isinstance(c, dict) and c.get("name"):
                    out[str(c["name"])] = str(c.get("value", ""))
            if out:
                return out
        except Exception:
            pass

    # 2) Netscape cookies.txt (7 cột tab)
    if "\t" in raw or raw.lstrip().startswith(("# Netscape", "# HTTP")):
        out = {}
        for line in raw.splitlines():
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line.strip() or line.lstrip().startswith("#"):
                continue
            p = line.split("\t")
            if len(p) >= 7 and p[5]:
                out[p[5].strip()] = p[6].strip()
        if out:
            return out

    # 3) header thô: "a=1; b=2"  (có thể xuống dòng)
    out = {}
    for chunk in raw.replace("\n", ";").split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            k = k.strip()
            if k:
                out[k] = v.strip()
    return out


def load(path_or_text: str) -> Dict[str, str]:
    """Nhận đường dẫn file HOẶC nội dung cookie dán thẳng."""
    s = (path_or_text or "").strip().strip('"')
    if not s:
        return {}
    try:
        p = Path(s)
        if len(s) < 400 and p.exists() and p.is_file():
            return parse(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        pass
    return parse(s)


def to_header(ck: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in ck.items() if k)


def is_logged_in(ck: Dict[str, str]) -> bool:
    return any(k in ck and ck[k] for k in LOGIN_KEYS)


def has_ttwid(ck: Dict[str, str]) -> bool:
    return bool((ck or {}).get("ttwid"))


def summary(ck: Dict[str, str]) -> str:
    """Mô tả an toàn để hiện lên UI — KHÔNG lộ giá trị cookie."""
    if not ck:
        return "chưa có cookie — sẽ tự xin ttwid khi dò phòng"
    state = "có ttwid ✓" if has_ttwid(ck) else "THIẾU ttwid — sẽ tự xin khi dò phòng"
    extra = " · đã đăng nhập (live không cần)" if is_logged_in(ck) else ""
    return f"{len(ck)} cookie · {state}{extra}"
