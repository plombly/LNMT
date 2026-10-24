// Talking to the dashboard's API: the session cookie plus the CSRF token on
// every change. `session` holds who is signed in.
export const session = { me: null, onSignedOut: () => {} };

export async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (session.me?.csrf) headers['X-CSRF-Token'] = session.me.csrf;
  const response = await fetch(`/api/${path}`, { credentials: 'same-origin', ...options, headers });
  let body = {};
  try { body = await response.json(); } catch { /* empty body */ }
  if (response.status === 401 && session.me && path !== 'login') {  // signed out meanwhile
    session.me = null;
    session.onSignedOut();
  }
  if (!response.ok) throw new Error(body.detail || `${response.status} ${response.statusText}`);
  return body;
}

export const post = (path, data = {}) => api(path, { method: 'POST', body: JSON.stringify(data) });
export const patch = (path, data = {}) => api(path, { method: 'PATCH', body: JSON.stringify(data) });
export const del = path => api(path, { method: 'DELETE' });
export const isAdmin = () => session.me?.role === 'admin';
