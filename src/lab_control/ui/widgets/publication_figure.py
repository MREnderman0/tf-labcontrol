"""Standalone white-paper figures, independent of the application's plot theme.

Qt supplies raster, SVG and PDF output; no plotting dependency or screen grab is
needed. Coordinates are physical typographic points at the final column width.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
from PySide6.QtCore import QMarginsF, QPointF, QRectF, QSize, QSizeF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QImage,
    QPageLayout,
    QPageSize,
    QPainter,
    QPainterPath,
    QPdfWriter,
    QPen,
)
from PySide6.QtSvg import QSvgGenerator

COLORS = ("#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#333333", "#7A5195")
STYLES = (Qt.PenStyle.SolidLine, Qt.PenStyle.DashLine, Qt.PenStyle.DotLine)


def prepare_publication_font() -> str:
    """Load the existing Windows Arial font for headless Qt as well as desktop Qt."""
    if QGuiApplication.instance() is None:
        raise RuntimeError("导图需要已初始化的 Qt 图形应用")
    if "Arial" not in QFontDatabase.families() and os.name == "nt":
        path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf"
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))
    return "Arial" if "Arial" in QFontDatabase.families() else QGuiApplication.font().family()


def _envelope(x: np.ndarray, y: np.ndarray, target: int = 12000) -> tuple[np.ndarray, np.ndarray]:
    """Bound vector complexity by retaining endpoints/extrema, never bridging gaps."""
    if x.size <= target:
        return x, y
    step = math.ceil(x.size / (target // 4))
    xx, yy = [], []
    previous = None
    for start in range(0, x.size, step):
        stop = min(start + step, x.size)
        valid = np.flatnonzero(np.isfinite(x[start:stop]) & np.isfinite(y[start:stop])) + start
        if not valid.size:
            if previous is not None:
                xx.append(float("nan"))
                yy.append(float("nan"))
                previous = None
            continue
        values = y[valid]
        indices = sorted(
            {int(valid[0]), int(valid[-1]), int(valid[np.argmin(values)]), int(valid[np.argmax(values)])}
        )
        for index in indices:
            if previous is not None and (
                not np.isfinite(x[previous:index]).all() or not np.isfinite(y[previous:index]).all()
            ):
                xx.append(float("nan"))
                yy.append(float("nan"))
            xx.append(x[index])
            yy.append(y[index])
            previous = index
    return np.asarray(xx), np.asarray(yy)


def _ticks(lo: float, hi: float) -> np.ndarray:
    step = 10 ** math.floor(math.log10((hi - lo) / 4))
    step *= next(value for value in (1, 2, 2.5, 5, 10) if step * value >= (hi - lo) / 4)
    return np.arange(math.ceil(lo / step), math.floor(hi / step) + 1) * step


def _limits(lo: float, hi: float) -> tuple[float, float]:
    pad = (hi - lo) * 0.04 if hi > lo else max(abs(lo) * 1e-6, 1e-9)
    return lo - pad, hi + pad


def export_figure(
    path: Path,
    curves: list[tuple[str, np.ndarray, np.ndarray]],
    x_label: str,
    y_label: str,
    *,
    width_mm: int = 183,
) -> None:
    """Write a 600-dpi PNG or vector PDF/SVG with 8-pt Arial and 0.65-pt lines."""
    if width_mm not in (89, 183) or path.suffix.lower() not in {".png", ".svg", ".pdf"}:
        raise ValueError("请选择 89/183 mm 宽度及 PNG、SVG 或 PDF 格式")
    if path.exists():
        raise FileExistsError("图片已存在，不覆盖已有文件")
    family = prepare_publication_font()
    compact = [(label, *_envelope(np.asarray(x), np.asarray(y))) for label, x, y in curves]
    finite = [(label, x, y) for label, x, y in compact if np.any(np.isfinite(x) & np.isfinite(y))]
    if not finite:
        raise ValueError("当前没有可导出的曲线")
    width = width_mm / 25.4 * 72
    columns = 2 if width_mm == 89 else 4
    legend_height = math.ceil(len(finite) / columns) * 13
    height = width * 0.63 + legend_height + 18
    size = QSize(round(width * 600 / 72), round(height * 600 / 72))
    device: QImage | QSvgGenerator | QPdfWriter
    if path.suffix.lower() == ".png":
        device = QImage(size, QImage.Format.Format_ARGB32)
        device.fill(QColor("white"))
        device.setDotsPerMeterX(round(600 / 0.0254))
        device.setDotsPerMeterY(round(600 / 0.0254))
    elif path.suffix.lower() == ".svg":
        device = QSvgGenerator()
        device.setFileName(str(path))
        device.setResolution(600)
        device.setSize(size)
        device.setViewBox(QRectF(0, 0, size.width(), size.height()))
        device.setTitle("Laboratory measurement")
        device.setDescription("White background; Arial 8 pt; 0.65 pt lines; original measured values.")
    else:
        device = QPdfWriter(str(path))
        device.setResolution(600)
        device.setPageSize(QPageSize(QSizeF(width, height), QPageSize.Unit.Point))
        device.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Point)
        device.setCreator("TF-LabControl")
    painter = QPainter(device)
    if not painter.isActive():
        raise OSError("无法创建插图文件")
    try:
        painter.scale(600 / 72, 600 / 72)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Pixel size is expressed in logical point coordinates before scaling.
        font = QFont(family)
        font.setPixelSize(8)
        painter.setFont(font)
        painter.fillRect(QRectF(0, 0, width, height), QColor("white"))
        chart = QRectF(59, legend_height + 18, width - 75, height - legend_height - 59)
        valid = [
            (x[np.isfinite(x) & np.isfinite(y)], y[np.isfinite(x) & np.isfinite(y)]) for _, x, y in finite
        ]
        xlo, xhi = _limits(min(float(x.min()) for x, _ in valid), max(float(x.max()) for x, _ in valid))
        ylo, yhi = _limits(min(float(y.min()) for _, y in valid), max(float(y.max()) for _, y in valid))
        # Large carrier frequencies retain readable variation without duplicate tick labels.
        offset = float(valid[0][1][0]) if max(abs(ylo), abs(yhi)) > (yhi - ylo) * 1e4 else 0.0
        if offset:
            y_label += f"; offset {offset:.12g}"

        def xp(value):
            return chart.left() + (value - xlo) / (xhi - xlo) * chart.width()

        def yp(value):
            return chart.bottom() - (value - ylo) / (yhi - ylo) * chart.height()

        painter.setPen(QPen(QColor("#222222"), 0.65))
        painter.drawLine(chart.bottomLeft(), chart.bottomRight())
        painter.drawLine(chart.bottomLeft(), chart.topLeft())
        for value in _ticks(xlo, xhi):
            x = xp(value)
            painter.drawLine(QPointF(x, chart.bottom()), QPointF(x, chart.bottom() + 3))
            painter.drawText(
                QRectF(x - 28, chart.bottom() + 5, 56, 12), Qt.AlignmentFlag.AlignHCenter, f"{value:.5g}"
            )
        for delta in _ticks(ylo - offset, yhi - offset):
            y = yp(delta + offset)
            painter.drawLine(QPointF(chart.left() - 3, y), QPointF(chart.left(), y))
            painter.drawText(QRectF(13, y - 6, 41, 12), Qt.AlignmentFlag.AlignRight, f"{delta:.5g}")
        painter.drawText(
            QRectF(chart.left(), height - 17, chart.width(), 14), Qt.AlignmentFlag.AlignHCenter, x_label
        )
        painter.save()
        painter.translate(10, chart.center().y())
        painter.rotate(-90)
        painter.drawText(
            QRectF(-chart.height() / 2, -8, chart.height(), 14), Qt.AlignmentFlag.AlignHCenter, y_label
        )
        painter.restore()
        for index, (label, x, y) in enumerate(finite):
            pen = QPen(
                QColor(COLORS[index % len(COLORS)]), 0.65, STYLES[(index // len(COLORS)) % len(STYLES)]
            )
            painter.setPen(pen)
            col, row = index % columns, index // columns
            lx = chart.left() + col * chart.width() / columns
            ly = 10 + row * 13
            painter.drawLine(QPointF(lx, ly), QPointF(lx + 14, ly))
            painter.drawText(QPointF(lx + 19, ly + 3), label)
            painter.save()
            painter.setClipRect(chart.adjusted(-1, -1, 1, 1))
            path_data = QPainterPath()
            connected = False
            count = 0
            for xx, yy in zip(x, y, strict=True):
                if not np.isfinite(xx) or not np.isfinite(yy):
                    connected = False
                    continue
                point = QPointF(xp(float(xx)), yp(float(yy)))
                if connected:
                    path_data.lineTo(point)
                else:
                    path_data.moveTo(point)
                connected = True
                count += 1
                if count % 4096 == 0:
                    painter.drawPath(path_data)
                    path_data = QPainterPath(point)
            painter.drawPath(path_data)
            if np.count_nonzero(np.isfinite(y)) == 1:
                painter.setBrush(pen.color())
                painter.drawEllipse(point, 1.5, 1.5)
            painter.restore()
    finally:
        painter.end()
    if isinstance(device, QImage) and not device.save(str(path)):
        raise OSError("图片保存失败")
