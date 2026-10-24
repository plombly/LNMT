// Small line charts (inline SVG) for the usage the monitor records every
// two minutes: requests per site, CPU and memory per app. A day shows the
// two-minute points, a week the hourly averages.
import { $, $$, esc, size } from './ui.js';

const W = 600, H = 120, PAD = 4;

export const chartCard = (id, title, note = '') => `<div class="card" id="${id}"><div class="spread"><h2>${esc(title)}</h2>
  <div class="segmented"><button type="button" class="active" data-range="1">Day</button><button type="button" data-range="7">Week</button></div></div>
  ${note ? `<p class="subtle">${esc(note)}</p>` : ''}<div class="charts mt"><p class="subtle">Loading…</p></div></div>`;

const formats = {
  rate: v => (v >= 10 ? Math.round(v).toString() : v.toFixed(1)),
  percent: v => `${v >= 10 ? Math.round(v) : v.toFixed(1)}%`,
  bytes: v => size(v),
};

function chart(points, index, label, format, days) {
  const now = Date.now() / 1000;
  const shown = points.filter(p => now - p[0] < days * 86400);
  if (shown.length < 2) return `<div class="chart"><div class="spread"><b>${esc(label)}</b></div><p class="subtle">Not enough data yet: a point is recorded every 2 minutes.</p></div>`;
  const start = now - days * 86400;
  const values = shown.map(p => p[index]);
  const top = Math.max(...values) || 1;
  const x = t => PAD + ((t - start) / (now - start)) * (W - 2 * PAD);
  const y = v => H - PAD - (v / top) * (H - 2 * PAD);
  const line = shown.map(p => `${x(p[0]).toFixed(1)},${y(p[index]).toFixed(1)}`).join(' ');
  const area = `${x(shown[0][0]).toFixed(1)},${H - PAD} ${line} ${x(shown.at(-1)[0]).toFixed(1)},${H - PAD}`;
  const average = values.reduce((a, b) => a + b, 0) / values.length;
  return `<div class="chart"><div class="spread"><b>${esc(label)}</b><span class="subtle">now ${esc(formats[format](values.at(-1)))} · average ${esc(formats[format](average))} · peak ${esc(formats[format](top))}</span></div>
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(label)}"><polygon class="chart-area" points="${area}"/><polyline class="chart-line" points="${line}"/></svg>
    <div class="spread subtle chart-axis"><span>${days === 1 ? '24 h ago' : '7 days ago'}</span><span>now</span></div></div>`;
}

// series: [[column index, label, format], …]
export async function bindChart(id, load, series) {
  const card = $(`#${id}`);
  if (!card) return;
  let points = [];
  const draw = days => {
    $$(`#${id} [data-range]`).forEach(b => b.classList.toggle('active', Number(b.dataset.range) === days));
    card.querySelector('.charts').innerHTML = series.map(([index, label, format]) => chart(points, index, label, format, days)).join('');
  };
  $$(`#${id} [data-range]`).forEach(b => { b.onclick = () => draw(Number(b.dataset.range)); });
  try {
    points = (await load()).points || [];
    draw(1);
  } catch (error) {
    card.querySelector('.charts').innerHTML = `<p class="subtle">${esc(error.message)}</p>`;
  }
}
