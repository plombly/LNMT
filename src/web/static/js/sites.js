// The site list (cards or a table, sorted as the person prefers) and adding a site.
import { api, isAdmin, post, session } from './api.js';
import { prefsOf } from './prefs.js';
import { $, act, certPill, esc, form, iso, main, pill, uptimePill } from './ui.js';

const statusRank = s => (s.enabled ? 0 : 1) + (s.ddns?.status && s.ddns.status !== 'HEALTHY' ? -2 : 0) + (s.uptime?.up === false ? -4 : 0);

function badges(s) {
  return `${s.enabled ? uptimePill(s.uptime) : ''}${certPill(s.cert_expires)}${s.ddns?.status ? pill(`DNS ${s.ddns.status.toLowerCase()}`, s.ddns.status === 'HEALTHY' ? 'ok' : 'warn') : ''}${s.auth_enabled ? pill('password protected', 'ok') : ''}${s.level === 'view' ? pill('view only', 'off') : ''}`;
}

export async function sitesPage() {
  const data = await api('sites');
  const prefs = prefsOf(session.me);
  const sites = [...data.sites].sort((a, b) => (prefs.sites_sort === 'status' ? statusRank(a) - statusRank(b) : 0) || a.hostname.localeCompare(b.hostname));
  const link = s => `#/sites/${encodeURIComponent(s.hostname)}`;
  const state = s => (s.enabled ? pill('live', 'ok') : pill('disabled', 'off'));
  const block = group => (prefs.sites_view === 'table'
    ? `<div class="card table-wrap"><table><thead><tr><th>Site</th><th>State</th><th>Backend</th><th></th></tr></thead><tbody>${group.map(s => `<tr><td><a href="${link(s)}">${esc(s.hostname)}</a></td><td>${state(s)}</td><td>${esc(s.backend)}</td><td><div class="row">${badges(s)}</div></td></tr>`).join('')}</tbody></table></div>`
    : `<div class="grid">${group.map(s => `<a class="card site-card" href="${link(s)}"><div class="spread"><div class="name">${esc(s.hostname)}</div>${state(s)}</div>
        <div class="subtle">→ ${esc(s.backend)}</div><div class="row mt">${badges(s)}</div></a>`).join('')}</div>`);
  // Sites grouped under their domain (the domain is the parent of its subdomains).
  const zones = [...new Set(sites.map(s => s.zone || ''))].sort();
  const list = zones.map(z => `<div class="zone-group"><h2 class="zone-head">${isAdmin() && z ? `<a href="#/domains/${encodeURIComponent(z)}">${esc(z)}</a>` : esc(z || 'other')}</h2>${block(sites.filter(s => (s.zone || '') === z))}</div>`).join('');
  const ddns = data.ddns || {};
  const down = sites.filter(s => s.enabled && s.uptime?.up === false).length;
  const off = sites.filter(s => !s.enabled).length;
  const summary = sites.length ? `<p class="summary">${pill(`${sites.length - off - down} live`, 'ok')}${down ? pill(`${down} down`, 'fail') : ''}${off ? pill(`${off} disabled`, 'off') : ''}</p>` : '';
  main().innerHTML = `<div class="page-head"><h1>Sites</h1>${isAdmin() ? '<a class="button" href="#/add">Add a site</a>' : ''}</div>${summary}
    ${isAdmin() && ddns.public_ip ? `<p class="subtle">Public IP ${esc(ddns.public_ip)} · DNS last checked ${esc(iso(ddns.last_check))} · ${esc(ddns.message || '')}</p>` : ''}
    ${sites.length ? list : `<div class="empty">${isAdmin() ? 'No sites yet. Add the first one.' : 'No sites are shared with you yet.'}</div>`}`;
}

export async function addPage(zone = '') {
  if (!isAdmin()) { location.hash = '#/sites'; return; }
  const creds = (await api('credentials')).credentials;
  main().innerHTML = `<div class="page-head"><h1>Add a site</h1></div>
    <div class="card"><p class="subtle">For safety, the manager checks the hostname's Cloudflare zone, refuses to overwrite an existing DNS record, makes sure the zone's Origin certificate covers the name, tests the backend, then creates a proxied DNS record and the nginx site in one step that is rolled back if anything fails. Share the site with people afterwards on its page.</p>
    ${creds.length ? `<p class="subtle">Zones with a Cloudflare token: ${creds.map(c => `<code>${esc(c.zone)}</code>`).join(', ')}</p>` : '<p>No Cloudflare token yet: <a href="#/domains">add one</a> for the zone first.</p>'}
    <form class="form" id="add-form">
      <label class="field">Public hostname<input name="hostname" placeholder="${zone ? `app.${esc(zone)}` : 'app.example.com'}" value="${zone ? `.${esc(zone)}` : ''}" required></label>
      <label class="field">Backend IPv4 (LAN)<input name="backend_ip" placeholder="192.0.2.20" required></label>
      <label class="field">Port<input name="backend_port" type="number" min="1" max="65535" placeholder="3000" required class="narrow"></label>
      <label class="field">Protocol<select name="backend_protocol"><option value="http">http</option><option value="https">https</option></select></label>
      <label class="field"><span><input type="checkbox" name="force" value="1"> Deploy even if the backend does not answer</span></label>
      <button type="submit">Deploy</button></form></div>`;
  const el = $('#add-form');
  if (zone) { el.hostname.focus(); el.hostname.setSelectionRange(0, 0); }
  el.addEventListener('submit', async event => {
    event.preventDefault();
    const data = form(el);
    data.force = Boolean(data.force);
    const result = await act(el.querySelector('button'), () => post('sites', data), r => `Deployed ${r.hostname}.${r.backend_warning ? ` Backend warning: ${r.backend_warning}` : ''}`);
    if (result) location.hash = `#/sites/${encodeURIComponent(result.hostname)}`;
  });
}
