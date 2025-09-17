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
from typing import TYPE_CHECKING, NamedTuple, Optional

import psutil
from ayon_core.lib import get_launcher_local_dir

if TYPE_CHECKING:
    import subprocess


class ProcessIdTriplet(NamedTuple):
    """Triplet of process identification values."""
    pid: int
    executable: str
    start_time: Optional[float]  # the same goes for start time


@dataclass
class ProcessInfo:
    """Information about a process launched by the addon.

    Attributes:
        name (str): Name of the process.
        executable (Path): Path to the executable.
        args (list[str]): Arguments for the process.
        env (dict[str, str]): Environment variables for the process.
        cwd (str): Current working directory for the process.
        pid (int): Process ID of the launched process.
        active (bool): Whether the process is currently active.
        output (Path): Output of the process.

    """

    name: str
    executable: Path
    args: list[str]
    env: dict[str, str]
    cwd: str
    pid: Optional[int] = None
    active: bool = False
    output: Optional[Path] = None
    start_time: Optional[float] = None
    created_at: Optional[str] = None


class ProcessManager:
    """Manager for handling processes in AYON Applications."""

    log: logging.Logger

    def __init__(self) -> None:
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
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
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
        # include executable name (if available) to reduce collisions when
        # PIDs are reused
        exe = process_info.executable
        # include start_time (if available) to make hash much harder to collide
        start = (
            f"{process_info.start_time}"
            if process_info.start_time is not None else ""
        )
        key = f"{process_info.name}{process_info.pid}{exe}{start}"
        return sha256(key.encode()).hexdigest()

    def store_process_info(self, process_info: ProcessInfo) -> None:
        """Store process information.

        Args:
            process_info (ProcessInfo): Process handler to store.

        """
        if process_info.pid is None:
            self.log.warning((
                "Cannot store process info for process without PID. "
                "Process name: %s"
            ), process_info.name)
            return

        cnx = self._get_process_storage_connection()
        cursor = cnx.cursor()
        process_hash = self.get_process_info_hash(process_info)
        cursor.execute(
            "INSERT OR REPLACE INTO process_info "
            "(hash, name, executable, args, env, cwd, "
            "pid, output_file, start_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                process_hash,
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
            )
        )
        cnx.commit()

    def get_process_info(self, process_hash: str) -> Optional[ProcessInfo]:
        """Get process information by hash.

        Args:
            process_hash (str): Hash of the process.

        Returns:
            Optional[ProcessInfo]: Process information or None if not found.
        """
        cnx = self._get_process_storage_connection()
        cursor = cnx.cursor()
        cursor.execute(
            "SELECT * FROM process_info WHERE hash = ?",
            (process_hash,)
        )
        row = cursor.fetchone()
        if row is None:
            return None

        return ProcessInfo(
            name=row[1],
            executable=Path(row[2]),
            args=json.loads(row[3]),
            env=json.loads(row[4]),
            cwd=row[5],
            pid=row[6],
            output=Path(row[7]) if row[7] else None,
            start_time=row[8],
            created_at=row[9],
        )

    def get_process_info_by_name(
        self, name: str) -> Optional[ProcessInfo]:
        """Get process information by name.

        Args:
            name (str): Name of the process.

        Returns:
            Optional[ProcessInfo]: Process information or None if not found.
        """
        cnx = self._get_process_storage_connection()
        cursor = cnx.cursor()
        query = "SELECT * FROM process_info WHERE name = ?"
        params = [name]

        cursor.execute(query, params)
        row = cursor.fetchone()
        if row is None:
            return None

        return ProcessInfo(
            name=row[1],
            executable=Path(row[2]),
            args=json.loads(row[3]),
            env=json.loads(row[4]),
            cwd=row[5],
            pid=row[6],
            output=Path(row[7]) if row[7] else None,
            start_time=row[8],
            created_at=row[9],
        )

    def get_all_process_info(self) -> list[ProcessInfo]:
        """Get all process information from the database.

        Returns:
            list[ProcessInfo]: List of all process information.
        """
        cnx = self._get_process_storage_connection()
        cursor = cnx.cursor()
        cursor.execute("SELECT * FROM process_info ORDER BY created_at DESC")
        rows = cursor.fetchall()

        processes: list[ProcessInfo] = [
            ProcessInfo(
                name=row[1],
                executable=Path(row[2]),
                args=json.loads(row[3]) if row[3] else [],
                env=json.loads(row[4]) if row[4] else {},
                cwd=row[5],
                pid=row[6],
                output=Path(row[7]) if row[6] else None,
                start_time=row[8],
                created_at=row[9],
            )
            for row in rows
        ]
        # Check if processes are still running
        # This is done by checking the pid of the process.
        # It is using `_are_processes_running` method which
        # checks for processes in batch, mostly because of the fallback
        # on systems without `psutil` module. See `_are_processes_running`
        # documentation for more details.
        # Build list of (pid, executable_name, start_time) triplets so the
        # check can verify PID + image and, when possible, process start time
        # (stronger protection against PID reuse).
        pid_triplets: list[ProcessIdTriplet] = []
        processes_with_pid = []
        for proc in processes:
            if proc.pid is None:
                continue
            exe = proc.executable.as_posix()
            pid_triplets.append(
                ProcessIdTriplet(proc.pid, exe, proc.start_time))
            processes_with_pid.append(proc)

        if pid_triplets:
            running_status = self._are_processes_running(pid_triplets)
            for proc, (_, is_running) in zip(
                    processes_with_pid, running_status):
                proc.active = is_running

        return processes

    def delete_process_info(self, process_hash: str) -> bool:
        """Delete process information by hash.

        This also deletes the output file if it exists.

        Args:
            process_hash (str): Hash of the process to delete.

        Returns:
            bool: True if deleted, False if not found.
        """
        process = self.get_process_info(process_hash)
        if process.output and Path(process.output).exists():
            # File might not exist anymore, so we use contextlib.suppress
            with contextlib.suppress(OSError):
                os.remove(process.output)

        cnx = self._get_process_storage_connection()
        cursor = cnx.cursor()
        cursor.execute(
            "DELETE FROM process_info WHERE hash = ?",
            (process_hash,))
        cnx.commit()
        return cursor.rowcount > 0

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
                    not process.active
                    and (process.output and Path(process.output).exists())
            )
        ]

        inactive_hashes = []

        for process in all_processes:
            if not process.active:
                process_hash = self.get_process_info_hash(process)
                inactive_hashes.append(process_hash)

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

    @staticmethod
    def _is_process_running(
            pid: int,
            executable: str,
            start_time: Optional[float] = None) -> bool:
        """Check if a process is running using psutil.

        Args:
            pid (int): Process ID to check.
            executable (str): Executable name to verify.
            start_time (Optional[float]): Start time to verify.

        Returns:
            bool: True if the process is running, False otherwise.

        """
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
    def _are_processes_running(
            pid_triplets: list[ProcessIdTriplet]) -> list[tuple[int, bool]]:
        """Check if the processes are still running.

        This checks for presence of `psutil` module and uses it if available.

        Args:
            pid_triplets (list[ProcessIdTriplet]): Processes ID to check.

        Returns:
            list[tuple[int, bool]]: List of tuples with process ID and
                boolean indicating if the process is running.

        """
        if not pid_triplets:
            result: list[tuple[int, bool]] = []
            return result

        return ProcessManager._check_processes_running(
                pid_triplets)

    @staticmethod
    def _check_processes_running(
            pid_triplets: list[ProcessIdTriplet]) -> list[tuple[int, bool]]:
        """Check if processes are running using psutil.

        Args:
            pid_triplets (list[ProcessIdTriplet]): List of triplets

        Returns:
            list[tuple[int, bool]]: List of tuples with process ID and
                boolean indicating if the process is running.

        """
        result: list[tuple[int, bool]] = []

        for pid, exe, start_time in pid_triplets:
            try:
                is_running = ProcessManager._is_process_running(
                    pid, exe, start_time
                )
            except Exception:  # noqa: BLE001
                # if something goes wrong, fall back to pid_exists
                try:
                    is_running = psutil.pid_exists(pid)
                except Exception:   # noqa: BLE001
                    is_running = False
            result.append((pid, is_running))
        return result

    @staticmethod
    def get_executable_path_by_pid(pid: int) -> Optional[Path]:
        """Get the executable path of a process by its PID using psutil.

        Args:
            pid (int): Process ID.

        Returns:
            Optional[Path]: The executable path of the process, or None if it
                cannot be determined.

        """
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
            process: subprocess.Popen) -> Optional[float]:
        """Get the start time of a process using psutil.

        Returns:
            Optional[float]: The start time of the process in seconds since
                the epoch, or None if it cannot be determined.

        """
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
    def get_process_start_time_by_pid(pid: int) -> Optional[float]:
        """Get the start time of a process by PID using psutil.

        Args:
            pid (int): Process ID.

        Returns:
            Optional[float]: The start time of the process in seconds since
                the epoch, or None if it cannot be determined.

        """
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
