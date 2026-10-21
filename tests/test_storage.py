"""Storage validation and destination-volume budgeting; no model downloads."""
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="moss-storage-")
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def config(self, **changes):
        config = dict(schema_version=2, data_dir=str(self.root / "中文 空间" / "环境"),
                      model_dir=str(self.root / "中文 空间" / "模型"),
                      output_dir=str(self.root / "中文 空间" / "作品"), source="modelscope")
        config.update(changes)
        return config

    def paths(self):
        return storage.paths_from_config(self.config())

    def test_load_legacy_retains_layout_and_does_not_create(self):
        root = self.root / "portable"
        paths = storage.load_paths(legacy_root=root)
        self.assertEqual(paths.runtime, root / "runtime")
        self.assertEqual(paths.cache, root / "runtime")
        self.assertEqual(paths.model, root / "models" / "MOSS-Image-SFT")
        self.assertEqual(paths.output, root / "pictures")
        self.assertEqual(paths.logs, root / "logs")
        self.assertFalse(root.exists())

    def test_json_bom_chinese_and_spaces(self):
        filename = self.root / "设置.json"
        config = self.config(source="huggingface")
        filename.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8-sig")
        paths = storage.load_paths(filename)
        self.assertEqual(paths.to_config(), config)
        self.assertEqual(paths.cache, paths.data_root / "cache")
        self.assertFalse(paths.data_root.exists())

    def test_explicit_missing_config_does_not_fall_back(self):
        with self.assertRaisesRegex(storage.StorageError, "无法读取"):
            storage.load_paths(self.root / "missing.json")

    def test_malformed_config(self):
        file = self.root / "broken.json"
        file.write_text("{invalid", encoding="utf-8")
        with self.assertRaises(storage.StorageError):
            storage.load_paths(file)

    def test_unknown_schema_and_source(self):
        for changes in ({"schema_version": 1}, {"source": "unknown"}):
            with self.subTest(changes=changes), self.assertRaises(storage.StorageError):
                storage.paths_from_config(self.config(**changes))

    def test_relative_drive_relative_unc_device_and_root_rejected(self):
        invalid = ["relative", r"C:relative", r"\\server\share\model", r"\\?\C:\model",
                   str(Path(self.root.anchor)), "", "bad\x00name"]
        if os.name == "nt":
            invalid.append(r"\rooted-without-drive")
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(storage.StorageError):
                storage.paths_from_config(self.config(data_dir=value))

    def test_profile_root_and_system_children_rejected(self):
        values = [str(Path.home())]
        if os.name == "nt":
            values.extend([str(Path(os.environ["SystemRoot"]) / "MOSS-data"),
                           str(Path(os.environ["ProgramFiles"]) / "MOSS-data")])
        for value in values:
            with self.subTest(value=value), self.assertRaises(storage.StorageError):
                storage.paths_from_config(self.config(data_dir=value))

    def test_control_characters_rejected_before_filesystem_access(self):
        for char in ("\r", "\n", "\t", "\x00", "\x1f", "\x7f"):
            with self.subTest(char=repr(char)), mock.patch.object(Path, "lstat") as probe:
                with self.assertRaisesRegex(storage.StorageError, "无效字符"):
                    storage.paths_from_config(self.config(data_dir=str(self.root / ("bad" + char + "name"))))
                probe.assert_not_called()

    def test_software_home_ancestors_and_descendants_rejected(self):
        appdata = self.root / "Local AppData"
        desktop_home = appdata / "MOSS-Image-Desktop"
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(appdata)}):
            for target in (appdata, desktop_home, desktop_home / "runtime"):
                with self.subTest(target=target), self.assertRaisesRegex(storage.StorageError, "软件自身目录"):
                    storage.paths_from_config(self.config(data_dir=str(target)))
            paths = storage.paths_from_config(self.config(data_dir=str(appdata / "MOSS-Image-Data")))
            self.assertEqual(paths.data_root, appdata / "MOSS-Image-Data")

    @unittest.skipUnless(os.name == "nt", "Windows names")
    def test_windows_reserved_names_and_trailing_space(self):
        for name in ("CON", "nul.txt", "trailing ", "trailing.", "has:stream", "has*wildcard"):
            with self.subTest(name=name), self.assertRaises(storage.StorageError):
                storage.paths_from_config(self.config(data_dir=str(self.root / name)))

    def test_equal_nested_and_reverse_nested_roots_rejected(self):
        data = Path(self.config()["data_dir"])
        for model in (data, data / "child", data.parent):
            with self.subTest(model=model), self.assertRaisesRegex(storage.StorageError, "互相包含"):
                storage.paths_from_config(self.config(model_dir=str(model)))
        model = Path(self.config()["model_dir"])
        with self.assertRaises(storage.StorageError):
            storage.paths_from_config(self.config(output_dir=str(model / "pictures")))

    @unittest.skipUnless(os.name == "nt", "Windows case folding")
    def test_case_insensitive_overlap_rejected(self):
        data = self.config()["data_dir"]
        with self.assertRaises(storage.StorageError):
            storage.paths_from_config(self.config(model_dir=data.upper()))

    def test_sibling_prefixes_are_valid(self):
        paths = storage.paths_from_config(self.config(data_dir=str(self.root / "moss"),
                    model_dir=str(self.root / "moss-models"), output_dir=str(self.root / "moss-pictures")))
        self.assertEqual(paths.model_root.name, "moss-models")

    def test_existing_file_rejected(self):
        file = self.root / "ordinary-file"
        file.write_text("preserve me", encoding="utf-8")
        with self.assertRaisesRegex(storage.StorageError, "指向了文件"):
            storage.paths_from_config(self.config(output_dir=str(file)))
        self.assertEqual(file.read_text(encoding="utf-8"), "preserve me")

    def test_prepare_creates_cache_temp_and_preserves_user_files(self):
        paths = self.paths()
        paths.output.mkdir(parents=True)
        existing = paths.output / "我的作品.txt"
        existing.write_text("existing work", encoding="utf-8")
        storage.prepare_paths(paths)
        self.assertTrue((paths.cache / "temp").is_dir())
        self.assertTrue((paths.cache / "uv-cache").is_dir())
        self.assertTrue(paths.runtime.is_dir())
        self.assertTrue(paths.model.is_dir())
        self.assertEqual(existing.read_text(encoding="utf-8"), "existing work")
        self.assertEqual(list(paths.output.iterdir()), [existing])
        self.assertFalse(list(self.root.rglob(".moss-write-*")))

    def test_prepare_legacy_layout(self):
        paths = storage.load_paths(legacy_root=self.root / "portable")
        storage.prepare_paths(paths)
        self.assertTrue(paths.runtime.is_dir())
        self.assertTrue((paths.runtime / "temp").is_dir())

    def test_failed_write_probe_precedes_all_directory_creation(self):
        paths = self.paths()
        with mock.patch.object(storage.tempfile, "TemporaryFile", side_effect=PermissionError("no write")):
            with self.assertRaisesRegex(storage.StorageError, "无法写入"):
                storage.prepare_paths(paths)
        self.assertFalse(paths.data_root.exists())
        self.assertFalse(paths.model_root.exists())
        self.assertFalse(paths.output.exists())

    def test_invalid_existing_child_detected_before_creation(self):
        paths = self.paths()
        paths.data_root.mkdir(parents=True)
        paths.cache.write_text("file blocks cache", encoding="utf-8")
        with self.assertRaises(storage.StorageError):
            storage.prepare_paths(paths)
        self.assertFalse(paths.runtime.exists())
        self.assertFalse(paths.model_root.exists())

    def test_redirected_subfolder_outside_selection_rejected(self):
        paths = self.paths()
        paths.data_root.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        try:
            paths.cache.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks is unavailable for this account")
        with self.assertRaisesRegex(storage.StorageError, "符号链接"):
            storage.prepare_paths(paths)
        self.assertFalse(paths.runtime.exists())

    def test_selected_symlink_root_and_ancestor_rejected_before_resolve(self):
        ordinary = self.root / "ordinary"
        ordinary.mkdir()
        alias = self.root / "alias"
        try:
            alias.symlink_to(ordinary, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks is unavailable for this account")
        for target in (alias, alias / "not-created-yet"):
            with self.subTest(target=target), mock.patch.object(Path, "resolve") as resolve:
                with self.assertRaisesRegex(storage.StorageError, "符号链接"):
                    storage.paths_from_config(self.config(data_dir=str(target)))
                resolve.assert_not_called()
        self.assertFalse((ordinary / "not-created-yet").exists())

    def test_windows_reparse_attribute_rejected_even_without_symlink_mode(self):
        target = self.root / "mountpoint"
        original_lstat = Path.lstat
        def fake_lstat(path):
            if path == target:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=1024)
            return original_lstat(path)
        with mock.patch.object(Path, "lstat", fake_lstat), mock.patch.object(Path, "resolve") as resolve:
            with self.assertRaisesRegex(storage.StorageError, "卷挂载点"):
                storage.paths_from_config(self.config(data_dir=str(target / "future-child")))
            resolve.assert_not_called()

    def usage(self, free=100):
        return SimpleNamespace(total=200 * storage.GIB, used=(200 - free) * storage.GIB, free=free * storage.GIB)

    def test_same_volume_adds_all_budgets_initial(self):
        paths = self.paths()
        report = storage.plan_space(paths, 25 * storage.GIB, False,
                    disk_usage=lambda p: self.usage(), volume_key=lambda p: "D:\\")
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["required_bytes"], 46 * storage.GIB)
        self.assertEqual(report[0]["required_gib"], 46)
        self.assertTrue(report[0]["sufficient"])
        self.assertEqual(len(report[0]["paths"]), 4)
        json.dumps(report, ensure_ascii=False)
        self.assertFalse(paths.data_root.exists())

    def test_ready_environment_reduces_budget(self):
        report = storage.plan_space(self.paths(), 5 * storage.GIB, True,
                    disk_usage=lambda p: self.usage(), volume_key=lambda p: "D:\\")
        self.assertEqual(report[0]["required_bytes"], 14 * storage.GIB)

    def test_separate_volumes_checked_individually(self):
        paths = self.paths()
        def key(path):
            if path == paths.model:
                return "E:\\"
            if path == paths.output:
                return "F:\\"
            return "D:\\"
        reports = storage.plan_space(paths, 25 * storage.GIB, False,
                    disk_usage=lambda p: self.usage(), volume_key=key)
        budgets = {r["volume"]: r["required_bytes"] // storage.GIB for r in reports}
        self.assertEqual(budgets, {"D:\\": 18, "E:\\": 27, "F:\\": 1})

    def test_runtime_and_cache_mounts_separate(self):
        paths = self.paths()
        def key(path):
            return "mounted-cache-volume" if path == paths.cache else "system-volume"
        reports = storage.plan_space(paths, 0, False, disk_usage=lambda p: self.usage(), volume_key=key)
        budgets = {r["volume"]: r["required_bytes"] // storage.GIB for r in reports}
        self.assertEqual(budgets, {"system-volume": 15, "mounted-cache-volume": 6})

    def test_one_byte_short_is_insufficient_and_names_destination(self):
        paths = self.paths()
        usage = self.usage()
        usage.free = 21 * storage.GIB - 1
        with self.assertRaises(storage.StorageError) as caught:
            storage.check_space(paths, 0, False, disk_usage=lambda p: usage, volume_key=lambda p: "目标数据盘")
        self.assertIn("目标数据盘", str(caught.exception))
        self.assertIn(str(paths.model), str(caught.exception))
        self.assertIn("空间不足", str(caught.exception))
        self.assertIn("GiB", str(caught.exception))
        self.assertNotIn(" GB", str(caught.exception))

    def test_exact_free_budget_passes(self):
        reports = storage.check_space(self.paths(), 0, False,
                    disk_usage=lambda p: self.usage(21), volume_key=lambda p: "D:\\")
        self.assertTrue(reports[0]["sufficient"])

    def test_negative_or_invalid_remaining_size_rejected(self):
        for size in (-1, "50", True, 1.5):
            with self.subTest(size=size), self.assertRaises(storage.StorageError):
                storage.plan_space(self.paths(), size, False)

    def test_disk_error_has_clear_destination(self):
        def broken_usage(path):
            raise OSError("disk disconnected")
        paths = self.paths()
        with self.assertRaisesRegex(storage.StorageError, "目标磁盘"):
            storage.plan_space(paths, 0, False, disk_usage=broken_usage, volume_key=lambda p: "volume")

    def test_disk_usage_reads_destination_ancestor(self):
        checked = []
        def usage(path):
            checked.append(path)
            return self.usage()
        storage.plan_space(self.paths(), 0, False, disk_usage=usage, volume_key=lambda p: "volume")
        self.assertEqual(checked, [self.root])

    def test_real_volume_detection_for_nonexistent_target(self):
        reports = storage.plan_space(self.paths(), 0, False)
        self.assertEqual(len(reports), 1)
        self.assertGreater(reports[0]["total_bytes"], 0)
        self.assertTrue(reports[0]["volume_id"])
        self.assertEqual(reports[0]["required_bytes"], 21 * storage.GIB)


if __name__ == "__main__":
    unittest.main()
