# -*- coding: utf-8 -*-
"""GUI 主题：深色优先（借鉴 GameScrcpy 的深色风格），可切换浅色。

apply_theme(app, "dark"|"light") 一次性设置调色板 + QSS；
自定义绘制的控件（如投屏画面）用 color(key) 取当前主题色。
按钮语义色用 set_role(btn, "accent"|"danger"|"success")，
弱化说明文字用 set_subdued(label)。
"""

from __future__ import annotations

THEMES = {
    "dark": {
        "window": "#1b1f27",
        "panel": "#232833",
        "panel2": "#2c3342",
        "hover": "#353d4f",
        "pressed": "#2a3140",
        "border": "#3a4254",
        "text": "#dce3ec",
        "subtext": "#8b95a7",
        "accent": "#4f8cff",
        "accent_hover": "#6ba1ff",
        "danger": "#e5534b",
        "danger_hover": "#f06a62",
        "success": "#3fb950",
        "success_hover": "#56c964",
        "input_bg": "#151922",
        "selection": "#2f5eb8",
        "video_bg": "#0d1015",
    },
    "light": {
        "window": "#f5f6f8",
        "panel": "#ffffff",
        "panel2": "#eceff4",
        "hover": "#e2e7ef",
        "pressed": "#d6dce6",
        "border": "#d3d9e2",
        "text": "#24292f",
        "subtext": "#6b7484",
        "accent": "#2f6fed",
        "accent_hover": "#4a82f2",
        "danger": "#d64545",
        "danger_hover": "#e05c5c",
        "success": "#2da44e",
        "success_hover": "#3fb95f",
        "input_bg": "#ffffff",
        "selection": "#cfe0ff",
        "video_bg": "#14171c",
    },
}

_current = "dark"

_QSS = """
* {{ outline: none; }}
QWidget {{
    background: {window}; color: {text};
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 13px;
}}
QMainWindow::separator {{ width: 2px; height: 2px; background: {border}; }}
QToolTip {{
    background: {panel2}; color: {text}; border: 1px solid {border};
    padding: 4px 8px; border-radius: 4px;
}}

/* ---- 工具栏 / 状态栏 ---- */
QToolBar {{
    background: {panel}; border: none; border-bottom: 1px solid {border};
    padding: 6px 10px; spacing: 6px;
}}
QToolBar::separator {{ width: 1px; background: {border}; margin: 4px 6px; }}
QStatusBar {{
    background: {panel}; border-top: 1px solid {border}; color: {subtext};
}}
QStatusBar::item {{ border: none; }}

/* ---- 标签 ---- */
QLabel {{ background: transparent; }}
QLabel[subdued="true"] {{ color: {subtext}; font-size: 12px; }}
QLabel#fpsBadge {{
    background: {panel2}; color: {accent}; border: 1px solid {border};
    border-radius: 10px; padding: 2px 10px; font-weight: 600;
}}
QLabel#stateBadge {{
    background: {panel2}; border: 1px solid {border};
    border-radius: 10px; padding: 2px 10px; color: {subtext};
}}
/* 投屏画面右上角「拖出独立展示」把手：半透明压在画面上不挡观察 */
QToolButton#popoutBtn {{
    background: rgba(0, 0, 0, 110); color: #ffffff; border: 1px solid {border};
    border-radius: 6px; padding: 2px 8px; font-size: 13px;
}}
QToolButton#popoutBtn:hover {{ background: {accent}; border-color: {accent}; }}

/* ---- 按钮 ---- */
QPushButton {{
    background: {panel2}; border: 1px solid {border}; border-radius: 6px;
    padding: 6px 14px; color: {text};
}}
QPushButton:hover {{ background: {hover}; border-color: {accent}; }}
QPushButton:pressed {{ background: {pressed}; }}
QPushButton:disabled {{ color: {subtext}; background: {panel}; border-color: {border}; }}
QPushButton[role="accent"] {{
    background: {accent}; border-color: {accent}; color: #ffffff; font-weight: 600;
}}
QPushButton[role="accent"]:hover {{ background: {accent_hover}; border-color: {accent_hover}; }}
QPushButton[role="accent"]:pressed {{ background: {accent}; }}
QPushButton[role="accent"]:disabled {{
    background: {panel2}; color: {subtext}; border-color: {border};
}}
QPushButton[role="danger"] {{
    background: {danger}; border-color: {danger}; color: #ffffff; font-weight: 600;
}}
QPushButton[role="danger"]:hover {{ background: {danger_hover}; border-color: {danger_hover}; }}
QPushButton[role="danger"]:disabled {{
    background: {panel2}; color: {subtext}; border-color: {border};
}}
QPushButton[role="success"] {{
    background: {success}; border-color: {success}; color: #ffffff; font-weight: 600;
}}
QPushButton[role="success"]:hover {{ background: {success_hover}; border-color: {success_hover}; }}
QPushButton[role="success"]:disabled {{
    background: {panel2}; color: {subtext}; border-color: {border};
}}
QPushButton[key="true"] {{ padding: 4px 10px; }}

/* ---- 侧边抽屉把手（录制面板折叠按钮）---- */
QToolButton#recHandle {{
    background: {panel2}; color: {accent};
    border: 1px solid {accent}; border-radius: 4px;
    padding: 0; margin: 0; font-size: 13px; font-weight: 600;
}}
QToolButton#recHandle:hover {{
    background: {accent}; color: #ffffff; border-color: {accent};
}}
QToolButton#recHandle:pressed {{ background: {accent_hover}; border-color: {accent_hover}; }}
QToolButton#recHandle[recording="true"] {{
    background: {danger}; color: #ffffff; border-color: {danger};
}}

/* ---- 输入控件 ---- */
QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {input_bg}; border: 1px solid {border}; border-radius: 6px;
    padding: 5px 8px; selection-background-color: {selection};
    selection-color: {text};
}}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {accent}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {subtext}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{
    image: none; border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid {subtext}; margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background: {panel}; border: 1px solid {border}; border-radius: 4px;
    selection-background-color: {selection}; selection-color: {text};
}}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    width: 16px; border: none; background: {panel2};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: none; border-left: 3px solid transparent; border-right: 3px solid transparent;
    border-bottom: 4px solid {subtext};
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: none; border-left: 3px solid transparent; border-right: 3px solid transparent;
    border-top: 4px solid {subtext};
}}
QPlainTextEdit#codeView, QPlainTextEdit#logView {{
    font-family: Consolas, "Cascadia Mono", "Courier New", monospace;
    font-size: 13px;
}}

/* ---- 页签 ---- */
QTabWidget::pane {{
    border: 1px solid {border}; border-radius: 8px; top: -1px; background: {window};
}}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: transparent; color: {subtext}; padding: 8px 18px;
    border: none; border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {text}; border-bottom: 2px solid {accent}; font-weight: 600; }}
QTabBar::tab:hover:!selected {{ color: {text}; }}

/* ---- 分组框 ---- */
QGroupBox {{
    border: 1px solid {border}; border-radius: 8px; background: {panel};
    margin-top: 12px; padding: 12px 10px 10px 10px; font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {subtext};
}}

/* ---- 表格 ---- */
QTableWidget {{
    background: {input_bg}; alternate-background-color: {panel};
    border: 1px solid {border}; border-radius: 6px;
    selection-background-color: {selection}; selection-color: {text};
}}
QTableWidget::item {{ padding: 2px 4px; }}
QHeaderView::section {{
    background: {panel2}; color: {subtext}; border: none;
    border-bottom: 1px solid {border}; padding: 6px 8px; font-weight: 600;
}}
QTableCornerButton::section {{ background: {panel2}; border: none; }}

/* ---- 树（控件树 / 属性表）----
   show-decoration-selected: 选中行背景横贯整个缩进区，深层条目也能看出属于哪一支 */
QTreeWidget {{
    background: {input_bg}; alternate-background-color: {panel};
    border: 1px solid {border}; border-radius: 6px;
    selection-background-color: {selection}; selection-color: {text};
    show-decoration-selected: 1;
}}
QTreeWidget::item {{ padding: 2px 2px; }}
QTreeWidget::item:hover {{ background: {panel2}; }}
QTreeWidget::branch {{ background: transparent; }}

/* ---- 复选框 ---- */
QCheckBox {{ spacing: 6px; background: transparent; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid {border}; background: {input_bg};
}}
QCheckBox::indicator:checked {{ background: {accent}; border-color: {accent}; }}
QCheckBox::indicator:disabled {{ border-color: {border}; background: {panel}; }}

/* ---- 滚动条 / 分割条 ---- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {border}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {subtext}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {border}; border-radius: 4px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {subtext}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QSplitter::handle {{ background: {border}; }}
QSplitter::handle:horizontal {{ width: 2px; }}
QSplitter::handle:vertical {{ height: 2px; }}

/* ---- 表单 ---- */
QFormLayout QLabel {{ color: {subtext}; }}
"""


def apply_theme(app, name: str) -> None:
    """应用主题：调色板兜底 + QSS 主样式。name 非法时回退深色。"""
    global _current
    from PySide6.QtGui import QColor, QPalette

    name = name if name in THEMES else "dark"
    _current = name
    c = THEMES[name]
    pal = QPalette()
    role = QPalette.ColorRole
    pal.setColor(role.Window, QColor(c["window"]))
    pal.setColor(role.WindowText, QColor(c["text"]))
    pal.setColor(role.Base, QColor(c["input_bg"]))
    pal.setColor(role.AlternateBase, QColor(c["panel"]))
    pal.setColor(role.Text, QColor(c["text"]))
    pal.setColor(role.Button, QColor(c["panel2"]))
    pal.setColor(role.ButtonText, QColor(c["text"]))
    pal.setColor(role.Highlight, QColor(c["selection"]))
    pal.setColor(role.HighlightedText, QColor(c["text"]))
    pal.setColor(role.ToolTipBase, QColor(c["panel2"]))
    pal.setColor(role.ToolTipText, QColor(c["text"]))
    pal.setColor(role.PlaceholderText, QColor(c["subtext"]))
    pal.setColor(role.Link, QColor(c["accent"]))
    disabled = QColor(c["subtext"])
    for r in (role.WindowText, role.Text, role.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, r, disabled)
    app.setPalette(pal)
    app.setStyleSheet(_QSS.format(**c))


def current() -> str:
    return _current


def color(key: str) -> str:
    """取当前主题色（自定义绘制用，如投屏画面背景）。"""
    return THEMES[_current].get(key, THEMES["dark"].get(key, "#000000"))


def set_role(btn, role: str) -> None:
    """按钮语义色：accent=主操作 / danger=录制等警示 / success=回放等确认。"""
    btn.setProperty("role", role)
    btn.style().unpolish(btn)
    btn.style().polish(btn)


def set_subdued(label) -> None:
    label.setProperty("subdued", True)
    label.style().unpolish(label)
    label.style().polish(label)