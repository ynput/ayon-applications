"""Handling of processes in Ayon Applications."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import platform
import sqlite3
import threading
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from ayon_core.lib import get_launcher_local_dir

if TYPE_CHECKING:
    import subprocess


@dataclass
class ProcessInfo:
    """Information about a process launched by the addon.

    Attributes:
        name (str): Name of the process.
        executable (Path): Path to the executable.
        args (list[str]): Arguments for the process.
        env (dict[str, str]): Environment variables for the process.
        hash (str): Hash of the process information.
        cwd (str): Current working directory for the process.
        pid (int): Process ID of the launched process.
        output (Path): Output of the process.
        start_time (float): Start time of the process in
            seconds since the epoch.
        created_at (str): Timestamp of when the process info was created in

    """
    name: str
    executable: Path
    args: list[str]
    env: dict[str, str]
    cwd: str
    hash: str = ""
    pid: int | None = None
    output: Path | None = None
    start_time: float | None = None
    created_at: str | None = None
    stopped: bool = False

    def __post_init__(self) -> None:
        """Post-initialization to compute the hash if not provided."""
        if self.hash == "":
            self.hash = ProcessManager.get_process_info_hash(self)

    @classmethod
    def from_row_values(
        cls,
        # NOTE Not sure why we don't use this?
        _process_hash: str,
        name: str,
        executable: str,
        args: str,
        env: str,
        cwd: str,
        pid: int | None,
        output: str | None,
        start_time: float | None,
        created_at: str | None,
        stopped: int,
    ) -> ProcessInfo:
        """Create a ProcessInfo instance from database row values."""
        if pid is None:
            stopped = True
        return cls(
            name=name,
            executable=Path(executable),
            args=json.loads(args) if args else [],
            env=json.loads(env) if env else {},
            cwd=cwd,
            pid=pid,
            output=Path(output) if output else None,
            start_time=start_time,
            created_at=created_at,
            stopped=bool(stopped),
        )


class ProcessManager:
    """Manager for handling processes in AYON Applications."""

    log: logging.Logger
    _select_cols = ", ".join((
        "hash",
        "name",
        "executable",
        "args",
        "env",
        "cwd",
        "pid",
        "output_file",
        "start_time",
        "created_at",
        "stopped",
    ))

    def __init__(self) -> None:
        """Initialize the ProcessManager."""
        self.log = logging.getLogger(f"{__name__}.ProcessManager")
        # Use thread-local storage for SQLite connections to avoid
        # sharing connections between threads (fixes Linux SQLite issues)
        self._thread_local = threading.local()

    @staticmethod
    def get_process_info_storage_location() -> Path:
        """Get the path to process info storage.

        Returns:
            Path: Path to the process handlers storage.

        """
        return Path(get_launcher_local_dir()) / "process_handlers.db"

    def _get_process_storage_connection(self) -> sqlite3.Connection:
        """Get a thread-local SQLite connection.

        Each thread gets its own connection to avoid thread-safety issues
        that can occur on Linux.

        Returns:
            sqlite3.Connection: Thread-local connection to the process storage.

        """
        # Check if this thread already has a connection
        if hasattr(self._thread_local, "connection"):
            return self._thread_local.connection

        # Create a new connection for this thread
        cnx = sqlite3.connect(
            self.get_process_info_storage_location(),
            # Enable thread safety for SQLite operations
            check_same_thread=False
        )
        cursor = cnx.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS process_info ("
            "hash TEXT PRIMARY KEY, "
            "name TEXT, "
            "executable TEXT, "
            "args TEXT DEFAULT NULL, "
            "env TEXT DEFAULT NULL, "
            "cwd TEXT DEFAULT NULL, "
            "pid INTEGER DEFAULT NULL, "
            "output_file TEXT DEFAULT NULL, "
            "start_time REAL DEFAULT NULL, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "stopped INTEGER DEFAULT 0"
            ")"
        )
        # Migrate existing databases that lack the stopped column.
        with contextlib.suppress(sqlite3.OperationalError):
            cursor.execute(
                "ALTER TABLE process_info "
                "ADD COLUMN stopped INTEGER DEFAULT 0"
            )
        cnx.commit()
        self._thread_local.connection = cnx

        return self._thread_local.connection

    @staticmethod
    def get_process_info_hash(process_info: ProcessInfo) -> str:
        """Get hash of the process information.

        Returns:
            str: Hash of the process information.
        """
        return ProcessManager.get_process_info_hash_by_values(
            process_info.executable,
            process_info.name,
            process_info.pid,
            process_info.start_time,
        )

    @staticmethod
    def get_process_info_hash_by_values(
        executable: Path,
        name: str,
        pid: int | None = None,
        start_time: float | None = None,
    ) -> str:
        """Get hash of the process information by values.

        Args:
            executable (Path): Path to the executable.
            name (str): Name of the process.
            pid (Optional[int]): Process ID of the launched process.
            start_time (Optional[float]): Start time of the process.

        Returns:
            str: Hash of the process information.

        """
        start = (
            f"{start_time}"
            if start_time is not None
            else ""
        )
        key = f"{name}{pid}{executable}{start}"
        return sha256(key.encode()).hexdigest()

    def store_process_info(self, process_info: ProcessInfo) -> None:
        """Store process information.

        Args:
            process_info (ProcessInfo): Process handler to store.

        """
        # refresh hash in case some values changed
        process_info.hash = ProcessManager.get_process_info_hash(process_info)
        if process_info.pid is None:
            self.log.warning((
                "Cannot store process info for process without PID. "
                "Process name: %s"
            ), process_info.name)
            return

        cnx = self._get_process_storage_connection()
        cursor = cnx.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO process_info "
            "(hash, name, executable, args, env, cwd, "
            "pid, output_file, start_time, stopped) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                process_info.hash,
                process_info.name,
                process_info.executable.as_posix(),
                json.dumps(process_info.args),
                json.dumps(process_info.env),
                process_info.cwd,
                process_info.pid,
                (
                    process_info.output.as_posix()
                    if process_info.output else None
                ),
                process_info.start_time,
                int(process_info.stopped),
            )
        )
        cnx.commit()

    def get_process_info(self, process_hash: str) -> ProcessInfo | None:
        """Get process information by hash.

        Args:
            process_hash (str): Hash of the process.

        Returns:
            Optional[ProcessInfo]: Process information or None if not found.
        """
        cnx = self._get_process_storage_connection()
        cursor = cnx.cursor()
        cursor.execute(
            f"SELECT {self._select_cols} FROM process_info WHERE hash = ?",
            (process_hash,)
        )
        row = cursor.fetchone()
        if row is None:
            return None

        return ProcessInfo.from_row_values(*row)

    def get_process_info_by_name(
        self, name: str) -> ProcessInfo | None:
        """Get process information by name.

        Args:
            name (str): Name of the process.

        Returns:
            Optional[ProcessInfo]: Process information or None if not found.
        """
        cnx = self._get_process_storage_connection()
        cursor = cnx.cursor()
        query = f"SELECT {self._select_cols} FROM process_info WHERE name = ?"
        params = [name]

        cursor.execute(query, params)
        row = cursor.fetchone()
        if row is None:
            return None

        return ProcessInfo.from_row_values(*row)

    def get_process_info_by_pid(self, pid: int) -> ProcessInfo | None:
        """Get process information by process id.

        Args:
            pid (int): ID of the process.

        Returns:
            Optional[ProcessInfo]: Process information or None if not found.
        """
        cnx = self._get_process_storage_connection()
        cursor = cnx.cursor()
        query = f"SELECT {self._select_cols} FROM process_info WHERE pid = ?"
        params = [pid]

        cursor.execute(query, params)
        row = cursor.fetchone()
        if row is None:
            return None

        return ProcessInfo.from_row_values(*row)

    def get_current_process_info(self) -> ProcessInfo | None:
        """Get information for the current process.

        Returns:
            Optional[ProcessInfo]: Process information or None if not found.
        """
        return self.get_process_info_by_pid(os.getpid())

    def get_all_process_info(
        self, *, invalidate: bool = True
    ) -> list[ProcessInfo]:
        """Get all process information from the database.

        Args:
            invalidate (bool): Invalidate stopped state.

        Returns:
            list[ProcessInfo]: List of all process information.

        """
        cnx = self._get_process_storage_connection()
        cursor = cnx.cursor()
        sql = (
            f"SELECT {self._select_cols} FROM process_info"
            " ORDER BY created_at DESC"
        )
        cursor.execute(sql)
        rows = cursor.fetchall()

        processes: list[ProcessInfo] = [
            ProcessInfo.from_row_values(*row)
            for row in rows
        ]
        deactivated: set[str] = set()
        for proc in processes:
            if proc.pid is None and not proc.stopped:
                proc.stopped = True
                deactivated.add(proc.hash)

            elif invalidate and not proc.stopped:
                exe = proc.executable.as_posix()
                is_running = self._is_process_running(
                    proc.pid, exe, proc.start_time
                )
                if not is_running:
                    proc.stopped = True
                    deactivated.add(proc.hash)

        self._mark_processes_stopped(set(deactivated))

        return processes

    def _mark_processes_stopped(self, process_hashes: set[str]) -> None:
        """Mark processes as stopped in the database.

        Args:
            process_hashes (set[str]): Hashes of processes to mark as stopped.

        """
        if not process_hashes:
            return

        placeholders = ",".join("?" * len(process_hashes))
        cnx = self._get_process_storage_connection()
        cursor = cnx.cursor()
        cursor.execute(
            f"UPDATE process_info SET stopped=?"
            f" WHERE hash IN ({placeholders})",
            (1, *process_hashes)
        )
        cnx.commit()

    def delete_processes_info(self, process_hashes: set[str]) -> bool:
        """Delete process information by hash.

        This also deletes the output file if it exists.

        Args:
            process_hashes (set[str]): Hashes of processes to delete.

        Returns:
            bool: True if deleted, False if not found.

        """
        if not process_hashes:
            return False

        # Convert to tuple for delete operation
        process_hashes = tuple(process_hashes)
        placeholders = ",".join("?" * len(process_hashes))

        cnx = self._get_process_storage_connection()
        cursor = cnx.cursor()
        cursor.execute(
            (
                "SELECT hash, output_file FROM process_info"
                f" WHERE hash IN ({placeholders})"
            ),
            process_hashes
        )
        filtered_hashes = []
        for row in cursor.fetchall():
            process_hash, output = row
            filtered_hashes.append(process_hash)
            if output and Path(output).exists():
                # File might not exist anymore, so we use contextlib.suppress
                with contextlib.suppress(OSError):
                    os.remove(output)

        if not filtered_hashes:
            return False

        placeholders = ",".join("?" * len(filtered_hashes))
        cursor.execute(
            f"DELETE FROM process_info WHERE hash IN ({placeholders})",
            filtered_hashes
        )
        cnx.commit()
        return cursor.rowcount > 0

    def delete_process_info(self, process_hash: str) -> bool:
        """Delete process information by hash.

        This also deletes the output file if it exists.

        Args:
            process_hash (str): Hash of the process to delete.

        Returns:
            bool: True if deleted, False if not found.

        """
        return self.delete_processes_info({process_hash})

    def delete_inactive_processes(self) -> int:
        """Delete all inactive process information.

        This also deletes the output files of the inactive processes.

        Returns:
            int: Number of deleted processes.
        """
        cnx = self._get_process_storage_connection()

        # Get all processes and check which ones are inactive
        all_processes = self.get_all_process_info()

        files_to_delete = [
            process.output
            for process in all_processes
            if (
                process.stopped
                and (process.output and Path(process.output).exists())
            )
        ]

        inactive_hashes = [
            process.hash
            for process in all_processes
            if process.stopped
        ]
        if not inactive_hashes:
            return 0

        cursor = cnx.cursor()
        placeholders = ",".join("?" * len(inactive_hashes))
        cursor.execute(
            ("DELETE FROM process_info WHERE "  # noqa: S608
            f"hash IN ({placeholders})"),
            inactive_hashes
        )
        cnx.commit()

        for file_path in files_to_delete:
            # File might not exist anymore, so we use contextlib.suppress
            with contextlib.suppress(OSError):
                os.remove(file_path)

        return cursor.rowcount

    def invalidate_process(self, process: ProcessInfo) -> bool:
        """Check if a process is running using psutil.

        Args:
            process (ProcessInfo): Process information to check.

        """
        if process.stopped or process.pid is None:
            return False

        is_running = self._is_process_running(
            process.pid,
            str(process.executable),
            process.start_time
        )
        if not is_running:
            process.stopped = True
            self._mark_processes_stopped({process.hash})
        return is_running

    @staticmethod
    def _is_process_running(
            pid: int,
            executable: str,
            start_time: float | None = None) -> bool:
        """Check if a process is running using psutil.

        Args:
            pid (int): Process ID to check.
            executable (str): Executable name to verify.
            start_time (Optional[float]): Start time to verify.

        Returns:
            bool: True if the process is running, False otherwise.

        """
        import psutil

        try:
            proc = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return False

        # If start_time provided, verify it matches process creation time
        if start_time is not None:
            try:
                proc_ct = proc.create_time()
                # allow small tolerance for float differences
                if abs(proc_ct - float(start_time)) > 1.0:
                    return False
            except Exception:  # noqa: BLE001
                # cannot verify start time -> conservative False
                return False

        if not executable:
            # No executable provided, process exists
            # (and start_time matched if provided)
            return True

        # Try to get executable path/name and command line first
        candidates = set()
        with contextlib.suppress(Exception):
            exe_path = proc.exe() if hasattr(proc, "exe") else None
            if exe_path:
                candidates.add(Path(exe_path).as_posix())

            name = proc.name()
            if name:
                candidates.add(name)

            cmd = proc.cmdline()
            if cmd:
                first = cmd[0]
                candidates.add(first)
        if platform.system().lower() == "windows":
            # On Windows be more relaxed and check image name only
            candidates = {c.lower() for c in candidates if c}
            return Path(executable).name.lower() in candidates

        return Path(executable).as_posix() in candidates

    @staticmethod
    def get_executable_path_by_pid(pid: int) -> Path | None:
        """Get the executable path of a process by its PID using psutil.

        Args:
            pid (int): Process ID.

        Returns:
            Optional[Path]: The executable path of the process, or None if it
                cannot be determined.

        """
        import psutil

        exe_path = None
        if pid:
            try:
                exe_path_str = psutil.Process(pid).exe()
                if exe_path_str:
                    exe_path = Path(exe_path_str)
            except (
                    psutil.NoSuchProcess,
                    psutil.ZombieProcess,
                    psutil.AccessDenied):
                exe_path = None
        return exe_path

    @staticmethod
    def get_process_start_time(
            process: subprocess.Popen) -> float | None:
        """Get the start time of a process using psutil.

        Returns:
            Optional[float]: The start time of the process in seconds since
                the epoch, or None if it cannot be determined.

        """
        import psutil

        start_time = None
        if process.pid:
            try:
                start_time = psutil.Process(process.pid).create_time()
            except (
                    psutil.NoSuchProcess,
                    psutil.ZombieProcess,
                    psutil.AccessDenied):
                start_time = None
        return start_time

    @staticmethod
    def get_process_start_time_by_pid(pid: int) -> float | None:
        """Get the start time of a process by PID using psutil.

        Args:
            pid (int): Process ID.

        Returns:
            Optional[float]: The start time of the process in seconds since
                the epoch, or None if it cannot be determined.

        """
        import psutil

        start_time = None
        if pid:
            try:
                start_time = psutil.Process(pid).create_time()
            except (
                    psutil.NoSuchProcess,
                    psutil.ZombieProcess,
                    psutil.AccessDenied):
                start_time = None
        return start_time

    @staticmethod
    def get_descendant_processes_by_pid(pid: int) -> list[ProcessInfo]:
        """Get descendant processes of a given process id.

        Args:
            pid (int): Process ID of the parent process.

        Returns:
            list[ProcessInfo]: List of descendant process information.

        """
        import psutil

        descendants: list[ProcessInfo] = []
        with contextlib.suppress(
                psutil.NoSuchProcess,
                psutil.ZombieProcess,
                psutil.AccessDenied):
            parent_proc = psutil.Process(pid)
            child_procs = parent_proc.children(recursive=True)
            for child in child_procs:
                #  environment isn't used on child processes for now
                proc_info = ProcessInfo(
                    name=child.name(),
                    executable=Path(child.exe()),
                    args=child.cmdline(),
                    env={},  # skipped for performance reasons
                    cwd=child.cwd(),
                    pid=child.pid,
                    start_time=child.create_time(),
                    stopped=False,
                )
                # If psutil returned the process, it's currently running
                proc_info.stopped = True
                descendants.append(proc_info)
        return descendants

    def get_descendant_processes(
            self, process_info: ProcessInfo) -> list[ProcessInfo]:
        """Get descendant processes of a given process information.

        Args:
            process_info (ProcessInfo): Parent process information.

        Returns:
            list[ProcessInfo]: List of descendant process information.

        """
        if process_info.pid is None or process_info.stopped:
            return []
        return self.get_descendant_processes_by_pid(process_info.pid)
