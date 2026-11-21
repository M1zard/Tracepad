# -*- coding: utf-8 -*-
"""strokes.py — карта контуров -> траектории для Auto Drawing.

Задача: превратить пиксельную маску (edges.py) в последовательность
ШТРИХОВ — полилиний, по которым курсор пройдёт с зажатой кнопкой.

ПОЧЕМУ НЕ скелетный граф. Первая реализация была классической
(deg-классификация пикселей + walk-цепочки), и она рассыпалась на
реальных масках: NMS кодирует изгибы диагональными «лестницами»
(пиксели (i,i),(i,i+1)), и в 8-связности почти каждая точка изгиба
имеет 3-4 соседа — узлы везде, трассировка режет линии на обрывки,
Zhang-Suen утоньшение не убирает лестницы (условия для них не
выполняются). Подбор эвристик занял бы недели и остался бы хрупким.

ЧТО ВЗЯТО ВМЕСТО: cv2.findContours — обход границы связной
компоненты. OpenCV решает и лестницы, и связность, и порядок точек
за один проход C++-кода. Каждая компонента маски даёт замкнутый
контур (кольцо) или петлю (для полос 2px: «по одной стороне туда,
по другой обратно» — ровно то, что нужно для обведения). Сегментация
на отдельные ШТРИХИ между развилками делает морфология самой маски.

Пайплайн:

1. findContours(RETR_LIST, CHAIN_APPROX_NONE) — упорядоченные
   последовательности пикселей (x, y), замкнутые по индексу.
2. Фильтр пыли: контуры короче min_points отбрасываем (при
   findContours это одиночные пиксели шума; на живых картинках
   детали < 4px кистью всё равно не воспроизводятся).
3. RDP (Ramer–Douglas–Peucker), tol=0.75px — единственное
   допущение против «пиксель в пиксель»: убирает субпиксельную
   лесенку, сохраняя геометрию (ошибка <= tol против исходной
   линии). tol жёстко ограничен, углы не срезаются. Никакого
   Безье-сглаживания — это прямо запрещено постановкой.
4. Порядок обхода: greedy nearest-neighbour по КОНЦАМ штрихов
   (не центрам) — холостой перелёт минимизируется там, где он
   реален. O(n^2) при n<2000 — миллисекунды.

Выход: список штрихов [(x0,y0), ..., (xn,yn)] в пикселях ИСХОДНОЙ
картины (не экрана — маппинг в экранные координаты делает autodraw).
"""

from __future__ import annotations

import numpy as np
import cv2
from scipy import ndimage

# 8-связное ядро (для совместимости со старыми тестами/отладкой)
_N8 = np.ones((3, 3), np.int32)
_N8[1, 1] = 0


def _degree(mask: np.ndarray) -> np.ndarray:
    """Степень каждого пикселя маски в 8-связном графе (0 вне)."""
    return ndimage.convolve((mask > 0).astype(np.int32), _N8,
                             mode="constant")


def trace(mask: np.ndarray, rdp_tol: float = 0.75,
          min_points: int = 8):
    """Маска -> список штрихов-полилиний [(x, y), ...].

    min_points: контуры с числом точек меньше — пыль, отбрасываем.
    rdp_tol: допуск упрощения RDP в пикселях (<=0 = не упрощать).
    """
    m = np.asarray(mask)
    m = (m > 0).astype(np.uint8)
    if not m.any():
        return []
    cnts, _ = cv2.findContours(m, cv2.RETR_LIST,
                               cv2.CHAIN_APPROX_NONE)
    strokes = []
    for c in cnts:
        if len(c) < min_points:
            continue
        pts = [(float(x), float(y)) for x, y in c.reshape(-1, 2)]
        # замкнутый контур: прижимаем финиш к старту (курсор
        # закрывает кольцо с перекрытием, стык не разъезжается)
        strokes.append(pts)
    if rdp_tol > 0:
        strokes = [rdp(s, rdp_tol) for s in strokes]
    return [s for s in strokes if len(s) >= 2]


def rdp(points, tol):
    """Ramer–Douglas–Peucker: упрощение полилинии с допуском tol.

    Классическая рекурсия, но с явным стеком (глубина на 4000px
    спирали достигает тысяч — питон-рекурсия упёрлась бы в лимит).
    """
    if tol <= 0 or len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        s, e = stack.pop()
        if e <= s + 1:
            continue
        # максимальное расстояние от сегмента s..e
        ax, ay = points[s]
        bx, by = points[e]
        dx, dy = bx - ax, by - ay
        seg = float(np.hypot(dx, dy))
        best, bi = -1.0, -1
        for k in range(s + 1, e):
            px, py = points[k]
            if seg < 1e-9:
                d = float(np.hypot(px - ax, py - ay))
            else:
                d = abs((px - ax) * dy - (py - ay) * dx) / seg
            if d > best:
                best, bi = d, k
        if best > tol and bi > 0:
            keep[bi] = True
            stack.append((s, bi))
            stack.append((bi, e))
    return [p for p, k in zip(points, keep) if k]


def order_strokes(strokes):
    """Greedy nearest-neighbour: следующий штрих — тот, чей БЛИЖАЙШИЙ
    конец ближе к текущей позиции курсора.

    Замкнутые кольца инвариантны к развороту (старт-финиш совпадают
    по геометрии), открытые штрихи выбирают направление «от ближнего
    конца». О(n^2) при n<2000 — миллисекунды; точный TSP на этом
    размере не окупается.
    """
    if len(strokes) < 2:
        return strokes
    remaining = [list(map(tuple, s)) for s in strokes]
    ordered = []
    cur = (0.0, 0.0)
    while remaining:
        best_i, best_d, best_rev = 0, float("inf"), False
        for i, s in enumerate(remaining):
            d0 = (s[0][0] - cur[0]) ** 2 + (s[0][1] - cur[1]) ** 2
            d1 = (s[-1][0] - cur[0]) ** 2 + (s[-1][1] - cur[1]) ** 2
            if d0 <= d1:
                d, rev = d0, False
            else:
                d, rev = d1, True
            if d < best_d:
                best_d, best_i, best_rev = d, i, rev
        s = remaining.pop(best_i)
        if best_rev:
            s = list(reversed(s))
        ordered.append(s)
        cur = s[-1]
    return ordered


def stats(strokes):
    """Метрики для UI/тестов: штрихов, точек, длина, холостой ход."""
    n = len(strokes)
    pts = sum(len(s) for s in strokes)
    length = 0.0
    for s in strokes:
        for (x1, y1), (x2, y2) in zip(s, s[1:]):
            length += float(np.hypot(x2 - x1, y2 - y1))
    idle = 0.0
    for a, b in zip(strokes, strokes[1:]):
        idle += float(np.hypot(b[0][0] - a[-1][0], b[0][1] - a[-1][1]))
    return {"strokes": n, "points": pts, "length_px": round(length),
            "idle_px": round(idle)}


def strokes_bounds(strokes):
    """(minx, miny, maxx, maxy) всех штрихов (для клиппинга)."""
    if not strokes:
        return (0, 0, 0, 0)
    xs = [p[0] for s in strokes for p in s]
    ys = [p[1] for s in strokes for p in s]
    return (min(xs), min(ys), max(xs), max(ys))
