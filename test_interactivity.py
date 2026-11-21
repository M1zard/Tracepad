#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tracepad_qt — регрессия интерактивности (fix 1/2/3).

Тестируем РЕАЛЬНУЮ доставку событий через QApplication.notify():
синтетические QMouseEvent/QWheelEvent — как их сгенерировала бы ОС.
Покрывает баги, найденные на живой Windows-машине:

  fix1: RuntimeError libshiboken (висячая QPropertyAnimation) при
        быстрых переключениях свитча;
  fix2: drag панели за шапку не работал (eventFilter с магическими
        номерами 27/28/29 вместо QEvent.Type);
  fix3: события мыши поверх QLabel-картинки оверлея не доходили
        до окна (нет WA_TransparentForMouseEvents).
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


img = Image.new("RGBA", (300, 200), (255, 255, 255, 255))
d = ImageDraw.Draw(img)
d.rectangle([10, 10, 140, 120], fill=(220, 30, 30, 255))
TEST_IMG = os.path.join(TMP, "test.png")
img.save(TEST_IMG)

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication

app = QApplication([])
from tracepad_qt import style as S
app.setStyleSheet(S.app_qss())

from tracepad_qt.state import State, _state_dir

STATE_FILE = os.path.join(_state_dir(), "state.json")
if os.path.exists(STATE_FILE):
    os.remove(STATE_FILE)


def pump(sec=0.2):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def mouse(w, etype, local, press=False, release=False):
    """Отправить QMouseEvent виджету w через QApplication.notify()."""
    glob = w.mapToGlobal(QPoint(*local))
    if etype == QEvent.MouseButtonPress:
        e = QMouseEvent(QEvent.MouseButtonPress, QPointF(*local),
                        QPointF(glob.x(), glob.y()),
                        Qt.LeftButton, Qt.LeftButton,
                        Qt.NoModifier)
    elif etype == QEvent.MouseButtonRelease:
        e = QMouseEvent(QEvent.MouseButtonRelease, QPointF(*local),
                        QPointF(glob.x(), glob.y()),
                        Qt.LeftButton, Qt.NoButton,
                        Qt.NoModifier)
    else:  # move
        buttons = Qt.LeftButton if press else Qt.NoButton
        e = QMouseEvent(QEvent.MouseMove, QPointF(*local),
                        QPointF(glob.x(), glob.y()),
                        Qt.NoButton, buttons,
                        Qt.NoModifier)
    return app.notify(w, e)


# ================================================================ fix 1
from tracepad_qt.controls import Switch, RowSwitch

sw = Switch(False)
sw.resize(38, 22)
sw.show()
pump(0.05)

# 1a. одиночный клик: state переключилось (ждём анимацию 140 мс —
#     проверяем семантику «вкл», а не мгновенный кадр)
mouse(sw, QEvent.MouseButtonPress, (19, 11))
mouse(sw, QEvent.MouseButtonRelease, (19, 11))
pump(0.3)
check("fix1: single click toggles on", sw._is_on(), sw._v)

# 1b. НЕзавершённая анимация + немедленный новый клик (было: RuntimeError)
err = []
sys.excepthook = lambda *a: err.append(a)
oldhook = sys.excepthook
mouse(sw, QEvent.MouseButtonPress, (19, 11))
mouse(sw, QEvent.MouseButtonRelease, (19, 11))
# не ждём анимацию: сразу следующий клик
mouse(sw, QEvent.MouseButtonPress, (19, 11))
mouse(sw, QEvent.MouseButtonRelease, (19, 11))
mouse(sw, QEvent.MouseButtonPress, (19, 11))
mouse(sw, QEvent.MouseButtonRelease, (19, 11))
pump(0.4)
sys.excepthook = oldhook
check("fix1: rapid clicks, no RuntimeError", not err,
      ["".join(str(x) for x in a[:2]) for a in err[:1]])

# 1c. клики мимо свитча не переключают
v_before = sw._v
mouse(sw, QEvent.MouseButtonPress, (-5, -5))
mouse(sw, QEvent.MouseButtonRelease, (-5, -5))
pump(0.05)
check("fix1: click outside ignored", sw._v == v_before, (v_before, sw._v))

# 1d. RowSwitch: клик по строке переключает свитч (важно: события
#      идут на RowSwitch, не на дочерний Switch)
from tracepad_qt.state import State as _St
st = _St({"grid": False})
row = RowSwitch("сетка", False)
row.resize(248, 36)
row.show()
pump(0.05)
mouse(row, QEvent.MouseButtonPress, (60, 18))
mouse(row, QEvent.MouseButtonRelease, (60, 18))
pump(0.3)
check("fix1: RowSwitch click toggles", row._sw._is_on())

# ================================================================ fix 2
from tracepad_qt.panel import Panel

st = State()
panel = Panel(st, {"open": None, "reset": None, "fit": None,
                   "hide": None, "quit": None}, lambda: (0, 0, 800, 800))
panel.move(50, 50)
pump(0.3)

x0, y0 = panel.x(), panel.y()
hdr = panel._header
hg = hdr.mapTo(panel, QPoint(0, 0))   # позиция шапки в координатах панели
hp = (hg.x() + 40, hg.y() + 20)      # точка в шапке

# 2a. press в шапке
ok = mouse(hdr, QEvent.MouseButtonPress, hp)
pump(0.02)
check("fix2: header press accepted", ok and panel._drag is not None,
      panel._drag)

# 2b. move: панель едет
panel_pos_before = (panel.x(), panel.y())
mouse(hdr, QEvent.MouseMove, (hp[0] + 60, hp[1] + 40), press=True)
pump(0.05)
dx = panel.x() - panel_pos_before[0]
dy = panel.y() - panel_pos_before[1]
check("fix2: header drag moves panel", dx == 60 and dy == 40, (dx, dy))

# 2c. release: позиция сохранена в state
mouse(hdr, QEvent.MouseButtonRelease, (hp[0] + 60, hp[1] + 40))
pump(0.05)
check("fix2: release persists position",
      st.get("panel_x") == panel.x() and st.get("panel_y") == panel.y(),
      (st.get("panel_x"), panel.x()))

# 2d. drag завершён
check("fix2: drag cleared", panel._drag is None)

# 2e. release-последовательность без press не двигает панель
pos = (panel.x(), panel.y())
mouse(hdr, QEvent.MouseMove, (hp[0] + 500, hp[1] + 500), press=True)
pump(0.05)
check("fix2: move without press ignored",
      (panel.x(), panel.y()) == pos)

# ================================================================ fix 3
from tracepad_qt.app import App

if os.path.exists(STATE_FILE):
    os.remove(STATE_FILE)
a = App(app, image_path=TEST_IMG)
pump(0.8)
ov = a.overlay
ov.move(100, 100)
ov.apply("final")
pump(0.2)

# 3a. оверлей получил события мыши (drag)
pos0 = (ov.x(), ov.y())
lp = (150, 100)
mouse(ov, QEvent.MouseButtonPress, lp)
pump(0.02)
mouse(ov, QEvent.MouseMove, (lp[0] + 50, lp[1] + 30), press=True)
pump(0.1)
ov.apply("final")
dx = ov.x() - pos0[0]
dy = ov.y() - pos0[1]
check("fix3: overlay drag works", (dx, dy) == (50, 30), (dx, dy))
mouse(ov, QEvent.MouseButtonRelease, (lp[0] + 50, lp[1] + 30))
pump(0.05)

# 3b. клик по QLabel не съедает события: проверяем атрибут
check("fix3: QLabel mouse-transparent",
      bool(ov._lbl.testAttribute(Qt.WA_TransparentForMouseEvents)))

# 3c. double click -> reset view (on_reset вызван).
#     Подменяем ov.on_reset (а не App.reset_view): overlay держит
#     bound method из конструктора — подмена атрибута App его не видит.
called = []
ov.on_reset = lambda: called.append(1)
from PySide6.QtGui import QMouseEvent as QME
glob = ov.mapToGlobal(QPoint(*lp))
# полная последовательность dblclick по Qt-семантике:
for et, btn in ((QEvent.MouseButtonPress, Qt.LeftButton),
                (QEvent.MouseButtonRelease, Qt.NoButton),
                (QEvent.MouseButtonDblClick, Qt.LeftButton),
                (QEvent.MouseButtonRelease, Qt.NoButton)):
    e = QME(et, QPointF(*lp), QPointF(glob.x(), glob.y()),
            Qt.LeftButton, btn, Qt.NoModifier)
    app.notify(ov, e)
pump(0.05)
check("fix3: dblclick reaches overlay (reset called)", len(called) > 0)

a.quit()
pump(0.2)

fails = [r for r in RESULTS if not r[1]]
print("\n==== %d/%d passed ====" % (len(RESULTS) - len(fails), len(RESULTS)))
for name, ok, extra in fails:
    print("FAILED:", name, extra)
sys.exit(1 if fails else 0)
