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
  // sidebar is inside chat-tab now, no display toggle needed
  if (tab === 'queue') loadQueue();
  if (tab === 'workflows') loadWorkflows();
  if (tab === 'tools') loadToolsPanel();
}

function toggleSidebar() {
  var sb = document.getElementById('sidebar');
  sb.classList.toggle('closed');
}

function toggleDashboard() {
  var db = document.getElementById('dashboard-drawer');
  db.classList.toggle('closed');
  var btn = document.getElementById('dashboard-toggle');
  btn.innerHTML = db.classList.contains('closed') ? '&#x25C0;' : '&#x25B6;';
}

function openDashboard(html) {
  var db = document.getElementById('dashboard-drawer');
  var content = document.getElementById('dashboard-content');
  content.innerHTML = html;
  db.classList.remove('closed');
  document.getElementById('dashboard-toggle').innerHTML = '&#x25B6;';
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

// --- widget events ---
window.addEventListener('message', function(e) {
  if (e.data && e.data.type === 'widget_click') {
    console.log('Widget click:', e.data);
    // Forward to WebSocket so the agent can react
    if (ws) ws.send(JSON.stringify({type: 'widget_event', event: e.data}));
  }
});

// --- init ---
connectWs();
loadChats();
loadQueue();
queuePollInterval = setInterval(() => { if (activeTab === 'queue') loadQueue(); }, 5000);
fetch(`${API}/api/setup-status`).then(r => r.json()).then(d => {
  if (!d.complete) lockTabsForSetup();
}).catch(() => {});
