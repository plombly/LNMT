// Accounts (administrators): invite people with a one-time link, change a
// role, disable, reset a password, remove. Site access is set on each site.
import { api, del, patch, post, session } from './api.js';
import { $, $$, act, copy, esc, form, main, pill, when } from './ui.js';
import { laikaUserBadge } from './laika.js';

const accessText = user => {
  if (user.role === 'admin') return 'every site';
  const entries = Object.entries(user.access || {});
  return entries.length ? entries.map(([h, l]) => `${h} (${l})`).join(', ') : 'no sites yet';
};

export async function usersPage() {
  const [data, invites] = await Promise.all([api('users'), api('invites')]);
  main().innerHTML = `<div class="page-head"><h1>Users</h1></div>
    <div class="card"><h2>Invite someone</h2><p class="subtle">Makes a one-time link, valid for 24 hours, that opens only from the LAN or the VPN. The person picks their own user name and password. Members start with no sites: share sites with them on each site's page.</p>
      <form class="form" id="invite-form"><label class="field">Role<select name="role"><option value="member">member</option><option value="admin">administrator</option></select></label>
      <label class="field">Note (who it is for)<input name="note" maxlength="80" placeholder="optional"></label><button type="submit">Create invite link</button></form>
      <div id="invite-out" class="mt" hidden><label class="field">Send this link to them<input id="invite-url" readonly></label><div class="row mt"><button type="button" id="copy-invite">Copy</button></div></div>
      ${invites.invites.length ? `<h3>Open invites</h3><table><tbody>${invites.invites.map(i => `<tr><td>${esc(i.role)}${i.note ? ` · ${esc(i.note)}` : ''}</td><td class="subtle">by ${esc(i.created_by)} · until ${esc(when(i.expires))}</td><td class="actions"><button type="button" class="danger" data-revoke-invite="${esc(i.id)}">Cancel</button></td></tr>`).join('')}</tbody></table>` : ''}</div>
    <div class="card"><h2>Accounts</h2><div class="table-wrap"><table><thead><tr><th>User</th><th>Role</th><th>Sites</th><th>Hosting</th><th></th></tr></thead><tbody>${data.users.map(u => `<tr><td>${esc(u.username)}${u.disabled ? ' ' + pill('disabled', 'off') : ''} ${laikaUserBadge(u)}</td><td>${esc(u.role)}</td><td class="subtle">${esc(accessText(u))}</td><td>${u.hosting ? `${pill(`${u.hosting.disk_gb} GB`, 'ok')} <span class="subtle">${esc(u.hosting.memory_gb || 4)} GB memory · ${esc(u.hosting.cpus || 2)} CPU</span> <button type="button" class="quiet small" data-limits="${esc(u.username)}">Limits</button>
      <form class="limits-form mt" data-limits-form="${esc(u.username)}" hidden><label>Disk (GB, grows only)<input name="disk_gb" type="number" min="${esc(u.hosting.disk_gb)}" max="500" value="${esc(u.hosting.disk_gb)}" class="tiny"></label>
        <label>Memory (GB)<input name="memory_gb" type="number" min="0.25" max="64" step="0.25" value="${esc(u.hosting.memory_gb || 4)}" class="tiny"></label>
        <label>CPUs<input name="cpus" type="number" min="0.1" max="32" step="0.1" value="${esc(u.hosting.cpus || 2)}" class="tiny"></label>
        <label>Processes<input name="tasks" type="number" min="64" max="65536" value="${esc(u.hosting.tasks || 4096)}" class="tiny"></label>
        <button type="submit" class="small">Save limits</button></form>` : `<form class="inline-form" data-hosting="${esc(u.username)}"><input name="disk_gb" type="number" min="1" max="500" value="10" class="tiny" aria-label="Disk in GB"> GB <button type="submit" class="quiet">Enable hosting</button></form>`}</td>
      <td class="actions"><button type="button" class="quiet" data-edit="${esc(u.username)}">Edit</button> ${u.username === session.me.username ? '' : `<button type="button" class="danger" data-delete="${esc(u.username)}">Remove</button>`}</td></tr>`).join('')}</tbody></table></div></div>
    <div class="card" id="user-editor" hidden></div>`;
  const inviteForm = $('#invite-form');
  inviteForm.onsubmit = async e => {
    e.preventDefault();
    const r = await act(inviteForm.querySelector('button'), () => post('invites', form(inviteForm)), 'Invite link created.');
    if (r) { $('#invite-out').hidden = false; $('#invite-url').value = r.url; $('#invite-url').select(); }
  };
  $('#copy-invite').onclick = () => copy($('#invite-url').value);
  $$('[data-revoke-invite]').forEach(b => { b.onclick = async () => { if (await act(b, () => del(`invites/${b.dataset.revokeInvite}`), 'Invite cancelled.')) usersPage(); }; });
  $$('[data-delete]').forEach(b => { b.onclick = async () => { if (confirm(`Remove ${b.dataset.delete}?`) && await act(b, () => del(`users/${encodeURIComponent(b.dataset.delete)}`), 'Removed.')) usersPage(); }; });
  $$('[data-edit]').forEach(b => { b.onclick = () => editor(data.users.find(u => u.username === b.dataset.edit)); });
  $$('[data-limits]').forEach(b => { b.onclick = () => { const f = $(`[data-limits-form="${b.dataset.limits}"]`); f.hidden = !f.hidden; }; });
  $$('form[data-limits-form]').forEach(f => {
    f.onsubmit = async e => {
      e.preventDefault();
      const body = Object.fromEntries([...f.querySelectorAll('input')].map(i => [i.name, Number(i.value)]));
      if (await act(f.querySelector('button'), () => post(`users/${encodeURIComponent(f.dataset.limitsForm)}/hosting`, body), 'Limits saved: they apply to all of this person\'s apps together.')) usersPage();
    };
  });
  $$('form[data-hosting]').forEach(f => {
    f.onsubmit = async e => {
      e.preventDefault();
      if (await act(f.querySelector('button'), () => post(`users/${encodeURIComponent(f.dataset.hosting)}/hosting`, { disk_gb: Number(f.disk_gb.value) }), `Hosting enabled for ${f.dataset.hosting}.`)) usersPage();
    };
  });
}

function editor(user) {
  const box = $('#user-editor');
  box.hidden = false;
  box.innerHTML = `<h2>Edit ${esc(user.username)}</h2><form class="form" id="edit-user">
    <label class="field">Role<select name="role"><option value="member" ${user.role === 'member' ? 'selected' : ''}>member</option><option value="admin" ${user.role === 'admin' ? 'selected' : ''}>administrator</option></select></label>
    <label class="field">New password (optional)<input name="password" type="password" autocomplete="new-password"></label>
    <label class="field"><span><input type="checkbox" name="disabled" value="1" ${user.disabled ? 'checked' : ''}> Disabled</span></label>
    <button type="submit">Save</button><button type="button" class="quiet" id="cancel-edit">Cancel</button></form>
    <p class="subtle mt">Changing the role, disabling or a new password signs them out. Which sites a member sees is set on each site's page.</p>`;
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  $('#cancel-edit').onclick = () => { box.hidden = true; };
  $('#edit-user').onsubmit = async e => {
    e.preventDefault();
    const d = form(e.target);
    d.disabled = Boolean(d.disabled);
    if (!d.password) delete d.password;
    if (await act(e.target.querySelector('button'), () => patch(`users/${encodeURIComponent(user.username)}`, d), 'Saved.')) usersPage();
  };
}
