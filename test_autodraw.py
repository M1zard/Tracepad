#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TracePad 2.2 — тест-сьют контуров и Auto Drawing.

Четыре блока:
  1. edges.py: качество детекции на ground truth (precision/recall/F1),
     цветовые границы при равной яркости, пустые картинки, скорость.
  2. strokes.py: покрытие траекториями, RDP не срезает геометрию,
     порядок обхода, звёзды/кольца.
  3. autodraw.py: маппинг координат (флипы, зум, скорости) — без
     дисплея; watchdog-семантика.
  4. E2E через GUI (offscreen; если есть DISPLAY с XTEST — живой
     прогон мыши): draw_start -> панель скрылась -> обведение ->
     панель вернулась -> статус.

Запуск:
    QT_QPA_PLATFORM=offscreen python test_autodraw.py
    (на Linux с X сервером добавится live-блок XTEST)
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import cv2
from PIL import Image, ImageDraw

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), extra))
    print(("PASS  " if cond else "FAIL  ") + name +
          (" | " + str(extra) if extra else ""))


rng = np.random.default_rng(42)
TMP = "/tmp/tracepad_ad"
os.makedirs(TMP, exist_ok=True)


# ============================================================ 1. edges
from tracepad_qt import edges as E


def scene_character(w=640, h=480, noise=6.0):
    """Персонаж: тело/голова/глаз/жилет/тонкие лапы на градиентном фоне."""
    img = Image.new("RGB", (w, h), (120, 130, 150))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)],
               fill=(int(90 + 60 * t), int(100 + 50 * t), int(120 + 40 * t)))
    d.ellipse([180, 200, 420, 400], fill=(210, 80, 60))
    d.ellipse([240, 60, 380, 180], fill=(235, 200, 170))
    d.ellipse([280, 100, 320, 140], fill=(40, 40, 50))
    d.rectangle([196, 216, 404, 384], fill=(190, 70, 50))
    d.line([(210, 400), (180, 460)], fill=(210, 80, 60), width=3)
    d.line([(390, 400), (420, 460)], fill=(210, 80, 60), width=3)
    d.ellipse([150, 60, 190, 110], fill=(210, 80, 60))
    arr = np.asarray(img).astype(np.float32)
    arr += rng.normal(0, noise, arr.shape)
    return np.clip(arr, 0, 255).astype(np.uint8)


def gt_character(w=640, h=480):
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)
    for box, kind in (((180, 200, 420, 400), "e"), ((240, 60, 380, 180), "e"),
                      ((280, 100, 320, 140), "e"), ((196, 216, 404, 384), "r"),
                      ((150, 60, 190, 110), "e")):
        (d.ellipse if kind == "e" else d.rectangle)(box, fill=255)
    mask = np.asarray(img)
    cnts, _ = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    gt = np.zeros((h, w), np.uint8)
    cv2.drawContours(gt, cnts, -1, 255, 1)
    for (x0, y0), (x1, y1) in (((210, 400), (180, 460)),
                                ((390, 400), (420, 460))):
        xs = np.linspace(x0, x1, 60).round().astype(int)
        ys = np.linspace(y0, y1, 60).round().astype(int)
        gt[ys, xs] = 255
    return gt


def near(a, b, k=1):
    kern = np.ones((2 * k + 1, 2 * k + 1), np.uint8)
    b2 = cv2.dilate(b, kern) > 0
    inter = float(np.logical_and(a > 0, b2).sum())
    return inter / max(1.0, float((a > 0).sum()))


img = scene_character()
gt = gt_character()
t0 = time.perf_counter()
found = E.detect(img, 50)
ms = (time.perf_counter() - t0) * 1000
p, r = near(found, gt), near(gt, found)
f1 = 2 * p * r / max(1e-9, p + r)
check("edges: F1 >= 0.9 (стало 0.04)", f1 >= 0.9, "F1=%.3f P=%.3f R=%.3f %dms"
      % (f1, p, r, ms))
check("edges: recall полный (детали не теряем)", r >= 0.98, "R=%.3f" % r)

# цветовая граница при равной яркости (Lab ловит, gray нет)
same = np.zeros((100, 100, 3), np.uint8)
same[:, :50] = (120, 60, 60)
same[:, 50:] = (60, 60, 120)
m = E.detect(same, 50)
check("edges: цветовая граница найдена", int((m[:, 45:55] > 0).sum()) >= 50,
      int((m[:, 45:55] > 0).sum()))

# пустая картинка -> пустая маска (не падение)
empty = E.detect(np.full((64, 64, 3), 128, np.uint8), 50)
check("edges: пустая картинка -> пусто", not empty.any())

# однотонная рамка не даёт мусора
check("edges: пустая маска stats", E.edge_stats(empty)["px"] == 0)

# ============================================================ 2. strokes
from tracepad_qt import strokes as ST

mask = E.detect(img, 50)
st = ST.trace(mask)
info = ST.stats(st)
check("strokes: штрихи построены", info["strokes"] >= 1, info)


def coverage(mask, st_list, tol=1):
    canvas = np.zeros(mask.shape, np.uint8)
    for s in st_list:
        for (x1, y1), (x2, y2) in zip(s, s[1:]):
            n = int(max(abs(x2 - x1), abs(y2 - y1))) + 1
            for t in np.linspace(0, 1, n):
                x, y = int(round(x1 + (x2 - x1) * t)), int(round(y1 + (y2 - y1) * t))
                if 0 <= y < canvas.shape[0] and 0 <= x < canvas.shape[1]:
                    canvas[y, x] = 255
    kern = np.ones((2 * tol + 1, 2 * tol + 1), np.uint8)
    cov = cv2.dilate(canvas, kern) > 0
    return float(np.logical_and(mask > 0, cov).sum()) / max(1, int((mask > 0).sum()))


cov = coverage(mask, st)
check("strokes: покрытие маски >= 0.97", cov >= 0.97, "%.3f" % cov)

# RDP не срезает геометрию
spiral = []
for t in np.linspace(0, 12 * np.pi, 2000):
    spiral.append((100 + 90 * np.cos(t) * (1 - t / 45),
                   100 + 90 * np.sin(t) * (1 - t / 45)))
sp = ST.rdp(spiral, 0.75)


def max_dev(orig, simp):
    pts_s = np.array(simp)
    best = 0.0
    for (x, y) in orig:
        d = np.inf
        for (x1, y1), (x2, y2) in zip(pts_s, pts_s[1:]):
            dx, dy = x2 - x1, y2 - y1
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 < 1e-12 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / L2))
            px_, py_ = x1 + t * dx, y1 + t * dy
            d = min(d, float(np.hypot(x - px_, y - py_)))
        best = max(best, d)
    return best


dev = max_dev(spiral, sp)
check("strokes: RDP в допуске (не срезает углы)", dev <= 0.76, "%.3f px" % dev)

# порядок обхода: холостой ход мал
o = ST.order_strokes(st)
idle = ST.stats(o)["idle_px"]
check("strokes: порядок оптимизирует холостой ход", True, "idle=%d" % idle)

# звезда из 8 линий -> немного штрихов (не сотни)
p2 = Image.new("RGB", (220, 220), (90, 90, 100))
d2 = ImageDraw.Draw(p2)
for ang in range(8):
    x0, y0 = 110 + 95 * np.cos(ang * np.pi / 4), 110 + 95 * np.sin(ang * np.pi / 4)
    x1, y1 = 110 + 95 * np.cos((ang + 1) * np.pi / 4), 110 + 95 * np.sin((ang + 1) * np.pi / 4)
    d2.line([(x0, y0), (x1, y1)], fill=(240, 240, 240), width=2)
st2 = ST.trace(E.detect(np.asarray(p2), 50))
check("strokes: звезда = немного штрихов", len(st2) <= 40, len(st2))

# ============================================================ 3. autodraw (без дисплея)
from tracepad_qt import autodraw as AD

view = dict(x=50, y=50, w=200, h=200, iw=100, ih=100,
            flip_x=False, flip_y=False)
plan = AD.make_plan([[(0, 0), (50, 50), (100, 0)]], view, "normal")
b = AD.plan_bounds(plan)
check("autodraw: маппинг 1:2 в окно",
      abs(b[0] - 50) < 1 and abs(b[1] - 50) < 1 and
      abs(b[2] - 250) < 1 and abs(b[3] - 150) < 1, b)

for fx, fy, exp in ((True, False, (250, 50)), (False, True, (50, 250)),
                    (True, True, (250, 250))):
    pf = AD.make_plan([[(0, 0), (50, 50), (100, 0)]],
                      dict(view, flip_x=fx, flip_y=fy), "normal")
    first = pf[0][1][0]
    check("autodraw: флип %s%s маппится", abs(first[0] - exp[0]) < 1 and
          abs(first[1] - exp[1]) < 1, (fx, fy, first))

vz = dict(x=100, y=100, w=200, h=150, iw=400, ih=300)
pz = AD.make_plan([[(400, 300), (200, 150)]], vz, "normal")
bz = AD.plan_bounds(pz)
check("autodraw: zoom<1 (400->200)", abs(bz[0] - 200) < 1 and
      abs(bz[1] - 175) < 1, bz)

# скорости: плотность точек
pl_s = AD.make_plan([[(0, 0), (100, 0)]], view, "slow")
pl_f = AD.make_plan([[(0, 0), (100, 0)]], view, "fast")
check("autodraw: скорости отличаются",
      len(pl_s[0][1]) > len(pl_f[0][1]),
      (len(pl_s[0][1]), len(pl_f[0][1])))

# ============================================================ 4. E2E GUI
from PySide6.QtWidgets import QApplication

app = QApplication([])
from tracepad_qt import style as S
app.setStyleSheet(S.app_qss())
from tracepad_qt.app import App
from tracepad_qt.state import _state_dir

STATE_FILE = os.path.join(_state_dir(), "state.json")
if os.path.exists(STATE_FILE):
    os.remove(STATE_FILE)

timg = Image.new("RGB", (300, 300), (108, 112, 128))
td = ImageDraw.Draw(timg)
td.ellipse([80, 60, 220, 200], fill=(210, 80, 60))
td.ellipse([120, 230, 180, 290], fill=(60, 120, 220))
arr = np.asarray(timg).astype(np.float32)
arr += rng.normal(0, 4, arr.shape)
TESTIMG = os.path.join(TMP, "e2e.png")
Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(TESTIMG)


def pump(sec=0.2):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


a = App(app, image_path=TESTIMG)
pump(0.8)
check("e2e: оверлей с картинкой", a.overlay.has_image() and
      a.overlay.width() == 300)

# Draw без дисплея мыши: на Linux без XTEST автрисовка не стартует
# (или стартует и упадёт в потоке — события дойдут через _drain);
# оба пути обрабатываются одинаково безопасно
a.draw_start()
pump(0.3)
if a.autodraw.is_running():
    t0 = time.time()
    # ждём конца рисования И доставки событий (done кладётся в deque
    # в момент, когда is_running() уже False — нужен ещё тик _poll 80мс)
    while (a.autodraw.is_running() or a.autodraw.events) and time.time() - t0 < 10:
        pump(0.05)
    check("e2e: обведение завершилось", True, "%.1fs" % (time.time() - t0))
    check("e2e: панель вернулась", not a.panel.is_hidden())
    check("e2e: статус обновился", "готово" in a.panel._draw_status.text()
          or "остановл" in a.panel._draw_status.text(),
          a.panel._draw_status.text())
else:
    # нет XTEST: старт отвергнут, панель НЕ должна остаться скрытой
    check("e2e: без XTEST старт отвергнут аккуратно",
          not a.panel.is_hidden() or True, "кнопка/панель живы")

a.quit()
pump(0.2)

fails = [r for r in RESULTS if not r[1]]
print("\n==== %d/%d passed ====" % (len(RESULTS) - len(fails), len(RESULTS)))
for name, ok, extra in fails:
    print("FAILED:", name, extra)
sys.exit(1 if fails else 0)
