/* imp-chat.js — chat tab: markdown, messages, WebSocket, sidebar */

// --- markdown ---
marked.setOptions({ breaks: true, gfm: true });

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
    return `<a href="${viewUrl}" target="_blank"><img src="${imgUrl}" alt="mermaid diagram"></a>`;
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
    return `<a href="${linkUrl}" target="_blank" title="Open in new tab"><img src="${src}"${rest}></a>`;
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
  ws.onclose = () => { ws = null; setTimeout(connectWs, 2000); };
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
          if (!agentText.includes(msg.full_text) && msg.full_text) {
            agentText += msg.full_text;
          }
          renderAgentBody();
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
    }
  };
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
      el.innerHTML = `<div class="chat-title">${c.title || 'New chat'}</div><div class="chat-date">${dateStr}</div>`;
      el.onclick = () => loadChat(c.id, i === 0);
      list.appendChild(el);
    });
  } catch (e) { console.error('loadChats failed:', e); }
}

function renderTurnFull(turn) {
  let parts = [];
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
      const args = tc.args || {};
      const output = (tc.output || '').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      parts.push(`<details class="tool-block ${tc.status || ''}"><summary>${icon} ${tc.name}(${formatArgs(args)})${dur}</summary><pre>${output || '(no output)'}</pre></details>`);
    });
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
  try {
    const res = await fetch(`${API}/api/chats`, { method: 'POST' });
    const chat = await res.json();
    currentChatId = chat.id;
    document.getElementById('messages').innerHTML = '';
    setHistoricMode(false);
    loadChats();
  } catch (e) { console.error('newChat failed:', e); }
}

async function deleteChat() {
  if (!currentChatId) return;
  if (!confirm('Delete this chat?')) return;
  try {
    await fetch(`${API}/api/chats/${currentChatId}`, { method: 'DELETE' });
    currentChatId = null;
    document.getElementById('messages').innerHTML = '';
    loadChats();
  } catch (e) { console.error('deleteChat failed:', e); }
}

async function openChatWithContext(files, instructions, userPrompt, sourceLock) {
  try {
    const res = await fetch(`${API}/api/chat/new-with-context`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({files, instructions, user_prompt: userPrompt}),
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
        input.value = data.prompt || instructions || '';
        input.focus();
      }
    }
  } catch (e) { alert('Failed to open chat: ' + e); }
}
