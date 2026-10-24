// Appearance: theme, accent, density (per person, saved on the server) and
// the server's name (administrators). Applied to <html> at once.
export const ACCENTS = ['#5b9dff', '#3fcf8e', '#b48cff', '#ff9f5a', '#ef5d6c', '#3cc8c8'];
export const DEFAULTS = { theme: 'system', accent: '#5b9dff', density: 'comfortable', sites_view: 'cards', sites_sort: 'name' };

export function prefsOf(me) {
  return { ...DEFAULTS, ...(me?.prefs || {}) };
}

export function applyPrefs(prefs, settings = {}) {
  const root = document.documentElement;
  root.dataset.theme = prefs.theme;
  root.dataset.density = prefs.density;
  root.style.setProperty('--accent', prefs.accent);
  const name = settings.server_name || 'LNMT';
  document.title = name === 'LNMT' ? 'LNMT · LAIka NGINx Management Terminal' : name;
  const brand = document.getElementById('brand-name');
  if (brand) { brand.textContent = name; brand.title = 'LAIka NGINx Management Terminal'; }
}
