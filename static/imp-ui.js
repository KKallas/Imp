/* imp-ui.js — shared UI components
 *
 * Every card, item, button, fold, and code block in the app uses these.
 * Fix one, fix all.
 */

const imp = {
  // ── rendering ───────────────────────────────────────────────────

  /** Syntax-highlighted code block with line numbers. */
  code(src) {
    return `<pre class="imp-code">${imp._highlight(src)}</pre>`;
  },

  /** Collapsible details block. opts: {cls, open} */
  fold(title, content, opts) {
    const cls = opts?.cls ? ` ${opts.cls}` : '';
    const open = opts?.open ? ' open' : '';
    return `<details class="imp-fold${cls}"${open}><summary>${title}</summary>${content}</details>`;
  },

  /** File block: collapsible with highlighted code inside. */
  file(path, src, opts) {
    const lines = src.split('\n').length;
    const cls = opts?.cls || 'ok';
    return imp.fold(`📄 ${path} (${lines} lines)`, imp.code(src), {cls});
  },

  /**
   * Button. opts: {cls: 'primary'|'danger'|'ok'|'small', disabled, title}
   */
  btn(label, onclick, opts) {
    const o = opts || {};
    if (o.cls === 'primary') {
      const dis = o.disabled ? ' disabled' : '';
      return `<button class="wf-start"${dis} onclick="event.stopPropagation();event.preventDefault();${onclick}">${label}</button>`;
    }
    if (o.cls === 'small') {
      const dis = o.disabled ? ' disabled' : '';
      return `<button class="wf-start" style="padding:3px 8px;font-size:11px;"${dis} onclick="event.stopPropagation();event.preventDefault();${onclick}" title="${o.title||''}">${label}</button>`;
    }
    let style = '';
    if (o.cls === 'danger') style = ' style="color:#da3633;border-color:#da3633;"';
    else if (o.cls === 'ok') style = ' style="color:#3fb950;border-color:#3fb950;"';
    const dis = o.disabled ? ' disabled' : '';
    return `<button class="wf-btn"${style}${dis} onclick="event.stopPropagation();event.preventDefault();${onclick}">${label}</button>`;
  },

  /**
   * Card — top-level collapsible item (tool group or workflow).
   * opts: {id, name, meta, status?, buttons:[], body, open?}
   */
  card(opts) {
    const statusHtml = opts.status
      ? `<span class="wf-item-status ${opts.status.cls || ''}">${opts.status.icon || ''} ${opts.status.text || ''}</span>`
      : '';
    const open = opts.open ? ' open' : '';
    return `<details class="wf-item" data-id="${opts.id}"${open}>
      <summary>
        <span class="wf-item-name">${opts.name}</span>
        <span class="wf-item-meta">${opts.meta || ''}</span>
        ${statusHtml}
        ${(opts.buttons || []).join(' ')}
      </summary>
      <div class="wf-body">${opts.body || ''}</div>
    </details>`;
  },

  /**
   * Child item inside a card (tool script or workflow step).
   * opts: {id, name, buttons:[], body, status?, open?}
   */
  item(opts) {
    const cls = opts.status ? ` ${opts.status}` : '';
    const open = opts.open ? ' open' : '';
    return `<details class="wf-step-item${cls}" data-id="${opts.id}"${open}>
      <summary style="display:flex;align-items:center;gap:6px;">
        <span style="flex:1;">${opts.name}</span>
        ${(opts.buttons || []).join(' ')}
      </summary>
      <div class="wf-step-output" style="padding:8px 10px;">${opts.body || ''}</div>
    </details>`;
  },

  /** Markdown readme section (rendered or editable). */
  readme(html) {
    return html ? `<div class="wf-readme">${html}</div>` : '';
  },
  readmeEdit(id, content) {
    const escaped = (content||'').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    return `<div class="wf-readme"><textarea class="wf-readme-edit" id="${id}" style="min-height:150px;">${escaped}</textarea></div>`;
  },

  /** Steps/items container. */
  items(innerHtml) {
    return `<div class="wf-steps">${innerHtml}</div>`;
  },

  /** Meta line (description, last run, etc). */
  meta(text) {
    return `<div class="wf-item-meta" style="margin-bottom:8px;">${text}</div>`;
  },

  /** Spinner overlay for locked/loading items. */
  lockOverlay(msg) {
    return `<div style="display:flex;align-items:center;gap:10px;padding:12px;"><div class="wf-spinner"></div><span style="font-size:12px;color:var(--muted);">${msg}</span></div>`;
  },

  // ── post-render ─────────────────────────────────────────────────

  /** Apply syntax highlighting to all .imp-code blocks in a container. */
  highlightAll(container) {
    (container || document).querySelectorAll('pre.imp-code').forEach(pre => {
      if (pre.dataset.highlighted) return;
      pre.dataset.highlighted = '1';
      const lines = pre.querySelectorAll('.line');
      if (lines.length > 0) {
        lines.forEach(span => {
          const raw = span.textContent;
          span.innerHTML = imp._highlightLine(raw);
        });
      } else {
        const text = pre.textContent;
        pre.innerHTML = imp._highlight(text);
      }
    });
  },

  // ── details state ───────────────────────────────────────────────

  /** Save which <details> are open before re-render. */
  getOpenState(container) {
    const open = new Set();
    container.querySelectorAll('details[open]').forEach(d => {
      const id = d.dataset.id || d.querySelector('summary')?.textContent?.trim();
      if (id) open.add(id);
    });
    return open;
  },

  /** Restore open state after re-render. */
  restoreOpenState(container, openSet) {
    container.querySelectorAll('details').forEach(d => {
      const id = d.dataset.id || d.querySelector('summary')?.textContent?.trim();
      if (id && openSet.has(id)) d.open = true;
    });
  },

  /** Apply lock overlay to a specific data-id element. */
  applyLock(container, dataId, msg) {
    const locked = container.querySelector(`[data-id="${dataId}"]`);
    if (!locked) return;
    locked.open = true;
    const body = locked.querySelector('.wf-body') || locked.querySelector('.wf-step-output');
    if (body) body.innerHTML = imp.lockOverlay(msg);
    locked.querySelectorAll('.wf-btn, .wf-start').forEach(b => { b.disabled = true; b.style.opacity = '0.3'; });
  },

  // ── internal ────────────────────────────────────────────────────

  _highlightLine(line) {
    let h = line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const spans = [];
    function ph(s) { spans.push(s); return `\x00${spans.length-1}\x00`; }
    h = h.replace(/(#.*)$/, (_, c) => ph(`<span class="cm">${c}</span>`));
    h = h.replace(/((&quot;){3}.*?(&quot;){3}|'{3}.*?'{3})/g, (m) => ph(`<span class="str">${m}</span>`));
    h = h.replace(/(&quot;[^&]*?&quot;|"[^"]*?"|'[^']*?')/g, (m) => ph(`<span class="str">${m}</span>`));
    h = h.replace(/\b(def|return|import|from|if|else|elif|for|in|while|try|except|with|as|class|and|or|not|True|False|None|async|await)\b/g,
      (m) => ph(`<span class="kw">${m}</span>`));
    h = h.replace(/\x00(\d+)\x00/g, (_, i) => spans[parseInt(i)]);
    return h;
  },

  _highlight(src) {
    return src.split('\n').map(line => {
      return `<span class="line">${imp._highlightLine(line)}</span>`;
    }).join('');
  }
};
