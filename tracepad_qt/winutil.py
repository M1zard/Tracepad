# -*- coding: utf-8 -*-
"""Нативные возможности окна для оверлея (GUI-независимо).

Отдельно от Qt, чтобы при смене GUI-стека не трогать платформенные
механизмы. Qt-слой вызывает эти функции, передавая winId виджета.

  Windows: WS_EX_TRANSPARENT (клик-сквозь) + WS_EX_TOOLWINDOW
           (не в панели задач) + рабочая область через SPI_GETWORKAREA
           с DPI-awareness;
  Linux:   XShape input region (клик-сквозь), _NET_WORKAREA;
  macOS:   заглушки (возврат без изменений).
"""

from __future__ import annotations

import sys

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = not IS_WIN and not IS_MAC

if IS_LINUX:
    try:
        from Xlib import X
        from Xlib import display as _xdisplay
        from Xlib.ext.shape import SO, SK
        from Xlib.error import XError
        _XLIB = True
    except Exception:
        _XLIB = False
else:
    _XLIB = False


# ============================================================================
#  Windows
# ============================================================================
if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    HWND_TOPMOST = wintypes.HWND(-1)
    SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
    SPI_GETWORKAREA = 0x0030

    class _RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    def work_area():
        """Рабочая область первичного монитора (x, y, w, h)."""
        try:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                _user32.SetProcessDPIAware()
            r = _RECT()
            if _user32.SystemParametersInfoW(
                    SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
                return (r.left, r.top, r.right - r.left, r.bottom - r.top)
        except Exception:
            pass
        return None

    def click_through(hwnd, enable, w=None, h=None):
        try:
            style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if enable:
                _user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                       style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
            else:
                _user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                       style & ~WS_EX_TRANSPARENT)
            return True
        except Exception:
            return False

    def toolwindow(hwnd):
        try:
            style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            _user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                   style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        except Exception:
            pass

    def topmost(hwnd, on=True):
        try:
            _user32.SetWindowPos(hwnd, HWND_TOPMOST if on else None,
                                 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception:
            pass


# ============================================================================
#  Linux / X11
# ============================================================================
elif IS_LINUX and _XLIB:
    _d = None

    def _disp():
        global _d
        if _d is None:
            _d = _xdisplay.Display()
        return _d

    def work_area():
        try:
            d = _disp()
            r = d.screen().root
            prop = r.get_full_property(d.intern_atom("_NET_WORKAREA"), 0)
            if prop and prop.value and len(prop.value) >= 4:
                ax, ay, aw, ah = [int(v) for v in prop.value[:4]]
                return (ax, ay, aw, ah)
        except Exception:
            pass
        return None

    def _wrapper(hwnd):
        d = _disp()
        win = d.create_resource_object("window", int(hwnd))
        return win.query_tree().parent

    def click_through(hwnd, enable, w=None, h=None):
        try:
            d = _disp()
            win = _wrapper(hwnd)
            if enable:
                win.shape_rectangles(SO.Set, SK.Input, X.Unsorted,
                                      0, 0, [])
            else:
                if not w or not h:
                    w, h = 200, 200
                win.shape_rectangles(SO.Set, SK.Input, X.Unsorted, 0, 0,
                                     [(0, 0, int(w), int(h))])
            d.flush()
            return True
        except Exception:
            return False

    def toolwindow(hwnd):
        pass

    def topmost(hwnd, on=True):
        try:
            d = _disp()
            win = _wrapper(hwnd)
            win.configure(stack_mode=X.Above if on else X.Below)
            d.flush()
        except Exception:
            pass


# ============================================================================
#  macOS / fallback
# ============================================================================
else:
    def work_area():
        return None

    def click_through(hwnd, enable, w=None, h=None):
        return False

    def toolwindow(hwnd):
        pass

    def topmost(hwnd, on=True):
        pass


def supports_click_through():
    if IS_WIN:
        return True, ""
    if IS_LINUX and _XLIB:
        return True, ""
    if IS_LINUX:
        return False, ("клик-сквозь: поставь python-xlib "
                       "(pip install python-xlib)")
    if IS_MAC:
        return False, "на macOS клик-сквозь этой версией не поддерживается"
    return False, ""
