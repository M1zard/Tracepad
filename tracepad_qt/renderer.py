# -*- coding: utf-8 -*-
"""Renderer — PIL-пайплайн кадра оверлея (GUI-независимый).

Возвращает PIL RGBA, конвертацию в Qt-изображение делает GUI-слой.

Строение кадра (порядок слоёв, считаются от КЭША с фильтрами):
  filters_cache = [bw] -> [edge]      (пересчёт только при смене режимов)
  кадр = resize(zoom) -> flip(x/y) -> grid overlay
Сетка рисуется здесь же (в PIL), а не в GUI-виджете: тогда её не нужно
перерисовывать при каждом ресайзе окна и она кэшируется вместе с кадром.

Два качества рендера:
  final  — LANCZOS от кэша (после остановки взаимодействия);
  fast   — NEAREST (в движении: колесо, drag).

Edge-режим (контуры) с версии 2.2 — edges.py: многоканальный
детектор (Lab, NMS, гистерезис), 1px-маска раскрашивается в ч/б
карту. Старый FIND_EDGES-эффект удалён: F1 0.04 против 0.96 (см.
заголовок edges.py). Кэш edge-маски живёт отдельно от кэша
bw-кадра: маска нужна и траекториям (Auto Drawing), а не только
для показа.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageOps

from . import edges as E

MAX_CACHE_PIXELS = 40_000_000


def make_grid(w, h, step, line_rgba=(168, 184, 255, 70),
              center_rgba=(255, 92, 124, 200), dash=(3, 3)):
    """Сетка + крестик центра как RGBA-слой (для наложения на кадр)."""
    if w <= 0 or h <= 0 or step < 8:
        return None
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = layer.load()
    # линии сетки пунктиром (dash-паттерн)
    dx, dy = dash
    for x in range(step, w, step):
        for y in range(0, h):
            if (y // dx) % 2 == 0:
                px[x, y] = line_rgba
    for y in range(step, h, step):
        for x in range(0, w):
            if (x // dy) % 2 == 0:
                px[x, y] = line_rgba
    # крестик в центре
    cx, cy = w // 2, h // 2
    half = min(10, w // 8, h // 8)
    if half >= 2:
        for x in range(cx - half, cx + half + 1):
            px[x, cy] = center_rgba
        for y in range(cy - half, cy + half + 1):
            px[cx, y] = center_rgba
    return layer


class RenderPipeline:
    def __init__(self):
        self.orig = None                # исходная RGBA
        self._cache = None              # full-size после bw/edge
        self._cache_key = None
        self._edge_mask = None          # 1px-маска контуров (edges.py)
        self._edge_key = None

    # --- жизненный цикл -----------------------------------------------------
    def set_image(self, img: Image.Image):
        self.orig = img.convert("RGBA") if img.mode != "RGBA" else img
        self._cache = None
        self._cache_key = None
        self._edge_mask = None
        self._edge_key = None

    def has_image(self):
        return self.orig is not None

    def size(self):
        return self.orig.size if self.orig else (0, 0)

    # --- кэш фильтров --------------------------------------------------------
    def _filtered(self, bw, edge, strength):
        if self.orig is None:
            return None
        key = (id(self.orig), bw, edge, int(strength))
        if self._cache_key == key and self._cache is not None:
            return self._cache
        base = self.orig
        if base.width * base.height > MAX_CACHE_PIXELS:
            k = (MAX_CACHE_PIXELS / (base.width * base.height)) ** 0.5
            base = base.resize((max(1, int(base.width * k)),
                                max(1, int(base.height * k))),
                               Image.LANCZOS)
        img = base
        if bw and not edge:
            gray = ImageOps.grayscale(img.convert("RGB")).convert("RGB")
            img = Image.merge("RGBA", (*gray.split(), img.getchannel("A")))
        if edge:
            # карта контуров edges.py: 1px-маска -> чёрные линии на
            # прозрачном (та же семантика, что у старого режима).
            # Маска может быть меньше кадра (MAX_DETECT_PIX в edges.py):
            # линии растягиваем nearest-ом до полного размера — на 4K
            # линии будут 2px, это читаемо и честно.
            mask = self.edge_mask(strength)
            if mask is not None and mask.any():
                if mask.shape[1] != base.width or mask.shape[0] != base.height:
                    mask = np.asarray(
                        Image.fromarray(mask).resize(
                            (base.width, base.height), Image.NEAREST))
                img = E.mask_to_rgba(mask, color=(18, 20, 28, 255))
            else:
                img = Image.merge("RGBA", (
                    Image.new("L", img.size, 0),
                    Image.new("L", img.size, 0),
                    Image.new("L", img.size, 0),
                    Image.new("L", img.size, 0)))
        self._cache, self._cache_key = img, key
        return img

    # --- карта контуров (общая для показа и Auto Drawing) -------------------
    def edge_mask(self, strength=50):
        """1px-маска контуров (numpy uint8), с кэшем по strength."""
        if self.orig is None:
            return None
        key = (id(self.orig), int(strength))
        if self._edge_key == key and self._edge_mask is not None:
            return self._edge_mask
        base = self.orig
        if base.width * base.height > MAX_CACHE_PIXELS:
            k = (MAX_CACHE_PIXELS / (base.width * base.height)) ** 0.5
            base = base.resize((max(1, int(base.width * k)),
                                max(1, int(base.height * k))),
                               Image.LANCZOS)
        rgb = np.asarray(base.convert("RGB"))
        self._edge_mask = E.detect(rgb, int(strength))
        self._edge_key = key
        return self._edge_mask

    # --- кадр ------------------------------------------------------------------
    def render(self, zoom, bw=False, edge=False, edge_strength=60,
               flip_x=False, flip_y=False, grid=False, grid_size=48,
               quality="final"):
        if self.orig is None:
            return None
        src = self._filtered(bw, edge, edge_strength)
        if src is None:
            return None
        w = max(1, int(round(src.width * zoom)))
        h = max(1, int(round(src.height * zoom)))
        if (w, h) == src.size:
            img = src.copy()
        else:
            resample = Image.LANCZOS if quality == "final" else Image.NEAREST
            img = src.resize((w, h), resample)
        if flip_x:
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        if flip_y:
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
        if grid:
            layer = make_grid(w, h, max(8, int(grid_size)))
            if layer is not None:
                img = Image.alpha_composite(img, layer)
        return img

    def extent(self, zoom):
        if self.orig is None:
            return (0, 0)
        return (max(1, int(round(self.orig.width * zoom))),
                max(1, int(round(self.orig.height * zoom))))
