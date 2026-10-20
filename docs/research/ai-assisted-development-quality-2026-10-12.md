# AI 辅助开发的速度与软件质量

归档日期：2026-10-12。供 MIRA 与 mira-ui 后续开发参考。

AI 增加代码生成吞吐量后，验证能力必须同步增长。控制系统性故障，需要同时减少缺陷遗漏、限制影响范围、缩短发现与恢复时间。增加 reviewer 数量或测试数量，不能单独解决这个问题。

## LobeHub 的可借鉴机制

分析基于 LobeHub 当时公开的 canary 分支规范、CI、审查及验收规则，并抽查一个实际回归修复。无法据此推定 AI 生成代码的比例、生产事故率、人工投入或私有分支保护配置。

| 机制 | 已查看的做法 | 可借鉴之处 |
| --- | --- | --- |
| 架构约束 | 明确 server、routes、features、services、store 与 packages 的职责，部分边界通过 ESLint 强制执行 | 将容易反复违反的约定变成可执行规则 |
| 开发时快速检查 | 按变更运行 lint 与相关测试，类型检查单独组合 | 保持短反馈循环 |
| 广泛 CI | 包、前端、服务端、桌面、数据库分别测试，部分测试分片；数据库测试运行真实 PostgreSQL | 覆盖模块之间的集成问题 |
| 应用 E2E | 构建、迁移数据库、启动服务器后执行浏览器测试 | 证明实际应用可启动、关键流程可执行 |
| 独立审查 | 普通审查使用独立 reviewer；明确请求后才展开完整多维审查 | 避免实现者自我批准，并限制审查轮次 |
| 产品验收 | 检查实际行为，保存截图、录像与日志；首轮检查验收计划和证据 | 将 QA 转成可复查的交付物 |
| 证据复用 | 已有证据有效覆盖交付行为时复用，只补测受影响部分 | 避免为了开 PR 重复完整验收 |
| 规则校准 | 确定性规则放 ESLint，语义判断交给模型；低噪声规则才提升严重程度 | 减少误报、成本与规则疲劳 |

来源：

- [AGENTS.md](https://github.com/lobehub/lobehub/blob/canary/AGENTS.md)
- [测试 CI](https://github.com/lobehub/lobehub/blob/canary/.github/workflows/test.yml)
- [E2E CI](https://github.com/lobehub/lobehub/blob/canary/.github/workflows/e2e.yml)
- [独立审查](https://github.com/lobehub/lobehub/blob/canary/.agents/skills/deep-review/SKILL.md)
- [验收规范](https://github.com/lobehub/lobehub/blob/canary/.agents/skills/acceptance/SKILL.md)
- [验证环境](https://github.com/lobehub/lobehub/blob/canary/.agents/acceptance/PROJECT.md)
- [ALint 规则与校准](https://github.com/lobehub/lobehub/blob/canary/packages/alint/README.md)

这些链接指向可变分支。以后采用时应重新检查，不能将此快照理解为永久状态。

## 不能从流程配置推导出的结论

PR 准入脚本主要检查是否提供验收说明或有理由的豁免，并不证明证据真实、充分；部分维护者及自动账号豁免。其记录的合并标准为 CI、Codex review 与 acceptance，但分支保护是否严格强制执行仍需单独核实。

E2E 指南要求 mock LLM 响应。这适合稳定验证 UI 与流式协议，却不能证明真实模型、供应商及工具执行路径正常。规范中的 canary 是云端生产开发分支，名称本身不能证明小流量灰度。

实际回归案例 [PR 16228](https://github.com/lobehub/lobehub/pull/16228)：异步执行路径创建话题时遗漏 groupId，导致群组侧栏看不到对话。PR 描述记录了真实后端排查和回归测试，并说明撤回修复后新测试失败。其价值在于完成缺陷发现、修复、回归防线的闭环，而非证明此前没有 bug。

来源：[PR 准入脚本](https://github.com/lobehub/lobehub/blob/canary/.github/scripts/pr-admission-check.cjs)、[E2E 指南](https://github.com/lobehub/lobehub/blob/canary/e2e/CLAUDE.md)。

## MIRA 应优先维护的不变量

质量建设应围绕跨层契约、状态转换、持久化与故障恢复。典型遗漏包括前后端事件含义不一致、取消后继续执行、重连重复调用工具、新版本不能读旧数据，以及两个独立通过的 PR 合并后破坏共享状态。

- 取消确认之后，任务不会继续产生副作用。
- 断线重连不会重复执行工具。
- 未授权或未批准的操作不会执行。
- 项目创建失败和回滚不会破坏已有文件。
- 升级后已有会话与任务状态仍可加载。
- 错误必须留下版本、时间、进程与关联上下文，支持重建执行链。

这些不变量应同时约束实现、测试、验收与运行监控。测试应该验证行为，避免只匹配实现源码或假设所有依赖永远成功。

## 按风险组织开发与验证

| 风险 | 典型变更 | 自动验证 | 人工投入 |
| --- | --- | --- | --- |
| 低 | 局部样式、明确边界内的小修改 | 静态检查、相关测试；视觉变化补截图 | 抽查，可靠门禁建立后可评估自动合并 |
| 中 | 新交互、业务逻辑、状态变化 | 独立审查、集成测试、受影响流程验收 | 检查需求与证据 |
| 高 | 认证、权限、数据迁移、共享协议、工具执行 | 深度审查、故障注入、兼容性及恢复验证 | 负责人判断架构和发布风险 |

每个 PR 应被验证，但不必逐一人工运行完整 QA。人工精力集中于需求、架构、不可逆操作和风险接受。

1. 实现之前明确成功、失败、取消、恢复和持久化后的验收行为。
2. 一个 PR 聚焦一个行为，避免混合功能、重构和依赖升级。
3. 开发时运行相关检查；PR 运行集成及核心流程；发布前验证实际合并结果与安装包。
4. 保留 mock 测试和少量真实模型测试两条线，分别验证程序行为及实际执行能力。
5. 使用功能开关、超时、重试上限、幂等、故障隔离和兼容迁移限制缺陷影响。
6. 记录版本与关联 ID，提供本地诊断导出，缩短故障定位时间。
7. 追踪逃逸缺陷、回归、flaky 测试、恢复时间、检查耗时与人工审查投入。

多个 AI reviewer 一致通过不能替代可执行断言与真实运行证据。独立上下文能减少共同盲点，但不能保证独立错误。

## MIRA 与 mira-ui 的实施优先级

本轮查看的 CI 中，引擎已有 Python 3.11/3.12 的 pytest 与 coverage；UI 已有类型检查、单元测试与 compatibility schema 检查。这些不能单独证明两仓组合、实际服务进程或安装包正常。

1. 锁定取消、审批、恢复、工具幂等、文件保护、旧数据加载等核心不变量。
2. 明确 engine 与 UI 的 SHA，建立发送、流式响应、工具调用、审批、取消、重连与重启的配对 smoke。
3. 从实际安装包启动引擎并完成真实请求。
4. 实行风险分级、独立审查与证据化 PR。
5. 建立跨进程日志、崩溃记录和本地诊断包；默认不记录用户对话、工具参数、令牌和医学数据。
6. 用实际故障演练判断日志是否足以定位，不能只检查日志文件是否存在。

相关仓库入口：[引擎 CI](../../.github/workflows/tests.yml)；mira-ui 的 `.github/workflows/test.yml` 与 `.github/workflows/compatibility-check.yml`。
