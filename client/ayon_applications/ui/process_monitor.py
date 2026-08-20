"""Process Monitor UI for launched processes."""
from __future__ import annotations

import contextlib
from collections import deque
from dataclasses import dataclass, field
import enum
from logging import getLogger
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Optional, Union

import arrow
from qtpy import QtCore, QtGui, QtWidgets
from qtpy.QtCore import (
    QModelIndex,
    QPersistentModelIndex,
    QRunnable,
    QThreadPool,
    Slot,
)

from ayon_applications.process import ProcessInfo, ProcessManager
from ayon_core.style import load_stylesheet
from ayon_core.tools.utils import get_ayon_qt_app

from .ansi_parser import AnsiToHtmlConverter

DEFAULT_RELOAD_INTERVAL = 2000

if TYPE_CHECKING:
    from types import TracebackType

ModelIndex = Union[QModelIndex, QPersistentModelIndex]

PROCESS_NAME_ROLE = QtCore.Qt.UserRole + 1
PROCESS_EXECUTABLE_ROLE = QtCore.Qt.UserRole + 2
PROCESS_PID_ROLE = QtCore.Qt.UserRole + 3
PROCESS_STATUS_ROLE = QtCore.Qt.UserRole + 4
PROCESS_STATE_ROLE = QtCore.Qt.UserRole + 5
PROCESS_CREATED_ROLE = QtCore.Qt.UserRole + 6
PROCESS_START_TIME_ROLE = QtCore.Qt.UserRole + 7
PROCESS_OUTPUT_FILE_ROLE = QtCore.Qt.UserRole + 8
PROCESS_HASH_ROLE = QtCore.Qt.UserRole + 9
ITEM_TYPE_ROLE = QtCore.Qt.UserRole + 10

MAIN_PROCESS_ITEM = 0
DESCENDANT_PROCESS_ITEM = 1


class ProcessState:
    RUNNING = 0
    CHILD_RUNNING = 1
    STOPPED = 2
    UNKNOWN = 3


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


class FuncWrapper:
    def __init__(self, func, *args, **kwargs):
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.result = None
        self._done = False

    def is_done(self) -> bool:
        return self._done

    def execute(self) -> None:
        try:
            self.result = self.func(*self.args, **self.kwargs)
        finally:
            self._done = True


class SimpleSignals(QtCore.QObject):
    finished = QtCore.Signal(object)
    error = QtCore.Signal(str)


class SimpleWorker(QRunnable):
    def __init__(self, func, *args, **kwargs) -> None:
        super().__init__()
        self.wrapper = FuncWrapper(func, *args, **kwargs)
        self._log = getLogger(func.__name__)
        self._signals = SimpleSignals()

    @property
    def finished(self):
        return self._signals.finished

    @property
    def error(self):
        return self._signals.error

    @Slot()
    def run(self) -> None:
        with CatchTime() as timer:
            try:
                self.wrapper.execute()
                self.finished.emit(self.wrapper)
            except Exception as exc:
                self.error.emit(str(exc))
        self._log.debug(
            "Descendants update from db completed in %s", timer.readout
        )


class ProcessDescendantsUpdateWorkerSignals(QtCore.QObject):
    """Signals for ProcessDescendantsUpdateWorker.

    Signals can be defined only in classes derived from QObject.
    """
    # Emits (parent_hash, list[ProcessInfo])
    finished = QtCore.Signal(object, list)
    error = QtCore.Signal(str)


class ProcessDescendantsUpdateWorker(QRunnable):
    """Worker thread for updating process descendants."""

    def __init__(
        self,
        manager: ProcessManager,
        parent_process: ProcessInfo,
    ):
        """Initialize the worker."""
        super().__init__()
        self.signals = ProcessDescendantsUpdateWorkerSignals()
        self.signature = self.__class__.__name__
        self._manager = manager
        self._parent_process = parent_process
        self._log = getLogger(self.signature)

    @Slot()
    def run(self) -> None:
        """Update process descendants data in background thread."""
        with CatchTime() as timer:
            try:
                descendants = self._manager.get_descendant_processes(
                    self._parent_process
                )
                parent_hash = self._parent_process.hash
                self.signals.finished.emit(parent_hash, descendants)
            except Exception as e:  # noqa: BLE001
                self.signals.error.emit(str(e))
        self._log.debug(
            "Descendants update from db completed in %s", timer.readout)


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

    def __init__(
        self,
        manager: ProcessManager,
        cleanup_type: str,
        process_hashes: set[str] | None = None
    ) -> None:
        """Initialize the worker.

        Args:
            manager (ApplicationManager): Application manager instance.
            cleanup_type (str): Type of cleanup ("inactive" or "selection").
            process_hashes (set[str] | None): Hashes of the processes
                to delete if cleanup_type is "selection".

        """
        super().__init__()
        self.signals = CleanupWorkerSignals()
        self.signature = f"{self.__class__.__name__} ({cleanup_type})"
        self._manager = manager
        self._cleanup_type = cleanup_type  # "inactive" or "selection"
        self._process_hashes = process_hashes
        self._log = getLogger(self.signature)

    @Slot()
    def run(self) -> None:
        """Perform cleanup in background thread."""
        self._log.debug(
            "Starting cleanup of type: %s", self._cleanup_type)
        try:
            if self._cleanup_type == "inactive":
                self._cleanup_inactive()
            elif self._cleanup_type == "selection":
                self._remove_selected()
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(str(e))

    def _cleanup_inactive(self) -> None:
        """Clean up inactive processes."""
        deleted_count = self._manager.delete_inactive_processes()
        self.signals.finished.emit(deleted_count)

    def _remove_selected(self) -> None:
        """Remove selected processes."""
        if not self._process_hashes:
            self.signals.error.emit("No process hashes provided")
            return

        self._manager.delete_processes_info(self._process_hashes)
        self.signals.finished.emit(1)


@dataclass
class _ModelState:
    refreshing_states: bool = False
    refreshing_processes: bool = False
    stopped: bool = True
    has_new_roots: bool = False
    rows_changed: bool = False
    descendants_to_update: dict[str, list] = field(default_factory=dict)


class ProcessTreeModel(QtGui.QStandardItemModel):
    """Model for displaying process information.

    Each row represents a ProcessInfo. ProcessInfo objects are stored in
    Qt.UserRole on the first item of the row for easy retrieval.
    """
    processes_refreshed = QtCore.Signal(int)
    error = QtCore.Signal(str)

    _running_icon: QtGui.QIcon
    _stopped_icon: QtGui.QIcon
    _unknown_icon: QtGui.QIcon
    _child_running_icon: QtGui.QIcon
    ICON_SIZE = 12

    # Columns
    HEADERS = [
        "Name", "Executable", "PID", "Status", "Created", "Start Time",
        "Output File", "Hash"
    ]
    COLUMNS = enum.IntEnum(  # type: ignore[misc]
        "columns",
        {
            name.replace(" ", "_").upper(): i
            for i, name in enumerate(HEADERS)
        },
    )
    _display_roles_mapping = {
        COLUMNS.NAME: PROCESS_NAME_ROLE,
        COLUMNS.EXECUTABLE: PROCESS_EXECUTABLE_ROLE,
        COLUMNS.PID: PROCESS_PID_ROLE,
        COLUMNS.STATUS: PROCESS_STATUS_ROLE,
        COLUMNS.CREATED: PROCESS_CREATED_ROLE,
        COLUMNS.START_TIME: PROCESS_START_TIME_ROLE,
        COLUMNS.OUTPUT_FILE: PROCESS_OUTPUT_FILE_ROLE,
        COLUMNS.HASH: PROCESS_HASH_ROLE,
    }

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
        self.setColumnCount(len(self.HEADERS))
        self.setHorizontalHeaderLabels(self.HEADERS)

        self._generate_icons(size=self.ICON_SIZE)
        self._manager = manager

        self._process_by_hash: dict[str, ProcessInfo] = {}
        self._items_by_hash: dict[str, QtGui.QStandardItem] = {}
        # Used to handle refreshes of items
        self._root_hashes: set[str] = set()

        # Helper mappings to reliably cleanup cached items
        self._hashes_to_process: list[str] = []

        self._state = _ModelState()

        self._thread_pool = QThreadPool()
        # Timers (created once)
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.timeout.connect(self._on_refresh_timer)
        self._refresh_timer.setInterval(5000)

    def start_workers(self):
        self._state.stopped = False
        self._refresh_timer.start()
        if not self._state.refreshing_states:
            self._state.refreshing_states = True
            worker = SimpleWorker(self._refresh_states)
            worker.error.connect(self.error)
            self._thread_pool.start(worker)

    def stop_workers(self):
        self._state.stopped = True
        self._refresh_timer.stop()
        self._thread_pool.waitForDone()

    def refresh(self) -> None:
        self._refresh_processes()

    def _refresh_processes(self) -> None:
        if self._state.refreshing_processes:
            return

        self._state.refreshing_processes = True
        self._refresh_timer.stop()

        processes = self._manager.get_all_process_info(invalidate=False)
        processes_by_hash = {
            process.hash: process
            for process in processes
        }
        _hashless_process = processes_by_hash.pop(None, None)

        root_item = self.invisibleRootItem()

        to_remove = set(self._root_hashes)

        new_items = []
        hashes_to_process = []
        for process_hash, process in processes_by_hash.items():
            if not process.stopped:
                hashes_to_process.append(process_hash)
            to_remove.discard(process_hash)
            item = self._items_by_hash.get(process_hash)
            if item is not None:
                if process.stopped:
                    self._set_item_state(
                        item, process, MAIN_PROCESS_ITEM
                    )
                    if item.rowCount() > 0:
                        self._update_descendants(process_hash, [])
                continue

            item = QtGui.QStandardItem()
            item.setEditable(False)
            item.setColumnCount(self.columnCount())
            new_items.append(item)

            self._fill_item_data(item, process, MAIN_PROCESS_ITEM)

            self._items_by_hash[process_hash] = item

        hashes_to_process.reverse()
        self._hashes_to_process = hashes_to_process

        if self._state.rows_changed:
            self._state.rows_changed = False
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(root_item.rowCount() - 1, self.columnCount() - 1),
                [QtCore.Qt.DisplayRole, QtCore.Qt.DecorationRole]
            )

        if new_items:
            self._state.has_new_roots = True
            root_item.appendRows(new_items)
            self.processes_refreshed.emit(root_item.rowCount())

        self._process_by_hash.update(processes_by_hash)

        descendants_to_update = self._state.descendants_to_update
        self._state.descendants_to_update = {}
        for process_hash, descendants in descendants_to_update.items():
            self._update_descendants(process_hash, descendants)

        # Keep items with descendants
        for process_hash in tuple(to_remove):
            item = self._items_by_hash.get(process_hash)
            if item is None:
                continue

            if item.rowCount() > 0:
                to_remove.discard(process_hash)

        self._remove_root_items(to_remove)

        self._state.refreshing_processes = False
        if not self._state.stopped:
            self._refresh_timer.start()

    def _refresh_states(self) -> None:
        """Refresh descendants for all root processes."""
        queue = deque(self._hashes_to_process)
        self._state.refreshing_states = False
        while queue:
            if self._state.stopped:
                return

            QtCore.QThread.msleep(1)

            process_hash = queue.popleft()
            process = self._process_by_hash.get(process_hash)
            item = self._items_by_hash.get(process_hash)
            if process is None or item is None:
                continue

            state = item.data(PROCESS_STATE_ROLE)
            if state == ProcessState.STOPPED:
                continue

            state = ProcessState.UNKNOWN
            is_running = False
            try:
                is_running = self._manager.invalidate_process(process)
                state = (
                    ProcessState.RUNNING
                    if is_running
                    else ProcessState.STOPPED
                )
                state = ProcessState.STOPPED

            except Exception as exc:
                self.error.emit(str(exc))

            self._set_item_state(
                item,
                process,
                MAIN_PROCESS_ITEM,
                state=state
            )

            try:
                descendants = self._manager.get_descendant_processes(process)
            except Exception as exc:
                descendants = []
                self.error.emit(str(exc))

            self._state.descendants_to_update[process_hash] = descendants

        if self._state.descendants_to_update:
            self._refresh_timer.timeout.emit()

        # Wait for 5 seconds
        # NOTE wait time is skipped if worker should be stopped or
        #   new root items were added to the model.
        for _ in range(50):
            if self._state.stopped:
                return
            if self._state.has_new_roots:
                self._state.has_new_roots = False
                break
            QtCore.QThread.msleep(100)

        if not self._state.refreshing_states and not self._state.stopped:
            worker = SimpleWorker(self._refresh_states)
            worker.error.connect(self.error)
            self._thread_pool.start(worker)

    def _remove_root_items(self, process_hashes: set[str]) -> None:
        root_item = self.invisibleRootItem()

        for process_hash in process_hashes:
            item = self._items_by_hash.pop(process_hash)
            while item.rowCount() > 0:
                child = item.takeChild(0, 0)
                child_hash = child.data(PROCESS_HASH_ROLE)
                self._items_by_hash.pop(child_hash)
                self._process_by_hash.pop(child_hash)

            root_item.takeRow(item.row())
            self._root_hashes.discard(process_hash)
            self._items_by_hash.pop(process_hash)
            self._process_by_hash.pop(process_hash)

    def _update_descendants(
        self, parent_hash: str, descendants: list[ProcessInfo]
    ) -> None:
        """Update descendant processes under a given parent process.

        Args:
            parent_hash (str): Hash of the parent process.
            descendants (list[ProcessInfo]): List of descendant
                ProcessInfo objects.

        """
        parent_item = self._items_by_hash.get(parent_hash)
        parent_proc = self._process_by_hash.get(parent_hash)
        if parent_item is None:
            return

        descendants_by_hash = {
            proc.hash: proc
            for proc in descendants
        }
        for row in reversed(range(parent_item.rowCount())):
            item = parent_item.child(row)
            child_hash = item.data(PROCESS_HASH_ROLE)
            proc = descendants_by_hash.pop(child_hash, None)
            if proc is None:
                self._items_by_hash.pop(child_hash)
                self._process_by_hash.pop(child_hash)
                parent_item.removeRow(row)
                continue

            if proc.stopped:
                self._set_item_state(
                    item, proc, DESCENDANT_PROCESS_ITEM
                )

        new_items: list[QtGui.QStandardItem] = []
        for child_proc in descendants_by_hash.values():
            item = QtGui.QStandardItem()
            item.setEditable(False)
            item.setColumnCount(self.columnCount())
            new_items.append(item)

            # Make descendant name slightly italic to hint hierarchy
            font = item.font()
            font.setItalic(True)
            item.setFont(font)

            self._fill_item_data(item, child_proc, DESCENDANT_PROCESS_ITEM)

            self._process_by_hash[child_proc.hash] = child_proc
            self._items_by_hash[child_proc.hash] = item

        if new_items:
            parent_item.appendRows(new_items)

        if parent_proc is not None:
            self._set_item_state(
                parent_item, parent_proc, MAIN_PROCESS_ITEM
            )

    def get_process_by_hash(self, process_hash: str) -> ProcessInfo | None:
        return self._process_by_hash.get(process_hash)

    def get_index_by_hash(self, process_hash: str) -> QtCore.QModelIndex:
        """Get model index for process matching given hash.

        Args:
            process_hash (str): Process hash to search for.

        Returns:
            QtCore.QModelIndex: Matching model index or None
                if not found.

        """
        item = self._items_by_hash.get(process_hash)
        if item is not None:
            return item.index()
        return self.index(-1, -1)

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid():
            return super().data(index, role)

        col = index.column()
        if role == QtCore.Qt.DecorationRole:
            if col != self.COLUMNS.NAME:
                return None

        if role == QtCore.Qt.DisplayRole:
            role = self._display_roles_mapping.get(col)
            if role is None:
                return ""

        if role >= QtCore.Qt.UserRole:
            index = index.sibling(index.row(), 0)

        return super().data(index, role)

    def flags(self, index):
        return super().flags(index.sibling(index.row(), 0))

    def _get_status_icon(self, state: int) -> QtGui.QIcon:
        """Return a small colored circle icon representing process status.

        Args:
            state (int): Process state.

        Returns:
            QtGui.QIcon: Colored circle icon.

        """
        if state == ProcessState.RUNNING:
            return self._running_icon
        if state == ProcessState.CHILD_RUNNING:
            return self._child_running_icon
        if state == ProcessState.STOPPED:
            return self._stopped_icon
        return self._unknown_icon

    def _get_process_state(self, process: ProcessInfo, item_type: int) -> int:
        """.

        Args:
            process (ProcessInfo): ProcessInfo object.
            item_type (int): Item type.

        Returns:
            int: Process state.

        """
        if item_type == DESCENDANT_PROCESS_ITEM:
            return ProcessState.RUNNING

        # If top-level process has children, prefer child-running state
        if process.hash:
            parent_item = self._items_by_hash.get(process.hash)
            if parent_item is not None:
                for row in range(parent_item.rowCount()):
                    child = parent_item.child(row, 0)
                    process_hash = child.data(PROCESS_HASH_ROLE)
                    cproc = self._process_by_hash.get(process_hash)
                    if cproc and cproc.pid and not cproc.stopped:
                        return ProcessState.CHILD_RUNNING

        if process.stopped:
            return ProcessState.STOPPED
        return ProcessState.RUNNING

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
        if not hasattr(cls, "_child_running_icon"):
            # yellow = some child running
            cls._child_running_icon = cls._create_icon(
                QtGui.QColor(200, 180, 0), size)

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

    def _fill_item_data(
        self,
        item: QtGui.QStandardItem,
        process: ProcessInfo,
        item_type: int,
    ) -> None:
        executable = process.executable.as_posix()
        pid_value = "N/A"
        created_at = "N/A"
        start_time = "N/A"
        output_file = "N/A"
        if process.pid:
            pid_value = str(process.pid)

        if process.created_at:
            try:
                # Parse the UTC timestamp from SQLite and convert
                # to local timezone
                # SQLite CURRENT_TIMESTAMP format is "YYYY-MM-DD HH:MM:SS"
                created_arrow = arrow.get(process.created_at).to("local")
                created_at = created_arrow.strftime("%Y-%m-%d %H:%M:%S")

            except (ValueError, AttributeError):
                # If parsing fails, return the original string
                created_at = process.created_at

        if process.start_time:
            st_obj = arrow.get(process.start_time).to("local")
            start_time = st_obj.strftime("%Y-%m-%d %H:%M:%S")

        if process.output:
            output_file = str(process.output)

        item.setData(process.name, PROCESS_NAME_ROLE)
        item.setData(process.hash, PROCESS_HASH_ROLE)
        item.setData(executable, PROCESS_EXECUTABLE_ROLE)
        item.setData(pid_value, PROCESS_PID_ROLE)
        item.setData(created_at, PROCESS_CREATED_ROLE)
        item.setData(start_time, PROCESS_START_TIME_ROLE)
        item.setData(output_file, PROCESS_OUTPUT_FILE_ROLE)
        item.setData(item_type, ITEM_TYPE_ROLE)
        self._set_item_state(item, process, item_type)

    def _set_item_state(
        self,
        item: QtGui.QStandardItem,
        process: ProcessInfo,
        item_type: int,
        *,
        state: int | None = None,
    ) -> None:
        old_state = item.data(PROCESS_STATE_ROLE)

        if state is None:
            state = self._get_process_state(process, item_type)

        if (
            item_type == MAIN_PROCESS_ITEM
            and state == ProcessState.RUNNING
            and (old_state is None or old_state == ProcessState.UNKNOWN)
        ):
            state = ProcessState.UNKNOWN

        if old_state == state:
            return

        status = "UNKNOWN"
        if state == ProcessState.RUNNING:
            status = "Running"
        elif state == ProcessState.STOPPED:
            status = "Stopped"

        icon = self._get_status_icon(state)

        self.blockSignals(True)
        item.setData(state, PROCESS_STATE_ROLE)
        item.setData(status, PROCESS_STATUS_ROLE)
        item.setData(icon, QtCore.Qt.DecorationRole)
        self.blockSignals(False)

        if item.row() >= 0:
            self._state.rows_changed = True

    def _on_refresh_timer(self) -> None:
        self._refresh_processes()


class ProcessMonitorController(QtCore.QObject):
    """Controller that encapsulates data logic for ProcessMonitorWindow.

    Handles ApplicationManager, QThreadPool, and QTimers.

    """
    file_content = QtCore.Signal(str)
    cleanup_finished = QtCore.Signal(int)
    error = QtCore.Signal(str)

    def __init__(self, parent: Optional[QtCore.QObject] = None):
        """Initialize the controller."""
        super().__init__(parent)
        self.manager = ProcessManager()

        self._file_watcher = FileChangeWatcher(self)
        self._file_watcher.changed.connect(self._on_file_changed)

        self._thread_pool = QThreadPool()

        self._file_reload_timer = QtCore.QTimer(self)
        self._file_reload_timer.timeout.connect(self._on_file_reload_timeout)
        self._file_reload_timer.setSingleShot(False)
        self._file_reload_interval = 2000
        self._file_reload_target: Optional[Path] = None

    # Timer control
    def stop_timers(self) -> None:
        """Stop all active timers."""
        if self._file_reload_timer.isActive():
            self._file_reload_timer.stop()

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
        file_path = Path(str(file_obj))
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

    def delete_processes(self, process_hashes: set[str]) -> None:
        """Delete processes by hash in background thread.

        Args:
            process_hashes (set[str]): Hash of the processes to delete.

        """
        try:
            worker = CleanupWorker(
                self.manager, "selection", process_hashes
            )
            worker.signals.finished.connect(self._on_cleanup_finished)
            worker.signals.error.connect(self._on_error)
            self._thread_pool.start(worker)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))

    def _on_cleanup_finished(self, deleted_proc: int) -> None:
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

        self._controller.file_content.connect(self._on_file_content)
        self._controller.cleanup_finished.connect(self._on_cleanup_finished)
        self._controller.error.connect(self._on_error)

        self._current_process = None

        self._setup_ui()

    def keyReleaseEvent(self, event) -> None:
        if (
            event.modifiers() == QtCore.Qt.NoModifier
            and event.key() == QtCore.Qt.Key_Delete
        ):
            self._delete_selected_process()
            event.accept()
            return
        super().keyReleaseEvent(event)

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
            "Auto-reload output for running processes")
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
        self._tree_proxy = QtCore.QSortFilterProxyModel()
        self._tree_proxy.setSourceModel(self._tree_model)
        self._tree_view = QtWidgets.QTreeView()
        self._tree_view.setModel(self._tree_proxy)
        self._tree_view.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._tree_view.setSortingEnabled(True)
        self._tree_view.sortByColumn(
            ProcessTreeModel.COLUMNS.CREATED, QtCore.Qt.DescendingOrder
        )
        self._tree_view.doubleClicked.connect(self._on_row_double_clicked)
        self._tree_model.processes_refreshed.connect(
            self._on_processes_refreshed
        )
        self._tree_model.error.connect(self._on_error)

        header = self._tree_view.header()
        header.setStretchLastSection(True)
        for col in range(len(self._tree_model.HEADERS)):
            header.setSectionResizeMode(
                col, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)

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

        self._refresh_btn = QtWidgets.QPushButton("Refresh Process List")
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

        toolbar_layout.addWidget(self._refresh_btn, 0)
        toolbar_layout.addWidget(self._clean_inactive_btn, 0)
        toolbar_layout.addWidget(self._clean_selected_btn, 0)
        toolbar_layout.addStretch(1)
        return toolbar_layout

    def _refresh_data(self) -> None:
        """Refresh the process table data in background thread."""
        self._tree_model.refresh()

    def _on_processes_refreshed(self, process_count: int) -> None:
        self._status_bar.showMessage(f"Loaded {process_count} processes")
        self._log.debug("Process tree updated with new data")

    def _on_error(self, error_msg: str) -> None:
        """Handle refresh error.

        Args:
            error_msg (str): Error message to display.

        """
        self._status_bar.showMessage(f"Error: {error_msg}")

    def _on_row_double_clicked(self, index: QtCore.QModelIndex) -> None:
        """Handle double-click on a process row to load its output file.

        Args:
            index (QtCore.QModelIndex): Index of the clicked row.

        """
        if not index.isValid():
            return

        process_hash = index.data(PROCESS_HASH_ROLE)
        process = self._tree_model.get_process_by_hash(process_hash)
        if not process:
            return
        self._current_process = process
        self._load_output_content()
        if (
            self._auto_reload_checkbox.isChecked()
            and process.pid
            and not process.stopped
        ):
            self._controller.stop_file_reload()
            self._controller.start_file_watch(process.output)
        else:
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
        def restore_scroll() -> None:
            """Restore the scroll position to the bottom.

            If the user was at the bottom before reload, keep them at
            the bottom. Otherwise, maintain their relative position.

            This is done in a single-shot timer to ensure it runs
            after the layout has been updated.

            """
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

        elif (
            self._current_process
            and self._current_process.pid
            and not self._current_process.stopped
        ):
            self._controller.stop_file_reload()
            self._controller.start_file_watch(self._current_process.output)
            # self._controller.start_file_reload(
            #     self._current_process.output, DEFAULT_RELOAD_INTERVAL)

    def _clean_inactive_processes(self) -> None:
        """Clean all inactive processes from a database."""
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

        self._status_bar.showMessage("Cleaning inactive processes...")

        self._controller.clean_inactive()

    def _delete_selected_process(self) -> None:
        """Delete the selected process from database and its output file."""
        selection = self._tree_view.selectionModel()
        if not selection.hasSelection():
            QtWidgets.QMessageBox.information(
                self,
                "No Selection",
                "Please select a process to delete."
            )
            return

        indexes = selection.selectedRows()
        hashes = set()
        for index in indexes:
            if index.data(ITEM_TYPE_ROLE) == MAIN_PROCESS_ITEM:
                hashes.add(index.data(PROCESS_HASH_ROLE))

        if not hashes:
            QtWidgets.QMessageBox.information(
                self,
                "No Valid Selection",
                "Cannot delete a descendant process from DB.",
            )
            return

        suffix = "" if len(hashes) == 1 else "es"
        question = (
            f"Delete {len(hashes)} process{suffix} and its output file?"
        )

        reply = QtWidgets.QMessageBox.question(
            self,
            "Confirm Deletion",
            question,
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )

        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        self._status_bar.showMessage("Deleting process...")

        self._controller.delete_processes(hashes)

    def _on_cleanup_finished(
            self,
            deleted_proc: int) -> None:
        """Handle completion of cleanup operation."""
        self._refresh_data()
        self._status_bar.showMessage(
            f"Deleted {deleted_proc} inactive processes.")

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802
        """Apply stylesheet when the window is shown."""
        self.setStyleSheet(load_stylesheet())
        super().showEvent(event)
        self._tree_model.start_workers()
        self._refresh_data()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        """Clean up timers and threads when closing."""
        # Delegate shutdown to controller (stops timers and waits for workers)
        self._tree_model.stop_workers()
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
