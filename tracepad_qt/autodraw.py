# -*- coding: utf-8 -*-
"""autodraw.py — автоматическое обведение контуров мышью (Auto Drawing).

Сценарий: пользователь открыл редактор ПОД референсом, выставил чёрную
кисть, нажимает Draw — приложение проводит курсор по траекториям из
strokes.py, зажимая кнопку мыши; редактор рисует линии. Только обводка:
без заливки, смены цвета/кисти, каких-либо кликов по UI.

Ключевые решения:

1) КООРДИНАТЫ. Штрихи живут в пикселях ИСХОДНОЙ картинки; окно
   оверлея стоит в (x, y) с размером extent(zoom). Маппинг:
       screen = (img * zoom * mirror) + (x, y)
   Флипы входят в маппинг (курсор должен обвести то, что ВИДИТ
   пользователь). Смещение на полпикселя кисти не делаем: линии 1px,
   центр пикселя = вершина траектории.

2) ПОТОК. SendInput/XTEST — блокирующие системные вызовы, им нужен
   отдельный поток; GUI-поток не должен фризиться. Управление — через
   threading.Event (стоп), обратная связь — callback-очередь в deque,
   которую GUI опрашивает уже существующим 80-мс таймером (новых
   таймеров не заводим). Аварийная остановка — из любого места через
   stop(); поток поднимает кнопку мыши и завершается.

3) ПЛАВНОСТЬ. Скорость регулируется шагом дискретизации точек вдоль
   штриха: interp_step px между событиями (speed=fast -> 6px,
   precise -> 2px). События шлются с dt=1..4 мс (SendInput не
   коалесцирует, XTEST обрабатывает мгновенно — на живых системах
   редактор успевает отрисовать). Никаких кривых Безье — геометрия
   траекторий уже точная, интерполяция только по длине.

4) БЕЗОПАСНОСТЬ:
   * «Паника» — любое движение реальной мыши пользователем (детект
     по GetCursorPos/query_pointer между событиями) немедленно
     останавливает рисование: пользователь всегда сильнее автоматики.
   * Клик строго внутри bbox траекторий (никогда не UI чужого окна).
   * Ошибка любого вызова -> stop(), кнопка мыши отпускается.
   * Windows: SendInput с MOUSEEVENTF_ABSOLUTE в 0..65535 с
     физическим экраном (GetSystemMetrics) — DPI-осведованно, т.к.
     процесс с SetProcessDpiAwareness(2) (winutil уже делает).
   * Linux: XTEST fake input в координатах root-окна (физических
     пикселях X-сессии).
   * macOS: не поддерживается (как и клик-сквозь), честный отказ.

5) Синхронизация с click_through: автрисовка НЕ меняет режим
   оверлея. Если click_through включён — клики и так идут в окно
   под ним; если выключен — рисовать всё равно можно (курсор
   проходит сквозь ОС-события независимо от WS_EX_TRANSPARENT:
   SendInput шлёт события глобально, перехватывает их окно под
   курсором, а оверлей с click-through просто не участвует).
   Для гарантии того, что под курсором редактор, а не панель:
   во время Draw панель прячем (и восстанавливаем после).
"""

from __future__ import annotations

import collections
import sys
import threading
import time

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = not IS_WIN and not IS_MAC

# шаг интерполяции вдоль штриха (px) по скорости
_SPEED_STEP = {"slow": 2.0, "normal": 4.0, "fast": 7.0}
# пауза между событиями мыши (сек) по скорости
_SPEED_DT = {"slow": 0.004, "normal": 0.0025, "fast": 0.0015}


# ============================================================================
#  Бэкенд мыши (платформенная часть)
# ============================================================================
if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    SM_CXSCREEN, SM_CYSCREEN = 0, 1
    MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
    MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("mi", _MOUSEINPUT)]
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    def _screen_size():
        return (int(_user32.GetSystemMetrics(SM_CXSCREEN)),
                int(_user32.GetSystemMetrics(SM_CYSCREEN)))

    def _cursor_pos():
        pt = wintypes.POINT()
        if _user32.GetCursorPos(ctypes.byref(pt)):
            return (int(pt.x), int(pt.y))
        return None

    def _send(dx, dy, flags):
        inp = _INPUT(type=0)
        inp.u.mi.dx = int(dx)
        inp.u.mi.dy = int(dy)
        inp.u.mi.dwFlags = flags
        inp.u.mi.time = 0
        inp.u.mi.dwExtraInfo = None
        if _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)) != 1:
            raise OSError("SendInput failed")

    def mouse_move(x, y):
        """Абсолютное перемещение в физических координатах экрана."""
        sw, sh = _screen_size()
        # 0..65535 нормализация (согласовано с Mickey-документацией:
        # координата центра пикселя (i+0.5)/w*65536)
        nx = int(round((x + 0.5) / sw * 65535))
        ny = int(round((y + 0.5) / sh * 65535))
        _send(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)

    def mouse_down():
        _send(0, 0, MOUSEEVENTF_LEFTDOWN)

    def mouse_up():
        _send(0, 0, MOUSEEVENTF_LEFTUP)

    def supported():
        return True, ""

elif IS_LINUX:
    try:
        from Xlib import X, display as _xdisplay
        from Xlib.ext import xtest
        from Xlib.error import XError

        _D = None

        def _disp():
            global _D
            if _D is None:
                _D = _xdisplay.Display()
            return _D

        def _root_size():
            d = _disp()
            s = d.screen()
            return (int(s.width_in_pixels), int(s.height_in_pixels))

        def _cursor_pos():
            d = _disp()
            qp = d.screen().root.query_pointer()
            return (int(qp.root_x), int(qp.root_y))

        def mouse_move(x, y):
            d = _disp()
            d.xtest_fake_input(X.MotionNotify, x=int(x), y=int(y))
            d.flush()

        def mouse_down():
            d = _disp()
            d.xtest_fake_input(X.ButtonPress, detail=1)
            d.flush()

        def mouse_up():
            d = _disp()
            d.xtest_fake_input(X.ButtonRelease, detail=1)
            d.flush()

        def supported():
            try:
                _disp().xtest_fake_input  # noqa: B018
                return True, ""
            except Exception:
                pass
            return False, "XTEST недоступен на этом X-сервере"

    except ImportError:
        def supported():
            return False, "поставь python-xlib (pip install python-xlib)"

else:
    def supported():
        return False, "на macOS авто-рисование этой версией не поддерживается"


# ============================================================================
#  План рисования (чистые функции — тестируются без дисплея)
# ============================================================================
def make_plan(strokes, view, speed="normal"):
    """Штрихи + вид окна -> исполнимый план (список ходов).

    view: dict(x, y, w, h, flip_x, flip_y) — геометрия оверлея НА
    экране (w/h уже с учётом zoom). Координаты штрихов (x, y) —
    пиксели ИСХОДНОЙ картинки.

    Ход (move): ('pen_up' | 'pen_down' | 'move', sx, sy) — экранные
    координаты. pen_up между штрихами, pen_down в начале штриха,
    move вдоль штриха с шагом _SPEED_STEP по длине.
    """
    step = _SPEED_STEP.get(speed, _SPEED_STEP["normal"])
    vx, vy = float(view["x"]), float(view["y"])
    vw, vh = float(view["w"]), float(view["h"])
    iw, ih = float(view["iw"]), float(view["ih"])
    fx = bool(view.get("flip_x"))
    fy = bool(view.get("flip_y"))

    def to_screen(px, py):
        # нормализуем в [0..1] по картинке, зеркалим, растягиваем в окно
        nx = px / iw
        ny = py / ih
        if fx:
            nx = 1.0 - nx
        if fy:
            ny = 1.0 - ny
        return (vx + nx * vw, vy + ny * vh)

    plan = []
    for s in strokes:
        if len(s) < 2:
            continue
        pts = [to_screen(px, py) for px, py in s]
        # плотная интерполяция вдоль сегментов: равные шаги step px
        dense = [pts[0]]
        acc = 0.0
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            seg = float((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            if seg <= 1e-9:
                continue
            n = max(1, int(round(seg / step)))
            for k in range(1, n + 1):
                t = k / n
                dense.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
            acc += seg
        plan.append(("stroke", dense))
    return plan


def plan_bounds(plan):
    """Экранный bbox плана (для проверки «не выйти за пределы»)."""
    xs, ys = [], []
    for _, dense in plan:
        for x, y in dense:
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


# ============================================================================
#  Исполнитель (поток)
# ============================================================================
class AutoDraw:
    """Менеджер потока рисования. GUI создаёт, зовёт start()/stop().

    events: deque для обратной связи (GUI опрашивает своим таймером):
        ('progress', i, n)     — штрих i из n
        ('done', finished, total)
        ('error', msg)
    """

    def __init__(self, on_event=None):
        self.on_event = on_event or (lambda *a: None)
        self.events = collections.deque()
        self._thread = None
        self._stop = threading.Event()
        self._abort_reason = None

    # ---------------------------------------------------------------- API
    def start(self, plan, speed="normal", area=None):
        """Запустить обведение. area=(x, y, w, h) — экранный прямо-
        угольник, ВНУТРИ которого разрешено рисовать (bbox оверлея);
        None = без ограничения (не рекомендуется)."""
        if self.is_running():
            return False
        ok, msg = supported()
        if not ok:
            self.events.append(("error", msg))
            return False
        if not plan:
            self.events.append(("error", "нет траекторий"))
            return False
        self._stop.clear()
        self._abort_reason = None
        self._thread = threading.Thread(
            target=self._run, args=(plan, speed, area), daemon=True)
        self._thread.start()
        return True

    def stop(self):
        """Аварийная остановка (поток поднимет кнопку и выйдет)."""
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=1.0)

    def is_running(self):
        t = self._thread
        return bool(t and t.is_alive() and not self._stop.is_set())

    # ------------------------------------------------------------- внутри
    def _emit(self, kind, *args):
        self.events.append((kind,) + args)
        try:
            self.on_event(kind, *args)
        except Exception:
            pass

    def _run(self, plan, speed, area):
        dt = _SPEED_DT.get(speed, _SPEED_DT["normal"])
        down = False
        n = len(plan)
        try:
            for i, (_, dense) in enumerate(plan):
                if self._stop.is_set():
                    break
                # выход за разрешённую зону -> стоп (не рисуем мимо)
                if area is not None:
                    b = plan_bounds([(_, dense)])
                    if b and (b[0] < area[0] or b[1] < area[1] or
                              b[2] > area[0] + area[2] or
                              b[3] > area[1] + area[3]):
                        self._abort_reason = "выход за границы"
                        break
                # перо вверх -> к началу штриха -> перо вниз
                if down:
                    mouse_up()
                    down = False
                x0, y0 = dense[0]
                mouse_move(int(round(x0)), int(round(y0)))
                self._watchdog(x0, y0)      # проверяем ПОСЛЕ хода
                time.sleep(dt * 2)
                mouse_down()
                down = True
                self._emit("progress", i, n)
                for x, y in dense[1:]:
                    if self._stop.is_set():
                        break
                    mouse_move(int(round(x)), int(round(y)))
                    self._watchdog(x, y)    # позиция должна совпасть
                    time.sleep(dt)
                if self._stop.is_set():
                    break
            if down:
                mouse_up()
        except Exception as e:
            self._abort_reason = str(e)
            try:
                mouse_up()
            except Exception:
                pass
            self._emit("error", str(e))
            return
        finished = self._stop.is_set() or self._abort_reason is not None
        self._emit("done", finished, n)

    def _watchdog(self, x, y):
        """Паника: пользователь дёрнул реальную мышь — останавливаемся.

        Вызывается ПОСЛЕ mouse_move(x, y): наш ход сам ставит курсор
        точно в (x, y); если позиция НЕ совпала — между нашим
        событием и чтением позиции курсор двигал кто-то ещё, то есть
        пользователь. Порог PANIC_PX покрывает округление и дрожь
        (проверено на XTEST: без вмешательства совпадение точное).
        """
        cur = _cursor_pos()
        if cur and (abs(cur[0] - x) > PANIC_PX or
                    abs(cur[1] - y) > PANIC_PY):
            self._abort_reason = "пользователь перехватил мышь"
            self._stop.set()


PANIC_PX = 24     # горизонтальный порог «пользователь вмешался»
PANIC_PY = 24     # вертикальный
