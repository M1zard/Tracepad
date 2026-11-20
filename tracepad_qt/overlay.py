# -*- coding: utf-8 -*-
"""overlay.py — окно картинки поверх всех окон.

Frameless + ToolTip-уровень (не в панели задач), always-on-top через
winutil.topmost, click-through через winutil.click_through (по winId).
Прозрачность — WindowOpacity (умножает RGBA-кадр; альфа самой картинки в
пикселях сохраняется — Edge-контуры остаются полупрозрачными, как в MVP).

Отрисовка: QLabel с QPixmap из PIL-кадра (ImageQt). fast/final-кадры из
renderer; final — после остановки взаимодействия (debounce 160 мс).
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QMenu, QWidget

from PIL import Image

from . import geometry as G
from . import style as S, winutil
from .renderer import RenderPipeline


def pil_to_qpil(img: Image.Image) -> QImage:
    """RGBA PIL -> QImage без копии байтов (для показа в QLabel)."""
    return QImage(img.tobytes(), img.width, img.height,
                 4 * img.width, QImage.Format_RGBA8888)


class OverlayWindow(QWidget):
    def __init__(self, state, screen_area_provider, on_menu=None,
                 on_reset=None, on_save=None):
        super().__init__(None,
                         Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                         Qt.Tool)
        self.state = state
        self.area_of = screen_area_provider
        self.on_menu = on_menu
        self.on_reset = on_reset
        self.on_save = on_save

        self.pipeline = RenderPipeline()
        self._qimg = None
        self._final_timer = QTimer(self)
        self._final_timer.setSingleShot(True)
        self._final_timer.setInterval(160)
        self._final_timer.timeout.connect(lambda: self.apply("final"))
        self._drag = None
        self._apply_ct = True

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet(S.overlay_qss())
        self.setMouseTracking(True)
        if winutil.IS_WIN:
            winutil.toolwindow(int(self.winId()))
        self.hide()

    # ------------------------------------------------------------- image
    def load_image(self, path):
        try:
            img = Image.open(path)
            img.load()
        except Exception:
            return False
        self.pipeline.set_image(img)
        return True

    def has_image(self):
        return self.pipeline.has_image()

    def size_px(self):
        return self.pipeline.size()

    def extent(self):
        return self.pipeline.extent(float(self.state.get("zoom")))

    # ------------------------------------------------------------- render
    def apply(self, quality="final"):
        st = self.state
        if not self.pipeline.has_image():
            self.hide()
            return
        img = self.pipeline.render(
            zoom=float(st.get("zoom")),
            bw=bool(st.get("bw")), edge=bool(st.get("edge")),
            edge_strength=int(st.get("edge_strength")),
            flip_x=bool(st.get("flip_x")), flip_y=bool(st.get("flip_y")),
            grid=bool(st.get("grid")), grid_size=int(st.get("grid_size")),
            quality=quality)
        if img is None:
            return
        w, h = img.size
        ax, ay, aw, ah = self.area_of()
        x, y = G.clamp_into(int(st.get("x")), int(st.get("y")), w, h,
                            (ax, ay, aw, ah))
        self._qimg = pil_to_qpil(img)
        self.setPixmapInternal(w, h)
        self.setGeometry(x, y, w, h)
        self.setWindowOpacity(float(st.get("opacity")))
        self.show()
        self.apply_click_through(w, h)
        winutil.topmost(int(self.winId()), True)

    def setPixmapInternal(self, w, h):
        """QLabel-«экран» поверх окна.

        ВАЖНО: QLabel по умолчанию принимает события мыши и НЕ
        пропускает их окну-родителю — без WA_TransparentForMouseEvents
        ни drag, ни wheel, ни dblclick оверлея никогда бы не сработали
        (события доставлялись QLabel'у, который их просто съедал).
        """
        from PySide6.QtWidgets import QLabel
        if not hasattr(self, "_lbl"):
            self._lbl = QLabel(self)
            self._lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self._lbl.setGeometry(0, 0, w, h)
            self._lbl.setScaledContents(True)
            self._lbl.setStyleSheet("background: transparent; border: none;")
        else:
            self._lbl.setGeometry(0, 0, w, h)
        pix = QPixmap.fromImage(self._qimg)
        self._lbl.setPixmap(pix)

    def apply_interactive(self):
        self.apply("fast")

    def schedule_final(self, ms=160):
        self._final_timer.start(ms)

    # ----------------------------------------------------- click-through
    def apply_click_through(self, w=None, h=None):
        want = bool(self.state.get("click_through"))
        if not hasattr(self, "_ct_state") or self._ct_state != want:
            self._ct_state = want
            winutil.click_through(int(self.winId()), want, w, h)
            if not want and w and h:
                pass  # shape reset через полный rect уже внутри winutil

    # ------------------------------------------------------- interaction
    def wheelEvent(self, e):
        if not self.pipeline.has_image():
            return
        d = e.angleDelta().y()
        self.zoom_at(1 if d > 0 else -1, e.position().x(),
                     e.position().y())

    def zoom_at(self, direction, px=None, py=None):
        st = self.state
        z = float(st.get("zoom")) * (1.12 ** direction)
        z = min(G.MAX_ZOOM, max(G.MIN_ZOOM, z))
        if z == st.get("zoom"):
            return
        if px is not None and py is not None:
            ow, oh = self.pipeline.extent(float(st.get("zoom")))
            nw, nh = self.pipeline.extent(z)
            kx = px / ow if ow else 0
            ky = py / oh if oh else 0
            st.set("x", int(st.get("x")) + int(px - kx * nw))
            st.set("y", int(st.get("y")) + int(py - ky * nh))
        st.set("zoom", z)
        self.apply_interactive()
        self.schedule_final()

    def mouseDoubleClickEvent(self, _e):
        if self.on_reset:
            self.on_reset()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = (e.globalPosition().toPoint(),
                          self.pos())

    def mouseMoveEvent(self, e):
        if self._drag:
            d0, p0 = self._drag
            now = e.globalPosition().toPoint()
            st = self.state
            st.set("x", p0.x() + now.x() - d0.x())
            st.set("y", p0.y() + now.y() - d0.y())
            self.apply_interactive()
            self.schedule_final()
        elif e.button() == Qt.NoButton and bool(
                self.state.get("click_through")):
            # в сквозном режиме окно не получает событий — сюда не попадём
            pass

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = None
            if self.on_save:
                self.on_save()

    def contextMenuEvent(self, e):
        if self.on_menu:
            self.on_menu(e.globalPos())

    # ------------------------------------------------------- utils
    def move_to(self, x, y):
        st = self.state
        st.set("x", int(x))
        st.set("y", int(y))
        self.apply("final")
