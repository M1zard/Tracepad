# -*- coding: utf-8 -*-
"""Геометрия вписывания картинки в рабочую область экрана.

Центральная проблема MVP: большая картинка могла появиться «углом в центре
экрана» и вылезти за монитор. Причины были две:
  1) позиция (x,y) клампилась только сверху-слева [0..∞) — нет верхней
     границы экрана;
  2) при загрузке zoom не пересчитывался под размер экрана.

Здесь чистые функции, тестируемые без GUI:

    fit_rect(img_w, img_h, area, margin) -> (zoom, x, y)
        вписать картинку в область с полями, вернуть zoom и позицию
        ЛЕВОГО ВЕРХНЕГО угла так, чтобы картинка была по центру области.

    clamp_into(x, y, w, h, area) -> (x, y)
        сдвинуть прямоугольник внутрь рабочей области (если картинка
        больше области — прижимаем к центру по той оси, где не влезает).

    fit_zoom(img_w, img_h, area, margin) -> zoom
        только масштаб (используется при zoom>fit, чтобы не прятать
        картинку за край при колесе).

Рабочая область — кортеж (x, y, w, h) ЛЕВОГО ВЕРХНЕГО угла и размера
доступного экрана (минус панели задач, с учётом DPI-скейла — всё это
готовит platform.work_area()).
"""

from __future__ import annotations

MARGIN_RATIO = 0.94   # картинка занимает <=94% рабочей области (воздух)
MIN_ZOOM = 0.02       # абсолютный нижний предел (пиксель-арт не ломаем)
MAX_ZOOM = 12.0


def fit_zoom(img_w, img_h, area, margin_ratio=MARGIN_RATIO):
    """Масштаб, при котором картинка влезает в область целиком.

    Большие — уменьшаем; маленькие НЕ растягиваем (остаётся 1:1),
    иначе крошечный референс распухает на весь экран.
    """
    aw, ah = area[2], area[3]
    if img_w <= 0 or img_h <= 0 or aw <= 0 or ah <= 0:
        return 1.0
    z = min(aw * margin_ratio / img_w, ah * margin_ratio / img_h)
    return min(z, 1.0)


def fit_rect(img_w, img_h, area, margin_ratio=MARGIN_RATIO):
    """(zoom, x, y): картинка целиком, по центру рабочей области."""
    z = fit_zoom(img_w, img_h, area, margin_ratio)
    w, h = img_w * z, img_h * z
    ax, ay, aw, ah = area
    x = ax + (aw - w) / 2.0
    y = ay + (ah - h) / 2.0
    return z, int(x), int(y)


def clamp_into(x, y, w, h, area):
    """Сдвинуть прямоугольник в область; если не влезает — центрируем
    по переполненной оси (видны края с обеих сторон)."""
    ax, ay, aw, ah = area
    if w >= aw:                       # шире области — центр по X
        x = ax + (aw - w) / 2.0
    else:                              # иначе — внутрь
        x = min(max(x, ax), ax + aw - w)
    if h >= ah:
        y = ay + (ah - h) / 2.0
    else:
        y = min(max(y, ay), ay + ah - h)
    return int(x), int(y)


def screen_of(root):
    """Рабочая область через Tk (fallback, если платформа не дала лучше)."""
    try:
        root.update_idletasks()
        return (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
    except Exception:
        return (0, 0, 1920, 1080)
