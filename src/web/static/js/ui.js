// Small shared pieces: escaping, the notice banner, busy buttons, pills.
export const $ = selector => document.querySelector(selector);
export const $$ = selector => [...document.querySelectorAll(selector)];
export const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
export const main = () => $('#main');
export const form = el => Object.fromEntries(new FormData(el).entries());

export function notice(message, error = false) {
  const banner = $('#banner');
  banner.textContent = message;
  banner.className = error ? 'banner error' : 'banner';
  banner.hidden = !message;
  clearTimeout(notice.timer);
  if (message) notice.timer = setTimeout(() => { banner.hidden = true; }, error ? 12000 : 5000);
}

// Runs a button's action: busy while it works, the outcome in the banner.
export async function act(button, work, done = 'Done.') {
  button.disabled = true;
  const label = button.textContent;
  button.innerHTML = `<span class="spinner"></span> ${esc(label)}`;
  try {
    const result = await work();
    if (done) notice(typeof done === 'function' ? done(result) : done);
    return result;
  } catch (error) {
    notice(error.message, true);
    return null;
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

export const pill = (text, kind) => `<span class="pill ${esc(kind)}">${esc(text)}</span>`;
export const levelPill = level => pill({ ok: 'OK', warn: 'Attention', fail: 'Problem' }[level] || level, level);
export const when = epoch => (epoch ? new Date(epoch * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '—');
export const iso = text => (text ? new Date(text).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '—');
export const daysLeft = epoch => (epoch ? Math.floor((epoch * 1000 - Date.now()) / 86400000) : null);
export const size = bytes => (bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : bytes > 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1e3))} kB`);

export function certPill(epoch) {
  const days = daysLeft(epoch);
  if (days === null) return pill('no certificate', 'fail');
  return pill(`certificate ${days} days`, days > 30 ? 'ok' : days > 0 ? 'warn' : 'fail');
}

export function uptimePill(uptime) {
  if (!uptime || uptime.up === null || uptime.up === undefined) return '';
  if (!uptime.up) return pill('down', 'fail');
  const week = uptime.week;
  return pill(`${week === null ? '—' : week >= 99.95 ? '100' : week.toFixed(1)}% up 7 days`, week >= 99 ? 'ok' : 'warn');
}

export const checksList = checks =>
  `<ul class="checks">${checks.map(c => `<li>${levelPill(c.level)}<span class="name">${esc(c.name)}</span><span class="subtle">${esc(c.detail)}</span></li>`).join('')}</ul>`;

// Tabs inside a page: sections marked data-panel; the last tab chosen per
// page kind is remembered for this browser tab (re-renders keep it).
export const tabBar = tabs => `<nav class="tabs" role="tablist">${tabs.map(([key, label]) => `<button type="button" class="tab" role="tab" data-tab="${key}">${esc(label)}</button>`).join('')}</nav>`;

export function bindTabs(kind) {
  let chosen = null;
  try { chosen = sessionStorage.getItem(`tab:${kind}`); } catch { /* storage blocked */ }
  const show = key => {
    const exists = document.querySelector(`[data-panel="${key}"]`);
    key = exists ? key : document.querySelector('[data-panel]')?.dataset.panel;
    $$('[data-panel]').forEach(p => { p.hidden = p.dataset.panel !== key; });
    $$('.tabs [data-tab]').forEach(t => { t.classList.toggle('active', t.dataset.tab === key); t.setAttribute('aria-selected', t.dataset.tab === key); });
    try { sessionStorage.setItem(`tab:${kind}`, key); } catch { /* storage blocked */ }
  };
  $$('.tabs [data-tab]').forEach(t => { t.onclick = () => show(t.dataset.tab); });
  show(chosen);
}

export const crumbs = (href, label) => `<a class="back" href="${href}">← ${esc(label)}</a>`;

export function ago(epoch) {
  if (!epoch) return '—';
  const seconds = Math.round(Date.now() / 1000 - epoch);
  if (seconds < 90) return 'just now';
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 129600) return `${Math.round(seconds / 3600)} h ago`;
  return when(epoch);
}

// <button data-copy="text">: copies on click (handled once for the whole page).
export const copyButton = text => `<button type="button" class="quiet small" data-copy="${esc(text)}">Copy</button>`;
if (typeof document !== 'undefined') {
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-copy]');
    if (button) copy(button.dataset.copy);
  });
}

export async function copy(text) {
  try { await navigator.clipboard.writeText(text); notice('Copied.'); } catch { notice('Copy it from the box.'); }
}
