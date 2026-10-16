"""Trim-CLI 二进制引导层：定位并执行 vendor 目录下的 trim-cli 可执行文件。"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

# 项目根目录（fnos_mcp/ 的上一级）
ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = ROOT / "vendor" / "trim-cli"

# fnos-skill 源仓库（fa21571, 2026-10-17）打包发布 trim-cli v0.1.1
TRIM_CLI_SOURCE = "https://github.com/Muwei-TA/fnos-skill"
TRIM_CLI_VERSION = "0.1.1"

# 平台 -> 二进制文件名映射（与 fnos-skill manifest.json 的 bin 字段一致）
_PLATFORM_BINARIES = {
    ("Darwin", "arm64"): "trim-cli-darwin-arm64",
    ("Darwin", "x86_64"): "trim-cli-darwin-x64",
    ("Linux", "aarch64"): "trim-cli-linux-arm64",
    ("Linux", "x86_64"): "trim-cli-linux-x64",
    ("Windows", "AMD64"): "trim-cli-windows-x64.exe",
    ("Windows", "ARM64"): "trim-cli-windows-arm64.exe",
}


def resolve_binary() -> Path:
    """定位当前平台可用的 trim-cli 二进制；找不到时回退 PATH。"""
    key = (platform.system(), platform.machine())
    name = _PLATFORM_BINARIES.get(key)
    if name:
        candidate = VENDOR_DIR / name
        if candidate.is_file():
            return candidate
    # 兜底：机器别名匹配（如 aarch64 be / AMD64 别名）
    arch = platform.machine().lower()
    if arch in ("arm64",):
        arch = "aarch64" if platform.system() != "Darwin" else "arm64"
    for (osname, machine), name in _PLATFORM_BINARIES.items():
        if osname == platform.system() and machine.lower() == arch.lower():
            candidate = VENDOR_DIR / name
            if candidate.is_file():
                return candidate
    # 最后回退到 PATH 中的 trim-cli（用户自行安装的场景）
    fallback = shutil.which("trim-cli")
    if fallback:
        return Path(fallback)
    raise RuntimeError(
        f"未找到 {platform.system()}/{platform.machine()} 平台的 trim-cli 二进制，"
        f"vendor 目录（{VENDOR_DIR}）也没有匹配文件。"
    )


def run_trim(
    args: list[str],
    *,
    profile: str | None = None,
    host: str | None = None,
    port: int | None = None,
    scheme: str | None = None,
    allow_insecure_http: bool = False,
    tls_insecure: bool = False,
    timeout: float = 60.0,
) -> dict:
    """执行 trim-cli 命令并返回结构化结果。

    返回 dict：ok / exit_code / stdout / stderr / duration_ms。
    绝不抛异常，调用方（MCP 工具层）负责把失败信息转成人类可读文本。
    """
    import time

    binary = resolve_binary()
    cmd: list[str] = [str(binary)]
    # 全局连接选项必须放在子命令之前
    if profile:
        cmd += ["--profile", profile]
    if host:
        cmd += ["--host", host]
    if port is not None:
        cmd += ["--port", str(port)]
    if scheme:
        cmd += ["--scheme", scheme]
    if allow_insecure_http:
        cmd += ["--allow-insecure-http"]
    if tls_insecure:
        cmd += ["--tls-insecure"]
    cmd += args

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = int((time.monotonic() - start) * 1000)
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "duration_ms": elapsed,
            "command": " ".join(cmd),
        }
    except subprocess.TimeoutExpired:
        elapsed = int((time.monotonic() - start) * 1000)
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"命令超时（{timeout} 秒）：{' '.join(cmd)}",
            "duration_ms": elapsed,
            "command": " ".join(cmd),
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"trim-cli 二进制不存在：{binary}",
            "duration_ms": 0,
            "command": " ".join(cmd),
        }


if __name__ == "__main__":
    # 直接运行本模块时打印二进制定位结果，用于部署自检
    result = run_trim(["--help"], timeout=10)
    print(f"binary: {resolve_binary()}")
    print(f"help ok: {result['ok']} (exit {result['exit_code']})")
    sys.exit(0 if result["ok"] else 1)
