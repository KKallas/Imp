/* imp-tools.js — tools tab */

let _toolsCache = [];
let _editingToolGroup = null;
let _editingTool = null;
async function loadToolsPanel() {
  const el = document.getElementById('tools-list-panel');
  const openState = imp.getOpenState(el);
  try {
    const res = await fetch(`${API}/api/tools`);
    _toolsCache = await res.json();

    const groups = {};
    for (const t of _toolsCache) {
      if (!groups[t.group]) groups[t.group] = [];
      groups[t.group].push(t);
    }

    let html = '';
    for (const [group, tools] of Object.entries(groups).sort()) {
      let readmeHtml = '', rawReadme = '';
      try {
        const rres = await fetch(`${API}/api/tool-group-readme?group=${group}`);
        const rdata = await rres.json();
        rawReadme = rdata.readme || '';
        if (rawReadme) readmeHtml = marked.parse(rawReadme);
      } catch (e) {}

      const isEditing = _editingToolGroup === group;

      const groupBtns = isEditing ? [
        imp.btn('OK', `saveToolGroupReadme('${group}')`, {cls:'ok'}),
        imp.btn('Cancel', `cancelToolGroupEdit()`)
      ] : [
        imp.btn('Edit', `editToolGroup('${group}')`),
        imp.btn('P', `openPromptGroup('${group}')`, {cls:'small', title:'AI Chat'}),
        imp.btn('Copy', `copyToolGroup('${group}')`),
        imp.btn('Rename', `renameToolGroup('${group}')`),
        imp.btn('Delete', `deleteToolGroup('${group}')`, {cls:'danger'})
      ];

      let bodyHtml = '';
      if (isEditing) bodyHtml += imp.readmeEdit('tg-readme-edit', rawReadme);
      else bodyHtml += imp.readme(readmeHtml);

      const toolsHtml = tools.map(t => {
        const isToolEditing = _editingTool && _editingTool.group === group && _editingTool.name === t.name;
        const toolBtns = isToolEditing ? [
          imp.btn('OK', `saveToolScript('${group}','${t.name}')`, {cls:'ok'}),
          imp.btn('Cancel', `cancelToolScriptEdit()`)
        ] : [
          imp.btn('Edit', `editToolScript('${group}','${t.name}')`),
          imp.btn('P', `openPromptTool('${group}','${t.name}')`, {cls:'small', title:'AI Chat'}),
          imp.btn('Copy', `copyToolScript('${group}','${t.name}')`),
          imp.btn('Delete', `deleteToolScript('${group}','${t.name}')`, {cls:'danger'})
        ];
        let toolBody = '';
        if (isToolEditing) {
          toolBody = `<div style="margin-bottom:8px;display:flex;align-items:center;gap:8px;">
            <label style="font-size:12px;color:var(--muted);">Group:</label>
            <select id="tool-move-group" style="padding:3px 8px;background:var(--input-bg);color:var(--fg);border:1px solid var(--border);border-radius:4px;font-size:12px;">
              ${Object.keys(groups).sort().map(g => `<option value="${g}" ${g === group ? 'selected' : ''}>${g}</option>`).join('')}
            </select></div>
            <textarea class="wf-readme-edit" id="tool-desc-edit" style="min-height:80px;">${(t.description || '').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</textarea>`;
        }
        return imp.item({id: `tool-${group}-${t.name}`, name: t.name, buttons: toolBtns, body: toolBody});
      }).join('');

      bodyHtml += imp.items(toolsHtml);
      html += imp.card({id: `tg-${group}`, name: group, meta: `${tools.length} tools`, buttons: groupBtns, body: bodyHtml});
    }
    el.innerHTML = html;

    imp.restoreOpenState(el, openState);
    if (_editingToolGroup) {
      const tgEl = el.querySelector(`[data-id="tg-${_editingToolGroup}"]`);
      if (tgEl) tgEl.open = true;
    }
    if (_editingTool) {
      const tgEl = el.querySelector(`[data-id="tg-${_editingTool.group}"]`);
      if (tgEl) tgEl.open = true;
      const tEl = el.querySelector(`[data-id="tool-${_editingTool.group}-${_editingTool.name}"]`);
      if (tEl) tEl.open = true;
    }

    // Lazy-load tool detail on toggle
    el.querySelectorAll('.wf-step-item').forEach(det => {
      det.addEventListener('toggle', async function() {
        if (!this.open) return;
        const id = this.dataset.id;
        const parts = id.split('-');
        const tGroup = parts[1];
        const tName = parts.slice(2).join('-');
        const container = this.querySelector('.wf-step-output');
        if (container.dataset.loaded) return;
        container.dataset.loaded = '1';
        container.innerHTML = '<div class="wf-spinner" style="width:14px;height:14px;"></div>';
        try {
          const res = await fetch(`${API}/api/tool-detail?group=${tGroup}&name=${tName}`);
          const data = await res.json();
          let inner = '';
          if (data.docstring) inner += `<div style="font-size:12px;color:var(--muted);margin-bottom:8px;">${marked.parse(data.docstring)}</div>`;
          if (data.source) inner += imp.fold('Script', imp.code(data.source));
          if (data.step_template) inner += imp.fold('Workflow template', imp.code(data.step_template));
          if (data.step_template) inner += imp.fold('Testing', buildTestingPanel(tGroup, tName));
          container.innerHTML = inner || '<em style="color:var(--muted);">No source</em>';
        } catch (e) {
          container.innerHTML = `<em style="color:#da3633;">Failed to load</em>`;
        }
      });
    });

    if (_chatSourceLock && isWorking) {
      imp.applyLock(el, _chatSourceLock.id, 'AI is editing in Chat tab...');
    }
  } catch (e) { console.error('loadToolsPanel failed:', e); el.innerHTML = '<div style="padding:20px;color:#da3633;">Failed to load tools</div>'; }
}

function openPromptTool(group, name) {
  if (!confirm(`Open AI chat for ${group}/${name}?`)) return;
  const files = [`tools/${group}/${name}.py`, `tools/${group}/${name}.step.py`];
  const instructions = `Edit this tool script and/or its workflow step template based on the user's request. The tool is tools/${group}/${name}.py. After making changes, the files will be updated on disk.`;
  openChatWithContext(files, instructions, '', {type: 'tool', id: `tool-${group}-${name}`}, `Edit: ${group}/${name}`);
}

function openPromptGroup(group) {
  if (!confirm(`Open AI chat for "${group}" group?`)) return;
  const groupTools = _toolsCache.filter(t => t.group === group);
  const files = [`tools/${group}/README.md`];
  for (const t of groupTools) files.push(`tools/${group}/${t.name}.py`);
  const instructions = `Edit tools in the "${group}" group based on the user's request. You can modify the README, edit existing tool scripts, or create new ones. All files are under tools/${group}/.`;
  openChatWithContext(files, instructions, '', {type: 'group', id: `tg-${group}`}, `Edit: ${group}`);
}

function editToolGroup(group) { _editingToolGroup = group; loadToolsPanel(); }
function cancelToolGroupEdit() { _editingToolGroup = null; loadToolsPanel(); }

function saveToolGroupReadme(group) {
  const textarea = document.getElementById('tg-readme-edit');
  if (!textarea) return;
  fetch(`${API}/api/tool-group-readme-save`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({group, content: textarea.value}),
  }).then(r => r.json()).then(d => {
    if (d.error) alert('Save failed: ' + d.error);
    _editingToolGroup = null; loadToolsPanel();
  }).catch(e => alert('Save failed: ' + e));
}

function copyToolGroup(group) {
  const newName = prompt(`Copy "${group}" as:`, group + '_copy');
  if (!newName || !newName.trim()) return;
  fetch(`${API}/api/tool-group-copy`, { method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({group, new_name: newName.trim()}) })
    .then(r => r.json()).then(d => { if (d.error) alert('Copy failed: ' + d.error); loadToolsPanel(); })
    .catch(e => alert('Copy failed: ' + e));
}

function renameToolGroup(group) {
  const newName = prompt(`Rename "${group}" to:`, group);
  if (!newName || !newName.trim() || newName.trim() === group) return;
  fetch(`${API}/api/tool-group-rename`, { method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({group, new_name: newName.trim()}) })
    .then(r => r.json()).then(d => { if (d.error) alert('Rename failed: ' + d.error); loadToolsPanel(); })
    .catch(e => alert('Rename failed: ' + e));
}

function deleteToolGroup(group) {
  if (!confirm(`Delete entire "${group}" group and ALL its tools?`)) return;
  fetch(`${API}/api/tool-group-delete`, { method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({group}) })
    .then(r => r.json()).then(d => { if (d.error) alert('Delete failed: ' + d.error); loadToolsPanel(); })
    .catch(e => alert('Delete failed: ' + e));
}

function editToolScript(group, name) { _editingTool = {group, name}; loadToolsPanel(); }
function cancelToolScriptEdit() { _editingTool = null; loadToolsPanel(); }

async function saveToolScript(group, name) {
  const textarea = document.getElementById('tool-desc-edit');
  if (!textarea) return;
  const newDesc = textarea.value.trim();
  const newGroup = document.getElementById('tool-move-group')?.value || group;
  try {
    if (newGroup !== group) {
      const mres = await fetch(`${API}/api/tool-move`, { method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({group, name, new_group: newGroup}) });
      const mdata = await mres.json();
      if (mdata.error) { alert('Move failed: ' + mdata.error); _editingTool = null; loadToolsPanel(); return; }
    }
    const res = await fetch(`${API}/api/tool-describe-save`, { method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({group: newGroup, name, docstring: newDesc}) });
    const data = await res.json();
    if (data.error) alert('Save failed: ' + data.error);
  } catch (e) { alert('Save failed: ' + e); }
  _editingTool = null; loadToolsPanel();
}

function copyToolScript(group, name) {
  const newName = prompt(`Copy "${name}" as:`, name + '_copy');
  if (!newName || !newName.trim()) return;
  fetch(`${API}/api/tool-copy`, { method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({group, name, new_name: newName.trim()}) })
    .then(r => r.json()).then(d => { if (d.error) alert('Copy failed: ' + d.error); loadToolsPanel(); })
    .catch(e => alert('Copy failed: ' + e));
}

function deleteToolScript(group, name) {
  if (!confirm(`Delete tool "${group}/${name}"?`)) return;
  fetch(`${API}/api/tool-delete`, { method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({group, name}) })
    .then(r => r.json()).then(d => { if (d.error) alert('Delete failed: ' + d.error); loadToolsPanel(); })
    .catch(e => alert('Delete failed: ' + e));
}

function buildTestingPanel(group, name) {
  var defaultCtx = '{\n  "previous_results": {},\n  "workflow": "test",\n  "step": "' + name + '"\n}';
  var defaultParams = '{}';
  var runBtn = imp.btn('Run', "runToolDebug('" + group + "','" + name + "')", {cls:'primary'});
  return '<div style="display:flex;gap:8px;margin-bottom:8px;">'
    + '<div style="flex:1;"><label style="font-size:11px;color:var(--muted);">Context In</label>'
    + '<textarea class="wf-readme-edit tool-ctx-in" data-group="' + group + '" data-name="' + name + '" style="min-height:80px;font-family:monospace;font-size:11px;">' + defaultCtx + '</textarea></div>'
    + '<div style="flex:1;"><label style="font-size:11px;color:var(--muted);">Parameters</label>'
    + '<textarea class="wf-readme-edit tool-params" data-group="' + group + '" data-name="' + name + '" style="min-height:80px;font-family:monospace;font-size:11px;">' + defaultParams + '</textarea></div>'
    + '<div style="flex:1;"><label style="font-size:11px;color:var(--muted);">Context Out</label>'
    + '<textarea class="wf-readme-edit tool-ctx-out" data-group="' + group + '" data-name="' + name + '" style="min-height:80px;font-family:monospace;font-size:11px;"></textarea></div>'
    + '</div>'
    + '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">'
    + runBtn
    + '<span class="tool-debug-status" data-group="' + group + '" data-name="' + name + '" style="font-size:11px;color:var(--muted);"></span>'
    + '</div>'
    + '<div class="tool-debug-log" data-group="' + group + '" data-name="' + name + '" style="display:none;">'
    + '<label style="font-size:11px;color:var(--muted);">Log</label>'
    + '<pre class="imp-code" style="max-height:200px;overflow:auto;font-size:11px;"></pre>'
    + '</div>';
}

async function runToolDebug(group, name) {
  var ctxIn = document.querySelector('.tool-ctx-in[data-group="' + group + '"][data-name="' + name + '"]');
  var paramsEl = document.querySelector('.tool-params[data-group="' + group + '"][data-name="' + name + '"]');
  var ctxOut = document.querySelector('.tool-ctx-out[data-group="' + group + '"][data-name="' + name + '"]');
  var status = document.querySelector('.tool-debug-status[data-group="' + group + '"][data-name="' + name + '"]');
  var logDiv = document.querySelector('.tool-debug-log[data-group="' + group + '"][data-name="' + name + '"]');
  if (!ctxIn) return;

  var context, params;
  try {
    context = JSON.parse(ctxIn.value);
  } catch (e) {
    status.textContent = 'Invalid JSON in Context In';
    status.style.color = '#da3633';
    return;
  }
  try {
    params = JSON.parse(paramsEl.value || '{}');
  } catch (e) {
    status.textContent = 'Invalid JSON in Parameters';
    status.style.color = '#da3633';
    return;
  }

  status.textContent = 'Running...';
  status.style.color = 'var(--muted)';

  try {
    var res = await fetch(API + '/api/tool-debug', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({group: group, name: name, context: context, params: params}),
    });
    var data = await res.json();

    var icon = data.ok ? '\u2705' : '\u274c';
    var dur = data.duration_s ? ' \u00b7 ' + data.duration_s + 's' : '';
    status.textContent = icon + ' ' + (data.ok ? 'OK' : 'Error') + dur;
    status.style.color = data.ok ? '#3fb950' : '#da3633';

    if (data.context_out) {
      ctxOut.value = JSON.stringify(data.context_out, null, 2);
    } else if (data.error) {
      ctxOut.value = data.error;
    }

    var logText = '';
    if (data.result && data.result.output) logText += data.result.output + '\n';
    if (data.stderr) logText += data.stderr;
    if (data.error) logText += data.error;
    if (logText.trim()) {
      logDiv.style.display = '';
      logDiv.querySelector('pre').textContent = logText.trim();
    }
  } catch (e) {
    status.textContent = 'Request failed: ' + e;
    status.style.color = '#da3633';
  }
}
