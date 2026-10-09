# -*- coding: utf-8 -*-
"""Giao diện HGLIVE (PySide6) — canh và ghi nhiều phòng live cùng lúc.

Ý đồ UX, khác hẳn một trình tải video:
  • Ghi live là việc CANH, không phải việc tải. Cửa sổ này để mở cả buổi, nên mọi
    thứ quan trọng phải đọc được từ xa: trạng thái, thời lượng, dung lượng.
  • Không có phần trăm — live không biết trước bao dài. Cột "Nhịp" vì thế đập theo
    nhịp thở khi đang ghi (hoặc chạy theo miếng cắt nếu bật cắt file), để nhìn phát
    là biết còn sống hay đã treo.
  • Phòng chưa lên sóng KHÔNG phải là lỗi — đó là trạng thái bình thường, hiện xám.
  • Đóng app hỏi lại nếu đang ghi dở; danh sách canh được nhớ cho lần mở sau.
  • Ba mức dừng, đừng lẫn: TẠM DỪNG (thôi ghi, vẫn canh, số liệu giữ nguyên) ·
    GHI TIẾP (file mới nhưng tổng cộng dồn) · DỪNG HẲN (thôi luôn cả canh).
  • Bàn phím: Ctrl+L ô link · Ctrl+D canh sóng · Ctrl+R ghi ngay · Ctrl+S cài đặt
              Space tạm dừng/ghi tiếp dòng đang chọn · Delete bỏ phòng
              Ctrl+K bật/tắt nhật ký
"""
from __future__ import annotations

import math
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional

try:
    from PySide6.QtCore import QObject, QRectF, QSize, Qt, QTimer, Signal
    from PySide6.QtGui import (QColor, QGuiApplication, QKeySequence, QPainter,
                               QPainterPath, QPixmap, QShortcut)
    from PySide6.QtWidgets import (
        QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
        QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
        QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu,
        QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QStyledItemDelegate,
        QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    )
except ImportError as e:                      # để app.py in hướng dẫn cài đặt
    raise ImportError(f"PySide6 chưa được cài ({e})")

import requests

from . import cookies as CK
from . import lanes as LN
from . import nordvpn as NV
from . import live as LV
from . import recorder as RC
from .config import (EXT_CHOICES, PROTO_CHOICES, PROXY_MODE_CHOICES, QUALITY_CHOICES,
                     Settings)
from .util import (fmt_clock, fmt_hms, has_ffmpeg, human_rate, human_size,
                   human_speed, open_in_explorer)

# ───────────────────────── bảng màu ─────────────────────────
# Cùng hệ với HGDL để hai tool nhìn là một nhà, nhưng nghĩa của màu là của LIVE:
#   đỏ  = đang ghi (chấm thu hình, không phải lỗi)   · lam  = canh sóng
#   lục = đang phát chưa ghi                          · xám  = tắt sóng / đã dừng
#   cam = lỗi
C = {
    "bg": "#0E1116", "panel": "#151A21", "panel2": "#1B222C", "line": "#242D3B",
    "text": "#E7EAF0", "muted": "#8B94A7",
    "accent": "#FE2C55",      # brand + nút chính
    "rec": "#FF3B3B",         # chỉ: ĐANG GHI
    "watch": "#3B82F6",       # chỉ: canh sóng
    "livegreen": "#35C46B",   # chỉ: đang phát mà chưa ghi
    "queue": "#F5C542",       # chỉ: xếp hàng chờ suất ghi (vàng — chưa phải lỗi)
    "pause": "#A97BD6",       # chỉ: người dùng tạm dừng ghi (tím)
    "off": "#6B7688",         # tắt sóng / đã dừng hẳn / chờ dò
    "err": "#FF7A45",         # lỗi (cam đậm — đỏ đã dành cho "đang ghi")
    "info": "#25F4EE",
}

STATE_COLOR = {
    RC.IDLE: C["off"], RC.WAITING: C["watch"], RC.LIVE: C["livegreen"],
    RC.QUEUED: C["queue"], RC.REC: C["rec"], RC.PAUSED: C["pause"],
    RC.ENDED: C["off"], RC.STOPPED: C["off"], RC.ERROR: C["err"],
}


def _rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _min_gap() -> tuple:
    """Khoảng cách RGB nhỏ nhất giữa hai màu trạng thái KHÁC NGHĨA.

    Chặn trùng khít là chưa đủ: hai màu lệch nhau 30 đơn vị thì liếc qua vẫn thấy
    là một. Ngưỡng 50 giữ cho mọi cặp thật sự phân biệt được. "tắt sóng" và "đã
    dừng" cố ý dùng chung một màu (đều là "không chạy"), chữ trong cột đã phân biệt
    hộ rồi — nên loại cặp đó ra khỏi phép kiểm.
    """
    import itertools
    import math
    same = {frozenset((RC.ENDED, RC.STOPPED))}
    worst, pair = 1e9, ()
    for (s1, c1), (s2, c2) in itertools.combinations(STATE_COLOR.items(), 2):
        if frozenset((s1, s2)) in same or c1 == c2:
            continue
        d = math.dist(_rgb(c1), _rgb(c2))
        if d < worst:
            worst, pair = d, (s1, s2)
    return worst, pair


_GAP, _PAIR = _min_gap()
assert _GAP >= 50, f"trạng thái {_PAIR} trùng màu (khoảng cách {_GAP:.0f} < 50)"

def _arrow_icons() -> tuple:
    """Vẽ sẵn hai mũi tên lên/xuống ra file PNG cho QSS dùng.

    Qt stylesheet KHÔNG làm được tam giác kiểu CSS (border-width trick ra hình
    vuông), còn mũi tên mặc định của Qt thì đen thui trên nền tối — nhìn ra hai
    chấm mờ. Vẽ tay rồi trỏ `image: url(...)` là cách duy nhất chắc ăn.
    Hỏng thì trả ("", "") và QSS bỏ qua phần này.
    """
    try:
        import tempfile
        d = Path(tempfile.gettempdir()) / "hglive_ui"
        d.mkdir(parents=True, exist_ok=True)
        out = []
        for name, up in (("up", True), ("down", False)):
            f = d / f"arrow_{name}.png"
            if not f.exists():
                pm = QPixmap(14, 14)
                pm.fill(Qt.GlobalColor.transparent)
                p = QPainter(pm)
                p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(C["muted"]))
                path = QPainterPath()
                if up:
                    path.moveTo(7, 4.5)
                    path.lineTo(11, 9.5)
                    path.lineTo(3, 9.5)
                else:
                    path.moveTo(7, 9.5)
                    path.lineTo(11, 4.5)
                    path.lineTo(3, 4.5)
                path.closeSubpath()
                p.drawPath(path)
                p.end()
                pm.save(str(f))
            out.append(str(f).replace("\\", "/"))
        return tuple(out)
    except Exception:
        return ("", "")


def install_arrows() -> None:
    """Nối phần mũi tên vào QSS. PHẢI gọi SAU khi đã có QApplication — QPixmap
    không tạo được trước đó, mà module này thì import trước app rất lâu."""
    global QSS
    up, down = _arrow_icons()
    if not up or "up-arrow" in QSS:
        return
    QSS += (f'\nQSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{up}");'
            f' width: 14px; height: 14px; }}\n'
            f'QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url("{down}");'
            f' width: 14px; height: 14px; }}\n')


QSS = f"""
QWidget {{ background: {C['bg']}; color: {C['text']};
           font-family: "Segoe UI", "Microsoft YaHei UI", system-ui; font-size: 13px; }}
QFrame#card {{ background: {C['panel']}; border: 1px solid {C['line']}; border-radius: 12px; }}
QLabel#h1 {{ font-size: 19px; font-weight: 700; }}
QLabel#h2 {{ font-size: 14px; font-weight: 600; }}
QLabel#brand {{ font-size: 20px; font-weight: 800; color: {C['accent']}; letter-spacing: 1px; }}
QLabel#muted {{ color: {C['muted']}; }}
QLabel#cover {{ background: {C['panel2']}; border: 1px solid {C['line']}; border-radius: 8px; }}
QLabel[badge="true"] {{ background: {C['panel2']}; border: 1px solid {C['line']};
    border-radius: 9px; padding: 2px 9px; color: {C['muted']};
    font-size: 11px; font-weight: 600; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{
    background: {C['panel2']}; border: 1px solid {C['line']}; border-radius: 8px;
    padding: 7px 10px; selection-background-color: {C['accent']}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {C['accent']}; }}
QLineEdit#url {{ font-size: 14px; padding: 10px 12px; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
/* Nút tăng/giảm spinbox: mũi tên nằm ở _ARROW_QSS bên dưới (Qt không vẽ được
   tam giác bằng border như CSS, mà mũi tên mặc định thì đen trên nền tối). */
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    background: {C['panel']}; border: 1px solid {C['line']}; border-radius: 4px;
    width: 17px; margin: 2px 2px 1px 2px; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ margin: 1px 2px 2px 2px; }}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    border-color: {C['accent']}; }}
QComboBox QAbstractItemView {{ background: {C['panel2']}; border: 1px solid {C['line']};
    selection-background-color: {C['accent']}; outline: none; }}

QPushButton {{ background: {C['panel2']}; border: 1px solid {C['line']}; border-radius: 8px;
    padding: 8px 14px; font-weight: 600; }}
QPushButton:hover {{ border-color: {C['accent']}; color: #fff; }}
QPushButton:disabled {{ color: #555E70; border-color: #1D2531; }}
QPushButton#primary {{ background: {C['accent']}; border: none; color: #fff; padding: 9px 20px; }}
QPushButton#primary:hover {{ background: #FF4468; }}
QPushButton#primary:disabled {{ background: #4A2632; color: #9A8189; }}
QPushButton#tiny {{ padding: 5px 10px; font-size: 12px; font-weight: 500; }}
/* Nút nằm trong ô bảng: phải thấp và gọn hơn nút thường, không thì dòng bị đội cao. */
QPushButton#rowact {{ padding: 2px 6px; font-size: 11px; font-weight: 600;
    border-radius: 6px; }}

QTableWidget {{ background: {C['panel']}; border: 1px solid {C['line']}; border-radius: 10px;
    gridline-color: transparent; outline: none; }}
QTableWidget::item {{ padding: 6px; border-bottom: 1px solid #1A212B; }}
QTableWidget::item:selected {{ background: #23303F; color: {C['text']}; }}
QHeaderView::section {{ background: {C['panel']}; color: {C['muted']}; border: none;
    border-bottom: 1px solid {C['line']}; padding: 8px 6px; font-weight: 600; }}

QPlainTextEdit#log {{ font-family: Consolas, "Cascadia Mono", monospace; font-size: 12px;
    color: #A9B4C6; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid #3A465A; background: {C['panel2']}; }}
QCheckBox::indicator:checked {{ background: {C['info']}; border-color: {C['info']}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2C3644; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #3B475A; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QMenu {{ background: {C['panel2']}; border: 1px solid {C['line']}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {C['accent']}; color: #fff; }}
QToolTip {{ background: {C['panel2']}; color: {C['text']}; border: 1px solid {C['line']};
    padding: 5px; border-radius: 6px; }}
"""

(COL_ACT, COL_ROOM, COL_STATE, COL_LANE, COL_Q, COL_TIME, COL_SIZE, COL_SPEED,
 COL_BAR, COL_FILE) = range(10)
HEADERS = ["", "Phòng", "Trạng thái", "Làn", "Nét", "Thời lượng", "Dung lượng",
           "Tốc độ", "Nhịp", "File"]


def _legend_html() -> str:
    parts = [(C["rec"], "đang ghi"), (C["queue"], "xếp hàng"), (C["pause"], "tạm dừng"),
             (C["watch"], "canh sóng"), (C["livegreen"], "đang phát"),
             (C["off"], "tắt sóng / đã dừng"), (C["err"], "lỗi")]
    dots = "&nbsp;&nbsp;".join(
        f'<span style="color:{c}">&#9679;</span>&nbsp;'
        f'<span style="color:{C["muted"]}">{t}</span>' for c, t in parts)
    return f'<span style="font-size:11px">{dots}</span>'


# ───────────────────────── cầu nối luồng ─────────────────────────
class Bridge(QObject):
    log = Signal(str)
    task = Signal(object)
    probed = Signal(object, object)     # (room, qualities)
    probe_failed = Signal(str)
    cover = Signal(bytes)
    lanes_ready = Signal()


# ───────────────────────── cột "Nhịp" ─────────────────────────
class PulseDelegate(QStyledItemDelegate):
    """Live không có phần trăm, nên cột này KHÔNG giả vờ có.

    Đang ghi → vệt sáng chạy qua lại (còn chạy = còn sống, treo là đứng ngay).
    Bật cắt file → vệt chạy đúng theo tiến độ miếng hiện tại, thành ra vừa báo sống
    vừa cho biết bao giờ sang miếng mới. Trạng thái khác → thanh mờ đứng yên.
    """

    def paint(self, painter: QPainter, option, index):
        state = index.data(Qt.ItemDataRole.UserRole)
        frac = index.data(Qt.ItemDataRole.UserRole + 1)
        if not state:
            return super().paint(painter, option, index)
        r = option.rect.adjusted(6, 0, -6, 0)
        h = 8
        bar = QRectF(r.left(), r.center().y() - h / 2, r.width(), h)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#232C39"))
        painter.drawRoundedRect(bar, 4, 4)
        color = QColor(STATE_COLOR.get(state, C["off"]))
        if state == RC.REC:
            if frac is not None:                      # theo miếng cắt
                w = max(2.0, bar.width() * max(0.0, min(1.0, float(frac))))
                painter.setBrush(color)
                painter.drawRoundedRect(QRectF(bar.left(), bar.top(), w, bar.height()), 4, 4)
            else:                                     # vệt chạy qua lại
                phase = (time.monotonic() * 0.55) % 2.0
                pos = phase if phase < 1 else 2 - phase
                w = bar.width() * 0.28
                x = bar.left() + (bar.width() - w) * pos
                painter.setBrush(color)
                painter.drawRoundedRect(QRectF(x, bar.top(), w, bar.height()), 4, 4)
        elif state in (RC.WAITING, RC.IDLE, RC.LIVE):
            color.setAlpha(90)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(bar.left(), bar.top(), bar.width() * 0.18,
                                           bar.height()), 4, 4)
        painter.restore()


# ───────────────────────── hộp làn mạng ─────────────────────────
LANE_HEADERS = ["Làn", "IP ra", "Nước", "ms", "Server", "Đang ghi", "Phòng bám"]


class LanesDialog(QDialog):
    """Xem từng làn đang đi đường nào, và xoay IP làn nào đang rảnh."""

    def __init__(self, parent, mgr):
        super().__init__(parent)
        self.mgr = mgr
        self.setWindowTitle("Làn mạng")
        self.setMinimumSize(720, 320)
        self.setStyleSheet(QSS)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(10)

        self.head = QLabel("")
        self.head.setObjectName("h2")
        v.addWidget(self.head)

        self.tb = QTableWidget(0, len(LANE_HEADERS))
        self.tb.setHorizontalHeaderLabels(LANE_HEADERS)
        self.tb.verticalHeader().setVisible(False)
        self.tb.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tb.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self.tb.horizontalHeader()
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for c, w in ((0, 84), (1, 132), (2, 56), (3, 66), (5, 74), (6, 84)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
            self.tb.setColumnWidth(c, w)
        v.addWidget(self.tb, 1)

        note = QLabel("Làn đang ghi thì KHÔNG xoay được — xoay giữa chừng là đứt file. "
                      "Lệnh xoay sẽ nằm chờ tới lúc làn rảnh.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        v.addWidget(note)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.b_rot = QPushButton("Xoay IP làn đang chọn")
        self.b_rebuild = QPushButton("Dựng lại toàn bộ")
        self.b_rebuild.setObjectName("tiny")
        row.addWidget(self.b_rot)
        row.addWidget(self.b_rebuild)
        row.addStretch(1)
        close = QPushButton("Đóng")
        close.setObjectName("primary")
        row.addWidget(close)
        v.addLayout(row)

        self.b_rot.clicked.connect(self._rotate)
        self.b_rebuild.clicked.connect(self._rebuild)
        close.clicked.connect(self.accept)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1500)
        self.refresh()

    def refresh(self):
        rep = self.mgr.report()
        self.head.setText(self.mgr.summary())
        self.tb.setRowCount(len(rep))
        for r, lane in enumerate(rep):
            cells = [
                lane["id"],
                "IP máy" if lane["direct"] else (lane["exit_ip"] or "—"),
                lane["country"] or "—",
                f"{lane['latency_ms']:.0f}" if lane["latency_ms"] else "—",
                "—" if lane["direct"] else (lane["server"] or lane["proxy"] or "—"),
                str(lane["busy"]),
                str(len(lane["bound"])),
            ]
            for c, txt in enumerate(cells):
                it = QTableWidgetItem(txt)
                if c in (3, 5, 6):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight |
                                        Qt.AlignmentFlag.AlignVCenter)
                if c == 0:
                    it.setForeground(QColor(C["muted"] if lane["direct"] else C["info"]))
                if c == 5 and lane["busy"]:
                    it.setForeground(QColor(C["rec"]))
                self.tb.setItem(r, c, it)

    def _selected_lane(self) -> Optional[str]:
        rows = {i.row() for i in self.tb.selectedIndexes()}
        if not rows:
            return None
        it = self.tb.item(min(rows), 0)
        return it.text() if it else None

    def _rotate(self):
        lid = self._selected_lane()
        if not lid:
            QMessageBox.information(self, "Làn mạng", "Chọn một làn trong bảng đã.")
            return
        if not self.mgr.rotate_now(lid):
            QMessageBox.information(
                self, "Làn mạng",
                f"Làn {lid} không xoay được — làn direct dùng chính IP máy, "
                "còn làn ở chế độ danh sách thì IP do proxy quyết định.")
            return
        self.refresh()

    def _rebuild(self):
        if QMessageBox.question(
                self, "Dựng lại làn",
                "Dừng toàn bộ wireproxy rồi dựng lại từ đầu.\n"
                "Phòng đang ghi sẽ đứt luồng (ghi tiếp sang file mới nếu bật tự nối). "
                "Làm chứ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.accept()
        p = self.parent()
        if p is not None:
            threading.Thread(target=p.lanes.close, daemon=True).start()
            QTimer.singleShot(600, lambda: p._start_lanes())


# ───────────────────────── hộp cài đặt ─────────────────────────
class SettingsDialog(QDialog):
    def __init__(self, cfg: Settings, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Cài đặt HGLIVE")
        self.setMinimumWidth(640)
        self.setStyleSheet(QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 14)
        root.setSpacing(13)

        # ── nơi lưu & mạng ──
        box1 = self._card("Nơi lưu & mạng")
        f1 = QFormLayout()
        f1.setSpacing(9)
        self.out = QLineEdit(cfg.out_dir)
        f1.addRow("Thư mục ra", self._browse(self.out, folder=True))
        self.cookie = QLineEdit(cfg.cookie_path)
        self.cookie.setPlaceholderText("để trống cũng được — live chỉ cần ttwid, tool tự xin")
        f1.addRow("Cookie", self._browse(self.cookie, folder=False))
        self.ck_state = QLabel("")
        self.ck_state.setObjectName("muted")
        f1.addRow("", self.ck_state)
        self.proxy = QLineEdit(cfg.proxy)
        self.proxy.setPlaceholderText("http://user:pass@host:port")
        f1.addRow("Proxy", self.proxy)
        box1.layout().addLayout(f1)
        root.addWidget(box1)

        # ── luồng ──
        box2 = self._card("Luồng ghi")
        f2 = QFormLayout()
        f2.setSpacing(9)
        self.quality = QComboBox()
        for k, t in QUALITY_CHOICES:
            self.quality.addItem(t, k)
        self.quality.setCurrentIndex(max(0, self.quality.findData(cfg.quality)))
        f2.addRow("Mức nét", self.quality)
        self.proto = QComboBox()
        for k, t in PROTO_CHOICES:
            self.proto.addItem(t, k)
        self.proto.setCurrentIndex(max(0, self.proto.findData(cfg.proto)))
        f2.addRow("Giao thức", self.proto)
        self.ext = QComboBox()
        for k, t in EXT_CHOICES:
            self.ext.addItem(t, k)
        self.ext.setCurrentIndex(max(0, self.ext.findData(cfg.ext)))
        f2.addRow("Định dạng", self.ext)
        box2.layout().addLayout(f2)
        root.addWidget(box2)

        # ── hành vi canh ──
        box3 = self._card("Cách canh sóng")
        f3 = QFormLayout()
        f3.setSpacing(9)
        self.auto_rec = QCheckBox("Thấy phòng lên sóng là ghi ngay, không cần bấm")
        self.auto_rec.setChecked(cfg.auto_record)
        f3.addRow("", self.auto_rec)
        self.auto_res = QCheckBox("Luồng đứt giữa buổi mà phòng còn phát thì ghi tiếp")
        self.auto_res.setChecked(cfg.auto_resume)
        f3.addRow("", self.auto_res)
        self.resume_start = QCheckBox("Mở app là canh lại danh sách phòng lần trước")
        self.resume_start.setChecked(cfg.resume_on_start)
        f3.addRow("", self.resume_start)
        self.poll = QSpinBox()
        self.poll.setRange(15, 3600)
        self.poll.setSingleStep(5)
        self.poll.setSuffix(" giây")
        self.poll.setValue(cfg.poll_every)
        self.poll.setToolTip("Dò dày quá là tự chuốc risk-control. 30–60 giây là hợp lý.")
        f3.addRow("Dò lại mỗi", self.poll)
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 12)
        self.parallel.setValue(cfg.max_parallel)
        self.parallel.setToolTip("Chỉ đếm phòng ĐANG GHI. Phòng đang canh không tốn suất.")
        f3.addRow("Ghi cùng lúc tối đa", self.parallel)
        box3.layout().addLayout(f3)
        root.addWidget(box3)

        # ── làn mạng ──
        boxp = self._card("Làn mạng (proxy)")
        cap = QLabel("Ghi nhiều phòng qua một IP thì CDN bóp băng thông. Chia làn để "
                     "mỗi phòng đi một lối ra riêng.")
        cap.setObjectName("muted")
        cap.setWordWrap(True)
        boxp.layout().addWidget(cap)
        fp = QFormLayout()
        fp.setSpacing(9)
        self.pmode = QComboBox()
        for k, t in PROXY_MODE_CHOICES:
            self.pmode.addItem(t, k)
        self.pmode.setCurrentIndex(max(0, self.pmode.findData(cfg.proxy_mode)))
        fp.addRow("Chế độ", self.pmode)

        self.planes = QSpinBox()
        self.planes.setRange(0, 16)
        self.planes.setValue(cfg.proxy_lanes)
        self.planes.setToolTip("Số làn proxy, chưa kể làn direct.")
        fp.addRow("Số làn", self.planes)

        self.pdirect = QCheckBox("Dùng cả IP máy làm một làn")
        self.pdirect.setChecked(cfg.proxy_include_direct)
        fp.addRow("", self.pdirect)

        self.prooms = QSpinBox()
        self.prooms.setRange(1, 20)
        self.prooms.setValue(cfg.proxy_rooms_per_lane)
        self.prooms.setToolTip("Trần số phòng bám dính một làn. Vượt trần thì tràn "
                               "sang làn ít việc nhất.")
        fp.addRow("Phòng mỗi làn", self.prooms)

        self.plist = QPlainTextEdit("\n".join(cfg.proxy_list))
        self.plist.setPlaceholderText("http://user:pass@host:port\nhttp://host:port\n… "
                                      "mỗi dòng một proxy, mỗi proxy thành một làn")
        self.plist.setFixedHeight(78)
        fp.addRow("Danh sách", self.plist)

        self.ptoken = QLineEdit(cfg.nordvpn_token)
        self.ptoken.setEchoMode(QLineEdit.EchoMode.Password)
        self.ptoken.setPlaceholderText("access token NordVPN")
        self.ptoken.setToolTip("Lưu dạng CHỮ THƯỜNG trong hglive.config.json cạnh tool.")
        fp.addRow("Token NordVPN", self.ptoken)

        self.ptokfile = QLineEdit(cfg.nordvpn_token_file)
        self.ptokfile.setPlaceholderText("để trống = tự dò: biến môi trường, "
                                         ".nordvpn_token / .env cạnh tool…")
        fp.addRow("File token", self._browse(self.ptokfile, folder=False))
        self.ptok_state = QLabel("")
        self.ptok_state.setObjectName("muted")
        fp.addRow("", self.ptok_state)

        self.pcountry = QLineEdit(cfg.proxy_country)
        self.pcountry.setPlaceholderText("để trống = nước nào cũng được · hoặc JP / Singapore")
        fp.addRow("Nước", self.pcountry)

        self.pms = QSpinBox()
        self.pms.setRange(50, 5000)
        self.pms.setSingleStep(50)
        self.pms.setSuffix(" ms")
        self.pms.setValue(int(cfg.proxy_max_latency_ms))
        fp.addRow("Trần độ trễ", self.pms)

        self.pwire = QLineEdit(cfg.wireproxy_path)
        self.pwire.setPlaceholderText("để trống = tự tìm cạnh tool / trong PATH, "
                                      "không có thì tự tải")
        fp.addRow("wireproxy", self._browse(self.pwire, folder=False))

        self.prot = QDoubleSpinBox()
        self.prot.setRange(0.1, 72.0)
        self.prot.setSingleStep(0.5)
        self.prot.setDecimals(1)
        self.prot.setSuffix(" giờ")
        self.prot.setValue(cfg.rotate_min_hours)
        self.prot2 = QDoubleSpinBox()
        self.prot2.setRange(0.1, 96.0)
        self.prot2.setSingleStep(0.5)
        self.prot2.setDecimals(1)
        self.prot2.setSuffix(" giờ")
        self.prot2.setValue(cfg.rotate_max_hours)
        rot = QWidget()
        rh = QHBoxLayout(rot)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.setSpacing(6)
        rh.addWidget(self.prot, 1)
        rh.addWidget(QLabel("→"))
        rh.addWidget(self.prot2, 1)
        rot.setToolTip("Xoay IP ngẫu nhiên trong khoảng này. Làn đang ghi thì hoãn "
                       "tới lúc rảnh.")
        fp.addRow("Xoay IP mỗi", rot)
        self.prot_row = rot
        self.pwire_row = self.pwire.parentWidget()
        self.ptokfile_row = self.ptokfile.parentWidget()
        boxp.layout().addLayout(fp)
        root.addWidget(boxp)
        self._fp = fp
        self.pmode.currentIndexChanged.connect(self._sync_proxy_rows)
        self._sync_proxy_rows()

        # ── file ──
        box4 = self._card("Cắt file & đặt tên")
        f4 = QFormLayout()
        f4.setSpacing(9)
        self.seg = QSpinBox()
        self.seg.setRange(0, 720)
        self.seg.setSuffix(" phút")
        self.seg.setSpecialValueText("không cắt — một file liền mạch")
        self.seg.setValue(cfg.segment_min)
        f4.addRow("Cắt file mỗi", self.seg)
        self.maxh = QDoubleSpinBox()
        self.maxh.setRange(0.0, 48.0)
        self.maxh.setSingleStep(0.5)
        self.maxh.setDecimals(1)
        self.maxh.setSuffix(" giờ")
        self.maxh.setSpecialValueText("không giới hạn")
        self.maxh.setValue(cfg.max_hours)
        f4.addRow("Trần mỗi phiên", self.maxh)
        self.name = QLineEdit(cfg.name_tpl)
        self.name.setToolTip("{nick} {rid} {room} {title} {date} {time} {quality} {res}")
        f4.addRow("Mẫu tên", self.name)
        self.subdir = QCheckBox("Mỗi phòng một thư mục riêng")
        self.subdir.setChecked(cfg.room_subdir)
        f4.addRow("", self.subdir)
        self.meta = QCheckBox("Lưu kèm .info.json và ảnh bìa")
        self.meta.setChecked(cfg.save_meta)
        f4.addRow("", self.meta)
        self.verbose = QCheckBox("Nhật ký chi tiết (in cả dòng ffmpeg)")
        self.verbose.setChecked(cfg.log_verbose)
        f4.addRow("", self.verbose)
        box4.layout().addLayout(f4)
        root.addWidget(box4)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Lưu")
        bb.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("Huỷ")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self.cookie.textChanged.connect(self._refresh_cookie)
        self._refresh_cookie()
        for w in (self.ptoken, self.ptokfile):
            w.textChanged.connect(self._refresh_token)

    def _card(self, title: str) -> QFrame:
        fr = QFrame()
        fr.setObjectName("card")
        v = QVBoxLayout(fr)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)
        lab = QLabel(title)
        lab.setObjectName("h2")
        v.addWidget(lab)
        return fr

    def _browse(self, edit: QLineEdit, folder: bool) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        h.addWidget(edit, 1)
        b = QPushButton("Chọn…")
        b.setObjectName("tiny")
        h.addWidget(b)

        def pick():
            if folder:
                d = QFileDialog.getExistingDirectory(self, "Chọn thư mục", edit.text())
            else:
                d, _ = QFileDialog.getOpenFileName(self, "Chọn file cookie", edit.text(),
                                                   "Cookie (*.txt *.json);;Tất cả (*.*)")
            if d:
                edit.setText(d)
        b.clicked.connect(pick)
        return w

    def _sync_proxy_rows(self):
        """Chỉ hiện ô liên quan tới chế độ đang chọn — bày cả 10 ô ra thì rối, mà
        7 trong số đó vô nghĩa với chế độ đang dùng."""
        mode = self.pmode.currentData()
        on = mode != "off"
        for w in (self.planes, self.pdirect, self.prooms):
            self._fp.setRowVisible(w, on)
        self._fp.setRowVisible(self.plist, mode == "list")
        # Ô nào bị bọc trong widget con thì phải tra theo widget BỌC — đưa ô con vào
        # setRowVisible là Qt không tìm ra hàng, im lặng không làm gì.
        for w in (self.ptoken, self.ptokfile_row, self.ptok_state, self.pcountry,
                  self.pms, self.pwire_row, self.prot_row):
            self._fp.setRowVisible(w, mode == "nordvpn")
        if mode == "nordvpn":
            self._refresh_token()

    def _refresh_token(self):
        """Cho biết token đang lấy từ đâu — hiện NGUỒN và bản che, không hiện token."""
        typed = self.ptoken.text().strip()
        if typed:
            self.ptok_state.setText(f"Token: gõ tay · {NV.mask(typed)}")
            return
        tok, src = NV.discover_token(self.ptokfile.text().strip())
        self.ptok_state.setText(
            f"Token: {NV.mask(tok)} — dùng ké từ {src}" if tok
            else "Token: chưa tìm ra ở đâu cả — gõ tay hoặc chỉ file .env")
        self.ptok_state.setStyleSheet("" if tok else f"color:{C['err']};")

    def _refresh_cookie(self):
        self.ck_state.setText("Cookie: " + CK.summary(CK.load(self.cookie.text())))

    def apply_to(self, cfg: Settings) -> Settings:
        cfg.out_dir = self.out.text().strip() or cfg.out_dir
        cfg.cookie_path = self.cookie.text().strip()
        cfg.proxy = self.proxy.text().strip()
        cfg.quality = self.quality.currentData()
        cfg.proto = self.proto.currentData()
        cfg.ext = self.ext.currentData()
        cfg.auto_record = self.auto_rec.isChecked()
        cfg.auto_resume = self.auto_res.isChecked()
        cfg.resume_on_start = self.resume_start.isChecked()
        cfg.poll_every = self.poll.value()
        cfg.max_parallel = self.parallel.value()
        cfg.segment_min = self.seg.value()
        cfg.max_hours = self.maxh.value()
        cfg.proxy_mode = self.pmode.currentData()
        cfg.proxy_lanes = self.planes.value()
        cfg.proxy_include_direct = self.pdirect.isChecked()
        cfg.proxy_rooms_per_lane = self.prooms.value()
        cfg.proxy_list = [l.strip() for l in self.plist.toPlainText().splitlines() if l.strip()]
        cfg.nordvpn_token = self.ptoken.text().strip()
        cfg.nordvpn_token_file = self.ptokfile.text().strip()
        cfg.proxy_country = self.pcountry.text().strip()
        cfg.proxy_max_latency_ms = float(self.pms.value())
        cfg.wireproxy_path = self.pwire.text().strip()
        cfg.rotate_min_hours = self.prot.value()
        cfg.rotate_max_hours = max(self.prot2.value(), self.prot.value())
        cfg.name_tpl = self.name.text().strip() or cfg.name_tpl
        cfg.room_subdir = self.subdir.isChecked()
        cfg.save_meta = self.meta.isChecked()
        cfg.log_verbose = self.verbose.isChecked()
        cfg.sanitize()
        return cfg


# ───────────────────────── cửa sổ chính ─────────────────────────
class MainWindow(QMainWindow):
    def __init__(self, cfg: Settings):
        super().__init__()
        self.cfg = cfg
        self.bridge = Bridge()
        self.rows: Dict[str, int] = {}        # rid -> chỉ số dòng
        self.snap: Dict[str, RC.Task] = {}    # rid -> task mới nhất
        self.row_btn: Dict[str, QPushButton] = {}   # rid -> nút Tạm dừng/Ghi tiếp
        self.probe_room: Optional[dict] = None
        self.probe_qs: List[dict] = []

        self.lanes = LN.LaneManager(cfg, log=self.bridge.log.emit)
        self.rec = RC.Recorder(cfg, CK.load(cfg.cookie_path),
                               on_update=self.bridge.task.emit,
                               on_log=self.bridge.log.emit,
                               lanes=self.lanes)

        self.setWindowTitle("HGLIVE — bắt live Douyin")
        w, h = (cfg.window + [1120, 720])[:2]
        self.resize(int(w), int(h))
        self._build()
        self._wire()
        self._check_env()

        # Một nhịp tim duy nhất cho cả cửa sổ: vẽ lại vệt "Nhịp" và làm mới các ô
        # tiến độ. Mỗi dòng tự chạy timer riêng thì 10 phòng là 10 lần vẽ thừa.
        self.beat = QTimer(self)
        self.beat.timeout.connect(self._beat)
        self.beat.start(120)

        # Dựng làn TRƯỚC khi canh lại: chế độ nordvpn mất vài chục giây, mà phòng
        # nào bắt đầu ghi lúc làn chưa lên thì bám luôn vào direct và ở lại đó.
        self._start_lanes(then=self._resume_watch
                          if (cfg.resume_on_start and cfg.watch) else None)

    # ───────────────────────── dựng giao diện ─────────────────────────
    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(11)

        # ── đầu trang ──
        head = QHBoxLayout()
        head.setSpacing(9)
        brand = QLabel("HGLIVE")
        brand.setObjectName("brand")
        head.addWidget(brand)
        sub = QLabel("bắt luồng live Douyin")
        sub.setObjectName("muted")
        head.addWidget(sub)
        head.addStretch(1)
        self.b_ck = self._badge("cookie")
        self.b_ff = self._badge("ffmpeg")
        self.b_lane = self._badge("làn: —")
        self.b_lane.setCursor(Qt.CursorShape.PointingHandCursor)
        self.b_lane.setToolTip("Bấm để xem/xoay làn mạng")
        self.b_lane.mousePressEvent = lambda e: self.on_lanes()
        head.addWidget(self.b_ck)
        head.addWidget(self.b_ff)
        head.addWidget(self.b_lane)
        self.btn_dir = QPushButton("Thư mục")
        self.btn_log = QPushButton("Nhật ký")
        self.btn_cfg = QPushButton("Cài đặt")
        for b in (self.btn_dir, self.btn_log, self.btn_cfg):
            b.setObjectName("tiny")
            head.addWidget(b)
        v.addLayout(head)

        # ── ô nhập ──
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.url = QLineEdit()
        self.url.setObjectName("url")
        self.url.setPlaceholderText("Dán link phòng live  ·  live.douyin.com/578280456923  "
                                    "·  v.douyin.com/…  ·  hoặc chỉ web_rid")
        bar.addWidget(self.url, 1)
        self.btn_probe = QPushButton("Dò phòng")
        self.btn_watch = QPushButton("Canh sóng")
        self.btn_watch.setObjectName("primary")
        bar.addWidget(self.btn_probe)
        bar.addWidget(self.btn_watch)
        v.addLayout(bar)

        # ── thẻ phòng vừa dò ──
        self.card = QFrame()
        self.card.setObjectName("card")
        cv = QHBoxLayout(self.card)
        cv.setContentsMargins(12, 10, 12, 10)
        cv.setSpacing(12)
        self.cover = QLabel()
        self.cover.setObjectName("cover")
        self.cover.setFixedSize(112, 63)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cv.addWidget(self.cover)
        info = QVBoxLayout()
        info.setSpacing(3)
        self.c_title = QLabel("—")
        self.c_title.setObjectName("h2")
        self.c_title.setWordWrap(False)
        self.c_sub = QLabel("")
        self.c_sub.setObjectName("muted")
        self.c_streams = QLabel("")
        self.c_streams.setObjectName("muted")
        info.addWidget(self.c_title)
        info.addWidget(self.c_sub)
        info.addWidget(self.c_streams)
        cv.addLayout(info, 1)
        self.btn_recnow = QPushButton("Ghi ngay")
        self.btn_recnow.setObjectName("primary")
        self.btn_open_web = QPushButton("Mở web")
        self.btn_open_web.setObjectName("tiny")
        side = QVBoxLayout()
        side.setSpacing(6)
        side.addWidget(self.btn_recnow)
        side.addWidget(self.btn_open_web)
        cv.addLayout(side)
        self.card.hide()
        v.addWidget(self.card)

        # ── bảng ──
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.setItemDelegateForColumn(COL_BAR, PulseDelegate(self.table))
        hh = self.table.horizontalHeader()
        # Chỉ CỘT FILE giãn: tên file là thứ dài không đoán trước được. Cột Phòng để
        # kéo tay vì nick tiếng Trung ngắn mà nick tiếng Việt thì dài.
        hh.setSectionResizeMode(COL_ROOM, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(COL_FILE, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(COL_ROOM, 160)
        for c, w in ((COL_ACT, 106), (COL_STATE, 98), (COL_LANE, 70), (COL_Q, 134),
                     (COL_TIME, 84), (COL_SIZE, 88), (COL_SPEED, 84), (COL_BAR, 88)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(c, w)
        v.addWidget(self.table, 1)

        # ── chân trang ──
        foot = QHBoxLayout()
        foot.setSpacing(9)
        self.legend = QLabel(_legend_html())
        foot.addWidget(self.legend)
        foot.addStretch(1)
        self.total = QLabel("—")
        self.total.setObjectName("muted")
        foot.addWidget(self.total)
        # Ba nút này ăn theo VÙNG CHỌN; không chọn dòng nào thì áp cho tất cả.
        self.btn_pause = QPushButton("Tạm dừng")
        self.btn_resume = QPushButton("Ghi tiếp")
        self.btn_stop_all = QPushButton("Dừng hẳn")
        for b, tip in ((self.btn_pause, "Dừng ghi các phòng đang chọn nhưng vẫn canh sóng"),
                       (self.btn_resume, "Ghi tiếp các phòng đang chọn"),
                       (self.btn_stop_all, "Dừng hẳn: thôi ghi, thôi canh")):
            b.setObjectName("tiny")
            b.setToolTip(tip + "  ·  không chọn dòng nào = áp cho tất cả")
            foot.addWidget(b)
        v.addLayout(foot)

        # ── nhật ký ──
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(4000)
        self.log.setFixedHeight(150)
        self.log.hide()
        v.addWidget(self.log)

    def _badge(self, text: str, color: Optional[str] = None) -> QLabel:
        lab = QLabel(text)
        lab.setProperty("badge", "true")
        if color:
            lab.setStyleSheet(f"color:{color}; border-color:{color};")
        return lab

    # ───────────────────────── nối tín hiệu ─────────────────────────
    def _wire(self):
        self.btn_probe.clicked.connect(self.on_probe)
        self.btn_watch.clicked.connect(self.on_watch)
        self.btn_recnow.clicked.connect(self.on_rec_now)
        self.btn_open_web.clicked.connect(self.on_open_web)
        self.btn_pause.clicked.connect(self.on_pause_sel)
        self.btn_resume.clicked.connect(self.on_resume_sel)
        self.btn_stop_all.clicked.connect(self.on_stop_all)
        self.btn_dir.clicked.connect(lambda: open_in_explorer(Path(self.cfg.out_dir)))
        self.btn_log.clicked.connect(self.on_toggle_log)
        self.btn_cfg.clicked.connect(self.on_settings)
        self.url.returnPressed.connect(self.on_watch)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.doubleClicked.connect(self._open_row)

        self.bridge.log.connect(self._log)
        self.bridge.task.connect(self._on_task)
        self.bridge.probed.connect(self._on_probed)
        self.bridge.probe_failed.connect(self._on_probe_failed)
        self.bridge.cover.connect(self._on_cover)
        self.bridge.lanes_ready.connect(self._on_lanes_ready)

        for keys, fn in ((("Ctrl+L",), lambda: (self.url.setFocus(), self.url.selectAll())),
                         (("Ctrl+D",), self.on_watch),
                         (("Ctrl+R",), self.on_rec_now),
                         (("Ctrl+S",), self.on_settings),
                         (("Ctrl+K",), self.on_toggle_log),
                         (("Space",), self.on_toggle_sel),
                         (("Delete",), self.on_remove_selected)):
            QShortcut(QKeySequence(keys[0]), self, activated=fn)

    def _check_env(self):
        ck = CK.load(self.cfg.cookie_path)
        ok_ck = CK.has_ttwid(ck)
        self.b_ck.setText("ttwid ✓" if ok_ck else "ttwid: tự xin")
        self.b_ck.setToolTip("Cookie: " + CK.summary(ck) +
                             "\nLive chỉ cần ttwid — không cần đăng nhập.")
        self.b_ck.setStyleSheet(f"color:{C['livegreen']}; border-color:{C['livegreen']};"
                                if ok_ck else "")
        ff = has_ffmpeg()
        self.b_ff.setText("ffmpeg ✓" if ff else "THIẾU ffmpeg")
        self.b_ff.setStyleSheet("" if ff else f"color:{C['err']}; border-color:{C['err']};")
        if not ff:
            self._log("✗ Không tìm thấy ffmpeg trong PATH — không ghi được. "
                      "Tải tại https://www.gyan.dev/ffmpeg/builds/ rồi thêm vào PATH.")

    # ───────────────────────── làn mạng ─────────────────────────
    def _start_lanes(self, then=None):
        """Dựng làn ở luồng nền (chế độ NordVPN mất vài chục giây)."""
        self._lanes_then = then
        self.b_lane.setText("làn: đang dựng…")
        self.b_lane.setStyleSheet(f"color:{C['watch']}; border-color:{C['watch']};")

        def work():
            try:
                self.lanes.start()
            except Exception as e:
                self.bridge.log.emit(f"✗ dựng làn hỏng: {str(e)[:140]}")
            self.bridge.lanes_ready.emit()
        threading.Thread(target=work, daemon=True).start()

    def _on_lanes_ready(self):
        n = len(self.lanes.lanes)
        prox = sum(1 for l in self.lanes.lanes if not l.is_direct)
        self.b_lane.setText(f"làn: {n}" + (f" ({prox} proxy)" if prox else " (thẳng)"))
        self.b_lane.setToolTip(self.lanes.summary() + "\nBấm để xem/xoay làn mạng")
        self.b_lane.setStyleSheet(f"color:{C['info']}; border-color:{C['info']};"
                                  if prox else "")
        then, self._lanes_then = getattr(self, "_lanes_then", None), None
        if then:
            then()

    def _lane_tip(self, lane_id: str) -> str:
        if not lane_id:
            return ""
        for r in self.lanes.report():
            if r["id"] != lane_id:
                continue
            bits = [f"làn {r['id']}"]
            if r["direct"]:
                bits.append("đi thẳng bằng IP máy")
            else:
                bits.append(f"proxy {r['proxy']}")
                if r["exit_ip"]:
                    bits.append(f"IP ra {r['exit_ip']} ({r['country'] or '?'})")
                if r["latency_ms"]:
                    bits.append(f"{r['latency_ms']:.0f} ms")
                if r["server"]:
                    bits.append(str(r["server"]))
            bits.append(f"{r['busy']} đang ghi · {len(r['bound'])} phòng bám dính")
            return "\n".join(bits)
        return f"làn {lane_id}"

    def on_lanes(self):
        LanesDialog(self, self.lanes).exec()

    # ───────────────────────── nhật ký ─────────────────────────
    def _log(self, msg: str):
        self.log.appendPlainText(f"[{fmt_clock()}] {msg}")

    def on_toggle_log(self):
        self.log.setVisible(not self.log.isVisible())

    # ───────────────────────── dò phòng ─────────────────────────
    def _rid_from_box(self) -> Optional[str]:
        raw = self.url.text().strip()
        if not raw:
            QMessageBox.information(self, "HGLIVE", "Dán link phòng live vào ô trên đã.")
            return None
        try:
            return LV.resolve_rid(raw)
        except LV.LiveError as e:
            QMessageBox.warning(self, "Không đọc được link", str(e))
            return None

    def on_probe(self):
        rid = self._rid_from_box()
        if not rid:
            return
        self.btn_probe.setEnabled(False)
        self.btn_probe.setText("Đang dò…")
        cfg, ck = self.cfg, CK.load(self.cfg.cookie_path)

        def work():
            try:
                room = LV.room_info(rid, ck, cfg.proxy)
            except LV.LiveError as e:
                self.bridge.probe_failed.emit(str(e))
                return
            self.bridge.probed.emit(room, LV.qualities(room))
            url = LV.cover_url(room)
            if url:
                try:
                    r = requests.get(url, headers={"User-Agent": LV.UA}, timeout=15)
                    if r.ok:
                        self.bridge.cover.emit(r.content)
                except Exception:
                    pass
        threading.Thread(target=work, daemon=True).start()

    def _on_probe_failed(self, msg: str):
        self.btn_probe.setEnabled(True)
        self.btn_probe.setText("Dò phòng")
        self.card.hide()
        self._log(f"✗ dò hỏng: {msg}")
        QMessageBox.warning(self, "Dò phòng hỏng", msg)

    def _on_probed(self, room: dict, qs: List[dict]):
        self.btn_probe.setEnabled(True)
        self.btn_probe.setText("Dò phòng")
        self.probe_room, self.probe_qs = room, qs
        live_now = LV.is_live(room)
        self.c_title.setText(f"{LV.nickname(room) or '?'} — {room.get('title') or '(không tiêu đề)'}")
        state = LV.STATUS.get(room.get("status"), str(room.get("status")))
        color = C["livegreen"] if live_now else C["off"]
        self.c_sub.setText(
            f'<span style="color:{color}">● {state}</span>'
            f'<span style="color:{C["muted"]}">  ·  {LV.viewers(room) or "?"} người xem'
            f'  ·  room {room.get("id_str") or "?"}</span>')
        if qs:
            self.c_streams.setText("Luồng: " + "   ".join(
                f"{q['key']}{' ' + q['res'] if q['res'] else ''}" for q in qs))
        else:
            self.c_streams.setText("Phòng chưa phát luồng nào — canh sóng để tự ghi khi mở.")
        self.cover.setText("" if LV.cover_url(room) else "không có bìa")
        self.card.show()
        self.btn_recnow.setEnabled(live_now)
        self._log(f"✓ {LV.describe(room)}")

    def _on_cover(self, data: bytes):
        pm = QPixmap()
        if not pm.loadFromData(data):
            return
        pm = pm.scaled(self.cover.width(), self.cover.height(),
                       Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                       Qt.TransformationMode.SmoothTransformation)
        # bo góc cho khớp viền thẻ, không thì ảnh vuông chồi ra khỏi khung bo
        out = QPixmap(self.cover.size())
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, out.width(), out.height()), 7, 7)
        p.setClipPath(path)
        p.drawPixmap(int((out.width() - pm.width()) / 2),
                     int((out.height() - pm.height()) / 2), pm)
        p.end()
        self.cover.setPixmap(out)

    def on_open_web(self):
        try:
            webbrowser.open(f"https://live.douyin.com/{LV.resolve_rid(self.url.text())}")
        except LV.LiveError:
            pass

    # ───────────────────────── canh / ghi ─────────────────────────
    def on_watch(self, armed: bool = False):
        rid = self._rid_from_box()
        if not rid:
            return
        self._ensure_row(rid)
        self.rec.add(rid, armed=bool(armed))
        if rid not in self.cfg.watch:
            self.cfg.watch.append(rid)
        self._log(f"+ canh phòng {rid}" + (" (ghi ngay khi có sóng)" if armed else ""))
        self.url.clear()
        self.card.hide()

    def on_rec_now(self):
        self.on_watch(armed=True)

    def _resume_watch(self):
        for rid in list(self.cfg.watch):
            self._ensure_row(rid)
            self.rec.add(rid)
        self._log(f"↻ canh lại {len(self.cfg.watch)} phòng của lần trước")

    # ── tạm dừng / ghi tiếp: từng phòng, hoặc theo vùng chọn, hoặc tất cả ──
    def on_toggle_row(self, rid: str):
        t = self.snap.get(rid)
        resting = bool(t) and t.state in (RC.PAUSED, RC.STOPPED)
        self.rec.resume(rid) if resting else self.rec.pause(rid)
        self._log(("▶ ghi tiếp " if resting else "⏸ tạm dừng ") + (t.label() if t else rid))
        if t:
            t.state = RC.WAITING if resting else RC.PAUSED   # phản hồi ngay, luồng
            self._on_task(t)                                  # nền sẽ đính chính sau

    def _act_on_selection(self, fn, verb: str):
        rids = self._selected_rids() or list(self.rows)
        for rid in rids:
            fn(rid)
        self._log(f"{verb} {len(rids)} phòng")

    def on_pause_sel(self):
        self._act_on_selection(self.rec.pause, "⏸ tạm dừng")

    def on_resume_sel(self):
        self._act_on_selection(self.rec.resume, "▶ ghi tiếp")

    def on_toggle_sel(self):
        for rid in (self._selected_rids() or list(self.rows)):
            self.on_toggle_row(rid)

    def on_stop_all(self):
        self.rec.stop_all()
        self._log("⏹ dừng hẳn tất cả")

    def on_remove_selected(self):
        for rid in self._selected_rids():
            self.rec.remove(rid)
            if rid in self.cfg.watch:
                self.cfg.watch.remove(rid)
            row = self.rows.pop(rid, None)
            self.snap.pop(rid, None)
            self.row_btn.pop(rid, None)   # removeRow xoá luôn widget, chỉ cần bỏ tham chiếu
            if row is not None:
                self.table.removeRow(row)
                for k, r in self.rows.items():
                    if r > row:
                        self.rows[k] = r - 1

    def _selected_rids(self) -> List[str]:
        rows = {i.row() for i in self.table.selectedIndexes()}
        return [rid for rid, r in self.rows.items() if r in rows]

    # ───────────────────────── bảng ─────────────────────────
    def _ensure_row(self, rid: str) -> int:
        if rid in self.rows:
            return self.rows[rid]
        row = self.table.rowCount()
        self.table.insertRow(row)
        for c in range(len(HEADERS)):
            it = QTableWidgetItem("")
            if c in (COL_TIME, COL_SIZE, COL_SPEED):
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight |
                                    Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, c, it)
        self.table.item(row, COL_ROOM).setText(rid)
        # Nút nằm ngay trên dòng: dựng MỘT lần rồi chỉ đổi chữ. Tạo lại widget mỗi
        # lần cập nhật (2-3 lần/giây mỗi phòng đang ghi) là phí và làm nháy nút.
        b = QPushButton("Tạm dừng")
        b.setObjectName("rowact")
        b.setFixedHeight(22)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda _=False, r=rid: self.on_toggle_row(r))
        # Bọc trong widget có lề: setCellWidget cho nút chiếm trọn ô, dán sát viền
        # dòng trên/dưới trông như lỗi vẽ.
        wrap = QWidget()
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(6, 0, 6, 0)
        wl.addWidget(b)
        self.table.setCellWidget(row, COL_ACT, wrap)
        self.row_btn[rid] = b
        self.rows[rid] = row
        return row

    def _on_task(self, t: RC.Task):
        self.snap[t.rid] = t
        row = self._ensure_row(t.rid)
        room = self.table.item(row, COL_ROOM)
        room.setText(t.nick or t.rid)
        room.setToolTip(f"web_rid {t.rid}\nroom {t.room_id}\n{t.title}")

        st = self.table.item(row, COL_STATE)
        st.setText(RC.STATE_TEXT.get(t.state, t.state))
        st.setForeground(QColor(STATE_COLOR.get(t.state, C["off"])))
        st.setToolTip(t.error or t.note or "")

        ln = self.table.item(row, COL_LANE)
        ln.setText(("thẳng" if t.lane == LN.DIRECT else t.lane.replace("proxy-", "#"))
                   if t.lane else "—")
        ln.setForeground(QColor(C["muted"] if t.lane in ("", LN.DIRECT) else C["info"]))
        ln.setToolTip(self._lane_tip(t.lane))

        self.table.item(row, COL_Q).setText(
            f"{t.quality} {t.res}".strip() if t.quality else "—")
        self.table.item(row, COL_TIME).setText(fmt_hms(t.secs) if t.secs else "—")
        self.table.item(row, COL_SIZE).setText(human_size(t.bytes) if t.bytes else "—")
        self.table.item(row, COL_SPEED).setText(
            human_speed(t.speed) if t.state == RC.REC else "—")

        bar = self.table.item(row, COL_BAR)
        bar.setData(Qt.ItemDataRole.UserRole, t.state)
        bar.setData(Qt.ItemDataRole.UserRole + 1, self._seg_frac(t))

        f = self.table.item(row, COL_FILE)
        if t.file:
            n = len(t.parts)
            f.setText(t.file.name + (f"  ({n} miếng)" if n > 1 else ""))
            f.setToolTip(str(t.file))
        else:
            f.setText(t.note or t.error or "")
            f.setToolTip(t.error or "")

        self._paint_btn(t)

    def _paint_btn(self, t: RC.Task):
        b = self.row_btn.get(t.rid)
        if not b:
            return
        resting = t.state in (RC.PAUSED, RC.STOPPED)
        b.setText("Ghi tiếp" if resting else "Tạm dừng")
        b.setToolTip("Ghi tiếp phòng này" if resting else
                     "Dừng ghi phòng này nhưng vẫn canh sóng "
                     "(file đang ghi được đóng tử tế)")
        b.setStyleSheet(f"color:{C['livegreen']}; border-color:{C['livegreen']};"
                        if resting else "")

    def _seg_frac(self, t: RC.Task) -> Optional[float]:
        """Đang cắt file thì vệt chạy theo tiến độ miếng; không cắt thì trả None
        để delegate chuyển sang kiểu nhịp thở."""
        if t.state != RC.REC or self.cfg.segment_min <= 0:
            return None
        span = self.cfg.segment_min * 60
        return (t.secs % span) / span if span > 0 else None

    def _beat(self):
        """Nhịp tim: vẽ lại vệt cho các dòng đang ghi + làm mới dòng tổng."""
        rec_rows = [r for rid, r in self.rows.items()
                    if (self.snap.get(rid) and self.snap[rid].state == RC.REC)]
        for r in rec_rows:
            # QAbstractItemView.update() nhận QModelIndex, KHÔNG nhận QRect như
            # QWidget.update() — đưa rect vào là TypeError mỗi nhịp tim.
            self.table.update(self.table.model().index(r, COL_BAR))
        self._paint_total(bool(rec_rows))

    def _paint_total(self, any_rec: bool):
        n_rec = sum(1 for t in self.snap.values() if t.state == RC.REC)
        n_q = sum(1 for t in self.snap.values() if t.state == RC.QUEUED)
        n_watch = sum(1 for t in self.snap.values() if t.state in (RC.WAITING, RC.LIVE, RC.IDLE))
        tot_b = sum(t.bytes for t in self.snap.values())
        tot_s = sum(t.speed for t in self.snap.values() if t.state == RC.REC)
        bits = []
        if n_rec:
            bits.append(f'<span style="color:{C["rec"]}">● {n_rec} đang ghi</span>')
        if n_q:
            bits.append(f'<span style="color:{C["queue"]}">● {n_q} xếp hàng</span>')
        n_p = sum(1 for t in self.snap.values() if t.state == RC.PAUSED)
        if n_p:
            bits.append(f'<span style="color:{C["pause"]}">● {n_p} tạm dừng</span>')
        if n_watch:
            bits.append(f'<span style="color:{C["watch"]}">● {n_watch} canh sóng</span>')
        bits.append(f'<span style="color:{C["muted"]}">{human_size(tot_b)}</span>')
        if tot_s:
            bits.append(f'<span style="color:{C["muted"]}">{human_speed(tot_s)}</span>')
        self.total.setText("&nbsp;&nbsp;·&nbsp;&nbsp;".join(bits) if bits else "—")

    # ───────────────────────── menu chuột phải ─────────────────────────
    def _menu(self, pos):
        rids = self._selected_rids()
        if not rids:
            return
        rid = rids[0]
        t = self.snap.get(rid)
        m = QMenu(self)
        resting = bool(t) and t.state in (RC.PAUSED, RC.STOPPED)
        a_rec = m.addAction("Ghi tiếp" if resting else "Ghi ngay")
        a_pause = m.addAction("Tạm dừng ghi (vẫn canh sóng)")
        a_pause.setEnabled(not resting)
        a_stop = m.addAction("Dừng hẳn (thôi canh)")
        m.addSeparator()
        a_web = m.addAction("Mở phòng trên web")
        a_dir = m.addAction("Mở thư mục chứa file")
        a_copy = m.addAction("Copy URL luồng (để dán vào VLC)")
        m.addSeparator()
        a_del = m.addAction("Bỏ khỏi danh sách")
        act = m.exec(self.table.viewport().mapToGlobal(pos))
        if act is a_rec:
            for r in rids:
                self.rec.resume(r)
        elif act is a_pause:
            for r in rids:
                self.rec.pause(r)
        elif act is a_stop:
            for r in rids:
                self.rec.stop(r)
        elif act is a_web:
            webbrowser.open(f"https://live.douyin.com/{rid}")
        elif act is a_dir:
            open_in_explorer(t.file if (t and t.file) else Path(self.cfg.out_dir))
        elif act is a_copy:
            self._copy_stream_url(rid)
        elif act is a_del:
            self.on_remove_selected()

    def _copy_stream_url(self, rid: str):
        cfg, ck = self.cfg, CK.load(self.cfg.cookie_path)

        def work():
            try:
                room = LV.room_info(rid, ck, cfg.proxy)
                q = LV.pick(LV.qualities(room), cfg.quality, cfg.proto)
                url = q.get(cfg.proto) or ""
            except LV.LiveError as e:
                self.bridge.log.emit(f"✗ không lấy được URL: {e}")
                return
            if not url:
                self.bridge.log.emit("✗ phòng không có luồng để copy")
                return
            QGuiApplication.clipboard().setText(url)
            self.bridge.log.emit(f"📋 đã copy URL {LV.label(q)} ({cfg.proto}) vào clipboard")
        threading.Thread(target=work, daemon=True).start()

    def _open_row(self, index):
        rid = next((r for r, row in self.rows.items() if row == index.row()), None)
        t = self.snap.get(rid or "")
        open_in_explorer(t.file if (t and t.file) else Path(self.cfg.out_dir))

    # ───────────────────────── cài đặt / đóng ─────────────────────────
    _PROXY_KEYS = ("proxy_mode", "proxy_lanes", "proxy_include_direct", "proxy_list",
                   "nordvpn_token", "nordvpn_token_file", "proxy_country", "proxy_max_latency_ms",
                   "wireproxy_path", "proxy_socks_start", "proxy_http_start")

    def on_settings(self):
        d = SettingsDialog(self.cfg, self)
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        before = {k: repr(getattr(self.cfg, k, None)) for k in self._PROXY_KEYS}
        d.apply_to(self.cfg)
        self.cfg.save()
        if any(before[k] != repr(getattr(self.cfg, k, None)) for k in self._PROXY_KEYS):
            # Đổi cấu hình làn thì phải dựng lại — sửa cfg không tự làm wireproxy
            # đang chạy đổi theo. Phòng đang ghi giữ nguyên làn cũ tới hết phiên.
            self._log("⚙ cấu hình làn đổi — dựng lại")
            old = self.lanes
            self.lanes = LN.LaneManager(self.cfg, log=self.bridge.log.emit)
            self.rec.lanes = self.lanes
            threading.Thread(target=old.close, daemon=True).start()
            self._start_lanes()
        # Recorder giữ tham chiếu tới CHÍNH object cfg này nên đổi là ăn ngay; riêng
        # cookie và số suất ghi song song phải nạp lại tay.
        self.rec.cookie = CK.load(self.cfg.cookie_path)
        self.rec._rec_slots = threading.Semaphore(max(1, self.cfg.max_parallel))
        self._check_env()
        self._log("✓ đã lưu cài đặt")

    def closeEvent(self, e):
        busy = [t for t in self.snap.values() if t.state == RC.REC]
        if busy:
            names = ", ".join(t.label() for t in busy[:3])
            r = QMessageBox.question(
                self, "Đang ghi",
                f"Còn {len(busy)} phòng đang ghi ({names}…).\n"
                "Thoát bây giờ sẽ đóng file lại và dừng canh. Thoát chứ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        self.beat.stop()
        self.rec.stop_all()
        self.cfg.window = [self.width(), self.height()]
        self.cfg.watch = [r for r in self.rows]
        self.cfg.save()
        # cho ffmpeg vài giây đóng đuôi file cho tử tế
        deadline = time.time() + 6
        while time.time() < deadline and any(t._proc for t in self.rec.tasks.values()):
            QApplication.processEvents()
            time.sleep(0.1)
        # Tắt wireproxy SAU ffmpeg: tắt trước là cắt đường ngay giữa lúc ffmpeg còn
        # đang ghi nốt. close() cũng dọn thư mục chứa private key.
        try:
            self.lanes.close()
        except Exception:
            pass
        e.accept()


def launch(argv: Optional[List[str]] = None) -> int:
    app = QApplication(sys.argv[:1] + list(argv or []))
    app.setApplicationName("HGLIVE")
    install_arrows()          # cần QApplication rồi mới vẽ được PNG mũi tên
    app.setStyleSheet(QSS)
    cfg = Settings.load()
    w = MainWindow(cfg)
    w.show()
    return app.exec()
