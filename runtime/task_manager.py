"""
Task Manager - Manages task lifecycle and status tracking on Google Drive.

Creates task directories, writes/reads status JSON files, and supports
the full task lifecycle (create, update, complete, fail, cancel).
"""

import json
import os
import time
import uuid
from typing import Dict, Any, Optional, List


TASK_BASE_DIR = "/content/drive/MyDrive/.colab-tasks"


class TaskManager:
    """Manages task lifecycle and status files on Google Drive."""

    def __init__(self, base_dir: str = TASK_BASE_DIR):
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)

    def create_task(
        self,
        template_id: str,
        template_name: str,
        inputs: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Create a new task with a unique ID.

        Args:
            template_id: ID of the template to execute.
            template_name: Display name of the template.
            inputs: Input parameters for the template.

        Returns:
            The generated task ID.
        """
        task_id = f"{template_id}_{uuid.uuid4().hex[:8]}_{int(time.time())}"
        task_dir = os.path.join(self.base_dir, task_id)
        os.makedirs(task_dir, exist_ok=True)

        status = {
            "taskId": task_id,
            "templateId": template_id,
            "templateName": template_name,
            "status": "pending",
            "progress": 0,
            "progressMessage": "Task created",
            "inputs": inputs or {},
            "result": None,
            "error": None,
            "createdAt": time.time(),
            "updatedAt": time.time(),
        }

        self._write_status(task_id, status)
        return task_id

    def update_task(self, task_id: str, **kwargs) -> None:
        """Update task status fields."""
        status = self.get_task(task_id)
        if status:
            status.update(kwargs)
            status["updatedAt"] = time.time()
            self._write_status(task_id, status)

    def complete_task(self, task_id: str, result: Any = None) -> None:
        """Mark a task as completed."""
        self.update_task(
            task_id,
            status="completed",
            progress=100,
            progressMessage="Task completed",
            result=result,
        )

    def fail_task(self, task_id: str, error: str) -> None:
        """Mark a task as failed."""
        self.update_task(
            task_id,
            status="failed",
            progressMessage="Task failed",
            error=error,
        )

    def cancel_task(self, task_id: str) -> None:
        """Mark a task as cancelled and write control command."""
        control_file = os.path.join(self.base_dir, task_id, "control.json")
        with open(control_file, "w") as f:
            json.dump({"command": "cancel", "timestamp": time.time()}, f)

        self.update_task(
            task_id,
            status="cancelled",
            progressMessage="Task cancelled",
        )

    def pause_task(self, task_id: str) -> None:
        """Pause a running task."""
        control_file = os.path.join(self.base_dir, task_id, "control.json")
        with open(control_file, "w") as f:
            json.dump({"command": "pause", "timestamp": time.time()}, f)

    def resume_task(self, task_id: str) -> None:
        """Resume a paused task."""
        control_file = os.path.join(self.base_dir, task_id, "control.json")
        with open(control_file, "w") as f:
            json.dump({"command": "resume", "timestamp": time.time()}, f)

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Read task status from disk."""
        status_file = os.path.join(self.base_dir, task_id, "status.json")
        if os.path.exists(status_file):
            try:
                with open(status_file, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return None
        return None

    def list_tasks(self, status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all tasks, optionally filtered by status."""
        tasks = []
        if not os.path.exists(self.base_dir):
            return tasks

        for task_id in os.listdir(self.base_dir):
            task_dir = os.path.join(self.base_dir, task_id)
            if os.path.isdir(task_dir) and task_id != "runtime_status.json":
                task = self.get_task(task_id)
                if task:
                    if status_filter is None or task.get("status") == status_filter:
                        tasks.append(task)

        tasks.sort(key=lambda t: t.get("createdAt", 0), reverse=True)
        return tasks

    def cleanup_old_tasks(self, max_age_hours: int = 72) -> int:
        """Remove task directories older than max_age_hours."""
        import shutil
        removed = 0
        cutoff = time.time() - (max_age_hours * 3600)

        for task_id in os.listdir(self.base_dir):
            task_dir = os.path.join(self.base_dir, task_id)
            if os.path.isdir(task_dir):
                task = self.get_task(task_id)
                if task and task.get("updatedAt", 0) < cutoff:
                    if task.get("status") in ("completed", "failed", "cancelled"):
                        shutil.rmtree(task_dir, ignore_errors=True)
                        removed += 1

        return removed

    def _write_status(self, task_id: str, status: Dict[str, Any]) -> None:
        """Write status to JSON file."""
        status_file = os.path.join(self.base_dir, task_id, "status.json")
        with open(status_file, "w") as f:
            json.dump(status, f, indent=2)
