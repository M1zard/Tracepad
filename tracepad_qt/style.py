# -*- coding: utf-8 -*-
"""style.py — дизайн-система TracePad (токены + QSS).

Единый источник стилей: цвета, скругления, шрифты, QSS для всех виджетов.
Никаких стилей внутри виджетов — только сюда.

Направление: Creative Pro + Glassmorphism — тёмное «ночное стекло»
студийного инструмента. Что это значит на практике:

  * поверх ТОКЕНОВ: нейтральный тёмный фон малоконтрастен, все
    смысловые акценты — один индиго + его производные;
  * КИСЛОТНЫЙ hi (светлее акцента на 2 ступени) для активных
    состояний — не второй цвет, а яркость того же;
  * СТРОГИЕ иерархии текста: 10px caps для названий секций, 12px
    значения, 11px вторичный — три размера, не больше;
  * стекло: карточка 94% непрозрачности, тонкая светлая кромка
    «свет по краю стекла», без блюра фона (desktop-окна поверх
    рабочих приложений: блюр съел бы CPU и читаемость);
  * фокус-состояния КНОПОК обязательны (border + фон), hover
    120-240 мс — быстро, но заметно;
  * кнопка Draw — единственный «primary» элемент: заливка
    индиго, белый глиф; в состоянии рисования — DANGER-красная
    «стоп». Это единственный акцентный прямоугольник во всём UI.

Палитра «ночное стекло»: тёмная основа + индиго-акцент, фиолетовый hi.
"""

from __future__ import annotations

# --- токены ---------------------------------------------------------------
BG = "#0d0d14"          # фон окна (за стеклом)
GLASS = "#15151f"       # поверхность панели
GLASS_HI = "#1d1d2b"    # hover-поверхность
FIELD = "#0f0f18"       # поле слайдера/сегмента
STROKE = "#282838"      # тонкая граница
STROKE_HI = "#3d3d56"

FG = "#ececf4"
FG_DIM = "#9a9ab4"
FG_FAINT = "#5e5e76"

ACCENT = "#7c6cff"
ACCENT_HI = "#9d8fff"
ACCENT_PRESS = "#5a4fd6"
ACCENT_ON = "#f4f2ff"

DANGER = "#ff5c7c"

R_CARD = 16
R_ROW = 10
R_PILL = 8

PANEL_W = 276

DUR = 140               # мс: переходы состояний
DUR_SLOW = 240          # мс: панели/появления
FADE = 220              # мс: появление картинки


def app_qss(font_family: str = "Segoe UI") -> str:
    """QSS всего приложения (шрифт резолвится системой)."""
    return f"""
    QWidget {{
        font-family: "{font_family}";
        font-size: 13px;
        color: {FG};
    }}
    QLabel {{
        background: transparent;
        border: none;
    }}
    QLabel[class="h1"] {{
        font-size: 15px; font-weight: 700; color: {FG};
        letter-spacing: 0.5px;
    }}
    QLabel[class="dim"] {{
        color: {FG_DIM}; font-size: 11.5px;
    }}
    QLabel[class="faint"] {{
        color: {FG_FAINT}; font-size: 10.5px;
    }}
    QLabel[class="caps"] {{
        color: {FG_FAINT}; font-size: 10px; font-weight: 700;
        letter-spacing: 1.6px;
        text-transform: uppercase;
    }}
    QLabel[class="value"] {{
        color: {FG}; font-size: 12px; font-weight: 600;
    }}

    /* ---- стеклянная панель ---- */
    #GlassPanel {{
        background: {GLASS};
        border: 1px solid {STROKE};
        border-radius: {R_CARD}px;
    }}
    #PanelHeader {{ background: transparent; border: none; }}

    /* ---- строки-кнопки ---- */
    #RowButton, #RowDanger {{
        background: {GLASS_HI}00;
        border: 1px solid transparent;
        border-radius: {R_ROW}px;
        padding: 7px 10px;
        text-align: left;
    }}
    #RowButton:hover {{
        background: {GLASS_HI};
        border-color: {STROKE};
    }}
    #RowButton:pressed {{
        background: {ACCENT_PRESS}22;
        border-color: {ACCENT}66;
    }}
    #RowDanger {{ color: {DANGER}; background: transparent; }}
    #RowDanger:hover {{
        background: {DANGER}1a;
        border-color: {DANGER}44;
    }}

    /* ---- кнопка Draw (primary) ---- */
    #DrawButton {{
        background: {ACCENT};
        color: {ACCENT_ON};
        border: 1px solid {ACCENT_HI}66;
        border-radius: {R_ROW}px;
        padding: 9px 10px;
        font-weight: 600;
        font-size: 13px;
        text-align: left;
    }}
    #DrawButton:hover {{
        background: {ACCENT_HI};
        border-color: {ACCENT_HI}aa;
    }}
    #DrawButton:pressed {{
        background: {ACCENT_PRESS};
        border-color: {ACCENT_PRESS};
    }}

    /* ---- сегменты ---- */
    #Segment {{
        background: {FIELD};
        border: 1px solid {STROKE};
        border-radius: {R_PILL}px;
        padding: 4px 0;
        color: {FG_DIM};
        font-size: 11.5px;
    }}
    #Segment:hover {{ border-color: {STROKE_HI}; color: {FG}; }}
    #Segment:checked {{
        background: {ACCENT}2e;
        border-color: {ACCENT};
        color: {ACCENT_ON};
        font-weight: 600;
    }}

    /* ---- контекстное меню ---- */
    QMenu {{
        background: {GLASS};
        border: 1px solid {STROKE};
        border-radius: {R_PILL}px;
        padding: 6px;
    }}
    QMenu::item {{
        color: {FG_DIM};
        padding: 6px 18px;
        border-radius: {R_PILL}px;
    }}
    QMenu::item:selected {{
        background: {ACCENT}2e;
        color: {ACCENT_ON};
    }}
    QMenu::separator {{
        height: 1px;
        background: {STROKE};
        margin: 4px 8px;
    }}
    """


def overlay_qss() -> str:
    return "background: transparent; border: none;"
