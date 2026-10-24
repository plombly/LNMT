// LAIka, the AI build system next door: connecting it (System), linking your
// account (Account), and the LAIka project that builds an app (app page).
import { api, del, post, session } from './api.js';
import { $, act, copyButton, esc, pill, when } from './ui.js';

const isWordPress = a => (a.env_names || []).includes('WORDPRESS_DB_HOST') || String(a.image || '').includes('wordpress');
const statusPill = s => pill(s || 'unknown', ['merged', 'done', 'completed', 'succeeded'].includes(s) ? 'ok' : ['failed', 'rejected', 'needs_human'].includes(s) ? 'fail' : 'warn');

export function laikaSystemCard(status) {
  return `<div class="card" id="laika-card"><div class="spread"><h2>LAIka</h2>${status.connected ? pill('connected', 'ok') : pill('not connected', 'off')}</div>
    ${status.connected
      ? `<p class="subtle">Connected to <code>${esc(status.url)}</code> since ${esc(when(status.connected_at))}. People sign in here with their LAIka account and appear under Users by themselves; apps can be built by LAIka projects.</p>
         <div class="row"><button type="button" class="quiet" id="laika-sync">Bring in LAIka's people now</button><button type="button" class="danger" id="laika-disconnect">Disconnect</button></div>`
      : `<p class="subtle">Connect LAIka so people sign in here with their LAIka accounts and LAIka can build sites: make a pairing code, then paste it in LAIka under Settings → System → LNMT. The code works once, for 30 minutes.</p>
         <div class="row"><button type="button" id="laika-pair">Make a pairing code</button></div><div id="laika-code"></div>`}</div>`;
}

export function bindLaikaSystem(reload) {
  const pair = $('#laika-pair');
  if (pair) pair.onclick = async () => {
    const r = await act(pair, () => post('laika/pair'), null);
    if (r) $('#laika-code').innerHTML = `<pre class="mt code-wrap">${esc(r.code)}</pre>${copyButton(r.code)}`;
  };
  const sync = $('#laika-sync');
  if (sync) sync.onclick = () => act(sync, () => post('laika/sync'), r => (r.made.length ? `New accounts: ${r.made.join(', ')}.` : 'Everyone is here already.'));
  const off = $('#laika-disconnect');
  if (off) off.onclick = async () => { if (confirm('Disconnect LAIka? Sign-in with LAIka stops; apps keep receiving pushes.') && await act(off, () => del('laika'), 'Disconnected.')) reload(); };
}

export function laikaAccountCard(me) {
  if (me.laika) return `<div class="card"><h2>LAIka</h2><p>Linked to the LAIka account <b>${esc(me.laika)}</b>: you can sign in with LAIka, and LAIka builds apps for you.</p></div>`;
  return `<div class="card" id="laika-link" hidden><h2>LAIka</h2><p class="subtle">${me.laika_suggest ? `There is a LAIka account <b>${esc(me.laika_suggest)}</b>. ` : ''}Link your LAIka account to sign in with it and have LAIka build your apps.</p><a class="button" href="/api/laika/login?link=1">Link my LAIka account</a></div>`;
}

export function bindLaikaAccount() {
  const card = $('#laika-link');
  if (card) api('laika/available').then(r => { card.hidden = !r.available; }).catch(() => {});
}

export const laikaAppCard = (a, info) => {
  if (info.linked) {
    const s = info.status || {};
    const work = [...(s.goals || []).map(g => ({ ...g, kind: 'Goal' })), ...(s.jobs || []).slice(0, 3).map(j => ({ ...j, kind: 'Job' }))];
    return `<div class="card"><div class="spread"><h2>Built by LAIka</h2>${info.url ? `<a class="button" href="${esc(info.url)}" target="_blank" rel="noopener">Open in LAIka</a>` : ''}</div>
      <p class="subtle">The LAIka project <b>${esc(s.name || info.project)}</b> builds this app: describe what you want there, approve the result, and it is pushed here, built and put live.</p>
      ${s.error ? `<p class="subtle">${esc(s.error)}</p>` : work.length ? `<table><tbody>${work.map(w => `<tr><td class="subtle">${w.kind}</td><td>${esc(w.title || w.id)}</td><td>${statusPill(w.status)}</td></tr>`).join('')}</tbody></table>` : '<p class="subtle">No work yet: give the project its first goal in LAIka.</p>'}</div>`;
  }
  if (!info.connected || !a.build) return '';
  return `<div class="card"><h2>Build with LAIka</h2><p class="subtle">LAIka makes a project for this app: you describe the ${isWordPress(a) ? 'site (look, pages, text, images)' : 'app'} in LAIka, approve what it builds, and every approved change is pushed here and put live.</p>
    <form class="form" id="laika-build"><label class="field">Project id<input name="project" value="${esc(a.name)}" pattern="[a-z0-9][a-z0-9-]{0,39}" required></label>
      <label class="field">Name<input name="title" value="${esc(a.name)}" required></label>
      <input type="hidden" name="template" value="${isWordPress(a) ? 'wordpress-theme' : ''}"><button type="submit">Create the LAIka project</button></form></div>`;
};

export function bindLaikaApp(path, reload) {
  const f = $('#laika-build');
  if (f) f.onsubmit = async e => {
    e.preventDefault();
    const body = { project: f.project.value.trim(), title: f.title.value.trim(), template: f.template.value };
    if (await act(f.querySelector('button'), () => post(`${path}/laika`, body), r => `LAIka project ${r.project} created; it deploys here.`)) reload();
  };
}

export const laikaUserBadge = u => (u.laika ? pill(u.password ? 'LAIka linked' : 'from LAIka', 'ok') : u.laika_suggest ? pill('LAIka account to link', 'warn') : '');

export const signedInWithLaika = () => Boolean(session.me?.laika);
