// Server-wide pages for administrators: status, Cloudflare tokens, backups, activity.
import { api, del, post } from './api.js';
import { $, $$, act, ago, certPill, checksList, esc, form, main, pill, size, when } from './ui.js';
import { bindLaikaSystem, laikaSystemCard } from './laika.js';

export async function systemPage() {
  const [system, edge, certs, nginx, alerts, node, laika] = await Promise.all([api('system'), api('edge'), api('certificates'), api('nginx'), api('alerts'), api('hosting/node').catch(e => ({ error: e.message })), api('laika').catch(() => ({ connected: false }))]);
  main().innerHTML = `<div class="page-head"><h1>System</h1></div>
    <div class="card"><h2>Status</h2>${checksList(system.checks)}</div>
    ${laikaSystemCard(laika)}
    <div class="card"><div class="spread"><h2>Alerts</h2><button type="button" class="quiet" id="alert-test">Send a test</button></div>
      <p class="subtle">Checked every 2 minutes; each change is sent once. Addresses are stored root-only and never shown again: leave a field empty to keep what is set.</p>
      <form id="alerts-form"><div class="alert-targets">
        <div class="target"><div class="spread"><b>Discord</b>${alerts.discord ? pill('set', 'ok') : pill('not set', 'off')}</div><input name="discord_webhook" type="password" autocomplete="off" placeholder="${alerts.discord ? 'replace the webhook…' : 'https://discord.com/api/webhooks/…'}">${alerts.discord ? '<label class="subtle"><input type="checkbox" name="remove_discord" value="1"> stop sending to Discord</label>' : ''}</div>
        <div class="target"><div class="spread"><b>ntfy</b>${alerts.ntfy ? pill('set', 'ok') : pill('not set', 'off')}</div><input name="ntfy_url" type="password" autocomplete="off" placeholder="${alerts.ntfy ? 'replace the topic URL…' : 'https://ntfy.sh/your-topic'}">${alerts.ntfy ? '<label class="subtle"><input type="checkbox" name="remove_ntfy" value="1"> stop sending to ntfy</label>' : ''}</div></div>
        <h3>Send an alert when</h3><div class="checkbox-list">${Object.entries(alerts.labels).map(([k, label]) => `<label><input type="checkbox" name="event:${k}" value="1" ${alerts.events[k] ? 'checked' : ''}> ${esc(label)}</label>`).join('')}</div>
        <div class="row mt"><button type="submit">Save</button></div></form></div>
    <div class="card"><div class="spread"><h2>Dynamic DNS</h2><button type="button" id="ddns">Run now</button></div>
      <p class="subtle">Every 5 minutes the manager points each site's proxied DNS record at this network's public IP (${esc(system.public_ip || 'unknown')}). Last check ${esc(system.last_check ? ago(Date.parse(system.last_check) / 1000) : '—')}.</p><pre id="ddns-out" hidden></pre></div>
    <div class="card"><div class="spread"><h2>Cloudflare-only firewall</h2><button type="button" id="edge">Update now</button></div>
      <p class="subtle">Port 443 accepts only Cloudflare's ranges (refreshed daily and at boot): ${edge.ipv4.length} IPv4 and ${edge.ipv6.length} IPv6, last updated ${esc(ago(edge.updated))}. Visitors' real IPs are restored from Cloudflare's header.</p></div>
    <div class="card"><h2>Hosting node</h2>${node.error ? `<p class="subtle">${esc(node.error)}</p>` : `<dl class="facts"><dt>Node</dt><dd>${esc(node.hostname)} · ${esc(node.node_ip)} · ${esc(node.cpus)} CPUs · ${esc(size(node.memory_available))} of ${esc(size(node.memory_total))} memory free · load ${esc(node.load.map(l => l.toFixed(2)).join(' '))}</dd><dt>Disk</dt><dd>${esc(size(node.disk_free))} free of ${esc(size(node.disk_total))}</dd></dl>${node.customers.length ? `<div class="table-wrap mt"><table><thead><tr><th>Customer</th><th>Disk</th><th>Apps</th></tr></thead><tbody>${node.customers.map(c => `<tr><td>${esc(c.name)}</td><td>${esc(size(c.disk_used))} of ${esc(size(c.disk_total))}</td><td>${esc(c.apps.join(', ') || '—')}</td></tr>`).join('')}</tbody></table></div>` : '<p class="subtle">No customers yet: enable hosting for someone on the Users page.</p>'}`}</div>
    <div class="card"><h2>Certificates</h2>${certs.certificates.length ? `<div class="table-wrap"><table><thead><tr><th>Zone</th><th>Covers</th><th>Expires</th></tr></thead><tbody>${certs.certificates.map(c => `<tr><td>${esc(c.zone)}</td><td>${esc((c.hostnames || []).join(', '))}</td><td>${certPill(c.expires)} <span class="subtle">${esc(when(c.expires))}</span></td></tr>`).join('')}</tbody></table></div>` : '<p class="subtle">None yet.</p>'}
      <p class="subtle mt">Cloudflare Origin CA certificates, one per zone (covering the zone and one label below it).</p></div>
    <div class="card"><h2>nginx check</h2>${nginx.ok ? pill('passes', 'ok') : pill('fails', 'fail')}<pre class="mt">${esc(nginx.output)}</pre></div>`;
  const alertsForm = $('#alerts-form');
  alertsForm.onsubmit = async e => {
    e.preventDefault();
    const d = form(alertsForm);
    const body = { events: Object.fromEntries(Object.keys(alerts.labels).map(k => [k, Boolean(d[`event:${k}`])])) };
    if (d.discord_webhook) body.discord_webhook = d.discord_webhook;
    if (d.ntfy_url) body.ntfy_url = d.ntfy_url;
    if (d.remove_discord) body.discord_webhook = '';
    if (d.remove_ntfy) body.ntfy_url = '';
    if (await act(alertsForm.querySelector('button'), () => post('alerts', body), 'Alerts saved.')) systemPage();
  };
  bindLaikaSystem(systemPage);
  $('#alert-test').onclick = e => act(e.target, () => post('alerts/test'), r => `Test sent to ${r.sent} place(s).`);
  $('#ddns').onclick = async e => { const r = await act(e.target, () => post('ddns/run'), x => (x.ok ? 'DNS is up to date.' : 'DNS update had problems.')); if (r) { const out = $('#ddns-out'); out.hidden = false; out.textContent = r.output; } };
  $('#edge').onclick = async e => { if (await act(e.target, () => post('edge/update'), x => (x.ok ? 'Cloudflare ranges updated.' : `Update failed: ${x.output}`))) systemPage(); };
}

export async function cloudflarePage() {
  const data = await api('credentials');
  main().innerHTML = `<div class="page-head"><h1>Cloudflare</h1></div>
    <div class="card"><h2>API tokens</h2><p class="subtle">One token per zone, stored on the server readable by root only and never shown again. The token needs Zone · DNS · Edit, Zone · Zone · Read and Zone · SSL and Certificates · Edit for its zone.</p>
    ${data.credentials.length ? `<div class="table-wrap"><table><thead><tr><th>Zone</th><th>Zone ID</th><th>Sites</th><th></th></tr></thead><tbody>${data.credentials.map(c => `<tr><td>${esc(c.zone)}</td><td><code>${esc(c.zone_id)}</code></td><td>${esc(c.sites.join(', ') || '—')}</td>
      <td class="actions"><button type="button" class="quiet" data-test="${esc(c.zone)}">Test</button> <button type="button" class="danger" data-remove="${esc(c.zone)}" ${c.sites.length ? 'disabled title="Remove its sites first"' : ''}>Remove</button></td></tr>`).join('')}</tbody></table></div>` : '<p class="subtle">No tokens yet.</p>'}</div>
    <div class="card"><h2>Add a token</h2><form class="form" id="cred-form"><label class="field">Zone<input name="zone" placeholder="example.com" required></label>
      <label class="field">API token<input name="token" type="password" autocomplete="off" required></label><button type="submit">Check and save</button></form></div>`;
  $('#cred-form').onsubmit = async e => { e.preventDefault(); if (await act(e.target.querySelector('button'), () => post('credentials', form(e.target)), r => `Saved; zone ${r.zone} reachable.`)) cloudflarePage(); };
  $$('[data-test]').forEach(b => { b.onclick = () => act(b, () => post(`credentials/${encodeURIComponent(b.dataset.test)}/test`), r => (r.ok ? `${r.zone}: API access OK.` : `${r.zone}: ${r.message}`)); });
  $$('[data-remove]').forEach(b => { b.onclick = async () => { if (confirm(`Remove the token for ${b.dataset.remove}?`) && await act(b, () => del(`credentials/${encodeURIComponent(b.dataset.remove)}`), 'Removed.')) cloudflarePage(); }; });
}

export async function backupsPage() {
  const data = await api('backups');
  main().innerHTML = `<div class="page-head"><h1>Backups</h1><button type="button" id="backup">Back up now</button></div>
    <div class="card"><p class="subtle">Taken daily at 03:40; the newest 14 are kept in <code>/var/backups/lnmt</code> (root only). Each holds the manager's state, Cloudflare tokens, certificates and keys, password-protection users, nginx sites, the default site, the firewall configuration and the services. "Verify" checks an archive completely without restoring anything.</p>
    ${data.backups.length ? `<div class="table-wrap"><table><thead><tr><th>Backup</th><th>Size</th><th>Taken</th><th></th></tr></thead><tbody>${data.backups.map(b => `<tr><td><code>${esc(b.name)}</code></td><td>${esc(size(b.bytes))}</td><td>${esc(when(b.created))}</td><td class="actions"><button type="button" class="quiet" data-verify="${esc(b.name)}">Verify</button></td></tr>`).join('')}</tbody></table></div>` : '<p class="subtle">No backups yet.</p>'}</div><pre id="verify-out" hidden></pre>`;
  $('#backup').onclick = async e => { if (await act(e.target, () => post('backups'), 'Backup made.')) backupsPage(); };
  $$('[data-verify]').forEach(b => {
    b.onclick = async () => { const r = await act(b, () => post(`backups/${encodeURIComponent(b.dataset.verify)}/verify`), x => (x.ok ? 'The backup is complete and intact.' : 'The backup has problems.')); if (r) { const out = $('#verify-out'); out.hidden = false; out.textContent = r.output; } };
  });
}

export async function activityPage() {
  const data = await api('audit');
  main().innerHTML = `<div class="page-head"><h1>Activity</h1></div><div class="card"><div class="table-wrap"><table><thead><tr><th>When</th><th>Who</th><th>What</th><th>Target</th><th>Result</th></tr></thead><tbody>
    ${data.entries.map(e => `<tr><td>${esc(when(e.at))}</td><td>${esc(e.user)} <span class="subtle">${esc(e.ip)}</span></td><td>${esc(e.action)}</td><td>${esc(e.target)}</td><td>${e.ok ? pill('ok', 'ok') : pill('failed', 'fail')} <span class="subtle">${esc(e.detail || '')}</span></td></tr>`).join('') || '<tr><td colspan="5" class="empty">Nothing yet.</td></tr>'}
    </tbody></table></div></div>`;
}
