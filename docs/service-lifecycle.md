# 本地引擎的启动、停止与重启

适用于 macOS LaunchAgent、Windows WinSW 服务，以及显式选择的独立 gateway。

## 系统服务（桌面版默认方式）

```sh
mira-engine status
mira-engine start
mira-engine stop
mira-engine restart
```

- `start`：已运行时不会杀进程；等待该服务自己的 gateway 通过健康检查才成功。
- `stop`：macOS 卸载当前 launchd job，避免 KeepAlive 自动拉起，但保留安装文件；Windows 请求服务停止。等待进程树退出，超时返回失败。
- `restart`：同一把跨进程锁内先停止、后启动。停止失败不会启动替代进程，沿用已安装的配置和端口。
- `status`：`installed`、`loaded`（macOS）、`running`、`healthy` 分别表示安装、注册、进程运行、接口健康。服务注册但崩溃循环不再算运行。
- 权限或查询错误返回非零退出码，不会把安装记录清掉。Windows 启停系统服务通常需要管理员权限。
- 停止后如仍有另一个 Mira gateway 占用端口，会报告其配置路径，不会擅自终止它。

macOS 的 stop 不会删除 plist，也不改变下次登录的自动启动设置。如需永久移除自启动，请使用 `uninstall-service`。

### 服务进程的 PATH

系统服务不经过登录 shell，拿到的是系统默认 PATH（macOS launchd 只有 `/usr/bin:/bin:/usr/sbin:/sbin`），所以终端里能用的命令，服务里不一定找得到——典型症状是桌面版的社区贡献工具报 `gh` 找不到，而终端里 `gh auth status` 正常。gateway 以服务方式启动时（`run-gateway`）会把本机存在的常见安装目录追加到 PATH 末尾：

- macOS：`/opt/homebrew/bin`、`/opt/homebrew/sbin`、`/usr/local/bin`、`/usr/local/sbin`、`/opt/local/bin`、`~/.local/bin`、`~/bin`、Nix profile
- Linux：`/usr/local/bin`、`~/.local/bin`、`~/bin`、Linuxbrew、`/snap/bin`、Nix profile
- Windows：GitHub CLI、WinGet Links、scoop shims、Chocolatey、Git for Windows 的安装目录

只追加不前置，原有 PATH 的优先级不变。装在别处的工具，把它软链到上面任一目录（如 `/usr/local/bin`）即可；社区贡献流程找不到 `gh`/`git` 时会在工具结果里列出搜索过的目录。

## 独立启动的 gateway

用 `mira gateway` 启动的进程不一定属于系统服务。控制时必须使用与该进程相同的 `MIRA_HOME`：

```sh
# macOS / shell：以下路径替换成自己的 Mira 数据目录
MIRA_HOME=/path/to/mira-data mira-engine status --gateway
MIRA_HOME=/path/to/mira-data mira-engine stop --gateway --port 18790
MIRA_HOME=/path/to/mira-data mira-engine start --gateway --port 18790
MIRA_HOME=/path/to/mira-data mira-engine restart --gateway --port 18790
```

只有一个实例时可省略 `--port`；一次 stop/start/restart 应保持相同的选择参数。`start --gateway` 恢复之前停止时保存的启动方式，不负责首次创建实例。首次运行仍使用 `mira gateway`。

Windows 的旧版 detached 后台进程不能可靠接收优雅停止信号。明确接受中断任务后可强制停止，后续建议改用系统服务：

```powershell
$env:MIRA_HOME = 'C:\path\to\mira-data'
mira-engine status --gateway
mira-engine stop --gateway --port 18790 --force
```

`--force` 只针对经过身份校验的独立/旧后台进程及其子进程；不会扫描并终止所有同名进程，也不会绕过 PID 复用、权限、配置归属检查。系统服务不需要 `--force`。

`--home` 指操作系统用户目录，数据目录是其下的 `.mira`，不是 `MIRA_HOME` 的同义参数。桌面版 Windows 提权时用它保持原用户目录。

## 每个 MIRA_HOME 只运行一个引擎

系统服务、独立 gateway、以后的 Remote-SSH 引擎都可能指向同一个 `MIRA_HOME`（服务器上还可能是多台机器共享的 NFS 主目录）。两个引擎同时写同一份数据会损坏会话和项目，所以 `mira gateway` 启动时先拿“引擎租约”：

- **本地锁**：运行时目录里的 `engine.lock`。内核在进程退出（包括崩溃、`kill -9`）时自动释放，系统服务重启不会被自己上一次的锁卡住。
- **租约记录**：`<MIRA_HOME>/engine.lease`，JSON，记录主机名、PID、端口、版本、启动时间和心跳时间（每 30 秒刷新）。同一台机器上按 PID 和进程创建时间判断是否还活着；其他机器上的记录在心跳超过 90 秒未更新时视为失效，可以接管。
- 已有引擎在运行时，第二个 `mira gateway` 不启动，退出码为 `3`，并在 stderr 说明占用者的主机、PID 和端口。

## 停止与端口占用

`mira gateway`（以及打包版的 `mira-engine run-gateway`）收到 SIGTERM 或 SIGINT（Windows 上还有 Ctrl+Break；WinSW 停止服务时发送 Ctrl+C）后优雅退出：停止接受新连接，用 1001（going away）关闭 WebSocket，停止各渠道、cron、heartbeat 和 MCP 子进程，释放引擎租约（删除 `engine.lease`），退出码 `0`。整个过程最多 10 秒，超时则释放租约后以退出码 `1` 强制退出；期间再收到一次信号会立即退出，退出码为 128 + 信号编号（SIGINT 为 130，SIGTERM 为 143）。

启动时如果端口已被占用，gateway 只会停止能证明是“同一 `MIRA_HOME` 的遗留引擎”的进程：属于当前用户、命令行是 Mira gateway、环境变量或本机 gateway 登记记录指向同一个 `MIRA_HOME`，且不是当前持有租约的引擎。其他进程（包括其他用户的进程、其他 `MIRA_HOME` 的引擎）一律不发信号，gateway 以退出码 `4` 退出，并在 stderr 给出占用者的 PID 和进程名。`mira remote attach` 遇到退出码 `4` 时会换一个空闲端口重试。完整的退出码表见 [Remote-SSH](remote-ssh.md#gateway-shutdown-and-exit-codes)。

运行时目录用于锁、租约等临时状态，按以下顺序确定，并为每个 `MIRA_HOME` 单独建子目录（权限 0700）：

1. 环境变量 `MIRA_RUNTIME_DIR`；
2. `$XDG_RUNTIME_DIR/mira`（仅当该目录属于当前用户且权限为 0700）；
3. `<MIRA_HOME>/runtime`。

数据仍留在 `MIRA_HOME`。gateway token（`<MIRA_HOME>/runtime/gateway-token`）和服务状态文件也保持原位置：`/run/user/<uid>` 在重启或退出登录时会被清空，token 放在那里会导致桌面端保存的 token 失效。

在服务器上使用时：`/run/user/<uid>` 在用户最后一个登录会话结束时可能被删除。租约记录会继续阻止第二个引擎启动，但建议执行 `loginctl enable-linger <用户名>`，或把 `MIRA_RUNTIME_DIR` 设为本地磁盘上的持久目录。

## 验证

```sh
.venv/bin/pytest -q tests/test_agent_service_lifecycle.py tests/test_agent_service_launchd.py tests/test_agent_service_platform_managers.py tests/test_agent_service_cli.py tests/test_gateway_failsafe.py tests/test_gateway_shutdown.py tests/test_port_guard.py

# 仅 macOS：显式启用真实 launchd 测试，随机服务名、临时目录、空闲端口
MIRA_TEST_REAL_LAUNCHD=1 .venv/bin/pytest -q tests/test_agent_service_lifecycle.py::test_real_macos_lifecycle
```

默认测试模拟 OS 调用，不操作用户已安装的服务。Windows SCM 的状态、安装归属、后台进程和失败路径有模拟回归；仍应在 Windows 真机上验收提权、WinSW 和安装包集成。

## 运行日志与崩溃诊断

日志路径、请求和任务关联、崩溃捕获、桌面白屏时的导出入口，以及 `mira-engine doctor --export` 的诊断包范围，见 [运行日志与故障诊断](runtime-diagnostics.md)。质量控制原则见 [AI 辅助开发的速度与软件质量](research/ai-assisted-development-quality-2026-10-12.md)。
