// Your account: password and appearance; administrators also name the server.
import { api, isAdmin, post, session } from './api.js';
import { ACCENTS, applyPrefs, prefsOf } from './prefs.js';
import { $, $$, act, esc, form, main, pill } from './ui.js';
import { bindLaikaAccount, laikaAccountCard } from './laika.js';

const choice = (name, value, options) => `<select name="${name}">${options.map(([v, label]) => `<option value="${v}" ${v === value ? 'selected' : ''}>${label}</option>`).join('')}</select>`;

export function accountPage() {
  const me = session.me;
  const p = prefsOf(me);
  main().innerHTML = `<div class="page-head"><h1>Your account</h1></div>
    <div class="card"><h2>Appearance</h2><p class="subtle">Only for you, on every device you sign in from.</p>
      <form class="form" id="prefs-form">
        <label class="field">Theme${choice('theme', p.theme, [['system', 'Follow this device'], ['dark', 'Dark'], ['light', 'Light']])}</label>
        <label class="field">Density${choice('density', p.density, [['comfortable', 'Comfortable'], ['compact', 'Compact']])}</label>
        <label class="field">Sites shown as${choice('sites_view', p.sites_view, [['cards', 'Cards'], ['table', 'Table']])}</label>
        <label class="field">Sites sorted by${choice('sites_sort', p.sites_sort, [['name', 'Name'], ['status', 'Problems first']])}</label>
        <div class="field">Accent<div class="swatches">${ACCENTS.map(c => `<label class="swatch-pick"><input type="radio" name="accent" value="${c}" ${c === p.accent ? 'checked' : ''}><span class="swatch" data-color="${c}"></span></label>`).join('')}</div></div>
        <button type="submit">Save</button></form></div>
    ${isAdmin() ? `<div class="card"><h2>This server</h2><form class="form" id="server-form"><label class="field">Name shown in the dashboard<input name="server_name" maxlength="40" value="${esc(me.settings?.server_name || 'LNMT')}"></label><button type="submit">Save</button></form></div>` : ''}
    ${me.hosting ? `<div class="card"><h2>SSH keys (git push and SFTP)</h2><p class="subtle">Your public keys, one per line (<code>ssh-ed25519 AAAA… you@laptop</code>). They let you push code and use SFTP on the hosting node from the LAN or the VPN; nothing else.</p><form class="form" id="keys-form"><label class="field wide"><textarea name="keys" rows="4" id="keys-text" placeholder="ssh-ed25519 AAAA…"></textarea></label><button type="submit">Save keys</button></form></div>` : ''}
    ${laikaAccountCard(me)}
    <div class="card" id="my-alerts"><h2>Your alerts</h2><p class="subtle">${isAdmin() ? 'Server-wide alerts are under System. Here: alerts only for you.' : 'Get a message when one of your sites or apps has a problem.'} Addresses are stored root-only and never shown again; leave a field empty to keep what is set.</p><div id="my-alerts-body" class="subtle">Loading…</div></div>
    <div class="card"><h2>Password</h2><p class="subtle">Signed in as <b>${esc(me.username)}</b> (${esc(me.role)}).</p>
      <form class="form stack" id="pw-form"><label class="field">Current password<input name="current" type="password" autocomplete="current-password" required></label>
      <label class="field">New password (10+)<input name="password" type="password" autocomplete="new-password" required></label><button type="submit">Change password</button></form></div>`;
  $$('.swatch').forEach(s => s.style.setProperty('background', s.dataset.color));
  const prefsForm = $('#prefs-form');
  prefsForm.onchange = () => applyPrefs({ ...p, ...form(prefsForm) }, me.settings);  // preview at once
  prefsForm.onsubmit = async e => {
    e.preventDefault();
    const r = await act(prefsForm.querySelector('button'), () => post('me/prefs', form(prefsForm)), 'Saved.');
    if (r) { me.prefs = r.prefs; applyPrefs(prefsOf(me), me.settings); }
  };
  const server = $('#server-form');
  if (server) server.onsubmit = async e => {
    e.preventDefault();
    const r = await act(server.querySelector('button'), () => post('settings', form(server)), 'Saved.');
    if (r) { me.settings = r; applyPrefs(prefsOf(me), me.settings); }
  };
  const keys = $('#keys-form');
  if (keys) {
    api('me/ssh-keys').then(r => { $('#keys-text').value = r.keys.join('\n'); }).catch(() => {});
    keys.onsubmit = async e => { e.preventDefault(); await act(keys.querySelector('button'), () => post('me/ssh-keys', { keys: form(keys).keys.split('\n').map(k => k.trim()).filter(Boolean) }), r => `${r.keys.length} key(s) saved.`); };
  }
  loadMyAlerts();
  bindLaikaAccount();
  $('#pw-form').onsubmit = async e => { e.preventDefault(); if (await act(e.target.querySelector('button'), () => post('me/password', form(e.target)), 'Password changed; your other sessions were signed out.')) e.target.reset(); };
}

async function loadMyAlerts() {
  const body = $('#my-alerts-body');
  let alerts;
  try { alerts = await api('me/alerts'); } catch (error) { body.textContent = error.message; return; }
  const target = (key, label, placeholder) => `<div class="target"><div class="spread"><b>${label}</b>${alerts[key] ? pill('set', 'ok') : pill('not set', 'off')}</div>
    <input name="${key === 'discord' ? 'discord_webhook' : 'ntfy_url'}" type="password" autocomplete="off" placeholder="${alerts[key] ? 'replace…' : placeholder}">
    ${alerts[key] ? `<label class="subtle"><input type="checkbox" name="remove_${key}" value="1"> stop sending to ${label}</label>` : ''}</div>`;
  body.classList.remove('subtle');
  body.innerHTML = `<form id="my-alerts-form"><div class="alert-targets">${target('discord', 'Discord', 'https://discord.com/api/webhooks/…')}${target('ntfy', 'ntfy', 'https://ntfy.sh/your-topic')}</div>
    <h3>Tell me when</h3><div class="checkbox-list">${Object.entries(alerts.labels).map(([k, label]) => `<label><input type="checkbox" name="event:${k}" value="1" ${alerts.events[k] ? 'checked' : ''}> ${esc(label)}</label>`).join('')}</div>
    <div class="row mt"><button type="submit">Save</button><button type="button" class="quiet" id="my-alerts-test">Send a test</button></div></form>`;
  const f = $('#my-alerts-form');
  f.onsubmit = async e => {
    e.preventDefault();
    const d = form(f);
    const data = { events: Object.fromEntries(Object.keys(alerts.labels).map(k => [k, Boolean(d[`event:${k}`])])) };
    if (d.discord_webhook) data.discord_webhook = d.discord_webhook;
    if (d.ntfy_url) data.ntfy_url = d.ntfy_url;
    if (d.remove_discord) data.discord_webhook = '';
    if (d.remove_ntfy) data.ntfy_url = '';
    if (await act(f.querySelector('button[type=submit]'), () => post('me/alerts', data), 'Saved.')) loadMyAlerts();
  };
  $('#my-alerts-test').onclick = e => act(e.target, () => post('me/alerts/test'), r => `Sent to ${r.sent} place(s).`);
}
