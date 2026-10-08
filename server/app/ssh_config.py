"""读取中心主机的 ~/.ssh/config，列出可以导入为服务器的主机。

按 OpenSSH 的规则取值：配置文件从上到下处理，每个选项取第一个匹配到的值，所以 Host * 里的默认值
只在前面没有设置时生效。支持 Include（相对路径相对于 ~/.ssh）、通配符和 ! 排除。Match 块无法静态判断，跳过。
"""

import fnmatch
import getpass
import glob
import os
import re
from dataclasses import dataclass
from pathlib import Path

MAX_INCLUDE_DEPTH = 16


@dataclass
class SshHost:
    alias: str
    hostname: str
    user: str
    port: int


def _tokens(line: str) -> tuple[str, list[str]] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    match = re.match(r"(\S+?)\s*(?:=\s*|\s+)(.*)$", line)
    if not match:
        return line.lower(), []
    key, rest = match.groups()
    values = [a or b for a, b in re.findall(r'"([^"]*)"|(\S+)', rest)]
    return key.lower(), values


def _read(path: Path, ssh_dir: Path, depth: int = 0) -> list[tuple[str, list[str]]]:
    """展开 Include，返回 (选项, 值) 列表。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    items = []
    for line in text.splitlines():
        token = _tokens(line)
        if token is None:
            continue
        key, values = token
        if key == "include" and depth < MAX_INCLUDE_DEPTH:
            for pattern in values:
                pattern = os.path.expanduser(pattern)
                if not os.path.isabs(pattern):
                    pattern = str(ssh_dir / pattern)
                for name in sorted(glob.glob(pattern)):
                    items += _read(Path(name), ssh_dir, depth + 1)
        else:
            items.append((key, values))
    return items


def _matches(alias: str, patterns: list[str]) -> bool:
    matched = False
    for pattern in patterns:
        if pattern.startswith("!"):
            if fnmatch.fnmatchcase(alias, pattern[1:]):
                return False
        elif fnmatch.fnmatchcase(alias, pattern):
            matched = True
    return matched


def _resolve(alias: str, items: list[tuple[str, list[str]]]) -> dict[str, str]:
    options: dict[str, str] = {}
    active = True  # 第一个 Host 之前的选项对所有主机生效
    for key, values in items:
        if key == "host":
            active = _matches(alias, values)
        elif key == "match":
            active = False
        elif active and values and key not in options:
            options[key] = values[0]
    return options


def list_hosts(config: Path | None = None) -> list[SshHost]:
    """config 中所有不含通配符的 Host 别名及其生效的地址、用户和端口。"""
    ssh_dir = Path.home() / ".ssh"
    items = _read(config or ssh_dir / "config", ssh_dir)
    aliases: list[str] = []
    for key, values in items:
        if key == "host":
            for value in values:
                if not any(c in value for c in "*?!") and value not in aliases:
                    aliases.append(value)
    hosts = []
    for alias in aliases:
        options = _resolve(alias, items)
        hostname = options.get("hostname", alias).replace("%h", alias).replace("%%", "%")
        try:
            port = int(options.get("port", 22))
        except ValueError:
            port = 22
        hosts.append(SshHost(alias, hostname, options.get("user") or getpass.getuser(), port))
    return hosts
