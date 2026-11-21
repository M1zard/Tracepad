# -*- coding: utf-8 -*-
"""Глобальные хоткеи, GUI-независимо.

Один модуль на обе платформы:
  Windows — RegisterHotKey в потоке с GetMessage; действия падают в deque;
  Linux   — XGrabKey через python-xlib в потоке с select по fd.

Оба варианта шлют действия в очередь, которую опрашивает главный поток
GUI (у Qt свой event loop, из потока трогать виджеты нельзя).

Схема хоткеев: Ctrl+Alt+<клавиша>, раскладко-независимая на X11
(грабим и латиницу, и кириллицу).
"""

from __future__ import annotations

import collections
import sys
import threading

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = not IS_WIN and not IS_MAC

# буква -> действие (единая карта для всех платформ)
ACTIONS = {
    "t": "opacity", "g": "grid", "c": "click_through",
    "f": "flip_x", "v": "flip_y", "b": "bw", "e": "edge",
    "r": "reset", "h": "panel", "o": "open", "q": "quit",
    "d": "draw",
}


# ============================================================================
#  Windows
# ============================================================================
if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    MOD_ALT, MOD_CONTROL = 0x0001, 0x0002
    WM_HOTKEY, WM_QUIT = 0x0312, 0x0012

    VK = {"t": 0x54, "g": 0x47, "c": 0x43, "f": 0x46, "v": 0x56,
          "b": 0x42, "e": 0x45, "r": 0x52, "h": 0x48, "o": 0x4F,
          "q": 0x51, "d": 0x44}

    class _MSG(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                    ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                    ("time", wintypes.DWORD), ("pt", wintypes.POINT)]

    class Hotkeys(threading.Thread):
        def __init__(self, q: collections.deque):
            super().__init__(daemon=True)
            self._q = q
            self._ids = {}
            self._tid = 0

        def run(self):
            self._tid = _kernel32.GetCurrentThreadId()
            for i, (ch, act) in enumerate(ACTIONS.items()):
                vk = VK.get(ch)
                if vk and _user32.RegisterHotKey(None, i + 1,
                                                MOD_CONTROL | MOD_ALT, vk):
                    self._ids[i + 1] = act
            msg = _MSG()
            while True:
                got = _user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
                if got in (0, -1):
                    break
                if msg.message == WM_HOTKEY and msg.wParam in self._ids:
                    self._q.append(self._ids[msg.wParam])

        def stop(self):
            try:
                _user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            except Exception:
                pass


# ============================================================================
#  Linux / X11
# ============================================================================
elif IS_LINUX:
    try:
        from Xlib import X, XK
        from Xlib import display as _xdisplay
        from Xlib.error import XError
        _XLIB = True
    except Exception:
        _XLIB = False

    if _XLIB:
        import select
        import time

        _CYR = {"t": "Cyrillic_e", "g": "Cyrillic_pe", "c": "Cyrillic_es",
                "f": "Cyrillic_a", "v": "Cyrillic_em", "b": "Cyrillic_i",
                "e": "Cyrillic_u", "r": "Cyrillic_ka", "h": "Cyrillic_er",
                "q": "Cyrillic_shorti", "o": "Cyrillic_che",
                "d": "Cyrillic_ve"}

        class Hotkeys(threading.Thread):
            EXTRA = (0, X.Mod2Mask, X.LockMask, X.Mod2Mask | X.LockMask)

            def __init__(self, q):
                super().__init__(daemon=True)
                self._q = q
                self._stop_ev = threading.Event()
                self._grabs = []
                self._by_code = {}
                self._d = None
                self._grabwin = None

            def _build(self):
                d = self._d
                root = d.screen().root
                self._grabwin = root
                mods = X.ControlMask | X.Mod1Mask
                for ch, act in ACTIONS.items():
                    codes = []
                    latin = getattr(XK, "XK_" + ch, 0)
                    if latin:
                        c = d.keysym_to_keycode(latin)
                        if c:
                            codes.append(c)
                    cyr = _CYR.get(ch)
                    if cyr:
                        sym = getattr(XK, "XK_" + cyr, 0)
                        if sym:
                            c = d.keysym_to_keycode(sym)
                            if c and c not in codes:
                                codes.append(c)
                    for code in codes:
                        self._by_code.setdefault(code, act)
                        for extra in self.EXTRA:
                            try:
                                root.grab_key(code, mods | extra, False,
                                              X.GrabModeAsync, X.GrabModeAsync)
                                self._grabs.append((code, mods | extra))
                            except XError:
                                pass
                d.sync()

            def run(self):
                try:
                    self._d = _xdisplay.Display()
                    self._build()
                except Exception:
                    return
                d, fd = self._d, self._d.fileno()
                down = set()
                while not self._stop_ev.is_set():
                    try:
                        r, _, _ = select.select([fd], [], [], 0.25)
                        if r:
                            d.sync()
                            while d.pending_events():
                                ev = d.next_event()
                                if ev.type == X.KeyPress:
                                    if ev.detail in down:
                                        continue
                                    down.add(ev.detail)
                                    if ev.state & (X.ControlMask | X.Mod1Mask) != \
                                            (X.ControlMask | X.Mod1Mask):
                                        continue
                                    act = self._by_code.get(ev.detail)
                                    if act:
                                        self._q.append(act)
                                elif ev.type == X.KeyRelease:
                                    down.discard(ev.detail)
                    except Exception:
                        time.sleep(0.1)
                self._ungrab()

            def _ungrab(self):
                try:
                    d = self._d
                    if d is not None:
                        for code, mods in self._grabs:
                            try:
                                d.ungrab_key(code, mods, self._grabwin)
                            except Exception:
                                pass
                        self._grabs = []
                        d.sync()
                except Exception:
                    pass

            def stop(self):
                """Снять грабы немедленно (главный поток) + разбудить поток."""
                self._stop_ev.set()
                self._ungrab()

    else:
        Hotkeys = None

else:
    Hotkeys = None


def make_hotkeys(q: collections.deque):
    """Запустить слушатель хоткеев; None, если платформа не даёт."""
    if IS_WIN or (IS_LINUX and _XLIB):
        try:
            h = Hotkeys(q)
            h.start()
            return h
        except Exception:
            return None
    return None


def supports_global_hotkeys():
    if IS_WIN or (IS_LINUX and _XLIB):
        return True, ""
    if IS_LINUX:
        return False, "поставь python-xlib (pip install python-xlib) для глобальных хоткеев"
    if IS_MAC:
        return False, "на macOS глобальные хоткеи не поддерживаются"
    return False, ""
