/* imp-app.js — globals, tabs, init */

const API = location.origin;
const WS_URL = `ws://${location.host}/ws/chat`;
let ws = null;
let currentChatId = null;
let isWorking = false;
let activeTab = 'queue';
let _chatSourceLock = null;

function isLockedByChat(dataId) {
  return _chatSourceLock && _chatSourceLock.id === dataId;
}

function unlockChatSource() {
  _chatSourceLock = null;
  if (activeTab === 'tools') loadToolsPanel();
  if (activeTab === 'workflows') loadWorkflows();
}

let _setupComplete = true; // assume complete, overridden by init check

function switchTab(tab) {
  if (!_setupComplete && tab !== 'chat') return;
  activeTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelector(`.tab[onclick*="${tab}"]`).classList.add('active');
  document.getElementById('queue-tab').style.display = tab === 'queue' ? '' : 'none';
  document.getElementById('chat-tab').style.display = tab === 'chat' ? '' : 'none';
  document.getElementById('workflows-tab').style.display = tab === 'workflows' ? '' : 'none';
  document.getElementById('tools-tab').style.display = tab === 'tools' ? '' : 'none';
  document.getElementById('sidebar').style.display = tab === 'chat' ? '' : 'none';
  if (tab === 'queue') loadQueue();
  if (tab === 'workflows') loadWorkflows();
  if (tab === 'tools') loadToolsPanel();
}

function lockTabsForSetup() {
  _setupComplete = false;
  document.querySelectorAll('.tab').forEach(t => {
    if (!t.onclick?.toString().includes('chat')) {
      t.disabled = true;
      t.style.opacity = '0.3';
      t.style.pointerEvents = 'none';
    }
  });
  switchTab('chat');
}

function unlockTabs() {
  _setupComplete = true;
  document.querySelectorAll('.tab').forEach(t => {
    t.disabled = false;
    t.style.opacity = '';
    t.style.pointerEvents = '';
  });
}

// --- init ---
connectWs();
loadChats();
loadQueue();
queuePollInterval = setInterval(() => { if (activeTab === 'queue') loadQueue(); }, 5000);
fetch(`${API}/api/setup-status`).then(r => r.json()).then(d => {
  if (!d.complete) lockTabsForSetup();
}).catch(() => {});
