# -*- coding: utf-8 -*-
"""Состояние приложения: единый источник правды + persistence.

Ключевое отличие от MVP: state живёт в одном объекте с inotify-колбэками.
UI-виджеты подписываются на ключи, а не тянут значения из разрозненных
Tk-переменных. Это убирает двойную синхронизацию (было: state <-> IntVar
<-> чекбокс) и связку trace_add, которая плодила циклы обновлений.

Persistence: JSON в ~/.config (Linux) / %APPDATA% (Windows). Путь к картинке
и позиция сохраняются только по явному запросу (debounce) — не на каждый чих.
"""

from __future__ import annotations

import json
import os
import sys

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


def _state_dir():
    if IS_WIN:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "TracePad")
    if IS_MAC:
        return os.path.expanduser("~/Library/Application Support/TracePad")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "tracepad")


DEFAULTS = {
    "image": "",
    "opacity": 0.72,
    "zoom": 1.0,
    "x": 0,
    "y": 0,
    "click_through": False,
    "grid": False,
    "flip_x": False,
    "flip_y": False,
    "bw": False,
    "edge": False,
    "edge_strength": 50,         # 0..100 (edges.py: чувствительность)
    "grid_size": 48,
    "panel_x": None,
    "panel_y": None,
    "panel_collapsed": False,
    # Auto Drawing
    "draw_speed": "normal",     # slow | normal | fast
    "draw_minlen": 8,            # px: штрихи короче не обводим
}


class State:
    """Значения + подписки. set(key, val) -> True если изменилось."""

    def __init__(self, data=None):
        self._d = dict(DEFAULTS)
        if data:
            for k in DEFAULTS:
                if k in data:
                    self._d[k] = data[k]
        self._subs = {}          # key -> {id: cb}
        self._sub_seq = 0
        self.path = os.path.join(_state_dir(), "state.json")

    # --- значения -----------------------------------------------------------
    def get(self, key, default=None):
        return self._d.get(key, DEFAULTS.get(key, default))

    def as_dict(self):
        return dict(self._d)

    def set(self, key, val):
        if self._d.get(key) == val:
            return False
        self._d[key] = val
        self._notify(key, val, explicit=True)
        return True

    # --- подписки -----------------------------------------------------------
    def subscribe(self, key, cb):
        """cb(val, explicit) — explicit=True если источник сам пользователь.
        Возвращает token для отписки."""
        self._sub_seq += 1
        tok = self._sub_seq
        self._subs.setdefault(key, {})[tok] = cb
        cb(self._d.get(key), False)   # сразу синхронизируем виджет
        return (key, tok)

    def unsubscribe(self, token):
        key, tok = token
        self._subs.get(key, {}).pop(tok, None)

    def _notify(self, key, val, explicit):
        for cb in list(self._subs.get(key, {}).values()):
            try:
                cb(val, explicit)
            except Exception:
                pass

    # --- persistence ---------------------------------------------------------
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for k in DEFAULTS:
                if k in saved:
                    self._d[k] = saved[k]
        except Exception:
            pass
        return self

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._d, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
