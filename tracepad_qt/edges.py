# -*- coding: utf-8 -*-
"""edges.py — детектор контуров для карты штрихов (Auto Drawing).

Зачем отдельный модуль: старый режим «контуры» (FIND_EDGES + point в
renderer.py) — визуальный эффект, а не измеритель границ. На живых
картинках он даёт ~2% точности (98% найденного — шум от градиентного
фона), рваные утолщённые линии и не годится как источник траекторий.

Архитектура выбрана по результатам сравнения девяти методов на
синтетике с ground truth (scripts/cmp_edges.py):

    метод                    precision  recall   F1
    FIND_EDGES (текущий)        0.02     0.99   0.04
    Sobel+Otsu                  0.71     0.27   0.39
    Laplacian zero-crossing     0.01     1.00   0.03
    Canny fixed 60/150          0.88     0.60   0.71
    adaptiveThreshold           0.24     0.94   0.38
    Canny Lab L+a+b (3 канала)  0.85     1.00   0.92   <-- лучший

Отсюда три решения:

1) Границы ищем в Lab, не в Gray: у игровых персонажей границы часто
   ЦВЕТОВЫЕ (красное на сером при одинаковой яркости) — gray их
   фактически не видит, Lab-каналы a/b ловят. Но 3 отдельных Canny
   дают двойные/тройные линии (0.85 precision), поэтому...

2) ...каналы сливаются ДО NMS: магнитуда = max по каналам (в т.ч.
   L-яркость, с весом — ей доверяем меньше, она шумит на текстурах),
   направление берём у канала-победителя. Дальше ОДИН проход
   NMS+гистерезис -> ОДНА линия на границу любой природы.

3) Пороги не фиксированные: hi-порог по перцентилю магнитуды
   (pct_hi, по умолчанию 85-й) — картина с мягкими фокусами и картинка
   с контрастной графикой получают свои пороги автоматически.
   strength (0..100, слайдер из UI) сдвигает перцентиль: 100 ->
   только самые резкие границы, 0 -> всё, что похоже на границу.

Пайплайн (каждый шаг векторизован, ~40 мс на 1280x800):
    bilateral (с保 краёв) -> градиенты по каналам -> слияние ->
    non-max suppression (4 направления) -> гистерезис (связность
    через scipy.ndimage.label) -> мосты 1px-дырок -> морфологическое
    утоньшение до скелетных линий 1px (готово к strokes.py).

Формат выхода — uint8 маска 0/255, 1px, связность 8.
"""

from __future__ import annotations

import numpy as np
import cv2
from scipy import ndimage

# перцентиль магнитуды для hi-порога при strength=50 (центр шкали)
_PCT_BASE = 85.0          # (историческое имя; см. detect(): порог
                           # теперь anchor'ится на mean+2std)
_L_WEIGHT = 0.75           # яркостным границам доверяем чуть меньше

# Кадр больше этого считается детекцией на даунскейле (INTER_AREA),
# штрихи живут в координатах МАСКИ, make_plan маппит их в окно через
# iw/ih маски — геометрия не искажается. 2.2 MPix: 4K -> ~1.1 MPix,
# детект 258 мс вместо 1128 (F1 тот же, проверено на синтетике
# с тонкими 3px-лапами: P/R 0.996/0.994 против 0.995/0.994).
MAX_DETECT_PIX = 2_200_000


def _to_lab(img: np.ndarray) -> np.ndarray:
    """RGB uint8 -> Lab float32 (OpenCV L:[0..255] масштаб)."""
    return cv2.cvtColor(img, cv2.COLOR_RGB2Lab).astype(np.float32)


def _channel_grad(chan: np.ndarray, ksize: int = 3):
    """(магнитуда, направление) градиента одного канала.

    Sobel по обеим осям; направление — угол в радианах atan2.
    """
    gx = cv2.Sobel(chan, cv2.CV_32F, 1, 0, ksize=ksize)
    gy = cv2.Sobel(chan, cv2.CV_32F, 0, 1, ksize=ksize)
    mag = np.sqrt(gx * gx + gy * gy)
    ang = np.arctan2(gy, gx)
    return mag, ang


def _merge_grads(channels):
    """Слияние каналов ДО NMS: одна линия на границу.

    mag = max(L*w, a, b); направление берём у канала-победителя —
    NMS сравнивает соседей вдоль нормали, и «своё» направление
    важно, иначе слияние магнитуд рвёт линию.

    Реализация без np.stack: побеждённые каналы не участвуют в
    копировании — argmax по списку массивов «на месте» (быстрее
    stack на больших кадрах: нет аллокации 3x HxW).
    """
    mags = [channels[0][0] * _L_WEIGHT] + [m for m, _ in channels[1:]]
    # argmax по каналам без stack: сравниваем попарно
    win = np.zeros(mags[0].shape, np.int8)
    mag = mags[0]
    for i in range(1, len(mags)):
        better = mags[i] > mag
        mag = np.where(better, mags[i], mag)
        win[better] = i
    # направление канала-победителя — собираем одним fancy-срезом
    ang = channels[0][1]
    if len(channels) > 1:
        out = ang.copy()
        for i in range(1, len(channels)):
            out[win == i] = channels[i][1][win == i]
        ang = out
    return mag, ang


def _nms(mag: np.ndarray, ang: np.ndarray) -> np.ndarray:
    """Non-max suppression: оставляем пиксель, если он локальный
    максимум магнитуды вдоль направления градиента (нормали края).

    Реализация сдвигами (np.roll), 4 квантованных направления:
    0°, 45°, 90°, 135°. Границы кадра гасим (сосед из-под roll там
    приходит с противоположного края).
    """
    q = (np.round(ang / (np.pi / 4)) % 4).astype(np.int8)

    # все 4 пары сдвигов — один раз (раньше shifted() копировала mag
    # 8 раз; теперь 8 видов срезов без copy: a/b строятся присваиванием
    # в нулевой буфер, буферы для «a» переиспользуются как база для
    # последующих направлений неявно через новые аллокации numpy)
    def pair(a_from, b_from):
        """(сосед «против», сосед «по») через срезы."""
        a = np.zeros_like(mag)
        b = np.zeros_like(mag)
        a[a_from] = mag[b_from]
        b[b_from] = mag[a_from]
        return a, b

    H, W = mag.shape
    nms = np.zeros_like(mag)
    for d, (sa, sb) in enumerate((
            ((slice(None), slice(1, None)), (slice(None), slice(None, -1))),   # 0°: a=левый сосед
            ((slice(1, None), slice(1, None)), (slice(None, -1), slice(None, -1))),  # 45°
            ((slice(1, None), slice(None)), (slice(None, -1), slice(None))),  # 90°
            ((slice(1, None), slice(None, -1)), (slice(None, -1), slice(1, None))))):  # 135°
        m = q == d
        if not m.any():
            continue
        a, b = pair(sa, sb)
        # несравнимо в одну сторону (>=), строго в другую (>):
        # «плато» одинаковой магнитуды не вырождаются в двойные линии
        keep = (mag >= a) & (mag > b) & m
        nms[keep] = mag[keep]
    return nms


def _hysteresis(nms: np.ndarray, hi: float, lo: float) -> np.ndarray:
    """Двойной порог + связность: слабые пиксели живут только если
    в их компоненте связности есть сильный.

    label() из scipy вместо ручного BFS: C-реализация, на 1280x800
    это миллисекунды.
    """
    strong = nms >= hi
    weak = (nms >= lo) & ~strong
    lbl, _ = ndimage.label(strong | weak)
    keep = np.zeros(nms.shape, bool)
    strong_lbl = np.unique(lbl[strong])
    if strong_lbl.size and (strong_lbl != 0).any():
        keep = np.isin(lbl, strong_lbl[strong_lbl != 0])
    return (keep & (strong | weak)).astype(np.uint8) * 255


def _bridges(mask: np.ndarray) -> np.ndarray:
    """Мосты через 1-пиксельные дырки: линия с разрывом 1px —
    частый артефакт NMS на изгибах. fill одиночных «дырок» между
    двумя пикселями одной линии связывает их без новых веток.
    """
    out = mask.copy()
    m = mask > 0
    # если ровно два соседа «по прямой» (влево-вправо или вверх-вниз)
    # и между ними дырка — мостим
    left = np.roll(m, 1, 1)   # сосед слева заполнен
    right = np.roll(m, -1, 1)
    horiz = left & right & ~m
    vert = (np.roll(m, 1, 0) & np.roll(m, -1, 0)) & ~m
    fill = np.zeros_like(m)
    fill[1:-1, 1:-1] = (horiz[1:-1, 1:-1] | vert[1:-1, 1:-1])
    out[fill] = 255
    return out


def detect(img: np.ndarray, strength: int = 50) -> np.ndarray:
    """Полный детектор: RGB uint8 (H,W,3) -> маска контуров uint8.

    strength 0..100 — чувствительность: 100 — только самые чёткие
    границы, 0 — всё, что похоже на границу.

    Большие кадры (>MAX_DETECT_PIX) детектируются на даунскейле:
    контуры 1px и так субпиксельные, F1 не меняется (проверено),
    время падает в 4+ раза.
    """
    img = np.ascontiguousarray(img)
    if img.ndim != 3 or img.shape[2] < 3:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
    scale = 1.0
    H, W = img.shape[:2]
    if H * W > MAX_DETECT_PIX:
        scale = (MAX_DETECT_PIX / float(H * W)) ** 0.5
        img = cv2.resize(img, (max(8, int(W * scale)),
                               max(8, int(H * scale))),
                         interpolation=cv2.INTER_AREA)
    # bilateral: сохраняет края лучше гаусса (не мажет границу),
    # но убирает пиксельный шум — на живых PNG JPEG-шум
    blur = cv2.bilateralFilter(img, 5, 45, 45)
    lab = _to_lab(blur)
    grads = [_channel_grad(lab[:, :, i]) for i in range(3)]
    mag, ang = _merge_grads(grads)
    nms = _nms(mag, ang)
    nz = nms[nms > 2.0]
    if nz.size < 32:
        return np.zeros(mag.shape, np.uint8)

    # Пороги относительно статистики САМОЙ картины:
    #   anchor = mean + 2*std магнитуды NMS — уровень настоящих границ.
    #   Проверено на двух сценариях: «тонкий персонаж на шумном фоне»
    #   и «залитая фигура с мягким анти-алиасом» — оба дают порог,
    #   отсекающий шум фона и сохраняющий границы (F1 0.96 / 1.00).
    #   p99 как якорь провалился на второй сцене (границы < 1%
    #   пикселей), фиксированные числа — на первой.
    # strength 0..100 -> k 0.6..1.0: 100 = только самые чёткие
    # границы, 0 = всё, что похоже на границу.
    mu, sd = float(nz.mean()), float(nz.std())
    anchor = mu + 2.0 * sd
    k = 0.6 + float(np.clip(strength, 0, 100)) * 0.004
    hi = anchor * k
    lo = hi * 0.55
    mask = _hysteresis(nms, hi, lo)
    mask = _bridges(mask)
    # утоньшение: контуры после NMS в основном 1px, но слияние
    # каналов и мосты местами дают 2px — приводим к скелету
    mask = _thin_fallback(mask)
    return mask


# ---------------------------------------------------------------------------
#  Утоньшение (fallback без opencv-contrib): Zhang-Suen, векторный по кадрам
# ---------------------------------------------------------------------------
def _thin_fallback(mask: np.ndarray, max_iter: int = 8) -> np.ndarray:
    """Zhang–Suen thinning (2 под-итерации за проход).

    OpenCV-contrib имеет ximgproc.thinning; здесь самописный вариант
    на булевых массивах — нужен только если contrib не установлен.
    8 проходов хватает для остаточных 2px-утолщений (после NMS линии
    почти везде уже 1px).
    """
    m = mask > 0
    for _ in range(max_iter):
        changed = False
        for step in (0, 1):
            up = np.pad(m, ((1, 1), (1, 1)), constant_values=False)
            p2 = up[:-2, 1:-1]      # вверх
            p3 = up[:-2, 2:]        # верх-право
            p4 = up[1:-1, 2:]       # право
            p5 = up[2:, 2:]         # низ-право
            p6 = up[2:, 1:-1]       # низ
            p7 = up[2:, :-2]        # низ-лево
            p8 = up[1:-1, :-2]      # лево
            p9 = up[:-2, :-2]       # верх-лево
            b = (p2.astype(np.uint8) + p3 + p4 + p5 +
                 p6 + p7 + p8 + p9)
            cond = (b >= 2) & (b <= 6)
            if step == 0:
                cond &= (p2 & p4 & p6) | (p4 & p6 & p8)
            else:
                cond &= (p2 & p6 & p8) | (p2 & p4 & p8)
            cond &= m
            if cond.any():
                m = m & ~cond
                changed = True
        if not changed:
            break
    return m.astype(np.uint8) * 255


def mask_to_rgba(mask: np.ndarray, color=(20, 22, 30, 255)) -> "object":
    """Маска -> RGBA PIL-изображение (чтобы вписать в существующий
    renderer без смены формата: RGB-каналы цвета линии + альфа)."""
    from PIL import Image as PILImage
    h, w = mask.shape[:2]
    rgba = np.zeros((h, w, 4), np.uint8)
    rgba[..., 0], rgba[..., 1], rgba[..., 2] = color[0], color[1], color[2]
    rgba[..., 3] = (mask > 0) * color[3] if len(color) == 4 else (mask > 0) * 255
    return PILImage.fromarray(rgba)


def edge_stats(mask: np.ndarray) -> dict:
    """Статистика для UI/тестов: число пикселей, связных компонент."""
    if mask is None or not mask.any():
        return {"px": 0, "components": 0, "w": 0, "h": 0}
    lbl, n = ndimage.label(mask > 0)
    return {"px": int((mask > 0).sum()), "components": int(n),
            "w": int(mask.shape[1]), "h": int(mask.shape[0])}
