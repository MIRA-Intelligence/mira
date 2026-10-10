return (async () => {
  const React = (await import('/node_modules/.vite/deps/react.js')).default;
  const { createRoot } = (await import('/node_modules/.vite/deps/react-dom_client.js')).default;
  const { flushSync } = (await import('/node_modules/.vite/deps/react-dom.js')).default;
  const { WorkflowInteractionPanel } = await import('/src/components/workflow/WorkflowInteractionPanel.tsx');
  // Follow Vite's exact HMR URL so the fixture and actual component share one store.
  const source = await (await fetch('/src/components/workflow/WorkflowInteractionPanel.tsx')).text();
  const storeUrl = source.match(/from\s+"([^"]*\/stores\/workflowStore\.ts[^"]*)"/)?.[1];
  if (!storeUrl) throw new Error('Cannot resolve the component store import');
  const { useWorkflowStore } = await import(storeUrl);
  const { useSettingsStore } = await import('/src/stores/settingsStore.ts');
  const { wsClient } = await import('/src/services/websocket.ts');
  const qa = window.__repeatableQA = { commands: [], messages: [], stage: 1 };
  useWorkflowStore.setState({ commandRun: async (...args) => { qa.commands.push(args); } });
  wsClient.send = (message) => qa.messages.push(message);
  useSettingsStore.setState({ language: 'zh' });
  document.getElementById('root').style.display = 'none';
  const mount = document.createElement('div');
  mount.id = 'repeatable-qa';
  mount.style.cssText = 'max-width:800px;margin:48px auto;padding:24px;background:#fff;color:#111;border:1px solid #ddd;border-radius:16px';
  document.body.appendChild(mount);
  const root = createRoot(mount);
  qa.render = () => {
    const second = qa.stage === 2;
    const run = {
      run_id: 'qa-repeatable', module_type: 'initiation', status: 'waiting_input',
      module_data: { interaction: {
        interaction_id: `qa-question-${qa.stage}`,
        interaction_type: second ? 'resolve_conflict' : 'clarify_requirements',
        title: second ? '新发现：现有数据不支持外部验证' : '本次调研希望帮助你做出什么决定？',
        prompt: second
          ? '已保留你的上一轮选择：基于现有数据提出可实验的候选方向。现在发现没有独立外部队列，建议调整验证范围。'
          : '你还未指定成果方向。明确这一点后，我会按证据和可行性归纳候选项，供你选择下一阶段的实验。',
        choices: second ? [
          { id: 'internal', label: '先做内部验证（推荐）', description: '保留现有数据边界，明确外部泛化尚未验证。' },
          { id: 'wait', label: '等待补充外部队列', description: '暂缓依赖外部数据的实验，不推断数据已经存在。' },
        ] : [
          { id: 'feasible', label: '基于现有数据的可行方向（推荐）', description: '给出少量有证据依据的候选方向、风险和最小验证实验。' },
          { id: 'overview', label: '只做领域综述与比较', description: '总结证据和研究空白，不强制提出实验候选项。' },
          { id: 'suggest', label: '请 AI 建议成果方向', description: '先给出适合当前目标的推荐，再继续调研。' },
        ],
        allow_freeform: true,
        response_schema: { type: 'object', required: ['selection'], properties: {
          selection: { type: 'string' }, notes: { type: 'string' },
        } },
      } },
    };
    flushSync(() => root.render(React.createElement('div', null,
      React.createElement('h2', { style: { fontSize: '22px', marginBottom: '12px' } }, `QA：连续交互确认，第 ${qa.stage} 轮`),
      React.createElement('p', { style: { marginBottom: '20px' } }, '实际表单组件 / 模拟数据。提交只在浏览器内捕获，不修改真实项目。'),
      React.createElement(WorkflowInteractionPanel, { projectId: 'QA-ONLY', run }),
    )));
  };
  qa.next = () => { qa.stage = 2; qa.render(); return { stage: qa.stage, commands: qa.commands }; };
  qa.verify = () => ({
    count: qa.commands.length,
    commands: qa.commands,
    candidateSelection: useWorkflowStore.getState().selectedCandidateByProject['QA-ONLY'] ?? null,
    cleanConsoleExpected: true,
  });
  qa.render();
  return { mounted: true, stage: qa.stage };
})();
