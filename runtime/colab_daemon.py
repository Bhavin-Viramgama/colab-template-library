"""
Universal Colab Runtime Daemon for Colab Template Launcher.

Runs a single persistent background loop inside Google Colab that:
1. Mounts Google Drive and initializes the .colab-tasks bridge directory.
2. Starts a local HTTP status/control API + optional Cloudflare Quick Tunnel
   for sub-second progress updates without Drive FUSE buffering lag.
3. Publishes real-time RAM, disk, GPU, tunnelUrl, and session telemetry to runtime_status.json.
4. Processes remote control commands and pending template tasks.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass


class ColabDaemon:
    """Persistent Colab runtime bridge daemon."""

    def __init__(
        self,
        repo_dir: str = REPO_DIR,
        task_dir: str = TASK_DIR,
        poll_interval: float = 1.0,
        status_interval: float = 8.0,
        enable_tunnel: bool = True,
    ):
        self.repo_dir = repo_dir
        self.task_dir = task_dir
        self.commands_dir = os.path.join(task_dir, "commands")
        self.poll_interval = poll_interval
        self.status_interval = status_interval
        self.enable_tunnel = enable_tunnel
        self.session_start_time = time.time()
        self._running = False
        self._active_workers: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self.tunnel_url: Optional[str] = None
        self._http_server: Optional[ThreadingHTTPServer] = None
        self._tunnel_proc: Optional[subprocess.Popen] = None

        sys.path.insert(0, self.repo_dir)
        from runtime.colab_bridge import get_runtime_info
        self._get_runtime_info = get_runtime_info

    def _start_local_api_and_tunnel(self) -> None:
        daemon = self

        class BridgeHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/health":
                    self._send_json(200, {"ok": True, "timestamp": time.time()})
                elif path == "/runtime":
                    self._send_json(200, daemon.build_runtime_info())
                elif path.startswith("/tasks/"):
                    task_id = path[len("/tasks/"):].strip("/")
                    status_path = os.path.join(daemon.task_dir, task_id, "status.json")
                    if not os.path.isfile(status_path):
                        self._send_json(404, {"error": "Task not found"})
                        return
                    try:
                        with open(status_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        self._send_json(200, data)
                    except (json.JSONDecodeError, OSError) as exc:
                        self._send_json(503, {"error": str(exc)})
                else:
                    self._send_json(404, {"error": "Not found"})

            def do_POST(self) -> None:
                path = self.path.split("?", 1)[0]
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "Invalid JSON"})
                    return

                if path == "/commands":
                    os.makedirs(daemon.commands_dir, exist_ok=True)
                    cmd_file = os.path.join(
                        daemon.commands_dir, f"http_{int(time.time() * 1000)}.json"
                    )
                    _write_json_atomic(cmd_file, body)
                    self._send_json(200, {"accepted": True})
                elif path.startswith("/tasks/") and path.endswith("/control"):
                    task_id = path[len("/tasks/"):-len("/control")].strip("/")
                    ctrl_file = os.path.join(daemon.task_dir, task_id, "control.json")
                    _write_json_atomic(ctrl_file, body)
                    self._send_json(200, {"accepted": True})
                else:
                    self._send_json(404, {"error": "Not found"})

        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), BridgeHandler)
            self._http_server = server
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            if self.enable_tunnel:
                threading.Thread(
                    target=self._launch_cloudflared, args=(port,), daemon=True
                ).start()
        except Exception as exc:
            print(f"Warning: Local HTTP bridge unavailable: {exc}")

    def _launch_cloudflared(self, port: int) -> None:
        binary = shutil.which("cloudflared")
        if not binary:
            local_bin = "/tmp/cloudflared"
            if not os.path.exists(local_bin):
                try:
                    url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
                    urllib.request.urlretrieve(url, local_bin)
                    os.chmod(local_bin, 0o755)
                except Exception as exc:
                    print(f"Quick tunnel unavailable (using Drive bridge): {exc}")
                    return
            binary = local_bin

        try:
            proc = subprocess.Popen(
                [binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self._tunnel_proc = proc
            pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
            if proc.stdout:
                for line in proc.stdout:
                    match = pattern.search(line)
                    if match and not self.tunnel_url:
                        self.tunnel_url = match.group(0)
                        print(f"Direct tunnel active: {self.tunnel_url}")
                        self.publish_runtime_status()
        except Exception as exc:
            print(f"Quick tunnel failed (falling back to Drive bridge): {exc}")

    def build_runtime_info(self) -> Dict[str, Any]:
        info = self._get_runtime_info()
        info["sessionStartTime"] = self.session_start_time
        info["daemonVersion"] = "2.1.0"
        if self.tunnel_url:
            info["tunnelUrl"] = self.tunnel_url
        with self._lock:
            alive = [tid for tid, t in self._active_workers.items() if t.is_alive()]
            for tid in list(self._active_workers.keys()):
                if not self._active_workers[tid].is_alive():
                    del self._active_workers[tid]
        info["activeTaskIds"] = alive
        return info

    def publish_runtime_status(self) -> Dict[str, Any]:
        """Write current runtime stats and active task count to Drive."""
        info = self.build_runtime_info()
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
            self._shutdown_servers()
            self._unassign_colab_runtime()
            return "terminated"
        return None

    def _shutdown_servers(self) -> None:
        if self._http_server:
            try:
                self._http_server.shutdown()
            except Exception:
                pass
        if self._tunnel_proc and self._tunnel_proc.poll() is None:
            self._tunnel_proc.terminate()

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
                data["status"] = "running"
                data["progressMessage"] = "Picked up by Colab daemon..."
                data["lastUpdated"] = time.time()
                _write_json_atomic(status_file, data)
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
        self._start_local_api_and_tunnel()
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
            self._shutdown_servers()


def start_daemon(repo_dir: str = REPO_DIR, task_dir: str = TASK_DIR) -> None:
    """Convenience entry point for Colab launcher notebook."""
    daemon = ColabDaemon(repo_dir=repo_dir, task_dir=task_dir)
    daemon.run()


if __name__ == "__main__":
    start_daemon()
