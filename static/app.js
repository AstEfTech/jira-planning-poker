const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const store = {
  get: k => { try { return localStorage.getItem(k) } catch { return null } },
  set: (k, v) => { try { localStorage.setItem(k, v) } catch { } },
};
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() :
  'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => { const r = Math.random() * 16 | 0; return (c == 'x' ? r : r & 3 | 8).toString(16) }));

const clientId = store.get('cid') || uuid();
store.set('cid', clientId);

/* ---------- theme ---------- */
function applyTheme(t) { document.documentElement.dataset.theme = t; store.set('theme', t); }
applyTheme(store.get('theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
$('#themeBtn').onclick = () => applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');

function show(id) {
  for (const s of ['landing', 'join', 'room']) $('#' + s).hidden = s !== id;
  if (id === 'join' && !$('#joinBtn').hidden) $('#nameIn').focus();
}
function toast(text, level = 'info') {
  const el = document.createElement('div');
  el.className = 'toast ' + level; el.textContent = text;
  $('#toasts').append(el); setTimeout(() => el.remove(), 4500);
}

const m = location.pathname.match(/^\/([0-9a-f-]{36})\/?$/i);
const roomId = m && m[1].toLowerCase();
let config = { decks: [], jira: false };

fetch('/api/config').then(r => r.json()).then(c => { config = c; roomId ? initRoom() : initLanding(); });

/* ---------- landing ---------- */
function initLanding() {
  show('landing');
  $('#deckSel').value = config.decks[0];
  combo($('#deckSel'), () => config.decks.map(d => ({ id: d, label: d })), it => { $('#deckSel').value = it.id; });
  $('#createBtn').onclick = () => createRoom($('#deckSel').value, $('#landPw').value, $('#landOwn').checked, $('#landRev').checked);
}

async function createRoom(deck, password, ownerOnlyTasks = false, ownerOnlyReveal = false) {
  const r = await fetch('/api/rooms', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ clientId, deck, password, ownerOnlyTasks, ownerOnlyReveal }),
  });
  const { id } = await r.json();
  if (password) store.set('pw:' + id, password);
  location.href = '/' + id;
}

/* ---------- room ---------- */
let ws, S = null, myVote = null, me = null, isOwner = false, joined = false, wantJoin = false;
let hostSig = '', lastRoundKey = '', prevRevealed = false, flipUntil = 0, tableSig = '';

function initRoom() {
  $('#nameIn').value = store.get('name') || '';
  $('#obsIn').checked = store.get('obs') === '1';
  $('#pwIn').value = store.get('pw:' + roomId) || '';
  $('#joinBtn').onclick = doJoin;
  $('#nameIn').onkeydown = $('#pwIn').onkeydown = e => { if (e.key === 'Enter') doJoin(); };
  if ($('#nameIn').value) doJoin(); else show('join');
}

function doJoin() {
  const name = $('#nameIn').value.trim();
  if (!name) return $('#nameIn').focus();
  store.set('name', name); store.set('obs', $('#obsIn').checked ? '1' : '0');
  $('#nameBtn').textContent = '✎ ' + name;
  wantJoin = true; $('#joinMsg').textContent = '';
  connect();
}

function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

function connect() {
  if (ws) { ws.onclose = null; ws.close(); }
  ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/' + roomId);
  ws.onopen = () => send({
    type: 'join', clientId, name: $('#nameIn').value.trim(),
    password: $('#pwIn').value, observer: $('#obsIn').checked,
  });
  ws.onmessage = e => onMsg(JSON.parse(e.data));
  ws.onclose = () => { if (wantJoin) setTimeout(connect, 1500); };
}

function onMsg(msg) {
  if (msg.type === 'error') {
    wantJoin = false; joined = false; show('join');
    if (msg.code === 'not_found') {
      $('#joinMsg').textContent = 'Room not found (it may have expired).';
      $('#joinBtn').hidden = true;
      $('#newRoomBtn').hidden = false;
      $('#newRoomBtn').onclick = () => createRoom(config.decks[0], '');
    } else {
      $('#pwRow').hidden = false;
      $('#joinMsg').textContent = msg.code === 'bad_password' ? 'Wrong password.' : 'This room is password protected.';
      $('#pwIn').focus();
    }
  } else if (msg.type === 'joined') {
    joined = true; show('room');
    store.set('pw:' + roomId, $('#pwIn').value);
    $('#copyBtn').hidden = false; $('#obsBtn').hidden = false; $('#nameBtn').hidden = false;
    $('#jira').hidden = !config.jira;
  } else if (msg.type === 'jira_meta') {
    projects = msg.projects;
    const key = (S && S.project) || store.get('jproject');
    if (key && projects.some(p => p.key === key)) pickProject(key, true);
  } else if (msg.type === 'jira_fields') {
    if (msg.project !== projKey) return;
    allFields = msg.allFields; numFields = msg.fields;
    // keep the chosen estimate field only if the project has it; otherwise preselect the usual suspect
    if (!numFields.some(f => f.id === estId)) {
      const sp = numFields.find(f => /story point/i.test(f.name));
      setEst(sp ? sp.id : '');
    } else setEst(estId);
  } else if (msg.type === 'state') {
    S = msg.room; me = msg.me; myVote = msg.myVote; render();
  } else if (msg.type === 'countdown') startCountdown(msg.n, msg.ms);
  else if (msg.type === 'throw') flyEmoji(msg);
  else if (msg.type === 'toast') toast(msg.text, msg.level);
}

/* ---------- actions ---------- */
$('#copyBtn').onclick = async () => {
  try { await navigator.clipboard.writeText(location.href); toast('Link copied', 'ok'); }
  catch { prompt('Copy link:', location.href); }
};
/* in-page replacement for prompt() (often blocked in embedded browsers) */
function ask(title, value = '', type = 'text') {
  return new Promise(res => {
    const dlg = $('#dlg'), inp = $('#dlgIn');
    $('#dlgTitle').textContent = title; inp.type = type; inp.value = value;
    let result = null;
    const done = v => { result = v; dlg.close(); };
    $('#dlgForm').onsubmit = e => { e.preventDefault(); done(inp.value); };
    $('#dlgCancel').onclick = () => done(null);
    dlg.onclose = () => res(result);
    dlg.showModal(); inp.focus(); inp.select();
  });
}
$('#nameBtn').onclick = async () => {
  const n = await ask('Your name', $('#nameIn').value);
  if (n === null || !n.trim()) return;
  $('#nameIn').value = n.trim().slice(0, 30);
  doJoin();
};
function savePassword() {
  const p = $('#roomPw').value;
  send({ type: 'set_password', password: p });
  store.set('pw:' + roomId, p); $('#pwIn').value = p;
}
$('#roomPwSave').onclick = savePassword;
$('#roomPw').onkeydown = e => { if (e.key === 'Enter') savePassword(); };
$('#hostList').onclick = e => { const b = e.target.closest('button[data-id]'); if (b) send({ type: b.dataset.act, to: b.dataset.id }); };
const sendOptions = () => send({ type: 'set_options', ownerOnlyTasks: $('#optTasks').checked, ownerOnlyReveal: $('#optReveal').checked });
$('#optTasks').onchange = $('#optReveal').onchange = sendOptions;
$('#obsBtn').onclick = () => { $('#obsIn').checked = !$('#obsIn').checked; store.set('obs', $('#obsIn').checked ? '1' : '0'); send({ type: 'observer', value: $('#obsIn').checked }); };
$('#revealBtn').onclick = () => send({ type: 'reveal' });
let counting = false;
function startCountdown(n, total = 2000) {
  const el = $('#countdown');
  counting = true; $('#revealBtn').disabled = true; $('#table').classList.add('counting');
  const step = total / n;  // the server picks the total (2-2.5 s)
  el.style.setProperty('--step', step + 'ms');
  const tick = () => {
    if (n < 1) { el.hidden = true; counting = false; $('#table').classList.remove('counting'); if (S) render(); return; }
    el.hidden = false; el.innerHTML = `<span>${n}</span>`; n--; setTimeout(tick, step);
  };
  tick();
}
$('#resetBtn').onclick = () => send({ type: 'reset' });
let allFields = [], numFields = [], estId = '', projects = [], projKey = '';
const fieldName = id => (numFields.concat(allFields).find(f => f.id === id) || {}).name || '';
function setEst(id) { estId = id || ''; $('#fieldIn').value = fieldName(estId) || estId; }
function resolveEst() {
  const v = $('#fieldIn').value.trim(), l = v.toLowerCase();
  if (estId && v === (fieldName(estId) || estId)) return estId;
  const f = numFields.find(f => f.id.toLowerCase() === l || f.name.toLowerCase() === l);
  return f ? f.id : v;
}
const setExtra = list => send({ type: 'set_extra', extra: list });

/* themed dropdown for text inputs (a native <datalist> can't be styled) */
function combo(input, getItems, onPick) {
  const wrap = document.createElement('div'); wrap.className = 'combo';
  input.replaceWith(wrap); wrap.append(input);
  const list = document.createElement('div'); list.className = 'combo-list'; list.hidden = true;
  wrap.append(list);
  let items = [], active = -1;
  const close = () => { list.hidden = true; active = -1; };
  const mark = () => list.querySelectorAll('.opt').forEach((el, i) => el.classList.toggle('act', i === active));
  const pick = i => { if (items[i]) { onPick(items[i]); close(); } };
  function open(showAll) {
    const q = showAll === true ? '' : input.value.trim().toLowerCase();
    const all = getItems();
    items = all.filter(it => !q || (it.label + ' ' + it.id + ' ' + (it.tags || []).join(' ')).toLowerCase().includes(q)).slice(0, 60);
    active = items.length ? 0 : -1;
    list.innerHTML = items.length ? items.map((it, i) =>
      `<div class="opt" data-i="${i}"><span class="lbl">${esc(it.label)}</span>${(it.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join('')}</div>`).join('')
      : `<div class="none">${esc(all.length ? 'Nothing found' : (getItems.empty || 'Nothing to choose from'))}</div>`;
    list.hidden = false; mark();
  }
  list.onmousedown = e => { e.preventDefault(); const o = e.target.closest('.opt'); if (o) pick(+o.dataset.i); };
  input.addEventListener('focus', () => { open(true); input.select(); });
  input.addEventListener('input', () => open());
  input.addEventListener('click', () => { if (list.hidden) open(true); });
  input.addEventListener('blur', close);
  input.addEventListener('keydown', e => {
    if (e.key === 'Escape') return close();
    if (list.hidden && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) return open(true);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault(); if (!items.length) return;
      active = (active + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length; mark();
      list.querySelector('.act')?.scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter' && !list.hidden && active >= 0) { e.preventDefault(); pick(active); }
  });
}

// every field of the selected project that isn't shown yet
const availFields = () => {
  const cur = (S && S.extra) || [];
  return allFields.filter(f => !cur.includes(f.id)).map(f => ({ id: f.id, label: f.name }));
};
availFields.empty = 'Pick a project first';
combo($('#extraIn'), availFields, it => { $('#extraIn').value = ''; setExtra([...(S.extra || []), it.id]); });
combo($('#fieldIn'), () => numFields.map(f => ({ id: f.id, label: f.name })), it => setEst(it.id));
$('#fieldIn').addEventListener('input', () => { estId = resolveEst(); });

const defaultJql = key => `project = "${key}" AND resolution = Unresolved ORDER BY Rank`;
/* settings can be folded away to give the issue list more room */
const setCfgFolded = f => { $('#jira').classList.toggle('folded', f); $('#cfgToggle').textContent = f ? '⚙ Settings ▸' : '⚙ Settings ▾'; };
$('#cfgToggle').onclick = () => setCfgFolded(!$('#jira').classList.contains('folded'));
setCfgFolded(false);

/* Jira metadata is fetched lazily, on the owner's first interaction with the Jira settings */
let metaAsked = false;
function ensureMeta() { if (metaAsked || !isOwner || !config.jira) return; metaAsked = true; send({ type: 'jira_meta' }); }
$('#jiraCfg').addEventListener('focusin', ensureMeta);
$('#jiraCfg').addEventListener('pointerdown', ensureMeta);
const projLabel = p => `${p.name} (${p.key})`;
combo($('#projectSel'), () => projects.map(p => ({ id: p.key, label: projLabel(p) })), it => pickProject(it.id));
$('#projectSel').addEventListener('change', e => {
  const v = e.target.value.trim().toLowerCase();
  const p = projects.find(p => p.key.toLowerCase() === v || p.name.toLowerCase() === v || projLabel(p).toLowerCase() === v);
  if (p) { if (p.key !== projKey) pickProject(p.key); else e.target.value = projLabel(p); }
  else { const cur = projects.find(p => p.key === projKey); e.target.value = cur ? projLabel(cur) : ''; }
});
function pickProject(key, restore) {
  const p = projects.find(p => p.key === key);
  projKey = key; $('#projectSel').value = p ? projLabel(p) : key; store.set('jproject', key);
  allFields = []; numFields = [];
  if (!key) return;
  send({ type: 'jira_project', key });
  if (!restore || !$('#jqlIn').value.trim()) $('#jqlIn').value = defaultJql(key);
}
$('#loadBtn').onclick = () => {
  const jql = $('#jqlIn').value.trim(), field = resolveEst();
  store.set('jql', jql); store.set('jfield', field);
  send({ type: 'jira_load', jql, field, project: projKey });
};
$('#jqlIn').onkeydown = e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) $('#loadBtn').click(); };
$('#pushVal').addEventListener('input', e => { e.target.value = e.target.value.replace(/[^0-9.,]/g, ''); });
$('#pushBtn').onclick = () => send({ type: 'jira_push', value: $('#pushVal').value });

/* ---------- throwing emoji at colleagues ---------- */
const EMOJI = ['🍅', '👋', '⏰', '🎈', '🎉', '🚀', '🐢', '💤', '❤️', '👀'];
const pop = $('#emojiPop');
pop.innerHTML = EMOJI.map(e => `<button type="button">${e}</button>`).join('');
let popTarget = null, popTimer = null;
const hidePop = () => { pop.hidden = true; popTarget = null; };
const schedHide = () => { clearTimeout(popTimer); popTimer = setTimeout(hidePop, 250); };
function showPop(pl) {
  clearTimeout(popTimer);
  if (!pl || pl.dataset.id === me) return hidePop();
  popTarget = pl.dataset.id;
  const r = pl.querySelector('.pcard').getBoundingClientRect();
  pop.hidden = false;
  const w = pop.offsetWidth, h = pop.offsetHeight;
  const below = r.top < h + 70;
  pop.style.left = Math.max(8, Math.min(innerWidth - w - 8, r.left + r.width / 2 - w / 2)) + 'px';
  pop.style.top = (below ? r.bottom + 6 : r.top - h - 6) + 'px';
}
$('#table').addEventListener('mouseover', e => { const pl = e.target.closest('.player'); if (pl && pl.dataset.id !== popTarget) showPop(pl); else if (pl) clearTimeout(popTimer); });
$('#table').addEventListener('click', e => { const pl = e.target.closest('.player'); if (pl) showPop(pl); });
for (const el of [$('#table'), pop]) el.addEventListener('mouseout', e => { if (!(e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest('.player, #emojiPop'))) schedHide(); });
pop.addEventListener('mouseover', () => clearTimeout(popTimer));
pop.addEventListener('click', e => {
  const b = e.target.closest('button');
  if (b && popTarget) send({ type: 'throw', to: popTarget, emoji: b.textContent });
});
addEventListener('scroll', hidePop, true);

function flyEmoji(msg) {
  const card = document.querySelector(`.player[data-id="${CSS.escape(msg.to)}"] .pcard`);
  if (!card) return;
  const r = card.getBoundingClientRect(), tx = r.left + r.width / 2, ty = r.top + r.height / 2;
  const side = Math.floor(Math.random() * 4);   // 0 left, 1 right, 2 top, 3 bottom
  const sx = side === 0 ? -50 : side === 1 ? innerWidth + 50 : Math.random() * innerWidth;
  const sy = side === 2 ? -50 : side === 3 ? innerHeight + 50 : Math.random() * innerHeight;
  const el = document.createElement('div'); el.className = 'fly'; el.textContent = msg.emoji; document.body.append(el);
  const spin = (Math.random() < .5 ? -1 : 1) * (360 + Math.random() * 360);
  const at = (x, y, rot, sc) => `translate(${x}px, ${y}px) translate(-50%, -50%) rotate(${rot}deg) scale(${sc})`;
  el.animate([{ transform: at(sx, sy, 0, .6) }, { transform: at(tx, ty, spin, 1.3) }],
    { duration: 650 + Math.random() * 250, easing: 'cubic-bezier(.3, .1, .6, 1)', fill: 'forwards' }).onfinish = () => { el.remove(); impact(tx, ty, msg); };
}
function impact(x, y, msg) {
  for (let i = 0; i < 7; i++) {
    const p = document.createElement('div'); p.className = 'fly small'; p.textContent = msg.emoji; document.body.append(p);
    const a = Math.random() * 6.28, d = 40 + Math.random() * 50;
    p.animate([{ transform: `translate(${x}px, ${y}px) translate(-50%, -50%) scale(.8)`, opacity: 1 },
      { transform: `translate(${x + Math.cos(a) * d}px, ${y + Math.sin(a) * d}px) translate(-50%, -50%) scale(.3)`, opacity: 0 }],
      { duration: 600, easing: 'ease-out' }).onfinish = () => p.remove();
  }
  const from = S && S.users.find(u => u.id === msg.from);
  if (from) {
    const c = document.createElement('div'); c.className = 'flytag'; c.textContent = from.name; document.body.append(c);
    c.animate([{ transform: `translate(${x}px, ${y - 30}px) translate(-50%, -50%)`, opacity: 1 }, { transform: `translate(${x}px, ${y - 80}px) translate(-50%, -50%)`, opacity: 0 }],
      { duration: 1100, easing: 'ease-out' }).onfinish = () => c.remove();
  }
  const card = document.querySelector(`.player[data-id="${CSS.escape(msg.to)}"] .pcard`);
  if (card) card.animate([{ translate: '0 0', rotate: '0deg' }, { translate: '-5px 0', rotate: '-5deg' }, { translate: '5px 0', rotate: '5deg' }, { translate: '-3px 0', rotate: '-2deg' }, { translate: '0 0', rotate: '0deg' }], { duration: 380 });
}

/* ---------- stats ---------- */
const num = v => { const n = parseFloat(String(v).replace('½', '0.5')); return isNaN(n) ? null : n; };

function stats() {
  const votes = S.users.filter(u => !u.observer && u.vote != null).map(u => u.vote);
  const nums = votes.map(num).filter(n => n !== null);
  const counts = {}; votes.forEach(v => counts[v] = (counts[v] || 0) + 1);
  const mode = Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0];
  const out = { votes, mode, consensus: votes.length > 1 && new Set(votes).size === 1, avg: null, median: null, suggest: mode };
  if (nums.length) {
    out.avg = nums.reduce((a, b) => a + b, 0) / nums.length;
    const s = [...nums].sort((a, b) => a - b), h = s.length >> 1;
    out.median = s.length % 2 ? s[h] : (s[h - 1] + s[h]) / 2;
    const cards = S.deck.filter(c => num(c) !== null);
    if (cards.length) out.suggest = cards.reduce((best, c) => Math.abs(num(c) - out.avg) <= Math.abs(num(best) - out.avg) ? c : best);
  }
  return out;
}

/* ---------- render ---------- */
function render() {
  const mine = S.users.find(u => u.id === me);
  isOwner = !!(mine && mine.host);
  $('#roomMenu').hidden = !isOwner;
  $('#cfgBox').hidden = !(isOwner && config.jira);
  $('#loadBtn').hidden = !(isOwner && config.jira);
  const hl = S.users.map(u => `<li><span>${esc(u.name)}${u.id === me ? ' (you)' : ''}</span>${u.host ? `<small>host</small>${u.id === S.ownerId ? '' : `<button type="button" data-act="remove_host" data-id="${esc(u.id)}">Remove host</button>`}` : `<button type="button" data-act="make_host" data-id="${esc(u.id)}">Make host</button>`}</li>`).join('');
  if (hl !== hostSig) { hostSig = hl; $('#hostList').innerHTML = hl; }
  // task
  const t = S.task;
  $('#taskView').innerHTML = !t ? '<small>No task selected — pick one from the Jira list</small>'
    : t.key ? `<a href="${esc(t.url)}" target="_blank" rel="noopener">${esc(t.key)}</a> ${esc(t.summary)} <small>${esc(t.status)}</small>`
      : esc(t.title || '');
  $('#reporter').hidden = !(t && t.reporter);
  $('#reporter').innerHTML = t && t.reporter ? `Reporter: <b>${esc(t.reporter)}</b>` : '';

  // description / comments / extra fields
  const d = t && t.details;
  $('#taskInfo').hidden = !(t && t.key);
  const canAdd = !!(isOwner && config.jira && t && t.key);
  $('#addField').hidden = !canAdd;
  $('#extraBox').hidden = !canAdd && !(d && d.extra && d.extra.length);
  if (t && t.key) {
    const nc = d && d.comments ? d.comments.length : 0;
    $('#descSum').textContent = 'More' + (d && !d.error ? ` (${nc} comment${nc === 1 ? '' : 's'})` : '');
    $('#descBody').innerHTML = !d ? '<small>Loading…</small>' : d.error ? `<p class="err">${esc(d.error)}</p>` :
      `<div class="desc">${d.description ? esc(d.description) : '<small>No description</small>'}</div>` +
      d.comments.map(c => `<div class="comment"><div class="chead"><b>${esc(c.author)}</b> <small>${esc(new Date(c.created).toLocaleString())}</small></div><div class="desc">${esc(c.body)}</div></div>`).join('');
  }
  if (d && d.extra) {
    const xs = [...d.extra.filter(x => x.value), ...d.extra.filter(x => !x.value)];
    $('#extraBody').innerHTML = xs.map(x => `<div class="ef${x.value ? '' : ' empty'}"><div class="efh"><span>${esc(x.name)}</span>${isOwner ? `<button type="button" data-id="${esc(x.id)}" title="Hide this field">×</button>` : ''}</div>${x.value ? `<div class="efv">${esc(x.value)}</div>` : ''}</div>`).join('');
    $('#extraBody').querySelectorAll('button').forEach(b => b.onclick = () => setExtra((S.extra || []).filter(x => x !== b.dataset.id)));
  }

  // table
  if (S.revealed && !prevRevealed) flipUntil = Date.now() + 1500;
  prevRevealed = S.revealed;
  const justRevealed = Date.now() < flipUntil;
  let ci = 0;
  const tableHtml = S.users.map(u => {
    let card;
    if (u.observer) card = '<div class="pcard obs">👁</div>';
    else if (S.revealed && u.vote != null) card = `<div class="pcard shown${justRevealed ? ' flip' : ''}" style="animation-delay:${(ci++ * 0.12).toFixed(2)}s"><span class="face front">${esc(u.vote)}</span><span class="face back"></span></div>`;
    else if (u.voted) card = '<div class="pcard voted"></div>';
    else card = '<div class="pcard"></div>';
    return `<div class="player ${u.online ? '' : 'off'}" data-id="${esc(u.id)}">${card}<div class="pname" title="${esc(u.name)}">${esc(u.name)}${u.id === me ? ' (you)' : ''}</div></div>`;
  }).join('');
  // rebuild only on real changes, so a running flip animation is not restarted by unrelated renders
  const sig = tableHtml.replace(/ flip/g, '');
  if (sig !== tableSig) { tableSig = sig; $('#table').innerHTML = tableHtml; }

  const voters = S.users.filter(u => !u.observer);
  $('#revealBtn').disabled = S.revealed || counting || (S.ownerOnlyReveal && !isOwner);
  $('#revealBtn').title = S.ownerOnlyReveal && !isOwner ? 'Only a host can reveal cards' : '';
  $('#optTasks').checked = !!S.ownerOnlyTasks; $('#optReveal').checked = !!S.ownerOnlyReveal;
  $('#revealBtn').textContent = `Reveal cards (${voters.filter(u => u.voted).length}/${voters.length})`;
  $('#obsBtn').classList.toggle('on', $('#obsIn').checked);

  // deck
  const observer = $('#obsIn').checked;
  $('#deck').hidden = observer;
  $('#obsHint').hidden = !observer;
  $('#deck').innerHTML = S.deck.map(c => `<button class="dcard ${c === myVote ? 'sel' : ''}" data-v="${esc(c)}">${esc(c)}</button>`).join('');
  $('#deck').querySelectorAll('button').forEach(b => b.onclick = () => send({ type: 'vote', value: b.dataset.v === myVote ? null : b.dataset.v }));

  // results
  const st = S.revealed ? stats() : null;
  $('#results').innerHTML = !st || !st.votes.length ? '' :
    (st.avg !== null ? `<div class="stat"><b>${+st.avg.toFixed(2)}</b><span>Average</span></div><div class="stat"><b>${+st.median.toFixed(2)}</b><span>Median</span></div>` : '') +
    `<div class="stat"><b>${esc(st.mode)}</b><span>Most common</span></div>` +
    (st.consensus ? '<div class="stat good"><b>✔</b><span>Consensus</span></div>' : '');

  // push box
  const roundKey = (t && (t.key || t.title) || '') + '|' + S.revealed;
  const canPush = isOwner && config.jira && t && t.key;
  $('#pushBox').hidden = !canPush;
  $('#pushLbl').textContent = S.field ? '→ ' + (fieldName(S.field) || S.field) : 'set the estimate field first';
  if (roundKey !== lastRoundKey) {
    lastRoundKey = roundKey;
    $('#pushVal').value = st && st.votes.length ? st.suggest : (t && t.estimate != null ? t.estimate : '');
  }

  // jira list
  if (isOwner && !projects.length && !metaAsked && S.project && document.activeElement !== $('#projectSel')) $('#projectSel').value = S.project;
  if (isOwner && projects.length && !projKey && (S.project || store.get('jproject'))) pickProject(S.project || store.get('jproject'), true);
  if (!$('#jqlIn').value && document.activeElement !== $('#jqlIn')) $('#jqlIn').value = S.jql || store.get('jql') || (projKey ? defaultJql(projKey) : '');
  if (!estId && document.activeElement !== $('#fieldIn')) setEst(S.field || store.get('jfield') || '');
  $('#taskList').innerHTML = (!S.tasks.length && !isOwner ? '<li class="empty">No host has loaded any issues yet</li>' : '') + S.tasks.map(x => `
    <li data-k="${esc(x.key)}" class="${t && t.key === x.key ? 'cur' : ''}">
      ${x.estimate != null ? `<span class="est">${esc(x.estimate)}</span>` : ''}
      <span class="k">${esc(x.key)}</span> <span class="st">${esc(x.status)}</span><br>${esc(x.summary)}
    </li>`).join('');
  const locked = !!S.ownerOnlyTasks && !isOwner;
  $('#taskList').classList.toggle('locked', locked);
  $('#taskList').title = locked ? 'Only a host can switch tasks' : '';
  $('#taskList').querySelectorAll('li[data-k]').forEach(li => li.onclick = () => { if (!locked) send({ type: 'task', key: li.dataset.k }); });
}
