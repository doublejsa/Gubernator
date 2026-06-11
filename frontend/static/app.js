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
  const terminals = document.getElementById('right-panel');
  const feed      = document.getElementById('whats-happening');
  const btn       = document.getElementById('terminals-toggle-btn');

  if (terminalsVisible) {
    terminals.style.display = '';
    feed.style.display      = 'none';
    btn.classList.add('active');
  } else {
    terminals.style.display = 'none';
    feed.style.display      = '';
    btn.classList.remove('active');
  }
  // Reflow after CSS transition so xterm sizes correctly
  setTimeout(reflowTerminals, animate ? 320 : 0);
}

function toggleTerminals() {
  terminalsVisible = !terminalsVisible;
  localStorage.setItem('gov_terminals', terminalsVisible ? 'open' : 'closed');
  applyTerminalsState(true);
}

// ── Sidebar collapse + Ideas ──────────────────────────────────────────────────
function applySidebarState() {
  const collapsed = localStorage.getItem('gov_sidebar') === 'collapsed';
  document.getElementById('sidebar').classList.toggle('collapsed', collapsed);
}
function toggleSidebar() {
  const sb = document.getElementById('sidebar');
  const collapsed = sb.classList.toggle('collapsed');
  localStorage.setItem('gov_sidebar', collapsed ? 'collapsed' : 'open');
  setTimeout(reflowTerminals, 240);
}

// ── Mobile: drawer + bottom-tab view switching ────────────────────────────────
function isMobile() { return window.matchMedia('(max-width: 768px)').matches; }

function openDrawer()  {
  document.getElementById('sidebar').classList.add('open');
  document.getElementById('drawer-backdrop').classList.add('open');
}
function closeDrawer() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('drawer-backdrop').classList.remove('open');
}
function toggleDrawer() {
  document.getElementById('sidebar').classList.contains('open') ? closeDrawer() : openDrawer();
}

function switchMobileView(view) {
  document.getElementById('app').dataset.mview = view;
  document.querySelectorAll('.mtab').forEach(t =>
    t.classList.toggle('active', t.dataset.mview === view));
  closeDrawer();
  // A terminal panel that was display:none has no measurable size; fit it a
  // couple of times once it's visible and laid out so xterm renders.
  if (view === 'agent' || view === 'console') {
    const fit = view === 'agent' ? tuiFit : shellFit;
    [80, 250, 500].forEach(d => setTimeout(() => {
      try { fit && fit.fit(); } catch (_) {}
      reflowTerminals();
    }, d));
  }
}

async function loadIdeas() {
  try {
    const ideas = await (await fetch('/api/suggestions')).json();
    const list  = document.getElementById('ideas-list');
    list.innerHTML = ideas.map((it, i) => `
      <button class="idea-card" onclick="runIdea(${i})" title="${esc(it.title)}">
        <span class="idea-card-ico">${it.icon}</span>
        <span class="idea-card-title">${esc(it.title)}</span>
      </button>`).join('');
    window._ideas = ideas;   // stash prompts for runIdea
  } catch (_) {}
}

function runIdea(i) {
  const idea = (window._ideas || [])[i];
  if (!idea) return;
  if (!chatWs || chatWs.readyState !== WebSocket.OPEN || sending) {
    alert('Claude is busy or disconnected — try again in a moment.');
    return;
  }
  lastUserMessage = idea.prompt;
  addBubble('you', idea.prompt);
  sending = true;
  document.getElementById('chat-send').disabled = true;
  chatWs.send(JSON.stringify({ type: 'user_message', content: idea.prompt }));
}

// ── "What's happening" feed ───────────────────────────────────────────────────
let feedStatusEl  = null;   // single transient entry for thinking/browsing
const feedByAction = {};    // action_id → feed entry element

function feedEl() { return document.getElementById('feed-list'); }

function feedAdd(icon, text, state) {
  const list = feedEl();
  const empty = list.querySelector('.feed-empty');
  if (empty) empty.remove();
  const el = document.createElement('div');
  el.className = `feed-item ${state || 'info'}`;
  const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  el.innerHTML = `<span class="feed-ico">${icon}</span>
    <span class="feed-text">${esc(text)}</span>
    <span class="feed-time">${time}</span>`;
  list.appendChild(el);
  list.scrollTop = list.scrollHeight;
  return el;
}

function feedSet(el, icon, text, state) {
  if (!el) return;
  el.className = `feed-item ${state || 'info'}`;
  el.querySelector('.feed-ico').textContent  = icon;
  if (text !== undefined) el.querySelector('.feed-text').textContent = text;
}

function feedTransient(icon, text, state) {
  // One rolling entry for live agent status (thinking / browsing)
  if (!feedStatusEl || !feedStatusEl.isConnected) {
    feedStatusEl = feedAdd(icon, text, state || 'pending');
  } else {
    feedSet(feedStatusEl, icon, text, state || 'pending');
    feedEl().appendChild(feedStatusEl);   // move to bottom
    feedEl().scrollTop = feedEl().scrollHeight;
  }
}

function feedClearTransient() {
  if (feedStatusEl && feedStatusEl.isConnected) feedStatusEl.remove();
  feedStatusEl = null;
}

function clearFeed() {
  feedEl().innerHTML = '<div class="feed-empty">Activity will appear here as your agent works.</div>';
  feedStatusEl = null;
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
  const emoji = STATUS_EMOJI[code] || '🟢';
  const pill = document.getElementById('agent-status-pill');
  if (pill) { pill.dataset.status = code || 'ready'; pill.textContent = `${emoji} ${label || 'Agent is ready'}`; }
  // Mirror to the mobile top-bar pill (icon only)
  const mpill = document.getElementById('mobile-status-pill');
  if (mpill) { mpill.dataset.status = code || 'ready'; mpill.textContent = emoji; }
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

let pendingCheck = false;   // run a check once the current (stuck) turn unlocks

function checkAgent() {
  if (!chatWs || chatWs.readyState !== WebSocket.OPEN) return;
  if (sending) {
    // Busy or stuck ("Agent is thinking" that won't clear) — interrupt whatever
    // is running, then auto-run the check the moment the UI unlocks.
    pendingCheck = true;
    chatWs.send(JSON.stringify({ type: 'cancel_tui' }));
    chatWs.send(JSON.stringify({ type: 'cancel_claude' }));
    addBubble('status', '⛔ Stopping… will check the agent once it stops.');
    return;
  }
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
    return renderMarkdown(part);
  }).join('');
}

// Lightweight, safe markdown → HTML for non-code text (escape first, then format)
function renderMarkdown(text) {
  if (!text) return '';
  const lines = text.split('\n');
  let html = '', listType = null;   // 'ul' | 'ol' | null

  const closeList = () => { if (listType) { html += `</${listType}>`; listType = null; } };
  const inline = (s) => {
    s = esc(s);
    // inline code first so its contents aren't further formatted
    s = s.replace(/`([^`]+)`/g, '<code class="md-code">$1</code>');
    // links [text](url)
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
    // bold then italic
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    s = s.replace(/\b_([^_\n]+)_\b/g, '<em>$1</em>');
    return s;
  };

  for (let raw of lines) {
    const line = raw.replace(/\s+$/, '');
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {           // headings
      closeList();
      const lvl = m[1].length;
      html += `<div class="md-h md-h${lvl}">${inline(m[2])}</div>`;
    } else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {      // bullet list
      if (listType !== 'ul') { closeList(); html += '<ul class="md-list">'; listType = 'ul'; }
      html += `<li>${inline(m[1])}</li>`;
    } else if ((m = line.match(/^\s*\d+\.\s+(.*)$/))) {     // numbered list
      if (listType !== 'ol') { closeList(); html += '<ol class="md-list">'; listType = 'ol'; }
      html += `<li>${inline(m[1])}</li>`;
    } else if (line.trim() === '') {                        // blank line
      closeList();
      html += '<div class="md-gap"></div>';
    } else {                                                // normal text
      closeList();
      html += `<div class="md-p">${inline(line)}</div>`;
    }
  }
  closeList();
  return `<span class="msg-text md">${html}</span>`;
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

// ── Friendly error messages ───────────────────────────────────────────────────
const ERROR_PATTERNS = [
  [/connection refused/i,                                  "Can't reach your server",        "Your server refused the connection — it may be powered off, or a firewall/port is blocking it."],
  [/nodename nor servname|name or service not known|getaddrinfo|could not resolve/i, "Server address not found", "The server address couldn't be looked up — check for typos in the host or IP."],
  [/timed?\s?out|timeout/i,                                "Your server didn't respond in time", "The server took too long to answer. It might be busy or temporarily unreachable."],
  [/permission denied|authentication failed|auth fail/i,   "Login was refused",              "The username or password may be incorrect."],
  [/no space left/i,                                       "Your server is out of disk space", "The disk is full — free up some space and try again."],
  [/command not found/i,                                   "A required tool is missing",     "The server doesn't have a program needed for this step. Ask Claude to install it."],
  [/host key|known_hosts/i,                                "Server identity changed",        "The server's SSH fingerprint changed — this can happen after a rebuild."],
  [/rate limit/i,                                          "Too many requests just now",     "We briefly hit a rate limit. Wait a moment and try again."],
];

function friendlyError(raw) {
  const r = String(raw || '');
  for (const [re, title, body] of ERROR_PATTERNS) if (re.test(r)) return { title, body, raw: r };
  return { title: "Something went wrong", body: "An unexpected error occurred. You can try again, or ask Claude to look into it.", raw: r };
}

function addFriendlyError(raw) {
  const f  = friendlyError(raw);
  const el = document.createElement('div');
  el.className = 'bubble friendly-error';
  el.innerHTML = `
    <div class="ferr-title">⚠️ ${esc(f.title)}</div>
    <div class="ferr-body">${esc(f.body)}</div>
    <div class="ferr-actions">
      <button class="retry-btn" onclick="retryLastMessage(this)">↩ Try again</button>
      <button class="ferr-help-btn" data-raw="${esc(f.raw)}" onclick="askClaudeAboutError(this)">💬 Ask Claude</button>
    </div>
    ${f.raw ? `<button class="ferr-toggle" onclick="toggleErrDetails(this)">technical details ▾</button>
               <pre class="ferr-raw" style="display:none">${esc(f.raw)}</pre>` : ''}`;
  document.getElementById('chat-messages').appendChild(el);
  scrollChat();
  feedAdd('✗', f.title, 'error');
}

function addLoopBanner(count) {
  document.querySelectorAll('.loop-banner').forEach(el => el.remove());
  const el = document.createElement('div');
  el.className = 'bubble loop-banner';
  el.innerHTML = `
    <div class="loop-title">🔁 Gubernator noticed this is looping${count ? ` (tried ~${count}×)` : ''}</div>
    <div class="loop-body">The same approach keeps failing. For a repeatable job like this, the fix is
      to build a <strong>reliable script</strong> that works first-time, every time, instead of retrying by hand.</div>
    <button class="retry-btn" onclick="buildTool()" style="margin-top:8px">🔧 Build the tool now</button>`;
  document.getElementById('chat-messages').appendChild(el);
  scrollChat();
}

function buildTool() {
  if (!chatWs || chatWs.readyState !== WebSocket.OPEN || sending) {
    alert('Claude is busy or disconnected — try again in a moment.');
    return;
  }
  const prompt =
    "Stop the current approach. Follow the 'build a tool when stuck' playbook to turn what we're "
    + "working on into a reliable, repeatable tool:\n"
    + "1. Run read-only commands to discover the specifics (paths, branch names, git config, where "
    + "secrets live) and [REMEMBER] each fact.\n"
    + "2. Set up any auth ONCE (e.g. bake the GitHub PAT into the git remote or use a credential "
    + "helper) so it stops failing — never handle the token per-run.\n"
    + "3. Write a single idempotent script via [VPS_WRITE] with clean JSON output, one sub-command "
    + "per step.\n"
    + "4. Wrap it in a thin SKILL.md, register it, restart the agent, and [REMEMBER] how to run it.\n"
    + "Work through it one step at a time.";
  lastUserMessage = prompt;
  addBubble('you', '🔧 Build a reliable tool for this');
  document.querySelectorAll('.loop-banner').forEach(el => el.remove());
  sending = true;
  document.getElementById('chat-send').disabled = true;
  chatWs.send(JSON.stringify({ type: 'user_message', content: prompt }));
}

function toggleErrDetails(btn) {
  const pre = btn.nextElementSibling;
  const open = pre.style.display !== 'none';
  pre.style.display = open ? 'none' : 'block';
  btn.textContent = open ? 'technical details ▾' : 'technical details ▴';
}

function askClaudeAboutError(btn) {
  const raw = btn.getAttribute('data-raw') || '';
  if (sending || !chatWs || chatWs.readyState !== WebSocket.OPEN) return;
  const prompt = `I hit this error and don't understand it. In plain English, what does it mean and how do I fix it?\n\nError: ${raw}`;
  lastUserMessage = prompt;
  addBubble('you', 'Help me understand that error.');
  sending = true; document.getElementById('chat-send').disabled = true;
  chatWs.send(JSON.stringify({ type: 'user_message', content: prompt }));
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
      case 'status':      addBubble('status', msg.message); feedAdd('•', msg.message, 'info'); break;
      case 'tui_thinking': {
        const st = msg.status || { code: 'thinking', label: 'Agent is thinking…' };
        updateAgentStatus(st.code, st.label);
        feedTransient(STATUS_EMOJI[st.code] || '🟡', `${st.label} (${msg.elapsed}s)`, 'pending');
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
        // A Check-agent request was queued while busy — run it now that we're unlocked
        if (pendingCheck) { pendingCheck = false; setTimeout(checkAgent, 120); }
        // Claude said it's waiting on the agent — surface a manual check button
        else if (msg.awaiting_agent) addCheckAgentPrompt();
        break;
      case 'stats':  updateClaudeStats(msg); break;
      case 'vps_output':
        addVpsOutputBubble(msg.cmd, msg.output); maybeShowTuiHint(msg.output); fetchOpenClawStats(); break;
      case 'tui_output':
        if (tuiThinkingBubble) { tuiThinkingBubble.remove(); tuiThinkingBubble = null; }
        removeThinkingBubble();
        document.getElementById('tui-cancel-btn').style.display = 'none';
        updateAgentStatus('ready', 'Agent is ready');
        feedClearTransient();
        feedAdd('🦞', 'Agent replied', 'done');
        addTuiOutputBubble(msg.cmd, msg.output); maybeShowTuiHint(msg.output); break;

      case 'agent_status':
        updateAgentStatus(msg.code, msg.label);
        if (msg.code === 'ready') feedClearTransient();
        else feedTransient(STATUS_EMOJI[msg.code] || '🟡', msg.label || 'Working…', 'pending');
        break;
      case 'loop_detected':
        addLoopBanner(msg.count || 0);
        feedAdd('🔁', 'Loop detected — switching to a more reliable approach', 'pending');
        break;
      case 'tasks_updated':
        if (document.getElementById('activity-modal').style.display === 'flex') loadActivity();
        break;
      case 'memory_updated':
        if (document.getElementById('memory-modal').style.display === 'flex') loadMemory();
        break;
      case 'action': {
        addActionBubble(msg);
        const d = msg.desc || {};
        feedByAction[msg.id] = feedAdd(d.icon || '⚙️', d.headline || 'Action proposed', 'info');
        break;
      }
      case 'action_done':
        dismissTuiHint(); resolveAction(msg.action_id, 'done', `✓ ${msg.label}`);
        feedSet(feedByAction[msg.action_id], '✓', undefined, 'done');
        document.getElementById('claude-cancel-btn').style.display = ''; break;
      case 'action_error':
        resolveAction(msg.action_id, 'error-state', `✗ ${msg.message}`);
        feedSet(feedByAction[msg.action_id], '✗', undefined, 'error');
        break;
      case 'error':
        removeThinkingBubble();
        feedClearTransient();
        if (currentClaudeBubble) { currentClaudeBubble.remove(); currentClaudeBubble = null; }
        if (msg.subtype === 'credits_exhausted') addCreditsBubble();
        else if (msg.subtype === 'rate_limit')   addRateLimitBubble();
        else if (msg.subtype === 'no_api_key')   addNoApiKeyBubble();
        else if (msg.subtype === 'auth_expired') { localStorage.removeItem('gov_token'); location.href = '/'; }
        else if (msg.subtype === 'subscription_required') { showPaywall(); }
        else addFriendlyError(msg.message || 'Unknown error');
        sending = false; setActionButtonsDisabled(false);
        document.getElementById('chat-send').disabled = false; break;
    }
  };
}

// ── Modal helpers ─────────────────────────────────────────────────────────────
function openModal(id)  { document.getElementById(id).style.display = 'flex'; if (typeof closeDrawer === 'function') closeDrawer(); }
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
// ── Audit log modal ───────────────────────────────────────────────────────────
const AUDIT_ICON = { vps_cmd: '💻', vps_write: '📝', tui_input: '🤖' };

async function openAuditModal() {
  openModal('audit-modal');
  loadAudit();
}

async function loadAudit() {
  const list = document.getElementById('audit-list');
  list.innerHTML = '<p class="empty-state">Loading…</p>';
  try {
    const rows = await (await fetch('/api/audit')).json();
    if (!rows.length) {
      list.innerHTML = '<p class="empty-state">No commands recorded yet.</p>';
      return;
    }
    list.innerHTML = rows.map(r => {
      const icon = AUDIT_ICON[r.action_type] || '•';
      const when = new Date(r.at).toLocaleString();
      const dot  = r.status === 'ok' ? '✓' : (r.status === 'failed' ? '✗' : '⛔');
      const detail = r.action_type === 'vps_write'
        ? `${esc(r.path)}${r.content_hash ? ` · sha256:${esc(r.content_hash)}` : ''}`
        : esc(r.command || '');
      const out = r.output ? `<pre class="audit-out">${esc(r.output)}</pre>` : '';
      return `<div class="audit-row audit-${r.status}">
        <span class="audit-ico">${icon}</span>
        <div class="audit-body">
          <div class="audit-headline">${dot} ${esc(r.headline)}</div>
          ${detail ? `<code class="audit-cmd">${detail}</code>` : ''}
          ${out}
          <div class="audit-when">${esc(r.vps_host)} · ${when}</div>
        </div>
      </div>`;
    }).join('');
  } catch (_) {
    list.innerHTML = '<p class="empty-state">Couldn\'t load the audit log.</p>';
  }
}

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
  loadBillingSettings();
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

async function changePassword() {
  const cur = document.getElementById('acc-cur').value;
  const nw  = document.getElementById('acc-new').value;
  const st  = document.getElementById('acc-status');
  st.style.color = 'var(--dim)';
  if (!cur || !nw) { st.textContent = 'Enter both fields.'; st.style.color = 'var(--orange)'; return; }
  if (nw.length < 8) { st.textContent = 'New password must be at least 8 characters.'; st.style.color = 'var(--orange)'; return; }
  const res = await fetch('/api/auth/change-password', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ current_password: cur, new_password: nw }),
  });
  if (res.ok) {
    document.getElementById('acc-cur').value = '';
    document.getElementById('acc-new').value = '';
    st.textContent = '✓ Password changed — please sign in again.';
    st.style.color = 'var(--green)';
    setTimeout(() => { localStorage.removeItem('gov_token'); location.href = '/'; }, 1500);
  } else {
    const d = await res.json().catch(() => ({}));
    st.textContent = d.detail || 'Could not change password.';
    st.style.color = 'var(--red)';
  }
}

async function deleteAccount() {
  if (!confirm('Permanently delete your account and all data? This cannot be undone.')) return;
  const pw = prompt('Confirm your password to delete your account:');
  if (!pw) return;
  const res = await fetch('/api/auth/account', {
    method: 'DELETE', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password: pw }),
  });
  if (res.ok) {
    localStorage.removeItem('gov_token');
    alert('Your account has been deleted.');
    location.href = '/';
  } else {
    const d = await res.json().catch(() => ({}));
    alert(d.detail || 'Could not delete account.');
  }
}

// ── Billing / paywall ─────────────────────────────────────────────────────────
let _billing = null;
let _paypalSDKLoaded = false;

async function fetchBilling() {
  try { _billing = await (await fetch('/api/billing')).json(); } catch (_) { _billing = null; }
  return _billing;
}

function loadPaypalSDK(clientId) {
  return new Promise((resolve, reject) => {
    if (_paypalSDKLoaded && window.paypal) return resolve();
    const s = document.createElement('script');
    s.src = `https://www.paypal.com/sdk/js?client-id=${encodeURIComponent(clientId)}&vault=true&intent=subscription`;
    s.onload = () => { _paypalSDKLoaded = true; resolve(); };
    s.onerror = reject;
    document.head.appendChild(s);
  });
}

async function showPaywall() {
  const b = await fetchBilling();
  if (!b) return;
  // Paywall takes precedence over everything else
  const ob = document.getElementById('onboarding'); if (ob) ob.style.display = 'none';
  const pw = document.getElementById('paywall');
  pw.style.display = 'flex';
  pw.style.zIndex = '3000';

  const isReturning = ['past_due', 'expired', 'cancelled'].includes(b.status);
  const days = b.trial_days || 14;
  // First-charge date = trial_ends_at if known, else today + trial days
  const chargeDate = b.trial_ends_at ? new Date(b.trial_ends_at)
                                     : new Date(Date.now() + days * 86400000);
  const fmt = chargeDate.toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' });
  const dateEl = document.getElementById('pw-charge-date');
  if (dateEl) dateEl.textContent = fmt;

  if (isReturning) {
    // Returning/lapsed user — no "free trial" framing, straight resubscribe
    document.getElementById('pw-title').textContent    = 'Resume Gubernator';
    document.getElementById('pw-headline').textContent = '$29/month';
    document.getElementById('pw-subhead').textContent  =
      b.status === 'past_due' ? 'Your last payment failed — reactivate to continue.'
                              : 'Your subscription ended — reactivate to continue.';
    ['pw-timeline', 'pw-zero', 'pw-benefits'].forEach(id => { const e = document.getElementById(id); if (e) e.style.display = 'none'; });
    document.getElementById('pw-choose').textContent = 'Choose how to pay:';
    document.getElementById('pw-under').textContent  = 'Your subscription resumes immediately. Cancel anytime.';
  }

  if (!b.plan_id) {
    document.getElementById('paywall-status').textContent =
      '⚠ Billing not configured yet (no plan). Contact support.';
    return;
  }
  try {
    await loadPaypalSDK(b.paypal_client_id);
    document.getElementById('paypal-button-container').innerHTML = '';
    window.paypal.Buttons({
      style: { layout: 'vertical', color: 'blue', shape: 'pill', label: 'subscribe' },
      createSubscription: (data, actions) => actions.subscription.create({ plan_id: b.plan_id }),
      onApprove: async (data) => {
        document.getElementById('paywall-status').textContent = 'Activating…';
        const res = await fetch('/api/billing/subscribe', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ subscription_id: data.subscriptionID }),
        });
        if (res.ok) { document.getElementById('paywall-status').textContent = '✓ Subscribed!'; setTimeout(() => location.reload(), 1200); }
        else document.getElementById('paywall-status').textContent = 'Could not activate — contact support.';
      },
      onError: () => { document.getElementById('paywall-status').textContent = 'PayPal error — please try again.'; },
    }).render('#paypal-button-container');
  } catch (_) {
    document.getElementById('paywall-status').textContent = 'Could not load PayPal. Check your connection.';
  }
}

async function cancelSubscription() {
  if (!confirm('Cancel your subscription? You keep access until the end of the paid period.')) return;
  const res = await fetch('/api/billing/cancel', { method: 'POST' });
  if (res.ok) { alert('Subscription cancelled.'); loadBillingSettings(); }
  else { const d = await res.json().catch(()=>({})); alert(d.detail || 'Cancellation failed.'); }
}

async function loadBillingSettings() {
  const b = await fetchBilling();
  const el = document.getElementById('billing-settings');
  if (!el || !b) return;
  const labels = { none:'No subscription', trialing:'Free trial', active:'Active',
                   cancelled:'Cancelled', expired:'Expired', past_due:'Payment failed' };
  let html = `<div style="font-size:13px;margin-bottom:6px">Status: <strong>${labels[b.status]||b.status}</strong></div>`;
  if (b.status === 'trialing' && b.trial_ends_at)
    html += `<div style="font-size:12px;color:var(--dim)">Trial ends ${new Date(b.trial_ends_at).toLocaleDateString()}</div>`;
  if (b.entitled && (b.status === 'active' || b.status === 'trialing'))
    html += `<button class="cred-btn danger" style="margin-top:10px" onclick="cancelSubscription()">Cancel subscription</button>`;
  else
    html += `<button class="btn-primary" style="margin-top:10px" onclick="showPaywall()">Subscribe</button>`;
  el.innerHTML = html;
}

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  await checkAuth();
  // Gate on subscription before anything else — paywall takes over the whole
  // screen and nothing else (onboarding, terminals) runs until they subscribe.
  const b = await fetchBilling();
  if (b && !b.entitled) { showPaywall(); return; }

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
  applySidebarState();
  updateAgentSubtitle();
  loadIdeas();

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
