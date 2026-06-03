/* Gubernator — frontend */
'use strict';

const HOST  = window.location.host;
const WS_PROTO = window.location.protocol === 'https:' ? 'wss' : 'ws';

let tuiWs, shellWs, chatWs;
let tuiTerm, shellTerm;
let tuiFit, shellFit;
let currentClaudeBubble = null;
let thinkingBubble      = null;
let tuiThinkingBubble   = null;
let sending             = false;
let lastUserMessage     = '';
let authToken           = localStorage.getItem('gov_token') || '';

// ── Auth guard ────────────────────────────────────────────────────────────────
async function checkAuth() {
  const res = await fetch('/api/auth/me');
  if (!res.ok) { location.href = '/'; return null; }
  const user = await res.json();
  document.getElementById('bar-user').textContent = user.email;
  return user;
}

async function doLogout() {
  await fetch('/api/auth/logout', { method: 'POST' });
  localStorage.removeItem('gov_token');
  location.href = '/';
}

// ── Status indicator ──────────────────────────────────────────────────────────
function setStatus(col, state, label) {
  document.getElementById(`status-${col}`).className = `status-dot ${state}`;
  if (label !== undefined)
    document.getElementById(`status-label-${col}`).textContent = label;
}

// ── Terminal init ─────────────────────────────────────────────────────────────
function initTerminal(wrapperId, wsPath, col) {
  const term = new Terminal({
    theme: { background: '#000000', foreground: '#e6edf3', cursor: '#58a6ff',
             selectionBackground: 'rgba(88,166,255,0.25)' },
    fontFamily: "'JetBrains Mono', 'Menlo', 'Monaco', monospace",
    fontSize: 13, cursorBlink: true, scrollback: 3000,
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(document.getElementById(wrapperId));
  fit.fit();

  const handle = { ws: null, fit };
  let reconnectDelay = 2000;
  let reconnectTimer = null;

  function connect() {
    const url = `${WS_PROTO}://${HOST}${wsPath}?token=${encodeURIComponent(authToken)}`;
    const ws  = new WebSocket(url);
    ws.binaryType = 'arraybuffer';
    handle.ws = ws;
    if (col === 'tui')   tuiWs   = ws;
    else                 shellWs = ws;

    ws.onopen = () => {
      reconnectDelay = 2000;
      setStatus(col, 'connected', 'Connected');
      ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
    };
    ws.onclose = () => {
      setStatus(col, 'error', `Disconnected — reconnecting in ${Math.round(reconnectDelay/1000)}s…`);
      scheduleReconnect();
    };
    ws.onerror = () => setStatus(col, 'error', 'Error');

    ws.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(e.data), () => term.scrollToBottom());
      } else {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === 'error') {
            setStatus(col, 'error', 'SSH Error');
            term.write(`\r\n\x1b[31m✗ ${msg.message}\x1b[0m\r\n`, () => term.scrollToBottom());
          } else if (msg.type === 'ssh_disconnected') {
            setStatus(col, 'error', 'SSH dropped — reconnecting…');
            term.write('\r\n\x1b[33m⚠ SSH session dropped — reconnecting…\x1b[0m\r\n', () => term.scrollToBottom());
          }
        } catch (_) {}
      }
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer  = null;
      reconnectDelay  = Math.min(reconnectDelay * 1.5, 30_000);
      term.write('\r\n\x1b[33m⟳ Reconnecting…\x1b[0m\r\n', () => term.scrollToBottom());
      connect();
    }, reconnectDelay);
  }

  term.onData((data) => {
    if (handle.ws && handle.ws.readyState === WebSocket.OPEN)
      handle.ws.send(JSON.stringify({ type: 'input', data }));
  });

  new ResizeObserver(() => {
    fit.fit();
    if (handle.ws && handle.ws.readyState === WebSocket.OPEN)
      handle.ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
  }).observe(document.getElementById(wrapperId));

  connect();
  return { term, get ws() { return handle.ws; }, fit };
}

// ── Draggable dividers ────────────────────────────────────────────────────────
function reflowTerminals() {
  if (tuiFit)   try { tuiFit.fit();   } catch (_) {}
  if (shellFit) try { shellFit.fit(); } catch (_) {}
  if (tuiWs   && tuiWs.readyState   === WebSocket.OPEN && tuiTerm)
    tuiWs.send(JSON.stringify({ type: 'resize', cols: tuiTerm.cols, rows: tuiTerm.rows }));
  if (shellWs && shellWs.readyState === WebSocket.OPEN && shellTerm)
    shellWs.send(JSON.stringify({ type: 'resize', cols: shellTerm.cols, rows: shellTerm.rows }));
}

function initDragDividers() {
  const vDiv     = document.getElementById('v-divider');
  const colChat  = document.getElementById('col-chat');
  const hDiv     = document.getElementById('h-divider');
  const colTui   = document.getElementById('col-tui');
  const rightPanel = document.getElementById('right-panel');
  let dragging = null, startX, startY, startW, startH;

  vDiv.addEventListener('mousedown', (e) => {
    dragging = 'v'; startX = e.clientX; startW = colChat.offsetWidth;
    vDiv.classList.add('dragging'); document.body.classList.add('dragging-v'); e.preventDefault();
  });
  hDiv.addEventListener('mousedown', (e) => {
    dragging = 'h'; startY = e.clientY; startH = colTui.offsetHeight;
    hDiv.classList.add('dragging'); document.body.classList.add('dragging-h'); e.preventDefault();
  });
  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    if (dragging === 'v') {
      const newW = Math.max(220, Math.min(startW + e.clientX - startX, window.innerWidth - 300));
      colChat.style.width = newW + 'px';
    } else {
      const newH = Math.max(100, Math.min(startH + e.clientY - startY, rightPanel.offsetHeight - hDiv.offsetHeight - 80));
      colTui.style.flex = 'none'; colTui.style.height = newH + 'px';
    }
    reflowTerminals();
  });
  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    if (dragging === 'v') vDiv.classList.remove('dragging');
    else                  hDiv.classList.remove('dragging');
    document.body.classList.remove('dragging-v', 'dragging-h');
    dragging = null; reflowTerminals();
  });
}

// ── Stats ─────────────────────────────────────────────────────────────────────
function updateClaudeStats(msg) {
  const n = msg.session_tokens;
  document.getElementById('stat-tokens').textContent = n >= 1000 ? (n/1000).toFixed(1)+'k tok' : n+' tok';
  document.getElementById('stat-ctx').textContent    = msg.context_pct + '% ctx';
  document.getElementById('stat-cost').textContent   = '$' + msg.session_cost.toFixed(4);
}
function setActionButtonsDisabled(d) {
  document.querySelectorAll('.btn-confirm,.btn-dismiss').forEach(b => b.disabled = d);
}

async function fetchOpenClawStats() {
  try {
    const data = await (await fetch('/api/openclaw-stats')).json();
    if (data.context_pct != null) {
      document.getElementById('stat-oc-ctx').textContent   = data.context_pct + '% ctx';
      document.getElementById('stat-oc-cache').textContent = data.cache_pct != null ? data.cache_pct + '% cached' : 'cache —';
    }
  } catch (_) {}
}

// ── TUI helpers ───────────────────────────────────────────────────────────────
const _CRED_KEYWORDS = ['password','passphrase','api key','api_key','secret','token','credential','enter your','provide your'];
function maybeShowTuiHint(text) {
  if (_CRED_KEYWORDS.some(k => text.toLowerCase().includes(k))) {
    document.getElementById('tui-input-hint').style.display = 'flex';
    reflowTerminals();
  }
}
function dismissTuiHint() {
  const f = document.getElementById('tui-direct-input');
  if (f) f.value = '';
  document.getElementById('tui-input-hint').style.display = 'none';
  reflowTerminals();
}
function sendDirectToTui() {
  const f = document.getElementById('tui-direct-input');
  const t = f ? f.value : '';
  if (!t || !tuiWs || tuiWs.readyState !== WebSocket.OPEN) return;
  tuiWs.send(JSON.stringify({ type: 'input', data: t + '\r' }));
  f.value = '';
  dismissTuiHint();
}
function cancelTuiPoll() {
  if (chatWs && chatWs.readyState === WebSocket.OPEN)
    chatWs.send(JSON.stringify({ type: 'cancel_tui' }));
  document.getElementById('tui-cancel-btn').style.display = 'none';
}
function cancelClaude() {
  if (chatWs && chatWs.readyState === WebSocket.OPEN)
    chatWs.send(JSON.stringify({ type: 'cancel_claude' }));
  document.getElementById('claude-cancel-btn').style.display = 'none';
}
function enterTuiScrollMode() {
  if (!tuiWs || tuiWs.readyState !== WebSocket.OPEN) return;
  tuiWs.send(JSON.stringify({ type: 'input', data: '\x02[' }));
  const btn = document.getElementById('tui-scroll-btn');
  btn.textContent = '✕ Exit scroll (q)'; btn.classList.add('active'); btn.onclick = exitTuiScrollMode;
  tuiTerm.focus();
}
function exitTuiScrollMode() {
  if (tuiWs && tuiWs.readyState === WebSocket.OPEN)
    tuiWs.send(JSON.stringify({ type: 'input', data: 'q' }));
  const btn = document.getElementById('tui-scroll-btn');
  btn.textContent = '↑ Scroll'; btn.classList.remove('active'); btn.onclick = enterTuiScrollMode;
}

// ── Chat helpers ──────────────────────────────────────────────────────────────
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function formatClaudeMessage(text) {
  return text.split(/(```[\s\S]*?```)/g).map(part => {
    const m = part.match(/^```(\w*)\n?([\s\S]*?)```$/);
    if (m) {
      const lang = m[1]||'', code = m[2].replace(/\n$/,''), lines = code.split('\n');
      const collapsed = lines.length > 5;
      const id = 'cb-'+Math.random().toString(36).slice(2,9);
      return `<div class="code-block"><div class="code-block-hdr"><span class="code-lang">${esc(lang)||'code'}</span>`+
        (collapsed ? `<button class="code-toggle" onclick="toggleCode('${id}',this)">▾ ${lines.length} lines</button>` : '')+
        `</div><div class="code-wrap${collapsed?' code-collapsed':''}" id="${id}"><pre class="code-pre"><code>${esc(code)}</code></pre></div></div>`;
    }
    return `<span class="msg-text">${esc(part)}</span>`;
  }).join('');
}
function toggleCode(id, btn) {
  const w = document.getElementById(id); if (!w) return;
  const c = w.classList.toggle('code-collapsed');
  btn.textContent = c ? `▾ ${w.querySelector('pre').textContent.split('\n').length} lines` : '▴ collapse';
}
function toggleOutput(id, btn) {
  const w = document.getElementById(id); if (!w) return;
  const c = w.classList.toggle('code-collapsed');
  btn.textContent = c ? `▾ ${w.querySelector('pre').textContent.split('\n').length} lines` : '▴ collapse';
}
function scrollChat() { const el = document.getElementById('chat-messages'); el.scrollTop = el.scrollHeight; }
function addBubble(cls, text) {
  const el = document.createElement('div');
  el.className = `bubble ${cls}`; el.textContent = text;
  document.getElementById('chat-messages').appendChild(el); scrollChat(); return el;
}
function addThinkingBubble() {
  const el = document.createElement('div');
  el.className = 'bubble thinking';
  el.innerHTML = '<span class="thinking-dots"><span></span><span></span><span></span></span>';
  document.getElementById('chat-messages').appendChild(el); scrollChat(); return el;
}
function removeThinkingBubble() { if (thinkingBubble) { thinkingBubble.remove(); thinkingBubble = null; } }
function addActionBubble(action) {
  const el = document.createElement('div');
  el.id = `action-${action.id}`; el.className = `action-bubble ${action.action_type}`;
  const dis = sending ? 'disabled' : '';
  if (action.action_type === 'vps_write') {
    const lines = (action.data||'').split('\n'), lc = lines.length;
    const preview = lines.slice(0,8).join('\n')+(lc>8?`\n… (${lc} lines total)`:'');
    el.innerHTML = `<div class="action-header">📄 → VPS FILE WRITE</div><div class="action-file-path">${esc(action.path||'')}</div><pre class="action-file-preview">${esc(preview)}</pre><div class="action-buttons"><button class="btn-confirm" onclick="confirmAction('${action.id}')" ${dis}>Write ▶</button><button class="btn-dismiss" onclick="dismissAction('${action.id}')" ${dis}>Skip</button></div><div class="action-result" id="result-${action.id}"></div>`;
  } else {
    const icon = action.action_type === 'tui_input' ? '🦞 → OPENCLAW TUI' : '💻 → VPS SHELL';
    el.innerHTML = `<div class="action-header">${icon}</div><code>${esc(action.data)}</code><div class="action-buttons"><button class="btn-confirm" onclick="confirmAction('${action.id}')" ${dis}>Run ▶</button><button class="btn-dismiss" onclick="dismissAction('${action.id}')" ${dis}>Skip</button></div><div class="action-result" id="result-${action.id}"></div>`;
  }
  document.getElementById('chat-messages').appendChild(el); scrollChat();
}
function resolveAction(id, state, label) {
  const el = document.getElementById(`action-${id}`); if (!el) return;
  el.className = `action-bubble ${state}`;
  el.querySelectorAll('button').forEach(b => b.remove());
  const res = document.getElementById(`result-${id}`);
  if (res && label) res.textContent = label;
}
function confirmAction(id) {
  if (sending) return;
  sending = true;
  document.getElementById('chat-send').disabled = true;
  setActionButtonsDisabled(true);
  thinkingBubble = addThinkingBubble();
  chatWs.send(JSON.stringify({ type: 'confirm', action_id: id }));
}
function dismissAction(id) {
  chatWs.send(JSON.stringify({ type: 'dismiss', action_id: id }));
  resolveAction(id, 'dismissed', 'Skipped');
}
function addCollapsibleOutput(icon, label, cmd, output) {
  const el = document.createElement('div'); el.className = 'vps-output-bubble';
  const lines = output.split('\n'), long = lines.length > 10;
  const id = 'op-'+Math.random().toString(36).slice(2,9);
  el.innerHTML = `<div class="vps-output-header">${icon} ${label}${long?` <button class="code-toggle" style="margin-left:auto" onclick="toggleOutput('${id}',this)">▾ ${lines.length} lines</button>`:''}</div><div class="vps-output-cmd">${esc(cmd)}</div><div class="output-wrap${long?' code-collapsed':''}" id="${id}"><pre class="vps-output-pre">${esc(output)}</pre></div>`;
  document.getElementById('chat-messages').appendChild(el); scrollChat();
}
function addVpsOutputBubble(cmd, output) { addCollapsibleOutput('💻', 'VPS Output', cmd, output); }
function addTuiOutputBubble(cmd, output) { addCollapsibleOutput('🦞', 'OpenClaw TUI Output', cmd, output); }
function addNoApiKeyBubble() {
  const el = document.createElement('div');
  el.className = 'bubble credits-error';
  el.innerHTML = `
🔑 <strong>Claude API key not set</strong><br><br>
Gubernator needs your Anthropic API key to use Claude.<br><br>
<button class="retry-btn" onclick="openSettingsModal()" style="margin-bottom:10px">⚙️ Open Settings to add it</button><br>
Or get a key at <a href="https://console.anthropic.com/settings/api-keys" target="_blank">console.anthropic.com</a>
  `.trim();
  document.getElementById('chat-messages').appendChild(el);
  scrollChat();
}

function addCreditsBubble() {
  const el = document.createElement('div'); el.className = 'bubble credits-error';
  el.innerHTML = `💳 <strong>Anthropic API credits exhausted</strong><br><br>The Claude column is unavailable until credits are topped up.<br><br>👉 <a href="https://console.anthropic.com/settings/billing" target="_blank">console.anthropic.com → Billing</a><br><br><em>Once topped up, click Retry.</em><br><br><button class="retry-btn" onclick="retryLastMessage(this)">↩ Retry</button>`;
  document.getElementById('chat-messages').appendChild(el); scrollChat();
}
function addRateLimitBubble() {
  const el = document.createElement('div'); el.className = 'bubble credits-error';
  el.innerHTML = `⚡ <strong>Rate limit reached</strong><br><br>Wait ~60 seconds then send your message again.`;
  document.getElementById('chat-messages').appendChild(el); scrollChat();
}
function retryLastMessage(btn) {
  if (!lastUserMessage || sending) return;
  btn.disabled = true; btn.textContent = '…';
  addBubble('you', lastUserMessage);
  sending = true; document.getElementById('chat-send').disabled = true;
  chatWs.send(JSON.stringify({ type: 'user_message', content: lastUserMessage }));
}

// ── Send message ──────────────────────────────────────────────────────────────
function sendMessage() {
  const input = document.getElementById('chat-input');
  const text  = input.value.trim();
  if (!text || sending || !chatWs || chatWs.readyState !== WebSocket.OPEN) return;
  lastUserMessage = text;
  addBubble('you', text);
  input.value = ''; input.style.height = 'auto';
  sending = true; document.getElementById('chat-send').disabled = true;
  chatWs.send(JSON.stringify({ type: 'user_message', content: text }));
}

// ── Chat WebSocket ────────────────────────────────────────────────────────────
function initChat() {
  const url = `${WS_PROTO}://${HOST}/ws/chat?token=${encodeURIComponent(authToken)}`;
  chatWs = new WebSocket(url);
  chatWs.onopen  = () => setStatus('chat', 'connected', 'Ready');
  chatWs.onclose = () => { setStatus('chat', 'error', 'Disconnected'); setTimeout(initChat, 3000); };
  chatWs.onerror = () => setStatus('chat', 'error', 'Error');

  chatWs.onmessage = (e) => {
    let msg; try { msg = JSON.parse(e.data); } catch (_) { return; }
    switch (msg.type) {
      case 'status':      addBubble('status', msg.message); break;
      case 'tui_thinking':
        if (!tuiThinkingBubble) tuiThinkingBubble = addBubble('status', '');
        tuiThinkingBubble.textContent = `⏳ OpenClaw agent thinking… (${msg.elapsed}s)`;
        document.getElementById('tui-cancel-btn').style.display = '';
        break;
      case 'claude_start':
        currentClaudeBubble = null;
        if (!thinkingBubble) thinkingBubble = addThinkingBubble();
        document.getElementById('claude-cancel-btn').style.display = '';
        break;
      case 'claude_cancel':
        if (currentClaudeBubble) { currentClaudeBubble.remove(); currentClaudeBubble = null; }
        removeThinkingBubble();
        document.getElementById('claude-cancel-btn').style.display = 'none';
        break;
      case 'chunk':
        if (!currentClaudeBubble) { removeThinkingBubble(); currentClaudeBubble = addBubble('claude', ''); }
        currentClaudeBubble.textContent += msg.content; scrollChat();
        break;
      case 'done':
        removeThinkingBubble();
        document.getElementById('tui-cancel-btn').style.display = 'none';
        document.getElementById('claude-cancel-btn').style.display = 'none';
        if (msg.clean_text !== undefined) {
          if (!currentClaudeBubble) currentClaudeBubble = addBubble('claude', '');
          currentClaudeBubble.innerHTML = formatClaudeMessage(msg.clean_text);
        }
        currentClaudeBubble = null; sending = false;
        setActionButtonsDisabled(false);
        document.getElementById('chat-send').disabled = false;
        document.getElementById('chat-input').focus();
        break;
      case 'stats':  updateClaudeStats(msg); break;
      case 'vps_output':
        addVpsOutputBubble(msg.cmd, msg.output); maybeShowTuiHint(msg.output); fetchOpenClawStats(); break;
      case 'tui_output':
        if (tuiThinkingBubble) { tuiThinkingBubble.remove(); tuiThinkingBubble = null; }
        removeThinkingBubble();
        document.getElementById('tui-cancel-btn').style.display = 'none';
        addTuiOutputBubble(msg.cmd, msg.output); maybeShowTuiHint(msg.output); break;
      case 'action':       addActionBubble(msg); break;
      case 'action_done':
        dismissTuiHint(); resolveAction(msg.action_id, 'done', `✓ ${msg.label}`);
        document.getElementById('claude-cancel-btn').style.display = ''; break;
      case 'action_error': resolveAction(msg.action_id, 'error-state', `✗ ${msg.message}`); break;
      case 'error':
        removeThinkingBubble();
        if (currentClaudeBubble) { currentClaudeBubble.remove(); currentClaudeBubble = null; }
        if (msg.subtype === 'credits_exhausted') addCreditsBubble();
        else if (msg.subtype === 'rate_limit')   addRateLimitBubble();
        else if (msg.subtype === 'no_api_key')   addNoApiKeyBubble();
        else addBubble('error', msg.message || 'Unknown error');
        sending = false; setActionButtonsDisabled(false);
        document.getElementById('chat-send').disabled = false; break;
    }
  };
}

// ── Modal helpers ─────────────────────────────────────────────────────────────
function openModal(id)  { document.getElementById(id).style.display = 'flex'; }
function closeModal(id) { document.getElementById(id).style.display = 'none'; }
function closeModalOnBg(e, id) { if (e.target === document.getElementById(id)) closeModal(id); }

// ── Vault modal ───────────────────────────────────────────────────────────────
function openVaultModal() {
  openModal('vault-modal');
  loadCredentials();
}

async function loadCredentials() {
  const res   = await fetch('/api/credentials');
  const creds = await res.json();
  const list  = document.getElementById('cred-list');
  if (!creds.length) {
    list.innerHTML = '<p class="empty-state">No credentials saved yet. Click + Add to add one.</p>';
    return;
  }
  list.innerHTML = `
    <table class="cred-table">
      <thead><tr><th>Name</th><th>Username</th><th>VPS</th><th></th></tr></thead>
      <tbody>${creds.map(c => `
        <tr>
          <td><span class="cred-name">${esc(c.name)}</span></td>
          <td><span class="cred-user">${esc(c.username||'—')}</span></td>
          <td>${c.vps_synced ? '<span class="cred-badge">synced</span>' : ''}</td>
          <td><div class="cred-actions">
            <button class="cred-btn" onclick="editCredential('${c.id}','${esc(c.name)}','${esc(c.username||'')}','${esc(c.notes||'')}',${c.vps_synced})">Edit</button>
            <button class="cred-btn danger" onclick="deleteCredential('${c.id}','${esc(c.name)}')">Delete</button>
          </div></td>
        </tr>`).join('')}
      </tbody>
    </table>`;
}

function showCredForm(name='', username='', notes='', vps=false) {
  document.getElementById('cf-name').value  = name;
  document.getElementById('cf-user').value  = username;
  document.getElementById('cf-pass').value  = '';
  document.getElementById('cf-notes').value = notes;
  document.getElementById('cf-vps').checked = vps;
  document.getElementById('cf-name').disabled = !!name;  // can't rename
  document.getElementById('cred-form-wrap').style.display = '';
}
function hideCredForm() { document.getElementById('cred-form-wrap').style.display = 'none'; }

async function editCredential(id, name, username, notes, vps) {
  showCredForm(name, username, notes, vps);
  document.getElementById('cf-pass').placeholder = '(leave blank to keep current)';
}

async function saveCredential() {
  const name  = document.getElementById('cf-name').value.trim();
  const user  = document.getElementById('cf-user').value.trim();
  const pass  = document.getElementById('cf-pass').value;
  const notes = document.getElementById('cf-notes').value.trim();
  const vps   = document.getElementById('cf-vps').checked;
  if (!name) { alert('Name is required'); return; }
  if (!pass && !document.getElementById('cf-name').disabled) { alert('Password is required'); return; }
  // If editing with blank password, we skip — but we still need a value to send
  // For now, require it. Future: PATCH endpoint that allows partial update.
  if (!pass) { alert('Enter the password (re-enter to confirm edit)'); return; }
  const res = await fetch('/api/credentials', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, username: user, password: pass, notes, vps_synced: vps }),
  });
  if (res.ok) { hideCredForm(); loadCredentials(); }
  else { const d = await res.json(); alert(d.detail || 'Save failed'); }
}

async function deleteCredential(id, name) {
  if (!confirm(`Delete credential "${name}"?`)) return;
  await fetch(`/api/credentials/${id}`, { method: 'DELETE' });
  loadCredentials();
}

function togglePw(inputId, btn) {
  const inp = document.getElementById(inputId);
  if (inp.type === 'password') { inp.type = 'text';     btn.textContent = '🙈'; }
  else                         { inp.type = 'password'; btn.textContent = '👁'; }
}

// ── VPS modal ─────────────────────────────────────────────────────────────────
async function openVpsModal() {
  openModal('vps-modal');
  const res  = await fetch('/api/vps');
  const vpss = await res.json();
  const list = document.getElementById('vps-list');
  if (vpss.length) {
    list.innerHTML = vpss.map(v => `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid var(--border);font-size:13px">
        <span style="flex:1"><strong>${esc(v.label)}</strong> — ${esc(v.username)}@${esc(v.host)}:${v.port}</span>
        <button class="cred-btn danger" onclick="deleteVps('${v.id}','${esc(v.label)}')">Delete</button>
      </div>`).join('');
  } else {
    list.innerHTML = '<p style="color:var(--dim);font-size:12px;margin-top:12px">No VPS configured yet.</p>';
  }
}

async function saveVps() {
  const host  = document.getElementById('vf-host').value.trim();
  const port  = parseInt(document.getElementById('vf-port').value)||22;
  const user  = document.getElementById('vf-user').value.trim()||'root';
  const pass  = document.getElementById('vf-pass').value;
  const label = document.getElementById('vf-label').value.trim()||'My VPS';
  if (!host || !pass) { alert('Host and password are required'); return; }
  const res = await fetch('/api/vps', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label, host, port, username: user, password: pass }),
  });
  if (res.ok) {
    closeModal('vps-modal');
    addBubble('status', '✅ VPS saved — reconnecting terminals…');
    setTimeout(() => location.reload(), 500);
  } else {
    const d = await res.json(); alert(d.detail || 'Save failed');
  }
}

async function deleteVps(id, label) {
  if (!confirm(`Delete VPS "${label}"?`)) return;
  await fetch(`/api/vps/${id}`, { method: 'DELETE' });
  openVpsModal();
}

// ── Settings modal ────────────────────────────────────────────────────────────
async function openSettingsModal() {
  openModal('settings-modal');
  // Show whether a key is already saved
  const res   = await fetch('/api/credentials');
  const creds = await res.json();
  const has   = creds.some(c => c.name === '_anthropic_key');
  const status = document.getElementById('sf-apikey-status');
  status.textContent = has ? '✓ API key saved' : '✗ No API key saved yet';
  status.style.color = has ? 'var(--green)' : 'var(--orange)';
}

async function saveApiKey() {
  const key = document.getElementById('sf-apikey').value.trim();
  if (!key || !key.startsWith('sk-')) { alert('Enter a valid Anthropic API key (starts with sk-)'); return; }
  const res = await fetch('/api/credentials', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: '_anthropic_key', username: '', password: key, notes: 'Anthropic API key', vps_synced: false }),
  });
  if (res.ok) {
    document.getElementById('sf-apikey').value = '';
    document.getElementById('sf-apikey-status').textContent = '✓ Saved — reconnect Claude to use it';
    document.getElementById('sf-apikey-status').style.color = 'var(--green)';
    // Reconnect the chat WebSocket to pick up the new key
    if (chatWs) { chatWs.close(); }
  } else {
    alert('Failed to save API key');
  }
}

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  await checkAuth();

  const chatInput = document.getElementById('chat-input');
  chatInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  chatInput.addEventListener('input', () => {
    chatInput.style.height = 'auto';
    chatInput.style.height = Math.min(chatInput.scrollHeight, 160) + 'px';
  });
  document.getElementById('tui-direct-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); sendDirectToTui(); }
  });

  // Check if VPS is configured — if not, prompt setup
  const vpsRes = await fetch('/api/vps');
  const vpsList = await vpsRes.json();
  if (!vpsList.length) {
    openVpsModal();
    addBubble('status', '⚙️ No VPS configured yet — add one to get started.');
  }

  const tui   = initTerminal('tui-terminal',   '/ws/tui',   'tui');
  const shell = initTerminal('shell-terminal', '/ws/shell', 'shell');
  tuiTerm   = tui.term;   tuiWs   = tui.ws;   tuiFit   = tui.fit;
  shellTerm = shell.term; shellWs = shell.ws; shellFit = shell.fit;

  document.getElementById('tui-terminal').addEventListener('click',   () => tuiTerm.focus());
  document.getElementById('shell-terminal').addEventListener('click', () => shellTerm.focus());

  initDragDividers();
  initChat();
  fetchOpenClawStats();
});
