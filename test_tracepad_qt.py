#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TracePad (Qt) — тест-сьют: юнит + GUI на offscreen-платформе Qt.

Особенности GUI-тестов на offscreen:
  * windowOpacity не поддерживается плагином — проверяем через
    property-based assert (no crash) и через состояние state;
  * реальные paint-проверки делаем через QWidget.grab() (offscreen
    рендерит в изображение).
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw

TMP = "/tmp/tracepad_test"
os.makedirs(TMP, exist_ok=True)

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), extra))
    print(("PASS  " if cond else "FAIL  ") + name +
          (" | " + str(extra) if extra else ""))


# ============================================================== test images
img = Image.new("RGBA", (300, 200), (255, 255, 255, 255))
d = ImageDraw.Draw(img)
d.rectangle([10, 10, 140, 120], fill=(220, 30, 30, 255))
d.ellipse([160, 40, 280, 160], fill=(30, 60, 220, 255))
TEST_IMG = os.path.join(TMP, "test.png")
img.save(TEST_IMG)

big = Image.new("RGBA", (3900, 2900), (245, 238, 225, 255))
db = ImageDraw.Draw(big)
db.rectangle([100, 100, 3800, 2800], outline=(60, 60, 90, 255), width=12)
db.ellipse([500, 500, 1300, 1300], fill=(220, 40, 40, 255))
BIG_IMG = os.path.join(TMP, "big.png")
big.save(BIG_IMG)

# ============================================================== UNIT
from tracepad_qt import geometry as G

z, x, y = G.fit_rect(300, 200, (0, 0, 1600, 900))
check("unit: small keeps 1:1", z == 1.0 and (x, y) == (650, 350), (z, x, y))
z, x, y = G.fit_rect(3900, 2900, (0, 0, 1600, 900))
w, h = 3900 * z, 2900 * z
check("unit: big fits", w <= 1600 * 0.94 + 1 and h <= 900 * 0.94 + 1, (z,))
check("unit: big centered", abs((x + w / 2) - 800) < 2 and abs((y + h / 2) - 450) < 2)
check("unit: clamp right", G.clamp_into(1500, 100, 300, 200, (0, 0, 1600, 900)) == (1300, 100))
cx, cy = G.clamp_into(0, 0, 2000, 1800, (0, 0, 1600, 900))
check("unit: oversize centers", cx == -200 and cy == -450)

from tracepad_qt.renderer import RenderPipeline, make_grid

rp = RenderPipeline()
rp.set_image(img)
check("render: 1:1", rp.render(1.0).size == (300, 200))
check("render: 2x", rp.render(2.0).size == (600, 400))
rp.render(1.0, bw=True)
c0 = rp._cache
rp.render(1.5, bw=True)
check("render: cache reused", rp._cache is c0)
rp.render(1.0, bw=True, edge=True)
check("render: cache invalidated", rp._cache is not c0)
im = rp.render(1.0, edge=True, edge_strength=40)
a_min, a_max = im.getchannel("A").getextrema()
check("render: edge alpha", a_min == 0 and a_max == 255, (a_min, a_max))
img_grid = rp.render(1.0, grid=True, grid_size=48)
# сетка: непрозрачные пиксели линии на x=48
px = img_grid.getpixel((48, 20))
check("render: grid drawn", px[3] > 0, px)
# быстрый vs финальный
t0 = time.perf_counter()
for _ in range(5):
    rp.render(0.6, quality="fast")
t_fast = (time.perf_counter() - t0) / 5
t0 = time.perf_counter()
rp.render(0.6, quality="final")
t_final = time.perf_counter() - t0
check("render: fast cheaper", t_fast < t_final, (round(t_fast, 3), round(t_final, 3)))

from tracepad_qt.state import State, _state_dir

st = State({"opacity": 0.5})
calls = []
tok = st.subscribe("opacity", lambda v, e: calls.append((v, e)))
check("state: initial sync", calls == [(0.5, False)])
st.set("opacity", 0.5)
check("state: no dup notify", calls == [(0.5, False)])
st.set("opacity", 0.8)
check("state: explicit notify", calls[-1] == (0.8, True))
st.unsubscribe(tok)
st.set("opacity", 0.1)
check("state: unsubscribed", len(calls) == 2)

# ============================================================== GUI
from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

app = QApplication([])
from tracepad_qt import style as S
app.setStyleSheet(S.app_qss())

STATE_FILE = os.path.join(_state_dir(), "state.json")
if os.path.exists(STATE_FILE):
    os.remove(STATE_FILE)

from tracepad_qt.app import App
from tracepad_qt.overlay import OverlayWindow, pil_to_qpil


def pump(sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


# ---- 1. запуск: вписан и центрирован
a = App(app, image_path=TEST_IMG)
pump(0.8)
ov = a.overlay
check("gui: starts 1:1 centered",
      (ov.width(), ov.height()) == (300, 200)
      and abs((ov.x() + 150) - 400) < 3 and abs((ov.y() + 100) - 400) < 3,
      (ov.x(), ov.y(), ov.width(), ov.height()))
check("gui: overlay visible", ov.isVisible())

# ---- 2. прозрачность окна: state -> windowOpacity (на offscreen property
#         поддерживается частично; главное — вызов не падает и логика верна)
a.state.set("opacity", 0.4)
pump(0.1)
check("gui: opacity state applied", abs(float(a.state.get("opacity")) - 0.4) < 1e-6)
try:
    op = ov.windowOpacity()  # может вернуть 1.0 на offscreen
    check("gui: windowOpacity callable", True, op)
except Exception as e:
    check("gui: windowOpacity callable", False, e)

# ---- 3. компоновка панели: НЕТ наложений (глобальные прямоугольники,
#         исключая пары предок-потомок: вложение — не наложение)
panel = a.panel
pump(0.2)

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QWidget as _QW


def overlaps_of(w):
    """Пары ВИДИМЫХ потомков с пересекающимися ГЛОБАЛЬНЫМИ прямоугольниками."""
    ch = [c for c in w.findChildren(_QW)
          if c.isVisible() and c.width() > 1 and c.height() > 1]
    bad = []
    for i in range(len(ch)):
        for j in range(i + 1, len(ch)):
            A, B = ch[i], ch[j]
            if A.isAncestorOf(B) or B.isAncestorOf(A):
                continue
            r1 = QRect(A.mapToGlobal(A.rect().topLeft()), A.size())
            r2 = QRect(B.mapToGlobal(B.rect().topLeft()), B.size())
            if r1.intersects(r2):
                bad.append((type(A).__name__, type(B).__name__,
                            r1.getRect(), r2.getRect()))
    return bad


bad = overlaps_of(panel)
check("gui: no overlapping widgets in panel", len(bad) == 0, bad[:4])
check("gui: panel fixed width", panel.width() == S.PANEL_W, panel.width())
h = panel.height()
check("gui: panel sane height", 300 < h < 1000, h)

# ---- 4. свёртка панели
panel.toggle_collapse()
pump(0.5)
check("gui: collapse shrinks", panel.height() < h - 100, (h, panel.height()))
panel.toggle_collapse()
pump(0.5)
check("gui: expand restores", abs(panel.height() - h) < 60, (h, panel.height()))

# ---- 5. под-настройки появляются при включении режима
a.state.set("edge", True)
pump(0.6)
check("gui: edge subrow revealed", a.panel._col_edge.isVisible())
a.state.set("edge", False)
pump(0.6)
check("gui: edge subrow hidden", not a.panel._col_edge.isVisible())

# ---- 6. зум к курсору
a.state.set("zoom", 1.0)
pump(0.3)
x0, y0 = ov.x(), ov.y()
ov.zoom_at(1, 150, 100)
pump(0.4)
check("gui: zoom in", float(a.state.get("zoom")) > 1.1, a.state.get("zoom"))

# ---- 7. drag: эмуляция мыши через QMouseEvent — используем прямое
#        программное перемещение (state.set x/y)
a.state.set("x", 200)
a.state.set("y", 200)
ov.apply("final")
pump(0.2)
check("gui: move applies", ov.x() == 200 and ov.y() == 200, (ov.x(), ov.y()))

# ---- 8. кламп при попытке уехать
ov.move_to(5000, 5000)
pump(0.3)
check("gui: clamped into screen", 0 <= ov.x() and ov.x() + ov.width() <= 800
      and 0 <= ov.y() and ov.y() + ov.height() <= 800, (ov.x(), ov.y()))

# ---- 9. большая картинка: fit + центр + размер
a.load_image(BIG_IMG)
pump(0.8)
check("gui: big fits screen",
      0 <= ov.x() and ov.x() + ov.width() <= 800
      and 0 <= ov.y() and ov.y() + ov.height() <= 800,
      (ov.x(), ov.y(), ov.width(), ov.height()))
check("gui: big centered", abs((ov.x() + ov.width() / 2) - 400) < 4
      and abs((ov.y() + ov.height() / 2) - 400) < 4,
      (ov.x(), ov.y(), ov.width(), ov.height()))
check("gui: big zoom < 1", float(a.state.get("zoom")) < 1.0,
      a.state.get("zoom"))

# ---- 10. сетка/флипы/чб/контуры через state (без падений)
for k, v in (("grid", True), ("flip_x", True), ("flip_y", True),
             ("bw", True), ("edge", True)):
    a.state.set(k, v)
    pump(0.15)
    ov.apply("final")
    pump(0.05)
    im = ov.pipeline.render(zoom=float(a.state.get("zoom")), grid=True,
                            flip_x=True, flip_y=True, bw=True, edge=True,
                            edge_strength=60)
    check("gui: mode %s renders" % k, im is not None and im.size[0] > 0)
    a.state.set(k, False)
    pump(0.05)

# ---- 11. кадр в QImage без падения (для QLabel)
qim = pil_to_qpil(ov.pipeline.render(1.0))
check("gui: PIL->QImage", qim.width() == ov.size_px()[0])

# ---- 12. перезапуск: сохранение и восстановление
a.quit()
pump(0.2)

a2 = App(app)
pump(0.8)
check("gui: restore image", os.path.basename(a2.state.get("image")) == "big.png",
      a2.state.get("image"))
ov2 = a2.overlay
check("gui: restored centered", abs((ov2.x() + ov2.width() / 2) - 400) < 4
      and abs((ov2.y() + ov2.height() / 2) - 400) < 4,
      (ov2.x(), ov2.y(), ov2.width(), ov2.height()))
# выключатели в панели синхронизированы со state (нет рассинхрона)
check("gui: panel syncs state",
      a2.panel._rows["grid"]._sw._is_on() == bool(a2.state.get("grid")))

# ---- 13. горячие клавиши: actions map корректна (12 с Auto Drawing)
from tracepad_qt import hotkeys as HK
check("hotkeys: actions non-empty", len(HK.ACTIONS) == 12, len(HK.ACTIONS))
a2.hotkey_action("grid")
check("hotkeys: action toggles grid", a2.state.get("grid") is True
      or a2.state.get("grid") == True)
a2.hotkey_action("grid")
check("hotkeys: toggles back", not bool(a2.state.get("grid")))

# ---- 14. DPI: рендер честно умножает пиксели (DPR имитируем зумом)
iw, ih = a2.overlay.size_px()
z = float(a2.state.get("zoom"))
w, h = a2.overlay.extent()
check("dpi: extent consistent", abs(w - iw * z) < 2 and abs(h - ih * z) < 2,
      (w, iw * z))

# ---- 15. производительность простоя: таймеры тикают, но циклов CPU нет
# (проверяем программно: poll 80мс и topmost 4000 — не чаще)
check("perf: poll interval sane", 50 <= a2._poll.interval() <= 200,
      a2._poll.interval())
check("perf: topmost interval sane", a2._top_timer.interval() >= 2000,
      a2._top_timer.interval())

a2.quit()
pump(0.2)

fails = [r for r in RESULTS if not r[1]]
print("\n==== %d/%d passed ====" % (len(RESULTS) - len(fails), len(RESULTS)))
for name, ok, extra in fails:
    print("FAILED:", name, extra)
sys.exit(1 if fails else 0)
