/* imp-chat.js — chat tab: markdown, messages, WebSocket, sidebar */

// --- markdown ---
marked.setOptions({ breaks: true, gfm: true });

// --- diff rendering (GitHub-style) ---
function renderDiff(text) {
  var lines = text.split('\n');
  var rows = '';
  var oldN = 0, newN = 0;
  // Detect new-file diffs (all content lines are +, no - lines)
  var isNewFile = lines.some(function(l) { return l.startsWith('+'); }) &&
                  !lines.some(function(l) { return l.startsWith('-'); });
  var cols = isNewFile ? 2 : 3; // single line-number col for new files
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];
    var bg, sign, lOld, lNew, content;
    if (line.startsWith('@@')) {
      var m = line.match(/@@ -(\d+)/);
      if (m) { oldN = parseInt(m[1]) - 1; }
      m = line.match(/\+(\d+)/);
      if (m) { newN = parseInt(m[1]) - 1; }
      rows += '<tr style="background:#1e3a5f;"><td colspan="' + cols + '" style="padding:2px 8px;font-size:10px;color:#58a6ff;font-family:monospace;">' + (isNewFile ? 'new file' : line) + '</td></tr>';
      continue;
    }
    if (line.startsWith('-')) {
      oldN++;
      bg = 'rgba(248,81,73,0.15)';
      sign = '<span style="color:#da3633;user-select:none;">-</span>';
      lOld = oldN;
      lNew = '';
      content = line.substring(1);
    } else if (line.startsWith('+')) {
      newN++;
      bg = 'rgba(63,185,80,0.15)';
      sign = '<span style="color:#3fb950;user-select:none;">+</span>';
      lOld = '';
      lNew = newN;
      content = line.substring(1);
    } else {
      oldN++; newN++;
      bg = 'transparent';
      sign = ' ';
      lOld = oldN;
      lNew = newN;
      content = line.startsWith(' ') ? line.substring(1) : line;
    }
    if (isNewFile) {
      rows += '<tr style="background:' + bg + ';">' +
        '<td style="padding:0 6px;font-size:10px;color:var(--muted);text-align:right;user-select:none;min-width:28px;font-family:monospace;">' + lNew + '</td>' +
        '<td style="padding:0 4px;font-family:monospace;font-size:11px;white-space:pre-wrap;">' + content + '</td></tr>';
    } else {
      rows += '<tr style="background:' + bg + ';">' +
        '<td style="padding:0 6px;font-size:10px;color:var(--muted);text-align:right;user-select:none;min-width:28px;font-family:monospace;">' + lOld + '</td>' +
        '<td style="padding:0 6px;font-size:10px;color:var(--muted);text-align:right;user-select:none;min-width:28px;font-family:monospace;">' + lNew + '</td>' +
        '<td style="padding:0 4px;font-family:monospace;font-size:11px;white-space:pre-wrap;">' + sign + content + '</td></tr>';
    }
  }
  return '<table style="width:100%;border-collapse:collapse;border-spacing:0;">' + rows + '</table>';
}

function renderMd(text) {
  const toolBlocks = [];
  text = text.replace(/<details class="(?:tool-block|thinking-block|imp-fold)[^"]*">[\s\S]*?<\/details>/g, (match) => {
    toolBlocks.push(match);
    return `%%TOOL_BLOCK_${toolBlocks.length - 1}%%`;
  });
  text = text.replace(/```mermaid\n([\s\S]*?)```/g, (_, diagram) => {
    const encoded = encodeURIComponent(diagram.trim());
    const imgUrl = `${API}/render/mermaid?diagram=${encoded}`;
    const viewUrl = `${API}/render/mermaid?diagram=${encoded}&mode=viewer`;
    return `<span style="position:relative;display:inline-block;"><a href="#" onclick="event.preventDefault();loadInDashboard('${viewUrl}')" title="Open in dashboard"><img src="${imgUrl}" alt="mermaid diagram"></a><a href="${imgUrl}" download style="position:absolute;top:4px;right:4px;background:rgba(0,0,0,0.6);color:#fff;padding:2px 6px;border-radius:4px;font-size:10px;text-decoration:none;cursor:pointer;" title="Download PNG">⬇</a></span>`;
  });
  let html = marked.parse(text);
  toolBlocks.forEach((block, i) => {
    html = html.replace(`%%TOOL_BLOCK_${i}%%`, block);
  });
  html = html.replace(/<img src="([^"]*)"([^>]*)>/g, (match, src, rest) => {
    if (html.indexOf(`<a `) !== -1 && html.indexOf(match) > html.lastIndexOf(`<a `, html.indexOf(match))) {
      return match;
    }
    let linkUrl = src;
    if (src.includes('/render/')) {
      linkUrl = src.includes('mode=') ? src : src + (src.includes('?') ? '&' : '?') + 'mode=viewer';
    } else if (src.includes('/public/charts/')) {
      linkUrl = src;
    }
    if (src.includes('/render/')) {
      return `<span style="position:relative;display:inline-block;"><a href="#" onclick="event.preventDefault();loadInDashboard('${linkUrl}')" title="Open in dashboard"><img src="${src}"${rest}></a><a href="${src}" download style="position:absolute;top:4px;right:4px;background:rgba(0,0,0,0.6);color:#fff;padding:2px 6px;border-radius:4px;font-size:10px;text-decoration:none;cursor:pointer;" title="Download PNG">⬇</a></span>`;
    }
    return `<span style="position:relative;display:inline-block;"><a href="${linkUrl}" target="_blank" title="Open in new tab"><img src="${src}"${rest}></a><a href="${src}" download style="position:absolute;top:4px;right:4px;background:rgba(0,0,0,0.6);color:#fff;padding:2px 6px;border-radius:4px;font-size:10px;text-decoration:none;cursor:pointer;" title="Download PNG">⬇</a></span>`;
  });
  // Make chart links open in the dashboard drawer
  html = html.replace(/<a href="([^"]*(?:\/dashboard|\/public\/charts\/)[^"]*)">/g, (match, url) => {
    return `<a href="#" onclick="event.preventDefault();loadInDashboard('${url}')">`;
  });
  return html;
}

function formatStepOutput(text) {
  const trimmed = text.trim();
  if (!trimmed) return '<pre>(no output)</pre>';
  try {
    const obj = JSON.parse(trimmed);
    let pretty = JSON.stringify(obj, null, 2);
    if (pretty.length > 5000) pretty = pretty.substring(0, 5000) + '\n... (truncated)';
    return marked.parse('```json\n' + pretty + '\n```');
  } catch (e) {}
  const lines = trimmed.split('\n');
  if (lines.length > 1 && lines.every(l => l.includes('\t'))) {
    const rows = lines.map(l => l.split('\t'));
    let md = '| ' + rows[0].map(c => c.trim()).join(' | ') + ' |\n';
    md += '| ' + rows[0].map(() => '---').join(' | ') + ' |\n';
    rows.slice(1).forEach(cols => { md += '| ' + cols.map(c => c.trim()).join(' | ') + ' |\n'; });
    return marked.parse(md);
  }
  // If output has markdown links, render as markdown (not code block)
  if (/\[.+\]\(.+\)/.test(trimmed)) {
    return marked.parse(trimmed);
  }
  return marked.parse('```\n' + trimmed + '\n```');
}

// --- messages ---
function addMessage(role, content) {
  const el = document.createElement('div');
  el.className = `msg ${role}`;
  el.innerHTML = `<div class="role">${role}</div><div class="body">${renderMd(content)}</div>`;
  document.getElementById('messages').appendChild(el);
  imp.highlightAll(el);
  scrollBottom();
  return el;
}

function scrollBottom() {
  const m = document.getElementById('messages');
  if (!m.offsetParent) return; // chat tab hidden — skip to preserve scroll position
  m.scrollTop = m.scrollHeight;
}

// --- status ---
function setStatus(text) {
  const el = document.getElementById('status');
  if (text) {
    el.innerHTML = `<span class="dot"></span>${text}<button class="stop-inline" onclick="stop()">Stop</button>`;
    el.className = 'active';
  } else {
    el.innerHTML = '';
    el.className = '';
  }
}

function setWorking(v) {
  isWorking = v;
  document.getElementById('send-btn').style.display = v ? 'none' : '';
  document.getElementById('stop-btn').style.display = v ? '' : 'none';
  document.getElementById('input').disabled = v;
  var newBtn = document.querySelector('#sidebar-header button:nth-child(2)');
  if (newBtn) { newBtn.disabled = v; newBtn.style.opacity = v ? '0.3' : ''; }
}

// --- websocket ---
let currentAgentMsg = null;
let agentText = '';
let pendingTools = {};

function ensureAgentMsg() {
  if (!currentAgentMsg) {
    currentAgentMsg = addMessage('agent', '');
    agentText = '';
  }
  return currentAgentMsg;
}

function renderAgentBody() {
  if (!currentAgentMsg) return;
  currentAgentMsg.querySelector('.body').innerHTML = renderMd(agentText);
  scrollBottom();
}

function formatArgs(args) {
  if (!args || Object.keys(args).length === 0) return '';
  return Object.entries(args).map(([k,v]) =>
    typeof v === 'string' ? `${k}="${v}"` : `${k}=${JSON.stringify(v)}`
  ).join(', ');
}

function connectWs() {
  ws = new WebSocket(WS_URL);
  ws.onopen = () => console.log('ws connected');
  ws.onclose = () => {
    ws = null;
    if (isWorking) { setWorking(false); setStatus(''); }
    setTimeout(connectWs, 2000);
  };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    switch (msg.type) {
      case 'token':
        ensureAgentMsg();
        agentText += msg.text;
        renderAgentBody();
        break;

      case 'tool_start': {
        ensureAgentMsg();
        const toolSeq = (pendingTools._seq = (pendingTools._seq || 0) + 1);
        const tag = `tool-${toolSeq}`;
        if (!pendingTools[msg.name]) pendingTools[msg.name] = [];
        const desc = (msg.args || {}).description || msg.name;
        pendingTools[msg.name].push({args: msg.args || {}, tag, desc});
        agentText += `\n\n<details class="tool-block running ${tag}"><summary>⏳ ${desc}...</summary><pre>Running...</pre></details>\n\n`;
        renderAgentBody();
        break;
      }

      case 'tool_done': {
        ensureAgentMsg();
        const icon = msg.status === 'ok' ? '✅' : '❌';
        const dur = msg.duration ? ` · ${msg.duration.toFixed(1)}s` : '';
        const queue = pendingTools[msg.name] || [];
        const entry = queue.shift();
        if (!queue.length) delete pendingTools[msg.name];
        const desc = entry ? entry.desc : msg.name;
        const tag = entry ? entry.tag : '';
        const formattedOutput = msg.output ? formatStepOutput(msg.output) : '<pre>(no output)</pre>';
        const argsLine = entry && entry.args ? `<div style="padding:4px 10px;font-size:11px;color:var(--muted);font-family:monospace;border-bottom:1px solid var(--border);">${msg.name}(${formatArgs(entry.args)})</div>` : '';
        if (tag) {
          const oldBlock = `<details class="tool-block running ${tag}"><summary>⏳ ${desc}...</summary><pre>Running...</pre></details>`;
          const newBlock = `<details class="tool-block ${msg.status} ${tag}"><summary>${icon} ${desc}${dur}</summary>${argsLine}<div class="wf-step-output">${formattedOutput}</div></details>`;
          agentText = agentText.replace(oldBlock, newBlock);
        }
        renderAgentBody();
        break;
      }

      case 'thinking': {
        ensureAgentMsg();
        const escaped = msg.text.replace(/</g,'&lt;').replace(/>/g,'&gt;');
        agentText += `\n\n<details class="thinking-block"><summary>Thinking</summary><div class="thinking-content">${escaped}</div></details>\n\n`;
        renderAgentBody();
        break;
      }

      case 'status':
        setStatus(msg.text);
        break;

      case 'done':
        if (currentAgentMsg && msg.full_text) {
          if (!agentText.trim()) {
            agentText = msg.full_text;
          }
          renderAgentBody();
        }
        // Send the composed agentText back so it's saved as-is
        if (agentText.trim() && msg.chat_id && ws) {
          ws.send(JSON.stringify({type: 'save_rendered', chat_id: msg.chat_id, rendered: agentText}));
        }
        currentAgentMsg = null;
        agentText = '';
        pendingTools = {};
        setWorking(false);
        setStatus('');
        ensureSnapshotBanner();
        scrollBottom();
        loadChats();
        if (_chatSourceLock && _chatSourceLock.chatId === currentChatId) {
          unlockChatSource();
        }
        break;

      case 'error':
        addMessage('agent', `**Error:** ${msg.text}`);
        setWorking(false);
        setStatus('');
        break;

      case 'image':
        ensureAgentMsg();
        agentText += `\n\n![${msg.alt || 'chart'}](${msg.url})\n`;
        renderAgentBody();
        break;

      case 'setup_complete':
        unlockTabs();
        break;

      case 'need_llm_config':
        showLlmBootstrap();
        break;

      case 'dashboard':
        openDashboard(msg.html || '');
        break;

      case 'confirm': {
        if (activeTab !== 'chat') switchTab('chat');
        ensureAgentMsg();
        const confirmId = msg.id;
        const preview = (msg.preview || '').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        const diffHtml = renderDiff(preview);
        agentText += `\n\n<div class="confirm-block" data-confirm-id="${confirmId}">` +
          `<details class="tool-block" open><summary>\u23f3 ${msg.tool} \u2014 ${msg.description || ''}</summary>` +
          `<div style="max-height:300px;overflow:auto;margin:8px 0;border:1px solid var(--border);border-radius:4px;">${diffHtml}</div>` +
          `<div style="padding:4px 0 8px;"><button class="wf-start" onclick="respondConfirm('${confirmId}',true)">Approve</button> ` +
          `<button class="wf-btn" style="color:#da3633;border-color:#da3633;" onclick="respondConfirm('${confirmId}',false)">Reject</button></div>` +
          `</details></div>\n\n`;
        renderAgentBody();
        // Scroll the approval block into view
        const block = document.querySelector(`[data-confirm-id="${confirmId}"]`);
        if (block) block.scrollIntoView({ behavior: 'smooth', block: 'center' });
        setStatus(`<a href="#" style="color:var(--accent)" onclick="event.preventDefault();var b=document.querySelector('[data-confirm-id=\\'${confirmId}\\']');if(b)b.scrollIntoView({behavior:'smooth',block:'center'})">Waiting for approval (click to view)</a>`);
        break;
      }

      case 'ask_apikey': {
        if (activeTab !== 'chat') switchTab('chat');
        ensureAgentMsg();
        const envVar = msg.env_var;
        const promptHtml = renderMd(msg.prompt || `Paste your **${envVar}**:`);
        agentText += `\n\n<div class="apikey-block" data-apikey-env="${envVar}">` +
          `<div style="padding:8px 12px;">${promptHtml}</div>` +
          `<div style="padding:0 12px 10px;display:flex;gap:8px;align-items:center;">` +
          `<input type="password" id="apikey-input-${envVar}" placeholder="Paste key here" ` +
          `style="flex:1;padding:6px 10px;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:4px;font-family:monospace;font-size:13px;" ` +
          `onkeydown="if(event.key==='Enter'){event.preventDefault();respondApiKey('${envVar}');}">` +
          `<button class="wf-start" onclick="respondApiKey('${envVar}')">Save</button>` +
          `<button class="wf-btn" style="color:#da3633;border-color:#da3633;" onclick="respondApiKey('${envVar}',true)">Cancel</button>` +
          `</div></div>\n\n`;
        renderAgentBody();
        const keyBlock = document.querySelector(`[data-apikey-env="${envVar}"]`);
        if (keyBlock) keyBlock.scrollIntoView({ behavior: 'smooth', block: 'center' });
        // Focus the input after render
        setTimeout(function() {
          var inp = document.getElementById('apikey-input-' + envVar);
          if (inp) inp.focus();
        }, 100);
        setStatus('Waiting for API key');
        break;
      }
    }
  };
}

function respondConfirm(id, approved) {
  if (ws) ws.send(JSON.stringify({type: 'confirm_response', id: id, approved: approved}));
  var icon = approved ? '\u2705' : '\u274c';
  var label = approved ? 'Approved' : 'Rejected';
  // Remove buttons from agentText so re-renders don't bring them back
  var btnRe = new RegExp('<div style="padding:4px 0 8px;"><button[^>]*onclick="respondConfirm\\(\'' + id + '\'[\\s\\S]*?</div>');
  agentText = agentText.replace(btnRe, '<div style="padding:4px 0 8px;font-size:11px;color:var(--muted);">' + icon + ' ' + label + '</div>');
  // Update summary icon
  agentText = agentText.replace(
    new RegExp('(data-confirm-id="' + id + '"[\\s\\S]*?<summary>)\\u23f3'),
    '$1' + icon
  );
  renderAgentBody();
}

function respondApiKey(envVar, cancel) {
  var input = document.getElementById('apikey-input-' + envVar);
  var value = cancel ? '' : (input ? input.value : '');
  if (ws) ws.send(JSON.stringify({type: 'apikey_response', env_var: envVar, value: value}));
  // Replace input with confirmation
  var icon = cancel ? '\u274c' : '\uD83D\uDD12';
  var label = cancel ? 'Cancelled' : 'Key saved to OS keychain';
  var btnRe = new RegExp('<div style="padding:0 12px 10px;display:flex[\\s\\S]*?</div></div>');
  agentText = agentText.replace(btnRe, '<div style="padding:4px 12px 10px;font-size:11px;color:var(--muted);">' + icon + ' ' + label + '</div></div>');
  renderAgentBody();
}

function showLlmBootstrap() {
  // Show LLM backend picker in the messages area
  if (activeTab !== 'chat') switchTab('chat');
  var msgs = document.getElementById('messages');
  msgs.innerHTML = '';
  var div = document.createElement('div');
  div.className = 'msg agent';
  div.innerHTML = '<div class="llm-bootstrap">' +
    '<h3 style="margin:0 0 8px;">Configure LLM Backend</h3>' +
    '<p style="margin:0 0 12px;color:var(--muted);font-size:13px;">' +
    'No Claude access detected. Configure an alternative LLM backend to proceed with setup.</p>' +
    '<div id="llm-presets-list" style="margin-bottom:12px;"><em>Loading presets...</em></div>' +
    '<div style="border-top:1px solid var(--border);padding-top:12px;">' +
    '<label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px;">API Key</label>' +
    '<input type="password" id="llm-bootstrap-key" placeholder="Paste your API key" ' +
    'style="width:100%;padding:8px 10px;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:4px;font-family:monospace;font-size:13px;box-sizing:border-box;margin-bottom:12px;">' +
    '<button id="llm-bootstrap-btn" class="wf-start" onclick="submitLlmBootstrap()" disabled>Connect</button>' +
    '<span id="llm-bootstrap-status" style="margin-left:8px;font-size:12px;color:var(--muted);"></span>' +
    '</div></div>';
  msgs.appendChild(div);
  // Fetch presets
  fetch(API + '/api/llm-presets').then(function(r) { return r.json(); }).then(function(data) {
    var list = document.getElementById('llm-presets-list');
    if (!list) return;
    var presets = (data.presets || []).filter(function(p) { return p.base_url !== 'https://api.anthropic.com'; });
    var html = '';
    presets.forEach(function(p, i) {
      html += '<label style="display:flex;align-items:center;gap:8px;padding:8px 10px;border:1px solid var(--border);border-radius:4px;margin-bottom:6px;cursor:pointer;">' +
        '<input type="radio" name="llm-preset" value="' + i + '" onchange="selectLlmPreset(' + i + ')" style="margin:0;">' +
        '<div><strong>' + p.name + '</strong><br><span style="font-size:11px;color:var(--muted);">' + p.model + ' &middot; ' + p.notes + '</span></div>' +
        '</label>';
    });
    list.innerHTML = html;
    window._llmPresets = presets;
  }).catch(function() {
    var list = document.getElementById('llm-presets-list');
    if (list) list.innerHTML = '<em style="color:#da3633;">Failed to load presets</em>';
  });
}

function selectLlmPreset(index) {
  window._selectedLlmPreset = window._llmPresets[index];
  var btn = document.getElementById('llm-bootstrap-btn');
  if (btn) btn.disabled = false;
}

function submitLlmBootstrap() {
  var preset = window._selectedLlmPreset;
  if (!preset) return;
  var key = document.getElementById('llm-bootstrap-key').value.trim();
  var status = document.getElementById('llm-bootstrap-status');
  var btn = document.getElementById('llm-bootstrap-btn');
  if (!key) {
    if (status) status.textContent = 'API key is required';
    return;
  }
  if (btn) btn.disabled = true;
  if (status) status.textContent = 'Saving...';
  fetch(API + '/api/llm-bootstrap', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      model: preset.model,
      base_url: preset.base_url,
      api_key_env: preset.api_key_env,
      api_key: key
    })
  }).then(function(r) { return r.json(); }).then(function(data) {
    if (data.ok) {
      if (status) status.textContent = 'Connected! Starting setup...';
      // Tell the WebSocket handler we're ready
      if (ws) ws.send(JSON.stringify({type: 'llm_configured'}));
      // Clear the bootstrap UI
      setTimeout(function() {
        var msgs = document.getElementById('messages');
        if (msgs) msgs.innerHTML = '';
      }, 500);
    } else {
      if (status) status.textContent = data.error || 'Failed';
      if (btn) btn.disabled = false;
    }
  }).catch(function(err) {
    if (status) status.textContent = 'Error: ' + err.message;
    if (btn) btn.disabled = false;
  });
}

function send() {
  const input = document.getElementById('input');
  const text = input.value.trim();
  if (!text || !ws || isWorking) return;
  addMessage('user', text);
  ws.send(JSON.stringify({ type: 'message', text, chat_id: currentChatId }));
  input.value = '';
  input.style.height = 'auto';
  setWorking(true);
  setStatus('Thinking...');
}

function stop() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'stop' }));
  }
  // Always clear local UI — server-side cancel is best-effort.
  setWorking(false);
  setStatus('');
  pendingTools = {};
}

function onKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  const el = e.target;
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

// --- chat sidebar ---
async function loadChats() {
  try {
    const res = await fetch(`${API}/api/chats`);
    const chats = await res.json();
    const list = document.getElementById('chat-list');
    list.innerHTML = '';
    chats.forEach((c, i) => {
      const el = document.createElement('div');
      el.className = `chat-item${c.id === currentChatId ? ' active' : ''}`;
      const dateStr = c.created_at ? new Date(c.created_at).toLocaleString(undefined, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}) : '';
      const safeTitle = (c.title || 'New chat').replace(/'/g, "\\'").replace(/"/g, '&quot;');
      const pBtn = c.turn_count > 0 ? `<button class="chat-p-btn" onclick="promptFromChat(event, '${c.id}', '${safeTitle}')" title="Create tool or workflow from this chat">P</button>` : '';
      el.innerHTML = `<div class="chat-title">${c.title || 'New chat'}</div><div class="chat-date">${dateStr}${pBtn}</div>`;
      el.onclick = () => loadChat(c.id, i === 0);
      list.appendChild(el);
    });
  } catch (e) { console.error('loadChats failed:', e); }
}

function formatToolOutput(raw) {
  // Escape HTML but preserve markdown links as clickable <a> tags
  var escaped = (raw || '').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  escaped = escaped.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function(_, text, url) {
    if (url.includes('/dashboard') || url.includes('/public/charts/')) {
      return '<a href="#" onclick="event.preventDefault();loadInDashboard(\'' + url + '\')">' + text + '</a>';
    }
    return '<a href="' + url + '" target="_blank">' + text + '</a>';
  });
  return escaped;
}

function renderTurnFull(turn) {
  // If content has tool-block HTML, it was saved from the live stream — use as-is
  if (turn.content && turn.content.includes('tool-block')) {
    return turn.content;
  }
  // Fallback: rebuild from structured blocks (old format)
  let parts = [];
  if (turn.blocks && turn.blocks.length) {
    turn.blocks.forEach(b => {
      if (b.type === 'thinking') {
        const escaped = b.text.replace(/</g,'&lt;').replace(/>/g,'&gt;');
        parts.push(`<details class="thinking-block"><summary>Thinking</summary><div class="thinking-content">${escaped}</div></details>`);
      } else if (b.type === 'tool') {
        const icon = b.status === 'ok' ? '✅' : (b.status === 'error' ? '❌' : '⏳');
        const dur = b.duration_s ? ` · ${b.duration_s.toFixed(1)}s` : '';
        parts.push(`<details class="tool-block ${b.status || ''}"><summary>${icon} ${b.name}(${formatArgs(b.args || {})})${dur}</summary><pre>${formatToolOutput(b.output)}</pre></details>`);
      }
    });
  } else {
    if (turn.thinking && turn.thinking.length) {
      turn.thinking.forEach(t => {
        const escaped = t.replace(/</g,'&lt;').replace(/>/g,'&gt;');
        parts.push(`<details class="thinking-block"><summary>Thinking</summary><div class="thinking-content">${escaped}</div></details>`);
      });
    }
    if (turn.tool_calls && turn.tool_calls.length) {
      turn.tool_calls.forEach(tc => {
        const icon = tc.status === 'ok' ? '✅' : (tc.status === 'error' ? '❌' : '⏳');
        const dur = tc.duration_s ? ` · ${tc.duration_s.toFixed(1)}s` : '';
        parts.push(`<details class="tool-block ${tc.status || ''}"><summary>${icon} ${tc.name}(${formatArgs(tc.args || {})})${dur}</summary><pre>${formatToolOutput(tc.output)}</pre></details>`);
      });
    }
  }
  if (turn.content) parts.push(turn.content);
  return parts.join('\n\n');
}

let isHistoricView = false;

function setHistoricMode(on) {
  isHistoricView = on;
  document.getElementById('input-area').style.display = on ? 'none' : '';
  document.getElementById('status').style.display = on ? 'none' : '';
}

async function loadChat(id, isActive) {
  try {
    const res = await fetch(`${API}/api/chats/${id}`);
    const chat = await res.json();
    currentChatId = id;
    const msgs = document.getElementById('messages');
    msgs.innerHTML = '';
    const dateStr = chat.created_at ? new Date(chat.created_at).toLocaleString() : '';
    const title = chat.title || 'Chat';
    msgs.innerHTML = `<div style="text-align:center;padding:16px 0 8px;"><strong>${title}</strong><br><span style="font-size:11px;color:var(--muted);">${dateStr}</span></div>`;

    // Build timeline: interleave turns and snapshots by timestamp
    const items = [];
    (chat.turns || []).forEach(t => {
      items.push({type: 'turn', data: t, ts: t.timestamp || ''});
    });
    (chat.snapshots || []).forEach((s, i) => {
      items.push({type: 'snapshot', data: s, index: i, ts: s.timestamp || ''});
    });
    items.sort((a, b) => (a.ts || '').localeCompare(b.ts || ''));

    items.forEach(item => {
      if (item.type === 'turn') {
        const t = item.data;
        const role = t.role === 'user' ? 'user' : 'agent';
        const content = role === 'agent' ? renderTurnFull(t) : t.content;
        addMessage(role, content);
      } else {
        const el = document.createElement('div');
        el.innerHTML = renderSnapshotBlock(item.data, item.index, id);
        msgs.appendChild(el.firstChild);
      }
    });

    imp.highlightAll(msgs);
    setHistoricMode(!isActive);
    ensureSnapshotBanner();
    loadChats();
  } catch (e) { console.error('loadChat failed:', e); }
}

async function newChat() {
  if (isWorking) return;
  try {
    const res = await fetch(`${API}/api/chats`, { method: 'POST' });
    const chat = await res.json();
    currentChatId = chat.id;
    const msgs = document.getElementById('messages');
    const dateStr = new Date().toLocaleString();
    msgs.innerHTML = `<div style="text-align:center;padding:16px 0 8px;"><strong>New chat</strong><br><span style="font-size:11px;color:var(--muted);">${dateStr}</span></div>`;
    setHistoricMode(false);
    ensureSnapshotBanner();
    loadChats();
  } catch (e) { console.error('newChat failed:', e); }
}

async function deleteChat() {
  if (!currentChatId) return;
  try {
    const res = await fetch(`${API}/api/chats/${currentChatId}`);
    const chat = await res.json();
    const hasTurns = (chat.turns || []).length > 0;
    const snapCount = (chat.snapshots || []).length;
    if (hasTurns) {
      let snapWarning = '';
      if (snapCount > 0) {
        snapWarning = '\n\nThis chat has ' + snapCount + ' snapshot' + (snapCount > 1 ? 's' : '') + ' that haven\'t been shared as PRs. Deleting will remove them.';
      }
      const choice = prompt('Delete chat "' + (chat.title || 'Untitled') + '"?' + snapWarning + '\n\nType "issue" to create a GitHub issue from this chat first.\nType "delete" to delete without saving.\nLeave empty to cancel.');
      if (!choice) return;
      if (choice.toLowerCase() === 'issue') {
        const r = await fetch(`${API}/api/chats/${currentChatId}/create-issue`, { method: 'POST' });
        const data = await r.json();
        if (data.url) alert('Issue created: ' + data.url);
        else if (data.error) { alert('Issue creation failed: ' + data.error); return; }
      } else if (choice.toLowerCase() !== 'delete') { return; }
    } else {
      if (!confirm('Delete this chat?')) return;
    }
    await fetch(`${API}/api/chats/${currentChatId}`, { method: 'DELETE' });
    currentChatId = null;
    document.getElementById('messages').innerHTML = '';
    loadChats();
  } catch (e) { console.error('deleteChat failed:', e); }
}

function promptFromChat(ev, chatId, chatTitle) {
  ev.stopPropagation();
  const chatFile = '.imp/chats/' + chatId + '/chat.json';
  const presetText = 'Review the attached chat and create a reusable tool or workflow from it. Use make_tool.py or make_workflow.py to finalize.';
  openChatWithContext([chatFile], '', presetText, null, 'Productize: ' + chatTitle);
}

async function openChatWithContext(files, instructions, userPrompt, sourceLock, title) {
  try {
    const res = await fetch(`${API}/api/chat/new-with-context`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({files, instructions, user_prompt: userPrompt, title: title || ''}),
    });
    const data = await res.json();
    if (data.id) {
      if (sourceLock) {
        _chatSourceLock = {...sourceLock, chatId: data.id};
      }
      currentChatId = data.id;
      switchTab('chat');
      await loadChat(data.id, true);
      const input = document.getElementById('input');
      if (input) {
        input.value = '';
        input.focus();
      }
    }
  } catch (e) { alert('Failed to open chat: ' + e); }
}

// --- snapshots ---

function renderSnapshotBlock(snap, index, chatId) {
  const ts = snap.timestamp ? new Date(snap.timestamp).toLocaleString(undefined, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}) : '';
  const fileCount = (snap.changed_files || []).length;
  const fileTip = fileCount ? fileCount + ' file' + (fileCount > 1 ? 's' : '') : '';
  return '<div class="snapshot-block" data-snap-index="' + index + '">' +
    '<span class="snap-icon">\uD83D\uDCBE</span>' +
    '<div class="snap-info"><div class="snap-name">' + (snap.name || 'Snapshot') + '</div>' +
    '<div class="snap-meta">' + ts + (fileTip ? ' \u00B7 ' + fileTip : '') + '</div></div>' +
    '<div class="snap-actions">' +
    '<button onclick="restoreSnapshot(\'' + chatId + '\',' + index + ')">Restore</button>' +
    '<button onclick="snapshotPR(\'' + chatId + '\',' + index + ')">PR</button>' +
    '</div></div>';
}

function ensureSnapshotBanner() {
  // Place a clickable banner at the bottom of the messages area
  const msgs = document.getElementById('messages');
  if (!msgs) return;
  // Remove existing banner (we re-append it at the end)
  var old = msgs.querySelector('.snapshot-banner');
  if (old) old.remove();
  // Only show in active (non-historic) chats
  if (isHistoricView || !currentChatId) return;
  var banner = document.createElement('div');
  banner.className = 'snapshot-banner';
  banner.textContent = '+ Snapshot';
  banner.onclick = createSnapshot;
  msgs.appendChild(banner);
}

async function createSnapshot() {
  if (!currentChatId) return;
  const name = prompt('Name this snapshot:');
  if (!name) return;
  const banner = document.querySelector('.snapshot-banner');
  if (banner) { banner.classList.add('saving'); banner.textContent = 'Saving...'; }
  try {
    const res = await fetch(`${API}/api/chats/${currentChatId}/snapshots`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name}),
    });
    const data = await res.json();
    if (data.error === 'no_changes') {
      alert('No changes to snapshot.');
    } else if (data.error) {
      alert('Snapshot failed: ' + (data.message || data.error));
    } else if (data.ok) {
      // Insert snapshot block above the banner
      const msgs = document.getElementById('messages');
      const el = document.createElement('div');
      el.innerHTML = renderSnapshotBlock(data.snapshot, data.index, currentChatId);
      const bannerEl = msgs.querySelector('.snapshot-banner');
      if (bannerEl) {
        msgs.insertBefore(el.firstChild, bannerEl);
      } else {
        msgs.appendChild(el.firstChild);
      }
      scrollBottom();
      loadChats();
    }
  } catch (e) {
    alert('Snapshot failed: ' + e);
  } finally {
    ensureSnapshotBanner();
  }
}

async function restoreSnapshot(chatId, index) {
  // First try normal restore (will warn if dirty)
  try {
    const res = await fetch(`${API}/api/chats/${chatId}/snapshots/${index}/restore`, {method: 'POST'});
    const data = await res.json();
    if (data.warning === 'dirty') {
      const choice = prompt(
        'You have unsaved changes that will be lost.\n\n' +
        'Type "snapshot" to save current state first, then restore.\n' +
        'Type "restore" to discard changes and restore.\n' +
        'Leave empty to cancel.'
      );
      if (!choice) return;
      if (choice.toLowerCase() === 'snapshot') {
        const snapName = prompt('Name for current state snapshot:');
        if (!snapName) return;
        const snapRes = await fetch(`${API}/api/chats/${chatId}/snapshots`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: snapName}),
        });
        const snapData = await snapRes.json();
        if (snapData.error) { alert('Save failed: ' + (snapData.message || snapData.error)); return; }
        // Now add the snapshot block inline
        if (snapData.ok) {
          const msgs = document.getElementById('messages');
          const el = document.createElement('div');
          el.innerHTML = renderSnapshotBlock(snapData.snapshot, snapData.index, chatId);
          const bannerEl = msgs.querySelector('.snapshot-banner');
          if (bannerEl) {
            msgs.insertBefore(el.firstChild, bannerEl);
          } else {
            msgs.appendChild(el.firstChild);
          }
        }
      }
      // Force restore
      const forceRes = await fetch(`${API}/api/chats/${chatId}/snapshots/${index}/restore-force`, {method: 'POST'});
      const forceData = await forceRes.json();
      if (forceData.ok) {
        addMessage('agent', '\uD83D\uDCBE Restored to **' + forceData.restored_to + '**');
        ensureSnapshotBanner();
        scrollBottom();
      } else {
        alert('Restore failed: ' + (forceData.message || forceData.error));
      }
    } else if (data.ok) {
      addMessage('agent', '\uD83D\uDCBE Restored to **' + data.restored_to + '**');
      ensureSnapshotBanner();
      scrollBottom();
    } else if (data.error) {
      alert('Restore failed: ' + (data.message || data.error));
    }
  } catch (e) {
    alert('Restore failed: ' + e);
  }
}

async function snapshotPR(chatId, index) {
  if (!confirm('This will conclude this chat and open a new one to create the pull request.\n\nContinue?')) return;

  try {
    const snapRes = await fetch(`${API}/api/chats/${chatId}/snapshots`);
    const snapData = await snapRes.json();
    const snapshot = (snapData.snapshots || [])[index];
    if (!snapshot) { alert('Snapshot not found'); return; }

    const chatFile = '.imp/chats/' + chatId + '/chat.json';
    const changedFiles = (snapshot.changed_files || []).join(', ');
    const impDirs = ['tools/', 'workflows/', 'renderers/', 'server/', 'static/'];
    const isImpChange = snapshot.changed_files && snapshot.changed_files.length > 0 &&
      snapshot.changed_files.every(function(f) { return impDirs.some(function(d) { return f.startsWith(d); }); });
    const targetHint = isImpChange
      ? 'The changed files (' + changedFiles + ') are all Imp infrastructure files. Ask the user: should this PR go to the Imp repo or the project repo?'
      : 'Changed files: ' + changedFiles;

    const instructions = 'Create a pull request from snapshot "' + snapshot.name + '" (commit ' + snapshot.commit_hash.substring(0, 8) + '). ' +
      'The branch is already created. ' + targetHint + '\n\n' +
      'Propose a PR title and description based on the chat context and changed files. ' +
      'Ask the user to confirm before creating the PR.';

    openChatWithContext([chatFile], '', instructions, null, 'PR: ' + snapshot.name);
  } catch (e) { alert('Failed: ' + e); }
}
