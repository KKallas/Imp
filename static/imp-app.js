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

function switchTab(tab) {
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

// --- init ---
connectWs();
loadChats();
loadQueue();
queuePollInterval = setInterval(() => { if (activeTab === 'queue') loadQueue(); }, 5000);
