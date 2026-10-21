"""Validated user-selected storage and per-volume space budgets (stdlib only).

load_paths() never creates folders. Call prepare_paths() before installation.
The legacy layout uses runtime as its cache root to retain existing downloads.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path, PureWindowsPath
import shutil
import stat
import tempfile

ROOT = Path(__file__).resolve().parents[1]
GIB = 1024 ** 3
CACHE_FOLDERS = ("temp", "uv-cache", "hf-cache", "hf-modules", "torch", "gradio-temp")


class StorageError(ValueError):
    """A configuration, permission, or storage issue safe to show in the UI."""


@dataclass(frozen=True)
class StoragePaths:
    runtime: Path
    cache: Path
    model: Path
    output: Path
    logs: Path
    data_root: Path
    model_root: Path
    source: str
    _legacy: bool = field(default=False, repr=False, compare=False)

    def to_config(self):
        return dict(schema_version=2, data_dir=str(self.data_root),
                    model_dir=str(self.model_root), output_dir=str(self.output),
                    source=self.source)


def _same_or_child(path, parent):
    """Path comparison respects Windows case folding and directory boundaries."""
    path = os.path.normcase(str(path))
    parent = os.path.normcase(str(parent))
    try:
        return os.path.commonpath((path, parent)) == parent
    except ValueError:
        return False  # Different drives.


def _protected_roots():
    roots = []
    if os.name == "nt":
        for name in ("SystemRoot", "WINDIR", "ProgramFiles", "ProgramFiles(x86)",
                     "ProgramW6432", "ProgramData"):
            if os.environ.get(name):
                roots.append(Path(os.environ[name]).resolve())
        system_drive = os.environ.get("SystemDrive", "C:")
        roots.extend(Path(system_drive + "\\" + item).resolve()
                     for item in ("Windows", "Program Files", "Program Files (x86)", "ProgramData"))
    return roots


def _reject_reparse_ancestors(path, label):
    """Inspect the supplied spelling before resolve() can hide a junction."""
    for current in (path, *path.parents):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise StorageError(f"无法检查{label}的保存位置：{current}") from exc
        if (stat.S_ISLNK(info.st_mode) or
                getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)):
            raise StorageError(f"{label}不能经过符号链接、目录联接或卷挂载点，请选择普通文件夹：{current}")


def _validate_target(value, label):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise StorageError(f"请选择{label}的完整本地文件夹路径。")
    raw = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise StorageError(f"{label}路径含有无效字符。")
    # Device namespaces and network shares are unsuitable for the local runtime.
    if raw.startswith(("\\\\", "//")):
        raise StorageError(f"{label}必须位于本机磁盘，不能使用网络共享或设备路径。")
    path = Path(raw)
    if not path.is_absolute():
        raise StorageError(f"{label}必须使用绝对路径，例如 D:\\MOSS\\data。")
    if os.name == "nt":
        win = PureWindowsPath(raw)
        if not win.drive or not win.root or win.drive.startswith("\\"):
            raise StorageError(f"{label}必须使用完整的本机磁盘路径。")
        for part in win.parts[1:]:
            if part in (".", ".."):
                continue
            if part.endswith((" ", ".")) or any(c in part for c in '<>:"|?*'):
                raise StorageError(f"{label}路径含有 Windows 不支持的文件夹名称。")
            if PureWindowsPath(part).is_reserved():
                raise StorageError(f"{label}不能使用 Windows 保留名称。")
    _reject_reparse_ancestors(path, label)
    try:
        path = path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise StorageError(f"无法解析{label}：{raw}") from exc
    if path == Path(path.anchor):
        raise StorageError(f"{label}不能直接使用磁盘根目录，请选择其中的专用文件夹。")
    for protected in _protected_roots():
        if _same_or_child(path, protected):
            raise StorageError(f"{label}不能位于系统或程序目录：{path}")
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        desktop_home = (Path(local_app) / "MOSS-Image-Desktop").resolve()
        if _same_or_child(path, desktop_home) or _same_or_child(desktop_home, path):
            raise StorageError(f"{label}不能与软件自身目录相同、互相包含或重叠，请选择专用数据文件夹：{path}")
    profiles = [Path.home().resolve()]
    if os.environ.get("USERPROFILE"):
        profiles.append(Path(os.environ["USERPROFILE"]).resolve())
    profiles.extend(p.parent for p in tuple(profiles))
    if any(path == p for p in profiles):
        raise StorageError(f"{label}不能直接使用用户目录，请选择其中的专用文件夹。")
    if path.exists() and not path.is_dir():
        raise StorageError(f"{label}指向了文件，请选择文件夹：{path}")
    # Resolve again during preparation so a changed junction is also validated.
    return path


def _validate_roots(data, model, output):
    roots = [(_validate_target(data, "环境与缓存目录"), "环境与缓存目录"),
             (_validate_target(model, "模型目录"), "模型目录"),
             (_validate_target(output, "作品目录"), "作品目录")]
    for i, (left, left_label) in enumerate(roots):
        for right, right_label in roots[i + 1:]:
            if _same_or_child(left, right) or _same_or_child(right, left):
                raise StorageError(f"{left_label}与{right_label}不能相同或互相包含，请选择三个独立文件夹。")
    return tuple(path for path, _ in roots)


def paths_from_config(config):
    """Validate schema 2 without filesystem changes; useful for settings previews."""
    if not isinstance(config, dict) or config.get("schema_version") != 2:
        raise StorageError("存储设置格式不正确，请在软件中重新选择保存位置。")
    source = config.get("source", "modelscope")
    if source not in ("modelscope", "huggingface"):
        raise StorageError("下载源设置无效，请选择 ModelScope 或 Hugging Face。")
    data, model, output = _validate_roots(config.get("data_dir"), config.get("model_dir"),
                                          config.get("output_dir"))
    paths = StoragePaths(data / "runtime", data / "cache", model / "MOSS-Image-SFT",
                         output, data / "logs", data, model, source)
    _validate_derived(paths)
    return paths


def load_paths(config_path=None, legacy_root=ROOT):
    """Read an explicit config, or retain the portable v0.1 layout when omitted.

    A supplied but missing/invalid config is an error, never an implicit fallback.
    """
    if config_path is None:
        root = Path(legacy_root).resolve()
        return StoragePaths(root / "runtime", root / "runtime", root / "models" / "MOSS-Image-SFT",
                            root / "pictures", root / "logs", root, root / "models", "modelscope", True)
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageError(f"无法读取存储设置：{config_path}。请在软件中重新保存设置。") from exc
    return paths_from_config(config)


def _validate_derived(paths):
    # Existing junctions/symlinks inside selected roots must not redirect writes
    # to a system folder, another selected root, or outside their selected root.
    derived = ((paths.runtime, paths.data_root, "运行环境目录"),
                                (paths.cache, paths.data_root, "缓存目录"),
                                (paths.logs, paths.data_root, "日志目录"),
                                (paths.model, paths.model_root, "模型文件目录"))
    derived += tuple((paths.cache / name, paths.cache, "缓存子目录") for name in CACHE_FOLDERS)
    for target, root, label in derived:
        resolved = _validate_target(target, label)
        if not _same_or_child(resolved, root.resolve()):
            raise StorageError(f"{label}被重定向到了所选文件夹以外，请使用其他文件夹：{target}")
    if not paths._legacy:
        independent = [paths.runtime.resolve(), paths.cache.resolve(), paths.logs.resolve(), paths.model.resolve(), paths.output.resolve()]
        for i, left in enumerate(independent):
            for right in independent[i + 1:]:
                if _same_or_child(left, right) or _same_or_child(right, left):
                    raise StorageError("运行环境、缓存、日志、模型和作品文件夹发生重叠，请重新选择保存位置。")


def _existing_parent(path):
    path = Path(path)
    while not path.exists():
        if path == path.parent:
            raise StorageError(f"找不到目标磁盘，请检查磁盘是否已连接：{path}")
        path = path.parent
    if not path.is_dir():
        raise StorageError(f"保存路径的上级位置是文件，无法创建目录：{path}")
    return path


def _probe_write(folder):
    try:
        # NamedTemporaryFile uses exclusive creation and unpredictable names.
        # Only this file is removed; pre-existing user files are never touched.
        with tempfile.TemporaryFile(prefix=".moss-write-", dir=folder) as probe:
            probe.write(b"MOSS")
            probe.flush()
    except OSError as exc:
        raise StorageError(f"文件夹无法写入，请选择有写入权限的位置：{folder}") from exc


def prepare_paths(paths):
    """Validate all locations before creating anything, then safely probe writes."""
    if not paths._legacy:
        _validate_roots(paths.data_root, paths.model_root, paths.output)
    _validate_derived(paths)
    targets = tuple(dict.fromkeys((paths.runtime, paths.cache, paths.model, paths.output, paths.logs)
                                 + tuple(paths.cache / name for name in CACHE_FOLDERS)))
    for target in targets:
        _validate_target(target, "保存目录")
    ancestors = tuple(dict.fromkeys(_existing_parent(target) for target in targets))
    for folder in ancestors:
        _probe_write(folder)
    for target in targets:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"无法创建保存文件夹：{target}") from exc
        _probe_write(target)
    return paths


def _volume_mount(path):
    existing = _existing_parent(path)
    if os.name == "nt":
        buffer = ctypes.create_unicode_buffer(32768)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        func = kernel.GetVolumePathNameW
        func.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
        func.restype = ctypes.c_int
        if not func(str(existing), buffer, len(buffer)):
            raise StorageError(f"无法识别保存目录所在磁盘：{path}（错误 {ctypes.get_last_error()}）")
        return buffer.value
    current = existing
    device = current.stat().st_dev
    while current != current.parent and current.parent.stat().st_dev == device:
        current = current.parent
    return str(current)


def _volume_key(path):
    mount = _volume_mount(path)
    if os.name == "nt":
        # Multiple drive letters or directory mount points can refer to one
        # volume. The GUID aggregates their budgets instead of counting twice.
        buffer = ctypes.create_unicode_buffer(32768)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        func = kernel.GetVolumeNameForVolumeMountPointW
        func.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
        func.restype = ctypes.c_int
        if func(mount, buffer, len(buffer)):
            return buffer.value.casefold()
        # Some filesystems do not expose a volume GUID; retain the exact mount
        # point from GetVolumePathNameW rather than guessing from a drive letter.
        return os.path.normcase(mount)
    return str(Path(mount).stat().st_dev)


def plan_space(paths, remaining_model_bytes, environment_ready, *, disk_usage=None, volume_key=None):
    """Return JSON-friendly reports aggregated by actual destination volume.

    Model: remaining bytes + 2 GiB. Environment/cache: 18 GiB initially, 6 GiB
    when ready. Pictures: 1 GiB. These are conservative operating budgets, not
    guarantees of a particular model's peak memory/disk use.
    Injection hooks take a Path; disk_usage returns a shutil.disk_usage result.
    """
    if isinstance(remaining_model_bytes, bool) or not isinstance(remaining_model_bytes, int) or remaining_model_bytes < 0:
        raise StorageError("待下载模型大小无效。")
    usage_fn = disk_usage or shutil.disk_usage
    key_fn = volume_key or _volume_key
    components = [("运行环境", paths.runtime, (4 if environment_ready else 12) * GIB),
                  ("缓存", paths.cache, (2 if environment_ready else 6) * GIB),
                  ("模型", paths.model, remaining_model_bytes + 2 * GIB),
                  ("作品", paths.output, GIB)]
    volumes = {}
    for label, target, required in components:
        try:
            key = str(key_fn(target))
            if key not in volumes:
                usage = usage_fn(_existing_parent(target))
                volumes[key] = dict(volume=str(key) if volume_key else _volume_mount(target),
                                    volume_id=key, paths=[], items=[], required_bytes=0,
                                    free_bytes=int(usage.free), total_bytes=int(usage.total))
        except StorageError:
            raise
        except (OSError, ValueError) as exc:
            raise StorageError(f"无法读取目标磁盘的可用空间：{target}") from exc
        report = volumes[key]
        if str(target) not in report["paths"]:
            report["paths"].append(str(target))
        report["items"].append(dict(label=label, path=str(target), required_bytes=required))
        report["required_bytes"] += required
    reports = list(volumes.values())
    for report in reports:
        report["sufficient"] = report["free_bytes"] >= report["required_bytes"]
        report["required_gib"] = math.ceil(report["required_bytes"] / GIB)
        report["free_gib"] = round(report["free_bytes"] / GIB, 1)
    return reports


def check_space(paths, remaining_model_bytes, environment_ready, *, disk_usage=None, volume_key=None):
    reports = plan_space(paths, remaining_model_bytes, environment_ready,
                         disk_usage=disk_usage, volume_key=volume_key)
    failures = []
    for report in reports:
        if not report["sufficient"]:
            locations = "、".join(report["paths"])
            failures.append(f"磁盘 {report['volume']} 空间不足：需约 {report['required_gib']} GiB，"
                            f"可用 {report['free_gib']:.1f} GiB。对应目录：{locations}")
    if failures:
        raise StorageError("\n".join(failures) + "。请在设置中更换保存位置，或清理此磁盘后重试。")
    return reports
