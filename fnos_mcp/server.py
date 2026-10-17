"""fnOS MCP Server —— 将 trim-cli（fnos-skill）封装为 MCP 工具集。

通过 FastMCP 暴露飞牛 fnOS NAS 的管理能力：
认证 / 系统 / 文件 / 相册 / 应用中心 / Docker / 下载 / 日志 / 监控 /
网络 / 电源 / 存储 / 用户 / 百度网盘。

安全约定（继承自 fnos-skill SKILL.md）：
- 登录仅支持 OAuth 2.0 Authorization Code + PKCE，交互式完成
- Agent / 非交互流程执行写操作命令时一律显式传 --yes
- 远程明文 HTTP 需要 allow_insecure_http；自签 HTTPS 需要 tls_insecure
"""
from __future__ import annotations

import argparse
import json
from typing import Annotated, Any, Literal

from fastmcp import FastMCP

if __package__ in (None, ""):
    # 支持直接运行：python server.py（把项目根目录加入 sys.path）
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from fnos_mcp.cli import TRIM_CLI_SOURCE, TRIM_CLI_VERSION, run_trim
else:
    from .cli import TRIM_CLI_SOURCE, TRIM_CLI_VERSION, run_trim

mcp = FastMCP(
    "fnos-mcp",
    instructions=(
        "飞牛 fnOS NAS 管理 MCP 服务器（封装 trim-cli，基于 fnos-skill）。"
        "覆盖系统、文件、相册、应用中心、Docker、下载、日志、监控、网络、"
        "电源、存储、用户和百度网盘管理。登录采用 OAuth 2.0 + PKCE 交互式授权，"
        "不处理账号密码。"
    ),
)

# ---------------------------------------------------------------------------
# 公共参数类型
# ---------------------------------------------------------------------------
Profile = Annotated[str | None, "独立 session profile 名称，默认使用 default"]
Host = Annotated[str | None, "NAS 主机地址（IP 或域名）"]
Port = Annotated[int | None, "NAS 端口，如 5667（HTTPS）/5666（HTTP）/443"]
Scheme = Annotated[Literal["auto", "http", "https"] | None, "传输协议"]
InsecureHttp = Annotated[bool, "允许远程明文 HTTP（危险，仅在受信任网络使用）"]
TlsInsecure = Annotated[bool, "允许 HTTPS 自签证书（跳过证书校验）"]
Path = Annotated[str, "NAS 上的路径，如 /vol1/1000/文档"]


def _fmt(result: dict) -> str:
    """把 run_trim 结果转成给 LLM 的文本：优先 JSON，失败时给错误说明。"""
    if result["ok"]:
        out = result["stdout"]
        if not out:
            return "命令执行成功（无输出）"
        # 尝试解析为 JSON 美化输出，方便 LLM 读取
        try:
            parsed = json.loads(out)
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, TypeError):
            return out
    # 失败：给出退出码 + stderr（可能包含错误原因与提示）
    parts = [f"命令失败（exit {result['exit_code']}）"]
    if result["stderr"]:
        parts.append(result["stderr"])
    if result["stdout"]:
        parts.append(f"stdout: {result['stdout']}")
    return "\n".join(parts)


def _bool_flag(value: bool) -> list[str]:
    return ["--yes"] if value else []


def _multi_cmds(
    commands: list[tuple[str, list[str]]],
    profile: str | None,
    host: str | None,
    port: int | None,
    scheme: str | None,
) -> str:
    """依次执行多条只读命令，合并为单个 JSON 返回（单条失败不影响其余）。"""
    out: dict[str, Any] = {}
    for key, args in commands:
        r = run_trim(args, profile=profile, host=host, port=port, scheme=scheme)
        if r["ok"]:
            text = r["stdout"].strip()
            if not text:
                out[key] = "(无输出)"
                continue
            try:
                out[key] = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                out[key] = text
        else:
            err = (r["stderr"] or r["stdout"] or "").strip().splitlines()
            out[key] = "失败: " + (err[-1] if err else f"exit {r['exit_code']}")
    return json.dumps(out, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 认证与会话
# ---------------------------------------------------------------------------

@mcp.tool
def login(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
    allow_insecure_http: InsecureHttp = False,
    tls_insecure: TlsInsecure = False,
) -> str:
    """[交互式] 发起 OAuth 2.0 + PKCE 登录。

    返回授权 URL。用户需在浏览器打开该 URL，登录 fnOS 并确认授权，
    然后把页面显示的一次性 code 用 submit_auth_code 工具提交。
    不要向用户索取 NAS 账号密码。
    """
    result = run_trim(
        ["login", "--no-open"],
        profile=profile, host=host, port=port, scheme=scheme,
        allow_insecure_http=allow_insecure_http, tls_insecure=tls_insecure,
        timeout=15,
    )
    if result["ok"]:
        out = result["stdout"]
        # login --no-open 会打印授权 URL 后继续等待 stdin 输入 code；
        # 15 秒超时意味着 CLI 正在等待交互，此时提取 URL 返回给用户
        import re
        m = re.search(r"https?://\S+", out)
        if m:
            return (
                "登录流程已发起（OAuth 2.0 + PKCE，一次性 code 5 分钟内有效）。\n"
                f"请让用户在浏览器打开并授权：\n{m.group(0)}\n"
                "授权完成后，把页面显示的一次性 code 通过 submit_auth_code 工具提交。"
            )
        return _fmt(result)
    return _fmt(result)


@mcp.tool
def submit_auth_code(
    code: Annotated[str, "浏览器授权页面显示的一次性授权码"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
    allow_insecure_http: InsecureHttp = False,
    tls_insecure: TlsInsecure = False,
) -> str:
    """提交 OAuth 一次性授权码，完成 token 交换与 session 保存。

    注意：该工具通过带 pty 的后台进程完成交互（CLI 要求 TTY 等待输入），
    仅在 code 有效（5 分钟内、未使用）时成功。
    """
    import os
    import pty
    import select
    import time as _time

    binary_cmd = []
    from .cli import resolve_binary
    binary_cmd.append(str(resolve_binary()))
    if profile:
        binary_cmd += ["--profile", profile]
    if host:
        binary_cmd += ["--host", host]
    if port is not None:
        binary_cmd += ["--port", str(port)]
    if scheme:
        binary_cmd += ["--scheme", scheme]
    if allow_insecure_http:
        binary_cmd.append("--allow-insecure-http")
    if tls_insecure:
        binary_cmd.append("--tls-insecure")
    binary_cmd += ["login", "--no-open"]

    env = dict(os.environ)
    # 容器/无桌面环境：平台安全存储可能只读，回退文件存储
    env.setdefault("TRIM_CLI_SESSION_STORAGE", "file")

    pid, fd = pty.fork()
    if pid == 0:
        os.execve(binary_cmd[0], binary_cmd, env)

    buf = b""
    prompt_seen = False
    code_sent = False
    deadline = _time.time() + 90
    try:
        while _time.time() < deadline:
            r, _, _ = select.select([fd], [], [], 0.5)
            if r:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                if b"Authorization code" in buf:
                    prompt_seen = True
            if prompt_seen and not code_sent:
                os.write(fd, (code + "\r\n").encode())
                code_sent = True
            done, status = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                try:
                    while True:
                        chunk = os.read(fd, 4096)
                        if not chunk:
                            break
                        buf += chunk
                except OSError:
                    pass
                exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
                text = buf.decode(errors="replace")
                if exit_code == 0 and ("success" in text.lower() or "saved" in text.lower() or "logged" in text.lower()):
                    return "登录成功，session 已保存。"
                return f"登录进程退出（exit {exit_code}）。\n{text[-2000:]}"
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return "提交超时（90 秒）。请确认 code 仍有效（5 分钟内、未使用），或重新调用 login。"


@mcp.tool
def logout(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """退出登录并清理当前 profile 的 session 与 OAuth pending 文件。"""
    return _fmt(run_trim(["logout"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def whoami(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询当前登录用户信息（验证 session 有效性）。"""
    return _fmt(run_trim(["user", "info"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def status_overview(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """聚合只读状态：系统信息 + CPU/内存监控（对应 trim-cli +status）。"""
    return _fmt(run_trim(["+status"], profile=profile, host=host, port=port, scheme=scheme))


# ---------------------------------------------------------------------------
# 系统
# ---------------------------------------------------------------------------

@mcp.tool
def system_info(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """获取系统信息：主机名、trim 版本、内核版本、uptime 等。"""
    return _fmt(run_trim(["system", "info"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def system_hardware(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """获取硬件信息。"""
    return _fmt(run_trim(["system", "hardware"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def system_volumes(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出所有卷（含配置卷）。"""
    return _fmt(run_trim(["system", "all-vols"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def system_power_plan(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """获取定时开关机计划与状态。"""
    return _fmt(run_trim(["system", "power-plan"], profile=profile, host=host, port=port, scheme=scheme))


# 系统 20 个只读信息命令中，system_info / system_hardware / system_volumes(=all-vols)
# / system_power_plan 已有独立工具，其余 18 项聚合为 system_details，时区单列

_SYSTEM_ITEMS: dict[str, list[str]] = {
    "trim_version": ["system", "trim-version"],
    "kernel_version": ["system", "kernel-version"],
    "host_name": ["system", "host-name"],
    "machine_id": ["system", "machine-id"],
    "uptime": ["system", "uptime"],
    "unix_time": ["system", "unix-time"],
    "boot_on_power": ["system", "boot-on-power"],
    "reserved_partition": ["system", "reserved-partition"],
    "is_trim_machine": ["system", "is-trim-machine"],
    "trim_feature": ["system", "trim-feature"],
    "fan_mode": ["system", "fan-mode"],
    "trim_disk": ["system", "trim-disk"],
    "time_setting": ["system", "time-setting"],
    "next_power_off_time": ["system", "next-power-off-time"],
    "power_plan_status": ["system", "power-plan-status"],
    "power_on_seconds": ["system", "power-on-seconds"],
    "inited_timestamp": ["system", "inited-timestamp"],
    "docker": ["system", "docker"],
}


@mcp.tool
def system_details(
    items: Annotated[
        list[str] | None,
        "要查询的条目（可选值：" + ", ".join(_SYSTEM_ITEMS) + "）；缺省查询全部",
    ] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """聚合查询系统只读信息（trim 版本、内核、主机名、uptime、风扇模式、
    通电自启、下次定时关机、Docker 概要等 18 项；单条失败不影响其余）。"""
    cmds = [(k, a) for k, a in _SYSTEM_ITEMS.items() if items is None or k in items]
    if not cmds:
        return "未匹配到任何条目。可用条目：" + ", ".join(_SYSTEM_ITEMS)
    return _multi_cmds(cmds, profile, host, port, scheme)


@mcp.tool
def system_timezone_list(
    lang: Annotated[str | None, "语言代码，如 zh-CN"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出系统支持的时区。"""
    args = ["system", "timezone-list"]
    if lang:
        args += ["--lang", lang]
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


# ---------------------------------------------------------------------------
# 文件管理
# ---------------------------------------------------------------------------

@mcp.tool
def file_list(
    path: Path,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出目录内容（单页）。"""
    return _fmt(run_trim(["file", "ls", path], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_list_all(
    path: Path,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出目录全部内容（自动汇总所有分页为 JSON 数组）。"""
    return _fmt(run_trim(["file", "ls-dir", path], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_search(
    keyword: Annotated[str, "搜索关键词"],
    paths: Annotated[list[str] | None, "限定搜索的目录列表，默认全盘"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """全盘或指定目录搜索文件。"""
    args = ["file", "search", keyword]
    if paths:
        args += paths
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_property(
    path: Path,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """获取文件/目录属性。"""
    return _fmt(run_trim(["file", "prop", path], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_size(
    path: Path,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """计算文件/目录大小。"""
    return _fmt(run_trim(["file", "size", path], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_download_url(
    path: Path,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """获取文件下载 URL（通常需浏览器已登录 cookie）。"""
    return _fmt(run_trim(["file", "download-url", path], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_usage(
    vol: Annotated[str, "卷名或卷 ID，如 1 或 /vol1"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看卷空间使用分类汇总。"""
    return _fmt(run_trim(["file", "usage", vol], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_recent(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
    ) -> str:
    """列出最近访问的文件。"""
    return _fmt(run_trim(["file", "recent", "list"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_trash_list(
    path: Annotated[str | None, "回收站路径，默认全部"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出回收站内容。"""
    args = ["file", "trash", "list"]
    if path:
        args.append(path)
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_calc(
    path: Path,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """计算目录统计信息（递归统计，与 file_size 的单条目大小不同）。"""
    return _fmt(run_trim(["file", "calc", path], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_access(
    path: Path,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询当前用户对文件/目录的访问权限。"""
    return _fmt(run_trim(["file", "access", path], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_is_mount_point(
    path: Path,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """判断路径是否为挂载点。"""
    return _fmt(run_trim(["file", "is-mount-point", path], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_search_others(
    keyword: Annotated[str, "搜索关键词"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """搜索其他用户共享的文件。"""
    return _fmt(run_trim(["file", "search-others", keyword], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_task_cancel(
    task_id: Annotated[str, "后台任务 ID"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """取消文件后台任务（搜索/压缩等长任务，写操作）。"""
    return _fmt(run_trim(["file", "cancel", task_id] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_search_cancel(
    task_id: Annotated[str, "搜索任务 ID"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """取消全盘搜索任务（写操作）。"""
    return _fmt(run_trim(["file", "search-cancel", task_id] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_app_dir_list(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出应用专属目录（appdirs）。"""
    return _fmt(run_trim(["file", "app-dir-list"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_sys_part_info(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看系统分区信息。"""
    return _fmt(run_trim(["file", "sys-part-info"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_team_list(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出团队空间。"""
    return _fmt(run_trim(["file", "team", "list"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_team_trash_list(
    path: Annotated[str, "团队空间路径"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出指定团队空间的回收站。"""
    return _fmt(run_trim(["file", "team", "trash-list", path], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_team_trash_bin(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出团队空间垃圾桶（trashbin，与分空间回收站不同）。"""
    return _fmt(run_trim(["file", "team", "trash-list-trashbin"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_team_trash_restore(
    paths: Annotated[list[str], "团队空间回收站内文件路径列表"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """从团队空间回收站恢复文件（写操作）。"""
    return _fmt(run_trim(["file", "team", "trash-restore"] + paths + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_team_trash_clear(
    vol: Annotated[str, "卷名，如 1 或 /vol1"],
    team_dir: Annotated[str, "团队空间目录名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """清空团队空间回收站（危险写操作，不可恢复）。"""
    return _fmt(run_trim(["file", "team", "trash-clear", vol, team_dir] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_fav_list(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出收藏的文件。"""
    return _fmt(run_trim(["file", "fav", "list"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_fav_add(
    path: Path,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """收藏文件/目录（写操作）。"""
    return _fmt(run_trim(["file", "fav", "add", path] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_fav_del(
    path: Path,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """取消收藏（写操作）。"""
    return _fmt(run_trim(["file", "fav", "del", path] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_share_info(
    path: Path,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询文件/目录的共享信息。"""
    return _fmt(run_trim(["file", "share", "info", path], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_share_list(
    uid: Annotated[str | None, "用户 UID，默认当前用户"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出指定用户的文件共享。"""
    args = ["file", "share", "list"]
    if uid:
        args.append(uid)
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_share_list_others(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出其他用户的共享。"""
    return _fmt(run_trim(["file", "share", "list-others"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_share_admin_list(
    uid: Annotated[str | None, "用户 UID，缺省列出全部"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """管理员视角列出文件共享。"""
    args = ["file", "share", "admin-list"]
    if uid:
        args.append(uid)
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_share_admin_list_others(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """管理员视角列出其他用户的共享。"""
    return _fmt(run_trim(["file", "share", "admin-list-others"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_share_add(
    path: Path,
    share_name: Annotated[str, "共享名称"],
    permset: Annotated[str, "权限集 ID 或名称"],
    sub: bool = False,
    acl_mode: Annotated[str | None, "ACL 模式"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """创建文件共享（写操作）。"""
    args = ["file", "share", "add", path, share_name, "--permset", permset]
    if sub:
        args.append("--sub")
    if acl_mode:
        args += ["--acl-mode", acl_mode]
    args += _bool_flag(True)
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_share_del(
    path: Path,
    sub: bool = False,
    acl_mode: Annotated[str | None, "ACL 模式"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """删除文件共享（写操作）。"""
    args = ["file", "share", "del", path]
    if sub:
        args.append("--sub")
    if acl_mode:
        args += ["--acl-mode", acl_mode]
    args += _bool_flag(True)
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_acl_get(
    path: Path,
    prop: bool = False,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询文件 ACL 权限。"""
    args = ["file", "acl", "get", path]
    if prop:
        args.append("--prop")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_acl_set(
    path: Path,
    permset: Annotated[str, "权限集 ID 或名称"],
    sub: Annotated[int, "是否应用到子项：0 否 / 1 是"] = 0,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """设置文件 ACL 权限（写操作，可能批量影响子项）。"""
    return _fmt(run_trim(
        ["file", "acl", "set", path, "--permset", permset, "--sub", str(sub)] + _bool_flag(True),
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def file_trimacl_report(
    vol: Annotated[str, "卷名，如 1"],
    csv_path: Annotated[str, "报告保存路径（NAS 上）"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """导出卷 ACL 迁移报告（写操作，生成 CSV 报告文件）。"""
    return _fmt(run_trim(["file", "trimacl", "report", vol, csv_path] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


# ----- 文件写操作（一律显式 --yes） -----

@mcp.tool
def file_mkdir(
    path: Path,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """创建目录（写操作）。"""
    return _fmt(run_trim(["file", "mkdir", path] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_rename(
    path: Path,
    name: Annotated[str, "新名称（不含路径）"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """重命名文件/目录（写操作）。"""
    return _fmt(run_trim(["file", "rename", path, name] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_copy(
    src: Annotated[str, "源路径"],
    dest_dir: Annotated[str, "目标目录"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """复制文件到目标目录（写操作）。"""
    return _fmt(run_trim(["file", "cp", src, dest_dir] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_move(
    src: Annotated[str, "源路径"],
    dest_dir: Annotated[str, "目标目录"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """移动文件到目标目录（写操作）。"""
    return _fmt(run_trim(["file", "mv", src, dest_dir] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_delete(
    path: Path,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """删除文件/目录（进入回收站，写操作）。"""
    return _fmt(run_trim(["file", "rm", path] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_trash_restore(
    path: Path,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """从回收站恢复文件（写操作）。"""
    return _fmt(run_trim(["file", "trash", "restore", path] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_trash_clear(
    path: Annotated[str | None, "回收站路径，默认清空全部"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """清空回收站（危险写操作，不可恢复）。"""
    args = ["file", "trash", "clear"]
    if path:
        args.append(path)
    return _fmt(run_trim(args + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def file_check_upload(
    path: Path,
    size: Annotated[int, "文件大小（字节）"],
    overwrite: Literal["skip", "replace", "rename"] = "replace",
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """预检上传目标（写操作：探测远端占用与配额）。

    返回远端同名文件状态与空间配额信息，用于在真正上传前确认
    overwrite 策略。size 为计划上传的本地文件大小（字节）。
    """
    return _fmt(run_trim(
        ["file", "check-upload", path, str(size), "--overwrite", overwrite] + _bool_flag(True),
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def file_upload(
    local_file: Annotated[str, "本地文件路径（服务器所在机器）"],
    remote_dir: Annotated[str, "NAS 目标目录，如 /vol1/1000/文档"],
    overwrite: Literal["skip", "replace", "rename"] = "replace",
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """上传本地文件到 NAS 目录（写操作）。

    overwrite 决定远端同名文件冲突策略：skip 跳过、replace 覆盖、
    rename 保留旧文件并给新文件改名。上传前可先用 file_check_upload
    预检目标状态。
    """
    return _fmt(run_trim(
        ["file", "upload", remote_dir, local_file, "--overwrite", overwrite] + _bool_flag(True),
        profile=profile, host=host, port=port, scheme=scheme,
        timeout=600,
    ))


@mcp.tool
def file_write_text(
    path: Annotated[str, "NAS 目标文件路径，如 /vol1/1000/文档/note.md"],
    content: Annotated[str, "写入的文本内容"],
    overwrite: Literal["skip", "replace", "rename"] = "replace",
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """把文本内容直接写入 NAS 文件（写操作；组合 upload 实现）。

    等价于在服务器临时文件上执行 file upload：远端父目录不存在时会
    先用 file_mkdir 逐级创建。overwrite=skip 时远端同名文件已存在则
    跳过不写。适合写配置、脚本、compose 文件等小文本。
    """
    import os
    import tempfile

    if "/" not in path:
        return "路径无效：必须是 /vol{v}/... 形式的完整文件路径"
    dirpart, name = path.rsplit("/", 1)
    if not dirpart or not name:
        return "路径无效：必须是 /vol{v}/... 形式的完整文件路径"

    fd, tmp = tempfile.mkstemp(prefix="fnos-mcp-write-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        # 父目录缺失时逐级创建（file mkdir 支持递归建目录）
        parent_check = run_trim(
            ["file", "prop", dirpart], profile=profile, host=host, port=port, scheme=scheme,
        )
        if not parent_check["ok"]:
            mkdir_result = run_trim(
                ["file", "mkdir", dirpart] + _bool_flag(True),
                profile=profile, host=host, port=port, scheme=scheme,
            )
            if not mkdir_result["ok"]:
                return _fmt(mkdir_result)
        result = run_trim(
            ["file", "upload", dirpart, tmp, "--overwrite", overwrite] + _bool_flag(True),
            profile=profile, host=host, port=port, scheme=scheme,
            timeout=600,
        )
        if result["ok"]:
            return _fmt(result) + f"\n已写入 {path}（{len(content.encode('utf-8'))} 字节）"
        return _fmt(result)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


@mcp.tool
def file_compress(
    output_path: Annotated[str, "输出压缩包路径（NAS 上），如 /vol1/1000/backup.zip"],
    source_paths: Annotated[list[str], "源文件/目录路径列表（NAS 上）"],
    level: Annotated[int | None, "压缩级别 0-9"] = None,
    password: Annotated[str | None, "压缩包密码"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """压缩文件/目录为压缩包（写操作）。"""
    args = ["file", "compress", output_path] + source_paths
    if level is not None:
        args += ["--level", str(level)]
    if password:
        args += ["--password", password]
    args += _bool_flag(True)
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme, timeout=1800))


@mcp.tool
def file_extract(
    archive_path: Annotated[str, "压缩包路径（NAS 上）"],
    output_dir: Annotated[str, "解压目标目录（NAS 上）"],
    overwrite: Literal["skip", "replace", "rename"] = "replace",
    password: Annotated[str | None, "压缩包密码（加密包需要）"] = None,
    from_charset: Annotated[str | None, "压缩包内文件名原始编码，如 gbk"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """解压压缩包到指定目录（写操作）。"""
    args = ["file", "extract", archive_path, output_dir, "--overwrite", overwrite]
    if password:
        args += ["--password", password]
    if from_charset:
        args += ["--from-charset", from_charset]
    args += _bool_flag(True)
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme, timeout=1800))


@mcp.tool
def file_chown(
    path: Path,
    uid: Annotated[str, "目标 UID（用户 ID）"],
    sub: bool = False,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """修改文件/目录属主（写操作；sub=True 递归应用到子项）。"""
    args = ["file", "chown", path, "--uid", uid]
    if sub:
        args.append("--sub")
    args += _bool_flag(True)
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


# ---------------------------------------------------------------------------
# 相册 Photos
# ---------------------------------------------------------------------------

@mcp.tool
def photos_folders(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出相册文件夹。"""
    return _fmt(run_trim(["photos", "folders"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def photos_search(
    keyword: Annotated[str, "搜索关键词（含 AI 搜图）"],
    limit: Annotated[int, "最大返回条数"] = 20,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """搜索相册照片（支持 AI 语义搜图；total 才是匹配总数）。"""
    return _fmt(run_trim(
        ["photos", "search", keyword, "--limit", str(limit)],
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def photos_detail(
    photo_id: Annotated[str, "照片 ID"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """获取照片详情。"""
    return _fmt(run_trim(["photos", "info", photo_id], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def photos_preview_url(
    photo_id: Annotated[str, "照片 ID"],
    size: Literal["xxs", "xs", "s", "m", "o"] = "m",
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """获取照片预览 URL（通常需浏览器已登录 cookie）。"""
    return _fmt(run_trim(
        ["photos", "preview", photo_id, "--size", size],
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def photos_raw_request(
    method: Literal["get", "post", "put", "patch", "delete"],
    api_path: Annotated[str, "Photos 原生 API 路径"],
    query: Annotated[dict[str, str] | None, "URL 查询参数"] = None,
    body: Annotated[dict | None, "JSON 请求体"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """调用 Photos 原生 HTTP API（高级用法；POST/PUT/PATCH/DELETE 需确认）。"""
    args = ["photos", "request", method, api_path]
    if query:
        for k, v in query.items():
            args += ["--query", f"{k}={v}"]
    if body is not None:
        args += ["--json", json.dumps(body, ensure_ascii=False)]
    if method in ("post", "put", "patch", "delete"):
        args.append("--yes")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


# ---------------------------------------------------------------------------
# 应用中心
# ---------------------------------------------------------------------------

@mcp.tool
def app_list(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出已安装的应用。"""
    return _fmt(run_trim(["app", "list"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_status(
    app_name: Annotated[str, "应用名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询单个应用状态。"""
    return _fmt(run_trim(["app", "status", app_name], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_store_search(
    keyword: Annotated[str, "搜索关键词"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """在应用商店搜索应用。"""
    return _fmt(run_trim(["app", "store", "search", keyword], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_store_detail(
    app_name: Annotated[str, "应用名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看应用商店中的应用详情。"""
    return _fmt(run_trim(["app", "store", "detail", app_name], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_check_update(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """检查已安装应用的更新。"""
    return _fmt(run_trim(["app", "check-update"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_install(
    app_name: Annotated[str, "应用名"],
    version: Annotated[str, "要安装的版本"],
    source_id: Annotated[str | None, "来源 ID"] = None,
    volume_id: Annotated[str | None, "安装目标卷 ID"] = None,
    data_volume_id: Annotated[str | None, "数据卷 ID"] = None,
    accept_license: bool = True,
    no_start: bool = False,
    dry_run: bool = False,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """安装应用（写操作）。dry_run=True 时仅预演不执行。"""
    args = ["app", "install", app_name, "--version", version]
    if source_id:
        args += ["--source-id", source_id]
    if volume_id:
        args += ["--volume-id", volume_id]
    if data_volume_id:
        args += ["--data-volume-id", data_volume_id]
    if accept_license:
        args.append("--accept-license")
    if no_start:
        args.append("--no-start")
    if dry_run:
        args.append("--dry-run")
    args.append("--yes")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_uninstall(
    app_name: Annotated[str, "应用名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """卸载应用（写操作）。"""
    return _fmt(run_trim(["app", "uninstall", app_name] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_start(
    app_name: Annotated[str, "应用名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """启动应用（写操作）。"""
    return _fmt(run_trim(["app", "start", app_name] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_stop(
    app_name: Annotated[str, "应用名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """停止应用（写操作）。"""
    return _fmt(run_trim(["app", "stop", app_name] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_restart(
    app_name: Annotated[str, "应用名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """重启应用（写操作）。"""
    return _fmt(run_trim(["app", "restart", app_name] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_update(
    app_name: Annotated[str, "应用名"],
    version: Annotated[str, "目标版本"],
    volume_id: Annotated[str | None, "安装目标卷 ID"] = None,
    data_volume_id: Annotated[str | None, "数据卷 ID"] = None,
    accept_license: bool = True,
    no_start: bool = False,
    dry_run: bool = False,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """升级应用到指定版本（写操作）。dry_run=True 仅预演。"""
    args = ["app", "update", app_name, "--version", version]
    if volume_id:
        args += ["--volume-id", volume_id]
    if data_volume_id:
        args += ["--data-volume-id", data_volume_id]
    if accept_license:
        args.append("--accept-license")
    if no_start:
        args.append("--no-start")
    if dry_run:
        args.append("--dry-run")
    args.append("--yes")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_task_status(
    task_id: Annotated[str, "应用任务 ID（安装/更新等）"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询应用任务进度（安装/更新/卸载等）。"""
    return _fmt(run_trim(["app", "task-status", task_id], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_task_cancel(
    task_id: Annotated[str, "应用任务 ID"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """取消应用任务（写操作）。"""
    return _fmt(run_trim(["app", "task-cancel", task_id] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_install_cancel(
    task_id: Annotated[str, "安装任务 ID"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """取消应用安装任务（写操作）。"""
    return _fmt(run_trim(["app", "install-cancel", task_id] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_update_cancel(
    task_id: Annotated[str, "更新任务 ID"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """取消应用更新任务（写操作）。"""
    return _fmt(run_trim(["app", "update-cancel", task_id] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_download_cancel(
    task_id: Annotated[str, "应用下载任务 ID"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """取消应用商店下载任务（写操作）。"""
    return _fmt(run_trim(["app", "download", "cancel", task_id] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_install_fpk(
    remote_path: Annotated[str, "NAS 上的 fpk 安装包路径"] = "",
    local_file: Annotated[str | None, "服务器本地 fpk 文件路径（二选一）"] = None,
    volume_id: Annotated[str | None, "安装目标卷 ID"] = None,
    data_volume_id: Annotated[str | None, "数据卷 ID"] = None,
    custom_parameters: Annotated[list | None, "自定义参数（jsonArray）"] = None,
    api_scope: Annotated[dict | None, "API 权限范围（jsonObject）"] = None,
    accept_license: bool = True,
    cancel_on_failure: bool = False,
    no_start: bool = False,
    dry_run: bool = False,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """从 fpk 离线安装包安装应用（写操作；remote_path 或 local_file 二选一）。"""
    args = ["app", "install-fpk"]
    if local_file:
        args.append(local_file)
    if remote_path:
        args += ["--remote-path", remote_path]
    if volume_id:
        args += ["--volume-id", volume_id]
    if data_volume_id:
        args += ["--data-volume-id", data_volume_id]
    if custom_parameters is not None:
        args += ["--custom-parameters", json.dumps(custom_parameters, ensure_ascii=False)]
    if api_scope is not None:
        args += ["--api-scope", json.dumps(api_scope, ensure_ascii=False)]
    if accept_license:
        args.append("--accept-license")
    if cancel_on_failure:
        args.append("--cancel-on-failure")
    if no_start:
        args.append("--no-start")
    if dry_run:
        args.append("--dry-run")
    args.append("--yes")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_task_action(
    task_id: Annotated[str, "任务 ID"],
    action: Literal["task-cancel", "install-cancel", "update-cancel", "download-cancel"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """按类型取消应用任务（写操作，聚合入口）。

    action 对应 CLI 子命令：task-cancel / install-cancel /
    update-cancel / app download cancel。
    """
    if action == "download-cancel":
        args = ["app", "download", "cancel", task_id]
    else:
        args = ["app", action, task_id]
    return _fmt(run_trim(args + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_config_list(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出所有应用的配置项。"""
    return _fmt(run_trim(["app", "config", "list"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_config_detail(
    app_name: Annotated[str, "应用名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看指定应用的配置项详情。"""
    return _fmt(run_trim(["app", "config", "detail", app_name], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_config_sys(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看应用中心系统配置。"""
    return _fmt(run_trim(["app", "config", "sys"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_config_auth_paths(
    app_name: Annotated[str, "应用名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看应用的鉴权路径配置。"""
    return _fmt(run_trim(["app", "config", "auth-paths", app_name], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_config_wizard(
    app_name: Annotated[str, "应用名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """获取应用配置向导数据（安装向导表单定义）。"""
    return _fmt(run_trim(["app", "config", "wizard", app_name], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_config_set(
    app_name: Annotated[str, "应用名"],
    fields: Annotated[dict, "要写入的配置字段（JSON object）"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """修改应用配置项（写操作）。fields 为 JSON object 透传。"""
    return _fmt(run_trim(
        ["app", "config", "set", app_name, "--fields", json.dumps(fields, ensure_ascii=False)] + _bool_flag(True),
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def app_config_set_sys(
    fields: Annotated[dict, "要写入的系统配置字段（JSON object）"],
    allow_desktop_icon_field: bool = False,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """修改应用中心系统配置（写操作，影响全局应用行为）。"""
    args = ["app", "config", "set-sys", "--fields", json.dumps(fields, ensure_ascii=False)]
    if allow_desktop_icon_field:
        args.append("--allow-desktop-icon-field")
    args += _bool_flag(True)
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_config_auto_update(
    app_name: Annotated[str, "应用名"],
    enable: bool,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """开启/关闭应用自动更新（写操作）。"""
    flag = "--enable" if enable else "--disable"
    return _fmt(run_trim(
        ["app", "config", "auto-update", app_name, flag, "--yes"],
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def app_config_proxy_access(
    app_name: Annotated[str, "应用名"],
    allow: bool,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """允许/禁止应用的外网代理访问（写操作）。"""
    flag = "--allowed" if allow else "--denied"
    return _fmt(run_trim(
        ["app", "config", "proxy-access", app_name, flag, "--yes"],
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def app_openapi_app_detail(
    app_name: Annotated[str, "应用名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询应用 OpenAPI 详情。"""
    return _fmt(run_trim(["app", "openapi", "app-detail", app_name], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_openapi_allow_auth_paths(
    app_name: Annotated[str, "应用名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询应用 OpenAPI 允许的鉴权路径。"""
    return _fmt(run_trim(["app", "openapi", "allow-user-auth-paths", app_name], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_openapi_add_auth_path(
    app_name: Annotated[str, "应用名"],
    path: Annotated[str, "鉴权路径"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """为应用添加 OpenAPI 鉴权路径（写操作）。"""
    return _fmt(run_trim(["app", "openapi", "add-user-auth-path", app_name, path] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_openapi_add_auth_inherit_file(
    app_name: Annotated[str, "应用名"],
    path: Annotated[str, "鉴权继承文件路径"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """为应用添加 OpenAPI 鉴权继承文件（写操作）。"""
    return _fmt(run_trim(["app", "openapi", "add-user-auth-inherit-file", app_name, path] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_store_list(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出应用商店全部应用。"""
    return _fmt(run_trim(["app", "store", "list"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_store_latest_release(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """获取应用商店最新上架应用。"""
    return _fmt(run_trim(["app", "store", "latest-release"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_guide_check_installed(
    app_names: Annotated[list[str], "应用名列表"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """批量检查应用是否已安装（装机引导）。"""
    return _fmt(run_trim(["app", "guide", "check-installed"] + app_names, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_guide_batch_install(
    volume_id: Annotated[str, "安装目标卷 ID"],
    app_names: Annotated[list[str], "应用名列表"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """批量安装商店应用（写操作，装机引导流程）。"""
    return _fmt(run_trim(
        ["app", "guide", "batch-download-install", volume_id] + app_names + _bool_flag(True),
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def app_base_data(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """获取应用中心基础数据。"""
    return _fmt(run_trim(["app", "base-data"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_sac_store_list(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """获取 SAC 应用商店列表（应用推荐数据源）。"""
    return _fmt(run_trim(["app", "sac", "app-store-list"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_service_list(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出应用提供的服务。"""
    return _fmt(run_trim(["app", "service", "list"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_entry_list(
    only_hidden: bool = False,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出应用入口；only_hidden=True 仅列出隐藏入口。"""
    args = ["app", "entry", "list"]
    if only_hidden:
        args.append("--only-hidden")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_desktop_list(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出桌面应用图标。"""
    return _fmt(run_trim(["app", "desktop", "list"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_operation_settings(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看应用运营设置。"""
    return _fmt(run_trim(["app", "operation", "settings"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def app_common_volume(
    set_volume: Annotated[str | None, "要设置的应用公共卷（写操作，缺省仅查询）"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询/设置应用公共卷（set_volume 非空时为写操作）。"""
    args = ["app", "common", "volume"]
    if set_volume:
        args += ["--set", set_volume, "--yes"]
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

@mcp.tool
def docker_containers(
    all_containers: Annotated[bool, "包含已停止/已创建的容器"] = False,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出 Docker 容器。"""
    args = ["docker", "container", "ls"]
    if all_containers:
        args.append("--all")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def docker_container_action(
    container_id: Annotated[str, "容器 ID"],
    action: Literal["start", "stop", "restart", "kill"] = "restart",
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """对容器执行操作（写操作）：start / stop / restart / kill。"""
    return _fmt(run_trim(
        ["docker", "container", action, container_id] + _bool_flag(True),
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def docker_container_rm(
    container_id: Annotated[str, "容器 ID"],
    force: bool = False,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """删除容器（危险写操作；force=True 强制删除运行中的容器）。"""
    args = ["docker", "container", "rm", container_id]
    if force:
        args.append("--force")
    args.append("--yes")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def docker_images(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出 Docker 镜像。"""
    return _fmt(run_trim(["docker", "image", "ls"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def docker_image_pull(
    image_ref: Annotated[str, "镜像引用，如 nginx:latest"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """拉取 Docker 镜像（写操作）。"""
    return _fmt(run_trim(["docker", "image", "pull", image_ref] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def docker_image_rm(
    image_ref: Annotated[str, "镜像引用"],
    force: bool = False,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """删除 Docker 镜像（危险写操作）。"""
    args = ["docker", "image", "rm", image_ref]
    if force:
        args.append("--force")
    args.append("--yes")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def docker_stats(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看 Docker 资源占用统计。"""
    return _fmt(run_trim(["docker", "stats"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def docker_compose_ls(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出 Docker Compose 项目。"""
    return _fmt(run_trim(["docker", "compose", "ls"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def docker_request(
    endpoint: Annotated[str, "appcgi.dockermgr.* 端点名，如 composeContainers"],
    body: Annotated[dict | None, "JSON 请求体（object，不含 req/reqid）"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """调用任意 appcgi.dockermgr.* 端点（写操作，兜底入口）。

    固定命令未覆盖的 Docker 端点走这里：networkList、composeContainers、
    composeStart/Stop 等。endpoint 只需端点名（CLI 自动加前缀校验），
    body 为 JSON object 且不能包含 req/reqid（CLI 自动生成）。
    Compose/网络/系统类写操作前先做只读探测（如 composeList）。
    """
    endpoint = endpoint.removeprefix("appcgi.dockermgr.")
    args = ["docker", "request", f"appcgi.dockermgr.{endpoint}"]
    if body is not None:
        args += ["--json", json.dumps(body, ensure_ascii=False)]
    args.append("--yes")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def docker_compose_create(
    body: Annotated[dict, "composeCreate 请求体（object，不含 req/reqid；上游 schema 未完全标准化，透传 fnOS API）"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """创建 Docker Compose 项目（写操作；上游 composeCreate 透传封装）。

    固定子命令只有 compose ls，创建/启停走泛化端点
    appcgi.dockermgr.composeCreate。body 为 fnOS API 的 JSON object，
    不能包含 req/reqid。上游 schema 未完全文档化，字段以真机响应为准；
    典型流程：先 docker_compose_ls 探测现有项目，创建后用
    docker_request(composeContainers) 验证。
    """
    args = ["docker", "request", "appcgi.dockermgr.composeCreate",
            "--json", json.dumps(body, ensure_ascii=False), "--yes"]
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def docker_container_create(
    image: Annotated[str, "镜像引用，如 nginx:latest"],
    name: Annotated[str | None, "容器名"] = None,
    start: Annotated[bool, "创建后立即启动"] = False,
    restart: Annotated[bool, "设置 restart 策略"] = False,
    memory_mb: Annotated[int | None, "内存上限（MB）"] = None,
    cpu_shares: Annotated[int | None, "CPU 权重（shares）"] = None,
    env: Annotated[list[str] | None, "环境变量，如 TZ=Asia/Shanghai"] = None,
    cmd: Annotated[list[str] | None, "容器命令参数"] = None,
    port_mappings: Annotated[list[str] | None, "端口映射，如 8080:80"] = None,
    mount_mappings: Annotated[list[str] | None, "挂载映射，如 /vol1/1000/app:/data"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """创建 Docker 容器（写操作）。

    支持端口/挂载映射、环境变量、内存/CPU 限制与启动策略。
    创建后可用 docker_containers(--all) 与 docker_container_action 管理。
    """
    args = ["docker", "container", "create", "--image", image]
    if name:
        args += ["--name", name]
    if start:
        args.append("--start")
    if restart:
        args.append("--restart")
    if memory_mb is not None:
        args += ["--memory", str(memory_mb)]
    if cpu_shares is not None:
        args += ["--cpu", str(cpu_shares)]
    for e in env or []:
        args += ["--env", e]
    for c in cmd or []:
        args += ["--cmd", c]
    for p in port_mappings or []:
        args += ["--port", p]
    for m in mount_mappings or []:
        args += ["--mount", m]
    args.append("--yes")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def docker_container_inspect(
    container_id: Annotated[str, "容器 ID"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看容器详细配置（inspect）。"""
    return _fmt(run_trim(["docker", "container", "inspect", container_id], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def docker_container_top(
    container_id: Annotated[str, "容器 ID"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看容器内进程列表（top）。"""
    return _fmt(run_trim(["docker", "container", "top", container_id], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def docker_container_stats(
    container_id: Annotated[str, "容器 ID"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看单个容器的资源占用统计。"""
    return _fmt(run_trim(["docker", "container", "stats", container_id], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def docker_container_update(
    container_id: Annotated[str, "容器 ID"],
    restart: bool = False,
    memory_mb: Annotated[int | None, "内存上限（MB）"] = None,
    cpu_shares: Annotated[int | None, "CPU 权重（shares）"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """更新容器配置（写操作）：重启策略 / 内存 / CPU 限制。"""
    args = ["docker", "container", "update", container_id]
    if restart:
        args.append("--restart")
    if memory_mb is not None:
        args += ["--memory", str(memory_mb)]
    if cpu_shares is not None:
        args += ["--cpu", str(cpu_shares)]
    args.append("--yes")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def docker_image_inspect(
    image_ref: Annotated[str, "镜像引用，如 nginx:latest"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看镜像详细信息（inspect）。"""
    return _fmt(run_trim(["docker", "image", "inspect", image_ref], profile=profile, host=host, port=port, scheme=scheme))


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------

@mcp.tool
def download_list(
    keyword: Annotated[str | None, "按关键词过滤任务"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出下载任务。"""
    args = ["download", "ls"]
    if keyword:
        args.append(keyword)
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def download_info(
    task_id: Annotated[str, "任务 ID"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看下载任务详情。"""
    return _fmt(run_trim(["download", "info", task_id], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def download_files(
    task_id: Annotated[str, "任务 ID"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看下载任务的文件列表。"""
    return _fmt(run_trim(["download", "files", task_id], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def download_add_uri(
    uri: Annotated[str, "下载地址（http/ftp/magnet 等）"],
    save_dir: Annotated[str, "保存目录，如 /vol1/1000/downloads"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """新建下载任务（写操作）。"""
    return _fmt(run_trim(["download", "add-uri", uri, save_dir] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def download_action(
    task_ids: Annotated[list[str], "任务 ID 列表"],
    action: Literal["pause", "resume", "retry", "rm"] = "pause",
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """批量操作下载任务（写操作）：pause / resume / retry / rm。"""
    return _fmt(run_trim(
        ["download", action] + task_ids + _bool_flag(True),
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def download_stats(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看下载中心全局统计。"""
    return _fmt(run_trim(["download", "stat"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def download_add_path(
    path: Annotated[str, "NAS 上已有的种子/文件路径"],
    save_dir: Annotated[str, "保存目录"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """从 NAS 本地路径新建下载任务（写操作，如 .torrent 文件）。"""
    return _fmt(run_trim(["download", "add-path", path, save_dir] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def download_request(
    endpoint: Annotated[str, "下载中心原始端点"],
    body: Annotated[dict | None, "JSON 请求体"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """调用下载中心原始 API（兜底；写方法自动带 --yes）。"""
    args = ["download", "request", endpoint]
    if body is not None:
        args += ["--json", json.dumps(body, ensure_ascii=False)]
    args.append("--yes")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

@mcp.tool
def logger_list(
    page: int = 1,
    page_size: int = 20,
    level: Annotated[str | None, "日志级别，如 info/warning/error"] = None,
    module: Annotated[str | None, "模块名过滤"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """分页查询系统日志。"""
    args = ["logger", "list", "--page", str(page), "--page-size", str(page_size)]
    if level:
        args += ["--level", level]
    if module:
        args += ["--module", module]
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def logger_modules(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出日志模块。"""
    return _fmt(run_trim(["logger", "modules"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def logger_export(
    level: Annotated[str, "日志级别"],
    module: Annotated[str, "模块名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """导出日志（写操作）。"""
    return _fmt(run_trim(["logger", "export", "--level", level, "--module", module, "--yes"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def logger_clear(
    level: Annotated[str, "日志级别，如 info/warning/error"],
    module: Annotated[str, "模块名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """清除指定级别/模块的日志（危险写操作，不可恢复）。"""
    return _fmt(run_trim(["logger", "clear", "--level", level, "--module", module, "--yes"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def logger_archive_query(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询日志归档配置。"""
    return _fmt(run_trim(["logger", "archive", "query"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def logger_archive_set(
    switch: Annotated[str, "归档开关状态值"],
    file_path: Annotated[str, "归档保存路径（NAS 上）"],
    size_gt: Annotated[str | None, "大小阈值"] = None,
    date_unit: Annotated[str | None, "日期单位"] = None,
    date_before: Annotated[str | None, "归档日期阈值"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """设置日志归档配置（写操作；switch/file_path 必填，其余可选）。"""
    args = ["logger", "archive", "set", "--switch", switch, "--file-path", file_path]
    if size_gt:
        args += ["--size-gt", size_gt]
    if date_unit:
        args += ["--date-unit", date_unit]
    if date_before:
        args += ["--date-before", date_before]
    args.append("--yes")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def logger_debuglog(
    output_dir: Annotated[str, "服务器本地输出目录"],
    srv_type: Annotated[str | None, "服务类型过滤"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """开始复制调试日志到本地目录（写操作，生成诊断数据）。"""
    args = ["logger", "debuglog", "copy-start", "--output-dir", output_dir]
    if srv_type:
        args += ["--srv-type", srv_type]
    args.append("--yes")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


# ---------------------------------------------------------------------------
# 监控
# ---------------------------------------------------------------------------

@mcp.tool
def monitor_cpu(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看 CPU 使用率。"""
    return _fmt(run_trim(["monitor", "cpu"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def monitor_memory(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看内存使用率。"""
    return _fmt(run_trim(["monitor", "memory"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def monitor_network(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看网络流量。"""
    return _fmt(run_trim(["monitor", "net"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def monitor_disk(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看磁盘 IO 统计。"""
    return _fmt(run_trim(["monitor", "disk"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def monitor_gpu(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看 GPU 使用率。"""
    return _fmt(run_trim(["monitor", "gpu"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def monitor_processes(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出系统进程。"""
    return _fmt(run_trim(["monitor", "proc-list"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def monitor_warnings(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看系统告警。"""
    return _fmt(run_trim(["monitor", "sys-warn"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def monitor_npu(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看 NPU 使用率。"""
    return _fmt(run_trim(["monitor", "npu"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def monitor_battery(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看电池状态（笔记本/UPS 场景）。"""
    return _fmt(run_trim(["monitor", "battery"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def monitor_fan(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看系统风扇转速。"""
    return _fmt(run_trim(["monitor", "sys-fan"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def monitor_proc_info(
    pids: Annotated[list[int], "进程 PID 列表（至少一个）"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询指定进程的详细监控信息。"""
    return _fmt(run_trim(
        ["monitor", "proc-info", "--pids", ",".join(str(p) for p in pids)],
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def monitor_proc_services(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询系统服务进程监控。"""
    return _fmt(run_trim(["monitor", "proc-srv"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def monitor_beep_supported(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询是否支持蜂鸣器。"""
    return _fmt(run_trim(["monitor", "beep-supported"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def monitor_beep_events(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询蜂鸣器事件记录。"""
    return _fmt(run_trim(["monitor", "beep-events"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def monitor_beep_reasons(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询蜂鸣器鸣叫原因。"""
    return _fmt(run_trim(["monitor", "beep-reasons"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def monitor_mute_beeper(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """静音蜂鸣器（写操作）。"""
    return _fmt(run_trim(["monitor", "mute-beeper", "--yes"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def monitor_metrics(
    items: Annotated[
        list[Literal["storeSpeed", "netSpeed", "cpuBusy", "memPercent"]],
        "要查询的指标列表（至少一个）",
    ],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """按指标名批量查询监控数据（gen 通用入口）。

    可选指标：storeSpeed（存储吞吐）、netSpeed（网速）、
    cpuBusy（CPU 忙碌）、memPercent（内存占用）。
    """
    args = ["monitor", "gen"]
    for i in items:
        args += ["--item", i]
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------

@mcp.tool
def network_ssh_status(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询 SSH 服务状态。"""
    # CLI 无独立 status 子命令，通过 switch 不带参数时的输出判断
    result = run_trim(["network", "ssh", "switch"], profile=profile, host=host, port=port, scheme=scheme)
    return _fmt(result)


@mcp.tool
def network_ssh_switch(
    enable: bool,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """启用或禁用 SSH 服务（写操作）。"""
    flag = "--enable" if enable else "--disable"
    return _fmt(run_trim(
        ["network", "ssh", "switch", flag, "--yes"],
        profile=profile, host=host, port=port, scheme=scheme,
    ))


# ---------------------------------------------------------------------------
# 电源
# ---------------------------------------------------------------------------

@mcp.tool
def power_reboot(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """重启 NAS（危险写操作）。"""
    return _fmt(run_trim(["power", "reboot", "--yes"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def power_poweroff(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """关闭 NAS（危险写操作）。"""
    return _fmt(run_trim(["power", "poweroff", "--yes"], profile=profile, host=host, port=port, scheme=scheme))


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------

@mcp.tool
def storage_overview(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """存储总览。"""
    return _fmt(run_trim(["storage", "overview"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def storage_pools(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出存储池。"""
    return _fmt(run_trim(["storage", "pools"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def storage_disks(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出硬盘。"""
    return _fmt(run_trim(["storage", "disks"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def storage_disk_health(
    disk: Annotated[str, "硬盘标识，如 sda"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看硬盘健康状态。"""
    return _fmt(run_trim(["storage", "health", disk], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def storage_disk_smart(
    disk: Annotated[str, "硬盘标识，如 sda"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看硬盘 SMART 信息。"""
    return _fmt(run_trim(["storage", "smart", disk], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def storage_space(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看存储空间容量。"""
    return _fmt(run_trim(["storage", "space"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def storage_free_disks(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出未加入存储池的空闲硬盘。"""
    return _fmt(run_trim(["storage", "free-disks"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def storage_removable(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出可移动设备（U 盘/移动硬盘）。"""
    return _fmt(run_trim(["storage", "removable"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def storage_disk_info(
    disk: Annotated[str, "硬盘标识，如 sda"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看硬盘详细信息。"""
    return _fmt(run_trim(["storage", "disk-info", disk], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def storage_mount(
    uuid: Annotated[str, "存储空间 UUID"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """挂载存储空间（写操作）。"""
    return _fmt(run_trim(["storage", "mount", uuid] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def storage_umount(
    uuid: Annotated[str, "存储空间 UUID"],
    admin_password: Annotated[str | None, "管理员密码（系统要求时）"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """卸载存储空间（写操作；部分场景需管理员密码）。"""
    args = ["storage", "umount", uuid]
    if admin_password:
        args += ["--password", admin_password]
    args += _bool_flag(True)
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def storage_disk_mount(
    disk: Annotated[str, "硬盘标识，如 sdb"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """挂载独立硬盘（写操作）。"""
    return _fmt(run_trim(["storage", "disk-mount", disk] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def storage_disk_umount(
    disk: Annotated[str, "硬盘标识，如 sdb"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """卸载独立硬盘（写操作）。"""
    return _fmt(run_trim(["storage", "disk-umount", disk] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


# ---------------------------------------------------------------------------
# 用户
# ---------------------------------------------------------------------------

@mcp.tool
def user_list(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出 NAS 用户。"""
    return _fmt(run_trim(["user", "list"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def user_info(
    user: Annotated[str | None, "用户名，缺省为当前登录用户"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询用户信息。"""
    args = ["user", "info"]
    if user:
        args.append(user)
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def user_groups(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出用户组。"""
    return _fmt(run_trim(["user", "group-list"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def user_group_info(
    group: Annotated[str, "用户组名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询用户组详情。"""
    return _fmt(run_trim(["user", "group-info", group], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def user_frozen_list(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出被冻结的账号。"""
    return _fmt(run_trim(["user", "frozen-list"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def user_list_ug(
    users: bool = True,
    groups: bool = True,
    uver: Annotated[int | None, "数据版本号（增量同步用），如 0"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出用户与用户组（可带版本号做增量查询）。"""
    args = ["user", "list-ug"]
    if users:
        args.append("--users")
    if groups:
        args.append("--groups")
    if uver is not None:
        args += ["--uver", str(uver)]
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def user_group_users(
    uver: Annotated[int, "数据版本号（首次传 0），如 0"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询用户组成员关系（按版本增量）。"""
    return _fmt(run_trim(["user", "group-users", "--uver", str(uver)], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def user_login_devices(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出当前用户的登录设备。"""
    return _fmt(run_trim(["user", "list-login-device"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def user_group_add(
    group: Annotated[str, "新用户组名"],
    comment: Annotated[str | None, "备注"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """创建用户组（写操作）。"""
    args = ["user", "group-add", group]
    if comment:
        args += ["--comment", comment]
    args += _bool_flag(True)
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def user_group_mod(
    group: Annotated[str, "用户组名"],
    new_name: Annotated[str | None, "新组名"] = None,
    comment: Annotated[str | None, "新备注"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """修改用户组（写操作）。"""
    args = ["user", "group-mod", group]
    if new_name:
        args += ["--new-name", new_name]
    if comment:
        args += ["--comment", comment]
    args += _bool_flag(True)
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def user_group_del(
    group: Annotated[str, "用户组名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """删除用户组（危险写操作）。"""
    return _fmt(run_trim(["user", "group-del", group] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def user_group_set_users(
    group: Annotated[str, "用户组名"],
    users: Annotated[list[str], "成员用户名列表（全量覆盖）"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """整组设置用户组成员（写操作，覆盖原有成员）。"""
    return _fmt(run_trim(
        ["user", "group-set-users", group, "--users", ",".join(users)] + _bool_flag(True),
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def user_group_add_users(
    group: Annotated[str, "用户组名"],
    users: Annotated[list[str], "要添加的用户名列表"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """向用户组添加成员（写操作）。"""
    return _fmt(run_trim(
        ["user", "group-add-users", group, "--users", ",".join(users)] + _bool_flag(True),
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def user_group_del_users(
    group: Annotated[str, "用户组名"],
    users: Annotated[list[str], "要移除的用户名列表"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """从用户组移除成员（写操作）。"""
    return _fmt(run_trim(
        ["user", "group-del-users", group, "--users", ",".join(users)] + _bool_flag(True),
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def user_add(
    user: Annotated[str, "新用户名"],
    password: Annotated[str | None, "初始密码"] = None,
    groups: Annotated[list[str] | None, "所属用户组列表"] = None,
    comment: Annotated[str | None, "备注"] = None,
    email: Annotated[str | None, "邮箱"] = None,
    mobile: Annotated[str | None, "手机号"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """创建 NAS 用户（写操作）。"""
    args = ["user", "add", user]
    if password:
        args += ["--password", password]
    if groups:
        args += ["--groups", ",".join(groups)]
    if comment:
        args += ["--comment", comment]
    if email:
        args += ["--email", email]
    if mobile:
        args += ["--mobile", mobile]
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def user_mod(
    user: Annotated[str, "用户名"],
    new_name: Annotated[str | None, "新用户名"] = None,
    password: Annotated[str | None, "新密码"] = None,
    groups: Annotated[list[str] | None, "所属用户组列表（覆盖）"] = None,
    comment: Annotated[str | None, "备注"] = None,
    email: Annotated[str | None, "邮箱"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """修改 NAS 用户信息（写操作）。"""
    args = ["user", "mod", user]
    if new_name:
        args += ["--new-name", new_name]
    if password:
        args += ["--password", password]
    if groups:
        args += ["--groups", ",".join(groups)]
    if comment:
        args += ["--comment", comment]
    if email:
        args += ["--email", email]
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def user_del(
    user: Annotated[str, "用户名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """删除 NAS 用户（危险写操作）。"""
    return _fmt(run_trim(["user", "del", user] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def user_set_admin(
    user: Annotated[str, "用户名"],
    on: bool,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """授予/撤销用户管理员权限（写操作）。"""
    flag = "--on" if on else "--off"
    return _fmt(run_trim(["user", "set-admin", user, flag, "--yes"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def user_unfreeze(
    user: Annotated[str, "用户名"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """解冻被冻结的账号（写操作）。"""
    return _fmt(run_trim(["user", "unfreeze", user] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


# ---------------------------------------------------------------------------
# 百度网盘
# ---------------------------------------------------------------------------

@mcp.tool
def baidu_status(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看百度网盘应用状态（是否已绑定）。"""
    return _fmt(run_trim(["baidu-netdisk", "status"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def baidu_quota(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查看百度网盘容量配额。"""
    return _fmt(run_trim(["baidu-netdisk", "quota"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def baidu_list(
    dir: Annotated[str, "网盘目录，默认 /"] = "/",
    order: Literal["name", "size", "time", "ctime", "path"] = "time",
    ascending: bool = False,
    page: int = 1,
    limit: int = 80,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出百度网盘目录内容。"""
    args = ["baidu-netdisk", "ls", dir, "--order", order, "--page", str(page), "--limit", str(limit)]
    if ascending:
        args.append("--ascending")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def baidu_search(
    keyword: Annotated[str, "搜索关键词"],
    dir: Annotated[str, "限定搜索目录，默认 /"] = "/",
    category: Literal["video", "audio", "image", "document", "application", "other", "seed"] | None = None,
    recursive: bool = False,
    limit: int = 100,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """搜索百度网盘文件。"""
    args = ["baidu-netdisk", "search", keyword, "--dir", dir, "--limit", str(limit)]
    if category:
        args += ["--category", category]
    if recursive:
        args.append("--recursive")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def baidu_mkdir(
    path: Annotated[str, "网盘内新目录路径"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """在百度网盘创建目录（写操作）。"""
    return _fmt(run_trim(["baidu-netdisk", "mkdir", path] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def baidu_info(
    fsids: Annotated[list[str], "文件 fsid 列表（ls/search 结果中获取）"],
    dlink: bool = False,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询网盘文件详细信息（可含下载直链 dlink）。"""
    args = ["baidu-netdisk", "info"] + fsids
    if dlink:
        args.append("--dlink")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def baidu_delete(
    paths: Annotated[list[str], "网盘内文件路径列表"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """删除网盘文件（写操作，进入网盘回收站）。"""
    return _fmt(run_trim(["baidu-netdisk", "rm"] + paths + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def baidu_rename(
    path: Annotated[str, "网盘内文件路径"],
    new_name: Annotated[str, "新名称（不含路径）"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """重命名网盘文件（写操作）。"""
    return _fmt(run_trim(["baidu-netdisk", "rename", path, new_name] + _bool_flag(True), profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def baidu_move(
    src_paths: Annotated[list[str], "源路径列表（最后一项为目标目录）"],
    ondup: Literal["fail", "newcopy", "overwrite", "skip"] = "fail",
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """移动网盘文件（写操作；src_paths 最后一项为目标目录）。"""
    return _fmt(run_trim(
        ["baidu-netdisk", "move"] + src_paths + ["--ondup", ondup] + _bool_flag(True),
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def baidu_copy(
    src_paths: Annotated[list[str], "源路径列表（最后一项为目标目录）"],
    ondup: Literal["fail", "newcopy", "overwrite", "skip"] = "fail",
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """复制网盘文件（写操作；src_paths 最后一项为目标目录）。"""
    return _fmt(run_trim(
        ["baidu-netdisk", "copy"] + src_paths + ["--ondup", ondup] + _bool_flag(True),
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def baidu_upload(
    target_path: Annotated[str, "网盘目标目录"],
    local_files: Annotated[list[str], "服务器本地文件路径列表"],
    conflict: Literal["fail", "rename", "overwrite"] = "rename",
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """上传本地文件到网盘（写操作）。"""
    return _fmt(run_trim(
        ["baidu-netdisk", "upload", target_path] + local_files + ["--conflict", conflict] + _bool_flag(True),
        profile=profile, host=host, port=port, scheme=scheme, timeout=1800,
    ))


@mcp.tool
def baidu_download(
    target_path: Annotated[str, "NAS 保存目录"],
    fsids: Annotated[list[str], "要下载的文件 fsid 列表"],
    conflict: Literal["fail", "rename", "overwrite"] = "rename",
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """从网盘下载文件到 NAS（写操作）。"""
    return _fmt(run_trim(
        ["baidu-netdisk", "download", target_path] + fsids + ["--conflict", conflict] + _bool_flag(True),
        profile=profile, host=host, port=port, scheme=scheme, timeout=1800,
    ))


@mcp.tool
def baidu_share_save(
    share_url: Annotated[str, "分享链接 URL"],
    target_path: Annotated[str, "转存目标目录（网盘内）"],
    password: Annotated[str | None, "分享提取码"] = None,
    fsid: Annotated[str | None, "仅转存指定 fsid 的内容"] = None,
    save_all: bool = False,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """转存分享链接内容到网盘（写操作）。"""
    args = ["baidu-netdisk", "share-save", share_url, target_path]
    if password:
        args += ["--password", password]
    if fsid:
        args += ["--fsid", fsid]
    if save_all:
        args.append("--all")
    args += _bool_flag(True)
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def baidu_task_list(
    transfer: Literal["upload", "download"],
    state: Literal["running", "completed"] = "running",
    page: int = 1,
    page_size: int = 50,
    sort_by: Literal["name", "size", "status"] = "name",
    ascending: bool = False,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出网盘上传/下载传输任务。"""
    args = [
        "baidu-netdisk", "task", "list",
        "--transfer", transfer, "--state", state,
        "--page", str(page), "--page-size", str(page_size),
        "--sort-by", sort_by,
    ]
    if ascending:
        args.append("--ascending")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def baidu_task_info(
    task_id: Annotated[str, "任务 ID"],
    transfer: Literal["upload", "download"] = "download",
    state: Literal["running", "completed"] = "running",
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """查询网盘传输任务详情。"""
    return _fmt(run_trim(
        ["baidu-netdisk", "task", "info", task_id, "--transfer", transfer, "--state", state],
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def baidu_task_action(
    task_ids: Annotated[list[str], "任务 ID 列表"],
    action: Literal["pause", "resume", "cancel"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """批量操作网盘传输任务（写操作）：pause / resume / cancel。"""
    return _fmt(run_trim(
        ["baidu-netdisk", "task", action] + task_ids + _bool_flag(True),
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def baidu_task_clear(
    transfer: Literal["upload", "download"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """清空网盘传输任务记录（写操作）。"""
    return _fmt(run_trim(
        ["baidu-netdisk", "task", "clear", "--transfer", transfer] + _bool_flag(True),
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def baidu_sync_list(
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """列出网盘同步任务。"""
    return _fmt(run_trim(["baidu-netdisk", "sync", "list"], profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def baidu_sync_create(
    direction: Literal["download", "upload"],
    nas_path: Annotated[str, "NAS 侧目录"],
    netdisk_path: Annotated[str, "网盘侧目录"],
    interval: Literal["1m", "10m", "30m"] = "10m",
    conflict: Literal["fail", "rename", "overwrite"] = "rename",
    immediate: bool = False,
    keep_deleted: bool = False,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """创建网盘同步任务（写操作）。direction=download 网盘→NAS，
    upload NAS→网盘。"""
    args = [
        "baidu-netdisk", "sync", "create",
        "--direction", direction, "--nas-path", nas_path, "--netdisk-path", netdisk_path,
        "--interval", interval, "--conflict", conflict,
    ]
    if immediate:
        args.append("--immediate")
    if keep_deleted:
        args.append("--keep-deleted")
    args += _bool_flag(True)
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def baidu_sync_update(
    sync_id: Annotated[str, "同步任务 ID"],
    status: Literal["running", "paused"],
    interval: Literal["1m", "10m", "30m"],
    conflict: Literal["fail", "rename", "overwrite"],
    immediate: bool = False,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """修改网盘同步任务配置（写操作；status/interval/conflict 必填）。"""
    args = [
        "baidu-netdisk", "sync", "update", sync_id,
        "--status", status, "--interval", interval, "--conflict", conflict,
    ]
    if immediate:
        args.append("--immediate")
    args += _bool_flag(True)
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


@mcp.tool
def baidu_sync_action(
    sync_id: Annotated[str, "同步任务 ID"],
    action: Literal["start", "pause", "delete"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """启停/删除网盘同步任务（写操作）。"""
    return _fmt(run_trim(
        ["baidu-netdisk", "sync", action, sync_id] + _bool_flag(True),
        profile=profile, host=host, port=port, scheme=scheme,
    ))


@mcp.tool
def diagnostic_export(
    output_dir: Annotated[str, "服务器本地输出目录"],
    srv_type: Annotated[str | None, "服务类型过滤"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """导出 NAS 诊断日志到本地目录（写操作，生成诊断包）。"""
    args = ["diagnostic-log", "export", "--output-dir", output_dir]
    if srv_type:
        args += ["--srv-type", srv_type]
    args.append("--yes")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme, timeout=600))


@mcp.tool
def diagnostic_stop(
    task_id: Annotated[str, "诊断任务 ID"],
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """停止诊断日志导出任务（写操作）。"""
    return _fmt(run_trim(["diagnostic-log", "stop", "--task-id", task_id, "--yes"], profile=profile, host=host, port=port, scheme=scheme))


# ---------------------------------------------------------------------------
# 原始请求（兜底）
# ---------------------------------------------------------------------------

@mcp.tool
def raw_request(
    request: Annotated[str, "系统业务原始请求端点，如 /ogh/v/xxx"],
    method: Literal["get", "post", "put", "delete"] = "get",
    body: Annotated[dict | None, "JSON 请求体"] = None,
    profile: Profile = None,
    host: Host = None,
    port: Port = None,
    scheme: Scheme = None,
) -> str:
    """调用系统业务原始 API（兜底；仅内置审计过的只读端点可省略确认）。

    命令格式为 raw <METHOD> <REQUEST>；写方法一律带 --yes。
    """
    args = ["raw", method, request]
    if body is not None:
        args += ["--json", json.dumps(body, ensure_ascii=False)]
    if method != "get":
        args.append("--yes")
    return _fmt(run_trim(args, profile=profile, host=host, port=port, scheme=scheme))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="fnOS MCP Server（封装 trim-cli）")
    parser.add_argument("--transport", choices=["stdio", "http", "sse"], default="stdio")
    parser.add_argument("--host", help="HTTP/SSE 监听地址（仅 http/sse 传输）", default="127.0.0.1")
    parser.add_argument("--port", help="HTTP/SSE 监听端口（仅 http/sse 传输）", type=int, default=8000)
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
