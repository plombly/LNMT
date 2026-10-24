// Domains (Cloudflare zones). Each domain is the parent of its sites: any
// hostname in the zone, whether or not the domain itself points here. Its
// page lists those sites and the zone's DNS records, which can be added,
// changed and removed here (records of this server's sites change only
// through the site). Members see and manage the domains they added.
import { api, del, isAdmin, post, session } from './api.js';
import { $, $$, act, crumbs, esc, form, main, pill, uptimePill } from './ui.js';

const zoneLink = zone => `#/domains/${encodeURIComponent(zone)}`;

export async function domainsPage(zone) {
  if (zone) return domainPage(zone);
  const data = await api('domains');
  main().innerHTML = `<div class="page-head"><h1>Domains</h1></div>
    ${data.domains.length ? `<div class="grid">${data.domains.map(d => `<a class="card site-card" href="${zoneLink(d.zone)}"><div class="spread"><div class="name">${esc(d.zone)}</div>${pill(`${d.sites.length} site${d.sites.length === 1 ? '' : 's'}`, d.sites.length ? 'ok' : 'off')}</div>
      <div class="subtle">${esc(d.sites.slice(0, 4).join(', ') || 'no sites yet')}${d.sites.length > 4 ? ' …' : ''}</div>${isAdmin() ? `<div class="row mt">${pill(d.owner ? `added by ${d.owner}` : 'administrators', 'off')}</div>` : ''}</a>`).join('')}</div>`
      : '<div class="empty">No domains yet.</div>'}
    <div class="card"><h2>Add a domain</h2>
      <p class="subtle">Put the domain on Cloudflare (the free plan is fine), then create an API token for it (My Profile → API Tokens → Create Token) with <b>Zone · DNS · Edit</b>, <b>Zone · Zone · Read</b> and <b>Zone · SSL and Certificates · Edit</b> for that zone only. The token is checked, stored on the server for this one zone and never shown again. ${isAdmin() ? 'Domains you add are the administrators\' (any site may use them); members add their own and only they manage theirs.' : 'Only you (and the administrators) manage the domains you add.'}</p>
      <form class="form" id="domain-form"><label class="field">Domain<input name="zone" placeholder="example.com" required></label>
      <label class="field">Cloudflare API token<input name="token" type="password" autocomplete="off" required></label><button type="submit">Check and add</button></form></div>`;
  const el = $('#domain-form');
  el.onsubmit = async e => { e.preventDefault(); const r = await act(el.querySelector('button'), () => post('domains', form(el)), x => `${x.zone} added.`); if (r) location.hash = zoneLink(r.zone); };
}

const relative = (d, name) => (name === d.zone ? '@' : name.slice(0, -(d.zone.length + 1)));

const recordForm = (d, r = {}) => `<form class="record-form" data-record="${esc(r.id || '')}">
  <select name="type" aria-label="Type">${d.record_types.map(t => `<option ${t === (r.type || 'A') ? 'selected' : ''}>${t}</option>`).join('')}</select>
  <input name="name" value="${esc(r.name ? relative(d, r.name) : '')}" placeholder="@ or www" aria-label="Name" required>
  <input name="content" value="${esc(r.content || '')}" placeholder="203.0.113.10, target.example.com or text" aria-label="Content" required>
  <input name="priority" type="number" min="0" max="65535" value="${esc(r.priority ?? 10)}" class="tiny" aria-label="Priority (MX)" title="Priority (MX only)">
  <select name="ttl" aria-label="TTL">${[[1, 'Auto'], [300, '5 min'], [3600, '1 h'], [86400, '1 day']].map(([v, l]) => `<option value="${v}" ${Number(r.ttl || 1) === v ? 'selected' : ''}>${l}</option>`).join('')}</select>
  <label class="subtle"><input type="checkbox" name="proxied" value="1" ${r.proxied ? 'checked' : ''}> proxied</label>
  <button type="submit" class="small">${r.id ? 'Save' : 'Add record'}</button>${r.id ? '<button type="button" class="quiet small" data-cancel>Cancel</button>' : ''}</form>`;

// Technical records (SRV, CAA, NS below the domain, TLSA, …): one field per
// part, as Cloudflare stores them; the server checks every part.
const techFields = (d, type, r = {}) => d.technical_types[type].map(f => {
  const value = r.type === type ? (f.key === 'content' ? r.content : f.key === 'priority' && type === 'URI' ? r.priority : r.data?.[f.key]) ?? '' : '';
  const input = f.kind === 'choice'
    ? `<select name="f:${f.key}">${f.limits[0].map(o => `<option ${o === value ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>`
    : `<input name="f:${f.key}" value="${esc(value)}" ${f.kind === 'int' || f.kind === 'float' ? `type="number" min="${f.limits[0]}" max="${f.limits[1]}" step="${f.kind === 'int' ? 1 : 'any'}" class="narrow"` : ''} required>`;
  return `<label class="tech-field"><span class="subtle">${esc(f.label)}</span>${input}</label>`;
}).join('');

const techForm = (d, r = {}) => {
  const types = Object.keys(d.technical_types);
  const type = r.type || 'SRV';
  return `<form class="tech-form" data-record="${esc(r.id || '')}">
    <label class="tech-field"><span class="subtle">Type</span><select name="type">${types.map(t => `<option ${t === type ? 'selected' : ''}>${t}</option>`).join('')}</select></label>
    <label class="tech-field"><span class="subtle">Name</span><input name="name" value="${esc(r.name ? relative(d, r.name) : '')}" placeholder="_sip._tcp" required></label>
    <span class="tech-parts">${techFields(d, type, r)}</span>
    <label class="tech-field"><span class="subtle">TTL</span><select name="ttl">${[[1, 'Auto'], [300, '5 min'], [3600, '1 h'], [86400, '1 day']].map(([v, l]) => `<option value="${v}" ${Number(r.ttl || 1) === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
    <span class="row"><button type="submit" class="small">${r.id ? 'Save' : 'Add record'}</button>${r.id ? '<button type="button" class="quiet small" data-cancel>Cancel</button>' : ''}</span></form>`;
};

async function domainPage(zone) {
  const path = `domains/${encodeURIComponent(zone)}`;
  const d = await api(path);
  const mine = isAdmin() || d.owner === session.me.username;
  const siteRows = d.sites.map(s => `<tr><td><a href="#/sites/${encodeURIComponent(s.hostname)}">${esc(s.hostname)}</a>${s.hostname === d.zone ? ' <span class="subtle">(the domain itself)</span>' : ''}</td>
    <td>${s.enabled ? pill('live', 'ok') : pill('disabled', 'off')} ${s.enabled ? uptimePill(s.uptime) : ''}</td><td class="subtle hide-sm">${esc(s.backend)}</td></tr>`).join('');
  const row = r => `<tr><td><code>${esc(r.type)}</code></td><td>${esc(r.name)}</td>
    <td class="record-content">${esc(r.type === 'MX' ? `${r.priority} ${r.content}` : r.content)}</td><td class="hide-sm">${r.technical ? '' : r.proxied ? pill('proxied', 'ok') : pill('DNS only', 'off')}</td>
    <td class="actions">${r.site ? `<a class="subtle" href="#/sites/${encodeURIComponent(r.site)}">site</a>` : mine ? `<button type="button" class="quiet small" data-edit="${esc(r.id)}">Edit</button> <button type="button" class="danger small" data-delete="${esc(r.id)}">Delete</button>` : ''}</td></tr>`;
  const recordRows = d.records.filter(r => !r.technical).map(row).join('');
  const techRows = d.records.filter(r => r.technical).map(row).join('');
  main().innerHTML = `${crumbs('#/domains', 'Domains')}<div class="page-head"><div><h1>${esc(d.zone)}</h1><p class="subtle">${d.owner ? `Added by ${esc(d.owner)}` : 'The administrators\' domain'}</p></div>
      <div class="row">${isAdmin() ? `<a class="button" href="#/add/${encodeURIComponent(d.zone)}">Add a site here</a>` : session.me.hosting ? '<a class="button quiet" href="#/apps">Publish an app here</a>' : ''}</div></div>
    <div class="card"><h2>Sites</h2><p class="subtle">Every site on this domain: the domain itself and its subdomains, whether or not the domain itself points at this server.</p>
      ${siteRows ? `<div class="table-wrap"><table><tbody>${siteRows}</tbody></table></div>` : '<p class="subtle">No sites on this domain yet.</p>'}</div>
    <div class="card"><h2>DNS records</h2><p class="subtle">The domain's records at Cloudflare. Records of this server's sites change on the site's page. <b>proxied</b> = traffic goes through Cloudflare, which hides the address (web traffic only).</p>
      <div class="table-wrap"><table class="records"><thead><tr><th>Type</th><th>Name</th><th>Content</th><th class="hide-sm">Cloudflare</th><th></th></tr></thead><tbody>${recordRows || '<tr><td colspan="5" class="subtle">No records.</td></tr>'}</tbody></table></div>
      ${mine ? `<h3>Add a record</h3>${recordForm(d)}<p class="subtle mt">Name: <code>@</code> for ${esc(d.zone)} itself, or <code>www</code>, <code>mail</code>… The priority box is for MX records; TXT takes any text (SPF, verification codes).</p>` : ''}</div>
    <div class="card"><h2>Technical records</h2><p class="subtle">SRV (services such as SIP, Minecraft, Matrix), CAA (which authorities may issue certificates), NS and DS (hand a subdomain to other name servers), HTTPS/SVCB, TLSA, SSHFP, NAPTR, URI, CERT, DNSKEY, OPENPGPKEY, SMIMEA, PTR and LOC. NS, DS and DNSKEY only below the domain, and never on a site of this server.</p>
      ${techRows ? `<div class="table-wrap"><table class="records"><thead><tr><th>Type</th><th>Name</th><th>Content</th><th class="hide-sm">Cloudflare</th><th></th></tr></thead><tbody>${techRows}</tbody></table></div>` : '<p class="subtle">None yet.</p>'}
      ${mine ? `<h3>Add a technical record</h3>${techForm(d)}` : ''}</div>
    ${mine ? `<div class="card"><h2>Token</h2><div class="row"><button type="button" class="quiet" id="zone-test">Test the token</button><button type="button" class="danger" id="zone-remove" ${d.sites.length ? 'disabled title="Remove its sites first"' : ''}>Remove the domain</button></div></div>` : ''}`;
  const save = async (f, id) => {
    const v = form(f);
    const body = { type: v.type, name: v.name, content: v.content, ttl: Number(v.ttl), proxied: Boolean(v.proxied), priority: Number(v.priority) };
    if (id) body.record_id = id;
    if (await act(f.querySelector('button[type=submit]'), () => post(`${path}/records`, body), r => `${r.type} ${r.name} saved.`)) domainPage(zone);
  };
  const bindForms = () => $$('.record-form').forEach(f => {
    const sync = () => { f.priority.hidden = f.type.value !== 'MX'; f.proxied.parentElement.hidden = !['A', 'AAAA', 'CNAME'].includes(f.type.value); };
    f.type.onchange = sync;
    sync();
    f.onsubmit = e => { e.preventDefault(); save(f, f.dataset.record); };
  });
  const saveTech = async (f, id) => {
    const v = form(f);
    const data = Object.fromEntries(Object.entries(v).filter(([k]) => k.startsWith('f:')).map(([k, x]) => [k.slice(2), x]));
    const body = { type: v.type, name: v.name, ttl: Number(v.ttl), data };
    if (id) body.record_id = id;
    if (await act(f.querySelector('button[type=submit]'), () => post(`${path}/records`, body), r => `${r.type} ${r.name} saved.`)) domainPage(zone);
  };
  const bindTech = () => $$('.tech-form').forEach(f => {
    f.type.onchange = () => { f.querySelector('.tech-parts').innerHTML = techFields(d, f.type.value); };
    f.onsubmit = e => { e.preventDefault(); saveTech(f, f.dataset.record); };
  });
  bindForms();
  bindTech();
  $$('[data-edit]').forEach(b => {
    b.onclick = () => {
      const tr = b.closest('tr');
      const r = d.records.find(x => x.id === b.dataset.edit);
      tr.innerHTML = `<td colspan="5">${r.technical ? techForm(d, r) : recordForm(d, r)}</td>`;
      tr.querySelector('[data-cancel]').onclick = () => domainPage(zone);
      if (r.technical) bindTech(); else bindForms();
    };
  });
  $$('[data-delete]').forEach(b => {
    b.onclick = async () => {
      const r = d.records.find(x => x.id === b.dataset.delete);
      if (confirm(`Delete the ${r.type} record ${r.name} → ${r.content}?`) && await act(b, () => del(`${path}/records/${r.id}`), 'Deleted.')) domainPage(zone);
    };
  });
  const test = $('#zone-test');
  if (test) test.onclick = () => act(test, () => post(`${path}/test`), r => (r.ok ? 'The token works.' : r.message));
  const remove = $('#zone-remove');
  if (remove) remove.onclick = async () => { if (confirm(`Remove ${zone}? Its token is deleted from this server; its records at Cloudflare stay.`) && await act(remove, () => del(path), 'Removed.')) location.hash = '#/domains'; };
}
