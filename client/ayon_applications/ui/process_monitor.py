"""Process Monitor UI for launched processes."""
from __future__ import annotations

import contextlib
import enum
from datetime import datetime, timezone
from logging import getLogger
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Optional, Union

from ayon_applications.process import ProcessInfo, ProcessManager
from ayon_core.style import load_stylesheet
from ayon_core.tools.utils import get_ayon_qt_app
from qtpy import QtCore, QtGui, QtWidgets
from qtpy.QtCore import (
    QModelIndex,
    QPersistentModelIndex,
    QRunnable,
    QThreadPool,
    Slot,
)

from .ansi_parser import AnsiToHtmlConverter

DEFAULT_RELOAD_INTERVAL = 2000

if TYPE_CHECKING:
    from types import TracebackType

ModelIndex = Union[QModelIndex, QPersistentModelIndex]


class FileChangeWatcher(QtCore.QObject):
    """Qt-based file watcher with rotation handling and debounce."""
    changed = QtCore.Signal(object)  # emits Path (as object)

    def __init__(self, parent=None, debounce_ms: int = 150) -> None:
        super().__init__(parent)
        self._watcher = QtCore.QFileSystemWatcher(self)
        self._target: Optional[Path] = None

        # debounce timer to coalesce bursts of events
        # QFileSystemWatcher can emit multiple events for a single change
        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(debounce_ms)
        self._debounce.timeout.connect(self._emit_changed)

        self._watcher.fileChanged.connect(self._on_any_change)
        self._watcher.directoryChanged.connect(self._on_any_change)

    def set_target(self, file_path: Optional[Path]) -> None:
        """Start watching given file and its parent directory."""
        self.stop()
        self._target = file_path
        if not file_path:
            return

        # Clear watched paths
        for path in self._watcher.files():
            with contextlib.suppress(Exception):
                self._watcher.removePath(path)

        # Watch the file (if present)
        with contextlib.suppress(Exception):
            self._watcher.files()
            self._watcher.addPath(str(file_path))

    def stop(self) -> None:
        """Stop watching."""
        self._debounce.stop()
        files = self._watcher.files()
        if files:
            self._watcher.removePaths(files)
        dirs = self._watcher.directories()
        if dirs:
            self._watcher.removePaths(dirs)

    @QtCore.Slot(str)
    def _on_any_change(self, _path: str) -> None:
        """Handle file changes."""
        if not self._target:
            return
        # Debounce bursts of events.
        self._debounce.start()

    def _emit_changed(self) -> None:
        if self._target:
            self.changed.emit(self._target)


class CatchTime:
    """Context manager to measure execution time."""
    def __enter__(self):
        """Start timing.

        Returns:
            CatchTime: self, with start time initialized.

        """
        self.start = perf_counter()
        return self

    def __exit__(
            self,
            type_: Optional[type[BaseException]],
            value: Optional[BaseException],
            traceback: Optional[TracebackType],
    ) -> Optional[bool]:
        """Stop timing and store elapsed time.

        Returns:
            Optional[bool]: None

        """
        self.time = perf_counter() - self.start
        self.readout = f"Time: {self.time:.3f} seconds"
        return None


class ProcessRefreshWorkerSignals(QtCore.QObject):
    """Signals for ProcessRefreshWorker.

    Signals can be defined only in classes derived from QObject.
    """
    finished = QtCore.Signal(list)  # Emits list of ProcessInfo
    error = QtCore.Signal(str)


class ProcessRefreshWorker(QRunnable):
    """Worker thread for refreshing process data from the database."""

    def __init__(self, manager: ProcessManager):
        """Initialize the worker."""
        super().__init__()
        self.signals = ProcessRefreshWorkerSignals()
        self.signature = self.__class__.__name__
        self._manager = manager
        self._log = getLogger(self.signature)

    @Slot()
    def run(self) -> None:
        """Refresh process data in background thread."""
        with CatchTime() as timer:
            try:
                processes = self._manager.get_all_process_info()
                self.signals.finished.emit(processes)
            except Exception as e:  # noqa: BLE001
                self.signals.error.emit(str(e))
        self._log.debug(
            "Refresh from db completed in %s", timer.readout)


class FileContentWorkerSignals(QtCore.QObject):
    """Signals for FileContentWorker.

    Signals can be defined only in classes derived from QObject.
    """
    finished = QtCore.Signal(str)  # Emits file content
    error = QtCore.Signal(str)


class FileContentWorker(QRunnable):
    """Worker thread for loading file content."""

    def __init__(self, file_path: Path):
        """Initialize the worker.

        Args:
            file_path (Path): Path to the file to load.

        """
        super().__init__()
        self.signals = FileContentWorkerSignals()
        self.signature = self.__class__.__name__
        self._file_path = file_path
        self._log = getLogger(self.signature)

    @Slot()
    def run(self) -> None:
        """Load file content in background thread."""
        self._log.debug("Loading file content from %s", self._file_path)
        try:
            if not self._file_path or not Path(self._file_path).exists():
                self.signals.finished.emit("Output file not found")
                return

            content = Path(self._file_path).read_text(
                encoding="utf-8", errors="replace")
            self.signals.finished.emit(content)
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(f"Error reading file: {e}")


class CleanupWorkerSignals(QtCore.QObject):
    """Signals for CleanupWorker.

    Signals can be defined only in classes derived from QObject.
    """
    # Emits (deleted_processes, deleted_files)
    finished = QtCore.Signal(int)
    error = QtCore.Signal(str)


class CleanupWorker(QRunnable):
    """Worker thread for cleanup operations."""

    def __init__(self,
                 manager: ProcessManager,
                 cleanup_type: str,
                 process_hash: Optional[str] = None) -> None:
        """Initialize the worker.

        Args:
            manager (ApplicationManager): Application manager instance.
            cleanup_type (str): Type of cleanup ("inactive" or "single").
            process_hash (Optional[str]): Hash of the process to delete
                if cleanup_type is "single".

        """
        super().__init__()
        self.signals = CleanupWorkerSignals()
        self.signature = f"{self.__class__.__name__} ({cleanup_type})"
        self._manager = manager
        self._cleanup_type = cleanup_type  # "inactive" or "single"
        self._process_hash = process_hash
        self._log = getLogger(self.signature)

    @Slot()
    def run(self) -> None:
        """Perform cleanup in background thread."""
        self._log.debug(
            "Starting cleanup of type: %s", self._cleanup_type)
        try:
            if self._cleanup_type == "inactive":
                self._cleanup_inactive()
            elif self._cleanup_type == "single":
                self._remove_selected()
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(str(e))

    def _cleanup_inactive(self) -> None:
        """Clean up inactive processes."""
        deleted_count = self._manager.delete_inactive_processes()
        self.signals.finished.emit(deleted_count)

    def _remove_selected(self) -> None:
        """Remove a single selected process."""
        if not self._process_hash:
            self.signals.error.emit("No process hash provided")
            return

        self._manager.delete_process_info(self._process_hash)
        self.signals.finished.emit(1)


class ProcessTreeModel(QtGui.QStandardItemModel):
    """Model for displaying process information.

    Each row represents a ProcessInfo. ProcessInfo objects are stored in
    Qt.UserRole on the first item of the row for easy retrieval.
    """

    _running_icon: QtGui.QIcon
    _stopped_icon: QtGui.QIcon
    _unknown_icon: QtGui.QIcon
    ICON_SIZE = 12

    def __init__(
            self,
            manager: ProcessManager,
            parent: Optional[QtCore.QObject] = None,
            ) -> None:
        """Initialize the model.

        Args:
            manager (ProcessManager): Process manager
            parent (Optional[QtCore.QObject]): Parent QObject.

        """
        super().__init__(parent)
        self._generate_icons(size=self.ICON_SIZE)
        self._processes: list[ProcessInfo] = []
        self._manager = manager
        # Columns
        self.headers = [
            "Name", "Executable", "PID", "Status", "Created", "Start Time",
            "Output File", "Hash"
        ]
        self.columns = enum.IntEnum(  # type: ignore[misc]
            "columns",
            {
                name.replace(" ", "_").upper(): i
                for i, name in enumerate(self.headers)
            },
        )
        self.setColumnCount(len(self.headers))
        self.setHorizontalHeaderLabels(self.headers)

    def _status_icon(self, process: ProcessInfo) -> QtGui.QIcon:
        """Return a small colored circle icon representing process status.

        Args:
            process (ProcessInfo): ProcessInfo object.

        Returns:
            QtGui.QIcon: Colored circle icon.

        """
        if process.pid:
            return self._running_icon if process.active else self._stopped_icon
        return self._unknown_icon

    @classmethod
    def _generate_icons(cls, size: int = 12) -> None:
        """Generate static icons for process statuses.

        Args:
            size (int): Size of the icons in pixels.

        """
        if not hasattr(cls, "_running_icon"):
            cls._running_icon = cls._create_icon(
                QtGui.QColor(0, 180, 0), size)  # green = running
        if not hasattr(cls, "_stopped_icon"):
            cls._stopped_icon = cls._create_icon(
                QtGui.QColor(200, 0, 0), size)  # red = stopped
        if not hasattr(cls, "_unknown_icon"):
            cls._unknown_icon = cls._create_icon(
                QtGui.QColor(140, 140, 140), size)  # gray = unknown

    @staticmethod
    def _create_icon(color: QtGui.QColor, size: int = 12) -> QtGui.QIcon:
        """Create a colored circle icon.

        Args:
            color (QtGui.QColor): Color of the circle.
            size (int): Size of the icon in pixels.

        Returns:
            QtGui.QIcon: Colored circle icon.

        """
        pix = QtGui.QPixmap(size, size)
        pix.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pix)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setBrush(QtGui.QBrush(color))
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.drawEllipse(1, 1, size - 2, size - 2)
        painter.end()
        return QtGui.QIcon(pix)

    def update_processes(self, processes: list[ProcessInfo]) -> None:
        """Replace current content with provided processes.

        Args:
            processes (list[ProcessInfo]): List of ProcessInfo objects.

        """
        self._processes = list(processes)
        root_item = self.invisibleRootItem()
        root_item.removeRows(0, root_item.rowCount())

        for process in self._processes:
            row_items = []
            bg = self._data_background_role(process)
            for col in range(len(self.headers)):
                text = self._data_display_role(col, process)
                item = QtGui.QStandardItem(
                    str(text) if text is not None else ""
                )
                item.setEditable(False)
                # Store ProcessInfo in UserRole on the first column item
                if col == self.columns.NAME:
                    item.setData(process, QtCore.Qt.ItemDataRole.UserRole)
                    item.setIcon(self._status_icon(process))
                # Set background color for entire row via individual items
                if bg is not None:
                    item.setBackground(bg)
                row_items.append(item)
            root_item.appendRow(row_items)

    def get_process_at_row(self, row: int) -> Optional[ProcessInfo]:
        """Get the ProcessInfo stored at the given row.

        Args:
            row (int): Row index.

        Returns:
            Optional[ProcessInfo]: ProcessInfo object or None if not found.

        """
        item = self.item(row, self.columns.NAME)
        return None if item is None else item.data(
            QtCore.Qt.ItemDataRole.UserRole)

    def _data_display_role(  # noqa: C901, PLR0911, PLR0912
            self, column: int, process: ProcessInfo) -> Optional[str]:
        """Get display text for a given column and process.

        Args:
            column (int): Column index.
            process (ProcessInfo): ProcessInfo object.

        Returns:
            Optional[str]: Display text or None if column is invalid.

        """
        if column == self.columns.NAME:
            return process.name
        if column == self.columns.EXECUTABLE:
            return process.executable.as_posix() or "N/A"
        if column == self.columns.PID:
            return str(process.pid) if process.pid else "N/A"
        if column == self.columns.STATUS:
            if process.pid:
                return "Running" if process.active else "Stopped"
            return "Unknown"
        if column == self.columns.CREATED:
            if process.created_at:
                try:
                    # Parse the UTC timestamp from SQLite and convert
                    # to local timezone
                    # SQLite CURRENT_TIMESTAMP format is "YYYY-MM-DD HH:MM:SS"
                    utc_dt = datetime.strptime(  # noqa: DTZ007
                        process.created_at,
                        "%Y-%m-%d %H:%M:%S")
                    # Assume it is UTC and convert to local timezone
                    utc_dt = utc_dt.replace(
                        tzinfo=timezone.utc)  # noqa: UP017
                    local_dt = utc_dt.astimezone()
                    return local_dt.strftime(
                        "%Y-%m-%d %H:%M:%S")
                except (ValueError, AttributeError):
                    # If parsing fails, return the original string
                    return process.created_at
            return "N/A"
        if column == self.columns.START_TIME:
            if process.start_time:
                try:
                    return datetime.fromtimestamp(
                        process.start_time,
                        tz=datetime.now().astimezone().tzinfo).strftime(
                            "%Y-%m-%d %H:%M:%S")
                except (OSError, OverflowError, ValueError):
                    return str(process.start_time)
            return "N/A"
        if column == self.columns.OUTPUT_FILE:
            return str(process.output) if process.output else "N/A"
        if column == self.columns.HASH:
            with contextlib.suppress(Exception):
                return (
                    process.hash or
                    self._manager.get_process_info_hash(process)
                )
            return "N/A"
        return None

    @staticmethod
    def _data_background_role(process: ProcessInfo) -> QtGui.QColor:
        if process.pid:
            is_running = process.active
            if is_running:
                return QtGui.QColor(200, 255, 200)  # Light green

            return QtGui.QColor(255, 200, 200)  # Light red
        return QtGui.QColor(240, 240, 240)  # Light gray

    def sort(  # noqa: C901
            self,
            column: int,
            order: QtCore.Qt.SortOrder = QtCore.Qt.SortOrder.AscendingOrder
    ) -> None:
        """Sort the model based on a column and order.

        Args:
            column (int): Column index to sort by.
            order (QtCore.Qt.SortOrder): Sort order (Ascending or Descending).

        """
        if not self._processes:
            return
        reverse = order == QtCore.Qt.SortOrder.DescendingOrder

        def key_func(  # noqa: PLR0911
                    process: ProcessInfo) -> Union[str, int, float]:
            """Key function for sorting based on column.

            Returns:
                Union[str, int]: Value to sort by.

            """
            if column == self.columns.NAME:
                return process.name or ""
            if column == self.columns.EXECUTABLE:
                return (
                    process.executable.as_posix()
                    if process.executable else "")
            if column == self.columns.PID:
                return process.pid or 0
            if column == self.columns.STATUS:
                return process.active
            if column == self.columns.CREATED:
                return process.created_at or ""
            if column == self.columns.START_TIME:
                return process.start_time or 0
            if column == self.columns.OUTPUT_FILE:
                return process.output.as_posix() if process.output else ""
            if column == self.columns.HASH:
                return process.hash or ""

            return ""

        sorted_processes = sorted(
            self._processes, key=key_func, reverse=reverse)
        self.update_processes(sorted_processes)


class ProcessMonitorController(QtCore.QObject):
    """Controller that encapsulates data logic for ProcessMonitorWindow.

    Handles ApplicationManager, QThreadPool, and QTimers.

    """
    processes_refreshed = QtCore.Signal(list)
    file_content = QtCore.Signal(str)
    cleanup_finished = QtCore.Signal(int)
    error = QtCore.Signal(str)

    def __init__(self, parent: Optional[QtCore.QObject] = None):
        """Initialize the controller."""
        super().__init__(parent)
        self.manager = ProcessManager()
        self._thread_pool = QThreadPool()
        self._file_watcher = FileChangeWatcher(self)
        self._file_watcher.changed.connect(self._on_file_changed)

        # Timers (created once)
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh)
        self._refresh_timer.setInterval(5000)

        self._file_reload_timer = QtCore.QTimer(self)
        self._file_reload_timer.timeout.connect(self._on_file_reload_timeout)
        self._file_reload_timer.setSingleShot(False)
        self._file_reload_interval = 2000
        self._file_reload_target: Optional[Path] = None

    # Timer control
    def start_timers(self) -> None:
        """Start the refresh timer if not already active."""
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def stop_timers(self) -> None:
        """Stop all active timers."""
        if self._refresh_timer.isActive():
            self._refresh_timer.stop()
        if self._file_reload_timer.isActive():
            self._file_reload_timer.stop()

    # Refresh
    def refresh(self) -> None:
        """Refresh process data in background thread."""
        try:
            worker = ProcessRefreshWorker(self.manager)
            worker.signals.finished.connect(self._on_refresh_finished)
            worker.signals.error.connect(self._on_error)
            self._thread_pool.start(worker)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))

    def _on_refresh_finished(self, processes: list[ProcessInfo]) -> None:
        """Handle completion of process refresh.

        Args:
            processes (list[ProcessInfo]): List of refreshed processes.

        """
        self.processes_refreshed.emit(processes)

    # File content loading
    def load_file_content(self, file_path: Optional[Path]) -> None:
        """Load file content in background thread.

        Args:
            file_path (Optional[Path]): Path to the file to load.

        """
        if not file_path:
            self.file_content.emit("No output file available")
            return
        try:
            worker = FileContentWorker(file_path)
            worker.signals.finished.connect(self._on_file_content_loaded)
            worker.signals.error.connect(self._on_error)
            self._thread_pool.start(worker)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))

    def _on_file_content_loaded(self, content: str) -> None:
        """Handle completion of file content loading."""
        self.file_content.emit(content)

    # Auto-reload control
    def start_file_watch(self, file_path: Path) -> None:
        """Start watching file for instant updates.

        Args:
            file_path (Path): Path to the file to watch.

        """
        self._file_watcher.set_target(file_path)
        # Also load immediately so UI updates without waiting for first event.
        self.load_file_content(file_path)

    def stop_file_watch(self) -> None:
        """Stop watching file."""
        self._file_watcher.stop()

    def start_file_reload(self, file_path: Path, interval: int = 2000) -> None:
        """Start auto-reloading file content at given interval."""
        self._file_reload_target = file_path
        self._file_reload_interval = interval
        self._file_reload_timer.start(self._file_reload_interval)

    def stop_file_reload(self) -> None:
        """Stop auto-reloading file content."""
        self._file_reload_timer.stop()
        self._file_reload_target = None

    def _on_file_reload_timeout(self) -> None:
        """Handle file reload timer timeout."""
        if self._file_reload_target:
            self.load_file_content(self._file_reload_target)

    @QtCore.Slot(object)
    def _on_file_changed(self, file_obj: object) -> None:
        """Instant update on file change."""
        try:
            file_path = Path(str(file_obj))
        except Exception:
            return
        self.load_file_content(file_path)

    # Cleanup operations
    def clean_inactive(self) -> None:
        """Clean all inactive processes in background thread."""
        try:
            worker = CleanupWorker(self.manager, "inactive")
            worker.signals.finished.connect(self._on_cleanup_finished)
            worker.signals.error.connect(self._on_error)
            self._thread_pool.start(worker)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))

    def delete_single(self, process_hash: str) -> None:
        """Delete a single process by its hash in background thread.

        Args:
            process_hash (str): Hash of the process to delete.

        """
        try:
            worker = CleanupWorker(self.manager, "single", process_hash)
            worker.signals.finished.connect(self._on_cleanup_finished)
            worker.signals.error.connect(self._on_error)
            self._thread_pool.start(worker)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))

    def _on_cleanup_finished(
            self, deleted_proc: int) -> None:
        """Handle completion of cleanup operation."""
        self.cleanup_finished.emit(deleted_proc)

    def _on_error(self, msg: str) -> None:
        """Handle errors from workers."""
        self.error.emit(msg)

    def shutdown(self) -> None:
        """Shutdown controller.

        Stop timers and wait for workers.

        """
        self.stop_timers()
        with contextlib.suppress(Exception):
            self.stop_file_watch()
        with contextlib.suppress(Exception):
            self._thread_pool.waitForDone()


class ProcessMonitorWindow(QtWidgets.QDialog):
    """Main window for the Process Monitor application."""
    def __init__(self, parent=None):  # noqa: ANN001
        """Initialize the main window."""
        super().__init__(parent)
        self._log = getLogger(self.__class__.__name__)
        self.setWindowTitle("AYON Process Monitor")
        self.setMinimumSize(1000, 600)

        # Controller instance (owns manager, thread pool, timers)
        self._controller = ProcessMonitorController(self)

        # Connect controller signals to UI slots
        # ANSI to HTML converter
        self._ansi_converter = AnsiToHtmlConverter()

        self._controller.processes_refreshed.connect(
            self._on_processes_refreshed
        )
        self._controller.file_content.connect(self._on_file_content)
        self._controller.cleanup_finished.connect(self._on_cleanup_finished)
        self._controller.error.connect(self._on_error)

        self._current_process = None
        self._is_loading = False

        self._setup_ui()

    def _setup_ui(self) -> None:
        """Set up the user interface."""
        central_widget = self
        main_layout = QtWidgets.QVBoxLayout(central_widget)

        # Toolbar
        toolbar_layout = self._setup_toolbar_ui()

        main_layout.addLayout(toolbar_layout)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)

        # Process tree view
        self._setup_tree_view_ui()

        splitter.addWidget(self._tree_view)

        # Output area
        self._setup_output_ui()

        splitter.addWidget(self._output_widget)

        # Give the tree view slightly more space than the output pane
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        main_layout.addWidget(splitter, 1)

        # Status bar
        self._status_bar = QtWidgets.QStatusBar()
        self._status_bar.setSizeGripEnabled(False)
        main_layout.addWidget(self._status_bar, 0)
        self._status_bar.showMessage("Ready")

    def _setup_output_ui(self) -> None:
        self._output_widget = QtWidgets.QWidget()
        output_layout = QtWidgets.QVBoxLayout(self._output_widget)

        output_label = QtWidgets.QLabel("Output Content:")
        output_label.setStyleSheet("font-weight: bold; margin-top: 10px;")

        # Use QTextEdit instead of QPlainTextEdit for HTML support
        self._output_text = QtWidgets.QTextEdit()
        self._output_text.setReadOnly(True)
        # Set monospace font for consistent output formatting
        font = QtGui.QFont("Noto Sans Mono, Courier New, monospace")
        font.setPointSize(9)
        self._output_text.setFont(font)
        self._output_text.setPlaceholderText(
            "Double-click a process row to view its output file content...")

        # Auto-reload checkbox
        self._auto_reload_checkbox = QtWidgets.QCheckBox(
            "Auto-reload output for running processes (every 2s)")
        self._auto_reload_checkbox.setChecked(True)
        self._auto_reload_checkbox.toggled.connect(
            self._on_auto_reload_toggled)

        output_layout.addWidget(output_label, 0)
        output_layout.addWidget(self._output_text, 1)
        output_layout.addWidget(self._auto_reload_checkbox, 0)

        # Ensure output widget expands and takes available space
        self._output_widget.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding
        )
        self._output_text.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding
        )

    def _setup_tree_view_ui(self) -> None:
        """Set up the process tree view UI."""
        self._tree_model = ProcessTreeModel(manager=self._controller.manager)
        self._tree_view = QtWidgets.QTreeView()
        self._tree_view.setModel(self._tree_model)
        self._tree_view.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._tree_view.setSortingEnabled(True)
        self._tree_view.doubleClicked.connect(self._on_row_double_clicked)

        header = self._tree_view.header()
        header.setStretchLastSection(True)
        for i in range(len(self._tree_model.headers)):
            header.setSectionResizeMode(
                i, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)

        # Make tree view expand to fill available space
        self._tree_view.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding
        )

    def _setup_toolbar_ui(self) -> QtWidgets.QHBoxLayout:
        """Set up the toolbar UI.

        Returns:
            QtWidgets.QHBoxLayout: The toolbar layout.

        """
        toolbar_layout = QtWidgets.QHBoxLayout()

        self._refresh_btn = QtWidgets.QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self._refresh_data)

        self._clean_inactive_btn = QtWidgets.QPushButton("Clean Inactive")
        self._clean_inactive_btn.setToolTip(
            "Remove all inactive processes from database")
        self._clean_inactive_btn.clicked.connect(
            self._clean_inactive_processes)

        self._clean_selected_btn = QtWidgets.QPushButton("Delete Selected")
        self._clean_selected_btn.setToolTip(
            "Delete selected process from database and its output file")
        self._clean_selected_btn.clicked.connect(
            self._delete_selected_process)

        # Loading indicator
        self._loading_label = QtWidgets.QLabel("Loading...")
        self._loading_label.setVisible(False)

        toolbar_layout.addWidget(self._refresh_btn, 0)
        toolbar_layout.addWidget(self._clean_inactive_btn, 0)
        toolbar_layout.addWidget(self._clean_selected_btn, 0)
        toolbar_layout.addStretch(1)
        toolbar_layout.addWidget(self._loading_label, 0)
        return toolbar_layout

    def _set_loading_state(self, *, loading: bool) -> None:
        """Set the loading state of the UI.

        Args:
            loading (bool): True to show loading state, False to hide.

        """
        self._is_loading = loading
        self._loading_label.setVisible(loading)

        # Disable buttons during loading
        buttons = [
            self._refresh_btn,
            self._clean_inactive_btn,
            self._clean_selected_btn,
        ]
        for btn in buttons:
            btn.setEnabled(not loading)

    def _refresh_data(self) -> None:
        """Refresh the process table data in background thread."""
        self._set_loading_state(loading=True)
        self._controller.refresh()

    def _on_processes_refreshed(self, processes: list[ProcessInfo]) -> None:
        selection_model = self._tree_view.selectionModel()
        selected_hashes = set()
        if selection_model.hasSelection():
            for index in selection_model.selectedRows():
                process = self._tree_model.get_process_at_row(index.row())
                if process:
                    selected_hashes.add(process.hash)

        # Update the model with new processes
        self._tree_model.update_processes(processes)

        # Restore selection based on saved hashes
        for row in range(self._tree_model.rowCount()):
            process = self._tree_model.get_process_at_row(row)
            if process:
                if process.hash in selected_hashes:
                    index = self._tree_model.index(row, 0)
                    selection_model.select(
                        index, (
                            QtCore.QItemSelectionModel.SelectionFlag.Select |
                            QtCore.QItemSelectionModel.SelectionFlag.Rows)
                    )

        self._status_bar.showMessage(f"Loaded {len(processes)} processes")
        self._set_loading_state(loading=False)
        self._log.debug("Process tree updated with new data")

    def _on_error(self, error_msg: str) -> None:
        """Handle refresh error.

        Args:
            error_msg (str): Error message to display.

        """
        self._status_bar.showMessage(f"Error: {error_msg}")
        self._set_loading_state(loading=False)

    def _on_row_double_clicked(self, index: QtCore.QModelIndex) -> None:
        """Handle double-click on a process row to load its output file.

        Args:
            index (QtCore.QModelIndex): Index of the clicked row.

        """
        if not index.isValid() or self._is_loading:
            return
        process = self._tree_model.get_process_at_row(index.row())
        if not process:
            return
        self._current_process = process
        self._load_output_content()
        if (
            self._auto_reload_checkbox.isChecked()
            and process.pid
            and process.active
        ):
            # self._controller.start_file_reload(process.output, 2000)
            # Prefer instant updates via watcher
            self._controller.stop_file_reload()
            self._controller.start_file_watch(process.output)
        else:
            # self._controller.stop_file_reload()
            self._controller.stop_file_watch()
            self._controller.stop_file_reload()

    def _load_output_content(self) -> None:
        """Load output file content in background thread."""
        if not self._current_process or not self._current_process.output:
            self._output_text.setPlainText("No output file available")
            return

        self._output_text.setPlainText("Loading file content...")

        self._controller.load_file_content(self._current_process.output)

    def _on_file_content(self, content: str) -> None:
        """Handle file content loaded.

        Args:
            content (str): Loaded file content.

        """
        sb = self._output_text.verticalScrollBar()
        # Detect whether user was at bottom before reload
        at_bottom = sb.value() == sb.maximum()
        prev_max = sb.maximum()
        prev_val = sb.value()
        ratio = (prev_val / prev_max) if prev_max > 0 else 1.0

        if not content:
            self._output_text.setPlainText("Output file is empty")
        else:
            html_content = self._ansi_converter.convert(content)
            self._output_text.setHtml(html_content)

        # Restore scroll after layout pass
        def restore_scroll():
            if at_bottom:
                sb.setValue(sb.maximum())
            else:
                sb.setValue(int(ratio * sb.maximum()))
        QtCore.QTimer.singleShot(0, restore_scroll)

    def _on_auto_reload_toggled(self, checked: bool) -> None:  # noqa: FBT001
        """Handle auto-reload checkbox toggle."""
        if not checked:
            # self._controller.stop_file_reload()
            self._controller.stop_file_watch()
            self._controller.stop_file_reload()
        elif (self._current_process and
              self._current_process.pid and
              self._current_process.active):

            self._controller.stop_file_reload()
            self._controller.start_file_watch(self._current_process.output)
            # self._controller.start_file_reload(
            #     self._current_process.output, DEFAULT_RELOAD_INTERVAL)

    def _clean_inactive_processes(self) -> None:
        """Clean all inactive processes from a database."""
        if self._is_loading:
            return

        reply = QtWidgets.QMessageBox.question(
            self,
            "Confirm Cleanup",
            (
                "This will remove all inactive processes from the database "
                "and delete their output files. Continue?"
            ),
            (
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No
            ),
            QtWidgets.QMessageBox.StandardButton.No,
        )

        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        self._set_loading_state(loading=True)
        self._status_bar.showMessage("Cleaning inactive processes...")

        self._controller.clean_inactive()

    def _delete_selected_process(self) -> None:
        """Delete the selected process from database and its output file."""
        if self._is_loading:
            return
        selection = self._tree_view.selectionModel()
        if not selection.hasSelection():
            QtWidgets.QMessageBox.information(
                self,
                "No Selection",
                "Please select a process to delete."
            )
            return
        indexes = selection.selectedRows()
        if not indexes:
            return
        process = self._tree_model.get_process_at_row(indexes[0].row())
        if not process:
            return
        reply = QtWidgets.QMessageBox.question(
            self,
            "Confirm Deletion",
            f"Delete process '{process.name}' "
            f"(PID: {process.pid}) and its output file?",
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )

        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        self._set_loading_state(loading=True)
        self._status_bar.showMessage("Deleting process...")

        self._controller.delete_single(process.hash)

    def _on_cleanup_finished(
            self,
            deleted_processes: int) -> None:
        """Handle cleanup completion."""
        self._refresh_data()  # Refresh the table
        self._status_bar.showMessage(
            f"Cleaned {deleted_processes} inactive processes. "
        )

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802
        """Apply stylesheet when the window is shown."""
        self.setStyleSheet(load_stylesheet())
        super().showEvent(event)
        self._controller.start_timers()
        self._refresh_data()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        """Clean up timers and threads when closing."""
        # Delegate shutdown to controller (stops timers and waits for workers)
        with contextlib.suppress(Exception):
            self._controller.shutdown()
        super().closeEvent(event)


def main() -> None:
    """Helper function to debug the tool."""
    app = get_ayon_qt_app()

    window = ProcessMonitorWindow()
    window.show()

    app.exec_()


if __name__ == "__main__":
    main()
