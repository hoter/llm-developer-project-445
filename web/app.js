/* Чат-фронтенд (готовое): SSE-поток хода оркестратора + карточка действия.
 *
 * Оркестратор присылает события:
 *   {type:"plan", agents:[...]}            — план: какие доменные агенты пошли
 *   {type:"card", pending_id, card}        — карточка действия, ждём подтверждение
 *   {type:"reply", text, sources?}         — финальный ответ
 *   {type:"error", detail}                 — сбой хода
 *   {type:"done"}                          — ход завершён
 *
 * Подтверждение: POST /api/cards/{pending_id}/decision {"answer":"yes"|"no"}.
 */
const BACKEND = 'http://127.0.0.1:8000';

const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
// Ответ модели приходит в markdown: жирный и маркеры списка, остальное выводится как текст.
const md = s => esc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/^[-*] /gm, '• ');
const log = $('log');
let sessionId = null;
let busy = false;

function addMsg(cls, text, extra) {
  const div = document.createElement('div');
  div.className = `msg ${cls}`;
  if (cls === 'bot') div.innerHTML = md(text);
  else div.textContent = text;
  if (extra) {
    const src = document.createElement('span');
    src.className = 'src';
    src.textContent = extra;
    div.appendChild(src);
  }
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

function addCard(pendingId, card) {
  const div = document.createElement('div');
  div.className = 'card';
  const rows = Object.entries(card.rows || card)
    .filter(([k, v]) => typeof v !== 'object' && k !== 'action' && k !== 'ttl_sec')
    .map(([k, v]) => `<tr><td>${esc(k)}</td><td><b>${esc(v)}</b></td></tr>`).join('');
  div.innerHTML = `
    <h4>Требуется подтверждение</h4>
    <table>${rows}</table>
    <div class="ttl">истекает через ${esc(card.ttl_sec || 120)} с · действие: ${esc(card.action || '')}</div>
    <div class="btns">
      <button class="yes">Подтверждаю</button>
      <button>Отменить</button>
    </div>`;
  const [yes, no] = div.querySelectorAll('button');
  const decide = async answer => {
    [yes, no].forEach(b => b.disabled = true);
    div.classList.add('done');
    const r = await fetch(`${BACKEND}/api/cards/${pendingId}/decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, answer }),
    });
    const data = await r.json();
    addMsg('bot', data.text || `исход: ${data.outcome || data.detail || '?'}`);
  };
  yes.onclick = () => decide('yes');
  no.onclick = () => decide('no');
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

async function openSession() {
  const user = $('user').value;
  const r = await fetch(`${BACKEND}/api/session`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user }),
  });
  const data = await r.json();
  if (data.error) { addMsg('sys', `не удалось открыть сессию: ${data.error}`); return; }
  sessionId = data.session_id;
  $('session-state').textContent = `сессия ${sessionId}`;
  $('session-sub').textContent = `${data.name} · ${data.role}`;
  $('text').disabled = false;
  $('send').disabled = false;
  $('tools').innerHTML = `<b>Видимые инструменты (${(data.visible_tools || []).length})</b>` +
    (data.visible_tools || []).map(t => `<div class="t">${esc(t)}</div>`).join('');
  addMsg('sys', `сессия открыта для ${data.name} (${data.role})`);
}

async function send(text) {
  addMsg('user', text);
  busy = true; $('send').disabled = true;
  try {
    const r = await fetch(`${BACKEND}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, message: text }),
    });
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const line = chunk.split('\n').find(l => l.startsWith('data: '));
        if (!line) continue;
        handleEvent(JSON.parse(line.slice(6)));
      }
    }
  } catch (e) {
    addMsg('sys', `сеть: ${e}`);
  } finally {
    busy = false; $('send').disabled = false;
  }
}

function handleEvent(ev) {
  if (ev.type === 'plan') addMsg('sys', `план: ${ev.agents.join(' → ')}`);
  else if (ev.type === 'card') addCard(ev.pending_id, ev.card);
  else if (ev.type === 'reply') addMsg('bot', ev.text, ev.sources?.length ? `источники: ${ev.sources.join(', ')}` : null);
  else if (ev.type === 'error') addMsg('sys', `ошибка хода: ${ev.detail}`);
}

$('form').onsubmit = e => {
  e.preventDefault();
  if (busy || !sessionId) return;
  const text = $('text').value.trim();
  if (!text) return;
  $('text').value = '';
  send(text);
};
$('user').onchange = () => { location.reload(); };
if (sessionId === null) openSession();
