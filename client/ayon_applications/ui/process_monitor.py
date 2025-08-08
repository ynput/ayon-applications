from __future__ import annotations
import contextlib
from logging import getLogger
import os
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Optional, Union

from qtpy import QtWidgets, QtCore, QtGui
from qtpy.QtCore import QRunnable, Slot, QThreadPool
from qtpy.QtCore import QModelIndex, QPersistentModelIndex

from ayon_core.style import load_stylesheet
from ayon_core.tools.utils import get_ayon_qt_app

from ..manager import ApplicationManager

if TYPE_CHECKING:
    from ..manager import ProcessInfo


ModelIndex = Union[QModelIndex, QPersistentModelIndex]


class CatchTime:
    """Context manager to measure execution time."""
    def __enter__(self):
        self.start = perf_counter()
        return self

    def __exit__(self, type, value, traceback):
        self.time = perf_counter() - self.start
        self.readout = f'Time: {self.time:.3f} seconds'


class ProcessRefreshWorkerSignals(QtCore.QObject):
    """Signals for ProcessRefreshWorker."""
    finished = QtCore.Signal(list)  # Emits list of ProcessInfo
    error = QtCore.Signal(str)

class ProcessRefreshWorker(QRunnable):
    """Worker thread for refreshing process data from the database."""

    def __init__(self, manager: ApplicationManager):
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
            except Exception as e:
                self.signals.error.emit(str(e))
        self._log.debug(
            "Refresh from db completed in %s", timer.readout)

class FileContentWorkerSignals(QtCore.QObject):
    """Signals for FileContentWorker."""
    finished = QtCore.Signal(str)  # Emits file content
    error = QtCore.Signal(str)


class FileContentWorker(QRunnable):
    """Worker thread for loading file content."""

    def __init__(self, file_path):
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

            content = Path(self._file_path).read_text(encoding='utf-8', errors='replace')
            self.signals.finished.emit(content)
        except Exception as e:
            self.signals.error.emit(f"Error reading file: {e}")


class CleanupWorkerSignals(QtCore.QObject):
    """Signals for CleanupWorker."""
    # Emits (deleted_processes, deleted_files)
    finished = QtCore.Signal(int, int)
    error = QtCore.Signal(str)

class CleanupWorker(QRunnable):
    """Worker thread for cleanup operations."""

    def __init__(self,
                 manager: ApplicationManager,
                 cleanup_type: str,
                 process_hash: Optional[str] = None) -> None:
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
                self._cleanup_single()
        except Exception as e:
            self.signals.error.emit(str(e))

    def _cleanup_inactive(self) -> None:
        """Clean up inactive processes."""
        # Get all processes to check which files to delete
        all_processes = self._manager.get_all_process_info()
        files_to_delete: list[Path] = []

        files_to_delete.extend(
            process.output
            for process in all_processes
            if (
                not process.active
                and (process.output and Path(process.output).exists())
            )
        )
        # Delete from database
        deleted_count = self._manager.delete_inactive_processes()

        # Delete output files
        files_deleted = 0
        for file_path in files_to_delete:
            # File might not exist anymore, so we use contextlib.suppress
            with contextlib.suppress(OSError):
                os.remove(file_path)
                files_deleted += 1
        self.signals.finished.emit(deleted_count, files_deleted)

    def _cleanup_single(self) -> None:
        """Clean up a single process."""
        if not self._process_hash:
            self.signals.error.emit("No process hash provided")
            return

        # Find the process first
        all_processes = self._manager.get_all_process_info()
        target_process = next(
            (
                process
                for process in all_processes
                if self._manager.get_process_info_hash(process)
                == self._process_hash
            ),
            None,
        )
        if not target_process:
            self.signals.error.emit("Process not found")
            return

        # Delete the output file if it exists
        file_deleted = 0
        if target_process.output and Path(target_process.output).exists():
            # File might not exist anymore, so we use contextlib.suppress
            with contextlib.suppress(OSError):
                os.remove(target_process.output)
                file_deleted = 1
        # Delete from database
        deleted = self._manager.delete_process_info(self._process_hash)
        process_deleted = 1 if deleted else 0

        self.signals.finished.emit(process_deleted, file_deleted)


class ProcessTableModel(QtCore.QAbstractTableModel):
    """Table model for displaying process information."""

    def __init__(
            self,
            parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self._processes: list[ProcessInfo] = []
        self._headers = [
            "Name", "PID", "Status", "Created", "Site ID", "Output File"
        ]
        self._manager = ApplicationManager()

    def update_processes(self, processes: list[ProcessInfo]) -> None:
        """Update process data from background thread."""
        self.beginResetModel()
        self._processes = processes
        self.endResetModel()

    def rowCount(
            self,
            parent: ModelIndex=QtCore.QModelIndex()) -> int:
        """Return the number of rows in the model.

        Args:

        Returns:
            int: Number of processes in the model.

        """
        return len(self._processes)

    def columnCount(
            self,
            parent: ModelIndex = QtCore.QModelIndex()) -> int:
        return len(self._headers)

    def headerData(
            self,
            section: int,
            orientation: QtCore.Qt.Orientation,
            role: int = QtCore.Qt.ItemDataRole.DisplayRole) -> Optional[str]:
        if (
            orientation == QtCore.Qt.Orientation.Horizontal
            and role == QtCore.Qt.ItemDataRole.DisplayRole
        ):
            return self._headers[section]
        return None

    @staticmethod
    def _data_display_role(
            column: int, process: ProcessInfo) -> Optional[str]:
        """Return data for the display role based on column index.

        Returns:
            str: Data for the specified column.

        """
        if column == 0:
            return process.name
        elif column == 1:
            return str(process.pid) if process.pid else "N/A"
        elif column == 2:
            if process.pid:
                return "Running" if process.active else "Stopped"
            return "Unknown"
        elif column == 3:
            return process.created_at or "N/A"
        elif column == 4:
            return process.site_id or "N/A"
        elif column == 5:
            return str(process.output) if process.output else "N/A"
        return None

    @staticmethod
    def _data_background_role(
        process: ProcessInfo
    ) -> Optional[QtGui.QColor]:
        """Return background color for the process status."""
        if process.pid:
            is_running = process.active
            if is_running:
                return QtGui.QColor(200, 255, 200)  # Light green
            else:
                return QtGui.QColor(255, 200, 200)  # Light red
        return QtGui.QColor(240, 240, 240)  # Light gray


    def data(
            self, index: ModelIndex,
            role: int = QtCore.Qt.ItemDataRole.DisplayRole) -> Optional[Union[str, QtGui.QColor, ProcessInfo]]:  # noqa: E501
        """Return data for the specified index and role."""
        if not index.isValid() or index.row() >= len(self._processes):
            return None

        process = self._processes[index.row()]
        column = index.column()

        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            return self._data_display_role(column, process)

        elif role == QtCore.Qt.ItemDataRole.BackgroundRole:
            return self._data_background_role(process)

        elif role == QtCore.Qt.ItemDataRole.UserRole:
            # Return the process info for the row
            return process

        return None

    def get_process_at_row(self, row: int) -> Optional[ProcessInfo]:
        """Get process info at specific row."""
        return (
            self._processes[row]
            if 0 <= row < len(self._processes) else None
        )


class ProcessMonitorWindow(QtWidgets.QDialog):
    """Main window for monitoring application processes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._log = getLogger(self.__class__.__name__)
        self.setWindowTitle("AYON Process Monitor")
        self.setMinimumSize(1000, 600)
        self._thread_pool = QThreadPool()
        self._log.debug(
            "Using thread pool with %s threads.",
            self._thread_pool.maxThreadCount())

        self._manager = ApplicationManager()
        self._current_process = None
        self._is_loading = False

        self._setup_ui()

    def _setup_ui(self):
        """Set up the user interface."""
        # central_widget = QtWidgets.QWidget()
        central_widget = self
        # self.setCentralWidget(central_widget)

        # Main layout
        main_layout = QtWidgets.QVBoxLayout(central_widget)

        # Toolbar
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

        toolbar_layout.addWidget(self._refresh_btn)
        toolbar_layout.addWidget(self._clean_inactive_btn)
        toolbar_layout.addWidget(self._clean_selected_btn)
        toolbar_layout.addStretch()
        toolbar_layout.addWidget(self._loading_label)

        main_layout.addLayout(toolbar_layout)

        # Splitter for table and output
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)

        # Process table
        self._table_model = ProcessTableModel()
        self._table_view = QtWidgets.QTableView()
        self._table_view.setModel(self._table_model)
        self._table_view.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._table_view.setAlternatingRowColors(True)
        self._table_view.setSortingEnabled(True)
        self._table_view.doubleClicked.connect(self._on_row_double_clicked)

        # Set column widths
        header = self._table_view.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(
            3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(
            4, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)

        splitter.addWidget(self._table_view)

        # Output area
        output_widget = QtWidgets.QWidget()
        output_layout = QtWidgets.QVBoxLayout(output_widget)

        output_label = QtWidgets.QLabel("Output Content:")
        output_label.setStyleSheet("font-weight: bold; margin-top: 10px;")

        self._output_text = QtWidgets.QPlainTextEdit()
        self._output_text.setReadOnly(True)
        self._output_text.setPlaceholderText(
            "Double-click a process row to view its output file content...")

        # Auto-reload checkbox
        self._auto_reload_checkbox = QtWidgets.QCheckBox(
            "Auto-reload output for running processes (every 2s)")
        self._auto_reload_checkbox.setChecked(True)
        self._auto_reload_checkbox.toggled.connect(
            self._on_auto_reload_toggled)

        output_layout.addWidget(output_label)
        output_layout.addWidget(self._output_text)
        output_layout.addWidget(self._auto_reload_checkbox)

        splitter.addWidget(output_widget)
        splitter.setSizes([400, 300])

        main_layout.addWidget(splitter)

        # Status bar
        # self.statusBar().showMessage("Ready")

    def _setup_timers(self):
        """Setup periodic refresh timers."""
        # Timer for refreshing table data
        self._refresh_timer = QtCore.QTimer()
        self._refresh_timer.timeout.connect(self._refresh_data)
        self._refresh_timer.start(5000)  # Refresh every 5 seconds

        # Timer for reloading file content
        self._file_reload_timer = QtCore.QTimer()
        self._file_reload_timer.timeout.connect(self._reload_output_content)
        self._file_reload_timer.setSingleShot(False)

    def _set_loading_state(self, loading):
        """Set the loading state of the UI."""
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

    def _refresh_data(self):
        """Refresh the process table data in background thread."""

        # Create worker and thread
        worker = ProcessRefreshWorker(self._manager)
        worker.signals.finished.connect(self._on_refresh_finished)
        worker.signals.error.connect(self._on_refresh_error)
        self._thread_pool.start(worker)


    def _on_refresh_finished(
            self, processes: list[ProcessInfo]) -> None:
        """Handle refresh completion."""
        self._table_model.update_processes(processes)

        # self.statusBar().showMessage(f"Loaded {count} processes")
        self._set_loading_state(False)
        self._log.debug("Process table updated with new data")

    def _on_refresh_error(self, error_msg):
        """Handle refresh error."""
        # self.statusBar().showMessage(f"Error refreshing data: {error_msg}")
        self._set_loading_state(False)

    def _on_row_double_clicked(self, index):
        """Handle double-click on table row."""
        if not index.isValid() or self._is_loading:
            return

        process = self._table_model.get_process_at_row(index.row())
        if not process:
            return

        self._current_process = process
        self._load_output_content()

        # Start auto-reload timer if the process is running and
        # auto-reload is enabled
        if (self._auto_reload_checkbox.isChecked() and
                process.pid and process.active == "Active"):
            self._file_reload_timer.start(2000)  # Reload every 2 seconds
        else:
            self._file_reload_timer.stop()

    def _load_output_content(self):
        """Load output file content in background thread."""
        if not self._current_process or not self._current_process.output:
            self._output_text.setPlainText("No output file available")
            return

        self._output_text.setPlainText("Loading file content...")

        # Create worker and thread
        worker = FileContentWorker(self._current_process.output)
        worker.signals.finished.connect(self._on_file_content_loaded)
        worker.signals.error.connect(self._on_file_content_error)
        self._thread_pool.start(worker)

    def _on_file_content_loaded(self, content):
        """Handle file content loaded."""
        self._output_text.setPlainText(content)

        # Scroll to bottom
        cursor = self._output_text.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        self._output_text.setTextCursor(cursor)

    def _on_file_content_error(self, error_msg):
        """Handle file content loading error."""
        self._output_text.setPlainText(error_msg)

    def _reload_output_content(self):
        """Reload output content if the current process is still running."""
        if (self._current_process and
            self._current_process.pid and
            self._current_process.active):
            self._load_output_content()
        else:
            # Stop timer if process is no longer running
            self._file_reload_timer.stop()

    def _on_auto_reload_toggled(self, checked):
        """Handle auto-reload checkbox toggle."""
        if not checked:
            self._file_reload_timer.stop()
        elif (self._current_process and
              self._current_process.pid and
              self._current_process.active):
            self._file_reload_timer.start(2000)

    def _clean_inactive_processes(self):
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

        self._set_loading_state(True)
        # self.statusBar().showMessage("Cleaning inactive processes...")

        # Create worker and thread
        worker = CleanupWorker(
            self._manager, "inactive")

        worker.signals.finished.connect(self._on_cleanup_finished)
        worker.signals.error.connect(self._on_cleanup_error)
        self._thread_pool.start(worker)


    def _delete_selected_process(self):
        """Delete a selected process from database and its output file."""
        if self._is_loading:
            return

        selection = self._table_view.selectionModel()
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

        process = self._table_model.get_process_at_row(indexes[0].row())
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

        self._set_loading_state(True)
        # self.statusBar().showMessage("Deleting process...")

        # Get process hash
        process_hash = self._manager.get_process_info_hash(process)

        # Create worker and thread
        worker = CleanupWorker(
            self._manager, "single", process_hash)

        worker.signals.finished.connect(
            lambda deleted_proc, deleted_files: (
                self._on_single_cleanup_finished(
                    process, deleted_proc, deleted_files)
            ))
        worker.signals.error.connect(self._on_cleanup_error)

        self._thread_pool.start(worker)

    def _on_cleanup_finished(self, deleted_processes, deleted_files):
        """Handle cleanup completion."""
        self._refresh_data()  # Refresh the table
        # self.statusBar().showMessage(
        #     f"Cleaned {deleted_processes} inactive processes "
        #     f"and {deleted_files} output files"
        # )

    def _on_single_cleanup_finished(
            self, process, deleted_processes, deleted_files):
        """Handle single process cleanup completion."""
        if deleted_processes > 0:
            # Clear output if this was the selected process
            if self._current_process == process:
                self._current_process = None
                self._output_text.clear()
                self._file_reload_timer.stop()

            status_msg = f"Deleted process '{process.name}'"
            if deleted_files > 0:
                status_msg += " and its output file"
            # self.statusBar().showMessage(status_msg)

            self._refresh_data()  # Refresh the table
        else:
            # self.statusBar().showMessage(
            # "Failed to delete process from database")
            self._set_loading_state(False)

    def _on_cleanup_error(self, error_msg):
        """Handle cleanup error."""
        QtWidgets.QMessageBox.warning(
            self, "Error", f"Cleanup failed: {error_msg}"
        )
        self._set_loading_state(False)

    def showEvent(self, event):
        """Apply stylesheet when the window is shown."""
        self.setStyleSheet(load_stylesheet())
        super().showEvent(event)
        self._setup_timers()
        self._refresh_data()

    def closeEvent(self, event):
        """Clean up timers and threads when closing."""
        if self._refresh_timer:
            self._refresh_timer.stop()
        if self._file_reload_timer:
            self._file_reload_timer.stop()

        # Wait for threads to finish
        self._thread_pool.waitForDone()

        super().closeEvent(event)


def main():
    """Main function to run the process monitor."""
    app = get_ayon_qt_app()

    window = ProcessMonitorWindow()
    window.show()

    app.exec_()


if __name__ == "__main__":
    main()
