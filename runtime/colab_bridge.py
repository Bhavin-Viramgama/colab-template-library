"""
Colab Runtime Bridge - Provides runtime information and task management.

Collects system stats, manages task lifecycle, and provides a bridge
between the Colab runtime and the Android application.
"""

import json
import os
import time
from typing import Dict, Any, Optional


def get_runtime_info() -> Dict[str, Any]:
    """
    Collect runtime information from the Colab environment.

    Returns:
        Dict with runtime stats including RAM, storage, GPU info, and session details.
    """
    info: Dict[str, Any] = {
        "connectionStatus": "connected",
        "runtimeType": "unknown",
        "ramUsedMb": 0,
        "ramTotalMb": 0,
        "storageUsedMb": 0,
        "storageTotalMb": 0,
        "gpuName": None,
        "gpuMemoryMb": None,
        "sessionStartTime": None,
        "timestamp": time.time(),
    }

    try:
        import psutil
        mem = psutil.virtual_memory()
        info["ramUsedMb"] = round(mem.used / (1024 * 1024))
        info["ramTotalMb"] = round(mem.total / (1024 * 1024))

        disk = psutil.disk_usage('/')
        info["storageUsedMb"] = round(disk.used / (1024 * 1024))
        info["storageTotalMb"] = round(disk.total / (1024 * 1024))
    except ImportError:
        pass

    # Detect GPU
    try:
        import subprocess
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split(',')
            info["gpuName"] = parts[0].strip()
            if len(parts) > 1:
                info["gpuMemoryMb"] = int(float(parts[1].strip()))
            info["runtimeType"] = "GPU"
        else:
            info["runtimeType"] = "CPU"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        info["runtimeType"] = "CPU"

    # Check for TPU
    if "COLAB_TPU_ADDR" in os.environ:
        info["runtimeType"] = "TPU"

    return info


def write_runtime_status(status_dir: str = "/content/drive/MyDrive/.colab-tasks") -> str:
    """
    Write runtime info to a status file on Google Drive.

    Args:
        status_dir: Directory to write the runtime status file.

    Returns:
        Path to the written status file.
    """
    os.makedirs(status_dir, exist_ok=True)
    status_file = os.path.join(status_dir, "runtime_status.json")
    info = get_runtime_info()

    with open(status_file, "w") as f:
        json.dump(info, f, indent=2)

    return status_file


def check_drive_mounted() -> bool:
    """Check if Google Drive is mounted."""
    return os.path.exists("/content/drive/MyDrive")


def mount_drive() -> bool:
    """
    Mount Google Drive in the Colab environment.

    Returns:
        True if Drive was mounted successfully.
    """
    if check_drive_mounted():
        return True

    try:
        from google.colab import drive
        drive.mount('/content/drive')
        return check_drive_mounted()
    except ImportError:
        print("Not running in Google Colab environment.")
        return False
    except Exception as e:
        print(f"Failed to mount Google Drive: {e}")
        return False


def setup_runtime(task_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Initialize the runtime environment.

    Args:
        task_id: Optional task ID for status reporting.

    Returns:
        Dict with setup results.
    """
    result = {
        "driveMounted": False,
        "runtimeInfo": {},
        "ready": False,
    }

    result["driveMounted"] = mount_drive()
    result["runtimeInfo"] = get_runtime_info()

    if result["driveMounted"]:
        write_runtime_status()

    result["ready"] = result["driveMounted"]
    return result


if __name__ == "__main__":
    setup_result = setup_runtime()
    print(json.dumps(setup_result, indent=2))
