"""
Direct Download Module - aria2-based file downloader for Google Colab.

Uses aria2c's JSON-RPC interface (aria2.addUri, aria2.tellStatus,
aria2.pause, aria2.unpause, aria2.forceRemove) for structured, reliable
progress reporting, pause/resume, and cancellation without fragile
stdout parsing.
"""

import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from typing import Optional, Dict, Any
from urllib.parse import urlparse, unquote


TASK_STATUS_DIR = "/content/drive/MyDrive/.colab-tasks"
SUPPORTED_SCHEMES = ("http", "https")


class DownloadError(Exception):
    """Raised when a download operation fails."""
    pass


class TaskStatus:
    """Manages task status reporting via JSON files on Google Drive."""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    def __init__(self, task_id: str, status_dir: str = TASK_STATUS_DIR):
        self.task_id = task_id
        self.task_dir = os.path.join(status_dir, task_id)
        self.status_file = os.path.join(self.task_dir, "status.json")
        self.control_file = os.path.join(self.task_dir, "control.json")
        os.makedirs(self.task_dir, exist_ok=True)
        self._status_data: Dict[str, Any] = {
            "taskId": task_id,
            "status": self.PENDING,
            "filename": "",
            "totalBytes": 0,
            "completedBytes": 0,
            "speedBytesPerSecond": 0,
            "etaSeconds": 0,
            "progress": 0,
            "progressMessage": "Preparing download...",
            "error": None,
            "outputPath": None,
            "startTime": time.time(),
            "lastUpdated": time.time(),
        }
        self._write_status()

    def update(self, **kwargs) -> None:
        """Update status fields and write in-place to disk."""
        self._status_data.update(kwargs)
        total = self._status_data.get("totalBytes", 0) or 0
        completed = self._status_data.get("completedBytes", 0) or 0
        if total > 0 and "progress" not in kwargs:
            self._status_data["progress"] = min(100.0, round((completed / total) * 100.0, 1))
        self._status_data["lastUpdated"] = time.time()
        self._write_status()

    def get_control_command(self) -> Optional[str]:
        """Read and consume the latest control command (pause, resume, cancel)."""
        if not os.path.exists(self.control_file):
            return None
        try:
            with open(self.control_file, "r", encoding="utf-8") as f:
                control = json.load(f)
            cmd = control.get("command")
            os.remove(self.control_file)
            return cmd
        except (json.JSONDecodeError, OSError):
            return None

    def _write_status(self) -> None:
        """Write status JSON in-place and fsync so Drive FUSE flushes promptly."""
        try:
            with open(self.status_file, "w", encoding="utf-8") as f:
                json.dump(self._status_data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            print(f"Warning: Could not write status file: {e}")


def sanitize_filename(filename: str) -> str:
    """Sanitize a filename to remove path traversal and invalid characters."""
    filename = filename.strip().replace("\\", "/").split("/")[-1]
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename)
    filename = filename.strip(". ")
    if not filename or filename in (".", ".."):
        filename = "downloaded_file"
    if len(filename) > 200:
        name, ext = os.path.splitext(filename)
        filename = name[: 200 - len(ext)] + ext
    return filename


def sanitize_destination(destination: str, drive_root: str = "/content/drive/MyDrive") -> str:
    """Resolve and constrain destination inside Google Drive root."""
    cleaned = destination.strip().lstrip("/")
    if cleaned.startswith("MyDrive/"):
        cleaned = cleaned[len("MyDrive/"):]
    elif cleaned == "MyDrive":
        cleaned = ""
    if cleaned.startswith("content/drive/MyDrive/"):
        cleaned = cleaned[len("content/drive/MyDrive/"):]
    resolved = os.path.realpath(os.path.join(drive_root, cleaned))
    root_real = os.path.realpath(drive_root)
    if not (resolved == root_real or resolved.startswith(root_real + os.sep)):
        raise DownloadError("Destination path escapes Google Drive root.")
    return resolved


def validate_url(url: str) -> str:
    """Validate and sanitize a download URL against injection and unsupported schemes."""
    url = url.strip()
    if not url or len(url) > 2048:
        raise DownloadError("URL is empty or exceeds maximum length.")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in SUPPORTED_SCHEMES:
        raise DownloadError(
            f"Invalid URL scheme '{parsed.scheme}'. Only HTTP and HTTPS are supported."
        )
    if not parsed.netloc or any(c.isspace() for c in url):
        raise DownloadError("Invalid URL format.")
    if any(c in url for c in (";", "|", "`", "$", "{", "}", "\n", "\r", "\x00")):
        raise DownloadError("URL contains disallowed control or shell characters.")
    return url


def extract_filename_from_url(url: str) -> str:
    """Extract a safe filename from a URL path."""
    parsed = urlparse(url)
    path = unquote(parsed.path)
    filename = os.path.basename(path)
    if not filename or "." not in filename:
        filename = "downloaded_file"
    return sanitize_filename(filename)


def install_aria2() -> None:
    """Install aria2c if not already available."""
    try:
        subprocess.run(["aria2c", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        subprocess.run(["apt-get", "update", "-qq"], capture_output=True, check=False)
        subprocess.run(["apt-get", "install", "-y", "-qq", "aria2"], capture_output=True, check=True)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Aria2RpcClient:
    """Lightweight JSON-RPC client for aria2c."""

    def __init__(self, endpoint: str, secret: str):
        self.endpoint = endpoint
        self.secret = secret
        self._req_id = 0

    def call(self, method: str, *params: Any) -> Any:
        self._req_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": str(self._req_id),
            "method": method,
            "params": [f"token:{self.secret}", *params],
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise DownloadError(f"aria2 RPC connection error: {exc}") from exc

        if "error" in body and body["error"]:
            raise DownloadError(f"aria2 RPC error: {body['error'].get('message', body['error'])}")
        return body.get("result")


def _wait_for_rpc(client: Aria2RpcClient, timeout_seconds: float = 10.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            client.call("aria2.getVersion")
            return
        except DownloadError:
            time.sleep(0.25)
    raise DownloadError("Timed out waiting for aria2 RPC server to start.")


def download_file(
    url: str,
    task_id: str,
    filename: Optional[str] = None,
    destination: str = "/content/drive/MyDrive/Downloads",
    max_connections: int = 16,
    split: int = 16,
    max_retries: int = 3,
    status_dir: str = TASK_STATUS_DIR,
) -> Dict[str, Any]:
    """Download a file using aria2c JSON-RPC with real-time Drive status reporting."""
    url = validate_url(url)
    status = TaskStatus(task_id, status_dir=status_dir)

    resolved_filename = (
        sanitize_filename(filename) if filename and filename.strip() else extract_filename_from_url(url)
    )
    safe_dest = (
        destination
        if destination.startswith("/content/drive/MyDrive")
        else sanitize_destination(destination)
    )
    os.makedirs(safe_dest, exist_ok=True)
    output_path = os.path.join(safe_dest, resolved_filename)

    rpc_port = _find_free_port()
    rpc_secret = f"ct_{task_id}_{int(time.time())}"
    rpc_endpoint = f"http://127.0.0.1:{rpc_port}/jsonrpc"

    aria2_cmd = [
        "aria2c",
        "--enable-rpc=true",
        "--rpc-listen-all=false",
        f"--rpc-listen-port={rpc_port}",
        f"--rpc-secret={rpc_secret}",
        "--continue=true",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        f"--max-connection-per-server={min(max(1, max_connections), 16)}",
        f"--split={min(max(1, split), 16)}",
        "--min-split-size=1M",
        "--file-allocation=none",
        f"--max-tries={max(1, max_retries)}",
        "--retry-wait=3",
        "--quiet=true",
    ]

    daemon = subprocess.Popen(aria2_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    client = Aria2RpcClient(rpc_endpoint, rpc_secret)
    start_time = time.time()

    try:
        _wait_for_rpc(client)
        gid = client.call(
            "aria2.addUri",
            [url],
            {"dir": safe_dest, "out": resolved_filename},
        )
        status.update(
            status=TaskStatus.DOWNLOADING,
            filename=resolved_filename,
            outputPath=output_path,
            progressMessage="Downloading...",
        )

        while True:
            command = status.get_control_command()
            if command == "cancel":
                try:
                    client.call("aria2.forceRemove", gid)
                except DownloadError:
                    pass
                status.update(
                    status=TaskStatus.CANCELLED,
                    speedBytesPerSecond=0,
                    etaSeconds=0,
                    progressMessage="Download cancelled by user.",
                )
                return {"success": False, "status": "cancelled", "error": "Cancelled by user"}
            elif command == "pause":
                client.call("aria2.pause", gid)
                status.update(status=TaskStatus.PAUSED, speedBytesPerSecond=0, progressMessage="Paused")
            elif command == "resume":
                client.call("aria2.unpause", gid)
                status.update(status=TaskStatus.DOWNLOADING, progressMessage="Resuming download...")

            info = client.call(
                "aria2.tellStatus",
                gid,
                ["status", "totalLength", "completedLength", "downloadSpeed", "errorCode", "errorMessage"],
            )
            aria_state = info.get("status", "")
            total_bytes = int(info.get("totalLength") or 0)
            completed_bytes = int(info.get("completedLength") or 0)
            speed_bps = int(info.get("downloadSpeed") or 0)
            remaining = max(0, total_bytes - completed_bytes)
            eta_seconds = int(remaining / speed_bps) if speed_bps > 0 and total_bytes > 0 else 0

            if aria_state == "complete":
                final_size = os.path.getsize(output_path) if os.path.exists(output_path) else total_bytes
                elapsed = round(time.time() - start_time, 2)
                status.update(
                    status=TaskStatus.COMPLETED,
                    totalBytes=final_size,
                    completedBytes=final_size,
                    speedBytesPerSecond=0,
                    etaSeconds=0,
                    progress=100,
                    progressMessage="Download completed",
                )
                return {
                    "success": True,
                    "filePath": output_path,
                    "fileSize": final_size,
                    "downloadTime": elapsed,
                    "filename": resolved_filename,
                }
            elif aria_state == "error":
                err_msg = info.get("errorMessage") or f"aria2 error code {info.get('errorCode')}"
                status.update(
                    status=TaskStatus.FAILED,
                    speedBytesPerSecond=0,
                    etaSeconds=0,
                    error=err_msg,
                    progressMessage="Download failed",
                )
                raise DownloadError(err_msg)
            elif aria_state == "removed":
                status.update(
                    status=TaskStatus.CANCELLED,
                    speedBytesPerSecond=0,
                    etaSeconds=0,
                    progressMessage="Download cancelled",
                )
                return {"success": False, "status": "cancelled"}
            elif aria_state == "paused":
                status.update(
                    status=TaskStatus.PAUSED,
                    totalBytes=total_bytes,
                    completedBytes=completed_bytes,
                    speedBytesPerSecond=0,
                    etaSeconds=0,
                    progressMessage="Paused",
                )
            else:
                status.update(
                    status=TaskStatus.DOWNLOADING,
                    totalBytes=total_bytes,
                    completedBytes=completed_bytes,
                    speedBytesPerSecond=speed_bps,
                    etaSeconds=eta_seconds,
                    progressMessage="Downloading...",
                )

            time.sleep(0.5)
    except Exception as exc:
        if not isinstance(exc, DownloadError):
            status.update(status=TaskStatus.FAILED, error=str(exc), progressMessage="Download failed")
            raise DownloadError(str(exc)) from exc
        raise
    finally:
        try:
            client.call("aria2.shutdown")
        except Exception:
            pass
        if daemon.poll() is None:
            daemon.terminate()
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
