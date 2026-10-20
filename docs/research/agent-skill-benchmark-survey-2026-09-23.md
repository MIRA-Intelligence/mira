# 智能体与 Skills Benchmark 调研：MIRA 可复用方案

- 调研快照日期：2026-09-23
- 本地归档日期：2026-09-24
- 状态：已核查官方仓库、数据说明和部分实际任务；尚未安装评测环境，尚未在 MIRA 上运行这些 benchmark，未产生模型成绩。
- 本文归档上一轮调研结果，归档时未重新检查外部项目更新。
- 优先级、接入难度和试点规模属于针对 MIRA 的建议，不是已经验证的接入结果。

## 1. 结论

建议首先采用以下五套评测：

1. **SkillsBench**：评估加入 skills 后的实际增益，以及智能体使用 skills 的能力。
2. **ScienceAgentBench**：评估科研代码生成、科学计算及结果复现。
3. **SpreadsheetBench**：评估 Excel 数据处理和文件交付。
4. **Terminal-Bench**：评估多步执行、终端操作和排错能力。
5. **BFCL**：诊断模型的工具选择、参数生成和多轮工具调用能力。

文献检索与生物信息分析可进一步采用 **LAB-Bench** 和 **BixBench**。

这批公共 benchmark 可以提供横向比较，但不足以覆盖 MIRA 核心的 DICOM/NIfTI、影像组学、生存分析、患者级数据划分和数据泄漏检查。需要另补带标准答案与隐藏检查的领域任务。

## 2. 先明确评测目标

| 要回答的问题 | 比较方法 | 解释边界 |
|---|---|---|
| 哪个模型更适合 MIRA？ | 固定 MIRA、工具、skills 和预算，只更换模型 | 不把框架、工具或预算的变化归因于模型 |
| 一个 skill 是否有效？ | 固定模型和执行环境，对比不加载与加载该 skill | 同时检查 skill 是否被正确选择和实际使用 |
| MIRA 整体能力怎么样？ | 运行真实任务，检查最终文件、计算结果和任务完成情况 | 测到的是模型、框架、工具和 skills 的组合能力 |

很多榜单测到的是“模型＋智能体框架＋工具”的组合能力，不能直接把榜单差异归因于模型或 skill。

## 3. 优先采用的现成评测

### 3.1 SkillsBench

- **用途**：专门评估 skills 的增益；任务包括指令、环境、skills、参考解和验证器。
- **MIRA 价值**：第一优先。可比较现有 skills、外部 skills，以及不同模型能否正确使用它们。
- **接入方式**：调研时当前仓库采用 BenchFlow，支持本地 Docker；需要接入 MIRA 执行器。
- **复用条件**：仓库采用 Apache-2.0；实际引用的第三方资源仍需检查对应条款。
- **版本注意**：论文介绍的是 86 个任务、11 个领域；当前 registry 的 v1.1 为 87 个任务，规定 BenchFlow 兼容版本为 `>=0.6.3,<0.7`。应固定 release 和配套运行框架，避免混用论文结果和主分支。

已查看的任务例子：

| 任务 | 内容 | 与 MIRA 的关联 |
|---|---|---|
| `citation-check` | 检查 BibTeX 中的虚假引用，输出结构化结果 | 文献核验、论文写作中的引用质量 |
| `exceltable-in-ppt` | 修改 PPT 内嵌 Excel 表格，保留公式和其他内容 | 文档与演示文稿处理、产物完整性 |
| `xlsx-recover-data` | 利用跨工作表关系恢复缺失数值 | 表格理解、关系推断和数据处理 |

来源：[官方仓库](https://github.com/benchflow-ai/skillsbench)、[论文](https://arxiv.org/abs/2602.12670)、[版本清单](https://github.com/benchflow-ai/skillsbench/blob/main/registry.json)、[citation-check](https://github.com/benchflow-ai/skillsbench/blob/v1.1/tasks/citation-check/task.md)、[exceltable-in-ppt](https://github.com/benchflow-ai/skillsbench/blob/v1.1/tasks/exceltable-in-ppt/task.md)、[xlsx-recover-data](https://github.com/benchflow-ai/skillsbench/blob/v1.1/tasks/xlsx-recover-data/task.md)。

### 3.2 ScienceAgentBench

- **用途**：原始版本从 44 篇论文提取 102 个科研任务，要求生成可运行的 Python 程序。
- **MIRA 价值**：第一优先。贴近数据分析、科学计算和科研结果复现。
- **接入方式**：有 Docker 评分环境，可以复用任务、数据和评分程序。
- **版本注意**：官方在 2026-04-30 发布 verified 版本，以减少评分中的假阴性；应采用 verified split 和配套 `benchmark_verified.zip`，不要混用旧版 artifacts。
- **复用条件**：代码为 MIT，多数任务为 CC BY 4.0，部分任务保留上游许可。作者要求不要在线再分发解压后的数据文件。
- **解释边界**：102 是原始版本的任务数量，本轮未另行统计 verified split 的实际条目数。

来源：[官方仓库、版本更新与许可说明](https://github.com/OSU-NLP-Group/ScienceAgentBench)、[论文](https://arxiv.org/abs/2410.05080)。

### 3.3 SpreadsheetBench

- **用途**：912 个真实表格任务，另有专家核验的 400 题 Verified 子集；根据输出表格评分。
- **MIRA 价值**：第一优先。测试 Excel 清洗、计算、跨表处理和文件交付。
- **接入方式**：沿用任务和评分器，接入 MIRA 的文件处理流程。
- **运行要求**：公式需要 Excel 或 LibreOffice 重算，以便评分程序读取计算后的单元格值。
- **复用条件**：CC BY-SA 4.0。
- **版本注意**：本次推荐先使用 V1 的 Verified 400 子集。官网已展示 V2；本轮没有深入核查 V2 的任务和接入细节。

来源：[官方仓库](https://github.com/RUCKBReasoning/SpreadsheetBench)、[官网](https://spreadsheetbench.github.io/)、[数据与 Verified 子集](https://huggingface.co/datasets/KAKA22/SpreadsheetBench/tree/main)。

### 3.4 Terminal-Bench 2.0

- **用途**：容器内的实际终端任务，包括程序调试和复杂环境操作。
- **MIRA 价值**：第一优先。测试持续执行、排错和任务完成能力。
- **接入方式**：官方运行框架是 Harbor，支持自定义 agent；需要 Docker 和 MIRA adapter。
- **接口依据**：官方 README 说明，可通过继承 `BaseInstalledAgent` 或 `BaseAgent` 接入自己的智能体。
- **复用条件**：仓库采用 Apache-2.0；任务依赖仍需按具体内容核查。
- **版本注意**：建议固定 2.0 建立基线，此建议不意味着它一定是所有渠道中的最新版本。

来源：[官方仓库与接入说明](https://github.com/harbor-framework/terminal-bench-2)、[许可证](https://github.com/harbor-framework/terminal-bench-2/blob/main/LICENSE)。

### 3.5 BFCL：Berkeley Function Calling Leaderboard

- **用途**：工具选择、参数正确性、并行调用、多轮工具使用；V4 扩展到 agentic evaluation。
- **MIRA 价值**：第一优先的底层诊断，适合筛查模型工具调用短板。
- **接入方式**：可以先用官方 runner 测模型，再验证 MIRA provider 的实际调用路径。
- **复用条件**：公开 Hugging Face 数据集标注 Apache-2.0。
- **版本注意**：数据、runner 和评分版本必须配套；HF 数据卡的部分介绍仍围绕早期版本，不应据此认为已取得完整 V4 数据。
- **解释边界**：工具调用成绩不能替代真实科研任务完成率。

来源：[官方榜单与版本说明](https://gorilla.cs.berkeley.edu/leaderboard)、[评测代码](https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard)、[公开数据集](https://huggingface.co/datasets/gorilla-llm/Berkeley-Function-Calling-Leaderboard)。

### 3.6 BixBench

- **用途**：调研时 v1.5 包含 205 个问题，要求分析真实生物数据，支持 Python、R 和 Bash。
- **MIRA 价值**：适合生物统计、生信和科学计算 skills，与医学影像流程的相关性较间接。
- **接入方式**：官方说明了自定义 agent 接入方式：实现符合要求的轨迹输出，复用后处理和评分流程。
- **运行要求**：需要数据 capsules 和 Docker。完整多模型、多次重复的评测成本较高，适合先选小规模任务验证。
- **复用条件**：公开数据集标注 Apache-2.0；原始科学数据的来源条件仍需逐项核查。
- **评测模式**：开放回答与选择题模式应分别报告，不混合比较。

来源：[官方仓库](https://github.com/Future-House/BixBench)、[数据集](https://huggingface.co/datasets/futurehouse/BixBench)、[论文](https://arxiv.org/abs/2503.00096)。

### 3.7 LAB-Bench

- **用途**：文献全文问答 LitQA2、数据库查询 DbQA、补充材料 SuppQA、科学图表 FigQA/TableQA 等。
- **MIRA 价值**：适合文献搜索、论文阅读和图表理解 skills。
- **接入方式**：提供 agent 函数接口，返回题目答案。
- **数据开放范围**：公开约 80%，保留约 20% 私有测试集。
- **复用条件**：CC BY-SA 4.0。
- **解释边界**：选择题成绩不能覆盖完整综述的结构、证据完整性和引用质量。

来源：[官方仓库](https://github.com/Future-House/LAB-Bench)、[许可证](https://github.com/Future-House/LAB-Bench/blob/main/LICENSE)。

## 4. 第二批或针对特定能力采用的评测

| Benchmark | 适合测试什么 | 建议与限制 | 来源 |
|---|---|---|---|
| DiscoveryBench | 根据数据和研究目标，通过统计分析形成假设或发现；包含 264 个真实任务以及合成任务 | 适合后续测试“数据→发现→解释”；开放答案评分需要人工抽查 | [仓库](https://github.com/allenai/discoverybench)、[论文](https://arxiv.org/abs/2407.01725) |
| DSBench | 真实数据分析和建模，任务来自 ModelOff、Kaggle | 内容适合，但官方明确限制非商业使用，商业开发需书面许可；不能直接作为无限制的产品回归集 | [仓库与使用限制](https://github.com/LiqiangJing/DSBench) |
| GAIA | 搜索、文件阅读和多步推理的综合助手能力 | 适合通用研究助理；下载有访问条件，禁止以可抓取形式再分发，联网任务受网页变化影响 | [官方数据卡](https://huggingface.co/datasets/gaia-benchmark/GAIA) |
| τ-bench 系列 | 多轮用户沟通、工具操作和业务规则遵循 | 适合澄清、状态跟踪和流程执行；领域以客服为主，与医学科研关联较弱 | [官方仓库](https://github.com/sierra-research/tau2-bench) |
| MLE-bench | 75 个 Kaggle 机器学习工程任务 | 适合建模、实验管理和调参；计算量较大，代码许可不覆盖外部竞赛数据 | [仓库](https://github.com/openai/mle-bench)、[许可范围](https://github.com/openai/mle-bench/blob/main/LICENSE)、[论文](https://arxiv.org/abs/2410.07095) |
| PaperBench | 从头复现 20 篇 AI 研究论文 | 适合长期科研能力评测；完整评测需要较多计算资源。Code-Dev 较便宜，但不能证明实验结果已复现 | [官方任务与运行说明](https://github.com/openai/frontier-evals/tree/main/project/paperbench) |
| SWE-bench Verified | 500 个经人工筛选的软件仓库问题 | 适合工程类 skills 和研究代码修复，不宜作为医学科研能力的主要指标 | [官网](https://www.swebench.com/) |
| WebArena-Verified | 浏览器内的任务执行 | 发展浏览器控制能力时再投入，需适配环境与交互 | [官方文档](https://servicenow.github.io/webarena-verified/) |
| OSWorld | 真实桌面应用操作 | 发展桌面控制能力时再投入，需虚拟机等环境及交互适配 | [官方仓库](https://github.com/xlang-ai/OSWorld) |

τ-bench 版本注意：调研时 `tau2-bench` 仓库已发展到 τ³。README 提醒，2026 年 7 月的 v1.0.1 修改了 `banking_knowledge` 部分评分，受影响领域的新旧版本结果不可直接比较。

以上为候选资源筛选，不代表已对第二批项目逐项完成数据下载、全部任务验证或完整许可证审查。

## 5. 可参考的外部 skill 对照案例

SciAgent-Skills 作者报告，在 BixBench-Verified-50 上，Claude Code（Opus 4.6）基线为 65.3%，加入 SciAgent-Skills 后为 92.0%，差异为 26.7 个百分点。

这是作者报告的特定子集和配置结果，本轮没有复现。可以参考其对照思路，但不能当作完整 BixBench 的成绩，也不能推断 MIRA 会获得同样增益。

来源：[SciAgent-Skills 项目与实验说明](https://github.com/jaechang-hits/SciAgent-Skills)。

## 6. 可以复用的评测方法与 MIRA 本地组件

Anthropic 的 skill-creator 提供了以下流程：

- 有 skill / 无 skill 的对照。
- 根据断言对产物评分。
- 汇总通过率、耗时、token 及配置间差异。
- 查看产物并进行人工评价。

它适合构建自己的测试集，不是覆盖各领域的标准题库。

来源：[Anthropic skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator)、[结果与评测数据格式](https://github.com/anthropics/skills/blob/main/skills/skill-creator/references/schemas.md)。

本轮查看的 MIRA 本地组件：

| 文件 | 已确认的行为 | 复用判断 |
|---|---|---|
| `mira_engine/skills/builtin_skills/engineering/skill-creator/scripts/aggregate_benchmark.py` | 汇总 `with_skill` 与 `without_skill` 的结果及差异 | 可作为结果汇总组件的起点 |
| `mira_engine/skills/builtin_skills/engineering/skill-creator/scripts/run_eval.py` | 通过启动 `claude -p` 测试 skill 描述是否触发使用 | 执行端需要适配，不能把它的成绩直接当成 MIRA 的 skill 选择成绩 |

这些是源码检查结果，不代表本轮执行过这些脚本。MIRA adapter 的具体入口和运行行为仍需在实现阶段验证。

## 7. 建议的第一轮试点

### 7.1 任务组成

| 来源 | 建议起步数量 | 目的 |
|---|---:|---|
| SkillsBench | 10 个任务 | 文献核验、文档、表格及 skill 增益 |
| ScienceAgentBench verified | 10 个任务 | 科研代码与计算结果 |
| SpreadsheetBench Verified | 5 个任务 | 表格处理与交付 |
| Terminal-Bench 2.0 | 5 个任务 | 多步执行与排错 |
| BFCL | 另选 100 个分层样本 | 单独诊断工具调用，不混入上述任务完成率 |

以上数量是试点建议，尚未选定具体样本。子集成绩应标记为内部评测，不作为官方全量成绩。

### 7.2 skill 对照设计

对适合 skill 对照的任务，固定模型、工具权限和运行预算，比较：

1. **关闭 skills**：测基础能力。
2. **自动选择 skills**：测实际产品中的发现、选择与使用效果。
3. **明确指定相关 skill**：区分 skill 内容无效与有用 skill 未被正确选择。

每组重复运行，报告任务成功率、成本、耗时和失败原因。最终文件与数值优先用程序判定，开放性解释再使用盲评和人工抽查。

比较不同模型时，保持上述配置一致。固定任务版本与配套评分器；不要把更换环境、预算或评分版本后的变化归因于模型能力。

### 7.3 MIRA 领域任务补充

公共 benchmark 之外，建议补充一小批带标准答案与隐藏检查的任务，覆盖：

- DICOM/NIfTI 数据读取、元数据和几何处理。
- 影像组学流程与特征处理。
- 生存分析的模型、统计结果和结果解释。
- 患者级数据划分。
- 数据泄漏检查。

公共集用于横向比较，自有集用于验证 MIRA 实际承诺的科研能力。这些自有任务尚未编写。

## 8. 当前完成情况与未完成项

已完成：

- 筛选上述公共 benchmark 与相关评测方法。
- 核查主要项目的官方仓库、数据说明、部分使用条件和版本差异。
- 查看 SkillsBench 的三个实际任务。
- 查看 MIRA 本地 skill 触发评测及结果汇总脚本。
- 提出分层评测目标、候选试点规模和 skill 对照方案。

尚未完成：

- 下载并完整核验各 benchmark 的数据、任务和运行环境。
- 实现 MIRA 与各评测框架的 adapter。
- 选定试点样本及模型配置。
- 验证参考解、评分器及实际运行成本。
- 运行模型或 skills 对照实验。
- 编写 MIRA 自有医学科研任务。

因此本文可用于决定接入顺序，不能作为已完成接入或模型能力的证据。
