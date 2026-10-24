// A small file manager for one app's folder on the hosting node: its data
// volumes (data/…) and, for apps built from a Dockerfile, its source (src/).
import { post } from './api.js';
import { $, $$, act, ago, esc, notice, size } from './ui.js';

const b64encode = buffer => {
  let binary = '';
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
};
const b64decode = text => Uint8Array.from(atob(text), c => c.charCodeAt(0));
const join = (dir, name) => (dir ? `${dir}/${name}` : name);

export function filesCard(path) {
  return `<div class="card" id="files-card"><div class="spread"><h2>Files</h2><div class="row">
      <label class="button quiet file-pick">Upload<input type="file" id="file-upload" multiple hidden></label>
      <button type="button" class="quiet" id="file-mkdir">New folder</button></div></div>
    <p class="subtle">Your app's data and source on the hosting node. Large files and whole folders: use SFTP (below) or git push.</p>
    <div class="crumbs" id="file-crumbs"></div><div id="file-list" class="mt"><p class="subtle">Loading…</p></div>
    <div id="file-editor" hidden><h3 id="editor-name"></h3><textarea id="editor-text" rows="16" class="code-area"></textarea>
      <div class="row mt"><button type="button" id="editor-save">Save</button><button type="button" class="quiet" id="editor-close">Close</button></div></div></div>`;
}

export function bindFiles(path, start = '') {
  let dir = start;
  const call = (op, args) => post(`${path}/files`, { op, ...args });
  async function load() {
    try {
      const data = await call('list', { path: dir });
      const parts = dir ? dir.split('/') : [];
      $('#file-crumbs').innerHTML = [`<a href="#" data-dir="">app</a>`, ...parts.map((p, i) => `<a href="#" data-dir="${esc(parts.slice(0, i + 1).join('/'))}">${esc(p)}</a>`)].join(' / ');
      $('#file-list').innerHTML = data.items.length ? `<div class="table-wrap"><table class="files"><thead><tr><th>Name</th><th class="hide-sm">Size</th><th class="hide-sm">Changed</th><th></th></tr></thead><tbody>${data.items.map(f => `<tr>
        <td>${f.dir ? `<a href="#" data-dir="${esc(join(dir, f.name))}">${esc(f.name)}/</a>` : `<a href="#" data-open="${esc(join(dir, f.name))}">${esc(f.name)}</a>`}</td>
        <td class="hide-sm">${f.dir ? '' : esc(size(f.size))}</td><td class="subtle hide-sm">${esc(ago(f.modified))}</td>
        <td class="actions">${f.dir ? '' : `<button type="button" class="quiet" data-download="${esc(join(dir, f.name))}">Download</button> `}<button type="button" class="quiet" data-rename="${esc(join(dir, f.name))}">Rename</button> <button type="button" class="danger" data-delete-file="${esc(join(dir, f.name))}">Delete</button></td></tr>`).join('')}</tbody></table></div>` : '<p class="subtle">Empty folder. Upload files, or use SFTP.</p>';
      bindList();
    } catch (error) { $('#file-list').innerHTML = `<p class="subtle">${esc(error.message)}</p>`; }
  }
  function bindList() {
    $$('#files-card [data-dir]').forEach(a => { a.onclick = e => { e.preventDefault(); dir = a.dataset.dir; $('#file-editor').hidden = true; load(); }; });
    $$('[data-download]').forEach(b => {
      b.onclick = async () => {
        const r = await act(b, () => call('read', { path: b.dataset.download }), null);
        if (!r) return;
        const url = URL.createObjectURL(new Blob([b64decode(r.content)]));
        const a = document.createElement('a');
        a.href = url; a.download = b.dataset.download.split('/').pop(); a.click();
        setTimeout(() => URL.revokeObjectURL(url), 5000);
      };
    });
    $$('[data-open]').forEach(a => {
      a.onclick = async e => {
        e.preventDefault();
        try {
          const r = await call('read', { path: a.dataset.open });
          const text = new TextDecoder('utf-8', { fatal: true }).decode(b64decode(r.content));
          $('#file-editor').hidden = false; $('#editor-name').textContent = a.dataset.open; $('#editor-text').value = text;
          $('#editor-text').scrollIntoView({ behavior: 'smooth', block: 'center' });
        } catch (error) { notice(error instanceof TypeError ? 'Not a text file: use Download.' : error.message, true); }
      };
    });
    $$('[data-delete-file]').forEach(b => { b.onclick = async () => { if (confirm(`Delete ${b.dataset.deleteFile}?`) && await act(b, () => call('delete', { path: b.dataset.deleteFile }), 'Deleted.')) load(); }; });
    $$('[data-rename]').forEach(b => {
      b.onclick = async () => {
        const name = prompt('New name:', b.dataset.rename.split('/').pop());
        if (name && await act(b, () => call('rename', { path: b.dataset.rename, to: join(dir, name) }), 'Renamed.')) load();
      };
    });
  }
  $('#file-upload').onchange = async e => {
    for (const file of e.target.files) {
      if (file.size > 8 * 1024 * 1024) { notice(`${file.name} is over 8 MB: use SFTP.`, true); continue; }
      try { await call('write', { path: join(dir, file.name), content: b64encode(await file.arrayBuffer()) }); notice(`Uploaded ${file.name}.`); } catch (error) { notice(error.message, true); }
    }
    e.target.value = '';
    load();
  };
  $('#file-mkdir').onclick = async e => { const name = prompt('Folder name:'); if (name && await act(e.target, () => call('mkdir', { path: join(dir, name) }), 'Created.')) load(); };
  $('#editor-close').onclick = () => { $('#file-editor').hidden = true; };
  $('#editor-save').onclick = e => act(e.target, () => call('write', { path: $('#editor-name').textContent, content: b64encode(new TextEncoder().encode($('#editor-text').value)) }), 'Saved.');
  load();
}
