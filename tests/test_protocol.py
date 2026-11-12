# -*- coding: utf-8 -*-
"""协议层单元测试：LWW 决策与分块增量重建。仅用标准库，可直接 python -m unittest 运行。"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import protocol as P


class DecideTest(unittest.TestCase):
    def test_push_when_only_local(self):
        plan = P.decide({"a": {"size": 1, "mtime": 100}}, {})
        self.assertEqual(plan["push"], ["a"])
        self.assertEqual(plan["pull"], [])

    def test_pull_when_only_remote(self):
        plan = P.decide({}, {"a": {"size": 1, "mtime": 100}})
        self.assertEqual(plan["pull"], ["a"])

    def test_newer_local_wins(self):
        plan = P.decide({"a": {"size": 1, "mtime": 200}}, {"a": {"size": 1, "mtime": 100}})
        self.assertEqual(plan["push"], ["a"])

    def test_newer_remote_wins(self):
        plan = P.decide({"a": {"size": 1, "mtime": 100}}, {"a": {"size": 1, "mtime": 200}})
        self.assertEqual(plan["pull"], ["a"])

    def test_clock_margin_avoids_ping_pong(self):
        # 1 秒差异在 2 秒容忍范围内且大小相同 -> 不动
        plan = P.decide({"a": {"size": 1, "mtime": 101}}, {"a": {"size": 1, "mtime": 100}})
        self.assertEqual((plan["push"], plan["pull"]), ([], []))

    def test_same_mtime_different_size_pushes(self):
        plan = P.decide({"a": {"size": 2, "mtime": 100}}, {"a": {"size": 1, "mtime": 100}})
        self.assertEqual(plan["push"], ["a"])


class SafePathTest(unittest.TestCase):
    def test_rejects_traversal(self):
        for bad in ["../x", "a/../../x", "..\\x", "a\\..\\..\\x",
                    "/etc/passwd", "\\\\srv\\share\\x", "", ".", "a//b", "a/./b"]:
            self.assertFalse(P.safe_rel(bad), bad)

    def test_accepts_normal_relative_paths(self):
        for good in ["a.sav", "sub/b.sav", "sub\\b.sav", "中文 存档.sav", "a.b.c"]:
            self.assertTrue(P.safe_rel(good), good)

    @unittest.skipUnless(os.name == "nt", "Windows 专用路径规则")
    def test_rejects_windows_escapes(self):
        for bad in ["D:evil", "C:evil", "file.sav:stream", "CON", "NUL.txt",
                    "aux", "a.", "a ", "sub/CON.sav"]:
            self.assertFalse(P.safe_rel(bad), bad)

    def test_safe_join_stays_inside_root(self):
        with tempfile.TemporaryDirectory() as root:
            dest = P.safe_join(root, "sub/a.sav")
            self.assertEqual(os.path.normcase(dest),
                             os.path.normcase(os.path.join(root, "sub", "a.sav")))
            self.assertIsNone(P.safe_join(root, "../x"))
            self.assertIsNone(P.safe_join(root, ".."))
            self.assertIsNone(P.safe_join(root, ""))


class DeltaMessageTest(unittest.TestCase):
    def test_parse_valid(self):
        size = P.BLOCK * 2 + 100
        msg = {"size": size, "block": P.BLOCK, "changed": [0, 2],
               "dsize": P.BLOCK + 100}
        data = b"a" * P.BLOCK + b"b" * 100
        parsed_size, pieces, block = P.parse_delta(msg, data)
        self.assertEqual((parsed_size, block), (size, P.BLOCK))
        self.assertEqual(set(pieces), {0, 2})
        self.assertEqual(len(pieces[2]), 100)

    def test_reject_malformed(self):
        cases = [
            ({"size": 100, "block": 50, "changed": [9], "dsize": 10}, b"x" * 10),
            ({"size": 100, "block": 50, "changed": [0, 0], "dsize": 100}, b"x" * 100),
            ({"size": 100, "block": 50, "changed": [0], "dsize": 30}, b"x" * 30),
            ({"size": 100, "block": 0, "changed": [], "dsize": 0}, b""),
            ({"size": 100, "block": 50, "changed": [0], "dsize": 200}, b"x" * 200),
            ({"block": 50, "changed": [0], "dsize": 50}, b"x" * 50),
        ]
        for msg, data in cases:
            with self.assertRaises(ValueError, msg=msg):
                P.parse_delta(msg, data)


class DeltaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gss-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_block_hashes_roundtrip(self):
        data = os.urandom(P.BLOCK * 2 + 123)
        path = self._write("a.bin", data)
        hashes = P.block_hashes(path)
        self.assertEqual(len(hashes), 3)

    def test_delta_rebuild_middle_block(self):
        size = P.BLOCK * 4
        old = bytearray(os.urandom(size))
        new = bytearray(old)
        new[P.BLOCK:P.BLOCK * 2] = os.urandom(P.BLOCK)  # 整块替换，保持长度
        basis_path = self._write("basis.bin", bytes(old))
        src_path = self._write("src.bin", bytes(new))

        basis = P.block_hashes(basis_path)
        changed = P.changed_blocks(src_path, size, basis)
        self.assertEqual([i for i, _ in changed], [1])

        dest = os.path.join(self.tmp, "dest.bin")
        shutil.copyfile(basis_path, dest)
        P.apply_delta(dest, size, dict(changed), P.BLOCK)
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), bytes(new))

    def test_delta_tail_block_partial(self):
        size = P.BLOCK * 2 + 500
        old = bytearray(os.urandom(size))
        new = bytearray(old)
        new[size - 500:size - 100] = os.urandom(400)  # 尾块内部修改
        basis_path = self._write("basis.bin", bytes(old))
        src_path = self._write("src.bin", bytes(new))
        changed = P.changed_blocks(src_path, size, P.block_hashes(basis_path))

        dest = os.path.join(self.tmp, "dest.bin")
        shutil.copyfile(basis_path, dest)
        P.apply_delta(dest, size, dict(changed), P.BLOCK)
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), bytes(new))

    def test_delta_shrink(self):
        old = os.urandom(P.BLOCK * 3)
        new = old[:P.BLOCK + 10]
        basis_path = self._write("basis.bin", old)
        src_path = self._write("src.bin", new)
        changed = P.changed_blocks(src_path, len(new), P.block_hashes(basis_path))

        dest = os.path.join(self.tmp, "dest.bin")
        shutil.copyfile(basis_path, dest)
        P.apply_delta(dest, len(new), dict(changed), P.BLOCK)
        self.assertEqual(os.path.getsize(dest), len(new))
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), new)

    def test_delta_without_basis_requires_all_blocks(self):
        new = os.urandom(P.BLOCK)
        src_path = self._write("src.bin", new)
        dest = os.path.join(self.tmp, "missing.bin")
        with self.assertRaises(OSError):
            P.apply_delta(dest, len(new), {}, P.BLOCK)

    def test_changed_blocks_respects_scanned_size(self):
        """扫描后文件变大：只按扫描时的 size 计算，不发送多出来的块。"""
        data = os.urandom(P.BLOCK * 2)
        path = self._write("grow.bin", data)
        hashes = P.block_hashes(path)
        self.assertEqual(P.changed_blocks(path, P.BLOCK, hashes), [])

    def test_changed_blocks_detects_shrink(self):
        data = os.urandom(P.BLOCK)
        path = self._write("shrink.bin", data)
        with self.assertRaises(P.LocalFileChanged):
            P.changed_blocks(path, P.BLOCK * 2, [])

    def test_apply_delta_failure_cleans_part_file(self):
        data = os.urandom(P.BLOCK)
        dest = os.path.join(self.tmp, "dest.bin")
        with self.assertRaises(OSError):
            P.apply_delta(dest, len(data), {}, P.BLOCK)
        self.assertFalse(os.path.exists(dest + ".part"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
