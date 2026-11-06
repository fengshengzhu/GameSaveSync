# -*- coding: utf-8 -*-
"""配置路径测试：打包（frozen）后配置必须落在 exe 目录，而不是临时解压目录。"""
import importlib
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

    def _reload_frozen(self, exe_path):
        old_frozen = getattr(sys, "frozen", None)
        old_exe = sys.executable
        sys.frozen = True
        sys.executable = exe_path
        try:
            return importlib.reload(cfgmod)
        finally:
            if old_frozen is None:
                del sys.frozen
            else:
                sys.frozen = old_frozen
            sys.executable = old_exe

    def test_source_mode_uses_project_root(self):
        self.assertEqual(os.path.dirname(cfgmod.CONFIG_PATH), ROOT)

    def test_frozen_uses_exe_directory(self):
        """单文件 exe：__file__ 在临时解压目录，配置必须写到 exe 同级目录。"""
        tmp = tempfile.mkdtemp(prefix="gss-frozen-")
        self.addCleanup(shutil.rmtree, tmp, True)
        m = self._reload_frozen(os.path.join(tmp, "GameSaveSync.exe"))
        self.assertEqual(m.APP_DIR, tmp)
        self.assertEqual(m.CONFIG_PATH, os.path.join(tmp, "sync_config.json"))

        # 保存后文件确实在 exe 目录，且能读回
        m.Config({"token": "RECOVERED"}).save()
        path = os.path.join(tmp, "sync_config.json")
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(m.load().token, "RECOVERED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
