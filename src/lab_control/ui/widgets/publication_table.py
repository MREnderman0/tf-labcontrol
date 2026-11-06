"""White 183 mm, 600 dpi summary tables using the existing Qt export runtime."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen

from .publication_figure import prepare_publication_font


def export_table(path, title, headers, rows, *, check_cancel=lambda: None):
    if path.exists():
        raise FileExistsError(path)
    family = prepare_publication_font()
    width, height = 183 / 25.4 * 72, 62 + 16 * (len(rows) + 1)
    device = QImage(round(width * 600 / 72), round(height * 600 / 72), QImage.Format.Format_ARGB32)
    device.fill(QColor("white"))
    device.setDotsPerMeterX(round(600 / 0.0254))
    device.setDotsPerMeterY(round(600 / 0.0254))
    painter = QPainter(device)
    try:
        painter.scale(600 / 72, 600 / 72)
        font = QFont(family)
        font.setPixelSize(8)
        painter.setFont(font)
        painter.setPen(QPen(QColor("#222222"), 0.65))
        painter.drawText(QRectF(18, 12, width - 36, 32), Qt.AlignmentFlag.AlignCenter, title)
        cell_width = (width - 36) / len(headers)
        for index, row in enumerate([headers, *rows]):
            check_cancel()
            y = 46 + index * 16
            for column, text in enumerate(row):
                painter.drawText(
                    QRectF(18 + column * cell_width, y, cell_width, 16),
                    Qt.AlignmentFlag.AlignCenter,
                    str(text),
                )
            if index == 0:
                painter.drawLine(18, y + 16, round(width - 18), y + 16)
    finally:
        painter.end()
    check_cancel()
    if not device.save(str(path)):
        raise OSError("无法保存统计汇总图片")
