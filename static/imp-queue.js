/* imp-queue.js — queue tab + popup */

let queueItems = [];
let queuePollInterval = null;

async function loadQueue() {
  try {
    const res = await fetch(`${API}/api/queue`);
    queueItems = await res.json();
    renderQueue();
  } catch (e) { console.error('loadQueue failed:', e); }
}

function renderQueue() {
  const el = document.getElementById('queue-list');
  if (!queueItems.length) {
    el.innerHTML = '<div class="queue-empty">No pending items</div>';
    return;
  }
  const groups = {};
  queueItems.forEach(item => {
    const g = item.tool || 'general';
    if (!groups[g]) groups[g] = [];
    groups[g].push(item);
  });
  let html = '';
  for (const [tool, items] of Object.entries(groups)) {
    html += `<div class="queue-group">`;
    html += `<div class="queue-group-header">${tool} <span class="count">(${items.length})</span></div>`;
    items.forEach(item => {
      const time = item.created_at ? new Date(item.created_at).toLocaleString(undefined, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}) : '';
      html += `<div class="queue-item" onclick="openPopup('${item.id}')">
        <div class="qi-title">${item.title}</div>
        <div class="qi-time">${time}</div>
      </div>`;
    });
    html += '</div>';
  }
  el.innerHTML = html;
}

function openPopup(itemId) {
  const item = queueItems.find(i => i.id === itemId);
  if (!item) return;
  document.getElementById('queue-popup-title').textContent = item.title;
  document.getElementById('queue-popup-body').innerHTML = item.detail_html || '<em>No details</em>';
  const syncCmd = document.getElementById('sync-cmd');
  if (syncCmd) syncCmd.textContent = 'curl -o imp-sync.py ' + location.origin + '/imp-sync.py && python imp-sync.py';
  const actionsEl = document.getElementById('queue-popup-actions');
  actionsEl.innerHTML = '';
  (item.actions || []).forEach((a, i) => {
    const btn = document.createElement('button');
    btn.textContent = a.label;
    btn.className = i === 0 ? 'action-primary' : 'action-secondary';
    btn.onclick = () => resolveItem(item.id, a.action);
    actionsEl.appendChild(btn);
  });
  const closeBtn = document.createElement('button');
  closeBtn.textContent = 'Close';
  closeBtn.className = 'action-close';
  closeBtn.onclick = closePopup;
  actionsEl.appendChild(closeBtn);
  document.getElementById('queue-popup').style.display = '';
}

function closePopup() {
  document.getElementById('queue-popup').style.display = 'none';
}

async function resolveItem(itemId, action) {
  try {
    await fetch(`${API}/api/queue/${itemId}/action`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action}),
    });
    closePopup();
    loadQueue();
  } catch (e) { console.error('resolveItem failed:', e); }
}
