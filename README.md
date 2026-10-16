# fnos-mcp

将 [fnos-skill](https://github.com/Muwei-TA/fnos-skill) 的 `trim-cli` 封装为 [MCP](https://modelcontextprotocol.io) 服务器（基于 [FastMCP](https://gofastmcp.com)），让任何 MCP 客户端（Claude Desktop、Knot、Cursor 等）都能用自然语言管理飞牛 fnOS NAS。

## 功能覆盖

83 个 MCP 工具，覆盖 trim-cli 的完整能力面：

| 模块 | 工具示例 |
| --- | --- |
| 认证 | `login`（OAuth 2.0 + PKCE）、`submit_auth_code`、`logout`、`whoami` |
| 系统 | `system_info`、`system_hardware`、`system_volumes`、`system_power_plan` |
| 文件 | `file_list`、`file_search`、`file_copy`、`file_delete`、`file_trash_*` |
| 相册 | `photos_search`（AI 语义搜图）、`photos_detail`、`photos_preview_url` |
| 应用中心 | `app_list`、`app_store_search`、`app_install`、`app_uninstall` |
| Docker | `docker_containers`、`docker_images`、`docker_image_pull`、`docker_stats` |
| 下载 | `download_list`、`download_add_uri`、`download_action` |
| 日志 | `logger_list`、`logger_modules`、`logger_export` |
| 监控 | `monitor_cpu`、`monitor_memory`、`monitor_network`、`monitor_disk`、`monitor_gpu` |
| 网络 | `network_ssh_status`、`network_ssh_switch` |
| 电源 | `power_reboot`、`power_poweroff` |
| 存储 | `storage_overview`、`storage_disks`、`storage_disk_smart` |
| 用户 | `user_list`、`user_info`、`user_groups` |
| 百度网盘 | `baidu_list`、`baidu_search`、`baidu_quota` |
| 原始 API | `raw_request`、`photos_raw_request`（兜底） |

## 安装

```bash
git clone https://github.com/Muwei-TA/fnos-mcp.git
cd fnos-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
```

要求 Python ≥ 3.10。`vendor/trim-cli/` 已内置六平台二进制（Linux x64/arm64、macOS x64/arm64、Windows x64/arm64），开箱即用，无需单独安装 trim-cli。

## 配置 MCP 客户端

stdio 方式（推荐，适用于 Claude Desktop / Knot 等）：

```json
{
  "mcpServers": {
    "fnos": {
      "command": "/path/to/fnos-mcp/.venv/bin/python",
      "args": ["-m", "fnos_mcp.server"]
    }
  }
}
```

HTTP 方式（先启动服务端 `fnos-mcp --transport http --port 8000`，客户端连 `http://127.0.0.1:8000/mcp`）。

## 首次登录（OAuth 2.0 + PKCE）

MCP 客户端中对话式完成：

1. 让客户端调用 `login`，参数给 `host`（如 `fnos.muwei.xyz`）和 `port`/`scheme`
2. 工具返回授权 URL，在浏览器打开、登录 fnOS 并确认授权
3. 把页面显示的一次性 code 告诉客户端，由 `submit_auth_code` 完成交换

也可以先在本机命令行手动登录（session 存在 `~/.config/trim-cli/`，MCP 服务共用）：

```bash
vendor/trim-cli/trim-cli-linux-x64 --profile default --host <host> --port 443 --scheme https login
```

说明：

- 登录仅支持 OAuth 2.0 授权码 + PKCE，不处理账号密码
- 远程明文 HTTP 需 `allow_insecure_http=True`；自签 HTTPS 需 `tls_insecure=True`
- 会话 token 自动刷新；连续 401 时需重新登录

## 安全设计

- 写操作命令一律显式传 `--yes`，避免 CLI 交互确认卡死 MCP 进程
- 危险操作（`power_reboot`、`file_trash_clear`、`docker_container_rm` 等）在工具描述中明确标注，由客户端侧决定是否二次确认
- 不封装 `media` 命令（fnos-skill 明确排除 Media API）
- `login` 使用 15 秒超时提取授权 URL 后返回，不阻塞 MCP 进程

## 项目结构

```
fnos-mcp/
├── fnos_mcp/
│   ├── cli.py       # trim-cli 二进制引导与执行层
│   └── server.py    # FastMCP 服务器与 83 个工具定义
├── vendor/trim-cli/ # 六平台 trim-cli 二进制（来自 fnos-skill fa21571）
├── pyproject.toml
└── README.md
```

## 致谢

- [fnos-skill](https://github.com/Muwei-TA/fnos-skill) 提供 trim-cli 与全部 fnOS API 能力
- [FastMCP](https://github.com/PrefectHQ/fastmcp) 提供 MCP 服务器框架

## License

MIT
