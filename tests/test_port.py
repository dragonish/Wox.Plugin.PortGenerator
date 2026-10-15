"""PortGenerator 纯函数单测（不依赖 Wox 运行时）"""

import socket
import unittest

from src.port_main import (
    check_commands,
    check_port_occupancy,
    generate_free_port,
    is_free_port,
    parse_input,
)


class TestParseInput(unittest.TestCase):
    def test_empty_defaults(self):
        p = parse_input("")
        self.assertEqual(p.kind, "empty")
        self.assertEqual((p.low, p.high), (1024, 65535))

    def test_single(self):
        p = parse_input("8080")
        self.assertEqual((p.kind, p.port), ("single", 8080))
        self.assertEqual(parse_input("0").kind, "error")
        self.assertEqual(parse_input("65536").kind, "error")
        self.assertEqual(parse_input("abc").kind, "error")

    def test_range_dash_tilde_dotdot(self):
        for text in ("8000-9000", "8000~9000", "8000..9000", "8000 - 9000"):
            p = parse_input(text)
            self.assertEqual((p.kind, p.low, p.high), ("range", 8000, 9000), text)
        # 开放端
        self.assertEqual((parse_input("8000-").low, parse_input("8000-").high), (8000, 65535))
        self.assertEqual((parse_input("-9000").low, parse_input("-9000").high), (1, 9000))

    def test_range_brackets(self):
        self.assertEqual((parse_input("[8000,9000]").low, parse_input("[8000,9000]").high), (8000, 9000))
        # (8000,9000) 两端开区间
        p = parse_input("(8000,9000)")
        self.assertEqual((p.low, p.high), (8001, 8999))
        p = parse_input("[8000,9000)")
        self.assertEqual((p.low, p.high), (8000, 8999))
        p = parse_input("(8000,9000]")
        self.assertEqual((p.low, p.high), (8001, 9000))
        # 逆序报错
        self.assertEqual(parse_input("9000-8000").kind, "error")
        self.assertEqual(parse_input("(8000,8000)").kind, "error")

    def test_comparison(self):
        self.assertEqual((parse_input(">8000").low, parse_input(">8000").high), (8001, 65535))
        self.assertEqual((parse_input(">=8000").low, parse_input(">=8000").high), (8000, 65535))
        self.assertEqual((parse_input("<8000").low, parse_input("<8000").high), (1, 7999))
        self.assertEqual((parse_input("<=8000").low, parse_input("<=8000").high), (1, 8000))
        self.assertEqual((parse_input("8000+").low, parse_input("8000+").high), (8000, 65535))
        # 全角符号
        self.assertEqual((parse_input("≥8000").low, parse_input("≥8000").high), (8000, 65535))
        self.assertEqual((parse_input("≤8000").low, parse_input("≤8000").high), (1, 8000))
        self.assertEqual(parse_input(">65535").kind, "error")
        self.assertEqual(parse_input("<1").kind, "error")

    def test_fullwidth_comma_bracket(self):
        p = parse_input("［8000，9000］")
        self.assertEqual((p.kind, p.low, p.high), ("range", 8000, 9000))


class TestOccupancy(unittest.TestCase):
    def test_occupied_vs_free(self):
        # 占用一个系统分配的临时端口，探测应报占用
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            occupied = s.getsockname()[1]
            self.assertTrue(check_port_occupancy(occupied).in_use)
        finally:
            s.close()
        # 释放后应恢复为空闲（UDP/TCP 都未占用）
        self.assertTrue(is_free_port(occupied))

    def test_generate_within_range(self):
        port = generate_free_port(52000, 52100)
        self.assertIsNotNone(port)
        assert port is not None
        self.assertTrue(52000 <= port <= 52100)
        self.assertTrue(is_free_port(port))

    def test_generate_single_free(self):
        # 找一个空闲端口，以它为单点范围生成应返回它本身
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        finally:
            s.close()
        self.assertEqual(generate_free_port(free, free), free)

    def test_generate_fully_occupied_returns_none(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            occupied = s.getsockname()[1]
            # TCP 占用即判占用，UDP 空闲也不影响
            self.assertIsNone(generate_free_port(occupied, occupied))
        finally:
            s.close()


class TestCheckCommands(unittest.TestCase):
    def test_commands_contain_port(self):
        cmds = check_commands(8080)
        for cmd in cmds.values():
            self.assertIn("8080", cmd)
        self.assertIn("netstat", cmds["windows_cmd"])
        self.assertIn("Get-NetTCPConnection", cmds["powershell"])
        self.assertIn("lsof", cmds["lsof"])
        self.assertIn("ss", cmds["ss"])


if __name__ == "__main__":
    unittest.main()
