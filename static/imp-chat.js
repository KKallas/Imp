/* imp-chat.js — chat tab: markdown, messages, WebSocket, sidebar */

// --- markdown ---
marked.setOptions({ breaks: true, gfm: true });

// --- diff rendering (GitHub-style) ---
function renderDiff(text) {
  var lines = text.split('\n');
  var rows = '';
  var oldN = 0, newN = 0;
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];
    var bg, sign, lOld, lNew, content;
    if (line.startsWith('@@')) {
      var m = line.match(/@@ -(\d+)/);
      if (m) { oldN = parseInt(m[1]) - 1; }
      m = line.match(/\+(\d+)/);
      if (m) { newN = parseInt(m[1]) - 1; }
      rows += '<tr style="background:#1e3a5f;"><td colspan="3" style="padding:2px 8px;font-size:10px;color:#58a6ff;font-family:monospace;">' + line + '</td></tr>';
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
    rows += '<tr style="background:' + bg + ';">' +
      '<td style="padding:0 6px;font-size:10px;color:var(--muted);text-align:right;user-select:none;min-width:28px;font-family:monospace;">' + lOld + '</td>' +
      '<td style="padding:0 6px;font-size:10px;color:var(--muted);text-align:right;user-select:none;min-width:28px;font-family:monospace;">' + lNew + '</td>' +
      '<td style="padding:0 4px;font-family:monospace;font-size:11px;white-space:pre-wrap;">' + sign + content + '</td></tr>';
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
        if (tag) {
          const oldBlock = `<details class="tool-block running ${tag}"><summary>⏳ ${desc}...</summary><pre>Running...</pre></details>`;
          const newBlock = `<details class="tool-block ${msg.status} ${tag}"><summary>${icon} ${desc}${dur}</summary><div class="wf-step-output">${formattedOutput}</div></details>`;
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
        scrollBottom();
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

      case 'dashboard':
        openDashboard(msg.html || '');
        break;

      case 'confirm': {
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
        document.getElementById('status').innerHTML =
          `<span class="dot"></span>Waiting for approval...` +
          `<button class="wf-start" style="margin-left:auto;font-size:11px;padding:2px 10px;" onclick="respondConfirm('${confirmId}',true)">Approve</button>` +
          `<button class="wf-btn" style="color:#da3633;border-color:#da3633;margin-left:4px;font-size:11px;padding:2px 10px;" onclick="respondConfirm('${confirmId}',false)">Reject</button>`;
        document.getElementById('status').className = 'active';
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
  if (ws) ws.send(JSON.stringify({ type: 'stop' }));
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
      const branchDot = c.branch ? '<span class="branch-dot" title="' + c.branch + '">\u2387</span>' : '';
      const safeTitle = (c.title || 'New chat').replace(/'/g, "\\'").replace(/"/g, '&quot;');
      const pBtn = c.turn_count > 0 ? `<button class="chat-p-btn" onclick="promptFromChat(event, '${c.id}', '${safeTitle}')" title="Create workflow from this chat">P</button>` : '';
      el.innerHTML = `<div class="chat-title">${branchDot}${c.title || 'New chat'}</div><div class="chat-date">${dateStr}${pBtn}</div>`;
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
    let headerHtml = `<div style="text-align:center;padding:16px 0 8px;"><strong>${title}</strong><br><span style="font-size:11px;color:var(--muted);">${dateStr}</span></div>`;
    if (chat.branch) {
      headerHtml += `<div class="branch-banner"><span class="branch-label">\u2387 ${chat.branch}</span><button class="wf-start" onclick="mergeBranch()">Merge</button><button class="wf-btn" style="color:#da3633;border-color:#da3633;" onclick="discardBranch()">Discard</button></div>`;
    }
    msgs.innerHTML = headerHtml;
    (chat.turns || []).forEach(t => {
      const role = t.role === 'user' ? 'user' : 'agent';
      const content = role === 'agent' ? renderTurnFull(t) : t.content;
      addMessage(role, content);
    });
    imp.highlightAll(msgs);
    setHistoricMode(!isActive);
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
    loadChats();
  } catch (e) { console.error('newChat failed:', e); }
}

async function deleteChat() {
  if (!currentChatId) return;
  // Check if chat has a branch — offer merge/discard
  try {
    const res = await fetch(`${API}/api/chats/${currentChatId}`);
    const chat = await res.json();
    if (chat.branch) {
      const choice = prompt('This chat has branch "' + chat.branch + '".\nType "merge" to merge into main, or "discard" to delete without merging.\nLeave empty to cancel.');
      if (!choice) return;
      if (choice.toLowerCase() === 'merge') {
        await fetch(`${API}/api/chats/${currentChatId}/merge`, { method: 'POST' });
      } else if (choice.toLowerCase() === 'discard') {
        await fetch(`${API}/api/chats/${currentChatId}/discard`, { method: 'POST' });
      } else { return; }
    } else {
      if (!confirm('Delete this chat?')) return;
    }
    await fetch(`${API}/api/chats/${currentChatId}`, { method: 'DELETE' });
    currentChatId = null;
    document.getElementById('messages').innerHTML = '';
    loadChats();
  } catch (e) { console.error('deleteChat failed:', e); }
}

async function mergeBranch() {
  if (!currentChatId) return;
  if (!confirm('Merge this branch into main?')) return;
  try {
    const res = await fetch(`${API}/api/chats/${currentChatId}/merge`, { method: 'POST' });
    const data = await res.json();
    if (data.merged) {
      loadChat(currentChatId, true);
    } else {
      alert('Merge failed: ' + (data.error || 'unknown error'));
    }
  } catch (e) { alert('Merge failed: ' + e); }
}

async function discardBranch() {
  if (!currentChatId) return;
  if (!confirm('Discard this branch? All uncommitted changes will be lost.')) return;
  try {
    const res = await fetch(`${API}/api/chats/${currentChatId}/discard`, { method: 'POST' });
    const data = await res.json();
    if (data.discarded) {
      loadChat(currentChatId, true);
    } else {
      alert('Discard failed: ' + (data.error || 'unknown error'));
    }
  } catch (e) { alert('Discard failed: ' + e); }
}

function promptFromChat(ev, chatId, chatTitle) {
  // Open a new chat with instructions to create a workflow from this chat's conversation
  ev.stopPropagation();
  const presetText = 'Review the conversation in chat "' + chatTitle + '" (id: ' + chatId + ') and create a reusable workflow from it. Extract the key steps, identify which tools were used, and create appropriate workflow step files under workflows/. If any custom tools are needed, create those too under tools/.';
  openChatWithContext([], '', presetText, null, 'Workflow from: ' + chatTitle);
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
