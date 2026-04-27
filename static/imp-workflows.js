/* imp-workflows.js — workflows tab */

let _allTools = [];
let editingWorkflow = null;

async function loadWorkflows() {
  try {
    const res = await fetch(`${API}/api/workflows`);
    const data = await res.json();
    const workflows = data.workflows || [];
    const toolsList = data.tools || [];
    renderToolBrowser(toolsList);
    const el = document.getElementById('workflows-list');
    if (!workflows.length) {
      el.innerHTML = '<div class="queue-empty">No workflows found.<br>Add step scripts to workflows/&lt;name&gt;/</div>';
      return;
    }

    const openState = imp.getOpenState(el);

    const anyBusy = workflows.some(w => w.status === 'running' || w.status === 'paused');

    let html = '';
    for (const wf of workflows) {
      const statusClass = wf.status || 'idle';
      const statusIcons = {idle:'⚪', running:'🔄', paused:'⏸', done:'✅', error:'❌'};
      const sIcon = statusIcons[wf.status] || '⚪';
      const isThisBusy = wf.status === 'running' || wf.status === 'paused';

      let stepsHtml = '', readmeHtml = '', rawReadme = '';
      try {
        const sres = await fetch(`${API}/api/workflows/${wf.name}`);
        const sdata = await sres.json();
        rawReadme = sdata.readme || '';
        if (sdata.readme) readmeHtml = marked.parse(sdata.readme);
        const steps = sdata.steps || [];
        stepsHtml = steps.map(s => {
          const stepIcon = {done:'✅', running:'🔄', paused:'⏸', pending:'⏳', error:'❌'}[s.status] || '⏳';
          const dur = s.result?.duration_s ? ` · ${s.result.duration_s}s` : '';
          let stepBody = '';
          if (s.result) {
            const ok = (s.result.ok === false) ? '❌' : '✅';
            const rdur = s.result.duration_s ? ` · ${s.result.duration_s}s` : '';
            const content = s.result.output || s.result.error || '';
            const tb = s.result.traceback || '';
            let outHtml = '';
            if (content) outHtml += formatStepOutput(content);
            if (tb) outHtml += imp.fold('Traceback', formatStepOutput(tb));
            stepBody += `<div class="wf-step-output"><strong>${ok} ${s.result.ok !== false ? 'OK' : 'Error'}${rdur}</strong>${outHtml}</div>`;
          }
          if (s.source) stepBody += imp.code(s.source);
          const editBtns = editingWorkflow === wf.name ? [
            imp.btn('▲', `moveStep('${wf.name}','${s.name}','up')`),
            imp.btn('▼', `moveStep('${wf.name}','${s.name}','down')`),
            imp.btn('−', `removeStep('${wf.name}','${s.name}')`, {cls:'danger'})
          ] : [];
          const stepName = `${editBtns.join('')}${stepIcon} ${s.description || s.name}${dur}`;
          return imp.item({id: `step-${wf.name}-${s.name}`, name: stepName, status: s.status, body: stepBody});
        }).join('');
      } catch (e) {}

      const startBtn = isThisBusy
        ? imp.btn('Interrupt', `interruptWorkflow('${wf.name}')`, {cls:'danger'})
        : imp.btn('Start', `startWorkflow('${wf.name}')`, {cls:'primary', disabled: anyBusy});
      const wfBtns = [
        startBtn,
        imp.btn(editingWorkflow === wf.name ? 'Done' : 'Edit', `toggleEditWorkflow('${wf.name}')`, {disabled: anyBusy}),
        imp.btn('P', `promptWorkflow('${wf.name}')`, {cls:'small', title:'AI edits workflow', disabled: anyBusy}),
        imp.btn('Clone', `cloneWorkflow('${wf.name}')`, {disabled: anyBusy}),
        imp.btn('Rename', `renameWorkflow('${wf.name}')`, {disabled: anyBusy}),
        imp.btn('Delete', `deleteWorkflow('${wf.name}')`, {cls:'danger', disabled: anyBusy})
      ];

      const ranAt = wf.ran_at ? `Last run: ${new Date(wf.ran_at).toLocaleString()}` : '';
      let bodyHtml = imp.meta(`${wf.description}${ranAt ? ' · <em>'+ranAt+'</em>' : ''}`);
      if (editingWorkflow === wf.name)
        bodyHtml += imp.readmeEdit(`wf-readme-${wf.name}`, rawReadme).replace('id="wf-readme-'+wf.name+'"', `data-wf="${wf.name}" onblur="saveReadme(this)" id="wf-readme-${wf.name}"`);
      else
        bodyHtml += imp.readme(readmeHtml);
      bodyHtml += imp.items(stepsHtml);

      html += imp.card({
        id: `wf-${wf.name}`, name: wf.name, meta: `${wf.step_count} steps`,
        status: {cls: statusClass, icon: sIcon, text: wf.status},
        buttons: wfBtns, body: bodyHtml,
        open: wf.status !== 'idle'
      });
    }
    el.innerHTML = html;

    imp.restoreOpenState(el, openState);
    if (_chatSourceLock && _chatSourceLock.type === 'workflow') {
      imp.applyLock(el, _chatSourceLock.id, 'AI is editing in Chat tab...');
    }
  } catch (e) { console.error('loadWorkflows failed:', e); }
}

async function startWorkflow(name) {
  try {
    // Immediately disable all buttons across all workflows
    document.querySelectorAll('#workflows-list .wf-btn, #workflows-list .wf-start').forEach(b => b.disabled = true);
    await fetch(`${API}/api/workflows/${name}/start`, {method:'POST'});
    await loadWorkflows();
    const poll = setInterval(async () => {
      if (activeTab !== 'workflows') { clearInterval(poll); return; }
      await loadWorkflows();
      try {
        const res = await fetch(`${API}/api/workflows/${name}`);
        const data = await res.json();
        if (data.status === 'done' || data.status === 'error' || data.status === 'idle' || data.status === 'paused')
          clearInterval(poll);
      } catch(e) { clearInterval(poll); }
    }, 800);
  } catch (e) { console.error('startWorkflow failed:', e); }
}

async function interruptWorkflow(name) {
  try {
    await fetch(`${API}/api/workflows/${name}/abort`, {method:'POST'});
    await loadWorkflows();
  } catch (e) { console.error('interruptWorkflow failed:', e); }
}

function renderToolBrowser(tools) {
  _allTools = tools;
  const el = document.getElementById('tools-list');
  let html = '<input id="tool-search" type="text" placeholder="Search tools..." oninput="filterTools()" style="width:100%;padding:6px 10px;background:var(--input-bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;font-size:12px;margin-bottom:10px;outline:none;">';
  html += '<div id="tools-filtered"></div>';
  el.innerHTML = html;
  filterTools();
}

function filterTools() {
  const q = (document.getElementById('tool-search')?.value || '').toLowerCase();
  const filtered = q ? _allTools.filter(t => t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q) || t.group.toLowerCase().includes(q)) : _allTools;
  const el = document.getElementById('tools-filtered');
  if (!filtered.length) { el.innerHTML = '<div class="queue-empty" style="padding:16px 0;">No matches</div>'; return; }
  const groups = {};
  filtered.forEach(t => { if (!groups[t.group]) groups[t.group] = []; groups[t.group].push(t); });
  let html = '';
  for (const [group, items] of Object.entries(groups)) {
    html += `<div class="tool-group"><div class="tool-group-header">${group}/</div>`;
    items.forEach(t => {
      html += `<div class="tool-item" onclick="showToolDetail('${t.group}','${t.name}')">
        <div style="display:flex;align-items:center;gap:6px;">
          <button class="tool-add" onclick="event.stopPropagation();addToolToWorkflow('${t.group}','${t.name}')">+</button>
          <div class="tool-name">${t.name}</div>
        </div>
        <div class="tool-desc">${t.description}</div>
      </div>`;
    });
    html += '</div>';
  }
  el.innerHTML = html;
}

function showToolDetail(group, name) {
  document.getElementById('queue-popup-title').textContent = `${group}/${name}`;
  document.getElementById('queue-popup-body').innerHTML = '<em>Loading...</em>';
  document.getElementById('queue-popup-actions').innerHTML = '';
  const closeBtn = document.createElement('button');
  closeBtn.textContent = 'Close';
  closeBtn.className = 'action-close';
  closeBtn.onclick = closePopup;
  document.getElementById('queue-popup-actions').appendChild(closeBtn);
  document.getElementById('queue-popup').style.display = '';
  fetch(`${API}/api/tool-source?group=${group}&name=${name}`)
    .then(r => r.json())
    .then(d => {
      document.getElementById('queue-popup-body').innerHTML = d.docstring ? marked.parse(d.docstring) : `<pre>${(d.source||'').replace(/</g,'&lt;')}</pre>`;
    })
    .catch(() => { document.getElementById('queue-popup-body').innerHTML = '<em>Could not load</em>'; });
}

function toggleEditWorkflow(name) {
  if (editingWorkflow === name) {
    editingWorkflow = null;
    document.getElementById('wf-tools').style.display = 'none';
    loadWorkflows();
    return;
  } else {
    editingWorkflow = name;
    document.getElementById('wf-tools').style.display = '';
  }
  loadWorkflows();
}

function addToolToWorkflow(group, name) {
  if (!editingWorkflow) { alert('Click Edit on a workflow first'); return; }
  fetch(`${API}/api/workflows/${editingWorkflow}/add-step`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tool_group: group, tool_name: name}),
  }).then(() => loadWorkflows()).catch(e => alert('Add failed: ' + e));
}

function cloneWorkflow(name) {
  const newName = prompt(`Clone "${name}" as:`, name + '_copy');
  if (!newName || !newName.trim()) return;
  fetch(`${API}/api/workflows/${name}/clone`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({new_name: newName.trim()}),
  }).then(() => loadWorkflows()).catch(e => alert('Clone failed: ' + e));
}

function renameWorkflow(name) {
  const newName = prompt(`Rename "${name}" to:`, name);
  if (!newName || !newName.trim() || newName.trim() === name) return;
  fetch(`${API}/api/workflows/${name}/rename`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({new_name: newName.trim()}),
  }).then(() => { if (editingWorkflow === name) editingWorkflow = newName.trim(); loadWorkflows(); })
    .catch(e => alert('Rename failed: ' + e));
}

function deleteWorkflow(name) {
  if (!confirm(`Delete workflow "${name}" and all its steps?`)) return;
  fetch(`${API}/api/workflows/${name}/delete`, {method: 'POST'})
    .then(() => { if (editingWorkflow === name) { editingWorkflow = null; document.getElementById('wf-tools').style.display = 'none'; } loadWorkflows(); })
    .catch(e => alert('Delete failed: ' + e));
}

function moveStep(wfName, stepName, direction) {
  fetch(`${API}/api/workflows/${wfName}/move-step`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({step_name: stepName, direction}),
  }).then(() => loadWorkflows()).catch(e => alert('Move failed: ' + e));
}

function removeStep(wfName, stepName) {
  if (!confirm(`Remove step "${stepName}" from ${wfName}?`)) return;
  fetch(`${API}/api/workflows/${wfName}/remove-step`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({step_name: stepName}),
  }).then(() => loadWorkflows()).catch(e => alert('Remove failed: ' + e));
}

async function promptWorkflow(name) {
  if (!confirm(`Open AI chat for workflow "${name}"?`)) return;
  const files = [`workflows/${name}/README.md`];
  try {
    const res = await fetch(`${API}/api/workflows/${name}`);
    const data = await res.json();
    for (const step of (data.steps || [])) {
      const stepFile = step.file || `workflows/${name}/${step.name}.py`;
      const rel = stepFile.includes('/workflows/') ? 'workflows/' + stepFile.split('/workflows/')[1] : stepFile;
      files.push(rel);
    }
  } catch (e) {}
  const instructions = `Edit this workflow based on the user's request. The workflow is in workflows/${name}/. You can modify the README, edit step scripts, add new steps, or remove steps. Each step has a run(context) function that receives previous_results. Ask the user if they want you to adjust the steps so the workflow matches the functionality described in the README, or something else entirely.`;
  await openChatWithContext(files, instructions, '', {type: 'workflow', id: `wf-${name}`}, `Edit: ${name}`);
}

async function configureWorkflow(name, userPrompt) {
  const wfEl = document.querySelector(`[data-id="wf-${name}"]`);
  let statusEl = null;
  if (wfEl) {
    wfEl.open = true;
    const body = wfEl.querySelector('.wf-body');
    if (body) {
      body.innerHTML = '<div style="padding:12px 16px;"><div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;"><div class="wf-spinner"></div><span id="configure-status" style="font-size:12px;color:var(--muted);">Starting AI configuration...</span></div><pre id="configure-log" style="font-size:11px;color:var(--muted);white-space:pre-wrap;max-height:300px;overflow:auto;margin:0;"></pre></div>';
      statusEl = document.getElementById('configure-status');
    }
  }
  const logEl = document.getElementById('configure-log');
  try {
    const res = await fetch(`${API}/api/workflows/${name}/configure`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({user_prompt: userPrompt || ''}),
    });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const evt = JSON.parse(line);
          if (evt.type === 'step_start' && statusEl) statusEl.textContent = `Configuring step ${evt.step}: ${evt.description}...`;
          else if (evt.type === 'step_done' && logEl) logEl.textContent += `✓ Step ${evt.step}: ${evt.description}\n`;
          else if (evt.type === 'step_skip' && logEl) logEl.textContent += `— Step ${evt.step}: skipped (no description)\n`;
          else if (evt.type === 'step_error' && logEl) logEl.textContent += `✕ Step ${evt.step}: ${evt.error}\n`;
          else if (evt.type === 'done' && statusEl) statusEl.textContent = `Done — ${evt.configured} of ${evt.total} steps updated`;
        } catch (e) {}
      }
    }
  } catch (e) { console.error('Configure failed:', e); }
  setTimeout(() => loadWorkflows(), 1500);
}

function saveReadme(el) {
  const wfName = el.dataset.wf;
  const content = el.value;
  fetch(`${API}/api/workflows/${wfName}/save-readme`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({content}),
  }).catch(e => console.error('Save readme failed:', e));
}
