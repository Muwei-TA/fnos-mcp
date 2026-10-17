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
