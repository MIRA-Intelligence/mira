return (async () => {
  const React = (await import('/node_modules/.vite/deps/react.js')).default;
  const { createRoot } = (await import('/node_modules/.vite/deps/react-dom_client.js')).default;
  const { flushSync } = (await import('/node_modules/.vite/deps/react-dom.js')).default;
  const { WorkflowInteractionPanel } = await import('/src/components/workflow/WorkflowInteractionPanel.tsx');
  const { useWorkflowStore } = await import('/src/stores/workflowStore.ts');
  const { useSettingsStore } = await import('/src/stores/settingsStore.ts');
  const { wsClient } = await import('/src/services/websocket.ts');
  window.__clarificationQA = { commands: [], messages: [] };
  useWorkflowStore.setState({commandRun: async (...args) => {
    window.__clarificationQA.commands.push(args);
  }});
  wsClient.send = (message) => window.__clarificationQA.messages.push(message);
  useSettingsStore.setState({language: 'zh'});
  document.getElementById('root').style.display = 'none';
  document.querySelectorAll('#clarification-qa').forEach(element => element.remove());
  const mount = document.createElement('div');
  mount.id = 'clarification-qa';
  mount.style.cssText = 'max-width:800px;margin:48px auto;padding:24px;background:#fff;color:#111;border:1px solid #ddd;border-radius:16px';
  document.body.appendChild(mount);
  const run = {
    run_id: 'qa-research-scope', module_type: 'initiation', status: 'waiting_input',
    module_data: {confirmation_policy:{mode:'required'}, interaction:{
      interaction_id:'qa-clarification', interaction_type:'approve_research_scope',
      title:'调研范围与预期成果确认',
      prompt:'用户原始请求：帮我调研肝脏MRI。\n拟调研范围：相关文献与研究空白。\n如需研究方向清单，请补充数量及各方向需要包含的内容。',
      choices:[{id:'approve', label:'批准', value:'approve'}, {id:'revise', label:'修改', value:'revise'}],
      allow_freeform:true, response_schema:{type:'object',required:['selection'],properties:{selection:{type:'string'},notes:{type:'string'}}}
    }}
  };
  flushSync(() => createRoot(mount).render(React.createElement('div', null,
    React.createElement('h2', {style:{fontSize:'22px',marginBottom:'12px'}}, 'QA：实际表单组件 / 模拟交互数据'),
    React.createElement('p', {style:{marginBottom:'20px'}}, '仅浏览器内测试，提交被捕获，不修改真实项目，不调用模型。'),
    React.createElement(WorkflowInteractionPanel, {projectId:'QA-ONLY',run})
  )));
  await new Promise(resolve => setTimeout(resolve, 250));
  return {fixture:true, formVisible:document.getElementById('clarification-qa').textContent.includes('批准并继续')};
})();
