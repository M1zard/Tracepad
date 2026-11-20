#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TracePad — картинка-подложка поверх окон для срисовки.

Точка входа. Логика — в пакете tracepad_qt рядом с этим файлом
(PyInstaller-friendly: sys.path правится только при обычном запуске).

Запуск:  python tracepad.py [путь/к/картинке]
Зависимости: PySide6 (pip install PySide6), Pillow;
             на Linux ещё python-xlib (клик-сквозь и глоб. хоткеи).
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def main():
    from tracepad_qt.app import main as app_main
    app_main()


if __name__ == "__main__":
    main()
