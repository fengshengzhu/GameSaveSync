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


if __name__ == "__main__":
    unittest.main(verbosity=2)
