"""Local, standard-library installer. No global Python or administrator access required."""
from __future__ import annotations

import collections
import argparse
import ctypes
import hashlib
import http.server
import json
import math
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from winprocess import ChildJob
from storage import load_paths, prepare_paths, check_space

ROOT = Path(__file__).resolve().parents[1]
GIB = 1024 ** 3
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def checked_path(root, relative):
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()) or target == root.resolve():
        raise ValueError("无效文件路径。")
    return target


class Cancelled(Exception):
    pass


def fetch_file(url, dest, size, digest, cancelled, report):
    """Resume only exact byte ranges; promote a file only after size AND SHA256 validation."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size == size and sha256(dest) == digest:
        report(f"已校验：{dest.name}")
        return
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, 4):
        if cancelled.is_set():
            raise Cancelled()
        try:
            offset = part.stat().st_size if part.exists() else 0
            if offset == size and sha256(part) == digest:
                part.replace(dest)
                return
            if offset >= size:
                part.unlink()
                offset = 0
            headers = {"User-Agent": "MOSS-Image-OneClick/0.1", "Accept-Encoding": "identity"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=45) as response:
                if response.status == 206:
                    expected_range = f"bytes {offset}-"
                    if not response.headers.get("Content-Range", "").startswith(expected_range):
                        raise ValueError("下载服务器返回了错误的续传位置。")
                elif response.status == 200:
                    offset = 0  # Server ignores Range: restart rather than append corrupt bytes.
                else:
                    raise ValueError(f"下载服务器状态异常：{response.status}")
                received, last = offset, 0.0
                with part.open("ab" if offset else "wb") as out:
                    while True:
                        if cancelled.is_set():
                            raise Cancelled()
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        out.write(block)
                        received += len(block)
                        if received > size:
                            raise ValueError("下载文件大于清单记录的大小。")
                        now = time.monotonic()
                        if now - last >= 3:
                            report(f"{dest.name}：{received / 1024**2:.1f} / {size / 1024**2:.1f} MB")
                            last = now
            if part.stat().st_size != size:
                raise ValueError("下载中断，已保留进度。")
            report(f"正在校验：{dest.name}")
            if sha256(part) != digest:
                part.unlink()
                raise ValueError("文件校验失败，已丢弃损坏的下载。")
            part.replace(dest)
            return
        except Cancelled:
            raise
        except (OSError, ValueError) as exc:
            report(f"下载暂未成功（{attempt}/3）：{type(exc).__name__}。")
            if attempt == 3:
                raise RuntimeError(f"下载 {dest.name} 失败。请检查网络，或切换下载源后重试；已下载的数据会保留。") from exc
            if cancelled.wait(attempt * 2):
                raise Cancelled()


def system_memory():
    class Memory(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in
            ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]
    memory = Memory()
    memory.length = ctypes.sizeof(memory)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise OSError("无法读取内存信息。")
    return memory.total / GIB


class Manager:
    def __init__(self, root=ROOT, config_path=None, desktop=False):
        self.root = root
        self.paths = load_paths(config_path, legacy_root=root)
        prepare_paths(self.paths)
        self.desktop = desktop
        self.runtime = self.paths.runtime
        self.model_dir = self.paths.model
        self.output_dir = self.paths.output
        self.log_dir = self.paths.logs
        self.cache = self.paths.cache
        self.manifest = json.loads((root / "model-manifest.json").read_text(encoding="utf-8"))
        self.model_bytes = sum(f["size"] for f in self.manifest["files"])
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.process = None
        self.service = None
        self.job = ChildJob()
        self.logs = collections.deque(maxlen=90)
        self.state = dict(phase="idle", message="点击“安装并启动”，自动完成环境和模型准备。", busy=False,
                          hardware=None, app_url=None, model_gib=round(self.model_bytes / GIB, 1),
                          required_gib=math.ceil(self.model_bytes / GIB + 21), source=self.paths.source,
                          storage=dict(data_dir=str(self.paths.data_root), model_dir=str(self.paths.model_root),
                                       output_dir=str(self.output_dir), volumes=[]))

    def log(self, line):
        line = str(line).rstrip()
        if not line:
            return
        with self.lock:
            self.logs.append(line[-2000:])
            with (self.log_dir / "launcher.log").open("a", encoding="utf-8") as f:
                f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + line + "\n")

    def update(self, phase, message, **kwargs):
        with self.lock:
            self.state.update(phase=phase, message=message, **kwargs)
        self.log(message)

    def snapshot(self):
        with self.lock:
            return dict(self.state, logs=list(self.logs))

    def environment(self):
        env = os.environ.copy()
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", UV_HTTP_TIMEOUT="60", UV_HTTP_RETRIES="3",
                   UV_CACHE_DIR=str(self.cache / "uv-cache"), UV_LINK_MODE="copy", UV_NO_CONFIG="1",
                   UV_PYTHON_INSTALL_DIR=str(self.runtime / "python"), UV_PYTHON_BIN_DIR=str(self.runtime / "bin"),
                   HF_HOME=str(self.cache / "hf-cache"), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                   HF_MODULES_CACHE=str(self.cache / "hf-modules"), TORCH_HOME=str(self.cache / "torch"),
                   TEMP=str(self.cache / "temp"), TMP=str(self.cache / "temp"),
                   GRADIO_ANALYTICS_ENABLED="False", GRADIO_TEMP_DIR=str(self.cache / "gradio-temp"),
                   CUDA_VISIBLE_DEVICES="0")
        return env

    def run(self, command):
        if self.cancel.is_set():
            raise Cancelled()
        with self.lock:
            if self.cancel.is_set():
                raise Cancelled()
            self.process = subprocess.Popen([str(x) for x in command], cwd=self.root, env=self.environment(),
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                            encoding="utf-8", errors="replace", creationflags=CREATE_FLAGS)
            process = self.process
            try:
                self.job.add(process)
            except Exception:
                self.kill(process)
                raise
        pulse_stop = threading.Event()
        started = time.monotonic()
        def pulse():
            while not pulse_stop.wait(30):
                if process.poll() is not None:
                    return
                self.log(f"环境任务仍在运行，已等待 {int(time.monotonic() - started)} 秒。大型运行库下载或解压期间输出可能较少。")
        threading.Thread(target=pulse, daemon=True).start()
        for line in process.stdout:
            self.log(line)
        code = process.wait()
        pulse_stop.set()
        with self.lock:
            if self.process is process:
                self.process = None
        if self.cancel.is_set():
            raise Cancelled()
        if code:
            raise RuntimeError(f"安装或检查未完成（退出代码 {code}）。请查看下方日志。网络失败可重试；如日志出现 DLL / WinError 126，请安装微软 VC++ x64 运行库。")

    def check_hardware(self):
        if os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64"):
            raise RuntimeError("本包支持 64 位 Windows 10/11（Intel / AMD）。")
        smi = shutil.which("nvidia-smi")
        if not smi:
            candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
            if candidate.is_file():
                smi = str(candidate)
        if not smi:
            raise RuntimeError("未检测到 NVIDIA 驱动。请安装 NVIDIA 官方驱动，重启电脑后再试。")
        output = subprocess.check_output([smi, "--query-gpu=name,memory.total,driver_version,compute_cap", "--format=csv,noheader,nounits"],
                                         timeout=20, text=True, creationflags=CREATE_FLAGS)
        fields = [s.strip() for s in output.strip().splitlines()[0].split(",")]
        gpu, mb, driver, capability = fields
        hardware = dict(gpu=gpu, vram_gib=round(float(mb)/1024, 1), ram_gib=round(system_memory(), 1),
                        free_gib=round(shutil.disk_usage(self.paths.data_root).free / GIB, 1), driver=driver)
        with self.lock:
            self.state["hardware"] = hardware
        if float(capability) < 8:
            raise RuntimeError("此显卡架构较旧。本包使用 BF16 模型，要求 NVIDIA Ampere 或更新架构（例如 RTX 30/40/50 系列）。")
        if tuple(int(part) for part in driver.split(".")[:2]) < (570, 65):
            raise RuntimeError("当前驱动较旧。本包保守要求 NVIDIA 570.65 或更新驱动，请更新后重试。")
        if hardware["ram_gib"] < 15:
            raise RuntimeError("本软件要求至少 16GB 系统内存，建议 32GB 或更多。")
        if hardware["vram_gib"] < 15:
            self.log("低显存提示：8–12GB 仅实验性尝试，默认逐层卸载 + 512 像素；速度较慢，仍可能内存不足。")
        already = 0
        for item in self.manifest["files"]:
            dest = checked_path(self.model_dir, item["path"])
            part = dest.with_name(dest.name + ".part")
            if dest.is_file() and dest.stat().st_size == item["size"]:
                already += item["size"]
            elif part.is_file():
                already += min(part.stat().st_size, item["size"])
        volumes = check_space(self.paths, self.model_bytes - already,
                              (self.runtime / "environment.ready.json").exists())
        with self.lock:
            self.state["required_gib"] = math.ceil(sum(v["required_bytes"] for v in volumes) / GIB)
            self.state["storage"]["volumes"] = volumes

    def start_install(self, source):
        if source not in ("huggingface", "modelscope"):
            raise ValueError("请选择有效下载源。")
        with self.lock:
            if self.state["busy"] or (self.service and self.service.poll() is None):
                return
            self.state.update(busy=True, source=source, app_url=None)
            self.cancel.clear()
        threading.Thread(target=self.install, args=(source,), daemon=True).start()

    def install(self, source):
        try:
            self.update("checking", "正在检查显卡、内存和可用空间…")
            self.check_hardware()
            uv = self.runtime / "uv" / "uv-0.13.1.data" / "scripts" / "uv.exe"
            python = self.runtime / "environment" / "Scripts" / "python.exe"
            lockfile = self.root / "requirements.lock"
            fingerprint = sha256(lockfile)
            ready = self.runtime / "environment.ready.json"
            repair = self.runtime / "environment.repair.json"
            self.update("environment", "正在准备生成环境，首次下载可能耗时较长…")
            had_python = python.is_file()
            self.run([uv, "venv", "--allow-existing", "--relocatable", "--python", sys.executable, self.runtime / "environment"])
            valid = False
            try:
                valid = had_python and json.loads(ready.read_text(encoding="utf-8"))["lock_sha256"] == fingerprint
            except (OSError, ValueError, KeyError):
                pass
            if not valid:
                command = [uv, "pip", "sync", "--python", python, "--require-hashes", "--only-binary", ":all:",
                           "--torch-backend", "cu128", lockfile]
                if repair.exists():
                    command.append("--reinstall")
                self.run(command)
            self.run([python, "-X", "utf8", self.root / "app" / "probe.py"])
            atomic_json(ready, {"lock_sha256": fingerprint})
            repair.unlink(missing_ok=True)
            self.update("model", "正在下载 / 校验模型。中断后可点击重试，已完成文件会复用。")
            for index, item in enumerate(self.manifest["files"], 1):
                self.log(f"文件 {index}/{len(self.manifest['files'])}：{item['path']}")
                fetch_file(item["urls"][source], checked_path(self.model_dir, item["path"]),
                           item["size"], item["sha256"], self.cancel, self.log)
            if self.cancel.is_set():
                raise Cancelled()
            self.update("starting", "正在打开中文创作界面；首次生成时还需要加载模型。")
            self.launch_studio(python)
        except Cancelled:
            self.update("idle", "已停止。已下载的文件会保留，下次点击可继续。", app_url=None)
        except Exception as exc:
            if self.state["phase"] == "environment":
                (self.runtime / "environment.ready.json").unlink(missing_ok=True)
                atomic_json(self.runtime / "environment.repair.json", {"retry_reinstall": True})
            self.update("error", str(exc), app_url=None)
            self.log(f"错误类型：{type(exc).__name__}")
        finally:
            with self.lock:
                self.state["busy"] = False

    def launch_studio(self, python):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        command = [str(python), "-X", "utf8", str(self.root / "app" / "studio.py"),
                   "--model", str(self.model_dir), "--output-dir", str(self.output_dir),
                   "--port", str(port), "--memory", "sequential"]
        with self.lock:
            if self.cancel.is_set():
                raise Cancelled()
            self.service = subprocess.Popen(command, cwd=self.root, env=self.environment(), stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                            creationflags=CREATE_FLAGS)
            service = self.service
            try:
                self.job.add(service)
            except Exception:
                self.kill(service)
                raise
        def watch():
            for line in service.stdout:
                self.log(line)
            code = service.wait()
            with self.lock:
                if self.service is service and self.state["phase"] == "ready":
                    self.update("error", f"创作服务已退出（代码 {code}）。可点击重试。", app_url=None)
        threading.Thread(target=watch, daemon=True).start()
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.cancel.is_set():
                self.kill(service)
                raise Cancelled()
            if service.poll() is not None:
                raise RuntimeError("创作界面未能启动，请查看日志。")
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        with self.lock:
                            if self.cancel.is_set() or self.service is not service:
                                raise Cancelled()
                            self.update("ready", "创作界面已准备好。首次生成时将加载模型，请耐心等待。", app_url=url)
                            if not self.desktop:
                                webbrowser.open(url)
                        return
            except (OSError, urllib.error.URLError):
                self.cancel.wait(1)
        self.kill(service)
        raise RuntimeError("创作界面启动超时，请查看日志后重试。")

    @staticmethod
    def kill(process):
        if process and process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=CREATE_FLAGS, timeout=20)
            else:
                process.terminate()

    def stop(self):
        self.cancel.set()
        with self.lock:
            process, service = self.process, self.service
            self.service = None
        self.kill(process)
        self.kill(service)
        with self.lock:
            if not self.state["busy"]:
                self.update("idle", "服务已停止。可以再次安装并启动。", app_url=None)


def create_server(manager, token, port=0):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, code, payload, kind="application/json; charset=utf-8"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            expected = f"127.0.0.1:{self.server.server_port}"
            origin = self.headers.get("Origin")
            return (self.headers.get("Host") == expected and
                    (not origin or origin == "http://" + expected) and
                    secrets.compare_digest(self.headers.get("X-Launcher-Token", ""), token))

        def do_GET(self):
            if self.path == "/":
                if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                    return self.respond(403, {"error": "禁止访问"})
                return self.respond(200, (manager.root / "app" / "installer.html").read_bytes(), "text/html; charset=utf-8")
            if not self.authorized():
                return self.respond(403, {"error": "请通过 Start.cmd 打开控制页。"})
            if self.path == "/api/status":
                return self.respond(200, manager.snapshot())
            self.respond(404, {"error": "未找到"})

        def do_POST(self):
            if not self.authorized():
                return self.respond(403, {"error": "禁止访问"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 4096:
                    raise ValueError("请求过大。")
                data = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/install":
                    manager.start_install(data.get("source", "modelscope"))
                elif self.path == "/api/stop":
                    manager.stop()
                elif self.path == "/api/open-output":
                    os.startfile(manager.output_dir)
                elif self.path == "/api/shutdown":
                    self.respond(200, {"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                else:
                    return self.respond(404, {"error": "未找到"})
                self.respond(200, {"ok": True})
            except (ValueError, OSError, TypeError, AttributeError) as exc:
                self.respond(400, {"error": str(exc)})
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--desktop", action="store_true")
    args = parser.parse_args()
    manager = Manager(config_path=args.config, desktop=args.desktop)
    manager.runtime.mkdir(exist_ok=True)
    instance = manager.runtime / "launcher-instance.json"
    import msvcrt
    instance_lock = (manager.runtime / "launcher.lock").open("a+b")
    instance_lock.seek(0)
    try:
        msvcrt.locking(instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
        owns_lock = True
        # Windows allows locking a range beyond EOF. Initialize only after
        # owning that range: reading the locked byte from a second process fails.
        if os.fstat(instance_lock.fileno()).st_size == 0:
            instance_lock.write(b"1")
            instance_lock.flush()
    except OSError:
        owns_lock = False
        # A first launch may still be publishing its local control-page address.
        for _ in range(20):
            if instance.exists():
                break
            time.sleep(0.1)
    try:
        old = json.loads(instance.read_text(encoding="utf-8"))
        request = urllib.request.Request(old["url"] + "/api/status", headers={"X-Launcher-Token": old["token"]})
        with urllib.request.urlopen(request, timeout=2) as response:
            if response.status == 200:
                if args.desktop:
                    raise RuntimeError("所选环境目录正由另一启动器使用。请先关闭该启动器，再重试。")
                webbrowser.open(old["url"] + "/#" + old["token"])
                return
    except (OSError, ValueError, KeyError):
        pass
    if not owns_lock:
        print("另一个启动器正在运行。请稍等片刻再双击，或返回已有的控制页面。")
        return
    token = secrets.token_urlsafe(32)
    server = create_server(manager, token)
    url = f"http://127.0.0.1:{server.server_port}"
    atomic_json(instance, {"url": url, "token": token, "pid": os.getpid()})
    if args.desktop:
        print("MOSS_CONTROL:" + json.dumps({"url": url, "token": token}), flush=True)
    else:
        print("中文控制页：" + url + "/#" + token, flush=True)
        print("请保留此窗口。停止服务可在控制页操作；退出启动器请按 Ctrl+C。", flush=True)
        webbrowser.open(url + "/#" + token)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        manager.stop()
        manager.job.close()
        server.server_close()
        try:
            if json.loads(instance.read_text(encoding="utf-8"))["token"] == token:
                instance.unlink()
        except (OSError, ValueError, KeyError):
            pass
        instance_lock.seek(0)
        msvcrt.locking(instance_lock.fileno(), msvcrt.LK_UNLCK, 1)
        instance_lock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # PowerShell 5.1 can stop at the first native stderr line. Keep the
        # actionable startup reason on stdout instead of a truncated traceback.
        print("启动失败：" + str(exc), flush=True)
        sys.exit(1)
