# -*- coding: utf-8 -*-
"""controls.py — составные контролы панели (прозрачность, зум, режимы).

Все контролы — QWidget с QSS из style.py, анимации — QPropertyAnimation
на qreal-свойства (juice по делу: 140 мс переходы, без вечных циклов).

Каждый контрол подписан на state: set -> valueChanged -> перерисовка.
Обратная связь (клик/драг) -> state.set. GUI ничего не хранит сам.
"""

from __future__ import annotations

from PySide6.QtCore import (QEasingCurve, QPropertyAnimation, Qt,
                            QPointF, QObject, Property, Signal)
from PySide6.QtGui import QColor, QPainter, QPen, QFont
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel,
                                QPushButton, QVBoxLayout, QWidget, QSizePolicy)

from . import style as S


# ---------------------------------------------------------------------------
#  анимируемое число (цель QPropertyAnimation)
# ---------------------------------------------------------------------------
class _Num(QObject):
    """Простой QObject с анимируемым qreal-свойством v."""

    def __init__(self, v=0.0, parent=None):
        super().__init__(parent)
        self._v = float(v)

    def _get(self):
        return self._v

    def _set(self, v):
        self._v = float(v)

    v = Property(float, _get, _set)


def glide(obj_attr_owner, start, end, ms=S.DUR, on_update=None,
          parent=None):
    """Одна QPropertyAnimation 0->1 c on_update(value)."""
    num = _Num(start, parent)
    a = QPropertyAnimation(num, b"v", parent)
    a.setDuration(ms)
    a.setStartValue(float(start))
    a.setEndValue(float(end))
    a.setEasingCurve(QEasingCurve.OutCubic)
    if on_update:
        a.valueChanged.connect(lambda: on_update(num.v))
    return a


# ---------------------------------------------------------------------------
#  Switch
# ---------------------------------------------------------------------------
class Switch(QWidget):
    """Пилюля-переключатель 36x20 с бегунком и плавным переходом."""

    toggled = Signal(bool)

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setFixedSize(38, 22)
        self.setCursor(Qt.PointingHandCursor)
        self._v = float(bool(checked))
        self._anim = None

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.set_checked(not self._is_on())

    def _is_on(self):
        return self._v > 0.5

    def set_checked(self, on, animate=True):
        on = bool(on)
        target = 1.0 if on else 0.0
        self.toggled.emit(on)
        if not animate:
            self._v = target
            self.update()
            return
        # НЕ используем DeleteWhenStopped: C++-объект удаляется, а
        # Python-ссылка self._anim остаётся висячей -> RuntimeError на
        # следующем клике. Держим ссылку, обнуляем по finished.
        if self._anim:
            self._anim.stop()
        self._anim = glide(self, self._v, target, S.DUR,
                           on_update=self._apply, parent=self)
        anim = self._anim
        anim.finished.connect(lambda: self._anim is anim and
                              setattr(self, "_anim", None))
        anim.start()

    def _apply(self, v):
        self._v = v
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t = self._v
        # трек
        track = QColor(S.ACCENT) if t > 0 else QColor(S.FIELD)
        if 0 < t <= 1:   # интерполяция цвета трека
            a = QColor(S.ACCENT)
            f = QColor(S.FIELD)
            track = QColor(
                int(f.red() + (a.red() - f.red()) * t),
                int(f.green() + (a.green() - f.green()) * t),
                int(f.blue() + (a.blue() - f.blue()) * t))
        pen = QPen(QColor(S.STROKE) if t < 0.6 else QColor(S.ACCENT))
        pen.setWidthF(1.2)
        p.setPen(pen)
        p.setBrush(track)
        p.drawRoundedRect(1, 1, self.width() - 2, self.height() - 2,
                         (self.height() - 2) / 2, (self.height() - 2) / 2)
        # бегунок
        kx = 2 + t * (self.width() - 2 - 16 - 2)
        kn = QColor(S.FG) if t < 0.5 else QColor(S.ACCENT_ON)
        p.setPen(Qt.NoPen)
        p.setBrush(kn)
        p.drawEllipse(QPointF(kx + 8, self.height() / 2), 7.0, 7.0)
        p.end()


# ---------------------------------------------------------------------------
#  Slider
# ---------------------------------------------------------------------------
class Slider(QWidget):
    """Кастомный слайдер: подпись, значение, дорожка с заполнением, ручка.

    Драг за дорожку/ручку; значение -> changed(float).
    """

    changed = Signal(float)

    def __init__(self, vmin, vmax, value, fmt=None, step=None,
                 title=None, parent=None):
        super().__init__(parent)
        self._vmin, self._vmax = float(vmin), float(vmax)
        self._step = float(step) if step else None
        self._fmt = fmt or (lambda v: "%d%%" % round(v * 100))
        self._v = self._clamp(float(value))
        self._drag = False
        self._hover = False

        lay = QGridLayout(self)
        lay.setContentsMargins(10, 6, 10, 8)
        lay.setHorizontalSpacing(6)
        lay.setVerticalSpacing(4)
        if title:
            t = QLabel(title)
            t.setProperty("class", "caps")
            lay.addWidget(t, 0, 0, Qt.AlignLeft)
        self._val = QLabel(self._fmt(self._v))
        self._val.setProperty("class", "value")
        lay.addWidget(self._val, 0, 1, Qt.AlignRight)
        self._track = _Trackpad(self)
        lay.addWidget(self._track, 1, 0, 1, 2)
        self._track.changed.connect(self._on_pad)
        self.setMinimumHeight(52)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    # --- value plumbing ------------------------------------------------
    def _clamp(self, v):
        v = min(self._vmax, max(self._vmin, v))
        if self._step:
            v = round(v / self._step) * self._step
        return v

    def set_value(self, v, animate=False):
        v = self._clamp(float(v))
        if v == self._v:
            return
        self._v = v
        self._val.setText(self._fmt(v))
        self._track.set_pos(self._frac(), animate)
        self.changed.emit(v)

    def value(self):
        return self._v

    def _frac(self):
        return (self._v - self._vmin) / (self._vmax - self._vmin)

    def _on_pad(self, f):
        v = self._clamp(self._vmin + f * (self._vmax - self._vmin))
        if v != self._v:
            self._v = v
            self._val.setText(self._fmt(v))
            self.changed.emit(v)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._track.set_pos(self._frac(), False)


class _Trackpad(QWidget):
    """Дорожка слайдера: клик/драг, заполнение, ручка, hover."""

    changed = Signal(float)

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self._f = 0.0
        self._drag = False
        self._hover = False
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(18)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_pos(self, f, animate=False):
        self._f = max(0.0, min(1.0, f))
        self.update()

    def _frac_from_x(self, x):
        w = self.width() - 20   # поля ручки
        return 0.0 if w <= 0 else max(0.0, min(1.0, (x - 10) / w))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = True
            self._emit(e.position().x())

    def mouseMoveEvent(self, e):
        if self._drag:
            self._emit(e.position().x())

    def mouseReleaseEvent(self, _e):
        self._drag = False

    def enterEvent(self, _e):
        self._hover = True
        self.update()

    def leaveEvent(self, _e):
        self._hover = False
        self.update()

    def _emit(self, x):
        self._f = self._frac_from_x(x)
        self.update()
        self.changed.emit(self._f)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self.height()
        cy = h / 2
        x0, x1 = 10, self.width() - 10
        # дорожка
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(S.FIELD))
        p.drawRoundedRect(x0, cy - 2, x1 - x0, 4, 2, 2)
        # заполнение
        kx = x0 + self._f * (x1 - x0)
        p.setBrush(QColor(S.ACCENT))
        p.drawRoundedRect(x0, cy - 2, max(2, kx - x0), 4, 2, 2)
        # ручка
        r = 8.5 if (self._hover or self._drag) else 6.5
        p.setBrush(QColor(S.FG))
        p.setPen(QPen(QColor(S.GLASS), 1.4))
        p.drawEllipse(QPointF(kx, cy), r, r)
        p.end()


# ---------------------------------------------------------------------------
#  Segmented
# ---------------------------------------------------------------------------
class Segmented(QWidget):
    """Пресеты в одну строку (кнопки-сегменты, эксклюзивно)."""

    changed = Signal(float)

    def __init__(self, values, current, labels=None, parent=None):
        super().__init__(parent)
        self._values = list(map(float, values))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 2, 10, 2)
        lay.setSpacing(5)
        self._btns = []
        for i, v in enumerate(self._values):
            b = QPushButton(labels[i] if labels else
                            "%d%%" % round(v * 100))
            b.setObjectName("Segment")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFlat(True)
            b.clicked.connect(lambda _c, v=v: self._pick(v))
            lay.addWidget(b, 1)
            self._btns.append((b, v))
        self.set_current(current)

    def set_current(self, v):
        v = float(v)
        best = min(range(len(self._values)),
                   key=lambda i: abs(self._values[i] - v))
        for i, (b, val) in enumerate(self._btns):
            b.setChecked(i == best)

    def _pick(self, v):
        self.changed.emit(float(v))


# ---------------------------------------------------------------------------
#  RowSwitch: строка «подпись + Switch»
# ---------------------------------------------------------------------------
class RowSwitch(QWidget):
    """Строка-переключатель: слева подпись, справа Switch, hover-подсветка."""

    toggled = Signal(bool)

    def __init__(self, label, checked=False, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self._hover = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lab = QLabel(label)
        lay.addWidget(lab, 1)
        self._sw = Switch(checked)
        self._sw.toggled.connect(lambda on: self.toggled.emit(on))
        lay.addWidget(self._sw, 0, Qt.AlignRight)
        self.setMinimumHeight(36)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_checked(self, on):
        self._sw.set_checked(on)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.pos()):
            self._sw.set_checked(not self._sw._is_on())

    def enterEvent(self, _e):
        self._hover = True
        self.update()

    def leaveEvent(self, _e):
        self._hover = False
        self.update()

    def paintEvent(self, _e):
        if self._hover:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(S.GLASS_HI))
            p.drawRoundedRect(2, 2, self.width() - 4, self.height() - 4,
                              S.R_ROW, S.R_ROW)
            p.end()
