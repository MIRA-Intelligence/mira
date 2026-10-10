# 可重复交互确认与调研成果方向：实现与验证

日期：2026-09-24

状态：DONE_WITH_CONCERNS。代码、专项测试、前端构建和隔离浏览器验证完成；未重启现有引擎、未进行真实模型端到端试跑。扩大后端回归存在两项已复现的旧 memory-store 测试失败。

## 行为契约

- 四个原生 ModuleRun 阶段共用 `request_workflow_interaction`，不局限于规划或第一次审批。
- 一次提出一个有决策意义的问题，回答后恢复；后续出现新疑问或新证据时可再次提出问题。
- `clarify_requirements`：缺少成果方向、目标或其他会改变执行路径的信息。
- `resolve_conflict`：任务需求、可配置默认值或新发现的约束不兼容。安全规则、访问权限和更高优先级约束不作为可批准绕过的选项。
- `approve_*`：明确审批执行范围，与一般澄清分开。重大范围变更需要重新审批。
- `choose_direction`：已经调研并记录的候选方向，不用于初始成果偏好澄清。
- 低风险实现细节由 AI 在已同意范围内自行决定；已回答的问题不无故重复询问。

## 实现

后端：

- `mira_engine/modules/prompts.py`：共享可重复交互规范；调研阶段轻量确认成果方向；研究候选项要求证据、问题/假设、贡献与不确定性、数据资源、风险、基线和最小验证标准；尊重仅综述/比较的要求，不凑候选数量。
- `mira_engine/modules/tools.py`：澄清/冲突工具说明；空答案和固定选项响应约束；执行审批采用明确 approve/revise。
- `mira_engine/modules/executors.py`：恢复上下文保留问题、选项标签和回答；提供现存的已回答交互历史（底层最多保留最近 100 条），不再只给不透明的选项 ID。调研契约允许检索前澄清，综述型结果可以没有方向列表。
- `mira_engine/modules/runtime.py` 与 `mira_engine/agent/base_loop.py`：等待输入后结束模型执行轮，阻止同批剩余工具的实际执行，同时保留完整工具响应协议；旧 attempt 不得在回答后继续执行新 attempt 的任务。
- `mira_engine/workflows/service.py`：未回答不能完成 Run；暂停/失败后的 resume/retry 返回 waiting_input，不能绕过问题；拒绝空白、无效固定选项、模糊审批以及无说明的 revise；旧 interaction_id 不能回答新问题。

前端（相邻 mira-ui 仓库）：

- `WorkflowInteractionPanel.tsx`：交互 ID 变化时重置输入；展示选项说明；支持选项加备注或自由回答；审批必须显式选择，修改必须说明；待提交期间禁用输入。
- `workflowCandidates.ts`：明确区分候选选择和一般澄清/冲突；后续澄清不会成为候选项，也不覆盖历史研究方向选择。
- `WorkflowWorkspace.tsx`：中英文启动提示与后端成果方向规范保持一致。

## 验证结果

| 验证 | 结果 |
| --- | --- |
| 工作流全套，包括 HTTP/WebSocket | 65 passed |
| 扩大后端回归：工作流、共享循环、研究循环、agent、工具注册、UI channel | 602 passed / 2 failed |
| 前端全套 | 64 files / 462 passed |
| 前端 TypeScript + Vite 生产构建 | 通过；仍有构建器的 chunk 大小与混合导入提示 |
| 本次后端改动 Ruff / 两仓 diff whitespace 检查 | 通过 |
| browse 实际组件两轮表单验证 | 通过；两次提交被浏览器模拟器捕获；无 console errors |

新增回归覆盖：

- initiation/planning/experiment/delivery 各自连续提出澄清与冲突问题，最终完成。
- 已经审批之后仍能再提问；同批后续文件写入及完成操作被阻止；不多调用一次模型来结束等待轮。
- 问题与选项含义在后续模型输入中保留。
- 暂停、失败恢复不能绕过未回答的问题，过期 ID 提交被拒绝。
- 新表单不沿用旧文本或选中状态；选择备注、纯自由回答、显式批准和有说明的修改；提交失败保留编辑内容。
- 新澄清不污染候选列表或既有下游分支选择。

### 两项扩大回归失败

`tests/agent/test_memory_store.py`：

1. `test_get_memory_context_returns_empty_when_missing`
2. `test_get_memory_context_returns_formatted_content`

原因：现有 MemoryStore 会叠加全局记忆；有全局内容时既不返回空字符串，也不用旧 `Long-term Memory` 标题。相关实现和测试未在本任务改动。用 `git show HEAD:mira_engine/agent/memory.py` 加载未修改版本，在临时目录中模拟全局内容，复现两个断言均不成立而本地正文仍存在。未为本次功能修改这组无关测试。

## 浏览器证据与复现

`browser-probe.js` 挂载实际 `WorkflowInteractionPanel`，仅使用模拟 Run，并拦截提交；不写真实项目，不调用外部模型。通过读取 Vite 转换后的组件 import URL，确保夹具和组件使用同一个热更新状态模块。

- [第一轮：成果方向、选项说明和备注](first-question.png)
- [第二轮：新冲突、旧输入已清空](second-question.png)

捕获的回答：

```json
[
  {"interaction_id":"qa-question-1","response":{"selection":"feasible","notes":"优先选择现有数据能做的小规模验证。"}},
  {"interaction_id":"qa-question-2","response":{"selection":"先做内部验证，并明确泛化结论的限制。"}}
]
```

候选选择状态仍为 null。第二轮初始按钮禁用、文本为空。截图已人工查看。

## 验证边界与启用

- 触发澄清的语义判断依赖 agent prompt，不是假装能通过硬编码识别所有缺失目标。专项运行使用 scripted provider，验证的是实际原生执行链和状态边界，不证明真实模型的触发率或研究质量。
- 浏览器验证使用真实组件与模拟提交；不宣称完成了“真实模型 → 运行中网关 → 桌面应用”的全链路验收。
- 现有引擎进程未重启，防止打断正在运行的用户任务。后端改动需重启引擎后加载；桌面分发还需另行打包。
- 未提交或推送 Git；保留了两仓原有的无关修改。

建议后续真实模型验收：模糊调研请求应先询问成果方向；明确要三个候选时不重复询问；回答后能研究并给出有依据的选项；中途新发现数据限制时再次确认；用户只要综述时不强制推进实验。
