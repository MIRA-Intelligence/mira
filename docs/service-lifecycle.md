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

## 验证

```sh
.venv/bin/pytest -q tests/test_agent_service_lifecycle.py tests/test_agent_service_launchd.py tests/test_agent_service_platform_managers.py tests/test_agent_service_cli.py tests/test_gateway_failsafe.py

# 仅 macOS：显式启用真实 launchd 测试，随机服务名、临时目录、空闲端口
MIRA_TEST_REAL_LAUNCHD=1 .venv/bin/pytest -q tests/test_agent_service_lifecycle.py::test_real_macos_lifecycle
```

默认测试模拟 OS 调用，不操作用户已安装的服务。Windows SCM 的状态、安装归属、后台进程和失败路径有模拟回归；仍应在 Windows 真机上验收提权、WinSW 和安装包集成。
