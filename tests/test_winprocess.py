"""Real Windows process-lifetime checks; no browser, GPU, or downloads."""
import ctypes
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest


MODULE = Path(__file__).resolve().parents[1] / "app" / "winprocess.py"
spec = importlib.util.spec_from_file_location("moss_winprocess_test_target", MODULE)
winprocess = importlib.util.module_from_spec(spec)
spec.loader.exec_module(winprocess)


@unittest.skipUnless(os.name == "nt", "Windows Job Object integration tests")
class ChildJobTests(unittest.TestCase):
    def job(self):
        job = winprocess.ChildJob()
        self.addCleanup(job.close)
        return job

    def spawn(self, code, **kwargs):
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", code],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
        self.addCleanup(self.reap, process)
        return process

    @staticmethod
    def reap(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if process.stdin:
            process.stdin.close()

    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "x64 structure layout")
    def test_win64_structure_sizes_and_offsets(self):
        self.assertEqual(ctypes.sizeof(winprocess._BasicLimitInformation), 64)
        self.assertEqual(ctypes.sizeof(winprocess._IOCounters), 48)
        self.assertEqual(ctypes.sizeof(winprocess._ExtendedLimitInformation), 144)
        self.assertEqual(winprocess._BasicLimitInformation.LimitFlags.offset, 16)
        self.assertEqual(winprocess._BasicLimitInformation.Affinity.offset, 48)
        self.assertEqual(winprocess._ExtendedLimitInformation.IoInfo.offset, 64)
        self.assertEqual(winprocess._ExtendedLimitInformation.ProcessMemoryLimit.offset, 112)

    def test_close_terminates_assigned_sleeping_child(self):
        job = self.job()
        process = self.spawn("import time; time.sleep(30)")
        job.add(process)
        self.assertIsNone(process.poll())
        job.close()
        self.assertIsNotNone(process.wait(timeout=5))
        job.close()  # Idempotent cleanup must not close a reused handle.

    def test_natural_child_exit_then_close(self):
        job = self.job()
        # The input gate ensures assignment happens before normal process exit.
        process = self.spawn("import sys; sys.stdin.buffer.read(1)", stdin=subprocess.PIPE)
        job.add(process)
        process.communicate(input=b"x", timeout=5)
        self.assertEqual(process.returncode, 0)
        job.close()
        job.close()

    def test_cannot_add_launcher_itself(self):
        job = self.job()
        current = SimpleNamespace(pid=os.getpid(), _handle=-1)
        with self.assertRaisesRegex(ValueError, "launcher itself"):
            job.add(current)

    def test_assignment_error_is_reported(self):
        job = self.job()
        invalid_process = SimpleNamespace(pid=-100, _handle=0)
        with self.assertRaises(OSError) as failure:
            job.add(invalid_process)
        self.assertEqual(failure.exception.winerror, 6)  # ERROR_INVALID_HANDLE

    def test_closed_job_rejects_new_processes(self):
        job = self.job()
        job.close()
        with self.assertRaisesRegex(RuntimeError, "closed child job"):
            job.add(SimpleNamespace(pid=-100, _handle=0))


@unittest.skipIf(os.name == "nt", "Non-Windows import compatibility")
class PortableImportTests(unittest.TestCase):
    def test_non_windows_job_has_no_process_side_effects(self):
        job = winprocess.ChildJob()
        job.add(object())
        job.close()
        job.close()


if __name__ == "__main__":
    unittest.main()
