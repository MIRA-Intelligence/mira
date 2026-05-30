# Mira 发布日操作清单

本文面向发布操作者，覆盖 `Mira` 与 `MiraUI` 双仓发布。

## 0) 发布范围确认

- [ ] 确认目标版本：
  - Agent tag: `vX.Y.Z`（`Mira`）
  - UI tag: `vA.B.C`（`MiraUI`）
- [ ] 确认 `mira-ui` 仓库的 `compatibility.json` 已更新到本次 release train，并且 `compatibility.json#ui` 等于（或落在 minor 范围内）即将打的 UI tag。该校验由 `mira-ui` 的 `desktop-release.yml#verify-compatibility` job 在打 tag 时强制执行，但发布前最好本地先跑一遍 `node scripts/validate-compatibility.mjs --file compatibility.json --require-ui A.B.C`。
- [ ] 如果本轮改了 wire format，确认 `mira_engine/channels/ui.py` 里的 `_API_CONTRACT_VERSION` 已 bump，且 `mira-ui/compatibility.json#api_contract` 同步更新。
- [ ] 确认里程碑与变更范围一致（只发已验收内容）

## 1) 发布前基线检查（T-1）

### Mira

- [ ] 在 `release` 分支同步最新代码
- [ ] 本地回归：

```bash
python -m pytest tests -q
```

- [ ] 检查关键 workflow 存在：
  - `.github/workflows/tests.yml`
  - `.github/workflows/agent-release.yml`
  - `.github/workflows/release-train.yml`

### MiraUI

- [ ] 在 `release` 分支同步最新代码
- [ ] 本地构建：

```bash
npm run build:web
npm run build:electron
```

- [ ] 检查关键 workflow 存在：
  - `.github/workflows/desktop-release.yml`

## 2) 版本打标（Release Day）

### 2.1 Agent 仓库打 tag（Mira）

```bash
git checkout release
git pull --ff-only
git tag vX.Y.Z
git push origin vX.Y.Z
```

预期：自动触发 `agent-release.yml`。

### 2.2 UI 仓库打 tag（MiraUI）

```bash
git checkout release
git pull --ff-only
git tag vA.B.C
git push origin vA.B.C
```

预期：自动触发 `desktop-release.yml`。

## 3) 流水线执行与产物验收

### Agent Release (`Mira`)

- [ ] `agent-release.yml` 全绿
- [ ] 检查 GitHub Release 产物：
  - wheel / sdist
  - `mira-engine` 可执行文件（各平台）
  - `SHA256SUMS.txt`
- [ ] 如启用 PyPI 发布，确认版本可见

### Desktop Release (`MiraUI`)

- [ ] `desktop-release.yml` 全绿
- [ ] 检查 Release 产物：
  - macOS: `dmg` / `zip` / `latest-mac.yml`
  - Windows: `exe` / `latest.yml`

## 4) 组合发布验证（Release Train）

在 `Mira` 手动触发 `release-train.yml`（workflow_dispatch）：

- `agent_tag = vX.Y.Z`
- `ui_tag = vA.B.C`

验收条件：

- [ ] `verify-tags` 通过
- [ ] `smoke` 通过
- [ ] 产出 `smoke-report.json` 与 release summary artifact

## 5) 上线后验证

- [ ] Cloud/API 健康检查：

```bash
curl http://127.0.0.1:18790/health
curl http://127.0.0.1:18790/version
```

- [ ] Desktop 首次启动验证：
  - 本地引擎可探活
  - 不兼容提示可见
  - 一键升级可执行并可回连

- [ ] Self-hosted 文档快验：
  - `release/docker-compose.yml`
  - `docs/self-hosted-docker.md`

## 6) 发布公告与记录

- [ ] 更新 Release Notes（关键变更 + breaking changes + 回滚说明）
- [ ] 在项目频道发布版本公告
- [ ] 在里程碑/项目看板记录“发布完成时间”和负责人

## 7) 回滚预案（必要时）

### Agent 回滚

```bash
mira-engine stop
python -m pip install --upgrade mira==<previous_version>
mira-engine start
mira-engine doctor
```

### UI 回滚

- 使用前一稳定安装包（dmg/exe）回退
- 或在 auto-update 源回退到前一个可用 release 元数据

### Self-hosted 回滚

1. 在 `release/.env` 回填上一个 tag
2. 重新拉取与启动：

```bash
docker compose pull
docker compose up -d
```

