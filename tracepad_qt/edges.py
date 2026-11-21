# -*- coding: utf-8 -*-
"""edges.py — детектор контуров для карты штрихов (Auto Drawing), v2.

Полная ревизия по результатам сравнительных экспериментов на корпусе
из 20 синтетических сцен с ground truth + 7 деградированных вариантов
(шум/JPEG/блюр/контраст/яркость/масштаб) и на реальных изображениях.

Ключевые дефекты старой версии, исправленные здесь (подробности в
отчёте, раздел B):

1. _thin_fallback: условия Zhang–Suen искажены (произведения соседей
   заменены на ИЛИ, число переходов A(P1)=1 не проверялось вовсе) —
   thinning РВАЛ замкнутые контуры на десятки компонент (круг 3px ->
   38 компонент вместо 1).
2. ndimage.label() без structure = 4-связность: диагональные контуры
   считались разорванными.
3. NMS через np.roll заносил соседей с противоположной стороны кадра —
   ложные линии на кромке изображения.
4. _bridges() клеил две НЕЗАВИСИМЫЕ параллельные линии, если между
   ними 1px фона.
5. detect() падал на RGBA (bilateralFilter требует 8UC1/8UC3).
6. Порог mean+2*std завышался на шумных изображениях (хвост шумовых
   магнитуд раздувает std) — заявленные F1 0.96/1.00 на широком корпусе
   не воспроизводились (фактически 0.33/0.76).

Выбранная архитектура (экспериментально, раздел C-D отчёта):

    normalize (RGBA/BGR/gray -> RGB, downscale >MAX_DETECT_PIX)
    -> Gauss 3x3 σ0.8 (bF1 0.938; эквивалент bilateral при меньшей цене)
    -> Lab (ловит изо-luma цветовые границы; RGB чуть слабее, gray
       теряет их полностью: bF1 0.93 vs 0.93 vs 0.915 при чистом gray)
    -> Scharr по каналам (точнее Sobel на тонких структурах)
    -> color structure tensor (Di Zenzo): Jxx/Jyy/Jxy с гауссовым
       сглаживанием, λ+ как магнитуда — единственная линия на границе
       и стабильное направление (winner-merge давал нестабильные
       направления: pixel F1 0.37 против 0.50 у тензора)
    -> NMS с билинейной интерполяцией соседей (4 сектора, eps
       tie-break против плато квантования) и replicate-дополнением
       на кромках (нет ложных контуров по краям кадра)
    -> порог alpha * p99.5(магнитуды NMS>0): робастный якорь к
       максимуму РЕАЛЬНЫХ границ сцены (mean+2std и otsu проверены и
       отклонены: 0.16/0.83 bF1 против 0.94)
    -> гистерезис 8-связный (scipy.ndimage.label, structure=8)
    -> удаление изолированных пикселей (не компонент! крупные малые —
       это глаза/кнопки: удаление компонент <5px теряло bF1 0.92->0.90)
    -> Zhang–Suen (канонический, A(P1)=1 + произведения) — только
       когда линия реально толще 1px
    -> upscale до исходного размера (ближайший сосед, координатная
       сетка сохраняется для make_plan).

Выход: uint8 маска 0/255, 1px, 8-связность, размер = размеру входа.

Публичный API совместим со старой версией:
    detect(img, strength=50) -> np.ndarray
    mask_to_rgba(mask, color) -> PIL.Image
    edge_stats(mask) -> dict
    MAX_DETECT_PIX
"""

from __future__ import annotations

import numpy as np
import cv2
from scipy import ndimage

__all__ = ["detect", "mask_to_rgba", "edge_stats", "MAX_DETECT_PIX",
           "EdgeConfig"]

# Кадр больше этого числа пикселей детектируется на даунскейле
# (INTER_AREA), маска возвращается в ИСХОДНОМ разрешении (upscale NN).
MAX_DETECT_PIX = 2_200_000

# 8-связность для контуров: диагональные линии связны.
_STRUCTURE8 = np.ones((3, 3), bool)

# strength (0..100) -> доля порога от p99.5 магнитуды.
# Подобрано по монотонности px(strength) на всём корпусе:
# 0 -> alpha 0.18 (максимум чувствительности),
# 100 -> alpha 0.45 (только выраженные границы).
_ALPHA_MIN, _ALPHA_MAX = 0.18, 0.45

# Отношение lo/hi порогов гистерезиса.
_LO_RATIO = 0.4


class EdgeConfig:
    """Параметры пайплайна (все значения подобраны экспериментально).

    Атрибуты:
        alpha_min/alpha_max: границы силы порога для strength 0/100;
        lo_ratio: отношение слабого порога к сильному;
        tensor_sigma: сглаживание structure tensor (стабилизация
            направления на изогнутых границах);
        nms_eps: допуск tie-break против плато квантования;
        max_thin_px: если после гистерезиса нет утолщений больше
            1px — thinning не запускается (лишняя операция вредит).
    """

    __slots__ = ("alpha_min", "alpha_max", "lo_ratio", "tensor_sigma",
                 "nms_eps", "max_thin_iter")

    def __init__(self, alpha_min: float = _ALPHA_MIN,
                 alpha_max: float = _ALPHA_MAX, lo_ratio: float = _LO_RATIO,
                 tensor_sigma: float = 1.0, nms_eps: float = 1e-3,
                 max_thin_iter: int = 30):
        self.alpha_min = alpha_min
        self.alpha_max = alpha_max
        self.lo_ratio = lo_ratio
        self.tensor_sigma = tensor_sigma
        self.nms_eps = nms_eps
        self.max_thin_iter = max_thin_iter


# ---------------------------------------------------------------------------
# 1. Нормализация входа
# ---------------------------------------------------------------------------
def _normalize(img: np.ndarray) -> tuple[np.ndarray, float]:
    """Любой поддерживаемый вход -> RGB uint8 C-contiguous + scale.

    Принимает RGB/RGBA (4 канала, альфа игнорируется), BGR-подобные
    3-канальные (каналы 3/4 трактуются как RGB), grayscale 2D и 3D,
    float [0..1]. Возвращает (rgb, scale), scale = во сколько раз
    кадр уменьшен для детекции (маска вернётся в исходном размере).
    """
    img = np.asarray(img)
    if img.ndim == 2:
        rgb = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
    elif img.ndim == 3 and img.shape[2] == 1:
        rgb = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
    elif img.ndim == 3 and img.shape[2] in (3, 4):
        if img.dtype != np.uint8:
            img = np.clip(img, 0, 1)
            img = (img * 255.0 + 0.5).astype(np.uint8) if img.max() <= 1.0 \
                else np.clip(img, 0, 255).astype(np.uint8)
        img = np.ascontiguousarray(img)
        if img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_RGBA2RGB)
        rgb = img
    else:
        raise ValueError(f"неподдерживаемый формат изображения: "
                         f"shape={img.shape}, dtype={img.dtype}")
    if rgb.dtype != np.uint8:
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    H, W = rgb.shape[:2]
    scale = 1.0
    if H * W > MAX_DETECT_PIX:
        scale = (MAX_DETECT_PIX / float(H * W)) ** 0.5
        rgb = cv2.resize(rgb, (max(8, int(round(W * scale))),
                               max(8, int(round(H * scale)))),
                         interpolation=cv2.INTER_AREA)
    return np.ascontiguousarray(rgb), scale


# ---------------------------------------------------------------------------
# 2. Градиенты и structure tensor
# ---------------------------------------------------------------------------
def _lab_channels(rgb: np.ndarray) -> list[np.ndarray]:
    """RGB -> каналы Lab (float32)."""
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2Lab)
    return [lab[:, :, i].astype(np.float32) for i in range(3)]


def _structure_tensor(channels: list[np.ndarray],
                      sigma: float) -> tuple[np.ndarray, np.ndarray]:
    """Color structure tensor (Di Zenzo/Cumani) -> (магнитуда, угол).

    Jxx = Σ gx_i², Jyy = Σ gy_i², Jxy = Σ gx_i gy_i  (по каналам Lab),
    с гауссовым сглаживанием компонент. λ+ = ((Jxx+Jyy) + √disc)/2 —
    магнитуда границы; направление нормали — 0.5·atan2(2Jxy, Jxx−Jyy).
    Уступает winner-merge по направлению только на изолированном
    канале; на многоцветных границах стабильно даёт ОДНУ линию.
    """
    Jxx = np.zeros(channels[0].shape, np.float32)
    Jyy = np.zeros_like(Jxx)
    Jxy = np.zeros_like(Jxx)
    for ch in channels:
        gx = cv2.Scharr(ch, cv2.CV_32F, 1, 0)
        gy = cv2.Scharr(ch, cv2.CV_32F, 0, 1)
        Jxx += gx * gx
        Jyy += gy * gy
        Jxy += gx * gy
    Jxx = cv2.GaussianBlur(Jxx, (0, 0), sigma)
    Jyy = cv2.GaussianBlur(Jyy, (0, 0), sigma)
    Jxy = cv2.GaussianBlur(Jxy, (0, 0), sigma)
    tr = Jxx + Jyy
    det = Jxx * Jyy - Jxy * Jxy
    disc = np.sqrt(np.maximum(0.0, tr * tr * 0.25 - det))
    lam1 = tr * 0.5 + disc           # λ+
    mag = np.sqrt(np.maximum(lam1, 0.0))
    ang = 0.5 * np.arctan2(2.0 * Jxy, Jxx - Jyy)  # нормаль границы
    return mag, ang


# ---------------------------------------------------------------------------
# 3. NMS с интерполяцией
# ---------------------------------------------------------------------------
def _nms(mag: np.ndarray, ang: np.ndarray, eps: float) -> np.ndarray:
    """Canny-NMS: 4 сектора, билинейная интерполяция соседей вдоль
    нормали, replicate-дополнение на кромках, eps tie-break.

    Соседи считаются по ЗНАКАМ cos/sin нормали (угол из atan2, без
    свёртки mod pi: у нормалей d≈0 и d≈π знаки противоположны —
    свёртывание даёт неверную пару соседей и разрывает контур).
    Пиксель проходит, если он >= соседа «против» и > соседа «по
    направлению» (полустрогое правило против двойных линий на
    плато); eps подавляет плато от квантования float32.
    """
    H, W = mag.shape
    pad = np.pad(mag, 1, mode="edge")  # replicate: кромка не даёт ложных линий
    up, down = pad[:-2, 1:-1], pad[2:, 1:-1]
    left, right = pad[1:-1, :-2], pad[1:-1, 2:]
    up_l, up_r = pad[:-2, :-2], pad[:-2, 2:]
    dn_l, dn_r = pad[2:, :-2], pad[2:, 2:]
    tol = eps * np.maximum(1.0, mag)
    keep = np.zeros(mag.shape, bool)

    # компоненты единичной нормали (знак важен)
    nx, ny = np.cos(ang), np.sin(ang)

    # Сектор 0: |ny| <= |nx| — нормаль ближе к горизонтали.
    m = np.abs(ny) <= np.abs(nx)
    if m.any():
        # доля смещения по y при шаге 1 по x
        w = np.abs(ny) / np.maximum(np.abs(nx), 1e-9)
        # смещение по y в сторону нормали: sign(ny)
        sy = np.sign(ny)
        y_off_up = (sy <= 0)   # нормаль вверх (в коорд. изображений y вниз)
        n_neg_x = np.where(nx >= 0, left, right)
        n_pos_x = np.where(nx >= 0, right, left)
        # сосед «против нормали» и «по нормали» с интерполяцией по y
        a_neg = np.where(y_off_up, up, down)
        a_pos = np.where(y_off_up, down, up)
        n_neg = (1 - w) * n_neg_x + w * a_neg
        n_pos = (1 - w) * n_pos_x + w * a_pos
        keep |= m & (mag >= n_neg - tol) & (mag > n_pos + tol)

    # Сектор 2: |ny| > |nx| — нормаль ближе к вертикали.
    m = np.abs(ny) > np.abs(nx)
    if m.any():
        w = np.abs(nx) / np.maximum(np.abs(ny), 1e-9)
        sx = np.sign(nx)
        x_off_left = (sx <= 0)
        n_neg_y = np.where(ny >= 0, up, down)
        n_pos_y = np.where(ny >= 0, down, up)
        b_neg = np.where(x_off_left, left, right)
        b_pos = np.where(x_off_left, right, left)
        n_neg = (1 - w) * n_neg_y + w * b_neg
        n_pos = (1 - w) * n_pos_y + w * b_pos
        keep |= m & (mag >= n_neg - tol) & (mag > n_pos + tol)

    return np.where(keep, mag, 0.0)


# ---------------------------------------------------------------------------
# 4. Порог и гистерезис
# ---------------------------------------------------------------------------
def _thresholds(nms: np.ndarray, cfg: EdgeConfig,
                strength: int) -> tuple[float, float]:
    """alpha·p99.5(магнитуды NMS) — робастный якорь к уровню настоящих
    границ сцены; strength линейно сдвигает alpha в [0.18, 0.45]."""
    alpha = cfg.alpha_min + (cfg.alpha_max - cfg.alpha_min) * \
        float(np.clip(strength, 0, 100)) / 100.0
    nz = nms[nms > 0]
    hi = float(np.percentile(nz, 99.5)) * alpha
    return hi, hi * cfg.lo_ratio


def _hysteresis(nms: np.ndarray, hi: float, lo: float) -> np.ndarray:
    """Двойной порог + связность: слабые выживают только в компоненте
    с сильным пикселем. 8-связность (диагональные контуры связны)."""
    strong = nms >= hi
    weak = (nms >= lo) & ~strong
    lbl, _ = ndimage.label(strong | weak, structure=_STRUCTURE8)
    out = np.zeros(nms.shape, bool)
    if strong.any():
        ids = np.unique(lbl[strong])
        ids = ids[ids != 0]
        if ids.size:
            out = np.isin(lbl, ids)
    return out.astype(np.uint8) * 255


# ---------------------------------------------------------------------------
# 5. Артефакты: изолированные пиксели и двойные линии
# ---------------------------------------------------------------------------
def _drop_isolated(mask: np.ndarray) -> np.ndarray:
    """Удаление пикселей без единого 8-соседа (не могут быть
    траекторией). Компоненты НЕ удаляем: малый контур — часто глаз,
    кнопка, деталь (эксперимент: удаление <5px теряет bF1 0.94→0.90)."""
    m = mask > 0
    if not m.any():
        return mask
    nb = ndimage.convolve(m.astype(np.uint8), np.ones((3, 3), np.uint8))
    nb = nb - m.astype(np.uint8)
    return (m & (nb > 0)).astype(np.uint8) * 255


def _anti_double(mask: np.ndarray, ang: np.ndarray,
                 sgn: np.ndarray, max_dist: int = 2,
                 ang_tol: float = 0.4) -> np.ndarray:
    """Слияние пар противоположных кромок тонкого штриха (1-3px).

    Тонкий тёмный штрих на светлом фоне даёт ДВЕ параллельные линии
    (кромки штриха) вместо одной центральной — «двойной контур» из
    списка проблем проекта. Пара признаётся кромками одного штриха,
    если пиксели на расстоянии 1..max_dist по направлению -sgn*normal
    (строгое округление, без floor/ceil-расширения), их нормали
    коллинеарны (|Δang| < ang_tol), а проекции суммарного векторного
    градиента (знак sgn) противоположны. Пара заменяется средней точкой.

    max_dist=2 (штрихи <=3px): рамки и объектные границы >=4px не
    склеиваются (эксперимент: s06 bF1 1.00 сохраняется, тонкие
    штрихи s12/s14 pixel-F1 +0.5/+0.6). Корпус: bF1 0.954 / F1 0.485.
    """
    p = mask > 0
    if not p.any():
        return mask
    ys, xs = np.nonzero(p)
    if ys.size == 0:
        return mask
    # параметры только точек маски (fancy-индексация вместо полного
    # .tolist() кадра: 8 мс против 126 на 1280x800 с 2.3k точек)
    ang_v = ang[ys, xs].astype(np.float64)
    cos_v = np.cos(ang_v)
    sin_v = np.sin(ang_v)
    sgn_v = np.where(sgn[ys, xs] >= 0, 1, -1)
    info = {}
    for i, (y, x) in enumerate(zip(ys.tolist(), xs.tolist())):
        info[(y, x)] = (float(ang_v[i]), float(cos_v[i]), float(sin_v[i]),
                        int(sgn_v[i]))
    out = np.zeros(p.shape, bool)
    removed = set()
    for (y, x) in info:
        if (y, x) in removed:
            continue
        a1, c1, s1c, s1 = info[(y, x)]
        # направление поиска: ПРОТИВ своего градиента — в сторону
        # противоположной кромки штриха
        n1x = c1 * (-s1)
        n1y = s1c * (-s1)
        partner = None
        for d in range(1, max_dist + 1):
            q = (int(round(y + n1y * d)), int(round(x + n1x * d)))
            if q in info and q not in removed and q != (y, x):
                a2, c2, s2c, s2 = info[q]
                if s2 == -s1 and abs(a1 - a2) < ang_tol:
                    partner = q
                    break
        if partner is not None:
            out[(y + partner[0]) // 2, (x + partner[1]) // 2] = True
            removed.add((y, x))
            removed.add(partner)
        else:
            out[y, x] = True
    return out.astype(np.uint8) * 255


def _signed_gradient(channels: list[np.ndarray],
                     ang: np.ndarray) -> np.ndarray:
    """Знак проекции суммарного векторного градиента на нормаль ang.

    Тензор не различает ±n (квадратичная форма); знак нужен только
    _anti_double для распознавания противоположных кромок штриха.
    """
    gx = np.zeros(channels[0].shape, np.float32)
    gy = np.zeros_like(gx)
    for ch in channels:
        gx += cv2.Scharr(ch, cv2.CV_32F, 1, 0)
        gy += cv2.Scharr(ch, cv2.CV_32F, 0, 1)
    return np.sign(gx * np.cos(ang) + gy * np.sin(ang))


# ---------------------------------------------------------------------------
# 6. Zhang–Suen thinning (канонический)
# ---------------------------------------------------------------------------
def _thin(mask: np.ndarray, max_iter: int) -> np.ndarray:
    """Канонический Zhang–Suen (обе подитерации, A(P1)=1, точные
    произведения соседей, одновременное удаление помеченных).

    Отличие от прежней _thin_fallback: условия в старом коде были
    записаны как (p2&p4&p6)|(p4&p6&p8) — ИЛИ вместо И произведений —
    и без A(P1): замкнутые контуры рвались (круг 3px -> 38 кусков).
    """
    m = mask > 0
    if not m.any():
        return mask
    for _ in range(max_iter):
        changed = False
        for step in (0, 1):
            p = np.pad(m, 1, constant_values=False)
            p2 = p[:-2, 1:-1]   # N
            p3 = p[:-2, 2:]     # NE
            p4 = p[1:-1, 2:]    # E
            p5 = p[2:, 2:]      # SE
            p6 = p[2:, 1:-1]    # S
            p7 = p[2:, :-2]     # SW
            p8 = p[1:-1, :-2]   # W
            p9 = p[:-2, :-2]    # NW
            ring = np.stack([p2, p3, p4, p5, p6, p7, p8, p9, p2])
            b = ring[:-1].astype(np.uint8).sum(axis=0)
            a = (ring[1:] & ~ring[:-1]).astype(np.uint8).sum(axis=0)
            if step == 0:
                cond = (p2 & p4 & p6) | (p4 & p6 & p8)
            else:
                cond = (p2 & p6 & p8) | (p2 & p4 & p8)
            delete = m & (b >= 2) & (b <= 6) & (a == 1) & ~cond
            if delete.any():
                m = m & ~delete
                changed = True
        if not changed:
            break
    return m.astype(np.uint8) * 255


def _needs_thin(mask: np.ndarray) -> bool:
    """Есть ли утолщения >1px (дешёвый тест эрозией 3x3).

    Эрозия пуста <=> все линии однопиксельные <=> thin не нужен.
    EDT-тест давал ту же информацию за 48 мс на 1280x800 против
    0.3 мс у эрозии.
    """
    m8 = (mask > 0).astype(np.uint8)
    if not m8.any():
        return False
    er = cv2.erode(m8, np.ones((3, 3), np.uint8))
    return bool(er.any())


# ---------------------------------------------------------------------------
# Детектор
# ---------------------------------------------------------------------------
def detect(img: np.ndarray, strength: int = 50,
           cfg: EdgeConfig | None = None) -> np.ndarray:
    """Детектирование контуров -> бинарная маска uint8 (0/255).

    Аргументы:
        img: RGB/RGBA uint8, grayscale, HxWx{1,3,4}; большие кадры
            (>MAX_DETECT_PIX) детектируются на даунскейле, маска
            возвращается в исходном разрешении.
        strength: 0..100, чувствительность (0 — максимум
            чувствительности, 100 — только выраженные границы);
            монотонен: пикселей с ростом strength не становится больше.
        cfg: необязательная конфигурация параметров (EdgeConfig).

    Возвращает:
        маска HxW uint8 {0, 255}, однопиксельные 8-связные линии,
        готовые к извлечению траекторий (strokes.py).
    """
    cfg = cfg or EdgeConfig()
    rgb, scale = _normalize(img)
    H0, W0 = (int(round(rgb.shape[0] / scale)) if scale != 1.0 else rgb.shape[0],
              int(round(rgb.shape[1] / scale)) if scale != 1.0 else rgb.shape[1])
    H0 = max(H0, 8); W0 = max(W0, 8)

    smooth = cv2.GaussianBlur(rgb, (3, 3), 0.8)
    channels = _lab_channels(smooth)
    mag, ang = _structure_tensor(channels, cfg.tensor_sigma)
    nms = _nms(mag, ang, cfg.nms_eps)

    nz = nms[nms > 0]
    if nz.size < 32:
        return np.zeros((H0, W0), np.uint8)

    hi, lo = _thresholds(nms, cfg, strength)
    mask = _hysteresis(nms, hi, lo)
    # двойные линии тонких штрихов (<=3px) -> центральная линия;
    # изолированные пиксели убираем ПОСЛЕ (mid-точки могут остаться
    # без соседей)
    sgn = _signed_gradient(channels, ang)
    mask = _anti_double(mask, ang, sgn)
    mask = _drop_isolated(mask)
    if _needs_thin(mask):
        mask = _thin(mask, cfg.max_thin_iter)
    if mask.shape != (H0, W0):
        mask = cv2.resize(mask, (W0, H0), interpolation=cv2.INTER_NEAREST)
    return mask


# ---------------------------------------------------------------------------
# Совместимые вспомогательные функции
# ---------------------------------------------------------------------------
def mask_to_rgba(mask: np.ndarray,
                 color: tuple = (20, 22, 30, 255)) -> "object":
    """Маска -> RGBA PIL-изображение (цвет линии + альфа из маски)."""
    from PIL import Image as PILImage
    h, w = mask.shape[:2]
    rgba = np.zeros((h, w, 4), np.uint8)
    if len(color) >= 3:
        rgba[..., 0], rgba[..., 1], rgba[..., 2] = color[0], color[1], color[2]
    alpha = color[3] if len(color) >= 4 else 255
    rgba[..., 3] = (mask > 0) * alpha
    return PILImage.fromarray(rgba)


def edge_stats(mask: np.ndarray) -> dict:
    """Статистика для UI/тестов: пиксели, компоненты (8-связность)."""
    if mask is None or not mask.any():
        return {"px": 0, "components": 0, "w": 0, "h": 0}
    lbl, n = ndimage.label(mask > 0, structure=_STRUCTURE8)
    return {"px": int((mask > 0).sum()), "components": int(n),
            "w": int(mask.shape[1]), "h": int(mask.shape[0])}
