# MedPilot 发布与部署蓝图（可落地版）

## 1. 目标形态（北极星）

- 普通用户默认走 `Web Hosted`：`app.medpilot.ai`，零安装。
- 需要本地/隐私的用户走 `Desktop` 一体包：安装一个 App，内部可切换 Cloud / Local Agent。
- 开发者与机构用户走 `Self-hosted`：`docker compose` 一键起服务。
- `MedPilot` 与 `MedPilotUI` 继续独立开发、独立测试、独立发版，但通过兼容矩阵绑定成“组合发行版”。

## 2. 三条发布通道（对外产品）

| 通道 | 面向用户 | 交付物 | 更新方式 |
|---|---|---|---|
| Cloud (默认) | 普通用户 | 托管 Web + 托管 Agent API | 后台持续发布 |
| Desktop | 本地优先用户 | `dmg` / `exe` | 应用内自动更新 |
| Self-hosted | IT/开发者 | `docker-compose.yml` + 镜像 | 拉取新镜像升级 |

## 3. 两个 repo 的职责边界

| Repo | 主要职责 | 必发产物 |
|---|---|---|
| `MedPilot` | Agent 核心能力、API、任务执行 | PyPI 包、Docker 镜像、OpenAPI 规范 |
| `MedPilotUI` | 前端交互、桌面壳、连接管理 | Web 静态构建、桌面安装包 |

建议新增一个轻量“编排层”（可新 repo：`medpilot-release`，也可放在 UI repo）：

- 维护 `compatibility.json`（UI 版本与 Agent 版本映射）
- 维护自托管模板 `docker-compose.yml`
- 维护安装脚本与发行说明模板

## 4. 版本与兼容策略（关键）

- `MedPilot`：语义化版本（例如 `1.6.0`）
- `MedPilotUI`：语义化版本（例如 `2.3.0`）
- 对外定义“发行列车版本”（例如 `2026.04`），对应一组兼容组合

建议增加兼容清单文件：`compatibility.json`

```json
{
  "release_train": "2026.04",
  "ui": "2.3.x",
  "agent": "1.6.x",
  "api_contract": "v1",
  "min_agent_for_ui": "1.6.0"
}
```

同时让 Agent 暴露：

- `GET /health`
- `GET /version`

UI 启动时先校验版本兼容；不兼容时提示自动升级或一键修复。

## 5. CI/CD 蓝图（可直接建 workflow）

### 5.1 `MedPilot` CI/CD

- PR：单元测试 + contract test（OpenAPI）
- Tag（`v*`）：
  - 构建并推送 `ghcr.io/<org>/medpilot-agent:<tag>` 与 `:latest`
  - 发布 PyPI（`medpilot-agent`）
  - 上传 `openapi.json` 到 Release artifact

### 5.2 `MedPilotUI` CI/CD

- PR：单元测试 + e2e（mock agent）
- Tag（`v*`）：
  - 构建 Web artifact
  - 构建 Desktop（mac/win/linux）
  - 生成 auto-update 元数据（stable/beta channel）

### 5.3 组合发布（`medpilot-release`）

- 手动触发 `release_train`
  - 读取指定 UI tag + Agent tag
  - 跑真实端到端 smoke test
    - Desktop 连本地 agent
    - Desktop 连云端 agent
    - Web 连云端
  - 生成发布说明与兼容矩阵
  - 产出自托管包（compose 文件 + `.env.example`）

## 6. Desktop 一体化方案（推荐）

体验目标：用户“只装一个 App”。

- 本地 app（按易用优先）：
  1. 优先：`方案A`，本地 Engine + 系统服务（不依赖 Docker）
  2. 备选：应用内调用本地 Docker（高级用户或企业环境）
  3. 兜底：开发模式下手动 Python 进程（仅开发，不面向用户）
- UI 内置 `Engine Manager` 页面：
  - Agent 状态（running/stopped/version）
  - 一键启动/停止/升级
  - 一键诊断（端口占用、服务状态、日志路径）

### 6.1 方案A：本地 Engine + 系统服务（默认本地部署）

目标：替代 `tmux + python gateway`，让普通用户不需要理解终端和进程管理。

- 交付形态：
  - 一个可执行的 `medpilot-engine`（由 Python 打包而来）
  - 一个用户态服务（开机自启、异常拉起、统一日志）
- 服务托管方式：
  - macOS：`launchd`
  - Linux：`systemd --user`
  - Windows：Windows Service
- Desktop 与 Engine 的关系：
  - Desktop 通过本地 `http://127.0.0.1:<port>` 调用 engine API
  - Desktop 负责“探活 + 版本检查 + 引导升级”
  - Engine 负责真实任务执行，UI 不直接管理 Python 环境

建议定义 `medpilot-agent` CLI（用于安装与运维）：

```bash
medpilot-agent install-service
medpilot-agent start
medpilot-agent stop
medpilot-agent status
medpilot-agent logs
medpilot-agent doctor
medpilot-agent uninstall-service
```

目录与运维约定（建议）：

- 配置目录：`~/.medpilot/config/`
- 数据目录：`~/.medpilot/data/`
- 日志目录：`~/.medpilot/logs/`
- 端口约定：默认 `127.0.0.1:46321`（可配置）
- 健康检查：`GET /health`
- 版本检查：`GET /version`

升级策略（建议）：

- Desktop 启动时检查本地 engine 版本与 `compatibility.json`
- 不兼容时提示“一键升级本地引擎”
- 升级流程：下载新包 -> 停服务 -> 替换 -> 启服务 -> 健康检查

兼容与安全（建议）：

- 本地 API 默认只监听 `127.0.0.1`
- 使用短时 token 或本地随机 secret 做 UI-Engine 鉴权
- 明确弃用 `tmux` 作为生产部署方式，仅保留开发调试用途

## 7. Self-hosted 一键部署（给机构用户, 优先级低，暂时不需要做）

提供官方 `docker-compose.yml`（最小可用）：

- `medpilot-agent`
- `medpilot-ui`（或 nginx 托管前端）

并文档化三条基础命令：

```bash
cp .env.example .env
docker compose pull
docker compose up -d
```
建议补充 `medpilot doctor`（脚本或 CLI）做环境检查，降低支持成本。


---

## 附录 A：Github建立Milestone

### A1. 协议与兼容

- [ ] Agent 增加 `/version` 与 `/health` 字段规范
- [ ] UI 增加版本兼容检查与错误提示
- [ ] 建立 `compatibility.json` 与校验脚本

### A2. 构建与发布

- [ ] Agent：PyPI + 本地可执行包发布（Docker 作为可选通道）
- [ ] UI：Web + Desktop 双发布
- [ ] Release：组合测试与发行列车脚本

### A3. 交付与运维

- [ ] `medpilot-agent` CLI（install-service/start/stop/status/logs/doctor）
- [ ] 三平台服务注册脚本（launchd/systemd user/Windows Service）
- [ ] 本地引擎升级器（与 `compatibility.json` 联动）
- [ ] 自托管 `docker-compose.yml` 与 `.env.example`
- [ ] `medpilot doctor` 环境诊断工具
- [ ] 回滚手册与值班排障手册

## 附录 B：决策记录（可持续补充）

- Desktop 技术栈：`Electron`
- 本地引擎默认方式：系统服务（非 Docker）
- Docker 定位：高级/企业自托管可选项，不作为普通用户默认入口
- 是否新建 `medpilot-release` repo：待评估（可先放 `MedPilotUI`）

