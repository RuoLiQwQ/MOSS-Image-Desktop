"""Installer integration tests. All downloads use a local disposable HTTP server."""
from __future__ import annotations

import collections
from contextlib import contextmanager
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request


BUNDLE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tested_launcher", BUNDLE / "app" / "launcher.py")
launcher = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(BUNDLE / "app"))
try:
    SPEC.loader.exec_module(launcher)
    from storage import StorageError
finally:
    sys.path.pop(0)
PAYLOAD = bytes(range(256)) * 8193
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


class FastEvent:
    """Avoid real retry delays while preserving cancellation behavior."""
    def __init__(self):
        self.stopped = False
        self.delays = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.delays.append(seconds)
        return self.stopped


@contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dest = Path(self.temp.name) / "model.safetensors"
        self.part = self.dest.with_name(self.dest.name + ".part")
        self.event = FastEvent()
        self.messages = []

    @contextmanager
    def remote(self, mode="normal"):
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                header = self.headers.get("Range")
                requests.append(header)
                offset = int(header.split("=")[1].split("-")[0]) if header else 0
                if mode == "ignore-range":
                    offset = 0
                body = PAYLOAD[offset:]
                if mode == "corrupt":
                    body = b"x" * len(body)
                status = 206 if offset else 200
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                if status == 206:
                    claimed = offset + 1 if mode == "wrong-range" else offset
                    self.send_header("Content-Range", f"bytes {claimed}-{len(PAYLOAD)-1}/{len(PAYLOAD)}")
                self.end_headers()
                if mode == "truncate-first" and len(requests) == 1:
                    self.wfile.write(body[:1234567])
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                else:
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        with serving(server) as url:
            yield url + "/model", requests

    def fetch(self, url, event=None):
        launcher.fetch_file(url, self.dest, len(PAYLOAD), DIGEST, event or self.event, self.messages.append)

    def assert_complete(self):
        self.assertEqual(self.dest.read_bytes(), PAYLOAD)
        self.assertFalse(self.part.exists())

    def test_normal_download_checks_hash_and_promotes_file(self):
        with self.remote() as (url, requests):
            self.fetch(url)
        self.assert_complete()
        self.assertEqual(requests, [None])

    def test_completed_valid_file_makes_no_network_request(self):
        self.dest.write_bytes(PAYLOAD)
        with self.remote() as (url, requests):
            self.fetch(url)
        self.assertEqual(requests, [])

    def test_resume_uses_exact_range(self):
        self.part.write_bytes(PAYLOAD[:12345])
        with self.remote() as (url, requests):
            self.fetch(url)
        self.assert_complete()
        self.assertEqual(requests, ["bytes=12345-"])

    def test_server_ignoring_range_restarts_without_appending(self):
        self.part.write_bytes(PAYLOAD[:12345])
        with self.remote("ignore-range") as (url, requests):
            self.fetch(url)
        self.assert_complete()
        self.assertEqual(requests, ["bytes=12345-"])

    def test_truncated_response_resumes_on_retry(self):
        with self.remote("truncate-first") as (url, requests):
            self.fetch(url)
        self.assert_complete()
        self.assertGreaterEqual(len(requests), 2)
        self.assertTrue(requests[1].startswith("bytes="))
        self.assertNotEqual(requests[1], "bytes=0-")

    def test_wrong_content_range_never_promotes_bad_data(self):
        prefix = PAYLOAD[:12345]
        self.part.write_bytes(prefix)
        with self.remote("wrong-range") as (url, requests):
            with self.assertRaises(RuntimeError):
                self.fetch(url)
        self.assertEqual(len(requests), 3)
        self.assertEqual(self.part.read_bytes(), prefix)
        self.assertFalse(self.dest.exists())

    def test_corrupt_hash_retries_and_discards_part(self):
        with self.remote("corrupt") as (url, requests):
            with self.assertRaises(RuntimeError):
                self.fetch(url)
        self.assertEqual(len(requests), 3)
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.part.exists())

    def test_full_valid_part_is_promoted_without_network(self):
        self.part.write_bytes(PAYLOAD)
        with self.remote() as (url, requests):
            self.fetch(url)
        self.assert_complete()
        self.assertEqual(requests, [])

    def test_full_corrupt_part_is_replaced(self):
        self.part.write_bytes(b"x" * len(PAYLOAD))
        with self.remote() as (url, requests):
            self.fetch(url)
        self.assert_complete()
        self.assertEqual(requests, [None])

    def test_corrupt_destination_is_preserved_until_success(self):
        original = b"x" * len(PAYLOAD)
        self.dest.write_bytes(original)
        with self.remote("corrupt") as (url, _requests):
            with self.assertRaises(RuntimeError):
                self.fetch(url)
        self.assertEqual(self.dest.read_bytes(), original)

    def test_cancel_preserves_partial_download(self):
        prefix = PAYLOAD[:12345]
        self.part.write_bytes(prefix)
        self.event.set()
        with self.remote() as (url, requests):
            with self.assertRaises(launcher.Cancelled):
                self.fetch(url)
        self.assertEqual(requests, [])
        self.assertEqual(self.part.read_bytes(), prefix)

    def test_cancel_during_transfer_preserves_resumable_part(self):
        def cancel_after_progress(_line):
            self.event.set()

        with self.remote() as (url, _requests):
            with self.assertRaises(launcher.Cancelled):
                launcher.fetch_file(url, self.dest, len(PAYLOAD), DIGEST, self.event, cancel_after_progress)
        self.assertTrue(self.part.exists())
        self.assertGreater(self.part.stat().st_size, 0)
        self.assertFalse(self.dest.exists())


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "app").mkdir()
        (root / "app" / "installer.html").write_text("<html>Installer</html>", encoding="utf-8")
        self.manager = mock.Mock(root=root)
        self.manager.snapshot.return_value = {"phase": "idle"}
        self.token = "test-token-only"
        self.server = launcher.create_server(self.manager, self.token)
        self.context = serving(self.server)
        self.url = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)

    def request(self, path="/api/status", headers=None, body=None):
        req = urllib.request.Request(self.url + path, headers=headers or {}, data=body)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read()), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read()), dict(exc.headers)

    def test_status_requires_token(self):
        self.assertEqual(self.request()[0], 403)

    def test_status_accepts_correct_token(self):
        status, payload, headers = self.request(headers={"X-Launcher-Token": self.token})
        self.assertEqual((status, payload), (200, {"phase": "idle"}))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_wrong_token_is_rejected(self):
        self.assertEqual(self.request(headers={"X-Launcher-Token": "wrong"})[0], 403)

    def test_cross_origin_is_rejected_even_with_token(self):
        headers = {"X-Launcher-Token": self.token, "Origin": "https://evil.example"}
        self.assertEqual(self.request("/api/install", headers, b"{}")[0], 403)
        self.manager.start_install.assert_not_called()

    def test_null_origin_is_rejected(self):
        headers = {"X-Launcher-Token": self.token, "Origin": "null"}
        self.assertEqual(self.request("/api/install", headers, b"{}")[0], 403)

    def test_dns_rebinding_host_is_rejected(self):
        headers = {"X-Launcher-Token": self.token, "Host": "evil.example"}
        self.assertEqual(self.request(headers=headers)[0], 403)

    def test_same_origin_install_passes_selected_source(self):
        headers = {"X-Launcher-Token": self.token, "Origin": self.url}
        self.assertEqual(self.request("/api/install", headers, b'{"source":"huggingface"}')[0], 200)
        self.manager.start_install.assert_called_once_with("huggingface")

    def test_cross_origin_stop_cannot_kill_service(self):
        headers = {"X-Launcher-Token": self.token, "Origin": "https://evil.example"}
        self.assertEqual(self.request("/api/stop", headers, b"{}")[0], 403)
        self.manager.stop.assert_not_called()

    def test_stop_with_token_calls_manager(self):
        self.assertEqual(self.request("/api/stop", {"X-Launcher-Token": self.token}, b"{}")[0], 200)
        self.manager.stop.assert_called_once_with()

    def test_shutdown_requires_token(self):
        self.assertEqual(self.request("/api/shutdown", body=b"{}")[0], 403)

    def test_shutdown_with_token_stops_control_server(self):
        self.assertEqual(self.request("/api/shutdown", {"X-Launcher-Token": self.token}, b"{}")[0], 200)

    def test_open_output_uses_configured_folder(self):
        self.manager.output_dir = self.manager.root / "chosen-pictures"
        with mock.patch.object(launcher.os, "startfile", create=True) as open_folder:
            self.assertEqual(self.request("/api/open-output", {"X-Launcher-Token": self.token}, b"{}")[0], 200)
        open_folder.assert_called_once_with(self.manager.output_dir)

    def test_malformed_and_oversized_requests_are_rejected(self):
        headers = {"X-Launcher-Token": self.token}
        for body in (b"{", b"[]", b"x" * 4097):
            with self.subTest(body_length=len(body)):
                self.assertEqual(self.request("/api/install", headers, body)[0], 400)
        self.manager.start_install.assert_not_called()


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = {"files": [{"path": "transformer/model.safetensors", "size": 2 * launcher.GIB,
                                   "sha256": "0" * 64, "urls": {}}]}
        (self.root / "model-manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        self.manager = launcher.Manager(self.root)
        self.addCleanup(self.manager.job.close)

    def check_hardware(self, free_gib=100, gpu="NVIDIA Test, 24576, 581.08, 8.6", ram_gib=32):
        fake_os = mock.Mock(wraps=os)
        fake_os.name = "nt"
        fake_os.environ = os.environ
        disk = collections.namedtuple("Disk", "total used free")(200 * launcher.GIB, 0, free_gib * launcher.GIB)
        with mock.patch.object(launcher, "os", fake_os), \
             mock.patch.object(launcher.platform, "machine", return_value="AMD64"), \
             mock.patch.object(launcher.shutil, "which", return_value="nvidia-smi.exe"), \
             mock.patch.object(launcher.subprocess, "check_output", return_value=gpu), \
             mock.patch.object(launcher, "system_memory", return_value=ram_gib), \
             mock.patch.object(launcher.shutil, "disk_usage", return_value=disk):
            self.manager.check_hardware()

    def test_free_space_accounts_for_model_and_environment(self):
        self.check_hardware(free_gib=30)
        self.assertEqual(self.manager.snapshot()["required_gib"], 23)

    def test_insufficient_disk_is_rejected_before_install(self):
        with self.assertRaisesRegex(StorageError, "空间"):
            self.check_hardware(free_gib=21)

    def test_ready_environment_uses_smaller_headroom(self):
        self.manager.runtime.mkdir(exist_ok=True)
        (self.manager.runtime / "environment.ready.json").write_text("{}")
        self.check_hardware(free_gib=11)
        self.assertEqual(self.manager.snapshot()["required_gib"], 11)

    def test_existing_download_progress_reduces_remaining_space(self):
        # Mock size metadata instead of allocating a multi-gigabyte fixture.
        dest = self.manager.model_dir / "transformer" / "model.safetensors.part"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"x")
        original_stat = Path.stat

        def simulated_stat(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if path == dest:
                values = list(result)
                values[6] = launcher.GIB
                return os.stat_result(values)
            return result

        with mock.patch.object(Path, "stat", simulated_stat):
            self.check_hardware(free_gib=22)
        self.assertEqual(self.manager.snapshot()["required_gib"], 22)

    def test_gpu_without_native_bf16_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "BF16"):
            self.check_hardware(gpu="NVIDIA Test, 24576, 581.08, 7.5")

    def test_old_driver_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "驱动"):
            self.check_hardware(gpu="NVIDIA Test, 24576, 560.65, 8.6")

    def test_driver_minor_version_is_checked(self):
        with self.assertRaisesRegex(RuntimeError, "驱动"):
            self.check_hardware(gpu="NVIDIA Test, 24576, 570.10, 8.6")

    def test_invalid_download_source_is_rejected(self):
        with self.assertRaises(ValueError):
            self.manager.start_install("https://evil.example")

    def test_duplicate_install_does_not_create_worker(self):
        self.manager.state["busy"] = True
        with mock.patch.object(launcher.threading, "Thread") as worker:
            self.manager.start_install("modelscope")
        worker.assert_not_called()

    def test_stop_marks_cancel_and_kills_both_owned_processes(self):
        process, service = mock.Mock(), mock.Mock()
        self.manager.process, self.manager.service = process, service
        with mock.patch.object(self.manager, "kill") as kill:
            self.manager.stop()
        self.assertTrue(self.manager.cancel.is_set())
        self.assertIsNone(self.manager.service)
        self.assertEqual(kill.call_args_list, [mock.call(process), mock.call(service)])
        self.assertEqual(self.manager.snapshot()["phase"], "idle")

    def test_cancel_arriving_before_process_lock_prevents_spawn(self):
        with mock.patch.object(self.manager.cancel, "is_set", side_effect=[False, True]), \
             mock.patch.object(launcher.subprocess, "Popen") as spawn:
            with self.assertRaises(launcher.Cancelled):
                self.manager.run(["unused-program"])
        spawn.assert_not_called()

    def environment_fixture(self, python_exists):
        self.manager.runtime.mkdir(exist_ok=True)
        lockfile = self.root / "requirements.lock"
        lockfile.write_text("fixture-only", encoding="utf-8")
        ready = self.manager.runtime / "environment.ready.json"
        ready.write_text(json.dumps({"lock_sha256": launcher.sha256(lockfile)}), encoding="utf-8")
        if python_exists:
            python = self.manager.runtime / "environment" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
        self.manager.manifest["files"] = []
        return ready

    def test_ready_marker_with_missing_python_does_not_skip_dependency_install(self):
        self.environment_fixture(python_exists=False)
        with mock.patch.object(self.manager, "check_hardware"), \
             mock.patch.object(self.manager, "run") as run, \
             mock.patch.object(self.manager, "launch_studio"):
            self.manager.install("modelscope")
        commands = [[str(value) for value in call.args[0]] for call in run.call_args_list]
        self.assertTrue(any("venv" in command for command in commands))
        self.assertTrue(any("sync" in command and "--require-hashes" in command for command in commands))

    def test_failed_environment_probe_invalidates_marker_for_retry(self):
        ready = self.environment_fixture(python_exists=True)
        with mock.patch.object(self.manager, "check_hardware"), \
             mock.patch.object(self.manager, "run", side_effect=RuntimeError("fixture probe failure")):
            self.manager.install("modelscope")
        self.assertFalse(ready.exists())
        self.assertTrue((self.manager.runtime / "environment.repair.json").is_file())
        self.assertEqual(self.manager.snapshot()["phase"], "error")


class PathTests(unittest.TestCase):
    def test_manifest_paths_cannot_escape_model_directory(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "models"
            for path in ("../escape", "..", ".", str(Path(name) / "outside")):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    launcher.checked_path(root, path)
            self.assertEqual(launcher.checked_path(root, "vae/config.json"), root / "vae" / "config.json")


class BootstrapTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows PowerShell parser check")
    def test_windows_powershell_can_parse_chinese_bootstrap(self):
        # Parsing executes none of the installer, download, or environment commands.
        script = str(BUNDLE / "app" / "bootstrap.ps1").replace("'", "''")
        command = ("$t=$null; $e=$null; "
                   "[System.Management.Automation.Language.Parser]::ParseFile('" + script +
                   "',[ref]$t,[ref]$e) > $null; $e | ForEach-Object {$_.Message}; exit $e.Count")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command],
                                capture_output=True, timeout=20, creationflags=launcher.CREATE_FLAGS)
        self.assertEqual(result.returncode, 0, result.stdout.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
