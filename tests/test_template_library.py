"""Unit tests for colab-template-library modules."""

import importlib.util
import json
import os
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO_ROOT, rel_path))
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


downloader = load_module("downloader", os.path.join("templates", "direct-download", "downloader.py"))
task_manager = load_module("task_manager", os.path.join("runtime", "task_manager.py"))
colab_bridge = load_module("colab_bridge", os.path.join("runtime", "colab_bridge.py"))
colab_daemon = load_module("colab_daemon", os.path.join("runtime", "colab_daemon.py"))


class TestTemplateLibrary(unittest.TestCase):

    def test_manifest_and_templates_valid(self):
        with open(os.path.join(REPO_ROOT, "manifest.json"), "r", encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["version"], 1)
        ids = {t["id"] for t in manifest["templates"]}
        self.assertIn("direct-download", ids)
        self.assertIn("calculator", ids)
        for entry in manifest["templates"]:
            meta_path = os.path.join(REPO_ROOT, entry["metadataPath"])
            self.assertTrue(os.path.isfile(meta_path), f"Missing {meta_path}")
            with open(meta_path, "r", encoding="utf-8") as mf:
                detail = json.load(mf)
            self.assertEqual(detail["id"], entry["id"])

    def test_url_validation_blocks_injection(self):
        self.assertEqual(
            downloader.validate_url("https://example.com/archive.zip"),
            "https://example.com/archive.zip",
        )
        for bad in [
            "ftp://example.com/file.zip",
            "https://example.com/file;rm -rf /",
            "https://example.com/$(whoami)",
            "not-a-url",
            "",
        ]:
            with self.assertRaises(downloader.DownloadError):
                downloader.validate_url(bad)

    def test_filename_and_destination_sanitization(self):
        self.assertEqual(downloader.sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(downloader.sanitize_filename("a:b*c?.zip"), "a_b_c_.zip")
        self.assertEqual(downloader.sanitize_filename("..."), "downloaded_file")

        with tempfile.TemporaryDirectory() as tmp:
            resolved = downloader.sanitize_destination("/MyDrive/Downloads", drive_root=tmp)
            self.assertTrue(resolved.startswith(os.path.realpath(tmp)))
            with self.assertRaises(downloader.DownloadError):
                downloader.sanitize_destination("/MyDrive/../../escape", drive_root=tmp)

    def test_task_status_and_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = task_manager.TaskManager(base_dir=tmp)
            tid = mgr.create_task("direct-download", "Direct Download", {"url": "https://example.com/a.bin"})
            status = mgr.get_task(tid)
            self.assertIsNotNone(status)
            self.assertEqual(status["status"], "pending")

            mgr.update_task(tid, status="downloading", progress=45)
            self.assertEqual(mgr.get_task(tid)["progress"], 45)

            mgr.complete_task(tid, {"filePath": "/content/drive/MyDrive/Downloads/a.bin"})
            done = mgr.get_task(tid)
            self.assertEqual(done["status"], "completed")
            self.assertEqual(done["progress"], 100)

    def test_runtime_info_structure(self):
        info = colab_bridge.get_runtime_info()
        self.assertIn("ramTotalMb", info)
        self.assertIn("storageTotalMb", info)
        self.assertIn("runtimeType", info)

    def test_daemon_commands_and_status_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = colab_daemon.ColabDaemon(repo_dir=REPO_ROOT, task_dir=tmp, enable_tunnel=False)
            info = daemon.publish_runtime_status()
            self.assertEqual(info["daemonVersion"], "2.1.0")
            self.assertTrue(os.path.isfile(os.path.join(tmp, "runtime_status.json")))

            cmd_dir = os.path.join(tmp, "commands")
            os.makedirs(cmd_dir, exist_ok=True)
            cmd_file = os.path.join(cmd_dir, "cmd1.json")
            with open(cmd_file, "w", encoding="utf-8") as f:
                json.dump({"command": "refresh_stats"}, f)

            daemon.process_commands()
            self.assertFalse(os.path.exists(cmd_file))


if __name__ == "__main__":
    unittest.main()
