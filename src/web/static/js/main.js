// Router and top bar. Pages live in their own modules.
import { api, isAdmin, post, session } from './api.js';
import { accountPage } from './account.js';
import { appPage, appsPage, newAppPage } from './apps.js';
import { domainsPage } from './domains.js';
import { invitePage, loginPage } from './auth.js';
import { applyPrefs, prefsOf } from './prefs.js';
import { sitePage } from './site.js';
import { addPage, sitesPage } from './sites.js';
import { activityPage, backupsPage, systemPage } from './system.js';
import { usersPage } from './users.js';
import { $, esc, main } from './ui.js';

const PAGES = { sites: sitesPage, add: addPage, account: accountPage, apps: appsPage, 'new-app': newAppPage, domains: domainsPage };
const ADMIN_PAGES = { system: systemPage, cloudflare: domainsPage, backups: backupsPage, users: usersPage, activity: activityPage };
const TABS = [['sites', 'Sites'], ['apps', 'Apps'], ['domains', 'Domains'], ['system', 'System', true], ['backups', 'Backups', true], ['users', 'Users', true], ['activity', 'Activity', true]];

function topbar(page) {
  $('#topbar').hidden = false;
  $('#nav').innerHTML = TABS.filter(([, , need]) => !need || isAdmin() || (need === 'hosting' && session.me.hosting))
    .map(([id, label]) => `<a href="#/${id}" class="${page === id || (id === 'sites' && page === 'add') || (id === 'apps' && page === 'new-app') ? 'active' : ''}">${label}</a>`).join('');
  $('#who').textContent = `${session.me.username}${isAdmin() ? ' · admin' : ''}`;
}

async function render() {
  const [page, arg] = location.hash.slice(2).split('/').map(decodeURIComponent);
  if (page === 'invite' && arg) return invitePage(arg);
  if (!session.me) {
    try { session.me = await api('me'); } catch {
      try { applyPrefs(prefsOf(null), await api('settings')); } catch { /* defaults */ }
      return loginPage();
    }
  }
  applyPrefs(prefsOf(session.me), session.me.settings);
  topbar(page || 'sites');
  if (render.last !== location.hash) main().innerHTML = '<p class="subtle"><span class="spinner"></span> Loading…</p>';
  render.last = location.hash;
  try {
    if (page === 'sites' && arg) await sitePage(arg);
    else if (page === 'apps' && arg) await appPage(arg, location.hash.slice(2).split('/').map(decodeURIComponent)[2]);
    else if (PAGES[page]) await PAGES[page](arg);
    else if (ADMIN_PAGES[page] && isAdmin()) await ADMIN_PAGES[page]();
    else location.hash = '#/sites';
  } catch (error) {
    if (session.me) main().innerHTML = `<div class="empty">${esc(error.message)}</div>`;
  }
}

session.onSignedOut = render;
window.addEventListener('hashchange', render);
document.addEventListener('click', async event => {
  if (!event.target.closest('[data-action="logout"]')) return;
  try { await post('logout'); } catch { /* signed out anyway */ }
  session.me = null;
  location.hash = '';
  render();
});
render();
