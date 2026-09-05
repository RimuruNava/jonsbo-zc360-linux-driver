"""Standalone Qt control panel for the Jonsbo ZC-360 driver."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from PIL import Image

from . import __version__
from .cli import test_pattern
from .constants import LOGICAL_HEIGHT, LOGICAL_WIDTH
from .dashboards import (
    SystemDashboard,
    load_external_frames,
    validate_external_directory,
    weather_placeholder,
)
from .ipc import daemon_status, send_fan_frames
from .media import crop_box, open_preview_source, render_first_frame
from .state import default_state, normalized_framing, read_state, state_path, write_state

MEDIA_FILTER = (
    "Media (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.gif "
    "*.mp4 *.mkv *.webm *.mov *.avi *.m4v);;All files (*)"
)


def _qt_image(QtGui, image: Image.Image):
    rgb = image.convert("RGB")
    value = QtGui.QImage(
        rgb.tobytes(), rgb.width, rgb.height, rgb.width * 3, QtGui.QImage.Format.Format_RGB888
    )
    return value.copy()


def create_window_class(QtCore, QtGui, QtWidgets):
    class PreviewWidget(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.setMinimumHeight(190)
            self.setMaximumHeight(270)
            self.setMouseTracking(True)
            self.images = [None, None, None]
            self.rectangles = []

        def set_images(self, images):
            self.images = images
            self.update()

        def paintEvent(self, event):
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            painter.fillRect(self.rect(), QtGui.QColor("#090c12"))
            gap = 10
            available = self.width() - gap * 4
            width = max(80, available // 3)
            height = min(self.height() - 42, round(width * LOGICAL_HEIGHT / LOGICAL_WIDTH))
            top = max(20, (self.height() - height) // 2)
            self.rectangles = []
            for column in range(3):
                left = gap + column * (width + gap)
                rect = QtCore.QRect(left, top, width, height)
                self.rectangles.append(rect)
                painter.fillRect(rect, QtGui.QColor("#030407"))
                if self.images[column] is not None:
                    painter.drawImage(rect, self.images[column])
                painter.setPen(QtGui.QPen(QtGui.QColor("#435064"), 1))
                painter.drawRect(rect.adjusted(0, 0, -1, -1))
                painter.setPen(QtGui.QColor("#dce5f2"))
                painter.drawText(rect.left(), rect.bottom() + 20, f"POSITION {column + 1}")

    class CropCanvas(QtWidgets.QWidget):
        framingChanged = QtCore.Signal(float, float, float)

        def __init__(self, image, target_size, framing, segments=1, selected_segment=None):
            super().__init__()
            self.setMinimumSize(820, 430)
            self.setMouseTracking(True)
            self.source_size = image.size
            self.image = _qt_image(QtGui, image)
            self.target_size = target_size
            self.framing = normalized_framing(framing)
            self.segments = segments
            self.selected_segment = selected_segment
            self.image_rect = QtCore.QRectF()
            self.crop_rect = QtCore.QRectF()
            self.drag_offset = QtCore.QPointF()
            self.dragging = False

        def set_framing(self, framing):
            self.framing = normalized_framing(framing)
            self.update()

        def set_image(self, image):
            self.source_size = image.size
            self.image = _qt_image(QtGui, image)
            self.update()

        def paintEvent(self, event):
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            painter.fillRect(self.rect(), QtGui.QColor("#07090d"))
            margin = 22.0
            available_width = max(1.0, self.width() - margin * 2)
            available_height = max(1.0, self.height() - margin * 2)
            scale = min(
                available_width / self.source_size[0],
                available_height / self.source_size[1],
            )
            width = self.source_size[0] * scale
            height = self.source_size[1] * scale
            left = (self.width() - width) / 2
            top = (self.height() - height) / 2
            self.image_rect = QtCore.QRectF(left, top, width, height)
            painter.drawImage(self.image_rect, self.image)

            source_box = crop_box(self.source_size, self.target_size, self.framing)
            crop_left = left + source_box[0] * scale
            crop_top = top + source_box[1] * scale
            crop_width = (source_box[2] - source_box[0]) * scale
            crop_height = (source_box[3] - source_box[1]) * scale
            self.crop_rect = QtCore.QRectF(crop_left, crop_top, crop_width, crop_height)

            shade = QtGui.QColor(3, 5, 9, 190)
            painter.fillRect(QtCore.QRectF(left, top, width, max(0.0, crop_top - top)), shade)
            painter.fillRect(
                QtCore.QRectF(left, self.crop_rect.bottom(), width, max(0.0, self.image_rect.bottom() - self.crop_rect.bottom())),
                shade,
            )
            painter.fillRect(
                QtCore.QRectF(left, crop_top, max(0.0, crop_left - left), crop_height), shade
            )
            painter.fillRect(
                QtCore.QRectF(self.crop_rect.right(), crop_top, max(0.0, self.image_rect.right() - self.crop_rect.right()), crop_height),
                shade,
            )

            painter.setPen(QtGui.QPen(QtGui.QColor("#ff64b5"), 3))
            painter.drawRect(self.crop_rect)
            guide = QtGui.QPen(QtGui.QColor(110, 225, 245, 145), 1)
            painter.setPen(guide)
            for fraction in (1 / 3, 2 / 3):
                painter.drawLine(
                    QtCore.QPointF(self.crop_rect.left() + self.crop_rect.width() * fraction, self.crop_rect.top()),
                    QtCore.QPointF(self.crop_rect.left() + self.crop_rect.width() * fraction, self.crop_rect.bottom()),
                )
                painter.drawLine(
                    QtCore.QPointF(self.crop_rect.left(), self.crop_rect.top() + self.crop_rect.height() * fraction),
                    QtCore.QPointF(self.crop_rect.right(), self.crop_rect.top() + self.crop_rect.height() * fraction),
                )
            if self.segments == 3:
                painter.setPen(QtGui.QPen(QtGui.QColor("#f6f0e5"), 2))
                for fraction in (1 / 3, 2 / 3):
                    x = self.crop_rect.left() + self.crop_rect.width() * fraction
                    painter.drawLine(
                        QtCore.QPointF(x, self.crop_rect.top()),
                        QtCore.QPointF(x, self.crop_rect.bottom()),
                    )
                if self.selected_segment is not None:
                    segment_width = self.crop_rect.width() / 3
                    selected = QtCore.QRectF(
                        self.crop_rect.left() + segment_width * self.selected_segment,
                        self.crop_rect.top(),
                        segment_width,
                        self.crop_rect.height(),
                    )
                    painter.setPen(QtGui.QPen(QtGui.QColor("#61dff5"), 3))
                    painter.drawRect(selected)

            painter.setPen(QtGui.QColor("#f6f0e5"))
            label = f"OUTPUT  {self.target_size[0]} × {self.target_size[1]}   ·   {self.framing['zoom']:.2f}×"
            painter.drawText(
                QtCore.QPointF(self.crop_rect.left() + 8, self.crop_rect.top() + 20),
                label,
            )

        def mousePressEvent(self, event):
            if event.button() != QtCore.Qt.MouseButton.LeftButton or self.crop_rect.isEmpty():
                return
            point = event.position()
            if self.crop_rect.contains(point):
                self.drag_offset = point - self.crop_rect.topLeft()
            else:
                self.drag_offset = QtCore.QPointF(
                    self.crop_rect.width() / 2,
                    self.crop_rect.height() / 2,
                )
            self.dragging = True
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            self._move_crop(point)

        def mouseMoveEvent(self, event):
            if self.dragging:
                self._move_crop(event.position())
            elif self.crop_rect.contains(event.position()):
                self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            else:
                self.unsetCursor()

        def mouseReleaseEvent(self, event):
            self.dragging = False
            self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)

        def wheelEvent(self, event):
            delta = event.angleDelta().y() or event.pixelDelta().y()
            if delta == 0:
                event.ignore()
                return
            direction = 1 if delta > 0 else -1
            factor = 1.12 if direction > 0 else 1 / 1.12
            zoom = max(1.0, min(8.0, self.framing["zoom"] * factor))
            self.framing["zoom"] = zoom
            self.framingChanged.emit(
                self.framing["focus_x"], self.framing["focus_y"], zoom
            )
            self.update()
            event.accept()

        def _move_crop(self, point):
            desired_left = point.x() - self.drag_offset.x()
            desired_top = point.y() - self.drag_offset.y()
            x_range = max(0.0, self.image_rect.width() - self.crop_rect.width())
            y_range = max(0.0, self.image_rect.height() - self.crop_rect.height())
            desired_left = max(
                self.image_rect.left(),
                min(self.image_rect.left() + x_range, desired_left),
            )
            desired_top = max(
                self.image_rect.top(),
                min(self.image_rect.top() + y_range, desired_top),
            )
            focus_x = 0.5 if x_range < 0.001 else (desired_left - self.image_rect.left()) / x_range
            focus_y = 0.5 if y_range < 0.001 else (desired_top - self.image_rect.top()) / y_range
            self.framing.update({"focus_x": focus_x, "focus_y": focus_y})
            self.framingChanged.emit(focus_x, focus_y, self.framing["zoom"])
            self.update()

    class CropDialog(QtWidgets.QDialog):
        previewFrame = QtCore.Signal(object)
        previewError = QtCore.Signal(str)

        def __init__(
            self,
            parent,
            title,
            image,
            target_size,
            framing,
            preview_source,
            segments=1,
            selected_segment=None,
        ):
            super().__init__(parent)
            self.setWindowTitle(title)
            self.resize(1000, 650)
            self.result_framing = normalized_framing(framing)
            self.preview_source = preview_source
            self.preview_stop = threading.Event()
            self.preview_thread = None
            layout = QtWidgets.QVBoxLayout(self)
            heading = QtWidgets.QLabel(title.upper())
            heading.setStyleSheet("font-size: 18px; font-weight: 700; color: #ff64b5")
            self.help_text = QtWidgets.QLabel(
                "Drag the pink output rectangle over the live full-source preview. Scroll over the image or use Zoom."
            )
            self.help_text.setStyleSheet("color: #9fb0c8")
            layout.addWidget(heading)
            layout.addWidget(self.help_text)
            self.canvas = CropCanvas(
                image, target_size, self.result_framing, segments, selected_segment
            )
            self.canvas.framingChanged.connect(self._canvas_changed)
            self.previewFrame.connect(self.canvas.set_image)
            self.previewError.connect(self._preview_error)
            layout.addWidget(self.canvas, 1)

            controls = QtWidgets.QHBoxLayout()
            self.x_slider = self._slider(0, 1000)
            self.y_slider = self._slider(0, 1000)
            self.zoom_slider = self._slider(100, 800)
            self.x_value = QtWidgets.QLabel()
            self.y_value = QtWidgets.QLabel()
            self.zoom_value = QtWidgets.QLabel()
            for label, slider, value in (
                ("Horizontal", self.x_slider, self.x_value),
                ("Vertical", self.y_slider, self.y_value),
                ("Zoom", self.zoom_slider, self.zoom_value),
            ):
                controls.addWidget(QtWidgets.QLabel(label))
                controls.addWidget(slider, 1)
                controls.addWidget(value)
            layout.addLayout(controls)

            buttons = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.StandardButton.Cancel
                | QtWidgets.QDialogButtonBox.StandardButton.Save
            )
            buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save).setText("Use framing")
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)
            self._set_controls(self.result_framing)
            self.setStyleSheet("""
                QDialog { background: #111620; color: #dce5f2; }
                QLabel { color: #dce5f2; }
                QPushButton { background: #263246; border: 1px solid #435064; border-radius: 5px; padding: 8px 14px; color: #dce5f2; }
                QSlider::groove:horizontal { height: 5px; background: #354155; }
                QSlider::handle:horizontal { width: 14px; margin: -5px 0; background: #ff64b5; border-radius: 7px; }
            """)
            if self.preview_source.animated:
                self.preview_thread = threading.Thread(
                    target=self._preview_loop,
                    name="zc360-crop-preview",
                    daemon=True,
                )
                self.preview_thread.start()
            else:
                self.help_text.setText(
                    "Drag the pink output rectangle over the full source. Scroll over the image or use Zoom."
                )

        def _slider(self, minimum, maximum):
            slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            slider.setRange(minimum, maximum)
            slider.valueChanged.connect(self._controls_changed)
            return slider

        def _set_controls(self, framing):
            for slider, value in (
                (self.x_slider, round(framing["focus_x"] * 1000)),
                (self.y_slider, round(framing["focus_y"] * 1000)),
                (self.zoom_slider, round(framing["zoom"] * 100)),
            ):
                slider.blockSignals(True)
                slider.setValue(value)
                slider.blockSignals(False)
            self.x_value.setText(f"{framing['focus_x']:.2f}")
            self.y_value.setText(f"{framing['focus_y']:.2f}")
            self.zoom_value.setText(f"{framing['zoom']:.2f}×")

        def _controls_changed(self):
            self.result_framing = normalized_framing({
                "focus_x": self.x_slider.value() / 1000.0,
                "focus_y": self.y_slider.value() / 1000.0,
                "zoom": self.zoom_slider.value() / 100.0,
            })
            self._set_controls(self.result_framing)
            self.canvas.set_framing(self.result_framing)

        def _canvas_changed(self, focus_x, focus_y, zoom):
            self.result_framing = normalized_framing({
                "focus_x": focus_x,
                "focus_y": focus_y,
                "zoom": zoom,
            })
            self._set_controls(self.result_framing)
            self.canvas.set_framing(self.result_framing)

        def _preview_loop(self):
            while not self.preview_stop.is_set():
                started = time.monotonic()
                try:
                    frame, delay = self.preview_source.frame()
                except Exception as exc:
                    if not self.preview_stop.is_set():
                        self.previewError.emit(f"Live preview stopped: {exc}")
                    return
                self.previewFrame.emit(frame)
                self.preview_stop.wait(max(0.0, delay - (time.monotonic() - started)))

        def _preview_error(self, message):
            self.help_text.setText(message)
            self.help_text.setStyleSheet("color: #ffcf66")

        def done(self, result):
            self.preview_stop.set()
            if self.preview_thread:
                self.preview_thread.join(timeout=0.35)
            if self.preview_source:
                self.preview_source.close()
                self.preview_source = None
            if self.preview_thread and self.preview_thread.is_alive():
                self.preview_thread.join(timeout=0.75)
            super().done(result)

    class MainWindow(QtWidgets.QMainWindow):
        operationDone = QtCore.Signal(str, bool)

        def __init__(self):
            super().__init__()
            self.setWindowTitle("ZC-360 Control")
            self.resize(1050, 680)
            self.state = read_state()
            self.framings = [dict(item) for item in self.state["panel_framing"]]
            self.shared_framing = dict(self.state["framing"])
            self.preview_timer = QtCore.QTimer(self)
            self.preview_timer.setSingleShot(True)
            self.preview_timer.timeout.connect(self.refresh_preview)
            self.operationDone.connect(self._operation_done)
            self._build()
            self._load_state()
            self.status_timer = QtCore.QTimer(self)
            self.status_timer.timeout.connect(self.refresh_daemon_status)
            self.status_timer.start(2000)
            self.refresh_daemon_status()

        def _build(self):
            root = QtWidgets.QWidget()
            self.setCentralWidget(root)
            outer = QtWidgets.QVBoxLayout(root)
            outer.setContentsMargins(18, 18, 18, 18)
            outer.setSpacing(12)

            status_row = QtWidgets.QHBoxLayout()
            title = QtWidgets.QLabel("ZC-360 CONTROL")
            title.setStyleSheet("font-size: 22px; font-weight: 700; color: #ff64b5")
            version = QtWidgets.QLabel(f"v{__version__}")
            version.setStyleSheet("color: #77869c")
            self.daemon_label = QtWidgets.QLabel("Checking daemon…")
            self.daemon_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
            help_button = QtWidgets.QPushButton("Help")
            help_button.clicked.connect(self.show_help)
            status_row.addWidget(title)
            status_row.addWidget(version)
            status_row.addStretch()
            status_row.addWidget(self.daemon_label)
            status_row.addWidget(help_button)
            outer.addLayout(status_row)

            mode_row = QtWidgets.QHBoxLayout()
            mode_row.addWidget(QtWidgets.QLabel("Display mode"))
            self.mode_combo = QtWidgets.QComboBox()
            self.mode_combo.addItem("Media", "media")
            self.mode_combo.addItem("System telemetry", "telemetry")
            self.mode_combo.addItem("Weather", "weather")
            self.mode_combo.addItem("External telemetry frames", "external")
            self.mode_combo.setToolTip(
                "Choose what the background renderer keeps on the three displays"
            )
            self.mode_combo.currentIndexChanged.connect(self._mode_changed)
            mode_row.addWidget(self.mode_combo, 1)
            outer.addLayout(mode_row)

            self.media_box = QtWidgets.QGroupBox("Media")
            source_grid = QtWidgets.QGridLayout(self.media_box)
            self.layout_combo = QtWidgets.QComboBox()
            self.layout_combo.addItem("One image across all panels", "span")
            self.layout_combo.addItem("Mirror on every panel", "mirror")
            self.layout_combo.addItem("Separate source per position", "panels")
            self.layout_combo.currentIndexChanged.connect(self._layout_changed)
            source_grid.addWidget(QtWidgets.QLabel("Layout"), 0, 0)
            source_grid.addWidget(self.layout_combo, 0, 1, 1, 2)
            self.source_labels = []
            self.source_edits = []
            self.source_buttons = []
            for row, label in enumerate(("Source", "Centre", "Right"), start=1):
                source_label = QtWidgets.QLabel(label)
                edit = QtWidgets.QLineEdit()
                edit.textChanged.connect(self.schedule_preview)
                button = QtWidgets.QPushButton("Browse…")
                button.clicked.connect(lambda checked=False, index=row - 1: self.browse(index))
                source_grid.addWidget(source_label, row, 0)
                source_grid.addWidget(edit, row, 1)
                source_grid.addWidget(button, row, 2)
                self.source_labels.append(source_label)
                self.source_edits.append(edit)
                self.source_buttons.append(button)
            outer.addWidget(self.media_box)

            self.telemetry_box = QtWidgets.QGroupBox("System telemetry")
            telemetry_layout = QtWidgets.QHBoxLayout(self.telemetry_box)
            telemetry_layout.addWidget(QtWidgets.QLabel(
                "Built-in CPU, temperature, memory, storage, network, and clock dashboard."
            ))
            telemetry_layout.addStretch()
            telemetry_layout.addWidget(QtWidgets.QLabel("Refresh"))
            self.telemetry_refresh = QtWidgets.QDoubleSpinBox()
            self.telemetry_refresh.setRange(0.5, 30.0)
            self.telemetry_refresh.setSuffix(" s")
            self.telemetry_refresh.valueChanged.connect(self.schedule_preview)
            telemetry_layout.addWidget(self.telemetry_refresh)
            outer.addWidget(self.telemetry_box)

            self.weather_box = QtWidgets.QGroupBox("Weather")
            weather_layout = QtWidgets.QGridLayout(self.weather_box)
            weather_layout.addWidget(QtWidgets.QLabel("Location"), 0, 0)
            self.weather_location = QtWidgets.QLineEdit()
            self.weather_location.setPlaceholderText("City or postal code, for example Osaka")
            self.weather_location.textChanged.connect(self.schedule_preview)
            weather_layout.addWidget(self.weather_location, 0, 1)
            weather_layout.addWidget(QtWidgets.QLabel("Units"), 0, 2)
            self.weather_units = QtWidgets.QComboBox()
            self.weather_units.addItem("Metric", "metric")
            self.weather_units.addItem("Imperial", "imperial")
            self.weather_units.currentIndexChanged.connect(self.schedule_preview)
            weather_layout.addWidget(self.weather_units, 0, 3)
            weather_note = QtWidgets.QLabel(
                "Uses Open-Meteo. Location is geocoded only when the renderer loads weather."
            )
            weather_note.setStyleSheet("color: #96a3b8")
            weather_layout.addWidget(weather_note, 1, 1, 1, 3)
            outer.addWidget(self.weather_box)

            self.external_box = QtWidgets.QGroupBox("External telemetry frames")
            external_layout = QtWidgets.QGridLayout(self.external_box)
            external_layout.addWidget(QtWidgets.QLabel("Frame folder"), 0, 0)
            self.external_directory = QtWidgets.QLineEdit()
            self.external_directory.setPlaceholderText("Folder written by your telemetry tool")
            self.external_directory.textChanged.connect(self.schedule_preview)
            external_layout.addWidget(self.external_directory, 0, 1)
            external_browse = QtWidgets.QPushButton("Browse…")
            external_browse.clicked.connect(self.browse_external)
            external_layout.addWidget(external_browse, 0, 2)
            external_layout.addWidget(QtWidgets.QLabel("Poll"), 0, 3)
            self.external_refresh = QtWidgets.QDoubleSpinBox()
            self.external_refresh.setRange(0.2, 60.0)
            self.external_refresh.setSuffix(" s")
            external_layout.addWidget(self.external_refresh, 0, 4)
            external_note = QtWidgets.QLabel(
                "Provide triptych.png, or panel-0.png through panel-2.png. Images only; no code is run."
            )
            external_note.setStyleSheet("color: #96a3b8")
            external_layout.addWidget(external_note, 1, 1, 1, 4)
            outer.addWidget(self.external_box)

            self.media_options = QtWidgets.QWidget()
            options = QtWidgets.QHBoxLayout(self.media_options)
            options.setContentsMargins(0, 0, 0, 0)
            self.fit_combo = QtWidgets.QComboBox()
            for label, value in (
                ("Cover", "cover"), ("Contain", "contain"),
                ("Stretch", "stretch"), ("Manual crop", "manual"),
            ):
                self.fit_combo.addItem(label, value)
            self.fit_combo.currentIndexChanged.connect(self.schedule_preview)
            self.fps = QtWidgets.QDoubleSpinBox()
            self.fps.setRange(0.5, 20.0)
            self.fps.setSingleStep(0.5)
            self.fps.setSuffix(" FPS")
            options.addWidget(QtWidgets.QLabel("Fit"))
            options.addWidget(self.fit_combo)
            options.addSpacing(16)
            options.addWidget(QtWidgets.QLabel("Playback"))
            options.addWidget(self.fps)
            options.addStretch()
            outer.addWidget(self.media_options)

            order_box = QtWidgets.QGroupBox("Physical panel mapping")
            order_layout = QtWidgets.QHBoxLayout(order_box)
            self.order_combos = []
            for position in ("Left position", "Centre position", "Right position"):
                order_layout.addWidget(QtWidgets.QLabel(position))
                combo = QtWidgets.QComboBox()
                for index in range(3):
                    combo.addItem(f"USB panel {index}", index)
                combo.currentIndexChanged.connect(self.schedule_preview)
                order_layout.addWidget(combo)
                self.order_combos.append(combo)
            outer.addWidget(order_box)

            self.preview = PreviewWidget()
            outer.addWidget(self.preview)

            edit_row = QtWidgets.QHBoxLayout()
            edit_row.setContentsMargins(10, 0, 10, 0)
            edit_row.setSpacing(10)
            self.edit_buttons = []
            for position in range(3):
                button = QtWidgets.QPushButton(f"✎  Edit position {position + 1}")
                button.clicked.connect(
                    lambda checked=False, index=position: self.edit_position(index)
                )
                edit_row.addWidget(button, 1)
                self.edit_buttons.append(button)
            outer.addLayout(edit_row)

            buttons = QtWidgets.QHBoxLayout()
            self.apply_button = QtWidgets.QPushButton("Apply & Play")
            self.apply_button.setObjectName("primary")
            self.apply_button.setToolTip("Save these settings and resume the selected display mode")
            self.apply_button.clicked.connect(self.apply)
            self.pause_button = QtWidgets.QPushButton("Pause playback")
            self.pause_button.setToolTip("Freeze the displays on their current frame without stopping USB")
            self.pause_button.clicked.connect(self.toggle_pause)
            test_button = QtWidgets.QPushButton("Identify panels (4s)")
            test_button.setToolTip("Temporarily show USB panel numbers, then restore the previous display")
            test_button.clicked.connect(self.send_test)
            blank_button = QtWidgets.QPushButton("Blank")
            blank_button.setToolTip("Display black frames until another mode is applied")
            blank_button.clicked.connect(self.blank)
            idle_button = QtWidgets.QPushButton("Stop rendering")
            idle_button.setToolTip("Stop producing frames and retain the current image on the panels")
            idle_button.clicked.connect(self.idle)
            for button in (self.apply_button, self.pause_button, test_button, blank_button, idle_button):
                buttons.addWidget(button)
            buttons.addStretch()
            outer.addLayout(buttons)
            footer = QtWidgets.QHBoxLayout()
            self.message = QtWidgets.QLabel(f"Settings: {state_path()}")
            self.message.setStyleSheet("color: #96a3b8")
            self.message.setWordWrap(True)
            credit = QtWidgets.QLabel("Made by Lucille Hon 💗")
            credit.setStyleSheet("color: #77869c; font-size: 12px")
            footer.addWidget(self.message, 1)
            footer.addWidget(credit)
            outer.addLayout(footer)
            outer.addStretch()

            self.setStyleSheet("""
                QWidget { background: #111620; color: #dce5f2; font-size: 13px; }
                QGroupBox { border: 1px solid #354155; border-radius: 7px; margin-top: 9px; padding-top: 10px; }
                QGroupBox::title { subcontrol-origin: margin; left: 10px; color: #9fb0c8; }
                QLineEdit, QComboBox, QDoubleSpinBox { background: #090c12; border: 1px solid #435064; border-radius: 5px; padding: 6px; }
                QPushButton { background: #263246; border: 1px solid #435064; border-radius: 5px; padding: 8px 14px; }
                QPushButton:hover { background: #34445d; }
                QPushButton#primary { background: #b83278; border-color: #ff64b5; }
                QSlider::groove:horizontal { height: 5px; background: #354155; }
                QSlider::handle:horizontal { width: 14px; margin: -5px 0; background: #ff64b5; border-radius: 7px; }
            """)

        def _load_state(self):
            mode_index = self.mode_combo.findData(self.state["mode"])
            self.mode_combo.setCurrentIndex(mode_index if mode_index >= 0 else 0)
            layout_index = self.layout_combo.findData(self.state["layout"])
            self.layout_combo.setCurrentIndex(max(0, layout_index))
            fit_index = self.fit_combo.findData(self.state["fit"])
            self.fit_combo.setCurrentIndex(max(0, fit_index))
            self.fps.setValue(self.state["frames_per_second"])
            self.telemetry_refresh.setValue(self.state["telemetry_refresh_seconds"])
            self.weather_location.setText(self.state["weather_location"])
            units_index = self.weather_units.findData(self.state["weather_units"])
            self.weather_units.setCurrentIndex(max(0, units_index))
            self.external_directory.setText(self.state["external_directory"])
            self.external_refresh.setValue(self.state["external_refresh_seconds"])
            self.source_edits[0].setText(self.state.get("source", ""))
            for index, source in enumerate(self.state.get("sources", [])[:3]):
                self.source_edits[index].setText(source)
            for combo, panel in zip(self.order_combos, self.state["panel_order"]):
                combo.setCurrentIndex(combo.findData(panel))
            self._layout_changed()
            self._mode_changed()
            self._update_pause_button()
            self.schedule_preview()

        def _mode_changed(self):
            mode = self.mode_combo.currentData()
            is_media = mode == "media"
            self.media_box.setVisible(is_media)
            self.media_options.setVisible(is_media)
            self.telemetry_box.setVisible(mode == "telemetry")
            self.weather_box.setVisible(mode == "weather")
            self.external_box.setVisible(mode == "external")
            for button in self.edit_buttons:
                button.setVisible(is_media)
            self.apply_button.setText("Apply & Play" if is_media else "Apply display mode")
            self.schedule_preview()

        def _layout_changed(self):
            panels = self.layout_combo.currentData() == "panels"
            labels = ("Left", "Centre", "Right") if panels else ("Source", "", "")
            for index in range(3):
                visible = panels or index == 0
                self.source_labels[index].setText(labels[index])
                self.source_labels[index].setVisible(visible)
                self.source_edits[index].setVisible(visible)
                self.source_buttons[index].setVisible(visible)
            for index, button in enumerate(self.edit_buttons):
                button.setText(
                    f"✎  Edit position {index + 1}"
                    if panels
                    else "✎  Edit shared framing"
                )
            self.schedule_preview()

        def browse(self, index):
            selected, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select media", "", MEDIA_FILTER)
            if selected:
                self.source_edits[index].setText(selected)

        def browse_external(self):
            selected = QtWidgets.QFileDialog.getExistingDirectory(
                self, "Select external telemetry frame folder", self.external_directory.text()
            )
            if selected:
                self.external_directory.setText(selected)

        def edit_position(self, position):
            preview_source = None
            try:
                state = self.draft_state()
                self.validate_draft(state)
                layout = state["layout"]
                if layout == "panels":
                    path = Path(state["sources"][position]).expanduser()
                    target_size = (LOGICAL_WIDTH, LOGICAL_HEIGHT)
                    framing = self.framings[position]
                    title = f"Position {position + 1} framing"
                    segments = 1
                    selected_segment = None
                else:
                    path = Path(state["source"]).expanduser()
                    target_size = (
                        (LOGICAL_WIDTH * 3, LOGICAL_HEIGHT)
                        if layout == "span"
                        else (LOGICAL_WIDTH, LOGICAL_HEIGHT)
                    )
                    framing = self.shared_framing
                    title = (
                        f"Full span framing · position {position + 1}"
                        if layout == "span"
                        else "Shared framing"
                    )
                    segments = 3 if layout == "span" else 1
                    selected_segment = position if layout == "span" else None
                preview_source = open_preview_source(path)
                image, _ = preview_source.frame()
                dialog = CropDialog(
                    self,
                    title,
                    image,
                    target_size,
                    framing,
                    preview_source,
                    segments,
                    selected_segment,
                )
                preview_source = None
                if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                    return
                if layout == "panels":
                    self.framings[position] = dict(dialog.result_framing)
                else:
                    self.shared_framing = dict(dialog.result_framing)
                self.fit_combo.setCurrentIndex(self.fit_combo.findData("manual"))
                self.schedule_preview()
                self.message.setText("Framing updated locally; use Apply & Play when ready")
            except Exception as exc:
                if preview_source:
                    preview_source.close()
                self._error(str(exc))

        def panel_order(self):
            return [combo.currentData() for combo in self.order_combos]

        def draft_state(self):
            state = default_state()
            state.update(self.state)
            state.update({
                "mode": self.mode_combo.currentData(),
                "layout": self.layout_combo.currentData(),
                "fit": self.fit_combo.currentData(),
                "frames_per_second": self.fps.value(),
                "panel_order": self.panel_order(),
                "framing": normalized_framing(self.shared_framing),
                "panel_framing": [normalized_framing(item) for item in self.framings],
                "telemetry_refresh_seconds": self.telemetry_refresh.value(),
                "weather_location": self.weather_location.text().strip(),
                "weather_units": self.weather_units.currentData(),
                "external_directory": self.external_directory.text().strip(),
                "external_refresh_seconds": self.external_refresh.value(),
            })
            if state["layout"] == "panels":
                state["sources"] = [edit.text().strip() for edit in self.source_edits]
                state.pop("source", None)
            else:
                state["source"] = self.source_edits[0].text().strip()
                state.pop("sources", None)
            return state

        def validate_draft(self, state):
            if sorted(state["panel_order"]) != [0, 1, 2]:
                raise ValueError("Each USB panel must be assigned exactly once")
            if state["mode"] == "media":
                sources = (
                    state.get("sources", [])
                    if state["layout"] == "panels"
                    else [state.get("source", "")]
                )
                missing = next(
                    (source for source in sources if not Path(source).expanduser().is_file()), None
                )
                if missing is not None:
                    raise ValueError(f"Media source does not exist: {missing or '(empty)'}")
            elif state["mode"] == "weather" and not state["weather_location"]:
                raise ValueError("Enter a city or postal code for weather mode")
            elif state["mode"] == "external":
                validate_external_directory(Path(state["external_directory"]).expanduser())

        def schedule_preview(self):
            self.preview_timer.start(250)

        def refresh_preview(self):
            try:
                state = self.draft_state()
                self.validate_draft(state)
                if state["mode"] == "media":
                    images = render_first_frame(state)
                elif state["mode"] == "telemetry":
                    dashboard = SystemDashboard(state)
                    images, _ = dashboard.frame()
                elif state["mode"] == "weather":
                    images = weather_placeholder(state["weather_location"], state["panel_order"])
                else:
                    images = load_external_frames(
                        Path(state["external_directory"]).expanduser(), state["panel_order"]
                    )
                ordered = [_qt_image(QtGui, images[index]) for index in state["panel_order"]]
                self.preview.set_images(ordered)
                self.message.setText("Preview is local; apply the mode to update the displays")
            except Exception as exc:
                self.preview.set_images([None, None, None])
                self.message.setText(str(exc))

        def apply(self):
            try:
                state = self.draft_state()
                self.validate_draft(state)
                state["paused"] = False
                for key in (
                    "identify_until", "framing_editing", "framing_draft_fit",
                    "framing_draft", "panel_framing_draft",
                ):
                    state.pop(key, None)
                write_state(None, state)
                self.state = read_state()
                self._update_pause_button()
                self.message.setText("Saved. The background renderer will apply this display mode now.")
            except Exception as exc:
                self._error(str(exc))

        def toggle_pause(self):
            state = read_state()
            state["paused"] = not state["paused"]
            write_state(None, state)
            self.state = state
            self._update_pause_button()
            self.message.setText("Playback paused" if state["paused"] else "Playback resumed")

        def _update_pause_button(self):
            paused = bool(self.state.get("paused", False))
            self.pause_button.setText("Resume playback" if paused else "Pause playback")

        def send_test(self):
            state = read_state()
            state["identify_until"] = time.time() + 4.0
            write_state(None, state)
            self.message.setText("Identifying panels for four seconds…")
            self._send_async(
                {index: test_pattern(index) for index in range(3)},
                "Panel labels shown temporarily; the previous display resumes automatically",
            )

        def blank(self):
            state = read_state()
            state.update({"mode": "blank", "paused": False})
            write_state(None, state)
            black = Image.new("RGB", (LOGICAL_WIDTH, LOGICAL_HEIGHT), (0, 0, 0))
            self.message.setText("Blanking panels…")
            self._send_async({index: black for index in range(3)}, "Panels blanked")

        def idle(self):
            state = read_state()
            state.update({"mode": "idle", "paused": False})
            write_state(None, state)
            self.message.setText("Rendering stopped; the current framebuffer is retained")

        def refresh_daemon_status(self):
            try:
                status = daemon_status(timeout=0.5)
                panels = len(status.get("panels", []))
                detail = f"{panels}/3 panels ready" if panels else status.get("message", "online")
                self.daemon_label.setText(f"● daemon online · {detail}")
                self.daemon_label.setStyleSheet("color: #72e5b1")
            except Exception as exc:
                self.daemon_label.setText(f"○ daemon unavailable · {exc}")
                self.daemon_label.setStyleSheet("color: #ffcf66")

        def show_help(self):
            dialog = QtWidgets.QMessageBox(self)
            dialog.setWindowTitle("ZC-360 help")
            dialog.setIcon(QtWidgets.QMessageBox.Icon.Information)
            dialog.setText("Recommended workflow")
            dialog.setInformativeText(
                "1. Choose a display mode.\n"
                "2. Select valid media, enter a weather location, or choose an external frame folder.\n"
                "3. For media, use Edit position to frame it, then choose Use framing.\n"
                "4. Select Apply & Play or Apply display mode. Closing this window is safe.\n\n"
                "MP4 duration is not limited. Long videos loop normally; ordinary H.264 at about "
                "20 FPS is already plenty for the 640 × 180 panels.\n\n"
                "Identify panels shows labels for four seconds and then returns automatically. "
                "Pause freezes the current frame; use Resume playback to continue.\n\n"
                "If old panel labels are already stuck, run `zc360ctl resume`, then Apply again. "
                "A missing-source message usually means a path is empty or mistyped. The USB daemon "
                "does not need to be restarted for display changes."
            )
            dialog.exec()

        def _error(self, message):
            QtWidgets.QMessageBox.warning(self, "ZC-360", message)
            self.message.setText(message)

        def _send_async(self, frames, success):
            def worker():
                try:
                    send_fan_frames(frames)
                    self.operationDone.emit(success, True)
                except Exception as exc:
                    self.operationDone.emit(str(exc), False)

            threading.Thread(target=worker, name="zc360-gui-send", daemon=True).start()

        def _operation_done(self, message, succeeded):
            if succeeded:
                self.message.setText(message)
            else:
                self._error(message)

    return MainWindow


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        print(f"zc360-gui {__version__}")
        return 0
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
    except ImportError as exc:
        print("zc360-gui: PySide6 is not installed; reinstall with jonsbo-zc360[gui]", file=sys.stderr)
        return 1
    application = QtWidgets.QApplication([sys.argv[0], *argv])
    application.setApplicationName("ZC-360 Control")
    window_class = create_window_class(QtCore, QtGui, QtWidgets)
    window = window_class()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
