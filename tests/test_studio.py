"""Lightweight boundary tests: no GPU, model weights, Torch, or Gradio needed."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

MODULE = Path(__file__).resolve().parents[1] / "app" / "studio.py"
spec = importlib.util.spec_from_file_location("moss_studio_test_target", MODULE)
studio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(studio)


class ConfigDouble:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def validate(self):
        pass


class RunnerDouble:
    def __init__(self, output_dir, failure=None):
        self.output_dir = output_dir
        self.failure = failure
        self.calls = []
        self.active = 0
        self.peak_active = 0
        self.lock = threading.Lock()

    def generate(self, model, config, progress=None):
        self.calls.append((model, config))
        if self.failure:
            raise self.failure
        with self.lock:
            self.active += 1
            self.peak_active = max(self.active, self.peak_active)
            call_number = len(self.calls)
        time.sleep(0.01)
        folder = self.output_dir / str(call_number)
        folder.mkdir()
        picture = folder / "01.png"
        picture.write_bytes(b"test fixture only")
        params = folder / "parameters.json"
        params.write_text(json.dumps({"parameters": {"seed": 98765, "prompt": config.prompt},
                                     "elapsed_seconds": 1.25}, ensure_ascii=False), encoding="utf-8")
        if progress:
            progress(1, desc="Complete")
        with self.lock:
            self.active -= 1
        return [str(picture)], str(params), "upstream status"


class StudioTests(unittest.TestCase):
    def config(self, **overrides):
        args = dict(prompt="窗边的橘猫 🐈", shape="square", steps=50, cfg=3, seed=-1,
                    memory="sequential", text_length=512, negative="")
        args.update(overrides)
        return studio.make_config(ConfigDouble, **args)

    def test_low_memory_defaults_and_dimensions(self):
        config = self.config()
        self.assertEqual((config.width, config.height, config.max_sequence_length), (512, 512, 512))
        self.assertEqual(config.offload, "sequential")
        self.assertFalse(config.compile_transformer)
        self.assertEqual(config.num_images, 1)
        self.assertEqual((self.config(shape="landscape").width, self.config(shape="portrait").height), (640, 640))

    def test_invalid_inputs_rejected_before_generation(self):
        for overrides in [{"prompt": "  "}, {"steps": 50.5}, {"steps": 0}, {"seed": 2**53},
                          {"seed": float("nan")}, {"cfg": float("inf")}, {"memory": "auto"},
                          {"shape": "custom"}, {"text_length": 513}, {"steps": True}]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.config(**overrides)

    def test_actual_seed_unicode_and_downloads_are_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = RunnerDouble(root)
            controller = studio.StudioController(root, root, ConfigDouble, runner)
            events = []
            image, downloads, status = controller.generate("窗边的橘猫 🐈", "square", 50, 3, -1,
                                                          "sequential", 512, "", lambda *a, **kw: events.append(kw))
            self.assertTrue(Path(image).is_file())
            self.assertEqual(len(downloads), 3)
            self.assertIn("98765", status)
            self.assertIn("窗边的橘猫 🐈", Path(downloads[-1]).read_text(encoding="utf-8"))
            self.assertIn("生成完成", events[0]["desc"])
            self.assertEqual(runner.calls[0][1].seed, -1)

    def test_failed_generation_clears_old_result_and_explains_oom(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            controller = studio.StudioController(root, root, ConfigDouble,
                                                RunnerDouble(root, ValueError("GPU memory exhausted.")))
            with self.assertLogs("moss_studio", level="ERROR"):
                result = controller.generate("cat", "square", 50, 3, -1, "sequential", 512, "")
            self.assertIsNone(result[0])
            self.assertEqual(result[1], [])
            self.assertIn("显存", result[2])
            self.assertIn("512", result[2])

    def test_requests_are_serialized(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = RunnerDouble(root)
            controller = studio.StudioController(root, root, ConfigDouble, runner)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: controller.generate("cat", "square", 1, 3, -1,
                                                                      "sequential", 512, ""), range(4)))
            self.assertEqual(runner.peak_active, 1)
            self.assertTrue(all(result[0] for result in results))

    def test_progress_translation(self):
        events = []
        report = studio.chinese_progress(lambda *a, **kw: events.append(kw))
        report((2, 50), desc="Sampling · step 2/50", unit="steps")
        self.assertEqual(events[0]["desc"], "正在生成 · 第 2/50 步")
        self.assertEqual(events[0]["unit"], "步")

    def test_windows_error_guidance(self):
        self.assertIn("Visual C++", studio.explain_error(OSError("DLL load failed [WinError 126]")))
        self.assertIn("NVIDIA", studio.explain_error(ValueError("A CUDA GPU is required for generation.")))
        self.assertIn("重新运行安装", studio.explain_error(FileNotFoundError("weights missing")))
        self.assertIn("写入权限", studio.explain_error(PermissionError("access denied")))

    def test_cli_only_accepts_local_defaults_and_safe_port(self):
        args = studio.parse_args([])
        self.assertEqual(args.memory, "sequential")
        self.assertEqual(args.model.name, "MOSS-Image-SFT")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            studio.parse_args(["--host", "0.0.0.0"])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            studio.parse_args(["--port", "99999"])


if __name__ == "__main__":
    unittest.main()
