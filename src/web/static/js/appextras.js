// App page extras: database export / import, scheduled tasks, deploying
// from LAIka, and the usage chart.
import { api, post } from './api.js';
import { bindChart, chartCard } from './charts.js';
import { $, $$, act, copyButton, esc, notice } from './ui.js';

const isDatabase = a => /(^|\/)(mariadb|mysql|postgres)[:@]/.test(a.image);

export const usageCard = () => chartCard('usage', 'Usage', 'CPU and memory of this app, measured every 2 minutes.');

export function databaseCard(a) {
  if (!isDatabase(a)) return '';
  return `<div class="card"><h2>Database</h2>
    <p class="subtle">Export writes a complete SQL dump into this app's <code>backups/</code> folder (download it from Files, or with SFTP for big ones). Import loads an <code>.sql</code> file you put in the app's folder into this database.</p>
    <div class="row"><button type="button" id="db-export">Export now</button></div>
    <form class="form mt" id="db-import"><label class="field">SQL file in the app's folder<input name="path" placeholder="backups/shop-20261020-120000.sql" required></label><button type="submit" class="quiet">Import</button></form>
    <p class="subtle mt">For a web admin, create an app of the kind <b>Adminer</b>, publish it, and turn on password protection for its site; it reaches this database by the name <code>${esc(a.name)}</code>.</p></div>`;
}

const taskRow = (t = {}) => `<tr class="task-row"><td><input name="name" value="${esc(t.name || '')}" placeholder="nightly" pattern="[a-z][a-z0-9-]{0,30}" aria-label="Name"></td>
  <td><input name="schedule" value="${esc(t.schedule || '')}" placeholder="daily" aria-label="Schedule"></td>
  <td><input name="command" value="${esc(t.command || '')}" placeholder="php /var/www/html/cron.php" aria-label="Command"></td>
  <td class="actions">${t.name ? `<button type="button" class="quiet small" data-task-run="${esc(t.name)}">Run now</button> <button type="button" class="quiet small" data-task-log="${esc(t.name)}">Log</button> ` : ''}<button type="button" class="danger small" data-task-drop>Remove</button></td></tr>`;

export function tasksCard(a) {
  return `<div class="card" id="tasks-card"><h2>Scheduled tasks</h2>
    <p class="subtle">Commands run inside this app's container on a schedule: <code>hourly</code>, <code>daily</code>, <code>weekly</code>, <code>*-*-* 03:30</code> (every day at 03:30), <code>Mon *-*-* 08:00</code>, <code>*:0/15</code> (every 15 minutes). Missed runs happen once the app is back. Output goes to <code>tasks/&lt;name&gt;.log</code>.</p>
    <div class="table-wrap"><table class="tasks"><thead><tr><th>Name</th><th>Schedule</th><th>Command</th><th></th></tr></thead><tbody id="task-rows">${(a.tasks || []).map(taskRow).join('')}</tbody></table></div>
    <div class="row mt"><button type="button" class="quiet" id="task-add">Add a task</button><button type="button" id="tasks-save">Save tasks</button></div>
    <pre id="task-log" class="mt" hidden></pre></div>`;
}

export function laikaCard(a) {
  if (!a.build) return '';
  const url = `ssh://c-${a.customer}@${a.node_ip}/${a.name}.git`;
  return `<div class="card"><h2>Deploy from LAIka</h2>
    <p class="subtle">A LAIka project can deploy here on its own: every change you approve there is pushed to this app, built and put live.</p>
    <ol class="steps"><li>In LAIka, open the project's <b>Settings</b> and set its git push address to <code>${esc(url)}</code> ${copyButton(url)}</li>
      <li>LAIka shows a deploy key: add it to <a href="#/account">your SSH keys</a> here.</li>
      <li>The project needs a <code>Dockerfile</code> at its top level. The next approved change deploys.</li></ol></div>`;
}

export function bindExtras(a, path, reload) {
  bindChart('usage', () => api(`${path}/usage`), [[1, 'CPU', 'percent'], [2, 'Memory', 'bytes']]);
  const exportButton = $('#db-export');
  if (exportButton) exportButton.onclick = () => act(exportButton, () => post(`${path}/db`, { op: 'export' }), r => `Exported to ${r.file}.`);
  const importForm = $('#db-import');
  if (importForm) importForm.onsubmit = async e => {
    e.preventDefault();
    if (!confirm('Import this file into the database? Tables in the file replace the existing ones.')) return;
    await act(importForm.querySelector('button'), () => post(`${path}/db`, { op: 'import', path: importForm.path.value.trim() }), 'Imported.');
  };
  const rows = $('#task-rows');
  if (!rows) return;
  const bindRows = () => {
    $$('[data-task-drop]').forEach(b => { b.onclick = () => b.closest('tr').remove(); });
    $$('[data-task-run]').forEach(b => { b.onclick = () => act(b, () => post(`${path}/tasks`, { run: b.dataset.taskRun }), 'Started; see its log in a moment.'); });
    $$('[data-task-log]').forEach(b => {
      b.onclick = async () => {
        try {
          const r = await post(`${path}/files`, { op: 'read', path: `tasks/${b.dataset.taskLog}.log` });
          const text = new TextDecoder().decode(Uint8Array.from(atob(r.content), c => c.charCodeAt(0)));
          $('#task-log').hidden = false;
          $('#task-log').textContent = text.slice(-6000) || '(empty)';
        } catch { notice('No log yet: the task has not run.', true); }
      };
    });
  };
  bindRows();
  $('#task-add').onclick = () => { rows.insertAdjacentHTML('beforeend', taskRow()); bindRows(); };
  $('#tasks-save').onclick = async e => {
    const tasks = $$('#task-rows .task-row').map(tr => Object.fromEntries([...tr.querySelectorAll('input')].map(i => [i.name, i.value.trim()])))
      .filter(t => t.name || t.command);
    if (await act(e.target, () => post(`${path}/tasks`, { tasks }), r => `${r.tasks.length} task(s) scheduled.`)) reload();
  };
}
