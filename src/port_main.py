from __future__ import annotations

import errno
import random
import re
import socket
from dataclasses import dataclass

from wox_plugin import (
    ActionContext,
    Context,
    CopyParams,
    CopyType,
    Plugin,
    PluginInitParams,
    PublicAPI,
    Query,
    Result,
    ResultAction,
    WoxImage,
)

MIN_PORT = 1
MAX_PORT = 65535
DEFAULT_LOW = 1024
DEFAULT_HIGH = 65535
# 大范围随机尝试次数；小范围会全量洗牌遍历，不受此限制。
MAX_RANDOM_ATTEMPTS = 200

# 全角 / 中文标点 → 半角，便于解析
_FULLWIDTH_MAP = str.maketrans(
    {
        "，": ",",
        "～": "~",
        "〜": "~",
        "＞": ">",
        "＜": "<",
        "≥": ">=",
        "≤": "<=",
        "－": "-",
        "–": "-",
        "—": "-",
        "［": "[",
        "］": "]",
        "（": "(",
        "）": ")",
        "：": ":",
    }
)


@dataclass
class ParsedInput:
    kind: str  # "empty" | "single" | "range" | "error"
    port: int | None = None
    low: int | None = None
    high: int | None = None
    error: str = ""
    display: str = ""

    @property
    def range_display(self) -> str:
        if self.low is not None and self.high is not None:
            return f"{self.low}-{self.high}"
        return self.display


@dataclass
class PortStatus:
    port: int
    tcp_in_use: bool = False
    udp_in_use: bool = False
    permission_denied: bool = False

    @property
    def in_use(self) -> bool:
        return self.tcp_in_use or self.udp_in_use

    @property
    def unknown(self) -> bool:
        """权限不足导致无法确定（通常是 1-1023 特权端口且非管理员）。"""
        return self.permission_denied and not self.in_use


def _err(msg: str) -> ParsedInput:
    return ParsedInput(kind="error", error=msg)


def _validate_range(low: int, high: int) -> ParsedInput | None:
    if low < MIN_PORT or high > MAX_PORT:
        return _err(f"端口范围须在 {MIN_PORT}-{MAX_PORT} 内")
    if low > high:
        return _err("下限大于上限，没有可用端口")
    return None


def parse_input(raw: str) -> ParsedInput:
    """解析用户输入。支持：

    - 空白 → 默认范围 1024-65535
    - 单端口: 8080
    - 比较: >8000 / >=8000 / <8000 / <=8000 / 8000+
    - 区间: 8000-9000 / 8000~9000 / 8000..9000 / 8000- / -9000
    - 括号: [8000,9000] / (8000,9000) / [8000,9000) / (8000,9000]
    """
    text = (raw or "").strip()
    if not text:
        return ParsedInput(kind="empty", low=DEFAULT_LOW, high=DEFAULT_HIGH, display=f"{DEFAULT_LOW}-{DEFAULT_HIGH}")

    compact = re.sub(r"\s+", "", text.translate(_FULLWIDTH_MAP))

    # 单端口: 8080
    if re.fullmatch(r"\d+", compact):
        port = int(compact)
        if not MIN_PORT <= port <= MAX_PORT:
            return _err(f"端口须在 {MIN_PORT}-{MAX_PORT} 内")
        return ParsedInput(kind="single", port=port, display=str(port))

    # 括号区间: [8000,9000] / (8000,9000) / [8000,9000) / (8000,9000]
    m = re.fullmatch(r"([\[\(])(\d*),(\d*)([\]\)])", compact)
    if m:
        left_bracket, left_s, right_s, right_bracket = m.groups()
        try:
            if left_s == "":
                low = MIN_PORT
            else:
                low = int(left_s) + (1 if left_bracket == "(" else 0)
            if right_s == "":
                high = MAX_PORT
            else:
                high = int(right_s) - (1 if right_bracket == ")" else 0)
        except ValueError:
            return _err("区间数字无法解析")
        bad = _validate_range(low, high)
        if bad:
            return bad
        return ParsedInput(kind="range", low=low, high=high, display=f"{low}-{high}")

    # 比较: >8000 / >=8000 / <8000 / <=8000
    m = re.fullmatch(r"(>=|<=|>|<)(\d+)", compact)
    if m:
        op, num_s = m.groups()
        num = int(num_s)
        if not MIN_PORT <= num <= MAX_PORT:
            return _err(f"端口须在 {MIN_PORT}-{MAX_PORT} 内")
        if op == ">":
            low, high = num + 1, MAX_PORT
        elif op == ">=":
            low, high = num, MAX_PORT
        elif op == "<":
            low, high = MIN_PORT, num - 1
        else:  # <=
            low, high = MIN_PORT, num
        bad = _validate_range(low, high)
        if bad:
            return bad
        return ParsedInput(kind="range", low=low, high=high, display=f"{low}-{high}")

    # 后缀 +: 8000+ 即 >=8000
    m = re.fullmatch(r"(\d+)\+", compact)
    if m:
        low = int(m.group(1))
        if not MIN_PORT <= low <= MAX_PORT:
            return _err(f"端口须在 {MIN_PORT}-{MAX_PORT} 内")
        return ParsedInput(kind="range", low=low, high=MAX_PORT, display=f"{low}-{MAX_PORT}")

    # 区间分隔符: 8000-9000 / 8000~9000 / 8000..9000 / 8000- / -9000
    for pattern in (r"(\d*)\.\.(\d*)", r"(\d*)-(\d*)", r"(\d*)~(\d*)"):
        m = re.fullmatch(pattern, compact)
        if m:
            left_s, right_s = m.groups()
            if left_s == "" and right_s == "":
                return _err("请给出范围，如 8000-9000")
            low = int(left_s) if left_s != "" else MIN_PORT
            high = int(right_s) if right_s != "" else MAX_PORT
            bad = _validate_range(low, high)
            if bad:
                return bad
            return ParsedInput(kind="range", low=low, high=high, display=f"{low}-{high}")

    return _err("无法识别的格式")


def _try_bind(family: int, sock_type: int, address: tuple) -> tuple[bool, bool]:
    """尝试 bind 一次。返回 (in_use, permission_denied)。"""
    sock = None
    try:
        sock = socket.socket(family, sock_type)
        # 不设 SO_REUSEADDR: TIME_WAIT 的端口也应视为暂不可用。
        sock.bind(address)
        return False, False
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, 10048):  # 10048 = WSAEADDRINUSE
            return True, False
        if e.errno in (errno.EACCES, 10013):  # 10013 = WSAEACCES，特权端口
            return False, True
        # EADDRNOTAVAIL / EAFNOSUPPORT 等: 该地址族不可用，忽略。
        return False, False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def check_port_occupancy(port: int) -> PortStatus:
    """探测端口在本机是否被占用（TCP + UDP，127.0.0.1 + ::1）。

    注意：只探测回环地址。若进程只绑了某个外网 IP（如 192.168.x.x），
    bind 探测会误报“未占用”，此时请用复制的 lsof / netstat 命令二次确认。
    """
    status = PortStatus(port=port)
    # TCP
    tcp_v4_in_use, tcp_v4_perm = _try_bind(socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", port))
    tcp_v6_in_use, tcp_v6_perm = _try_bind(socket.AF_INET6, socket.SOCK_STREAM, ("::1", port, 0, 0))
    status.tcp_in_use = tcp_v4_in_use or tcp_v6_in_use
    # UDP
    udp_v4_in_use, udp_v4_perm = _try_bind(socket.AF_INET, socket.SOCK_DGRAM, ("127.0.0.1", port))
    udp_v6_in_use, udp_v6_perm = _try_bind(socket.AF_INET6, socket.SOCK_DGRAM, ("::1", port, 0, 0))
    status.udp_in_use = udp_v4_in_use or udp_v6_in_use
    status.permission_denied = tcp_v4_perm or tcp_v6_perm or udp_v4_perm or udp_v6_perm
    return status


def is_free_port(port: int) -> bool:
    """生成时用：未占用且无权限问题才算可用。"""
    status = check_port_occupancy(port)
    return not status.in_use and not status.permission_denied


def generate_free_port(low: int, high: int) -> int | None:
    """在 [low, high] 内找一个本机未占用的端口，找不到返回 None。"""
    size = high - low + 1
    if size <= 2000:
        candidates = list(range(low, high + 1))
        random.shuffle(candidates)
        for port in candidates:
            if is_free_port(port):
                return port
        return None
    for _ in range(MAX_RANDOM_ATTEMPTS):
        port = random.randint(low, high)
        if is_free_port(port):
            return port
    return None


def check_commands(port: int) -> dict[str, str]:
    """各平台查验该端口是否被占用的命令（复制用）。"""
    return {
        # CMD 的 findstr 是子串匹配，会把 18080 也匹配进来；精确查验请用 PowerShell。
        "windows_cmd": f"netstat -ano | findstr :{port}",
        "powershell": (
            f"Get-NetTCPConnection -LocalPort {port} | "
            f"Format-Table LocalAddress,LocalPort,State,OwningProcess -AutoSize; "
            f"Get-NetUDPEndpoint -LocalPort {port}"
        ),
        "lsof": f"lsof -i :{port}",
        "ss": f"ss -tulpn | grep :{port}",
    }


def _icon(emoji: str) -> WoxImage:
    return WoxImage.new_emoji(emoji)


def _copy_action(name: str, emoji: str, text: str, *, is_default: bool = False) -> ResultAction:
    return ResultAction(
        name=name,
        icon=_icon(emoji),
        is_default=is_default,
        context_data={"text": text},
        action=_action_copy_text,
    )


async def _action_copy_text(ctx: Context, ac: ActionContext) -> None:
    """复制动作: Wox 以 (ctx, ac) 调用；api 取自全局单例 plugin（init 时已注入）。"""
    text = (ac.context_data or {}).get("text", "")
    if text:
        await plugin.api.copy(ctx, CopyParams(type=CopyType.TEXT, text=text))


class PortGeneratorPlugin(Plugin):
    api: PublicAPI

    async def init(self, ctx: Context, init_params: PluginInitParams) -> None:
        self.api = init_params.api

    async def query(self, ctx: Context, query: Query) -> list[Result]:
        parsed = parse_input(query.search or "")

        if parsed.kind == "error":
            return [
                Result(
                    title="格式不正确",
                    sub_title=f"{parsed.error} · 例如: 8080 / 8000-9000 / [8000,9000] / >=8000",
                    icon=_icon("⚠️"),
                ),
                Result(
                    title="端口语法速查",
                    sub_title="单个: 8080 | 区间: 8000-9000 / 8000~9000 | 括号: [8000,9000] | 比较: >8000 / >=8000 / <8000 / <=8000",
                    icon=_icon("📖"),
                ),
            ]

        if parsed.kind == "single":
            assert parsed.port is not None
            return self._single_port_results(parsed.port)

        # 空白 / 范围: 预览候选
        assert parsed.low is not None and parsed.high is not None
        low, high = parsed.low, parsed.high
        results = []
        preview = generate_free_port(low, high)
        if preview is not None:
            results.append(
                Result(
                    title=f"候选: {preview} (未占用)",
                    sub_title=f"在 {low}-{high} 内随机抽到的空闲端口 · 回车复制端口号",
                    icon=_icon("🟢"),
                    actions=[_copy_action("复制端口号", "📋", str(preview), is_default=True)],
                )
            )
        if parsed.kind == "empty":
            results.append(
                Result(
                    title="端口语法速查",
                    sub_title="单个: 8080 | 区间: 8000-9000 / 8000~9000 | 括号: [8000,9000] | 比较: >8000 / >=8000 / <8000 / <=8000",
                    icon=_icon("📖"),
                )
            )
        return results

    def _single_port_results(self, port: int) -> list[Result]:
        status = check_port_occupancy(port)
        cmds = check_commands(port)

        if status.unknown:
            icon_emoji, status_text = "❓", "占用情况未知 (需管理员权限才能准确探测)"
        elif status.in_use:
            parts: list[str] = []
            if status.tcp_in_use:
                parts.append("TCP")
            if status.udp_in_use:
                parts.append("UDP")
            icon_emoji, status_text = "🔴", f"本机已被占用 ({'+'.join(parts)})"
        else:
            icon_emoji, status_text = "🟢", "本机未占用"

        return [
            Result(
                title=f"端口 {port}  {icon_emoji} {status_text}",
                sub_title=f"{status_text} · 回车复制端口号，或从操作中复制各平台查验命令",
                icon=_icon(icon_emoji),
                actions=[
                    _copy_action("复制端口号", "📋", str(port), is_default=True),
                    _copy_action("复制 Windows CMD 查验命令", "🪟", cmds["windows_cmd"]),
                    _copy_action("复制 PowerShell 查验命令", "💙", cmds["powershell"]),
                    _copy_action("复制 macOS / Linux lsof 命令", "🍎", cmds["lsof"]),
                    _copy_action("复制 Linux ss 命令", "🐧", cmds["ss"]),
                ],
            )
        ]


plugin = PortGeneratorPlugin()
