"""Graphite, mint and warm white: a quiet workspace for an active assistant."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

STYLE = """
QWidget { font-family: 'Segoe UI', 'Tahoma'; font-size: 13px; color: #e1e8e9; }
QMainWindow, #workspace { background: #11181c; }
QScrollArea, #organizerPage { background: #11181c; border: none; }
QLabel { background: transparent; }
QToolTip { background: #e2f3cc; color: #15231d; border: none; padding: 10px; }
#sidebar { background: #0b1115; border-left: 1px solid #253037; }
#brand { color: #f1f6ed; font-size: 23px; font-weight: 700; }
#brandSub { color: #7e9691; font-size: 10px; letter-spacing: 2px; }
#sideLabel { color: #62777e; font-size: 11px; }
#sideNote { color: #9aadaf; font-size: 12px; }
#sidebar QPushButton { text-align: right; color: #97a9ad; background: transparent;
    border: none; border-radius: 10px; padding: 13px 14px; font-size: 13px; }
#sidebar QPushButton:hover { background: #172329; color: #e5efee; }
#sidebar QPushButton:checked { background: #243c31; color: #d2f5a8; font-weight: 600; }
#sidebar QPushButton:disabled { color: #47565e; }
#eyebrow { color: #a3bda9; font-size: 10px; font-weight: 600; letter-spacing: 2px; }
#pageTitle { color: #f1f4ed; font-size: 26px; font-weight: 700; }
#subtitle, #muted { color: #91a4a9; font-size: 12px; }
#sectionTitle { color: #e9f0ed; font-size: 15px; font-weight: 600; }
#panel { background: #192227; border: 1px solid #29363d; border-radius: 14px; }
#autoHero { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #1d3930,stop:1 #1b2929);
    border: 1px solid #375744; border-radius: 16px; }
#autoTitle { color: #eff9e7; font-size: 24px; font-weight: 700; }
#autoDescription { color: #b2c5bb; font-size: 13px; }
#autoStatus { color: #cfeead; background: #2e4639; border: 1px solid #456147;
    border-radius: 10px; padding: 5px 12px; font-size: 11px; }
#autoStatus[active="true"] { background: #c6ef91; border-color: #c6ef91; color: #173222; }
#autoButton { font-size: 15px; padding: 12px 24px; min-height: 23px; }
#statStrip { background: #192227; border: 1px solid #29363d; border-radius: 12px; }
#statValue { font-size: 23px; font-weight: 700; color: #d1efa7; }
#statCaption { color: #93a6ab; font-size: 11px; }
#routeLabel { color: #bcd4c9; font-size: 11px; font-weight: 600; }
#chip { background: #222f32; color: #b2c4bd; border-radius: 9px; padding: 6px 11px; font-size: 11px; }
#notice { color: #a0c1b5; background: #172922; border: 1px solid #2d4137;
    border-radius: 8px; padding: 8px 13px; font-size: 12px; }
#notice[warning="true"] { background: #30291e; color: #e2c08b; border-color: #544530; }
#emptyTitle { color: #e5eeea; font-size: 21px; font-weight: 600; }
#reviewHint { background: #202d31; border-radius: 8px; color: #a6b8bd; padding: 8px 12px; }
QPushButton { background: #243138; border: 1px solid #36454d; border-radius: 8px;
    padding: 8px 14px; font-weight: 600; min-height: 18px; }
QPushButton:hover { background: #2d4045; border-color: #59716e; }
QPushButton:pressed { background: #344c47; }
QPushButton:disabled { color: #52656d; background: #1a242a; border-color: #28353c; }
QPushButton[primary="true"] { background: #c7ee94; color: #183022; border-color: #c7ee94; }
QPushButton[primary="true"]:hover { background: #dcffac; border-color: #dcffac; }
QPushButton[primary="true"]:disabled { background: #3c5140; border-color: #3c5140; color: #829378; }
QPushButton[danger="true"] { color: #f3ac96; border-color: #695044; }
QPushButton[quiet="true"] { background: transparent; border-color: transparent; color: #9ab6ad; }
QPushButton[quiet="true"]:hover { background: #253b34; }
QLineEdit, QComboBox, QPlainTextEdit, QSpinBox { background: #111b21; border: 1px solid #34434a;
    border-radius: 7px; padding: 9px 10px; selection-background-color: #49734f;
    selection-color: #ffffff; min-height: 18px; }
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QSpinBox:focus { border: 1px solid #a4c77c; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { background: #162126; color: #788b90; }
QComboBox { padding-left: 25px; }
QComboBox::drop-down { width: 23px; border: none; }
QComboBox::down-arrow { image: url("__ASSETS__/chevron.svg"); width: 12px; height: 12px; }
QSpinBox { padding-left: 25px; }
QSpinBox::up-button, QSpinBox::down-button { subcontrol-origin: border; width: 22px;
    background: #243138; border: none; border-right: 1px solid #34434a; }
QSpinBox::up-button { subcontrol-position: top left; border-top-left-radius: 7px; }
QSpinBox::down-button { subcontrol-position: bottom left; border-bottom-left-radius: 7px; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: #365140; }
QSpinBox::up-arrow { image: url("__ASSETS__/chevron-up.svg"); width: 12px; height: 12px; }
QSpinBox::down-arrow { image: url("__ASSETS__/chevron.svg"); width: 12px; height: 12px; }
QComboBox QAbstractItemView { background: #1b2a30; color: #e1ebe7;
    selection-background-color: #365140; selection-color: #e4f7d0; border: 1px solid #405950; outline: none; }
QCheckBox { spacing: 8px; color: #bac9ca; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #566a6f;
    border-radius: 4px; background: #131e22; }
QCheckBox::indicator:checked { background: #5b8e5d; border-color: #87b776;
    image: url("__ASSETS__/check.svg"); }
QTableView { background: #192227; alternate-background-color: #1c272c; border: none;
    gridline-color: #2b383d; selection-background-color: #2d453a;
    selection-color: #edf8e4; outline: 0; }
QTableView::item { padding: 8px; border-bottom: 1px solid #26343a; }
QTableView::item:selected { background: #2d453a; color: #e3f6d2; }
QHeaderView::section { background: #141e23; border: none; border-bottom: 1px solid #2d3c42;
    color: #8fa3a9; padding: 11px 8px; font-size: 11px; font-weight: 600; }
QTableCornerButton::section { border: none; background: #141e23; }
QScrollBar:vertical { width: 9px; background: transparent; margin: 1px; }
QScrollBar::handle:vertical { background: #40565c; min-height: 32px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { height: 9px; background: transparent; }
QScrollBar::handle:horizontal { background: #40565c; min-width: 32px; border-radius: 4px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QProgressBar { border: none; border-radius: 3px; background: #263b33; height: 5px; }
QProgressBar::chunk { background: #b9e68a; border-radius: 3px; }
QDialog, QMessageBox { background: #172126; }
QMenu { background: #1b282e; border: 1px solid #3a4f55; padding: 5px; }
QMenu::item { padding: 8px 20px; }
QMenu::item:selected { background: #34513e; }
"""
STYLE = STYLE.replace("__ASSETS__", Path(__file__).parent.as_posix())


def app_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#c7ee94"))
    painter.drawRoundedRect(0, 0, 64, 64, 17, 17)
    painter.setPen(QPen(QColor("#1b3b2c"), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawLine(18, 21, 46, 21)
    painter.drawLine(18, 32, 37, 32)
    painter.drawLine(18, 43, 28, 43)
    painter.drawLine(37, 41, 41, 45)
    painter.drawLine(41, 45, 49, 36)
    painter.end()
    return QIcon(pixmap)
