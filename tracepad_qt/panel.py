# -*- coding: utf-8 -*-
"""panel.py — плавающая стеклянная панель управления (Вариант B).

QWidget frameless + translucent, компоновка QVBoxLayout (никаких ручных
координат — наложения исключены архитектурно). Шапка с drag, секции:
прозрачность (слайдер+пресеты), зум, режимы (с под-настройками, которые
появляются только при включённом режиме через QPropertyAnimation высоты),
действия.

Анимации: появление панели (fade 240 мс), раскрытие под-настроек
(высота 240 мс), свитчи/слайдеры (140 мс). Все — QPropertyAnimation,
завершаются и освобождаются, никаких вечных циклов.
"""

from __future__ import annotations

import os

from PySide6.QtCore import (QEasingCurve, QEvent, QPropertyAnimation,
                            Qt, QParallelAnimationGroup)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                                QSizePolicy, QVBoxLayout, QWidget)

from . import style as S, winutil
from .controls import RowSwitch, Segmented, Slider, glide


class GlassPanel(QFrame):
    """Стеклянная карточка: фон рисуем сами (QPainter), не QSS.

    Почему: WA_TranslucentBackground (нужен для прозрачных окон)
    обнуляет QSS-фон — QSS background рисуется в window-слое, который
    translucency вырезает. Ручная отрисовка в paintEvent с
    QColor(..., A) даёт настоящее полупрозрачное стекло поверх
    рабочего стола и работает одинаково на всех платформах.

    Creative Pro + Glassmorphism: карточка 94% непрозрачности, тонкая
    светлая кромка сверху («свет по краю стекла»), мягкая внутренняя
    подсветка у верхнего края (индиго 6% — «студийная подсветка
    стекла») и 1px-граница. Без блюра фона: он требует
    композиции всего экрана под окно и на десктопе неоправдан.
    """

    def __init__(self):
        super().__init__()
        self.setObjectName("GlassPanel")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Minimum)

    def paintEvent(self, _e):
        from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width() - 1, self.height() - 1
        # тело стекла
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(21, 21, 31, 240))          # GLASS @ 94%
        p.drawRoundedRect(0, 0, w, h, S.R_CARD, S.R_CARD)
        # внутренняя подсветка верхнего края: индиго-градиент 8%->0
        grad = QLinearGradient(0, 0, 0, 64)
        grad.setColorAt(0.0, QColor(124, 108, 255, 22))
        grad.setColorAt(1.0, QColor(124, 108, 255, 0))
        p.setBrush(grad)
        p.drawRoundedRect(1, 1, w - 2, 62, S.R_CARD - 1, S.R_CARD - 1)
        # тонкая кромка «свет по краю стекла»
        pen = QPen(QColor(84, 84, 118, 210))
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(0, 0, w, h, S.R_CARD, S.R_CARD)
        # граница QSS (STROKE) — для консистентности с остальным
        pen2 = QPen(QColor(S.STROKE))
        pen2.setWidthF(0.6)
        p.setPen(pen2)
        p.drawRoundedRect(1, 1, w - 2, h - 2, S.R_CARD - 1, S.R_CARD - 1)
        p.end()


class _Collapser(QWidget):
    """Обёртка, раскрывающая контент анимацией высоты.

    Односторонний поток: state -> reveal(). Анимация меняет только
    геометрию обёртки (minimumHeight), НЕ трогает layout родителя во
    время тиков — иначе событие LayoutRequest заново считает layout
    внутри processEvents и получается цикл. По завершении снимаем
    фиксацию высоты, отдавая её layout'у.
    """

    def __init__(self, content, parent=None):
        super().__init__(parent)
        self._content = content
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)
        self._anim = None
        # скрытое состояние = ровно ноль: без этого дефолтная геометрия
        # нового QWidget (640x480, maximumHeight=QWIDGETSIZE_MAX)
        # отравляет расчёт QVBoxLayout родителя и строки наезжают
        self.setFixedSize(0, 0)
        self.hide()

    def _stop_anim(self):
        if self._anim:
            self._anim.stop()
            self._anim.deleteLater()
            self._anim = None

    def reveal(self, on: bool, ms=S.DUR_SLOW):
        self._stop_anim()
        if on:
            self._content.show()
            self._content.adjustSize()
            self._max = self._content.sizeHint().height()
            self.setMinimumHeight(self.height() if self.height() > 0 else 0)
            self.setMaximumHeight(self._max)
            self.show()
            a = QPropertyAnimation(self, b"minimumHeight", self)
            a.setDuration(ms)
            a.setStartValue(self.height())
            a.setEndValue(self._max)
            a.setEasingCurve(QEasingCurve.OutCubic)
            a.finished.connect(self._after_reveal)
            self._anim = a
            a.start()
        else:
            h0 = self.height()
            self.setMinimumHeight(0)
            a = QPropertyAnimation(self, b"maximumHeight", self)
            a.setDuration(ms)
            a.setStartValue(h0)
            a.setEndValue(0)
            a.setEasingCurve(QEasingCurve.OutCubic)
            a.finished.connect(self._after_hide)
            self._anim = a
            a.start()

    def _after_reveal(self):
        self.setMinimumHeight(0)
        self.setMaximumHeight(self._max)

    def _after_hide(self):
        self._content.hide()
        self.setFixedSize(0, 0)
        self.hide()


class Panel(QWidget):
    """Панель управления TracePad."""

    def __init__(self, state, actions, area_provider):
        super().__init__(None, Qt.FramelessWindowHint |
                         Qt.WindowStaysOnTopHint | Qt.Tool)
        self.state = state
        self.actions = actions
        self.area_of = area_provider
        self._drag = None
        self._sub_anim = {}

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        if winutil.IS_WIN:
            winutil.toolwindow(int(self.winId()))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        self._glass = GlassPanel()
        outer.addWidget(self._glass)

        lay = QVBoxLayout(self._glass)
        lay.setContentsMargins(0, 0, 0, 6)
        lay.setSpacing(2)
        self._build_header(lay)
        self._build_body(lay)

        self.setFixedWidth(S.PANEL_W)
        # Окно Qt.Tool на без-WM платформах не активирует вложенные
        # layout'ы автоматически при adjustSize: первый проход даёт
        # «нулевые» позиции, второй (после явной активации) — правильные.
        # Без этого строки наезжают друг на друга.
        self.adjustSize()
        self._glass.layout().activate()
        self._body.layout().activate()
        self.adjustSize()
        self._glass.layout().activate()
        self._body.layout().activate()
        self.adjustSize()

        # позиция из state или у верхнего-левого угла рабочей области
        px, py = state.get("panel_x"), state.get("panel_y")
        if px is not None and py is not None:
            self.move(int(px), int(py))
        else:
            ax, ay, _, _ = self.area_of()
            self.move(ax + 24, ay + 24)

        self._fade = glide(None, 0.0, 1.0, S.DUR_SLOW,
                           on_update=self._set_fade, parent=self)
        self._set_fade(0.0)
        self.show()
        # без DeleteWhenStopped: держим ссылку до конца (иначе висячий
        # C++-объект -> RuntimeError при повторном show_panel)
        self._fade.start()

    def _set_fade(self, v):
        self.setWindowOpacity(max(0.01, v))

    # ------------------------------------------------------------- build
    def _build_header(self, lay):
        h = QWidget()
        hl = QHBoxLayout(h)
        hl.setContentsMargins(14, 12, 12, 6)
        hl.setSpacing(8)
        t = QLabel("TRACEPAD")
        t.setProperty("class", "h1")
        hl.addWidget(t, 0)
        self._fname = QLabel("нет картинки")
        self._fname.setProperty("class", "dim")
        self._fname.setMinimumWidth(10)
        hl.addWidget(self._fname, 1)
        self._chev = QPushButton("—")
        self._chev.setFixedSize(26, 26)
        self._chev.setCursor(Qt.PointingHandCursor)
        self._chev.setToolTip("Свернуть панель")
        self._chev.clicked.connect(self.toggle_collapse)
        hl.addWidget(self._chev, 0, Qt.AlignRight)
        lay.addWidget(h)
        # шапка = зона drag
        h.setCursor(Qt.SizeAllCursor)
        h.installEventFilter(self)
        self._header = h

    def _build_body(self, lay):
        self._body = QWidget()
        bl = QVBoxLayout(self._body)
        bl.setContentsMargins(4, 0, 4, 0)
        bl.setSpacing(6)

        # ---------- прозрачность
        self._sec("прозрачность", bl)
        self._sl_opacity = Slider(0.05, 1.0,
                                  float(self.state.get("opacity")),
                                  title="степень")
        self._sl_opacity.changed.connect(
            lambda v: self.state.set("opacity", v))
        bl.addWidget(self._sl_opacity)
        seg = Segmented((0.2, 0.4, 0.6, 0.8, 1.0),
                        float(self.state.get("opacity")))
        seg.changed.connect(lambda v: self.state.set("opacity", v))
        self._seg_opacity = seg
        bl.addWidget(seg)

        # ---------- зум
        self._sec("масштаб", bl)
        self._sl_zoom = Slider(
            0.05, 8.0,
            float(self.state.get("zoom")),
            fmt=lambda v: "%d%%" % round(v * 100), step=0.05,
            title="зум")
        self._sl_zoom.changed.connect(
            lambda v: self.state.set("zoom", v))
        bl.addWidget(self._sl_zoom)

        # ---------- режимы
        self._sec("режимы", bl)
        self._rows = {}
        self._collapsers = {}
        for label, key in (("клик сквозь", "click_through"),
                           ("сетка", "grid"),
                           ("флип ↔", "flip_x"), ("флип ↕", "flip_y"),
                           ("ч/б", "bw"), ("контуры", "edge")):
            row = RowSwitch(label, bool(self.state.get(key)))
            row.toggled.connect(lambda on, k=key: self.state.set(k, on))
            bl.addWidget(row)
            self._rows[key] = row

        # под-настройки: контуры/сетка (раскрываются при вкл. режиме)
        self._sub_edge = Slider(0, 100,
                                int(self.state.get("edge_strength")),
                                fmt=lambda v: "%d" % round(v), step=5,
                                title="чувствительность")
        self._sub_edge.changed.connect(
            lambda v: self.state.set("edge_strength", int(v)))
        self._col_edge = self._wrap_collapser(self._sub_edge)
        bl.addWidget(self._col_edge)

        self._sub_grid = Slider(16, 128,
                                int(self.state.get("grid_size")),
                                fmt=lambda v: "%d px" % round(v), step=8,
                                title="шаг сетки")
        self._sub_grid.changed.connect(
            lambda v: self.state.set("grid_size", int(v)))
        self._col_grid = self._wrap_collapser(self._sub_grid)
        bl.addWidget(self._col_grid)

        # ---------- авто-рисовка
        self._sec("авто-рисовка", bl)
        self._btn_draw = QPushButton("✎   Draw  ·  обвести контуры")
        self._btn_draw.setObjectName("DrawButton")
        self._btn_draw.setCursor(Qt.PointingHandCursor)
        self._btn_draw.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        cmd = self.actions.get("draw")
        if cmd:
            self._btn_draw.clicked.connect(cmd)
        bl.addWidget(self._btn_draw)
        self._draw_status = QLabel("выбери чёрную кисть в редакторе под "
                                    "референсом, потом Draw")
        self._draw_status.setProperty("class", "dim")
        self._draw_status.setWordWrap(True)
        bl.addWidget(self._draw_status)
        seg_speed = Segmented((0, 1, 2),
                              1,
                              labels=("точно", "обычно", "быстро"))
        seg_speed.changed.connect(
            lambda v: self.state.set(
                "draw_speed", ("slow", "normal", "fast")[int(v)]))
        self._seg_speed = seg_speed
        bl.addWidget(seg_speed)

        # ---------- действия
        self._sec("действия", bl)
        for glyph, label, key, danger in (
                ("◧", "открыть картинку", "open", False),
                ("↺", "сбросить вид", "reset", False),
                ("⤢", "вписать в экран", "fit", False),
                ("⌄", "скрыть панель  ·  Ctrl+Alt+H", "hide", False),
                ("✕", "выход  ·  Ctrl+Alt+Q", "quit", True)):
            b = QPushButton("%s   %s" % (glyph, label))
            b.setObjectName("RowDanger" if danger else "RowButton")
            b.setCursor(Qt.PointingHandCursor)
            cmd = self.actions.get(key)
            if cmd:
                b.clicked.connect(cmd)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            bl.addWidget(b)

        # подсказка
        hint = QLabel("Ctrl+Alt: T прозр. · G сетка · C сквозь · F/V флип · "
                      "B ч/б · E контуры · D рисовать · R сброс · O открыть · "
                      "H панель · Q выход")
        hint.setProperty("class", "faint")
        hint.setWordWrap(True)
        bl.addWidget(hint)

        ok, msg = winutil.supports_click_through()
        if not ok and msg:
            w = QLabel("⚠ " + msg)
            w.setProperty("class", "faint")
            w.setWordWrap(True)
            w.setStyleSheet("color: #d8b45a;")
            bl.addWidget(w)

        lay.addWidget(self._body)

    def _sec(self, title, lay):
        l = QLabel(title)
        l.setProperty("class", "caps")
        lay.addWidget(l)

    def _wrap_collapser(self, w):
        col = _Collapser(w)
        w.hide()
        col.hide()
        return col

    # ---------------------------------------------------- режимы->поднастройки
    def sync_subrows(self):
        self._col_edge.reveal(bool(self.state.get("edge")))
        self._col_grid.reveal(bool(self.state.get("grid")))

    # ---------------------------------------------------- сворачивание
    def toggle_collapse(self):
        """Свернуть панель до шапки: прячем body анимацией высоты окна."""
        if self._body.isVisible():
            h0 = self.height()
            target = self._header_height() + 16
            self._anim_c = QPropertyAnimation(self, b"maximumHeight", self)
            self._anim_c.setStartValue(h0)
            self._anim_c.setEndValue(target)
            self._anim_c.setDuration(S.DUR_SLOW)
            self._anim_c.setEasingCurve(QEasingCurve.OutCubic)
            self._anim_c.finished.connect(self._finish_collapse)
            self._anim_c.start()
            self._chev.setText("+")
        else:
            # сняли фиксацию и пересобрали layout в ПРАВИЛЬНОМ порядке:
            # вернуть maximum -> показать body -> invalidate стек layout'ов
            # (hint'ы кэшируются) -> adjustSize
            self._body.show()
            self._relayout()
            self._chev.setText("—")

    def _relayout(self):
        """Вернуть панели полную высоту после сворачивания.

        adjustSize() на окне Qt.Tool без WM не всегда принимает
        пересчитанные hint'ы с первого раза; надёжнее явно
        resize(sizeHint) после активации layout'ов.
        """
        self.setMaximumHeight(16777215)
        self.setMinimumHeight(0)
        for lay in (self.layout(), self._glass.layout(),
                    self._body.layout()):
            if lay:
                lay.invalidate()
        self._glass.layout().activate()
        self._body.layout().activate()
        self.resize(self.sizeHint().width(), self.sizeHint().height())

    def _header_height(self):
        return max(self._header.sizeHint().height(), 44)

    def _finish_collapse(self):
        self._body.hide()
        h = self._header_height() + 16
        self.setMaximumHeight(h)
        self.setMinimumHeight(h)
        self.resize(self.width(), h)

    # ---------------------------------------------------- state -> widgets
    def sync_state(self):
        st = self.state
        self._sl_opacity.set_value(float(st.get("opacity")))
        self._seg_opacity.set_current(float(st.get("opacity")))
        self._sl_zoom.set_value(float(st.get("zoom")))
        for key, row in self._rows.items():
            row.set_checked(bool(st.get(key)))
        self.sync_subrows()

    def set_draw_status(self, text):
        """Статус авто-рисовки (строка под кнопкой Draw)."""
        self._draw_status.setText(text)
        running = text.startswith("рисую")
        self._btn_draw.setText(
            "■   Стоп  ·  Esc" if running else "✎   Draw  ·  обвести контуры")

    def set_filename(self, path):
        name = os.path.basename(path) if path else "нет картинки"
        self._fname.setText(name[:30])

    # ---------------------------------------------------- перемещение
    def eventFilter(self, obj, e):
        """Drag панели за шапку.

        ВАЖНО: сверяемся с QEvent.Type ЯВНО (магические числа 27/28/29
        не совпадают с реальными enum'ами Qt — press=2, release=3,
        move=5 — из-за этого drag никогда не срабатывал).
        """
        if obj is self._header:
            t = e.type()
            if t == QEvent.MouseButtonPress:
                if (e.button() == Qt.LeftButton):
                    self._drag = (e.globalPosition().toPoint(), self.pos())
            elif t == QEvent.MouseMove and self._drag:
                d0, p0 = self._drag
                now = e.globalPosition().toPoint()
                self.move(p0 + now - d0)
            elif t == QEvent.MouseButtonRelease:
                if self._drag:
                    self._drag = None
                    self.state.set("panel_x", self.x())
                    self.state.set("panel_y", self.y())
        return super().eventFilter(obj, e)

    # ---------------------------------------------------- видимость
    def hide_panel(self):
        self.hide()

    def show_panel(self):
        self.show()
        self._fade = glide(None, 0.0, 1.0, S.DUR_SLOW,
                           on_update=self._set_fade, parent=self)
        self._fade.start()

    def is_hidden(self):
        return not self.isVisible()
