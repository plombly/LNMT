// Hosting: apps running in containers on the hosting node. Members see their
// own apps; administrators see everyone's and publish apps at a hostname.
import { api, del, isAdmin, patch, post, session } from './api.js';
import { $, $$, act, bindTabs, copyButton, crumbs, esc, form, main, notice, pill, size, tabBar, when } from './ui.js';
import { bindFiles, filesCard } from './files.js';
import { bindExtras, databaseCard, laikaCard, tasksCard, usageCard } from './appextras.js';
import { bindLaikaApp, laikaAppCard } from './laika.js';

const statePill = state => pill(state === 'active' ? 'running' : state || 'unknown', state === 'active' ? 'ok' : state === 'activating' ? 'warn' : 'fail');
const appLink = a => `#/apps/${encodeURIComponent(a.customer)}/${encodeURIComponent(a.name)}`;
const parseEnv = text => Object.fromEntries(text.split('\n').map(l => l.trim()).filter(l => l && !l.startsWith('#')).map(l => [l.slice(0, l.indexOf('=')).trim(), l.slice(l.indexOf('=') + 1)]).filter(([k]) => k));

export async function appsPage() {
  const [data, mine] = await Promise.all([api('apps'), session.me.hosting ? api('hosting/me').catch(() => null) : null]);
  const canCreate = isAdmin() || session.me.hosting;
  const disk = mine?.disk_total ? Math.round(100 * mine.disk_used / mine.disk_total) : null;
  main().innerHTML = `<div class="page-head"><h1>Apps</h1>${canCreate ? '<a class="button" href="#/new-app">New app</a>' : ''}</div>
    ${disk !== null ? `<div class="card"><div class="spread"><span>Your disk: <b>${esc(size(mine.disk_used))}</b> of ${esc(size(mine.disk_total))} used (files, images, databases)</span>${pill(`${disk}%`, disk < 80 ? 'ok' : disk < 95 ? 'warn' : 'fail')}</div><div class="progress mt"><i class="bar-${disk < 80 ? 'ok' : disk < 95 ? 'warn' : 'fail'}" data-width="${disk}"></i></div></div>` : ''}
    <p class="subtle">Apps run in their own containers on the hosting node: any PHP version, Node, Python, WordPress, databases, or any container image. Each person's apps share a private network (they reach each other by name) and a disk of their own.</p>
    ${data.apps.length ? `<div class="grid">${data.apps.map(a => `<a class="card site-card" href="${appLink(a)}"><div class="spread"><div class="name">${esc(a.name)}</div>${statePill(a.state)}</div>
      <div class="subtle">${esc(a.image)}</div><div class="row mt">${a.host_port ? pill('web', 'ok') : pill('private', 'off')}${isAdmin() ? pill(a.customer, 'off') : ''}</div></a>`).join('')}</div>`
      : `<div class="empty">${canCreate ? 'No apps yet.' : 'Hosting is not enabled for your account yet; ask an administrator.'}</div>`}`;
  $$('[data-width]').forEach(bar => bar.style.setProperty('width', `${bar.dataset.width}%`));
}

export async function newAppPage() {
  const [t, users] = await Promise.all([api('hosting/templates'), isAdmin() ? api('users') : null]);
  const customers = users ? users.users.filter(u => u.hosting).map(u => u.hosting.customer) : [];
  main().innerHTML = `<div class="page-head"><h1>New app</h1></div><div class="card"><form class="form" id="app-form">
    ${isAdmin() ? `<label class="field">For${customers.length ? `<select name="customer">${customers.map(c => `<option>${esc(c)}</option>`).join('')}</select>` : ' <a href="#/users">enable hosting for someone first</a>'}</label>` : ''}
    <label class="field">Name<input name="name" pattern="[a-z][a-z0-9-]{1,30}" placeholder="my-site" required></label>
    <label class="field">Kind<select name="template">${Object.entries(t.templates).map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join('')}</select></label>
    <label class="field" id="version-field">Version<select name="version"></select></label>
    <label class="field" id="image-field" hidden>Image<input name="image" placeholder="ghcr.io/you/app:latest"></label>
    <label class="field" id="port-field" hidden>Port inside the container (0 = private)<input name="port" type="number" min="0" max="65535" value="8080" class="narrow"></label>
    <label class="field">Memory (MB)<input name="memory_mb" type="number" min="32" max="16384" placeholder="default" class="narrow"></label>
    <label class="field">CPUs<input name="cpus" type="number" min="0.1" max="8" step="0.1" value="1" class="narrow"></label>
    <label class="field wide">Environment (NAME=value per line)<textarea name="env" rows="4" placeholder="APP_ENV=production"></textarea></label>
    <button type="submit">Create app</button></form></div><div id="created"></div>`;
  const el = $('#app-form');
  const sync = () => {
    const kind = t.templates[el.template.value];
    $('#version-field').hidden = !kind.versions.length;
    el.version.innerHTML = kind.versions.map(v => `<option ${v === kind.versions.at(-1) ? 'selected' : ''}>${esc(v)}</option>`).join('');
    $('#image-field').hidden = el.template.value !== 'image';
    $('#port-field').hidden = !['image', 'dockerfile'].includes(el.template.value);
  };
  el.template.onchange = sync;
  sync();
  el.onsubmit = async e => {
    e.preventDefault();
    const d = form(el);
    d.env = parseEnv(d.env || '');
    if (d.template !== 'image') delete d.image;
    if (!['image', 'dockerfile'].includes(d.template)) delete d.port;
    const r = await act(el.querySelector('button'), () => post('apps', d), x => `Created ${x.app.name}${x.also_created.length ? ` (and ${x.also_created.join(', ')})` : ''}.`);
    if (!r) return;
    if (Object.keys(r.shown_once || {}).length) {
      $('#created').innerHTML = `<div class="card"><h2>Save these now</h2><p class="subtle">They are not shown again. Other apps of yours reach this one by its name.</p><pre>${esc(Object.entries(r.shown_once).map(([k, v]) => `${k}: ${v}`).join('\n'))}</pre><a class="button mt" href="${appLink(r.app)}">Open the app</a></div>`;
    } else location.hash = appLink(r.app);
  };
}

export async function appPage(customer, name) {
  const path = `apps/${encodeURIComponent(customer)}/${encodeURIComponent(name)}`;
  const a = await api(path);
  main().innerHTML = `${crumbs('#/apps', 'Apps')}<div class="page-head"><div><h1>${esc(a.name)}</h1><p class="subtle">${esc(a.image)}${isAdmin() ? ` · ${esc(a.customer)}` : ''}</p></div>
      <div class="row">${statePill(a.state)}<button type="button" class="quiet" data-app-action="restart">Restart</button>${a.state === 'active' ? '<button type="button" class="quiet" data-app-action="stop">Stop</button>' : '<button type="button" data-app-action="start">Start</button>'}</div></div>
    ${tabBar([['overview', 'Overview'], ['code', a.build ? 'Code & build' : 'Files'], ['settings', 'Settings']])}<section data-panel="overview">
    <div class="card"><dl class="facts"><dt>Reached at</dt><dd>${a.host_port ? `inside: <code>${esc(a.name)}:${esc(a.port)}</code>${a.sites.length ? ` · public: ${a.sites.map(h => `<a href="#/sites/${encodeURIComponent(h)}">${esc(h)}</a>`).join(', ')}` : ' · not public yet'}` : `private: <code>${esc(a.name)}</code> from your other apps`}</dd>
      <dt>Limits</dt><dd>${esc(a.memory_mb)} MB memory, ${esc(a.cpus)} CPU${a.usage?.memory ? ` · now ${esc(a.usage.memory)}, ${esc(a.usage.cpu)}` : ''}</dd>
      <dt>Data</dt><dd>${a.volumes.length ? a.volumes.map(v => `<code>${esc(v.name)}</code> → ${esc(v.path)}`).join(', ') : 'none'}</dd>
      <dt>Updates</dt><dd>${a.build ? 'built from your code' : a.auto_update ? 'checked daily; a newer image is rolled back if the app does not start' : 'manual (Settings)'}</dd></dl></div>
    ${usageCard()}
    ${a.host_port ? `<div class="card"><h2>Publish</h2><p class="subtle">Serve this app at a hostname: a proxied site whose backend is this app (TLS, DNS, uptime and alerts as for every site). ${isAdmin() ? 'Any zone with a Cloudflare token.' : 'Use a hostname on one of <a href="#/domains">your domains</a>.'}</p>
      <form class="form" id="publish-form"><label class="field">Hostname<input name="hostname" placeholder="shop.example.com" required></label><button type="submit">Publish</button></form></div>` : ''}
    <div class="card"><div class="spread"><h2>Logs</h2><button type="button" class="quiet" id="logs-refresh">Refresh</button></div><pre id="logs" class="mt">Loading…</pre></div>
    </section><section data-panel="code" hidden>
    ${a.build ? `<div class="card"><div class="spread"><h2>Build</h2><button type="button" id="build-now">Build now</button></div><p class="subtle">Built from the <code>Dockerfile</code> at the top of its source (<code>src/</code>): every push to <code>main</code> builds and redeploys it; if a build fails, the running version stays.</p><div id="build-status" class="subtle">Loading…</div><pre id="build-log" class="mt" hidden></pre></div>` : ''}
    <div class="card"><h2>Code access</h2><p class="subtle">Add your SSH key under <a href="#/account">your account</a> first. From the LAN or the VPN:</p><dl class="facts">${a.build ? `<dt>Git</dt><dd><code>git remote add laika ssh://c-${esc(a.customer)}@${esc(a.node_ip)}/${esc(a.name)}.git</code> ${copyButton(`git remote add laika ssh://c-${a.customer}@${a.node_ip}/${a.name}.git`)}<br><code>git push laika main</code></dd>` : ''}<dt>SFTP</dt><dd><code>sftp c-${esc(a.customer)}@${esc(a.node_ip)}</code> ${copyButton(`sftp c-${a.customer}@${a.node_ip}`)} (starts in your apps folder)</dd></dl></div>
    <div id="laika-app"></div>
    ${filesCard(path)}
    </section><section data-panel="settings" hidden>
    <div class="card"><h2>Settings</h2><form class="form" id="settings-form">
      <label class="field">Image<input name="image" value="${esc(a.image)}"></label>
      <label class="field">Memory (MB)<input name="memory_mb" type="number" min="32" max="16384" value="${esc(a.memory_mb)}" class="narrow"></label>
      <label class="field">CPUs<input name="cpus" type="number" min="0.1" max="8" step="0.1" value="${esc(a.cpus)}" class="narrow"></label>
      <label class="field wide">Start command (empty = the image's own)<input name="command" value="${esc(a.command)}"></label>
      <label class="field wide"><span>Environment: set: ${a.env_names.length ? a.env_names.map(n => `<code>${esc(n)}</code>`).join(' ') : 'none'}. To change it, enter the complete new list (values are never shown).</span><textarea name="env" rows="3" placeholder="leave empty to keep the current values"></textarea></label>
      ${a.build ? '' : `<label class="field wide"><span><input type="checkbox" name="auto_update" value="1" ${a.auto_update ? 'checked' : ''}> keep the image updated (checked daily; rolled back if the app does not come up)</span></label>`}
      <button type="submit">Save and restart</button></form></div>
    ${databaseCard(a)}
    ${tasksCard(a)}
    <div class="card"><h2>Remove</h2><form class="form" id="remove-form"><label class="field">Type ${esc(a.name)} to confirm<input name="confirm" autocomplete="off"></label>
      <label class="field"><span><input type="checkbox" name="keep_data" value="1" checked> keep its data on the disk</span></label><button type="submit" class="danger">Remove app</button></form></div></section>`;
  bindTabs('app');
  bindFiles(path);
  bindExtras(a, path, () => appPage(customer, name));
  api(`${path}/laika`).then(info => {
    $('#laika-app').innerHTML = laikaAppCard(a, info) || laikaCard(a);
    bindLaikaApp(path, () => appPage(customer, name));
  }).catch(() => { $('#laika-app').innerHTML = laikaCard(a); });
  if (a.build) {
    const loadBuild = async () => {
      try {
        const b = await api(`${path}/build`);
        $('#build-status').innerHTML = `${pill(b.build.state, b.build.state === 'deployed' ? 'ok' : b.build.state === 'failed' ? 'fail' : 'warn')} ${b.build.rev ? `commit <code>${esc(b.build.rev)}</code> · ` : ''}${b.build.finished ? esc(when(b.build.finished)) : ''} ${b.build.error ? esc(b.build.error) : ''}`;
        if (b.log) { $('#build-log').hidden = false; $('#build-log').textContent = b.log.slice(-6000); }
      } catch (error) { $('#build-status').textContent = error.message; }
    };
    loadBuild();
    $('#build-now').onclick = async e => { await act(e.target, () => post(`${path}/build`), r => (r.build.state === 'failed' ? 'Build failed: see the log.' : 'Built and deployed.')); loadBuild(); };
  }
  const loadLogs = async () => { try { $('#logs').textContent = (await api(`${path}/logs`)).logs || '(no output yet)'; } catch (error) { $('#logs').textContent = error.message; } };
  loadLogs();
  $('#logs-refresh').onclick = loadLogs;
  $$('[data-app-action]').forEach(b => { b.onclick = async () => { if (await act(b, () => post(`${path}/action`, { action: b.dataset.appAction }), 'Done.')) appPage(customer, name); }; });
  const pub = $('#publish-form');
  if (pub) pub.onsubmit = async e => { e.preventDefault(); if (await act(pub.querySelector('button'), () => post(`${path}/publish`, form(pub)), r => `Published at ${r.hostname}.`)) appPage(customer, name); };
  const settings = $('#settings-form');
  settings.onsubmit = async e => {
    e.preventDefault();
    const d = form(settings);
    const body = { image: d.image, memory_mb: Number(d.memory_mb), cpus: Number(d.cpus), command: d.command };
    if (!a.build) body.auto_update = Boolean(d.auto_update);
    if (d.env.trim()) body.env = parseEnv(d.env);
    if (await act(settings.querySelector('button'), () => patch(path, body), 'Saved; restarting.')) appPage(customer, name);
  };
  const remove = $('#remove-form');
  remove.onsubmit = async e => {
    e.preventDefault();
    const d = form(remove);
    if (d.confirm.trim() !== a.name) { notice(`Type ${a.name} exactly to confirm.`, true); return; }
    if (await act(remove.querySelector('button'), () => api(path, { method: 'DELETE', body: JSON.stringify({ keep_data: Boolean(d.keep_data) }) }), 'Removed.')) location.hash = '#/apps';
  };
}
