# -*- coding: utf-8 -*-
"""app.py — TracePad-приложение (оркестрация на Qt).

Связывает state (чистая логика) с окнами (overlay, panel) через
подписки state: GUI ничего не хранит, логика не знает про Qt.

Обязанности:
  * запуск/остановка хоткеев (hotkeys.py) + периодический опрос deque;
  * вписывание в экран при загрузке (geometry.fit_rect);
  * keep-topmost таймер (4 c) — дешёвый, без перерисовок;
  * сохранение с debounce 400 мс;
  * контекстное меню оверлея.
"""

from __future__ import annotations

import collections
import os
import sys

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu

from . import geometry as G
from . import hotkeys as HK
from . import style as S, winutil
from .state import State
from .overlay import OverlayWindow
from .panel import Panel
from . import strokes as ST
from . import autodraw as AD


class App:
    def __init__(self, qapp, image_path=None):
        self.qapp = qapp
        self.state = State().load()
        if image_path and os.path.isfile(image_path):
            self.state.set("image", image_path)

        self.hk_queue = collections.deque()

        # QSS ДО создания окон: элементы рассчитывают свои sizeHint'ы
        # с учётом QSS-отступов; без стиля панели в момент построения
        # layout получает заниженные высоты и строки наезжают
        # друг на друга. Применяем глобальный стиль здесь же,
        # независимо от того, вызвал ли пользователь setStyleSheet.
        qapp.setStyleSheet(S.app_qss())

        def area():
            """Рабочая область: платформа или Qt-экран (с DPR)."""
            a = winutil.work_area()
            if a:
                return a
            scr = qapp.primaryScreen().availableGeometry()
            return (scr.x(), scr.y(), scr.width(), scr.height())

        self.area_of = area

        # окна
        self.overlay = OverlayWindow(
            self.state, area,
            on_menu=self._ctx_menu,
            on_reset=self.reset_view,
            on_save=self._save_soon)
        self.panel = Panel(self.state, {
            "open": self.open_image,
            "reset": self.reset_view,
            "fit": self.fit_to_screen,
            "hide": self.toggle_panel,
            "quit": self.quit,
            "draw": self.draw_toggle,
        }, area)

        # подписки state -> реакции
        for key, fn in (
                ("zoom", lambda *_: self.overlay.schedule_final(30)),
                ("x", lambda *_: None),
                ("y", lambda *_: None),
                ("opacity", lambda v, _e: self.overlay.setWindowOpacity(float(v)) or self._save_soon()),
                ("bw", lambda *_: self.overlay.apply("final")),
                ("edge", lambda *_: self.overlay.apply("final")),
                ("edge_strength", lambda *_: self.overlay.schedule_final(60)),
                ("grid", lambda *_: self.overlay.apply("final")),
                ("grid_size", lambda *_: self.overlay.apply("final")),
                ("flip_x", lambda *_: self.overlay.apply("final")),
                ("flip_y", lambda *_: self.overlay.apply("final")),
                ("image", self._on_image_changed),
                ("click_through",
                 lambda *_: self.overlay.apply_click_through()),
        ):
            self.state.subscribe(key, fn)
        for k in ("edge", "grid"):
            self.state.subscribe(
                k, lambda *_: self.panel.sync_subrows())

        # глобальные хоткеи
        self.hotkeys = HK.make_hotkeys(self.hk_queue)
        self._poll = QTimer(self.panel)
        self._poll.setInterval(80)
        self._poll.timeout.connect(self._drain_hotkeys)
        self._poll.start()

        self._top_timer = QTimer(self.panel)
        self._top_timer.setInterval(4000)
        self._top_timer.timeout.connect(self._keep_on_top)
        self._top_timer.start()
        QTimer.singleShot(150, self._keep_on_top)

        # Auto Drawing: исполнитель + обработка его событий СУЩЕСТВУЮЩИМ
        # 80-мс таймером _poll (новых таймеров не заводим — они не нужны)
        self.autodraw = AD.AutoDraw()
        self._drawn_panel_hidden = False

        # стартовая загрузка (если картинки нет — ждём выбора
        # пользователя: диалог не открываем принудительно, панель живёт)
        path = self.state.get("image")
        if path and os.path.isfile(path):
            self.load_image(path)

    # ------------------------------------------------------------- image
    def _on_image_changed(self, path, explicit):
        if explicit and path and path != getattr(self, "_loaded_path", None):
            self.load_image(path)

    def load_image(self, path):
        """Загрузка + вписывание в экран (центр, fit)."""
        if not self.overlay.load_image(path):
            return False
        self._loaded_path = path
        self.state.set("image", path)
        self.panel.set_filename(path)
        self.fit_to_screen()
        self.overlay.apply("final")
        self.overlay.setWindowOpacity(0.0)
        self._fade_in()
        self._save_soon()
        return True

    def _fade_in(self):
        """Появление картинки: 0 -> opacity за 220 мс."""
        from .controls import glide
        target = float(self.state.get("opacity"))

        def upd(v):
            self.overlay.setWindowOpacity(max(0.01, v))

        self._fade_anim = glide(None, 0.0, target, S.FADE if hasattr(S, 'FADE') else 220,
                                on_update=upd, parent=self.panel)
        self._fade_anim.start()

    def open_image(self):
        """Диалог выбора картинки (getOpenFileName — модальный, но через
        native static: не блокирует цикл навсегда на без-WM системах,
        где диалог не может открыться; провал => тихий возврат)."""
        from PySide6.QtWidgets import QFileDialog
        try:
            path, _ = QFileDialog.getOpenFileName(
                self.panel, "TracePad — выбрать картинку", "",
                "Картинки (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tiff);;"
                "Все файлы (*.*)")
        except Exception:
            return
        if path:
            self.load_image(path)

    def fit_to_screen(self):
        """Вписать и центрировать в рабочую область."""
        if not self.overlay.has_image():
            return
        area = self.area_of()
        iw, ih = self.overlay.size_px()
        z, x, y = G.fit_rect(iw, ih, area)
        self.state.set("zoom", z)
        self.state.set("x", x)
        self.state.set("y", y)

    # ------------------------------------------------------------ actions
    def reset_view(self):
        for k in ("grid", "flip_x", "flip_y", "bw", "edge",
                  "click_through"):
            self.state.set(k, False)
        self.state.set("opacity", 0.72)
        self.fit_to_screen()
        self.overlay.apply("final")
        self._save_soon()

    def toggle_panel(self):
        if self.panel.is_hidden():
            self.panel.show_panel()
        else:
            self.panel.hide_panel()

    def hotkey_action(self, action):
        if action == "opacity":
            cur = float(self.state.get("opacity"))
            if cur > 0.2:
                self._last_opacity = cur
                self.state.set("opacity", 0.15)
            else:
                self.state.set(
                    "opacity",
                    max(0.2, getattr(self, "_last_opacity", 0.72)))
        elif action in ("grid", "click_through", "flip_x", "flip_y",
                        "bw", "edge"):
            self.state.set(action, not bool(self.state.get(action)))
        elif action == "reset":
            self.reset_view()
        elif action == "panel":
            self.toggle_panel()
        elif action == "open":
            self.open_image()
        elif action == "draw":
            self.draw_toggle()
        elif action == "quit":
            self.quit()

    # ------------------------------------------------------------ auto draw
    def draw_toggle(self):
        """Кнопка Draw: идёт рисование — стоп; иначе — старт."""
        if self.autodraw.is_running():
            self.draw_stop()
        else:
            self.draw_start()

    def draw_start(self):
        """Запуск Auto Drawing.

        Сценарий: редактор под оверлеем уже выбран пользователем,
        кисть уже чёрная. Панель прячем на время рисования (курсор
        ходит по экрану — панель не должна попасть под перо), после —
        вернуть. Рисуем строго внутри bbox оверлея.
        """
        if not self.overlay.has_image():
            self.panel.set_draw_status("сначала открой картинку")
            return
        if self.autodraw.is_running():
            return
        # контуры нужны как источник траекторий; включаем молча,
        # если выключены (пользователь увидит их на подложке)
        if not bool(self.state.get("edge")):
            self.state.set("edge", True)
            self.overlay.apply("final")
        st = self.state
        mask = self.overlay.pipeline.edge_mask(int(st.get("edge_strength")))
        if mask is None or not mask.any():
            self.panel.set_draw_status("контуры не найдены")
            return
        tr = ST.trace(mask, rdp_tol=0.75,
                      min_points=int(st.get("draw_minlen")))
        if not tr:
            self.panel.set_draw_status("контуры не найдены")
            return
        tr = ST.order_strokes(tr)
        # ВАЖНО: iw/ih — размер МАСКИ (она может быть даунскейлом
        # оригинала для скорости, MAX_DETECT_PIX в edges.py); штрихи
        # живут в координатах маски, и маппинг в окно обязан
        # нормализоваться по ней, иначе траектории сместятся.
        view = dict(
            x=int(st.get("x")), y=int(st.get("y")),
            w=self.overlay.width(), h=self.overlay.height(),
            iw=mask.shape[1], ih=mask.shape[0],
            flip_x=bool(st.get("flip_x")),
            flip_y=bool(st.get("flip_y")))
        plan = AD.make_plan(tr, view, str(st.get("draw_speed")))
        # рисуем строго внутри bbox оверлея (не по чужому UI)
        area = (view["x"], view["y"], view["w"], view["h"])
        # спрятать панель (курсор её не должен задеть)
        if not self.panel.is_hidden():
            self.panel.hide_panel()
            self._drawn_panel_hidden = True
        ok = self.autodraw.start(plan, str(st.get("draw_speed")), area)
        if ok:
            self.panel.set_draw_status("рисую…  Esc/Draw — стоп")
        else:
            if self._drawn_panel_hidden:
                self.panel.show_panel()
                self._drawn_panel_hidden = False

    def draw_stop(self):
        self.autodraw.stop()

    def _draw_event(self, ev):
        """События исполнителя (уже в GUI-потоке — из _drain_hotkeys)."""
        kind = ev[0]
        if kind == "progress":
            _, i, n = ev
            self.panel.set_draw_status("рисую…  %d/%d" % (i + 1, n))
        elif kind == "done":
            _, interrupted, n = ev
            why = self.autodraw._abort_reason
            if interrupted and why:
                self.panel.set_draw_status("остановлено: %s" % why)
            elif interrupted:
                self.panel.set_draw_status("остановлено")
            else:
                self.panel.set_draw_status("готово: %d штрихов" % n)
            self._after_draw()
        elif kind == "error":
            self.panel.set_draw_status("ошибка: %s" % ev[1])
            self._after_draw()

    def _after_draw(self):
        if self._drawn_panel_hidden:
            self.panel.show_panel()
            self._drawn_panel_hidden = False

    def _drain_hotkeys(self):
        while self.hk_queue:
            self.hotkey_action(self.hk_queue.popleft())
        while self.autodraw.events:
            self._draw_event(self.autodraw.events.popleft())

    def _keep_on_top(self):
        if self.overlay.isVisible():
            winutil.topmost(int(self.overlay.winId()), True)
        if not self.panel.is_hidden():
            winutil.topmost(int(self.panel.winId()), True)

    # ------------------------------------------------------------ persistence
    def _save_soon(self):
        """Отложенное сохранение (debounce 400 мс).

        Таймер один, создаётся лениво и переиспользуется: у QTimer нет
        key­word-аргумента interval= в конструкторе — только
        setSingleShot/setInterval (иначе TypeError на некоторых
        сборках PySide6).
        """
        if not hasattr(self, "_save_timer") or self._save_timer is None:
            self._save_timer = QTimer(self.panel)
            self._save_timer.setSingleShot(True)
            self._save_timer.setInterval(400)
            self._save_timer.timeout.connect(self._save_now)
        self._save_timer.start()

    def _save_now(self):
        try:
            if self.overlay.isVisible() and self.overlay.width() > 2:
                self.state._d["x"] = self.overlay.x()
                self.state._d["y"] = self.overlay.y()
            if not self.panel.is_hidden() and self.panel.width() > 2:
                self.state._d["panel_x"] = self.panel.x()
                self.state._d["panel_y"] = self.panel.y()
        except Exception:
            pass
        self.state.save()

    # ---------------------------------------------------------------- menu
    def _ctx_menu(self, gpos):
        m = QMenu(self.overlay)
        m.setStyleSheet(S.app_qss())
        for label, fn in (
                ("Открыть…", self.open_image),
                ("Вписать в экран", self.fit_to_screen),
                ("Сбросить вид", self.reset_view),
                (None, None),
                ("Авто-рисовка: Draw/стоп", self.draw_toggle),
                ("Панель скрыть/показать", self.toggle_panel),
                ("Выход", self.quit)):
            if label is None:
                m.addSeparator()
                continue
            a = QAction(label, m)
            a.triggered.connect(fn)
            m.addAction(a)
        m.exec(gpos)

    # ---------------------------------------------------------------- quit
    def quit(self):
        self.draw_stop()
        try:
            self._save_now()
        except Exception:
            pass
        if getattr(self, "hotkeys", None):
            try:
                self.hotkeys.stop()
            except Exception:
                pass
        self.qapp.quit()


def main():
    app = QApplication(sys.argv if sys.argv else ["TracePad"])
    app.setStyleSheet(S.app_qss())
    app.setQuitOnLastWindowClosed(False)
    a = App(app, image_path=(sys.argv[1] if len(sys.argv) > 1 else None))

    # Esc — стоп автрисовки (глобальный фильтр: во время Draw фокус
    # может быть у редактора ПОД оверлеем, наши окна клавиш не увидят)
    from PySide6.QtCore import QEvent

    class _EscFilter(QObject):
        def eventFilter(self, obj, ev):
            if (ev.type() == QEvent.KeyPress and
                    ev.key() == Qt.Key_Escape and
                    a.autodraw.is_running()):
                a.draw_stop()
                return True
            return QObject.eventFilter(self, obj, ev)

    filt = _EscFilter(app)
    app.installEventFilter(filt)
    app.exec()


if __name__ == "__main__":
    main()
