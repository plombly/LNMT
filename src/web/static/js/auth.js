// Signing in, the first administrator (setup code) and invite links.
import { api, post, session } from './api.js';
import { $, esc, form, main, notice } from './ui.js';

function signedIn(me) {
  session.me = me;
  notice('');
  // Setting the hash it already has fires no hashchange (e.g. signed out
  // while on #/sites): the page would stay on the sign-in form.
  if (location.hash === '#/sites') window.dispatchEvent(new HashChangeEvent('hashchange'));
  else location.hash = '#/sites';
}

export async function loginPage() {
  $('#topbar').hidden = true;
  let setup = false;
  try { setup = (await api('setup')).needed; } catch { /* sign in instead */ }
  main().innerHTML = setup
    ? `<div class="card login"><h1>Set up</h1><p class="subtle">Create the first administrator. Get a setup code on the server with <code>sudo lnmt-web setup-code</code>.</p>
       <form class="form stack" id="auth-form"><label class="field">Setup code<input name="code" autocomplete="off" required></label>
       <label class="field">User name<input name="username" autocomplete="username" required></label>
       <label class="field">Password (10+ characters)<input name="password" type="password" autocomplete="new-password" required></label>
       <button type="submit">Create administrator</button></form></div>`
    : `<div class="card login"><h1>Sign in</h1><p class="subtle">LAIka NGINx Management Terminal</p><form class="form stack" id="auth-form">
       <label class="field">User name<input name="username" autocomplete="username" required autofocus></label>
       <label class="field">Password<input name="password" type="password" autocomplete="current-password" required></label>
       <button type="submit">Sign in</button></form><div id="laika-sign-in"></div><p class="subtle mt">No account? Ask an administrator for an invite link.</p></div>`;
  if (!setup) {
    api('laika/available').then(r => {
      if (r.available) $('#laika-sign-in').innerHTML = '<div class="or">or</div><a class="button wide-button" href="/api/laika/login">Sign in with LAIka</a>';
    }).catch(() => {});
    if (location.hash === '#/login-failed') notice('Signing in with LAIka did not work (expired, cancelled, or no account here). Try again.', true);
  }
  const el = $('#auth-form');
  el.addEventListener('submit', async event => {
    event.preventDefault();
    try { signedIn(await post(setup ? 'setup' : 'login', form(el))); } catch (error) { notice(error.message, true); }
  });
}

export async function invitePage(token) {
  $('#topbar').hidden = true;
  let invite;
  try {
    invite = await api(`invite/${encodeURIComponent(token)}`);
  } catch (error) {
    main().innerHTML = `<div class="card login"><h1>Invite</h1><p>${esc(error.message)}</p><a href="#/">Sign in</a></div>`;
    return;
  }
  main().innerHTML = `<div class="card login"><h1>Join ${esc(invite.server_name)}</h1>
    <p class="subtle">You were invited as ${invite.role === 'admin' ? 'an <b>administrator</b>' : 'a <b>member</b>: an administrator then shares sites with you'}. Choose your user name and password. The link works once, until ${esc(new Date(invite.expires * 1000).toLocaleString())}.</p>
    <form class="form stack" id="invite-form"><label class="field">User name<input name="username" autocomplete="username" required autofocus></label>
    <label class="field">Password (10+ characters)<input name="password" type="password" autocomplete="new-password" required></label>
    <button type="submit">Create my account</button></form></div>`;
  const el = $('#invite-form');
  el.addEventListener('submit', async event => {
    event.preventDefault();
    try { signedIn(await post(`invite/${encodeURIComponent(token)}`, form(el))); } catch (error) { notice(error.message, true); }
  });
}
