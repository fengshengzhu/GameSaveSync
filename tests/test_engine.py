# -*- coding: utf-8 -*-
"""同步引擎中不依赖网络的纯函数测试。"""
import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.engine import parse_target


class ParseTargetTest(unittest.TestCase):
    def test_ipv4_with_port(self):
        self.assertEqual(parse_target("192.168.1.5:29876"),
                         ("192.168.1.5", 29876, socket.AF_INET))

    def test_ipv4_without_port_uses_default(self):
        self.assertEqual(parse_target("192.168.1.5"),
                         ("192.168.1.5", 29876, socket.AF_INET))

    def test_bracketed_ipv6(self):
        self.assertEqual(parse_target("[240e:abcd::1]:29876"),
                         ("240e:abcd::1", 29876, socket.AF_INET6))

    def test_bare_ipv6(self):
        self.assertEqual(parse_target("240e:abcd::1"),
                         ("240e:abcd::1", 29876, socket.AF_INET6))

    def test_scope_id_is_preserved(self):
        self.assertEqual(parse_target("[fe80::1%12]:29876"),
                         ("fe80::1%12", 29876, socket.AF_INET6))

    def test_invalid_inputs(self):
        for bad in ("", "[::1", "192.168.1.5:abc", "192.168.1.5:70000",
                    "192.168.1.5:0"):
            with self.assertRaises(ValueError, msg=bad):
                parse_target(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
