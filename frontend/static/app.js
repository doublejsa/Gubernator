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
  let fatalError = false;

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
      if (col === 'tui') updateAgentStatus('ready', 'Agent is ready');
    };
    ws.onclose = () => {
      if (fatalError) return;   // config error — don't loop
      setStatus(col, 'error', `Disconnected — reconnecting in ${Math.round(reconnectDelay/1000)}s…`);
      if (col === 'tui') updateAgentStatus('ready', 'Reconnecting…');
      scheduleReconnect();
    };
    ws.onerror = () => setStatus(col, 'error', 'Error');

    ws.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(e.data), () => term.scrollToBottom());
      } else {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === 'error' && msg.subtype === 'auth_expired') {
            localStorage.removeItem('gov_token'); location.href = '/';
          } else if (msg.type === 'error' && msg.subtype === 'ssh_fatal') {
            // Fatal config error — stop reconnecting, show fix instructions
            fatalError = true;
            setStatus(col, 'error', 'Cannot connect');
            term.write(`\r\n\x1b[31m✗ ${msg.message}\x1b[0m\r\n\x1b[33mCheck your VPS settings: click 🖥 VPS in the bottom bar.\x1b[0m\r\n`, () => term.scrollToBottom());
            addVpsErrorBubble(msg.message);
            return;
          } else if (msg.type === 'error') {
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

// ── Terminals show/hide ───────────────────────────────────────────────────────
let terminalsVisible = localStorage.getItem('gov_terminals') === 'open';  // hidden by default

function applyTerminalsState(animate) {
  const right  = document.getElementById('right-panel');
  const chat   = document.getElementById('col-chat');
  const vdiv   = document.getElementById('v-divider');
  const btn    = document.getElementById('terminals-toggle-btn');

  if (terminalsVisible) {
    right.classList.remove('terminals-hidden');
    chat.classList.remove('terminals-hidden');
    vdiv.classList.remove('terminals-hidden');
    btn.classList.add('active');
    btn.textContent = '✕ Terminals';
  } else {
    right.classList.add('terminals-hidden');
    chat.classList.add('terminals-hidden');
    vdiv.classList.add('terminals-hidden');
    btn.classList.remove('active');
    btn.textContent = '⊞ Terminals';
  }
  // Reflow after CSS transition so xterm sizes correctly
  setTimeout(reflowTerminals, animate ? 320 : 0);
}

function toggleTerminals() {
  terminalsVisible = !terminalsVisible;
  localStorage.setItem('gov_terminals', terminalsVisible ? 'open' : 'closed');
  applyTerminalsState(true);
}

// ── Console collapse (Option B — chevron tab on divider) ──────────────────────
let consoleVisible = localStorage.getItem('gov_console') !== 'open';   // hidden by default

function applyConsoleState(animate) {
  const shell  = document.getElementById('col-shell');
  const hdiv   = document.getElementById('h-divider');
  const label  = document.getElementById('console-tab-label');
  if (consoleVisible) {
    shell.classList.remove('collapsed');
    hdiv.classList.remove('console-hidden');
    label.textContent = '▼ Console';
  } else {
    shell.classList.add('collapsed');
    hdiv.classList.add('console-hidden');
    label.textContent = '▲ Console';
  }
  // Reflow terminals after transition completes
  setTimeout(reflowTerminals, animate ? 280 : 0);
}

function toggleConsole() {
  consoleVisible = !consoleVisible;
  localStorage.setItem('gov_console', consoleVisible ? 'open' : 'closed');
  applyConsoleState(true);
}

// ── Agent status pill ─────────────────────────────────────────────────────────
const STATUS_EMOJI = { ready: '🟢', thinking: '🟡', browsing: '🔵', needs_input: '🔴' };

function updateAgentStatus(code, label) {
  const pill = document.getElementById('agent-status-pill');
  if (!pill) return;
  pill.dataset.status = code || 'ready';
  const emoji = STATUS_EMOJI[code] || '🟢';
  pill.textContent = `${emoji} ${label || 'Agent is ready'}`;
}

// ── Agent subtitle ────────────────────────────────────────────────────────────
async function updateAgentSubtitle() {
  try {
    const vpsList = await (await fetch('/api/vps')).json();
    const vps = vpsList[0];
    if (vps) {
      const sub = document.getElementById('agent-subtitle');
      if (sub) sub.textContent = `${vps.host} · powered by OpenClaw`;
    }
  } catch (_) {}
}

function initDragDividers() {
  const vDiv     = document.getElementById('v-divider');
  const colChat  = document.getElementById('col-chat');
  const hDiv     = document.getElementById('h-divider');
  const colTui   = document.getElementById('col-tui');
  const rightPanel = document.getElementById('right-panel');
  let dragging = null, startX, startY, startW, startH;

  vDiv.addEventListener('mousedown', (e) => {
    if (!terminalsVisible) return;   // can't drag when terminals are hidden
    dragging = 'v'; startX = e.clientX; startW = colChat.offsetWidth;
    vDiv.classList.add('dragging'); document.body.classList.add('dragging-v'); e.preventDefault();
  });
  hDiv.addEventListener('mousedown', (e) => {
    if (e.target.closest('.console-tab')) return;   // don't drag when clicking the tab
    if (!consoleVisible) return;                     // can't drag when console is collapsed
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

function checkAgent() {
  if (sending || !chatWs || chatWs.readyState !== WebSocket.OPEN) return;
  // Remove any lingering awaiting prompt bubbles
  document.querySelectorAll('.check-agent-prompt').forEach(el => el.remove());
  sending = true;
  document.getElementById('chat-send').disabled = true;
  thinkingBubble = addThinkingBubble();
  chatWs.send(JSON.stringify({ type: 'check_agent' }));
}

function addCheckAgentPrompt() {
  // Don't stack duplicates
  document.querySelectorAll('.check-agent-prompt').forEach(el => el.remove());
  const el = document.createElement('div');
  el.className = 'bubble check-agent-prompt';
  el.innerHTML = `⏳ Claude is waiting on the agent to finish.<br><br>
    <button class="retry-btn" onclick="checkAgent()">🔄 Check agent response</button>`;
  document.getElementById('chat-messages').appendChild(el);
  scrollChat();
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
  const el  = document.createElement('div');
  el.id     = `action-${action.id}`;
  el.className = `action-bubble ${action.action_type}`;
  const dis  = sending ? 'disabled' : '';
  const desc = action.desc || {};

  const icon = desc.icon || (
    action.action_type === 'tui_input' ? '🤖' :
    action.action_type === 'vps_write' ? '📝' : '💻'
  );
  const headline = desc.headline || (
    action.action_type === 'tui_input' ? 'Send instruction to agent' :
    action.action_type === 'vps_write' ? `Write ${(action.path||'').split('/').pop()}` :
    'Run command'
  );
  const detailHtml = desc.detail
    ? `<div class="action-detail">${esc(desc.detail)}</div>` : '';

  const rawId = `raw-${action.id}`;
  let rawHtml = '';
  if (action.action_type === 'vps_write') {
    const lines   = (action.data||'').split('\n'), lc = lines.length;
    const preview = lines.slice(0,6).join('\n')+(lc>6?`\n… (${lc} lines total)`:'');
    rawHtml = `<div class="action-raw" id="${rawId}" style="display:none">
      <div class="action-raw-path">${esc(action.path||'')}</div>
      <pre class="action-raw-pre">${esc(preview)}</pre></div>`;
  } else {
    rawHtml = `<div class="action-raw" id="${rawId}" style="display:none">
      <code class="action-raw-cmd">${esc(action.data)}</code></div>`;
  }

  el.innerHTML = `
    <div class="action-friendly">
      <span class="action-friendly-icon">${icon}</span>
      <div class="action-friendly-body">
        <div class="action-friendly-headline">${esc(headline)}</div>
        ${detailHtml}
        <button class="action-details-toggle" onclick="toggleActionDetails('${rawId}',this)">▾ Details</button>
        ${rawHtml}
      </div>
    </div>
    <div class="action-buttons">
      <button class="btn-confirm" onclick="confirmAction('${action.id}')" ${dis}>Allow</button>
      <button class="btn-dismiss" onclick="dismissAction('${action.id}')" ${dis}>Skip</button>
    </div>
    <div class="action-result" id="result-${action.id}"></div>`;

  document.getElementById('chat-messages').appendChild(el);
  scrollChat();
}

function toggleActionDetails(rawId, btn) {
  const raw = document.getElementById(rawId);
  if (!raw) return;
  const visible = raw.style.display !== 'none';
  raw.style.display = visible ? 'none' : '';
  btn.textContent   = visible ? '▾ Details' : '▴ Details';
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
function addVpsErrorBubble(detail) {
  // Only show once — remove any existing VPS error bubble first
  document.querySelectorAll('.vps-error-bubble').forEach(el => el.remove());
  const el = document.createElement('div');
  el.className = 'bubble credits-error vps-error-bubble';
  const reason = detail.includes('nodename') || detail.includes('Name or service')
    ? "The server address couldn't be found. Check for typos in the hostname or IP."
    : detail.includes('Connection refused')
    ? "The server refused the connection. Check the port number (usually 22)."
    : detail.includes('Authentication')
    ? "Login failed. Check your username and password."
    : "Couldn't connect to your server.";
  el.innerHTML = `
🖥️ <strong>Can't reach your server</strong><br><br>
${reason}<br><br>
<button class="retry-btn" onclick="openVpsModal()" style="margin-bottom:4px">🖥 Update VPS Settings</button>
  `.trim();
  document.getElementById('chat-messages').appendChild(el);
  scrollChat();
}

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
      case 'tui_thinking': {
        const st = msg.status || { code: 'thinking', label: 'Agent is thinking…' };
        updateAgentStatus(st.code, st.label);
        if (!tuiThinkingBubble) tuiThinkingBubble = addBubble('status', '');
        const emoji = STATUS_EMOJI[st.code] || '🟡';
        tuiThinkingBubble.textContent = `${emoji} ${st.label} (${msg.elapsed}s)`;
        document.getElementById('tui-cancel-btn').style.display = '';
        break;
      }
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
        // Claude said it's waiting on the agent — surface a manual check button
        if (msg.awaiting_agent) addCheckAgentPrompt();
        break;
      case 'stats':  updateClaudeStats(msg); break;
      case 'vps_output':
        addVpsOutputBubble(msg.cmd, msg.output); maybeShowTuiHint(msg.output); fetchOpenClawStats(); break;
      case 'tui_output':
        if (tuiThinkingBubble) { tuiThinkingBubble.remove(); tuiThinkingBubble = null; }
        removeThinkingBubble();
        document.getElementById('tui-cancel-btn').style.display = 'none';
        updateAgentStatus('ready', 'Agent is ready');
        addTuiOutputBubble(msg.cmd, msg.output); maybeShowTuiHint(msg.output); break;

      case 'agent_status':
        updateAgentStatus(msg.code, msg.label);
        break;
      case 'tasks_updated':
        if (document.getElementById('activity-modal').style.display === 'flex') loadActivity();
        break;
      case 'memory_updated':
        if (document.getElementById('memory-modal').style.display === 'flex') loadMemory();
        break;
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
        else if (msg.subtype === 'auth_expired') { localStorage.removeItem('gov_token'); location.href = '/'; }
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
    // Remove any existing error bubble and reload so terminals reconnect fresh
    document.querySelectorAll('.vps-error-bubble').forEach(el => el.remove());
    addBubble('status', '✅ VPS saved — reconnecting…');
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

function reprofileVps() {
  if (!chatWs || chatWs.readyState !== WebSocket.OPEN) {
    alert('Not connected — try again in a moment.');
    return;
  }
  if (sending) { alert('Claude is busy — wait for it to finish, then re-profile.'); return; }
  closeModal('vps-modal');
  sending = true;
  document.getElementById('chat-send').disabled = true;
  thinkingBubble = addThinkingBubble();
  chatWs.send(JSON.stringify({ type: 'reprofile_vps' }));
}

// ── Onboarding wizard ─────────────────────────────────────────────────────────
async function checkOnboarding() {
  const [vpsRes, credRes] = await Promise.all([
    fetch('/api/vps'),
    fetch('/api/credentials'),
  ]);
  const vpsList  = await vpsRes.json();
  const credList = await credRes.json();
  const hasVps   = vpsList.length > 0;
  const hasKey   = credList.some(c => c.name === '_anthropic_key');

  if (hasVps && hasKey) return false;   // fully set up — no onboarding needed

  const ob = document.getElementById('onboarding');
  ob.style.display = 'flex';

  if (!hasVps) {
    showObStep(1);
  } else if (!hasKey) {
    showObStep(2);
  }
  return true;
}

function showObStep(n) {
  [1, 2, 3].forEach(i => {
    document.getElementById(`ob-step-${i}`).style.display = i === n ? '' : 'none';
  });
}

async function obStep1Next() {
  const host  = document.getElementById('ob-host').value.trim();
  const port  = parseInt(document.getElementById('ob-port').value) || 22;
  const user  = document.getElementById('ob-user').value.trim() || 'root';
  const pass  = document.getElementById('ob-pass').value;
  const label = document.getElementById('ob-label').value.trim() || 'My VPS';
  const err   = document.getElementById('ob-1-error');
  const btn   = document.getElementById('ob-1-label');

  err.style.display = 'none';
  if (!host) { err.textContent = 'Please enter your server address.'; err.style.display = ''; return; }
  if (!pass) { err.textContent = 'Please enter your server password.'; err.style.display = ''; return; }

  btn.textContent = 'Connecting…';
  document.querySelector('#ob-step-1 .ob-btn').disabled = true;

  const res = await fetch('/api/vps', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label, host, port, username: user, password: pass }),
  });

  if (res.ok) {
    showObStep(2);
  } else {
    const d = await res.json();
    err.textContent = d.detail || 'Could not save — check your details and try again.';
    err.style.display = '';
  }
  btn.textContent = 'Connect & Continue →';
  document.querySelector('#ob-step-1 .ob-btn').disabled = false;
}

async function obStep2Next() {
  const key = document.getElementById('ob-apikey').value.trim();
  const err = document.getElementById('ob-2-error');
  const btn = document.getElementById('ob-2-label');

  err.style.display = 'none';
  if (!key || !key.startsWith('sk-')) {
    err.textContent = 'Please enter a valid Anthropic API key (starts with sk-).';
    err.style.display = ''; return;
  }

  btn.textContent = 'Saving…';
  document.querySelector('#ob-step-2 .ob-btn').disabled = true;

  const res = await fetch('/api/credentials', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: '_anthropic_key', username: '', password: key,
                           notes: 'Anthropic API key', vps_synced: false }),
  });

  if (res.ok) {
    showObStep(3);
  } else {
    err.textContent = 'Could not save the API key — please try again.';
    err.style.display = '';
  }
  btn.textContent = 'Save & Continue →';
  document.querySelector('#ob-step-2 .ob-btn').disabled = false;
}

function obSkipStep2() { showObStep(3); }

function finishOnboarding() {
  const ob = document.getElementById('onboarding');
  ob.style.opacity = '0';
  ob.style.transition = 'opacity 0.4s ease';
  setTimeout(() => {
    ob.style.display = 'none';
    ob.style.opacity = '';
    // Reconnect chat WebSocket now that credentials are set
    if (chatWs) chatWs.close();
    initChat();
    // Reload terminals to pick up new VPS
    location.reload();
  }, 400);
}

function skipOnboarding() {
  document.getElementById('onboarding').style.display = 'none';
}

// ── Activity (tasks) modal ────────────────────────────────────────────────────
const TASK_ICON = { in_progress: '⏳', done: '✓', failed: '✗' };

async function openActivityModal() {
  openModal('activity-modal');
  loadActivity();
}

async function loadActivity() {
  const tasks = await (await fetch('/api/tasks')).json();
  const list  = document.getElementById('activity-list');
  if (!tasks.length) {
    list.innerHTML = '<p class="empty-state">No activity yet. As your agent completes tasks, they\'ll appear here.</p>';
    return;
  }
  list.innerHTML = tasks.map(t => {
    const icon = TASK_ICON[t.status] || '•';
    const when = new Date(t.completed_at || t.created_at).toLocaleString();
    return `<div class="task-row task-${t.status}">
      <span class="task-icon">${icon}</span>
      <div class="task-body">
        <div class="task-title">${esc(t.title)}</div>
        ${t.outcome ? `<div class="task-outcome">${esc(t.outcome)}</div>` : ''}
        <div class="task-when">${when}</div>
      </div>
      <button class="cred-btn danger" onclick="deleteTask('${t.id}')">✕</button>
    </div>`;
  }).join('');
}

async function deleteTask(id) {
  await fetch(`/api/tasks/${id}`, { method: 'DELETE' });
  loadActivity();
}

// ── Skills modal ──────────────────────────────────────────────────────────────
async function openSkillsModal() {
  openModal('skills-modal');
  switchSkillsTab('marketplace');
  document.getElementById('skills-search-input').value = '';
  runSkillsSearch();   // default: show popular/featured (empty query)
}

function switchSkillsTab(tab) {
  const isMkt = tab === 'marketplace';
  document.getElementById('skills-tab-marketplace').style.display = isMkt ? '' : 'none';
  document.getElementById('skills-tab-installed').style.display   = isMkt ? 'none' : '';
  document.getElementById('skills-tab-btn-marketplace').classList.toggle('active', isMkt);
  document.getElementById('skills-tab-btn-installed').classList.toggle('active', !isMkt);
  if (!isMkt) loadInstalledSkills();
}

const SKILL_STATE = {
  ready:       { badge: '✓ Ready',       cls: 'ready' },
  installable: { badge: 'Needs setup',   cls: 'installable' },
  unavailable: { badge: 'Unavailable',   cls: 'unavailable' },
};

async function loadInstalledSkills() {
  const list = document.getElementById('skills-installed-list');
  list.innerHTML = '<p class="empty-state">Loading skills…</p>';
  try {
    const skills = await (await fetch('/api/skills/installed')).json();
    if (!skills.length) {
      list.innerHTML = '<p class="empty-state">No skills found — is the VPS connected?</p>';
      return;
    }
    list.innerHTML = skills.map(s => {
      const st = SKILL_STATE[s.state] || SKILL_STATE.unavailable;
      let action = `<span class="skill-state-badge ${st.cls}">${st.badge}</span>`;
      if (s.state === 'installable') {
        action = `<button class="btn-primary skill-install-btn" onclick="setupSkill('${esc(s.name)}','${esc((s.needs||[]).join(', '))}')">Set up</button>`;
      }
      const need = s.state === 'installable' && s.needs.length
        ? `<div class="skill-need">Needs: ${esc(s.needs.join(', '))}</div>`
        : (s.state === 'unavailable' && s.needs.length
            ? `<div class="skill-need">${esc(s.needs.join(', '))}</div>` : '');
      return `<div class="skill-card skill-${st.cls}">
        <div class="skill-card-icon">${s.emoji || '🧩'}</div>
        <div class="skill-card-body">
          <div class="skill-card-name">${esc(s.name)}</div>
          <div class="skill-card-summary">${esc(s.description || '')}</div>
          ${need}
        </div>
        <div class="skill-card-action">${action}</div>
      </div>`;
    }).join('');
  } catch (_) {
    list.innerHTML = '<p class="empty-state">Couldn\'t load skills — check the VPS connection.</p>';
  }
}

function setupSkill(name, needs) {
  // Hand off to Claude — it knows how to install apt packages / set env vars intelligently
  closeModal('skills-modal');
  const msg = `Set up the "${name}" skill on the VPS. It needs: ${needs}. `
            + `Install the missing requirement(s) so the skill becomes available, then confirm it's ready.`;
  if (!chatWs || chatWs.readyState !== WebSocket.OPEN || sending) {
    alert('Claude is busy or disconnected — try again in a moment.');
    return;
  }
  lastUserMessage = msg;
  addBubble('you', msg);
  sending = true;
  document.getElementById('chat-send').disabled = true;
  chatWs.send(JSON.stringify({ type: 'user_message', content: msg }));
}

async function restartAgent(btn) {
  btn.disabled = true; btn.textContent = '🔄 Restarting…';
  try {
    const data = await (await fetch('/api/skills/restart-agent', { method: 'POST' })).json();
    if (data.ok) {
      btn.textContent = '✓ Agent restarted';
      setTimeout(() => { document.getElementById('skills-restart-banner').style.display = 'none'; }, 2500);
    } else {
      btn.disabled = false; btn.textContent = '🔄 Retry restart';
      alert('Restart may have failed:\n\n' + (data.output || '').slice(-400));
    }
  } catch (_) {
    btn.disabled = false; btn.textContent = '🔄 Retry restart';
  }
}

async function runSkillsSearch() {
  const q    = document.getElementById('skills-search-input').value.trim();
  const list = document.getElementById('skills-results');
  list.innerHTML = '<p class="empty-state">Searching ClawHub…</p>';
  try {
    const results = await (await fetch('/api/skills/search?q=' + encodeURIComponent(q))).json();
    if (!results.length) {
      list.innerHTML = '<p class="empty-state">No skills found. Try a different search.</p>';
      return;
    }
    list.innerHTML = results.map(s => `
      <div class="skill-card" id="skill-${esc(s.slug)}">
        <div class="skill-card-body">
          <div class="skill-card-name">${esc(s.name)}
            ${s.owner ? `<span class="skill-card-owner">by ${esc(s.owner)}</span>` : ''}
          </div>
          <div class="skill-card-summary">${esc(s.summary || '')}</div>
        </div>
        <div class="skill-card-action">
          ${s.installed
            ? '<span class="skill-installed">✓ Installed</span>'
            : `<button class="btn-primary skill-install-btn" onclick="installSkill('${esc(s.slug)}', this)">Install</button>`}
        </div>
      </div>`).join('');
  } catch (_) {
    list.innerHTML = '<p class="empty-state">Couldn\'t reach the catalog — is the VPS connected?</p>';
  }
}

async function installSkill(slug, btn) {
  btn.disabled = true; btn.textContent = 'Installing…';
  try {
    const res = await fetch('/api/skills/install', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug }),
    });
    const data = await res.json();
    const action = btn.closest('.skill-card-action');
    if (data.ok) {
      action.innerHTML = '<span class="skill-installed">✓ Installed</span>';
      // Prompt to restart the agent so the new skill activates
      const banner = document.getElementById('skills-restart-banner');
      banner.style.display = 'flex';
      const rb = banner.querySelector('button');
      rb.disabled = false; rb.textContent = '🔄 Restart agent';
    } else {
      btn.disabled = false; btn.textContent = 'Retry';
      alert('Install may have failed:\n\n' + (data.output || 'unknown error').slice(-400));
    }
  } catch (_) {
    btn.disabled = false; btn.textContent = 'Retry';
    alert('Install request failed — check the VPS connection.');
  }
}

// ── Memory modal ──────────────────────────────────────────────────────────────
async function openMemoryModal() {
  openModal('memory-modal');
  loadMemory();
}

async function loadMemory() {
  const facts = await (await fetch('/api/memory')).json();
  const list  = document.getElementById('memory-list');
  if (!facts.length) {
    list.innerHTML = '<p class="empty-state">Nothing remembered yet. Claude will add facts as it learns about your setup.</p>';
    return;
  }
  list.innerHTML = `<table class="cred-table">
    <thead><tr><th>Key</th><th>Value</th><th>Category</th><th></th></tr></thead>
    <tbody>${facts.map(m => `
      <tr>
        <td><span class="cred-name">${esc(m.key)}</span></td>
        <td>${esc(m.value)}</td>
        <td><span class="cred-user">${esc(m.category)}</span></td>
        <td><button class="cred-btn danger" onclick="deleteMemory('${m.id}')">Delete</button></td>
      </tr>`).join('')}
    </tbody></table>`;
}

function showMemoryForm() {
  document.getElementById('mf-key').value = '';
  document.getElementById('mf-value').value = '';
  document.getElementById('mf-cat').value = '';
  document.getElementById('memory-form-wrap').style.display = '';
}
function hideMemoryForm() { document.getElementById('memory-form-wrap').style.display = 'none'; }

async function saveMemory() {
  const key   = document.getElementById('mf-key').value.trim();
  const value = document.getElementById('mf-value').value.trim();
  const cat   = document.getElementById('mf-cat').value.trim() || 'general';
  if (!key || !value) { alert('Key and value are required'); return; }
  const res = await fetch('/api/memory', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key, value, category: cat }),
  });
  if (res.ok) { hideMemoryForm(); loadMemory(); }
  else alert('Save failed');
}

async function deleteMemory(id) {
  await fetch(`/api/memory/${id}`, { method: 'DELETE' });
  loadMemory();
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
  document.getElementById('skills-search-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); runSkillsSearch(); }
  });

  // Check setup state — show onboarding wizard for new users
  await checkOnboarding();

  // Set initial panel visibility and agent subtitle
  applyTerminalsState(false);
  applyConsoleState(false);
  updateAgentSubtitle();

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
