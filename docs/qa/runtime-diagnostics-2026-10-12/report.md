# 运行诊断实现验证

日期：2026-10-12。范围：mira 与 mira-ui 源码改动、本地构建、隔离故障演练。没有部署、重启或终止用户现有的 MIRA 服务。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 引擎完整回归 `pytest tests/ -q` | 2,461 passed，2 skipped；82 warnings |
| 最终 HTTP/WS、取消、导出、故障回归 | 164 passed；71 warnings |
| 最终 UI 全量 Vitest | 75 个文件，550 passed |
| UI TypeScript `tsc -b` | 通过 |
| Web 与 Electron `build:desktop` | 通过；构建有依赖混合静态/动态导入及大 chunk 提示 |
| 引擎变更 F401/F841 lint | 通过 |
| 两仓 `git diff --check` | 通过 |

完整引擎回归在最后新增 `/api/version` 的 `diagnostics_contract` 能力字段和完善长异常栈导出前运行；这两项改动后重新运行 164 项相关回归。最终 UI 检查包含旧引擎 CORS 兼容测试和本地 dump 保留策略测试。

测试覆盖：轮转文件数量与 JSON 完整性、并发 ContextVar 隔离、私有正文与 token 不进入新日志、异常类型及代码位置、磁盘写失败降级、HTTP 失败请求 ID、旧 service 原始输出过滤、未捕获 Python 异常、独立子进程 SIGABRT、异常退出标记、引擎不可用时的桌面导出、IPC 元数据过滤、native dump 保留范围，以及 renderer 终止与卡死事件。

## 实际 Electron 故障演练

使用本地缓存中的 Electron 36.9.5，在 `/tmp` 启动专用测试应用。应用只加载受测诊断模块和空白测试页面，不启动引擎，也不读取用户的配置、会话或应用数据。

调用该测试窗口的 `forcefullyCrashRenderer()` 后，真实 `render-process-gone` 事件为 `reason=killed`、exit code 2。主进程同步写入 `renderer.process_gone`，在引擎未运行时完成 ZIP 导出。Crashpad 的 uploadToServer 为 false；本地生成 1 个 `.dmp`，普通诊断 ZIP 不含该文件。

- [原生演练结果](electron-native-result.json)
- [实际离线诊断包](electron-native-diagnostics.zip)

诊断包内 manifest 的 `engine_not_running` 是该演练的预期缺项。包中的桌面日志包含 app.started 与 renderer.process_gone。这证明独立 renderer 终止时主进程记录与离线导出有效，不证明当前安装版本、真实模型调用或所有操作系统的原生崩溃行为。

## 运行边界

Python 与 Electron 演练运行于 macOS。Windows、Linux 的原生崩溃、SCM/WinSW、系统级冻结、断电和 SIGKILL 恢复仍需对应环境验证。浏览器版的当前标签页诊断缓存不具备桌面 IPC 的跨 renderer 持久性。

正在运行的服务和已安装应用仍使用各自原版本；本轮没有将源码检查当作它们已生效的证据。发布时应保留对应版本 hidden source maps，并重新验证安装包中的日志与导出链路。
