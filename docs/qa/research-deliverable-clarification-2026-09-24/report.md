# 调研阶段遗漏交付物要求：定向 QA

日期：2026-09-24

状态：DONE_WITH_CONCERNS

范围：MIRA 当前工作区后端、mira-ui 当前工作区前端；确认行为，不修改产品实现。

## 结论

已有可用于补充请求的结构化交互机制，但没有针对“交付物要求缺失”的确定性识别和字段完整性校验。

从当前 UI 启动调研 ModuleRun，默认设置 `confirmation_policy.mode=required`。后端要求 AI 先发起 `approve_research_scope` 交互，得到明确批准后才允许外部搜索等操作。确认摘要的提示要求包含预期研究成果，表单支持“批准/修改”和自由文本补充。

然而，目标只写“帮我调研肝脏MRI”可以通过输入验证；确认摘要即使没有写交付物要求，也会被工具及后端接受。是否明确追问方向数量、内容结构等，仍依赖模型。因而不能承诺“忘写交付物就一定弹出专门补全问卷”。

## 已核查的实际路径

1. `mira-ui/src/components/workflow/WorkflowWorkspace.tsx:394`：启动模块仅检查目标非空；第 404 行加入 required 确认策略。
2. `mira-ui/src/components/workflow/ModuleStageControls.tsx:160`：模块工作页创建运行也加入 required 确认策略。
3. `mira_engine/modules/prompts.py:51`：调研确认摘要要求包括研究问题、来源、纳入排除边界、预期研究成果。
4. `mira_engine/modules/prompts.py:83`：要求调用结构化交互工具，提供 approve/revise，允许自由文本；修改后再次请求确认。
5. `mira_engine/modules/runtime.py:111`：确认前仅允许读取本地输入和请求交互；未批准时拦截搜索等工具。第 161 行的 completion guard 也提醒模型必须请求确认。
6. `mira_engine/modules/tools.py:306`：交互工具保存请求和检查点，进入 waiting_input；没有交付物字段或摘要内容完整性校验。
7. `mira-ui/src/components/workflow/WorkflowInteractionPanel.tsx:30`：waiting_input 时显示交互卡片；支持选择、补充文本和提交。
8. `mira_engine/workflows/service.py:900`：提交后保存 response 和 interaction_history，运行重新排队。
9. `mira_engine/workflows/confirmations.py:28`：只有 selection/decision/approval 为 approve 才算批准。

当前 UI 是模块工作区中的确认卡片，不是根据缺失字段动态生成的多问题弹窗。

## 用户提到的旧版 QA form

旧版规划问卷在 `mira_engine/agent/research_loop.py:884` 附近通过 `set_plan(phase='questions')` 生成 3–6 个问题，可包含单选、多选、文本题。前端由 `mira-ui/src/components/stages/PlanView.tsx` 渲染。

当前原生 ModuleRun 在 `mira_engine/modules/executors.py:22` 将 `set_plan` 列入禁用工具。因此旧规划问卷并没有直接用于调研阶段的交付物补全。当前采用的是 `request_workflow_interaction` 对应的独立交互机制。

## 边界与缺口

| 情况 | 当前验证结果 | 影响 |
|---|---|---|
| 用户只写“帮我调研肝脏MRI” | 输入验证通过 | 无独立的交付物必填检查 |
| 从 UI 启动调研 | 默认 required 确认 | 有执行前确认机制，但表单内容由模型生成 |
| AI 未确认就尝试 web_search | 被 runtime guard 拦截 | 搜索不能正常绕过 required 确认 |
| AI 确认摘要只写“检索肝脏MRI相关资料” | 交互工具接受 | 不校验摘要是否真的含预期交付物 |
| 用户选“修改”但不写说明 | 前端提交按钮禁用 | 能防止空修改意见 |
| 用户选“修改”并补充 3 个方向及内容要求 | selection=revise，notes 完整保存；尚未批准 | 模型应修订摘要并再次确认 |
| 用户选“批准”并补充文本 | notes 保存，立即判为批准 | 补充文本不会自动被识别为必须重新确认的范围变更 |
| 用户仅输入自由文本而不选批准/修改 | 文本被作为 selection；后端接受，但不判为批准 | 按钮文案可能是“批准并继续”，实际仍未完成批准；需要后续再次确认 |
| 通过服务/API 创建运行但不传 confirmation_policy | 不要求确认 | 不是所有入口都默认强制确认；UI 的保证不能泛化到所有 API 调用 |
| 研究结果 directions=[] | 数据模型允许 | 没有“至少几个方向”的结构化验收条件；不代表所有产品场景都应强制非空 |

另有一层默认提示：`mira_engine/modules/prompts.py:23` 已要求提出多个有权衡的研究方向；UI 的调研启动提示也要求候选方向。因此用户未明确说“给几个方向”，不等于模型完全没有这项默认要求。但它没有指定数量，也没有对应的强制验收约束。

## 验证证据与边界

### 自动化测试

- 后端既有测试：45 passed。
  - `tests/workflows/test_confirmations.py`
  - `tests/workflows/test_executors.py`
  - `tests/workflows/test_tool.py`
  - `tests/workflows/test_service.py`
  - `tests/workflows/test_initiation_literature_regression.py`
- 前端既有测试：3 files / 17 passed。
  - `ModuleStageControls.test.tsx`
  - `WorkflowWorkspace.test.tsx`
  - `workflowCandidates.test.ts`
- 新增临时定向探针：7 passed，见同目录 `test_research_clarification_probe.py`。
  - 验证模糊目标、调研搜索拦截、缺少交付物的确认摘要、三种响应语义、无确认策略的 API 运行和零方向的结果模型。

合计 69 项通过。这些测试验证的是当前行为，其中部分断言有意记录现有缺口，通过不代表产品已满足全部需求。

定向探针使用临时项目与 scripted provider，不调用真实模型或外部搜索。首次运行有一项因默认全局备份目录位于沙箱外而失败；将测试中的全局 workspace 路径隔离到临时目录后，7 项全部通过，没有为此修改产品代码。

### 浏览器验证

使用 browse 技能访问正在运行的 `http://localhost:5173`，加载实际 `WorkflowInteractionPanel` 组件，注入模拟的 approve_research_scope 数据，在浏览器内捕获提交，不修改真实项目，不调用真实模型。

已确认：

- 初始未选择时“批准并继续”按钮禁用。
- 选择“修改”但不填说明时“提交修改意见”禁用。
- 填写补充要求后可提交。
- 捕获的提交内容为：

```json
{
  "interaction_id": "qa-clarification",
  "response": {
    "selection": "revise",
    "notes": "请整理3个研究方向，每个包含创新点、数据需求和可行性。"
  }
}
```

- 组件同时发出了继续执行的 WebSocket 消息（已在浏览器内拦截）。
- 验证场景没有浏览器 console 错误。

截图：[实际组件的补充意见表单](revision-form-verified.png)。截图中的问题文本是 QA fixture，不是真实模型自动生成的结果。浏览器脚本见 `browser-form-probe.js`。

未验证：真实模型面对该模糊请求时，每次是否都生成完整确认摘要、是否主动问到方向数量，以及用户修改后真实模型如何修订并再次发起确认。不能把本轮组件验证说成完整的真实模型端到端测试。

## 建议（尚未实施）

复用当前交互组件，补上明确的交付物确认契约：

- 调研成果类型：文献综述、研究方向、方法比较，或用户自定义。
- 方向数量：由用户填写或确认 AI 提议的默认值。
- 每个方向的必需内容：创新点、证据、数据需求、方法与可行性等。
- 输出形式：界面结果、Markdown、Word 或其他需要的产物。

允许用户选择“由 Mira 建议”，但将建议的交付物内容明确展示并保存。最终验收按已确认的要求检查，而不是对所有调研任务一律强制生成固定数量的方向。

如果希望继续采用多问题 QA form，需要给原生 ModuleRun 增加对应的澄清数据结构和渲染接入；不宜直接恢复旧规划工具，因为其状态与流程属于另一套规划机制。

## 工作区影响

仅新增 QA 报告、探针脚本和截图；没有修改 MIRA 或 mira-ui 产品代码，没有提交 git，也没有修改真实项目数据。两个工作区原有未提交更改予以保留。
