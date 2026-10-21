"""Desktop/config integration, without installing or downloading a large model."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import launcher


@unittest.skipUnless(os.name == "nt", "Windows local storage paths")
class DesktopBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="MOSS 中文 空间 ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.code = self.base / "software"
        self.code.mkdir()
        (self.code / "model-manifest.json").write_text('{"files": []}', encoding="utf-8")
        self.config = self.base / "settings.json"
        self.settings = dict(schema_version=2, data_dir=str(self.base / "环境缓存"),
                             model_dir=str(self.base / "模型"), output_dir=str(self.base / "作品"),
                             source="huggingface")
        self.config.write_text(json.dumps(self.settings, ensure_ascii=False), encoding="utf-8-sig")
        self.manager = launcher.Manager(self.code, config_path=self.config, desktop=True)
        self.addCleanup(self.manager.job.close)

    def test_all_heavy_paths_follow_settings_instead_of_executable(self):
        self.assertEqual(self.manager.runtime, self.base / "环境缓存" / "runtime")
        self.assertEqual(self.manager.model_dir, self.base / "模型" / "MOSS-Image-SFT")
        self.assertEqual(self.manager.output_dir, self.base / "作品")
        self.assertFalse((self.code / "runtime").exists())
        self.assertEqual(self.manager.state["source"], "huggingface")
        json.dumps(self.manager.snapshot())

    def test_download_library_and_temporary_caches_follow_selected_data(self):
        env = self.manager.environment()
        cache = self.base / "环境缓存" / "cache"
        for key in ("UV_CACHE_DIR", "HF_HOME", "HF_MODULES_CACHE", "TORCH_HOME",
                    "GRADIO_TEMP_DIR", "TEMP", "TMP"):
            with self.subTest(key=key):
                self.assertTrue(Path(env[key]).is_relative_to(cache))
        self.assertTrue(Path(env["TEMP"]).is_dir())

    def test_log_is_written_to_chosen_data_folder(self):
        self.manager.log("中文缓存测试")
        log = self.base / "环境缓存" / "logs" / "launcher.log"
        self.assertIn("中文缓存测试", log.read_text(encoding="utf-8"))
        self.assertFalse((self.code / "logs").exists())

    def test_studio_gets_selected_model_output_and_does_not_open_browser_twice(self):
        process = mock.Mock(stdout=[], pid=12345)
        process.poll.return_value = None
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        with mock.patch.object(launcher.subprocess, "Popen", return_value=process) as spawn, \
             mock.patch.object(self.manager.job, "add"), \
             mock.patch.object(launcher.threading, "Thread"), \
             mock.patch.object(launcher.urllib.request, "urlopen", return_value=response), \
             mock.patch.object(launcher.webbrowser, "open") as browser:
            self.manager.launch_studio("fixture-python")
        args = spawn.call_args.args[0]
        self.assertEqual(args[args.index("--model") + 1], str(self.manager.model_dir))
        self.assertEqual(args[args.index("--output-dir") + 1], str(self.manager.output_dir))
        browser.assert_not_called()
        self.assertEqual(self.manager.state["phase"], "ready")
        self.manager.service = None

    def test_real_first_launch_duplicate_and_shutdown_without_downloads(self):
        # A new empty lock file must be lockable before writing its first byte.
        script = Path(launcher.__file__)
        command = [sys.executable, '-u', str(script), '--config', str(self.config), '--desktop']
        # Match bootstrap.ps1: encoding= below only controls decoding in this test,
        # while the child needs an explicit UTF-8 stdout encoding on Windows.
        child_env = os.environ.copy()
        child_env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding='utf-8', env=child_env,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        messages = queue.Queue()
        def read_output():
            for line in process.stdout:
                messages.put(line)
        threading.Thread(target=read_output, daemon=True).start()
        try:
            line = messages.get(timeout=15)
            self.assertTrue(line.startswith('MOSS_CONTROL:'), line)
            control = json.loads(line.split(':', 1)[1])
            duplicate = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                       env=child_env, timeout=15,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertIn('另一启动器', duplicate.stdout)
            self.assertIsNone(process.poll())
            request = urllib.request.Request(control['url'] + '/api/shutdown', data=b'{}',
                                             headers={'X-Launcher-Token': control['token']})
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(process.wait(timeout=10), 0)
            self.assertFalse((self.manager.runtime / 'launcher-instance.json').exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()


if __name__ == "__main__":
    unittest.main()
