// One site: health, backend, backend TLS, password protection, who can see
// and manage it (administrators), its nginx configuration, removal.
import { api, del, isAdmin, post } from './api.js';
import { bindChart, chartCard } from './charts.js';
import { $, $$, act, ago, bindTabs, certPill, checksList, crumbs, esc, form, iso, main, notice, pill, tabBar, uptimePill, when } from './ui.js';

const redirectLines = list => list.map(r => `${r.from} ${r.to}${r.code !== 301 ? ` ${r.code}` : ''}`).join('\n');

function parseRedirects(text) {
  return text.split('\n').map(l => l.trim()).filter(l => l && !l.startsWith('#')).map(line => {
    const [from, to, code] = line.split(/\s+/);
    if (!to) throw new Error(`"${line}": write the old path, a space, then where it goes.`);
    return { from, to, code: Number(code || 301) };
  });
}

function optionsCard(o, manage) {
  const off = manage ? '' : 'disabled';
  return `<div class="card"><div class="spread"><h2>Options</h2>${o.maintenance ? pill('maintenance on', 'warn') : ''}</div>
    <form id="options-form" class="stack-form">
      <label class="check"><input type="checkbox" name="security_headers" ${o.security_headers ? 'checked' : ''} ${off}> <span><b>Security headers</b><br><span class="subtle">HSTS, no MIME sniffing, no framing by other sites, a strict referrer. Turn off if the site must be shown inside another site's frame.</span></span></label>
      <label class="check"><input type="checkbox" name="friendly_errors" ${o.friendly_errors ? 'checked' : ''} ${off}> <span><b>Friendly error page</b><br><span class="subtle">Visitors see "we'll be right back" instead of nginx's 502 when the backend is down.</span></span></label>
      <label class="field">Rate limit per visitor<select name="rate_limit" ${off}><option value="0">Off</option>${o.rate_limits.map(r => `<option value="${r}" ${o.rate_limit === r ? 'selected' : ''}>${r} requests a second (bursts to ${r * 2})</option>`).join('')}</select></label>
      <label class="field wide"><span>Redirects, one per line: <code>/old-path /new-path</code> or <code>/old https://elsewhere.com/page 302</code> (301 if no code)</span><textarea name="redirects" rows="3" class="code-area" placeholder="/blog https://blog.example.com" ${off}>${esc(redirectLines(o.redirects))}</textarea></label>
      <label class="check"><input type="checkbox" name="maintenance" ${o.maintenance ? 'checked' : ''} ${off}> <span><b>Maintenance mode</b><br><span class="subtle">Every visitor gets the page below (HTTP 503) and the backend is left alone. Turn off to bring the site back.</span></span></label>
      <details><summary class="subtle">Maintenance page (HTML)</summary><textarea name="maintenance_page" rows="8" class="code-area mt" ${off}>${esc(o.maintenance_page)}</textarea></details>
      ${manage ? '<div class="row"><button type="submit">Save options</button></div>' : ''}</form></div>`;
}

function peopleCard(people) {
  const members = people.filter(p => p.role !== 'admin');
  const admins = people.filter(p => p.role === 'admin').map(p => esc(p.username)).join(', ');
  const rows = members.filter(p => p.level).map(p => `<tr><td>${esc(p.username)}${p.disabled ? ' ' + pill('disabled', 'off') : ''}</td>
    <td><select data-level="${esc(p.username)}"><option value="view" ${p.level === 'view' ? 'selected' : ''}>view</option><option value="manage" ${p.level === 'manage' ? 'selected' : ''}>manage</option></select></td>
    <td class="actions"><button type="button" class="danger" data-revoke="${esc(p.username)}">Remove access</button></td></tr>`).join('');
  const others = members.filter(p => !p.level);
  return `<div class="card"><h2>People</h2><p class="subtle">Administrators (${admins || 'none'}) can do everything. Members see only the sites shared with them here: <b>view</b> (status, health, configuration) or <b>manage</b> (also enable/disable, backend and password protection).</p>
    ${rows ? `<table><thead><tr><th>Member</th><th>Access</th><th></th></tr></thead><tbody>${rows}</tbody></table>` : '<p class="subtle">Not shared with any member.</p>'}
    ${others.length ? `<form class="form mt" id="share-form"><label class="field">Share with<select name="username">${others.map(p => `<option>${esc(p.username)}</option>`).join('')}</select></label>
      <label class="field">Access<select name="level"><option value="view">view</option><option value="manage">manage</option></select></label><button type="submit">Share</button></form>`
      : '<p class="subtle mt">Every member already has access, or there are no members yet (<a href="#/users">invite someone</a>).</p>'}</div>`;
}

export async function sitePage(host) {
  const path = `sites/${encodeURIComponent(host)}`;
  const [s, people] = await Promise.all([api(path), isAdmin() ? api(`${path}/people`) : null]);
  const manage = s.level === 'manage';
  const https = s.backend_protocol === 'https';
  main().innerHTML = `${crumbs('#/sites', 'Sites')}<div class="page-head"><div><h1>${esc(s.hostname)}</h1><p class="subtle">Zone ${esc(s.zone)} · <a href="https://${esc(s.hostname)}" target="_blank" rel="noopener">open site</a></p></div>
      <div class="row">${s.enabled ? pill('live', 'ok') : pill('disabled', 'off')}${manage ? `<button type="button" id="toggle">${s.enabled ? 'Disable' : 'Enable'}</button>` : ''}</div></div>
    ${tabBar([['overview', 'Overview'], ['access', 'Access'], ['advanced', 'Advanced']])}<section data-panel="overview">
    <div class="card"><div class="spread"><h2>Health</h2><div class="row"><button type="button" class="quiet" id="test">Test backend</button><button type="button" id="health">Run full check</button></div></div>
      <div id="health-out"><dl class="facts"><dt>Backend</dt><dd>${esc(s.backend)}</dd>
      <dt>Uptime</dt><dd>${s.uptime?.up === null || !s.uptime ? '<span class="subtle">no checks yet (every 2 minutes)</span>' : `${uptimePill(s.uptime)} <span class="subtle">24 hours ${esc(s.uptime.day ?? '—')}% · checked ${esc(ago(s.uptime.last))}${s.uptime.up ? '' : ` · ${esc(s.uptime.message)}`}</span>`}</dd><dt>Certificate</dt><dd>${certPill(s.cert_expires)} <span class="subtle">${esc(when(s.cert_expires))}</span></dd>
      <dt>DNS (DDNS)</dt><dd>${esc(s.ddns?.status || '—')} <span class="subtle">${esc(s.ddns?.message || '')} ${s.ddns?.last_check ? '· ' + esc(iso(s.ddns.last_check)) : ''}</span></dd></dl></div></div>
    <div class="card"><h2>Backend</h2>${manage ? `<form class="form" id="backend-form">
      <label class="field">IPv4<input name="backend_ip" value="${esc(s.backend_ip)}" required></label>
      <label class="field">Port<input name="backend_port" type="number" min="1" max="65535" value="${esc(s.backend_port)}" class="narrow" required></label>
      <label class="field">Protocol<select name="backend_protocol"><option ${https ? '' : 'selected'}>http</option><option ${https ? 'selected' : ''}>https</option></select></label>
      <button type="submit">Save</button></form><p class="subtle mt">The new backend is tested first; nginx is checked before it reloads, and the old setting comes back on any failure.</p>` : `<p>${esc(s.backend)}</p>`}
      ${https ? `<h3>Backend TLS</h3>${manage ? `<form class="form" id="tls-form"><label class="field"><span><input type="checkbox" name="verify" value="1" ${s.backend_tls_verify ? 'checked' : ''}> Verify the backend's certificate</span></label>
        <label class="field">SNI name (optional)<input name="sni" value="${esc(s.backend_tls_sni)}" placeholder="backend.internal"></label><button type="submit">Save</button></form>` : `<p class="subtle">${s.backend_tls_verify ? 'Verified' : 'Not verified'}${s.backend_tls_sni ? ` · SNI ${esc(s.backend_tls_sni)}` : ''}</p>`}` : ''}</div>
    ${chartCard('traffic', 'Traffic', 'Requests reaching this site through the proxy (visitors blocked by Cloudflare never get here).')}
    </section><section data-panel="access" hidden>
    <div class="card"><div class="spread"><h2>Password protection</h2>${s.auth_enabled ? pill('on', 'ok') : pill('off', 'off')}</div>
      <p class="subtle">Visitors must sign in with one of these users before reaching the site (HTTP Basic Auth). These are separate from dashboard accounts.</p>
      ${manage ? `<form class="form" id="auth-form"><label class="field">Login prompt<input name="realm" value="${esc(s.auth_realm)}"></label>
        <button type="submit" class="quiet">Save prompt</button><button type="button" id="auth-toggle">${s.auth_enabled ? 'Turn off' : 'Turn on'}</button></form>` : ''}
      <h3>Users</h3>${s.auth_users.length ? `<table><tbody>${s.auth_users.map(u => `<tr><td>${esc(u)}</td><td class="actions">${manage ? `<button type="button" class="danger" data-remove-user="${esc(u)}">Remove</button>` : ''}</td></tr>`).join('')}</tbody></table>` : '<p class="subtle">No users yet.</p>'}
      ${manage ? `<form class="form mt" id="user-form"><label class="field">User<input name="username" required></label><label class="field">Password (8+)<input name="password" type="password" autocomplete="new-password" required></label><button type="submit">Add or change user</button></form>` : ''}</div>
    ${people ? peopleCard(people.people) : ''}
    </section><section data-panel="advanced" hidden>
    ${optionsCard(s.options, manage)}
    <div class="card"><details><summary><h2 class="inline">nginx configuration</h2></summary><pre class="mt">${esc(s.config || '(missing)')}</pre></details></div>
    ${isAdmin() ? `<div class="card"><h2>Remove</h2><p class="subtle">Removes the nginx site, its DDNS registration and the DNS record this manager created, and everyone's access to it. The Cloudflare token and the certificate stay.</p>
      <form class="form" id="remove-form"><label class="field">Type ${esc(s.hostname)} to confirm<input name="confirm" autocomplete="off"></label><button type="submit" class="danger">Remove site</button></form></div>` : ''}</section>`;
  bindTabs('site');
  bind(s, path, manage);
  bindChart('traffic', () => api(`${path}/traffic`), [[1, 'Requests per minute', 'rate'], [2, 'Server errors (5xx) per minute', 'rate']]);
}

function bind(s, path, manage) {
  const rerender = () => sitePage(s.hostname);
  $('#test').onclick = e => act(e.target, () => post(`${path}/test`), r => (r.ok ? `Backend answers: ${r.message}` : `Backend problem: ${r.message}`));
  $('#health').onclick = async e => {
    const r = await act(e.target, () => api(`${path}/health`), null);
    if (r) $('#health-out').innerHTML = checksList(r.checks);
  };
  const share = $('#share-form');
  if (share) share.onsubmit = async e => { e.preventDefault(); if (await act(share.querySelector('button'), () => post(`${path}/people`, form(share)), 'Shared.')) rerender(); };
  $$('[data-level]').forEach(select => { select.onchange = async () => { if (await act(select, () => post(`${path}/people`, { username: select.dataset.level, level: select.value }), 'Access changed.')) rerender(); }; });
  $$('[data-revoke]').forEach(b => { b.onclick = async () => { if (await act(b, () => post(`${path}/people`, { username: b.dataset.revoke, level: '' }), 'Access removed.')) rerender(); }; });
  if (!manage) return;
  $('#toggle').onclick = async e => { if (await act(e.target, () => post(`${path}/enabled`, { enabled: !s.enabled }), s.enabled ? 'Disabled.' : 'Enabled.')) rerender(); };
  $('#backend-form').onsubmit = async e => { e.preventDefault(); if (await act(e.target.querySelector('button'), () => post(`${path}/backend`, form(e.target)), r => r.message || 'Saved.')) rerender(); };
  const tls = $('#tls-form');
  if (tls) tls.onsubmit = async e => { e.preventDefault(); const d = form(tls); if (await act(tls.querySelector('button'), () => post(`${path}/tls`, { verify: Boolean(d.verify), sni: d.sni }), 'Saved.')) rerender(); };
  $('#auth-form').onsubmit = async e => { e.preventDefault(); if (await act(e.target.querySelector('button'), () => post(`${path}/auth`, { realm: form(e.target).realm }), 'Prompt saved.')) rerender(); };
  $('#auth-toggle').onclick = async e => { if (await act(e.target, () => post(`${path}/auth`, { enabled: !s.auth_enabled }), s.auth_enabled ? 'Protection off.' : 'Protection on.')) rerender(); };
  $('#user-form').onsubmit = async e => { e.preventDefault(); if (await act(e.target.querySelector('button'), () => post(`${path}/auth/users`, form(e.target)), 'User saved.')) rerender(); };
  $$('[data-remove-user]').forEach(b => { b.onclick = async () => { if (await act(b, () => del(`${path}/auth/users/${encodeURIComponent(b.dataset.removeUser)}`), 'User removed.')) rerender(); }; });
  $('#options-form').onsubmit = async e => {
    e.preventDefault();
    const f = e.target;
    let redirects;
    try { redirects = parseRedirects(f.redirects.value); } catch (error) { notice(error.message, true); return; }
    const body = { security_headers: f.security_headers.checked, friendly_errors: f.friendly_errors.checked, rate_limit: Number(f.rate_limit.value),
      redirects, maintenance: f.maintenance.checked, maintenance_page: f.maintenance_page.value };
    if (await act(f.querySelector('button[type=submit]'), () => post(`${path}/options`, body), 'Options saved; nginx reloaded.')) rerender();
  };
  const remove = $('#remove-form');
  if (remove) remove.onsubmit = async e => {
    e.preventDefault();
    if (form(remove).confirm.trim().toLowerCase() !== s.hostname) { notice(`Type ${s.hostname} exactly to confirm.`, true); return; }
    if (await act(remove.querySelector('button'), () => del(path), `${s.hostname} removed.`)) location.hash = '#/sites';
  };
}
