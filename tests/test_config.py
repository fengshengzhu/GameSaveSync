# -*- coding: utf-8 -*-
"""配置路径测试。

规则：打包（frozen）后配置统一放 %APPDATA%\\GameSaveSync，绝不写进 exe 所在
目录（用户会把 exe 放到桌面/下载等位置）或临时解压目录；源码运行用项目根。
"""
import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config as cfgmod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConfigPathTest(unittest.TestCase):
    def tearDown(self):
        importlib.reload(cfgmod)  # 恢复正常（非 frozen）状态

    def _reload_frozen(self, exe_path, appdata):
        old = (getattr(sys, "frozen", None), sys.executable, os.environ.get("APPDATA"))
        sys.frozen = True
        sys.executable = exe_path
        os.environ["APPDATA"] = appdata
        try:
            return importlib.reload(cfgmod)
        finally:
            if old[0] is None:
                del sys.frozen
            else:
                sys.frozen = old[0]
            sys.executable = old[1]
            if old[2] is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = old[2]

    def test_source_mode_uses_project_root(self):
        self.assertEqual(os.path.dirname(cfgmod.CONFIG_PATH), ROOT)

    def test_frozen_uses_appdata_not_exe_dir(self):
        """单文件 exe：配置必须写进 %APPDATA%\\GameSaveSync，exe 目录保持干净。"""
        exe_dir = tempfile.mkdtemp(prefix="gss-exe-")
        appdata = tempfile.mkdtemp(prefix="gss-appdata-")
        self.addCleanup(shutil.rmtree, exe_dir, True)
        self.addCleanup(shutil.rmtree, appdata, True)
        m = self._reload_frozen(os.path.join(exe_dir, "GameSaveSync.exe"), appdata)

        expect_dir = os.path.join(appdata, "GameSaveSync")
        self.assertEqual(m.APP_DIR, expect_dir)
        self.assertTrue(os.path.isdir(expect_dir))
        self.assertEqual(m.CONFIG_PATH, os.path.join(expect_dir, "sync_config.json"))

        # 保存后文件在 APPDATA，exe 目录不产生任何文件
        m.Config({"token": "INAPPDATA"}).save()
        self.assertTrue(os.path.isfile(os.path.join(expect_dir, "sync_config.json")))
        self.assertEqual(m.load().token, "INAPPDATA")
        self.assertEqual([f for f in os.listdir(exe_dir) if f.endswith(".json")], [])

    def test_migrate_legacy_files(self):
        """旧版本遗留在 exe 旁边的配置应被迁移到 APPDATA，且不覆盖已有新配置。"""
        exe_dir = tempfile.mkdtemp(prefix="gss-exe-")
        appdata = tempfile.mkdtemp(prefix="gss-appdata-")
        self.addCleanup(shutil.rmtree, exe_dir, True)
        self.addCleanup(shutil.rmtree, appdata, True)
        m = self._reload_frozen(os.path.join(exe_dir, "GameSaveSync.exe"), appdata)

        # migrate 检查 sys.frozen，调用期间需保持 frozen 标志（源码运行本无此标志）
        old_exe = sys.executable
        sys.frozen = True
        sys.executable = os.path.join(exe_dir, "GameSaveSync.exe")

        def _restore():
            del sys.frozen
            sys.executable = old_exe
            importlib.reload(cfgmod)  # tearDown 会在 cleanup 之前 reload，这里先恢复正常状态

        self.addCleanup(_restore)

        # exe 旁有旧配置；APPDATA 里已有新配置 -> 不覆盖
        legacy = os.path.join(exe_dir, "sync_config.json")
        with open(legacy, "w", encoding="utf-8") as f:
            json.dump({"token": "OLD"}, f)
        existing = os.path.join(m.APP_DIR, "sync_config.json")
        with open(existing, "w", encoding="utf-8") as f:
            json.dump({"token": "NEW"}, f)

        msgs = m.migrate_legacy_files()
        self.assertEqual(msgs, [])
        self.assertEqual(m.load().token, "NEW")

        # APPDATA 无配置时才迁移
        os.remove(existing)
        msgs = m.migrate_legacy_files()
        self.assertEqual(len(msgs), 1)
        self.assertFalse(os.path.exists(legacy))
        self.assertEqual(m.load().token, "OLD")


if __name__ == "__main__":
    unittest.main(verbosity=2)
