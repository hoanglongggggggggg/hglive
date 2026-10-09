# -*- coding: utf-8 -*-
"""Tầng mạng của HGLIVE — dò phòng live và bóc ra danh sách luồng.

Thuần transport, không giữ trạng thái, không đụng đĩa: mọi quyết định "ghi hay chờ"
nằm ở recorder.py.

Khác hẳn nhánh video thường: endpoint webcast KHÔNG cần ký a_bogus, cũng KHÔNG cần
cookie đăng nhập. Điều kiện duy nhất là có cookie ttwid; thiếu nó server trả body
RỖNG (HTTP vẫn 200) chứ không báo lỗi — nên phải bắt riêng ca này, không thì tưởng
mất mạng.

Đã kiểm chứng trên phòng 578280456923 (2026-08-29), mỗi ô 3 lượt:
  · ttwid là cookie DUY NHẤT bắt buộc — không ttwid -> body rỗng, mọi lần.
  · a_bogus / Referer / User-Agent: không ảnh hưởng.
  · URL luồng ký sẵn hiệu lực đúng 168 giờ (7 ngày) -> ghi dài không cần lấy lại.
  · CDN kéo được trần trụi: không cookie, không Referer.
  · status_code=10001 "服务器打瞌睡了" là trục trặc chốc lát -> thử lại là qua.
"""
from __future__ import annotations

import json
import re
import time
from typing import Dict, List, Optional
from urllib.parse import urlencode

import requests

ENTER = "https://live.douyin.com/webcast/room/web/enter/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Thang nét cố định của Douyin. md (流畅) nằm DƯỚI ld (标清) dù tên gợi ý ngược lại.
QUALITY_ORDER = ("origin", "uhd", "hd", "sd", "ld", "md", "ao")
# Nhãn phía web (flv_pull_url) quy về đúng sdk_key để không đếm trùng một luồng.
WEB_LABEL = {"FULL_HD1": "origin", "HD1": "hd", "SD1": "ld", "SD2": "md"}
STATUS = {2: "đang phát", 4: "đã kết thúc"}
PROTOS = ("flv", "hls")


class LiveError(Exception):
    """Lỗi dò phòng. Luôn kèm câu tiếng Việt đủ để người dùng biết phải làm gì."""


# ───────────────────────── link -> web_rid ─────────────────────────
def resolve_rid(link: str) -> str:
    s = (link or "").strip().strip('"').strip()
    if re.fullmatch(r"\d{6,}", s):
        return s
    m = re.search(r"live\.douyin\.com/(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"https?://v\.douyin\.com/[\w-]+", s)
    if m:
        try:
            url = requests.get(m.group(0), headers={"User-Agent": UA},
                               allow_redirects=True, timeout=20).url
        except Exception as e:
            raise LiveError(f"không lần được link rút gọn: {str(e)[:80]}") from e
        m2 = re.search(r"live\.douyin\.com/(\d+)", url)
        if m2:
            return m2.group(1)
        m2 = re.search(r"webcast/reflow/(\d+)", url)
        if m2:
            raise LiveError(f"đây là link reflow (room_id={m2.group(1)}), không phải web_rid "
                            "— mở phòng trên trình duyệt rồi copy link ở thanh địa chỉ")
    raise LiveError(f"không rút được web_rid từ: {s[:80]}")


def looks_like_live(link: str) -> bool:
    """Đủ để GUI cảnh báo sớm khi người dùng dán nhầm link video vào ô live."""
    s = (link or "").lower()
    return "live.douyin.com" in s or bool(re.fullmatch(r"\s*\d{6,}\s*", s))


# ───────────────────────── phiên + gọi API ─────────────────────────
def _session(cookie: Optional[Dict[str, str]], rid: str, proxy: str = "") -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                      "Accept-Language": "zh-CN,zh;q=0.9",
                      "Referer": f"https://live.douyin.com/{rid}"})
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    for k, v in (cookie or {}).items():
        try:
            s.cookies.set(k, v, domain=".douyin.com")
        except Exception:
            pass
    if not s.cookies.get("ttwid"):        # tự xin: một lần GET trang phòng là server phát
        try:
            s.get(f"https://live.douyin.com/{rid}", timeout=25,
                  headers={"Accept": "text/html,application/xhtml+xml"})
        except Exception:
            pass
    if not s.cookies.get("ttwid"):
        raise LiveError("không lấy được cookie ttwid (mạng chặn douyin.com?) — "
                        "thiếu ttwid thì API trả rỗng")
    return s


def room_info(rid: str, cookie: Optional[Dict[str, str]] = None, proxy: str = "",
              attempts: int = 3) -> dict:
    """Thông tin phòng thô. Ném LiveError khi hỏng — KHÔNG trả {} im lặng."""
    s = _session(cookie, rid, proxy)
    params = {"aid": "6383", "app_name": "douyin_web", "live_id": "1",
              "device_platform": "web", "language": "zh-CN", "enter_from": "web_live",
              "cookie_enabled": "true", "screen_width": "1920", "screen_height": "1080",
              "browser_language": "zh-CN", "browser_platform": "Win32",
              "browser_name": "Mozilla", "browser_version": "120.0.0.0",
              "web_rid": rid, "room_id_str": "", "enter_source": "",
              "is_need_double_stream": "false", "insert_task_id": "", "live_reason": ""}
    last = ""
    for i in range(max(1, attempts)):
        if i:
            time.sleep(1.5 * i)
        try:
            r = s.get(f"{ENTER}?{urlencode(params)}", timeout=25)
        except Exception as e:
            last = f"lỗi mạng: {str(e)[:80]}"
            continue
        if r.status_code != 200:
            last = f"HTTP {r.status_code}"
            continue
        if not r.text.strip():
            last = "server trả rỗng — cookie ttwid hỏng/hết hạn, hoặc bị chặn"
            continue
        try:
            j = r.json()
        except Exception:
            last = "phản hồi không phải JSON (dính trang xác minh)"
            continue
        if j.get("status_code") != 0:      # 10001 = server chập chờn, thử lại là qua
            last = f"status_code={j.get('status_code')} {str(j.get('data'))[:90]}"
            continue
        rooms = (j.get("data") or {}).get("data") or []
        if not rooms:
            raise LiveError("không có phòng nào trong phản hồi — web_rid sai?")
        return rooms[0]
    raise LiveError(last or "không rõ")


# ───────────────────────── bóc luồng ─────────────────────────
def _res_px(res: str) -> int:
    m = re.fullmatch(r"(\d+)x(\d+)", res or "")
    return int(m.group(1)) * int(m.group(2)) if m else 0


def qualities(room: dict) -> List[dict]:
    """Gộp live_core_sdk_data + flv_pull_url -> danh sách luồng, nét cao trước.

    flv_pull_url (nhãn web) THIẾU mức so với stream_data — phòng thử chỉ hiện 2/4 —
    nên lấy stream_data làm gốc rồi mới vá thêm nhãn web nào chưa có.

    Xếp hạng theo QUALITY_ORDER (thang cố định), lấy options.qualities[].level của
    API để xếp mức lạ và phá hoà. TUYỆT ĐỐI không xếp theo vbitrate: phòng thử khai
    origin 338 kbps mà đo thực 593 kbps, còn ld tự nhận 1000 kbps — con số vô nghĩa.
    """
    su = room.get("stream_url") or {}
    pull = (su.get("live_core_sdk_data") or {}).get("pull_data") or {}
    out: List[dict] = []

    try:
        sd = json.loads(pull.get("stream_data") or "{}")
    except Exception:
        sd = {}
    opts = pull.get("options") or {}
    if isinstance(opts, str):
        try:
            opts = json.loads(opts)
        except Exception:
            opts = {}
    lvl = {q.get("sdk_key"): (q.get("level"), q.get("name"))
           for q in list(opts.get("qualities") or []) + [opts.get("default_quality") or {}]
           if isinstance(q, dict) and q.get("sdk_key")}

    for key, node in (sd.get("data") or {}).items():
        main = (node or {}).get("main") or {}
        try:
            prm = json.loads(main.get("sdk_params") or "{}")
        except Exception:
            prm = {}
        level, name = lvl.get(key, (None, ""))
        out.append({"key": key, "level": level, "name": name or "",
                    "res": prm.get("resolution") or "",
                    "codec": (prm.get("VCodec") or "h264"),
                    "flv": main.get("flv") or "", "hls": main.get("hls") or "",
                    "lls": main.get("lls") or ""})

    have = {q["key"] for q in out}
    for wname, url in (su.get("flv_pull_url") or {}).items():
        k = WEB_LABEL.get(wname, str(wname).lower())
        if k not in have:
            level, name = lvl.get(k, (None, ""))
            out.append({"key": k, "level": level, "name": name or "", "res": "",
                        "codec": "h264", "flv": url,
                        "hls": (su.get("hls_pull_url_map") or {}).get(wname, ""), "lls": ""})

    def rank(q: dict):
        lv = q.get("level") or 0
        base = QUALITY_ORDER.index(q["key"]) if q["key"] in QUALITY_ORDER else 50 - lv
        return (base, -lv, -_res_px(q["res"]))

    out.sort(key=rank)
    return out


def pick(qs: List[dict], want: str = "best", proto: str = "flv") -> dict:
    """want: 'best' | 'worst' | tên sdk_key. Không có mức đã chọn thì rơi về mức
    gần nhất còn sống — ghi được thứ gì đó vẫn hơn là bỏ lỡ buổi phát."""
    have = [q for q in qs if q.get(proto)]
    if not have:
        raise LiveError(f"phòng không phát luồng {proto} nào")
    if want in ("", "best"):
        return have[0]
    vid = [q for q in have if q["key"] != "ao"] or have
    if want == "worst":
        return vid[-1]
    for q in have:
        if q["key"] == want:
            return q
    return have[0]


def label(q: dict) -> str:
    """Nhãn ngắn cho UI: 'origin 1280x720 (高清)'."""
    bits = [q.get("key", "?")]
    if q.get("res"):
        bits.append(q["res"])
    if q.get("name"):
        bits.append(f"({q['name']})")
    return " ".join(bits)


# ───────────────────────── đọc phòng ─────────────────────────
def is_live(room: dict) -> bool:
    return bool(room) and room.get("status") == 2 and bool(room.get("stream_url"))


def nickname(room: dict) -> str:
    return ((room or {}).get("owner") or {}).get("nickname") or ""


def cover_url(room: dict) -> str:
    """Ảnh bìa phòng. Douyin trả nhiều URL cùng ảnh — lấy cái đầu là đủ."""
    for node in ((room or {}).get("cover"), ((room or {}).get("owner") or {}).get("avatar_thumb")):
        for u in ((node or {}).get("url_list") or []):
            if u:
                return u
    return ""


def viewers(room: dict) -> str:
    st = (room or {}).get("room_view_stats") or {}
    return st.get("display_value_with_unit") or st.get("display_short") \
        or (room or {}).get("user_count_str") or ""


def describe(room: dict) -> str:
    return (f"{nickname(room) or '?'} · {STATUS.get(room.get('status'), room.get('status'))}"
            f" · {viewers(room) or '?'} người xem · {room.get('title') or ''}")


def meta(room: dict) -> dict:
    """Bản rút gọn để hiện lên UI và ghi kèm file .info.json."""
    return {"room_id": room.get("id_str") or "", "title": room.get("title") or "",
            "nickname": nickname(room), "sec_uid": ((room.get("owner") or {}).get("sec_uid") or ""),
            "status": room.get("status"), "status_text": STATUS.get(room.get("status"), "?"),
            "viewers": viewers(room), "cover": cover_url(room),
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S")}
