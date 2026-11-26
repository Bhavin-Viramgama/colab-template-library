"""
Universal Colab Runtime Daemon for Colab Template Launcher.

Runs a single persistent background loop inside Google Colab that:
1. Mounts Google Drive and initializes the .colab-tasks bridge directory.
2. Publishes real-time RAM, disk, GPU, and session telemetry to runtime_status.json.
3. Processes remote control commands from .colab-tasks/commands/
   (refresh_stats, pull_library, unassign_runtime).
4. Picks up pending template tasks from .colab-tasks/{task_id}/status.json
   and executes them via their registered Python runner.
"""

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

REPO_DIR = "/content/colab-template-library"
TASK_DIR = "/content/drive/MyDrive/.colab-tasks"
COMMANDS_DIR = os.path.join(TASK_DIR, "commands")


def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json_atomic(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


class ColabDaemon:
    """Persistent Colab runtime bridge daemon."""

    def __init__(
        self,
        repo_dir: str = REPO_DIR,
        task_dir: str = TASK_DIR,
        poll_interval: float = 2.0,
        status_interval: float = 10.0,
    ):
        self.repo_dir = repo_dir
        self.task_dir = task_dir
        self.commands_dir = os.path.join(task_dir, "commands")
        self.poll_interval = poll_interval
        self.status_interval = status_interval
        self.session_start_time = time.time()
        self._running = False
        self._active_workers: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

        sys.path.insert(0, self.repo_dir)
        from runtime.colab_bridge import get_runtime_info
        self._get_runtime_info = get_runtime_info

    def publish_runtime_status(self) -> Dict[str, Any]:
        """Write current runtime stats and active task count to Drive."""
        info = self._get_runtime_info()
        info["sessionStartTime"] = self.session_start_time
        info["daemonVersion"] = "2.0.0"
        with self._lock:
            alive = [tid for tid, t in self._active_workers.items() if t.is_alive()]
            for tid in list(self._active_workers.keys()):
                if not self._active_workers[tid].is_alive():
                    del self._active_workers[tid]
        info["activeTaskIds"] = alive
        _write_json_atomic(os.path.join(self.task_dir, "runtime_status.json"), info)
        return info

    def handle_command(self, cmd_path: str) -> Optional[str]:
        """Process a single JSON command file and remove it after handling."""
        try:
            with open(cmd_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            try:
                os.remove(cmd_path)
            except OSError:
                pass
            return None

        command = (payload.get("command") or "").strip().lower()
        try:
            os.remove(cmd_path)
        except OSError:
            pass

        if command == "refresh_stats":
            self.publish_runtime_status()
            return "refreshed"
        elif command == "pull_library":
            if os.path.isdir(self.repo_dir):
                subprocess.run(
                    ["git", "-C", self.repo_dir, "pull", "--ff-only"],
                    check=False,
                    capture_output=True,
                )
            self.publish_runtime_status()
            return "pulled"
        elif command in ("terminate", "unassign_runtime"):
            self._running = False
            self._write_disconnected_status()
            self._unassign_colab_runtime()
            return "terminated"
        return None

    def _write_disconnected_status(self) -> None:
        status_path = os.path.join(self.task_dir, "runtime_status.json")
        _write_json_atomic(
            status_path,
            {
                "connectionStatus": "disconnected",
                "runtimeType": "unknown",
                "ramUsedMb": 0,
                "ramTotalMb": 0,
                "storageUsedMb": 0,
                "storageTotalMb": 0,
                "timestamp": time.time(),
            },
        )

    def _unassign_colab_runtime(self) -> None:
        try:
            from google.colab import runtime  # type: ignore
            runtime.unassign()
        except Exception:
            pass

    def process_commands(self) -> None:
        """Process all pending command files in commands/."""
        if not os.path.isdir(self.commands_dir):
            return
        for name in sorted(os.listdir(self.commands_dir)):
            if not name.endswith(".json"):
                continue
            self.handle_command(os.path.join(self.commands_dir, name))

        legacy_ctrl = os.path.join(self.task_dir, "runtime_control.json")
        if os.path.isfile(legacy_ctrl):
            self.handle_command(legacy_ctrl)

    def process_pending_tasks(self) -> None:
        """Check .colab-tasks/{task_id}/status.json for pending tasks."""
        if not os.path.isdir(self.task_dir):
            return
        for entry in sorted(os.listdir(self.task_dir)):
            if entry in ("commands",):
                continue
            task_subdir = os.path.join(self.task_dir, entry)
            status_file = os.path.join(task_subdir, "status.json")
            if not os.path.isdir(task_subdir) or not os.path.isfile(status_file):
                continue
            with self._lock:
                if entry in self._active_workers and self._active_workers[entry].is_alive():
                    continue
            try:
                with open(status_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            if data.get("status") == "pending":
                self._launch_task_worker(entry, data)

    def _launch_task_worker(self, task_id: str, task_data: Dict[str, Any]) -> None:
        template_id = task_data.get("templateId") or task_id.split("_")[0]
        inputs = task_data.get("inputs") or {}

        def _worker() -> None:
            status_file = os.path.join(self.task_dir, task_id, "status.json")
            try:
                if template_id == "direct-download":
                    dl_mod = _load_module(
                        "downloader",
                        os.path.join(self.repo_dir, "templates", "direct-download", "downloader.py"),
                    )
                    dl_mod.install_aria2()
                    dest = dl_mod.sanitize_destination(
                        inputs.get("destination", "/MyDrive/Downloads")
                    )
                    dl_mod.download_file(
                        url=inputs["url"],
                        task_id=task_id,
                        filename=inputs.get("filename"),
                        destination=dest,
                        status_dir=self.task_dir,
                    )
                else:
                    raise ValueError(f"Unsupported Colab template: {template_id}")
            except Exception as exc:
                task_data["status"] = "failed"
                task_data["error"] = str(exc)
                task_data["progressMessage"] = f"Error: {exc}"
                task_data["lastUpdated"] = time.time()
                _write_json_atomic(status_file, task_data)

        thread = threading.Thread(target=_worker, name=f"worker-{task_id}", daemon=True)
        with self._lock:
            self._active_workers[task_id] = thread
        thread.start()

    def run(self) -> None:
        """Main daemon loop."""
        os.makedirs(self.task_dir, exist_ok=True)
        os.makedirs(self.commands_dir, exist_ok=True)
        self._running = True
        last_status_time = 0.0
        print("Colab Template Launcher daemon started. Ready for commands and tasks.")

        try:
            while self._running:
                now = time.time()
                if now - last_status_time >= self.status_interval:
                    self.publish_runtime_status()
                    last_status_time = now
                self.process_commands()
                if not self._running:
                    break
                self.process_pending_tasks()
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            print("Daemon interrupted by user.")
            self._write_disconnected_status()


def start_daemon(repo_dir: str = REPO_DIR, task_dir: str = TASK_DIR) -> None:
    """Convenience entry point for Colab launcher notebook."""
    daemon = ColabDaemon(repo_dir=repo_dir, task_dir=task_dir)
    daemon.run()


if __name__ == "__main__":
    start_daemon()
