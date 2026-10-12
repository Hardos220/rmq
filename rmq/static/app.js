'use strict';
(() => {
  // ================================================================ helpers
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  function h(tag, props, ...kids) {
    const e = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v == null || v === false) continue;
        if (k === 'class') e.className = v;
        else if (k === 'text') e.textContent = v;
        else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
        else if (k === 'value') e.value = v;
        else e.setAttribute(k, v === true ? '' : v);
      }
    }
    for (const k of kids.flat()) {
      if (k == null || k === false) continue;
      e.append(k instanceof Node ? k : document.createTextNode(String(k)));
    }
    return e;
  }
  const setText = (el, text) => { if (el.textContent !== text) el.textContent = text; };
  // replaceChildren() would turn null into the text "null"
  const fill = (el, ...kids) => el.replaceChildren(...kids.flat().filter((k) => k != null && k !== false));

  const nf1 = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const nf2 = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 });
  const n1 = (v) => nf1.format(v || 0);
  const gb = (v) => (v == null ? '–' : v > 0 && v < 0.95 ? `${Math.round(v * 1024)} МБ` : `${n1(v)} ГБ`);
  const pct = (v) => `${Math.round(v || 0)}%`;
  const num = (v) => (v == null ? '–' : nf2.format(v));
  function dur(s) {
    if (s == null) return '–';
    s = Math.max(0, Math.round(s));
    const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (d) return `${d}д ${hh}ч`;
    if (hh) return `${hh}ч ${String(m).padStart(2, '0')}м`;
    if (m) return `${m}м ${String(sec).padStart(2, '0')}с`;
    return `${sec}с`;
  }
  const clock = (t, sec) => new Date(t * 1000).toLocaleTimeString('ru-RU', sec
    ? { hour: '2-digit', minute: '2-digit', second: '2-digit' } : { hour: '2-digit', minute: '2-digit' });
  const stamp = (t) => (t ? new Date(t * 1000).toLocaleString('ru-RU', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  }) : '–');
  const PRIO = { low: 'низкий', normal: 'обычный', high: 'высокий' };
  const GLYPH = { running: '▶', suspended: '❚❚', queued: '◷', done: '✓', failed: '✕', cancelled: '–', timeout: '⏱', lost: '?', hung: '!', remote: '⇄' };

  // ================================================================ state & API
  let TOKEN = null;
  let STATE = null;
  let SETTINGS = null;
  let RANGE = 900;
  let METRICS = null;
  let TAB = 'overview';
  let offline = false;

  async function api(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch { /* empty */ }
    if (res.status === 401) {
      showLogin('Токен не подошёл.');
      throw new Error('unauthorized');
    }
    if (!res.ok) throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
    return data;
  }

  let START_TAB = 'overview';
  let START_TASK = null;
  function initToken() {
    const m = location.hash.match(/token=([^&]+)/);
    const t = location.hash.match(/tab=(\w+)/);
    if (t) START_TAB = t[1];
    const k = location.hash.match(/task=(\d+)/);
    if (k) START_TASK = Number(k[1]);
    const sm = location.hash.match(/smooth=([01])/);
    if (sm) {
      SMOOTH = sm[1] === '1';
      try { localStorage.setItem('rmq_smooth', sm[1]); } catch { /* ignore */ }
    }
    if (m) {
      TOKEN = decodeURIComponent(m[1]);
      try { localStorage.setItem('rmq_token', TOKEN); } catch { /* private mode */ }
    } else {
      try { TOKEN = localStorage.getItem('rmq_token'); } catch { TOKEN = null; }
    }
    if (location.hash) history.replaceState(null, '', location.pathname);
  }

  function showLogin(msg) {
    $('#app').hidden = true;
    $('#login').hidden = false;
    setText($('#login-msg'), msg || '');
  }

  $('#login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    TOKEN = $('#login-token').value.trim();
    try {
      await api('GET', '/api/whoami');
      try { localStorage.setItem('rmq_token', TOKEN); } catch { /* ignore */ }
      $('#login').hidden = true;
      start();
    } catch (err) {
      setText($('#login-msg'), err.message === 'unauthorized' ? 'Токен не подошёл.' : err.message);
    }
  });

  // ================================================================ theme
  const THEMES = ['auto', 'light', 'dark'];
  const THEME_NAMES = { auto: 'Тема: авто', light: 'Тема: светлая', dark: 'Тема: тёмная' };
  function applyTheme(t) {
    if (t === 'auto') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = t;
    setText($('#theme-btn'), THEME_NAMES[t]);
    charts.forEach((c) => c.draw());
    tempCharts.forEach((c) => c.draw());
    statCharts.forEach((c) => c.draw());
    chronCharts.forEach((c) => c.update());
  }
  let theme = 'auto';
  try { theme = localStorage.getItem('rmq_theme') || 'auto'; } catch { /* ignore */ }
  $('#theme-btn').addEventListener('click', () => {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    try { localStorage.setItem('rmq_theme', theme); } catch { /* ignore */ }
    applyTheme(theme);
  });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    [...charts, ...tempCharts, ...statCharts].forEach((c) => c.draw());
    chronCharts.forEach((c) => c.update());
  });

  // ================================================================ charts
  function niceStep(raw) {
    const p = 10 ** Math.floor(Math.log10(raw));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= raw) return m * p;
    return 10 * p;
  }
  function yTicks(max, n) {
    const step = niceStep(max / n);
    const out = [];
    for (let v = 0; v <= max + 1e-9; v += step) out.push(+v.toFixed(6));
    return out;
  }
  const TIME_STEPS = [15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600];
  function xTicks(t0, t1, width) {
    const maxLabels = Math.max(2, Math.floor(width / 72));
    const step = TIME_STEPS.find((s) => (t1 - t0) / s <= maxLabels) || 21600;
    const off = -new Date().getTimezoneOffset() * 60;
    const out = [];
    for (let t = Math.ceil((t0 + off) / step) * step - off; t <= t1; t += step) out.push(t);
    return { ticks: out, step };
  }
  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function rgba(hex, a) {
    const v = hex.replace('#', '');
    const n = parseInt(v.length === 3 ? v.split('').map((c) => c + c).join('') : v, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  }

  const tooltip = $('#tooltip');

  // ================================================================ optional 5-second moving average
  // A trailing time window: each point is the mean of the samples from the last 5 seconds. Gaps in the
  // data stay gaps and missing values (a sleeping GPU) are skipped, never counted as zeros.
  const SMOOTH_S = 5;
  const STATE_COLUMNS = new Set(['t', 'game', 'heat', 'running', 'queued', 'suspended']);
  let SMOOTH = false;
  try { SMOOTH = localStorage.getItem('rmq_smooth') === '1'; } catch { /* ignore */ }
  let smoothCache = null;
  function chartData() {
    const m = METRICS;
    if (!SMOOTH || !m || !m.t.length) return m;
    const last = m.t[m.t.length - 1];
    if (smoothCache && smoothCache.src === m && smoothCache.len === m.t.length && smoothCache.last === last) return smoothCache.out;
    const out = { step: m.step, t: m.t };
    for (const k of Object.keys(m)) {
      if (!Array.isArray(m[k]) || k === 't') continue;
      if (STATE_COLUMNS.has(k)) { out[k] = m[k]; continue; }
      const v = m[k];
      const r = new Array(v.length);
      let lo = 0; let sum = 0; let n = 0;
      for (let i = 0; i < v.length; i += 1) {
        if (v[i] != null) { sum += v[i]; n += 1; }
        while (m.t[lo] <= m.t[i] - SMOOTH_S) {
          if (v[lo] != null) { sum -= v[lo]; n -= 1; }
          lo += 1;
        }
        r[i] = v[i] == null || !n ? null : sum / n;
      }
      out[k] = r;
    }
    smoothCache = { src: m, len: m.t.length, last, out };
    return out;
  }
  const smoothNote = () => (SMOOTH ? ' · среднее за 5 с' : '');

  class Chart {
    constructor(def) {
      this.def = def;
      this.hover = null;
      this.showTable = false;
      this.tbtn = h('button', { class: 'btn tbtn', type: 'button', 'aria-pressed': 'false', onclick: () => this.toggleTable() }, 'Таблица');
      this.sub = h('span', { class: 'sub' });
      this.legend = h('div', { class: 'legend' });
      this.canvas = h('canvas', { role: 'img', 'aria-label': `${def.title}: график` });
      this.plot = h('div', { class: 'plot' }, this.canvas);
      this.tableEl = h('div', { class: 'table-view', hidden: true });
      this.na = h('div', { class: 'na', hidden: true }, 'Видеокарта не найдена');
      this.root = h('section', { class: 'chart', 'aria-label': def.title },
        h('header', null, h('span', { class: 'title' }, def.title), this.sub, this.tbtn),
        this.legend, this.plot, this.tableEl, this.na);
      this.canvas.addEventListener('pointermove', (e) => this.onMove(e));
      this.canvas.addEventListener('pointerleave', () => { this.hover = null; tooltip.hidden = true; this.draw(); });
      new ResizeObserver(() => this.draw()).observe(this.plot);
    }

    available() { return !this.def.needsGpu || (STATE && STATE.system.vram); }

    toggleTable() {
      this.showTable = !this.showTable;
      this.tbtn.setAttribute('aria-pressed', String(this.showTable));
      setText(this.tbtn, this.showTable ? 'График' : 'Таблица');
      this.update();
    }

    series() {
      const d = this.def;
      const list = [
        { key: d.user, label: 'Вы и система', color: '--s-user', kind: 'area' },
        { key: d.tasks, label: 'Задачи агентов', color: '--s-tasks', kind: 'area' },
      ];
      if (d.limit) list.push({ key: d.limit, label: 'Граница для задач', color: '--ink-2', kind: 'dash' });
      return list;
    }

    update() {
      const ok = this.available();
      this.na.hidden = !!ok;
      this.plot.hidden = !ok || this.showTable;
      this.tableEl.hidden = !ok || !this.showTable;
      this.legend.hidden = !ok;
      this.tbtn.hidden = !ok;
      if (!ok || !STATE) return;
      setText(this.sub, this.def.sub(STATE));
      this.renderLegend();
      if (this.showTable) this.renderTable(); else this.draw();
    }

    last(key) {
      const d = chartData();
      const arr = d && d[key];
      return arr && arr.length ? arr[arr.length - 1] : null;
    }

    renderLegend() {
      const f = this.def.fmt;
      const items = this.series().map((s) => h('span', null,
        h('span', { class: `key ${s.kind === 'dash' ? 'line' : s.key.endsWith('user') ? 'user' : 'tasks'}` }),
        `${s.label} `, h('b', null, this.last(s.key) == null ? '–' : f(this.last(s.key)))));
      if (this.def.threshold) {
        items.push(h('span', null, h('span', { class: 'key thr' }), 'Порог паузы GPU-задач ', h('b', null, pct(this.def.threshold(STATE)))));
      }
      this.legend.replaceChildren(...items);
    }

    renderTable() {
      const m = chartData();
      if (!m || !m.t.length) { this.tableEl.replaceChildren(h('p', { class: 'muted' }, 'Нет данных')); return; }
      const n = m.t.length;
      const stride = Math.max(1, Math.floor(n / 30));
      const rows = [];
      for (let i = n - 1; i >= 0; i -= stride) rows.push(i);
      const cols = this.series();
      const f = this.def.fmt;
      this.tableEl.replaceChildren(h('table', null,
        h('thead', null, h('tr', null, h('th', null, `Время${smoothNote()}`), cols.map((c) => h('th', { class: 'num' }, c.label)))),
        h('tbody', null, rows.map((i) => h('tr', null, h('td', { class: 'num' }, clock(m.t[i], true)),
          cols.map((c) => h('td', { class: 'num' }, f(m[c.key][i]))))))));
    }

    geometry() {
      const w = this.plot.clientWidth;
      const ht = this.plot.clientHeight;
      const pad = { l: 46, r: 12, t: 8, b: 24 };
      const max = Math.max(1e-6, this.def.max(STATE) || 1);
      const t1 = METRICS && METRICS.t.length ? METRICS.t[METRICS.t.length - 1] : Date.now() / 1000;
      const t0 = t1 - RANGE;
      const pw = Math.max(10, w - pad.l - pad.r);
      const ph = Math.max(10, ht - pad.t - pad.b);
      return {
        w, ht, pad, max, t0, t1, pw, ph,
        X: (t) => pad.l + ((t - t0) / (t1 - t0)) * pw,
        Y: (v) => pad.t + ph - (Math.max(0, Math.min(max, v)) / max) * ph,
      };
    }

    segments() {
      // split the series where samples are missing (manager was not running)
      const t = METRICS.t;
      const gapAt = Math.max(3, (METRICS.step || 1) * 3.5);
      const segs = [];
      let start = 0;
      for (let i = 1; i <= t.length; i += 1) {
        if (i === t.length || t[i] - t[i - 1] > gapAt) { segs.push([start, i - 1]); start = i; }
      }
      return segs;
    }

    draw() {
      if (this.plot.hidden || !STATE) return;
      const dpr = window.devicePixelRatio || 1;
      const g = this.geometry();
      this.canvas.width = Math.round(g.w * dpr);
      this.canvas.height = Math.round(g.ht * dpr);
      const c = this.canvas.getContext('2d');
      c.setTransform(dpr, 0, 0, dpr, 0, 0);
      c.clearRect(0, 0, g.w, g.ht);
      const col = {
        grid: cssVar('--grid'), axis: cssVar('--axis'), muted: cssVar('--muted'), ink2: cssVar('--ink-2'),
        surface: cssVar('--surface'), user: cssVar('--s-user'), tasks: cssVar('--s-tasks'), game: cssVar('--game-band'),
      };
      c.font = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
      const m = chartData();
      const has = m && m.t.length > 1;

      // game-mode periods as a quiet background band
      if (has) {
        c.fillStyle = col.game;
        let s = null;
        for (let i = 0; i <= m.t.length; i += 1) {
          const on = i < m.t.length && m.game[i] > 0.5;
          if (on && s === null) s = i;
          if (!on && s !== null) {
            const x0 = Math.max(g.pad.l, g.X(m.t[s]));
            const x1 = g.X(m.t[i - 1]);
            c.fillRect(x0, g.pad.t, Math.max(1, x1 - x0), g.ph);
            s = null;
          }
        }
      }
      // grid and y labels
      c.lineWidth = 1;
      c.textAlign = 'right';
      c.textBaseline = 'middle';
      for (const v of yTicks(g.max, 4)) {
        const y = Math.round(g.Y(v)) + 0.5;
        c.strokeStyle = v === 0 ? col.axis : col.grid;
        c.beginPath(); c.moveTo(g.pad.l, y); c.lineTo(g.w - g.pad.r, y); c.stroke();
        c.fillStyle = col.muted;
        c.fillText(this.def.tick(v), g.pad.l - 6, y);
      }
      // x labels
      c.textAlign = 'center';
      c.textBaseline = 'top';
      for (const t of xTicks(g.t0, g.t1, g.pw).ticks) {
        const x = g.X(t);
        if (x < g.pad.l + 14 || x > g.w - g.pad.r - 14) continue;
        c.fillStyle = col.muted;
        c.fillText(clock(t), x, g.pad.t + g.ph + 6);
      }
      if (!has) {
        c.fillStyle = col.muted;
        c.textBaseline = 'middle';
        c.fillText('Данные появятся через пару секунд', g.pad.l + g.pw / 2, g.pad.t + g.ph / 2);
        return;
      }
      c.save();
      c.beginPath(); c.rect(g.pad.l, g.pad.t - 2, g.pw, g.ph + 4); c.clip();
      const U = m[this.def.user];
      const T = m[this.def.tasks];
      const segs = this.segments();
      const lo = (i) => (this.def.stacked ? U[i] : 0);
      const band = (base, top, color) => {
        for (const [a, b] of segs) {
          c.beginPath();
          for (let i = a; i <= b; i += 1) c.lineTo(g.X(m.t[i]), g.Y(top(i)));
          for (let i = b; i >= a; i -= 1) c.lineTo(g.X(m.t[i]), g.Y(base(i)));
          c.closePath();
          c.fillStyle = rgba(color, 0.12);
          c.fill();
        }
      };
      const line = (val, color, width, dash) => {
        c.strokeStyle = color; c.lineWidth = width; c.lineJoin = 'round'; c.lineCap = 'round';
        c.setLineDash(dash || []);
        for (const [a, b] of segs) {
          c.beginPath();
          for (let i = a; i <= b; i += 1) c.lineTo(g.X(m.t[i]), g.Y(val(i)));
          c.stroke();
        }
        c.setLineDash([]);
      };
      band(() => 0, (i) => U[i], col.user);
      band(lo, (i) => lo(i) + T[i], col.tasks);
      line((i) => U[i], col.user, 2);
      line((i) => lo(i) + T[i], col.tasks, 2);
      if (this.def.limit) line((i) => m[this.def.limit][i], col.ink2, 1.5, [5, 4]);
      if (this.def.threshold) {
        const y = Math.round(g.Y(this.def.threshold(STATE))) + 0.5;
        c.strokeStyle = col.muted; c.lineWidth = 1.5; c.setLineDash([2, 3]);
        c.beginPath(); c.moveTo(g.pad.l, y); c.lineTo(g.w - g.pad.r, y); c.stroke(); c.setLineDash([]);
      }
      c.restore();
      // hover: crosshair snapped to the nearest sample, ringed dots on each series
      if (this.hover != null && this.hover < m.t.length) {
        const i = this.hover;
        const x = Math.round(g.X(m.t[i])) + 0.5;
        c.strokeStyle = col.axis; c.lineWidth = 1;
        c.beginPath(); c.moveTo(x, g.pad.t); c.lineTo(x, g.pad.t + g.ph); c.stroke();
        const dots = [[U[i], col.user], [lo(i) + T[i], col.tasks]];
        for (const [v, color] of dots) {
          c.beginPath(); c.arc(x, g.Y(v), 4, 0, Math.PI * 2);
          c.fillStyle = color; c.fill();
          c.lineWidth = 2; c.strokeStyle = col.surface; c.stroke();
        }
      }
    }

    onMove(e) {
      const m = chartData();
      if (!m || m.t.length < 2) return;
      const g = this.geometry();
      const rect = this.canvas.getBoundingClientRect();
      const t = g.t0 + ((e.clientX - rect.left - g.pad.l) / g.pw) * (g.t1 - g.t0);
      let lo = 0; let hi = m.t.length - 1;
      while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (m.t[mid] < t) lo = mid; else hi = mid; }
      const i = Math.abs(m.t[lo] - t) < Math.abs(m.t[hi] - t) ? lo : hi;
      this.hover = i;
      this.draw();
      const f = this.def.fmt;
      const row = (val, label, color, dash) => h('div', { class: 'r' },
        h('span', { class: `lk${dash ? ' dash' : ''}`, style: `border-top-color: var(${color})` }),
        h('b', null, val), h('span', null, label));
      tooltip.replaceChildren(
        h('div', { class: 't' }, clock(m.t[i], true) + (m.game[i] > 0.5 ? ' · игровой режим' : '') + smoothNote()),
        ...this.series().map((s) => row(f(m[s.key][i]), s.label, s.color, s.kind === 'dash')));
      tooltip.hidden = false;
      const tw = tooltip.offsetWidth; const th = tooltip.offsetHeight;
      let left = e.clientX + 14; let top = e.clientY + 14;
      if (left + tw > window.innerWidth - 8) left = e.clientX - tw - 14;
      if (top + th > window.innerHeight - 8) top = e.clientY - th - 14;
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${top}px`;
    }
  }

  const fmtPctV = (v) => pct(v);
  const fmtGbV = (v) => `${n1(v)} ГБ`;
  const charts = [
    new Chart({
      title: 'Процессор', user: 'cpu_user', tasks: 'cpu_tasks', limit: 'cpu_limit', stacked: true,
      max: () => 100, fmt: fmtPctV, tick: (v) => `${v}%`,
      sub: (s) => `${s.system.cpu.n} потоков${s.system.cpu.ecores ? `, из них ${s.system.cpu.ecores} на E-ядрах` : ''}`,
    }),
    new Chart({
      title: 'Оперативная память', user: 'ram_user', tasks: 'ram_tasks', limit: 'ram_limit', stacked: true,
      max: (s) => s.system.ram.total, fmt: fmtGbV, tick: (v) => `${nf2.format(v)}`,
      sub: (s) => `${n1(s.system.ram.total)} ГБ всего`,
    }),
    new Chart({
      title: 'Видеопамять', user: 'vram_user', tasks: 'vram_tasks', limit: 'vram_limit', stacked: true, needsGpu: true,
      max: (s) => (s.system.vram ? s.system.vram.total : 1), fmt: fmtGbV, tick: (v) => `${nf2.format(v)}`,
      sub: (s) => (s.system.vram ? `${s.system.vram.name}, ${n1(s.system.vram.total)} ГБ` : ''),
    }),
    new Chart({
      title: 'Загрузка видеокарты', user: 'gpu_user', tasks: 'gpu_tasks', limit: null, stacked: false, needsGpu: true,
      max: () => 100, fmt: fmtPctV, tick: (v) => `${v}%`, threshold: (s) => s.system.gpu.threshold,
      sub: () => 'самый загруженный блок GPU, как в Диспетчере задач',
    }),
  ];
  $('#charts').append(...charts.map((c) => c.root));

  // ================================================================ level charts: temperatures and the regulator's share
  // Small multiples, each on its own fixed scale, with reference lines for the limits and quiet bands for
  // the periods when the regulator held tasks back or everything was frozen.
  const HEAT_NAMES = ['норма', 'регулятор урезает процессор задачам', 'перегрев — расчёты заморожены'];
  class LevelChart {
    // o: { title, series, color, min, max, step, values(), fmt(v), tick(v), refs() -> [{ v, color, dash, cls, label, text }],
    //      sub(), hidden(), empty(), wide }
    constructor(o) {
      this.o = o;
      this.hover = null;
      this.showTable = false;
      this.tbtn = h('button', { class: 'btn tbtn', type: 'button', 'aria-pressed': 'false', onclick: () => this.toggleTable() }, 'Таблица');
      this.sub = h('span', { class: 'sub' });
      this.legend = h('div', { class: 'legend' });
      this.canvas = h('canvas', { role: 'img', 'aria-label': `${o.title}: график` });
      this.plot = h('div', { class: 'plot' }, this.canvas);
      this.tableEl = h('div', { class: 'table-view', hidden: true });
      this.root = h('section', { class: `chart${o.wide ? ' wide' : ''}`, 'aria-label': o.title },
        h('header', null, h('span', { class: 'title' }, o.title), this.sub, this.tbtn), this.legend, this.plot, this.tableEl);
      this.canvas.addEventListener('pointermove', (e) => this.onMove(e));
      this.canvas.addEventListener('pointerleave', () => { this.hover = null; tooltip.hidden = true; this.draw(); });
      new ResizeObserver(() => this.draw()).observe(this.plot);
    }

    toggleTable() {
      this.showTable = !this.showTable;
      this.tbtn.setAttribute('aria-pressed', String(this.showTable));
      setText(this.tbtn, this.showTable ? 'График' : 'Таблица');
      this.update();
    }

    update() {
      const off = !STATE || this.o.hidden();
      this.root.hidden = off;
      if (off) return;
      setText(this.sub, this.o.sub());
      const refs = this.o.refs();
      const sig = refs.map((r) => `${r.label}|${r.text}`).join(',');
      if (this.legend.dataset.sig !== sig) {
        this.legend.dataset.sig = sig;
        const key = h('span', { class: 'key line' });
        key.style.borderTop = `2px solid var(${this.o.color})`;
        this.legend.replaceChildren(h('span', null, key, this.o.series),
          ...refs.map((r) => h('span', null, h('span', { class: `key ${r.cls}` }), `${r.label} `, h('b', null, r.text))));
      }
      this.plot.hidden = this.showTable;
      this.tableEl.hidden = !this.showTable;
      if (this.showTable) this.renderTable(); else this.draw();
    }

    renderTable() {
      const m = chartData();
      const v = this.o.values();
      if (!m || !m.t.length) { this.tableEl.replaceChildren(h('p', { class: 'muted' }, 'Нет данных')); return; }
      const n = m.t.length;
      const stride = Math.max(1, Math.floor(n / 30));
      const rows = [];
      for (let i = n - 1; i >= 0; i -= stride) rows.push(i);
      this.tableEl.replaceChildren(h('table', null,
        h('thead', null, h('tr', null, h('th', null, `Время${smoothNote()}`), h('th', { class: 'num' }, this.o.series), h('th', null, 'Режим'))),
        h('tbody', null, rows.map((i) => h('tr', null, h('td', { class: 'num' }, clock(m.t[i], true)),
          h('td', { class: 'num' }, v[i] == null ? '–' : this.o.fmt(v[i])),
          h('td', null, HEAT_NAMES[Math.round(m.heat[i] || 0)] || ''))))));
    }

    geometry() {
      const w = this.plot.clientWidth;
      const ht = this.plot.clientHeight;
      const pad = { l: 40, r: 12, t: 8, b: 24 };
      const { min, max } = this.o;
      const t1 = METRICS && METRICS.t.length ? METRICS.t[METRICS.t.length - 1] : Date.now() / 1000;
      const t0 = t1 - RANGE;
      const pw = Math.max(10, w - pad.l - pad.r);
      const ph = Math.max(10, ht - pad.t - pad.b);
      return {
        w, ht, pad, t0, t1, pw, ph,
        X: (t) => pad.l + ((t - t0) / (t1 - t0)) * pw,
        Y: (v) => pad.t + ph - ((Math.max(min, Math.min(max, v)) - min) / (max - min)) * ph,
      };
    }

    draw() {
      if (this.plot.hidden || this.root.hidden || !STATE) return;
      const dpr = window.devicePixelRatio || 1;
      const g = this.geometry();
      this.canvas.width = Math.round(g.w * dpr);
      this.canvas.height = Math.round(g.ht * dpr);
      const c = this.canvas.getContext('2d');
      c.setTransform(dpr, 0, 0, dpr, 0, 0);
      c.clearRect(0, 0, g.w, g.ht);
      const col = {
        grid: cssVar('--grid'), axis: cssVar('--axis'), muted: cssVar('--muted'), surface: cssVar('--surface'),
        line: cssVar(this.o.color), hot: cssVar('--critical'), warm: cssVar('--warning'),
      };
      c.font = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
      const m = chartData();
      const v = this.o.values();
      const has = m && m.t.length > 1;
      // periods of regulation and of the freeze as quiet bands
      if (has && m.heat) {
        for (const [level, color, a] of [[1, col.warm, 0.10], [2, col.hot, 0.09]]) {
          c.fillStyle = rgba(color, a);
          let s = null;
          for (let i = 0; i <= m.t.length; i += 1) {
            const on = i < m.t.length && Math.round(m.heat[i] || 0) === level;
            if (on && s === null) s = i;
            if (!on && s !== null) {
              const x0 = Math.max(g.pad.l, g.X(m.t[s]));
              c.fillRect(x0, g.pad.t, Math.max(1, g.X(m.t[i - 1]) - x0), g.ph);
              s = null;
            }
          }
        }
      }
      c.lineWidth = 1;
      c.textAlign = 'right';
      c.textBaseline = 'middle';
      for (let t = this.o.min; t <= this.o.max; t += this.o.step) {
        const y = Math.round(g.Y(t)) + 0.5;
        c.strokeStyle = t === this.o.min ? col.axis : col.grid;
        c.beginPath(); c.moveTo(g.pad.l, y); c.lineTo(g.w - g.pad.r, y); c.stroke();
        c.fillStyle = col.muted;
        c.fillText(this.o.tick(t), g.pad.l - 6, y);
      }
      c.textAlign = 'center';
      c.textBaseline = 'top';
      for (const t of xTicks(g.t0, g.t1, g.pw).ticks) {
        const x = g.X(t);
        if (x < g.pad.l + 14 || x > g.w - g.pad.r - 14) continue;
        c.fillStyle = col.muted;
        c.fillText(clock(t), x, g.pad.t + g.ph + 6);
      }
      for (const r of this.o.refs()) {
        const y = Math.round(g.Y(r.v)) + 0.5;
        c.strokeStyle = cssVar(r.color); c.lineWidth = 1.5; c.setLineDash(r.dash);
        c.beginPath(); c.moveTo(g.pad.l, y); c.lineTo(g.w - g.pad.r, y); c.stroke();
      }
      c.setLineDash([]);
      if (!has || !v.some((x) => x != null)) {
        c.fillStyle = col.muted;
        c.textBaseline = 'middle';
        c.fillText(this.o.empty(), g.pad.l + g.pw / 2, g.pad.t + g.ph / 2);
        return;
      }
      const gapAt = Math.max(3, (m.step || 1) * 3.5);
      c.strokeStyle = col.line; c.lineWidth = 2; c.lineJoin = 'round'; c.lineCap = 'round';
      c.beginPath();
      let pen = false;
      for (let i = 0; i < m.t.length; i += 1) {
        if (v[i] == null || (i > 0 && m.t[i] - m.t[i - 1] > gapAt)) pen = false;
        if (v[i] == null) continue;
        const x = g.X(m.t[i]); const y = g.Y(v[i]);
        if (pen) c.lineTo(x, y); else { c.moveTo(x, y); pen = true; }
      }
      c.stroke();
      if (this.hover != null && this.hover < m.t.length) {
        const i = this.hover;
        const x = Math.round(g.X(m.t[i])) + 0.5;
        c.strokeStyle = col.axis; c.lineWidth = 1;
        c.beginPath(); c.moveTo(x, g.pad.t); c.lineTo(x, g.pad.t + g.ph); c.stroke();
        if (v[i] != null) {
          c.beginPath(); c.arc(x, g.Y(v[i]), 4, 0, Math.PI * 2);
          c.fillStyle = col.line; c.fill(); c.lineWidth = 2; c.strokeStyle = col.surface; c.stroke();
        }
      }
    }

    onMove(e) {
      const m = chartData();
      if (!m || m.t.length < 2) return;
      const g = this.geometry();
      const rect = this.canvas.getBoundingClientRect();
      const t = g.t0 + ((e.clientX - rect.left - g.pad.l) / g.pw) * (g.t1 - g.t0);
      let lo = 0; let hi = m.t.length - 1;
      while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (m.t[mid] < t) lo = mid; else hi = mid; }
      const i = Math.abs(m.t[lo] - t) < Math.abs(m.t[hi] - t) ? lo : hi;
      this.hover = i;
      this.draw();
      const v = this.o.values();
      tooltip.replaceChildren(
        h('div', { class: 't' }, clock(m.t[i], true) + smoothNote()),
        h('div', { class: 'r' }, h('span', { class: 'lk', style: `border-top-color: var(${this.o.color})` }),
          h('b', null, v[i] == null ? '–' : this.o.fmt(v[i])), h('span', null, this.o.series)),
        h('div', { class: 'r' }, h('span', null, `режим: ${HEAT_NAMES[Math.round(m.heat[i] || 0)] || '–'}`)));
      tooltip.hidden = false;
      const tw = tooltip.offsetWidth; const th = tooltip.offsetHeight;
      let left = e.clientX + 14; let top = e.clientY + 14;
      if (left + tw > window.innerWidth - 8) left = e.clientX - tw - 14;
      if (top + th > window.innerHeight - 8) top = e.clientY - th - 14;
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${top}px`;
    }
  }

  const reading = (key) => (STATE && STATE.thermal ? STATE.thermal.readings.find((r) => r.key === key) : null);
  const tempChart = (key, title) => new LevelChart({
    title, series: 'температура', color: '--s-temp', min: 20, max: 110, step: 20,
    values: () => (chartData() || {})[`temp_${key}`] || [],
    fmt: (v) => `${Math.round(v)}°C`, tick: (v) => `${v}°`,
    hidden: () => !reading(key),
    sub: () => { const r = reading(key); return r.temp == null ? (r.note || 'нет данных') : `сейчас ${Math.round(r.temp)}°C`; },
    refs: () => {
      const r = reading(key);
      return [
        { v: r.hot, color: '--critical', dash: [5, 4], cls: 'thr-hot', label: 'перегрев', text: `${r.hot}°` },
        { v: r.target, color: '--warning', dash: [5, 4], cls: 'thr-warm', label: 'цель', text: `${r.target}°` },
        { v: r.normal, color: '--good', dash: [2, 3], cls: 'thr-norm', label: 'норма после перегрева', text: `≤ ${r.normal}°` },
      ];
    },
    empty: () => {
      const r = reading(key);
      if (r.temp != null) return 'Данные появятся через пару секунд';
      return r.note ? `Видеокарта ${r.note}: опрашивается, только когда работает` : 'Нет данных';
    },
  });
  const shareChart = new LevelChart({
    title: 'Регулятор температуры: доля процессора для задач', series: 'доля процессора', color: '--s-tasks',
    min: 0, max: 100, step: 25, wide: true,
    values: () => (chartData() || {}).cpu_factor || [],
    fmt: (v) => `${Math.round(v)}%`, tick: (v) => `${v}%`,
    hidden: () => !(STATE && STATE.thermal && STATE.thermal.enabled && STATE.thermal.regulate),
    sub: () => `сейчас ${Math.round(STATE.thermal.factor * 100)}% · 100% — без ограничений`,
    refs: () => [{ v: STATE.thermal.min_cpu_factor * 100, color: '--muted', dash: [2, 3], cls: 'thr', label: 'не меньше', text: `${Math.round(STATE.thermal.min_cpu_factor * 100)}%` }],
    empty: () => 'Данные появятся через пару секунд',
  });
  const tempCharts = [tempChart('cpu', 'Процессор'), tempChart('gpu', 'Видеокарта'),
    tempChart('board', 'Плата (ACPI)'), tempChart('ssd', 'SSD'), shareChart];
  $('#thermal-charts').append(...tempCharts.map((c) => c.root));

  function renderThermal(s) {
    const th = s.thermal;
    const box = $('#thermal-status');
    if (!th) return;
    const temps = th.readings.filter((r) => r.temp != null).map((r) => `${r.label} ${Math.round(r.temp)}°C`).join(', ');
    const norms = th.readings.map((r) => `${r.label} ≤ ${r.normal}°C`).join(', ');
    const share = Math.round(th.factor * 100);
    const lim = th.limiting;
    let icon = '✓';
    let head = 'Температура в норме';
    let body = th.regulate
      ? `Все компоненты не выше своих целей — расчёты идут без ограничений. Сейчас: ${temps || 'нет данных'}.`
      : `Плавный регулятор выключен: процессор урезается только заморозкой при перегреве. Сейчас: ${temps || 'нет данных'}.`;
    if (th.regulate && share < 100) body += ` Задачам пока ${share}% процессора: остальное возвращается постепенно.`;
    const noCpu = th.cpu_source === 'не отвечает' ? ' Датчик процессора недоступен: LibreHardwareMonitor не отвечает (он запускается при входе в Windows).'
      : th.cpu_source === 'нет' ? ' Датчика процессора нет: LibreHardwareMonitor не установлен (tools\\install_cpu_sensors.ps1).' : '';
    body += noCpu;
    if (!th.enabled) {
      head = 'Защита от перегрева выключена';
      body = `Включается в «Правилах и настройках». Сейчас: ${temps || 'нет данных'}.`;
    } else if (th.state === 'hot') {
      icon = '✕';
      head = 'Перегрев — все расчёты заморожены';
      body = `${th.trigger}. Расчёты продолжатся, когда все компоненты остынут до нормы (${norms}) и пробудут там ${dur(th.cool_confirm_s)}; сначала на половине процессора, остальное вернётся постепенно. `
        + (th.cool_left != null ? `Уже остыли — продолжим через ${dur(th.cool_left)}.` : `Сейчас: ${temps}.`);
    } else if (th.state === 'warm') {
      icon = '▲';
      head = `Регулятор держит температуру: задачам ${share}% процессора`;
      body = `${lim && lim.temp != null ? `${lim.label}: ${Math.round(lim.temp)}°C при цели ${lim.target}°C. ` : ''}Доля процессора для задач меняется плавно: снижается, пока компонент выше цели и не остывает, и так же плавно возвращается, когда он на 2° ниже цели. Сейчас: ${temps}.`;
    }
    box.className = `card thermal-status ${th.enabled ? th.state : 'normal'}`;
    fill(box, h('span', { class: 'icon', 'aria-hidden': 'true' }, icon),
      h('div', null, h('div', { class: 'head' }, head), h('div', { class: 'ink2' }, body)));
    $('#thermal-tiles').replaceChildren(...th.readings.map((r) => {
      const st = r.temp == null ? 'asleep' : (r.temp >= r.hot || r.throttling) ? 'hot' : r.temp > r.target ? 'warm' : 'normal';
      const label = { asleep: r.note || 'нет данных', hot: r.throttling ? 'прошивка тормозит процессор от нагрева' : 'перегрев', warm: 'выше цели', normal: 'не выше цели' }[st];
      return h('div', { class: 'tile' }, h('div', { class: 'label' }, r.label),
        h('div', { class: 'value' }, r.temp == null ? '–' : String(Math.round(r.temp)), h('span', { class: 'unit' }, '°C')),
        h('div', { class: 'rows' }, h('div', null, h('span', { class: `temp-state ${st}` }, h('i'), label)),
          h('div', null, `цель ${r.target}° · перегрев ${r.hot}° · норма после перегрева ≤ ${r.normal}°`)));
    }));
    tempCharts.forEach((c) => c.update());
  }

  // ================================================================ tiles
  function meter(parts, total, reserveAt) {
    const segs = parts.map(([v, cls]) => h('span', { class: cls, style: `width: ${Math.max(0, Math.min(100, (v / total) * 100))}%` }));
    const wrap = h('div', { class: 'meter-wrap' }, h('div', { class: 'meter', role: 'img', 'aria-label': 'Занято: вы и задачи' }, segs));
    if (reserveAt != null) wrap.append(h('span', { class: 'reserve', title: 'Граница резерва', style: `left: calc(${Math.max(0, Math.min(100, (reserveAt / total) * 100))}% - 1px)` }));
    return wrap;
  }
  function tile(label, value, unit, meterEl, rows) {
    return h('div', { class: 'tile' }, h('div', { class: 'label' }, label),
      h('div', { class: 'value' }, value, h('span', { class: 'unit' }, unit)), meterEl,
      h('div', { class: 'rows' }, rows.map((r) => h('div', null, r))));
  }
  const kv = (keyCls, label, val) => [keyCls ? h('span', { class: `key ${keyCls}` }) : null, `${label}: `, h('b', null, val)];

  function renderTiles(s) {
    const { ram, vram } = s.system;
    const lastM = (k, dflt) => (METRICS && METRICS[k] && METRICS[k].length ? METRICS[k][METRICS[k].length - 1] : dflt);
    const cpu = { ...s.system.cpu, user: lastM('cpu_user', s.system.cpu.user), tasks: lastM('cpu_tasks', s.system.cpu.tasks) };
    const gpu = { ...s.system.gpu, user: lastM('gpu_user', s.system.gpu.user), tasks: lastM('gpu_tasks', s.system.gpu.tasks) };
    const free = (pool, held) => Math.max(0, pool - held);
    const tiles = [];
    tiles.push(tile('Процессор', String(Math.round(cpu.user + cpu.tasks)), '%',
      meter([[cpu.user, 'm-user'], [cpu.tasks, 'm-tasks']], 100, 100 - (cpu.reserve_cores / cpu.n) * 100), [
        kv('user', 'Вы и система', pct(cpu.user)),
        kv('tasks', 'Задачи агентов', pct(cpu.tasks)),
        kv(null, 'Задачам доступно', `${n1(cpu.pool_cores)} из ${cpu.n} потоков${cpu.on_ecores ? ' (E-ядра)' : ''}, свободно ${n1(free(cpu.pool_cores, cpu.held))}`),
      ]));
    tiles.push(tile('Оперативная память', n1(ram.user + ram.tasks), `ГБ из ${n1(ram.total)}`,
      meter([[ram.user, 'm-user'], [ram.tasks, 'm-tasks']], ram.total, ram.total - ram.reserve), [
        kv('user', 'Вы и система', gb(ram.user)),
        kv('tasks', 'Задачи агентов', gb(ram.tasks)),
        kv(null, 'Пул задач', `${gb(ram.pool)}, свободно ${gb(free(ram.pool, ram.held))}`),
        kv(null, 'Резерв для вас', gb(ram.reserve)),
      ]));
    if (vram) {
      tiles.push(tile('Видеопамять', n1(vram.user + vram.tasks), `ГБ из ${n1(vram.total)}`,
        meter([[vram.user, 'm-user'], [vram.tasks, 'm-tasks']], vram.total, vram.total - vram.reserve), [
          kv('user', 'Вы и система', gb(vram.user)),
          kv('tasks', 'Задачи агентов', gb(vram.tasks)),
          kv(null, 'Свободно задачам', gb(free(vram.pool, vram.held))),
        ]));
      const busy = s.gpu_busy;
      tiles.push(tile('Загрузка видеокарты', String(Math.round(Math.max(gpu.user, gpu.tasks))), '%',
        meter([[gpu.user, 'm-user'], [gpu.tasks, 'm-tasks']], Math.max(100, gpu.user + gpu.tasks), null), [
          kv('user', 'Вы и система', pct(gpu.user)),
          kv('tasks', 'Задачи агентов', pct(gpu.tasks)),
          busy ? h('span', null, h('b', null, '❚❚ '), 'Видеокарта занята вами — GPU-задачи ждут')
            : `GPU-задачи ждут, если ваша загрузка выше ${pct(gpu.threshold)}`,
        ]));
    } else {
      tiles.push(tile('Видеокарта', '–', '', h('div'), ['Не найдена: GPU-задачи недоступны']));
    }
    $('#tiles').replaceChildren(...tiles);
  }

  function renderDisks(s) {
    const d = s.disk;
    const box = $('#disks');
    box.hidden = !(d && d.enabled && d.drives.length);
    if (box.hidden) return;
    const sig = `${d.min_free_gb}|${d.drives.map((x) => `${x.drive}|${Math.round(x.free * 10)}|${x.low}|${x.tasks}`).join(',')}`;
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    fill(box, h('span', { class: 'label' }, 'Диски'),
      d.drives.map((x) => h('div', { class: `drive${x.low ? ' low' : ''}` },
        h('span', { class: 'name' }, `${x.drive}${x.system ? ' (системный)' : ''}`),
        meter([[x.total - x.free, 'm-used']], x.total, x.total - d.min_free_gb),
        h('span', null, `свободно ${gb(x.free)} из ${gb(x.total)}`),
        x.low ? h('span', { class: 'tag warn' }, 'мало места — задачи стоят') : null)),
      h('span', { class: 'muted' }, `задачи замораживаются, если свободно меньше ${nf2.format(d.min_free_gb)} ГБ`));
  }

  // ================================================================ header, banners
  function renderHeader(s) {
    const chip = $('#status-chip');
    let cls = 'auto';
    let text = 'Авто: задачи на свободных ресурсах';
    if (s.mode === 'paused') { cls = 'paused'; text = 'Все задачи на паузе'; }
    else if (s.mode === 'game') { cls = 'game'; text = 'Игровой режим (вручную)'; }
    else if (s.game.active) { cls = 'game'; text = `Игра: ${s.game.source}`; }
    chip.className = `chip ${cls}`;
    setText($('.txt', chip), text);
    $$('#mode-seg button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === s.mode)));
    $('#awake-chip').hidden = !(s.power && s.power.keep_awake);
    const net = s.network;
    const nc = $('#net-chip');
    nc.hidden = !net || !net.network;
    if (net && net.network) {
      const cls = net.mode === 'offline' ? 'net-off' : net.online >= net.members ? 'net-on' : 'net-part';
      nc.className = `chip ${cls}`;
      setText($('.txt', nc), net.mode === 'offline' ? 'Сеть: офлайн'
        : `Сеть: на связи ${net.online} из ${net.members}${net.mode === 'one_way' ? ' · ПК только ваш' : ''}`);
      nc.title = `«${net.network}». Задач здесь из сети: ${net.hosted}, ваших задач на других ПК: ${net.remote}`;
    }
    const hc = $('#heat-chip');
    const heat = s.thermal && s.thermal.enabled ? s.thermal.state : 'normal';
    hc.hidden = heat === 'normal';
    if (heat !== 'normal') {
      hc.className = `chip ${heat}`;
      const lim = s.thermal.limiting;
      setText($('.txt', hc), heat === 'hot' ? 'Перегрев: расчёты заморожены'
        : `Регулятор: задачам ${Math.round(s.thermal.factor * 100)}% процессора`);
      hc.title = heat === 'hot' ? (s.thermal.trigger || '')
        : (lim && lim.temp != null ? `${lim.label}: ${Math.round(lim.temp)}°C при цели ${lim.target}°C` : '');
    }
    $('#mode-seg').hidden = !s.me.admin;
    $('#shutdown-btn').hidden = !s.me.admin;
    $$('.tabs button').forEach((b) => { if (['agents', 'rules', 'network'].includes(b.dataset.tab)) b.hidden = !s.me.admin; });
  }

  function renderBanners(s) {
    const out = [];
    const banner = (cls, icon, ...kids) => h('div', { class: `banner ${cls}`, role: 'status' }, h('span', { class: 'icon' }, icon), h('div', null, ...kids));
    if (offline) out.push(banner('warn', '!', 'Нет связи с менеджером. Он запущен? Запуск: start.cmd в папке проекта.'));
    if (s && s.mode === 'paused') {
      out.push(banner('warn', '❚❚', 'Все задачи агентов заморожены, новые не запускаются. ',
        h('button', { class: 'btn', type: 'button', onclick: () => setMode('auto') }, 'Вернуть авто')));
    } else if (s && s.game.active) {
      const cores = s.system.cpu.on_ecores ? `задачи на CPU перенесены на ${s.system.cpu.game_cores}` : 'задачи на CPU продолжают на свободных ядрах';
      const g = s.game;
      const mem = g.reserve_ram > 0.05 || g.reserve_vram > 0.05
        ? ` Для игры заранее держится свободным ${gb(g.reserve_ram)} ОЗУ${g.reserve_vram > 0.05 ? ` и ${gb(g.reserve_vram)} видеопамяти` : ''}${g.learned ? ' (сколько она заняла в прошлый раз)' : ''}.`
        : '';
      out.push(banner('', '🎮', `Игровой режим${g.source ? ` (${g.source})` : ''}: GPU-задачи на паузе, ${cores}.${mem}`));
    }
    if (s && s.thermal && s.thermal.enabled && s.thermal.state === 'hot') {
      const th = s.thermal;
      out.push(banner('warn', '🌡', `Перегрев — ${th.trigger}. Все расчёты заморожены до остывания`
        + (th.cool_left != null ? ` (продолжим через ${dur(th.cool_left)}). ` : '. '),
        h('button', { class: 'btn', type: 'button', onclick: () => tab('thermal') }, 'Подробнее')));
    }
    if (s && s.disk && s.disk.enabled) {
      for (const d of s.disk.drives.filter((x) => x.low)) {
        out.push(banner('warn', '!', `Мало места на диске ${d.drive}: свободно ${gb(d.free)} (минимум ${nf2.format(s.disk.min_free_gb)} ГБ). `
          + `${d.system ? 'Все расчёты заморожены: Windows тоже нужно место' : 'Задачи, работающие на этом диске, заморожены'}. Освободите место — они продолжат сами.`));
      }
    }
    if (s) {
      const c = s.system.commit;
      if (c.total > c.limit * (c.max_pct / 100)) {
        out.push(banner('warn', '!', `Виртуальная память почти заполнена: ${n1(c.total)} из ${n1(c.limit)} ГБ. Новые задачи не запускаются, при ${c.critical_pct}% задачи будут перезапущены позже.`));
      }
    }
    const box = $('#banners');
    const sig = out.map((b) => b.textContent).join('|');
    if (box.dataset.sig !== sig) { box.dataset.sig = sig; box.replaceChildren(...out); }
  }

  // ================================================================ tables
  function stateBadge(t) {
    const k = t.state === 'suspended' && t.reason === 'hung' ? 'hung' : t.state;
    return h('span', { class: `state ${k}` }, h('i'), h('span', { class: 'glyph', 'aria-hidden': 'true' }, GLYPH[k] || ''), t.state_text);
  }
  const used = (t, k) => (t.used ? t.used[k] : null);

  function syncRows(tbody, items, cols, build) {
    const map = tbody._rows || (tbody._rows = new Map());
    const keep = new Set(items.map((i) => i.id));
    for (const [id, tr] of map) if (!keep.has(id)) { tr.remove(); map.delete(id); }
    if (!items.length) {
      if (!tbody.querySelector('tr.empty')) tbody.replaceChildren(h('tr', { class: 'empty' }, h('td', { colspan: cols }, tbody.dataset.empty || 'Пусто')));
      return;
    }
    const empty = tbody.querySelector('tr.empty');
    if (empty) empty.remove();
    let prev = null;
    for (const item of items) {
      let tr = map.get(item.id);
      if (!tr) { tr = build(item); map.set(item.id, tr); }
      tr._update(item);
      const want = prev ? prev.nextSibling : tbody.firstChild;
      if (want !== tr) tbody.insertBefore(tr, want);
      prev = tr;
    }
  }

  function actionButtons(t) {
    const btns = [];
    if (t.state === 'running') btns.push(h('button', { class: 'btn', type: 'button', onclick: (e) => { e.stopPropagation(); taskAction(t.id, 'suspend'); } }, 'Пауза'));
    if (t.state === 'suspended') btns.push(h('button', { class: 'btn', type: 'button', onclick: (e) => { e.stopPropagation(); taskAction(t.id, 'resume'); } }, 'Продолжить'));
    if (['running', 'suspended', 'queued', 'remote'].includes(t.state)) {
      btns.push(h('button', { class: 'btn danger', type: 'button', onclick: (e) => { e.stopPropagation(); if (confirm(`Отменить задачу #${t.id} «${t.name}»?`)) taskAction(t.id, 'cancel'); } }, 'Отменить'));
    }
    return btns;
  }

  function taskRow(cells) {
    // cells: array of [className, (td, item) => void]; actions cell rebuilt only when state/priority change
    return (item) => {
      const tds = cells.map(([cls]) => h('td', { class: cls || null }));
      const tr = h('tr', { class: 'click', tabindex: '0', onclick: () => openTask(item.id), onkeydown: (e) => { if (e.key === 'Enter') openTask(item.id); } }, tds);
      tr._update = (it) => cells.forEach(([, fill], i) => fill(tds[i], it));
      return tr;
    };
  }
  const nameCell = (td, t) => {
    const sig = `${t.name}|${t.command}`;
    if (td._sig === sig) return;
    td._sig = sig;
    td.replaceChildren(h('div', { class: 'name' }, t.name), h('div', { class: 'cmd', title: t.command }, t.command));
  };
  const textCell = (fn) => (td, t) => setText(td, fn(t));
  const actionsCell = (td, t) => {
    const sig = `${t.state}|${t.priority}`;
    if (td._sig === sig) return;
    td._sig = sig;
    td.replaceChildren(...actionButtons(t));
  };
  const stateCell = (td, t) => {
    const tags = [];
    if (t.used && t.used.ram > t.ram * 1.1 + 0.2) tags.push('сверх заявки ОЗУ');
    if (t.overtime) tags.push('дольше заявленного');
    if (t.stopping) tags.push('сохраняется перед остановкой');
    let why = t.state === 'suspended' ? t.reason_text + (t.wait_text && t.wait === 'no_fit' ? `. ${t.wait_text}` : '') : '';
    if (t.stopping) why = `${t.reason_text}. Остановится принудительно через ${dur(Math.ceil(t.stopping.left_s / 5) * 5)}, если не завершится сама`;
    const sig = `${t.state}|${why}|${tags.join(',')}`;
    if (td._sig === sig) return;
    td._sig = sig;
    fill(td, stateBadge(t), tags.map((x) => h('span', { class: 'tag warn' }, x)), why ? h('div', { class: 'why' }, why) : null);
  };

  function progressText(p) {
    const count = p.total ? `${nf2.format(p.done)}/${nf2.format(p.total)} · ` : '';
    const eta = p.eta_s != null ? ` · ≈${dur(p.eta_s)}` : '';
    return `${count}${n1(p.pct)}%${eta}`;
  }
  const progressCell = (td, t) => {
    const p = t.progress;
    const sig = p ? `${p.pct}|${p.done}|${p.eta_s != null ? Math.round(p.eta_s / 10) : ''}|${p.note}` : '';
    if (td._sig === sig) return;
    td._sig = sig;
    if (!p) { fill(td, h('span', { class: 'muted small', title: 'Задача не сообщает прогресс (tqdm или RMQ_PROGRESS_FILE)' }, '—')); return; }
    const bar = h('div', { class: 'bar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(p.pct) }, h('span'));
    bar.firstChild.style.width = `${Math.max(0, Math.min(100, p.pct))}%`;
    fill(td, h('div', { class: 'progress', title: `${p.note ? `${p.note} · ` : ''}источник: ${p.source}${p.eta_s != null ? ' · оставшееся время — по скорости работы' : ''}` },
      bar, h('div', { class: 'ptext' }, progressText(p))));
  };

  const activeCols = [
    ['num', textCell((t) => String(t.id))],
    [null, nameCell],
    [null, textCell((t) => t.agent)],
    [null, stateCell],
    ['num', textCell((t) => (t.state === 'running'
      ? `${n1(used(t, 'cpu'))} / ${num(t.cpu)}${t.cap_cores != null ? ` (кап ${n1(t.cap_cores)})` : ''}` : `– / ${num(t.cpu)}`))],
    ['num', textCell((t) => `${t.used ? n1(t.used.ram) : '–'} / ${num(t.ram)}`)],
    ['num', textCell((t) => (t.vram || (t.used && t.used.vram > 0.05) ? `${t.used ? n1(t.used.vram) : '–'} / ${num(t.vram)}` : '–'))],
    ['num', textCell((t) => `${dur(t.run_s)} / ${dur(t.time_s)}`)],
    [null, progressCell],
    ['actions', actionsCell],
  ];
  const queueCols = [
    ['num', textCell((t) => (t.position != null ? String(t.position) : '–'))],
    ['num', textCell((t) => String(t.id))],
    [null, nameCell],
    [null, textCell((t) => t.agent)],
    ['num', textCell((t) => `${num(t.cpu)} яд · ${num(t.ram)} ГБ${t.vram ? ` · ${num(t.vram)} ГБ VRAM` : ''} · ~${dur(t.time_s)}${t.priority !== 'normal' ? ` · ${PRIO[t.priority]}` : ''}`)],
    ['num', textCell((t) => dur(STATE.now - t.queued_since))],
    ['num', textCell((t) => (t.score != null ? String(Math.round(t.score)) : '–'))],
    [null, (td, t) => setText(td, t.wait_text || (t.reason && t.reason.startsWith('requeued:') ? t.reason_text : 'Запустится на следующем шаге'))],
    ['actions', actionsCell],
  ];
  const allActiveCols = [
    ['num', textCell((t) => String(t.id))],
    [null, nameCell],
    [null, textCell((t) => t.agent)],
    [null, stateCell],
    ['num', textCell((t) => `${num(t.cpu)} яд · ${num(t.ram)} ГБ${t.vram ? ` · ${num(t.vram)} ГБ VRAM` : ''}`)],
    ['num', textCell((t) => (t.state === 'queued' ? `ждёт ${dur(STATE.now - t.queued_since)}` : `${dur(t.run_s)} / ${dur(t.time_s)}`))],
    [null, textCell((t) => (t.state === 'queued' ? t.wait_text : t.reason_text) || '')],
  ];
  const recentCols = [
    ['num', textCell((t) => String(t.id))],
    [null, nameCell],
    [null, textCell((t) => t.agent)],
    [null, (td, t) => {
      const sig = `${t.state}|${t.reason_text}`;
      if (td._sig === sig) return;
      td._sig = sig;
      fill(td, stateBadge(t), t.reason_text && t.state !== 'done' ? h('div', { class: 'why' }, t.reason_text) : null);
    }],
    ['num', textCell((t) => (t.exit_code == null ? '–' : String(t.exit_code)))],
    ['num', textCell((t) => dur(t.run_s))],
    ['num', textCell((t) => (t.peak_ram ? gb(t.peak_ram) : '–'))],
    ['num', textCell((t) => stamp(t.finished_at))],
  ];
  const builders = new Map();
  function rows(sel, items, cols, empty) {
    const tbody = $(`${sel} tbody`);
    tbody.dataset.empty = empty;
    if (!builders.has(sel)) builders.set(sel, taskRow(cols));
    syncRows(tbody, items, cols.length, builders.get(sel));
  }

  function renderTasks(s) {
    const active = s.tasks.filter((t) => t.state !== 'queued');
    const queued = s.tasks.filter((t) => t.state === 'queued').sort((a, b) => (a.position || 0) - (b.position || 0));
    rows('#t-active', active, activeCols, 'Сейчас ничего не выполняется');
    rows('#t-queue', queued, queueCols, 'Очередь пуста');
    if (TAB === 'tasks') {
      rows('#t-all-active', s.tasks, allActiveCols, 'Нет активных задач');
      rows('#t-recent', s.recent, recentCols, 'Пока ничего не завершалось');
    }
  }

  async function taskAction(id, action) {
    try {
      await api('POST', `/api/tasks/${id}/${action}`);
      poll(true);
    } catch (e) { alert(e.message); }
  }
  async function setMode(mode) {
    try { await api('POST', '/api/mode', { mode }); poll(true); } catch (e) { alert(e.message); }
  }
  $$('#mode-seg button').forEach((b) => b.addEventListener('click', () => setMode(b.dataset.mode)));

  // ================================================================ agents
  function renderAgents(s) {
    const tbody = $('#t-agents tbody');
    const map = tbody._rows || (tbody._rows = new Map());
    const build = (a) => {
      const weight = h('input', { type: 'number', min: '0.1', step: '0.1', value: a.weight, style: 'width: 70px', 'aria-label': 'Вес' });
      const maxRun = h('input', { type: 'number', min: '1', step: '1', value: a.max_running, style: 'width: 64px', 'aria-label': 'Макс. задач' });
      const prio = h('select', { 'aria-label': 'Макс. приоритет' }, ['low', 'normal', 'high'].map((p) => h('option', { value: p }, PRIO[p])));
      prio.value = a.max_priority;
      const patch = async (body) => { try { await api('PATCH', `/api/agents/${a.id}`, body); poll(true); } catch (e) { alert(e.message); } };
      weight.addEventListener('change', () => patch({ weight: Number(weight.value) }));
      maxRun.addEventListener('change', () => patch({ max_running: Number(maxRun.value) }));
      prio.addEventListener('change', () => patch({ max_priority: prio.value }));
      const tds = {
        name: h('td'), from: h('td', { class: 'small ink2' }), w: h('td', { class: 'num' }, weight), p: h('td', null, prio),
        r: h('td', { class: 'num' }, maxRun), usage: h('td', { class: 'num' }), fs: h('td', { class: 'num' }),
        now: h('td', { class: 'num' }), act: h('td', { class: 'actions' }),
      };
      const tr = h('tr', null, Object.values(tds));
      tr._update = (x) => {
        const sig = `${x.name}|${x.builtin}|${x.enabled}`;
        if (tds.name._sig !== sig) {
          tds.name._sig = sig;
          fill(tds.name, h('span', { class: 'name' }, x.builtin ? 'Вы (дашборд)' : x.name),
            x.builtin ? h('span', { class: 'tag' }, 'встроенный') : null, x.enabled ? null : h('span', { class: 'tag warn' }, 'отключён'));
          const btns = [];
          if (!x.builtin) {
            btns.push(h('button', { class: `btn${x.enabled ? ' danger' : ''}`, type: 'button', onclick: () => patch({ enabled: !x.enabled }) }, x.enabled ? 'Отключить' : 'Включить'));
          }
          btns.push(h('button', { class: 'btn', type: 'button', title: 'Обнулить накопленную нагрузку', onclick: () => patch({ reset_usage: true }) }, 'Сбросить нагрузку'));
          tds.act.replaceChildren(...btns);
        }
        setText(tds.from, x.builtin ? 'задачи из этого дашборда' : (x.note || ''));
        if (document.activeElement !== weight) weight.value = x.weight;
        if (document.activeElement !== maxRun) maxRun.value = x.max_running;
        if (document.activeElement !== prio) prio.value = x.max_priority;
        setText(tds.usage, n1(x.usage / 3600));
        tds.usage.title = 'Машино-часы с затуханием: 1 = весь ПК в течение часа';
        setText(tds.fs, x.fairshare == null ? '–' : nf2.format(x.fairshare));
        setText(tds.now, `▶ ${x.running}  ❚❚ ${x.suspended}  ◷ ${x.queued}`);
      };
      return tr;
    };
    const items = s.agents;
    const keep = new Set(items.map((a) => a.id));
    for (const [id, tr] of map) if (!keep.has(id)) { tr.remove(); map.delete(id); }
    for (const a of items) {
      let tr = map.get(a.id);
      if (!tr) { tr = build(a); map.set(a.id, tr); tbody.append(tr); }
      tr._update(a);
    }
  }

  async function loadAgentRules() {
    try {
      const r = await api('GET', '/api/agent-rules');
      setText($('#agent-rules'), r.text);
      fill($('#rules-installed'), r.installed.length
        ? [h('span', { class: 'ok' }, '✓ '), 'Правила уже в глобальных инструкциях: ',
          r.installed.map((x) => `${x.tool} (${x.path})`).join(', '), '.']
        : ['Правила пока не добавлены ни в один ИИ-инструмент. Добавить: ', h('code', null, 'bin\\rmq.cmd install-agents')]);
    } catch (e) { setText($('#agent-rules'), e.message); }
  }
  $('#copy-rules').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('#agent-rules').textContent);
      setText($('#copy-msg'), 'Скопировано — вставьте в Cursor Settings → Rules → User Rules');
    } catch (e) { setText($('#copy-msg'), e.message); }
  });

  // ================================================================ shutdown
  const modal = $('#shutdown-modal');
  function closeModal() { modal.hidden = true; $('#modal-overlay').hidden = true; }
  $('#shutdown-btn').addEventListener('click', () => {
    modal.hidden = false;
    $('#modal-overlay').hidden = false;
    $('.choices button', modal).focus();
  });
  $('#shutdown-cancel').addEventListener('click', closeModal);
  $('#modal-overlay').addEventListener('click', closeModal);
  $$('.choices button', modal).forEach((b) => b.addEventListener('click', async () => {
    try {
      const r = await api('POST', '/api/shutdown', { tasks: b.dataset.tasks });
      stopped = true;
      clearTimeout(pollTimer);
      closeModal();
      closeTask();
      $('#app').hidden = true;
      setText($('#stopped-note'), `${r.note[0].toUpperCase()}${r.note.slice(1)}.`);
      $('#stopped').hidden = false;
    } catch (e) { alert(e.message); }
  }));

  // ================================================================ submit form
  $('#submit-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = {
      command: f.get('command'), name: f.get('name') || undefined, cpu: Number(f.get('cpu')), ram: Number(f.get('ram')),
      vram: Number(f.get('vram')), time: f.get('time'), priority: f.get('priority'), preempt: f.get('preempt'),
    };
    if (f.get('cwd')) body.cwd = f.get('cwd');
    const msg = $('#submit-msg');
    try {
      const r = await api('POST', '/api/tasks', body);
      msg.className = 'msg ok';
      setText(msg, `Задача #${r.id} в очереди.${r.warnings.length ? ` ${r.warnings.join('; ')}` : ''}`);
      poll(true);
    } catch (err) { msg.className = 'msg err'; setText(msg, err.message); }
  });

  // ================================================================ rules & settings
  const LABELS = {
    reserve: ['Резерв для вас', {
      cpu_pct_of_free: 'CPU: % от свободного', cpu_min_cores: 'CPU: минимум потоков',
      ram_pct_of_free: 'ОЗУ: % от свободной', ram_min_gb: 'ОЗУ: минимум, ГБ',
      vram_pct_of_free: 'VRAM: % от свободной', vram_min_gb: 'VRAM: минимум, ГБ',
      commit_max_pct: 'Не запускать задачи при заполнении подкачки выше, %', commit_critical_pct: 'Перезапускать задачи при заполнении подкачки выше, %',
    }],
    smoothing: ['Сглаживание (сколько ждать, прежде чем снова отдать ресурсы задачам)', {
      cpu_window_s: 'CPU: ваша средняя нагрузка за, сек', ram_window_s: 'ОЗУ: ваш пик за, сек', vram_window_s: 'VRAM: ваш пик за, сек',
    }],
    gpu: ['Видеокарта', {
      adapter: 'Какая видеокарта (auto или часть названия; нужен перезапуск)', user_busy_pct: 'Ваша загрузка GPU, при которой GPU-задачи ждут, %',
      busy_confirm_s: 'Подтверждение занятости, сек', idle_confirm_s: 'Сколько GPU должна быть свободна до возобновления, сек',
      hide_gpu_for_cpu_tasks: 'Скрывать GPU от задач без заявки VRAM (CUDA_VISIBLE_DEVICES="")',
      free_vram_for_user: 'Если вам не хватает видеопамяти, останавливать замороженные GPU-задачи (перезапуск позже)',
    }],
    game_mode: ['Игровой режим', {
      auto_detect: 'Определять игры автоматически', detect_fullscreen: 'Считать игрой любое полноэкранное Direct3D-приложение',
      pause_gpu_tasks: 'Ставить GPU-задачи на паузу', cpu_cores: 'Ядра для задач во время игры (efficiency = E-ядра, а без них половина потоков; all = все)',
      learn_game_memory: 'Запоминать, сколько памяти берёт каждая игра, и освобождать её заранее',
      eco_qos: 'Режим энергоэффективности (EcoQoS) для задач во время игры', extra_ram_reserve_gb: 'Резерв ОЗУ при запуске незнакомой игры, ГБ',
      release_after_s: 'Выход из игрового режима через, сек после закрытия игры',
      path_patterns: 'Папки игр (часть пути, по одной в строке)', ignore_path_patterns: 'Исключения по папкам',
      process_names: 'Дополнительные игры по имени .exe', ignore_names: 'Не считать игрой (.exe)',
    }],
    thermal: ['Температура и перегрев', {
      enabled: 'Следить за температурой и замораживать расчёты при перегреве',
      regulate: 'Плавно урезать процессор задачам, чтобы компоненты не грелись выше цели',
      cpu_target_c: 'Процессор: цель, °C', cpu_hot_c: 'Процессор: перегрев с, °C', cpu_normal_c: 'Процессор: норма после перегрева до, °C',
      board_target_c: 'Плата (ACPI): цель, °C', board_hot_c: 'Плата (ACPI): перегрев с, °C', board_normal_c: 'Плата (ACPI): норма после перегрева до, °C',
      gpu_target_c: 'Видеокарта: цель, °C', gpu_hot_c: 'Видеокарта: перегрев с, °C (не выше паспортного максимума − 2)', gpu_normal_c: 'Видеокарта: норма после перегрева до, °C',
      ssd_target_c: 'SSD: цель, °C', ssd_hot_c: 'SSD: перегрев с, °C (не выше порога диска − 3)', ssd_normal_c: 'SSD: норма после перегрева до, °C',
      min_cpu_factor: 'Регулятор не урезает процессор задачам ниже этой доли (0,2 = 20%)',
      hot_confirm_s: 'Сколько секунд перегрев должен держаться до заморозки',
      cool_confirm_s: 'Сколько секунд всё должно быть в норме до возобновления',
      freeze_on_throttling: 'Считать перегревом торможение процессора прошивкой из-за нагрева',
    }],
    network: ['Вычислительная сеть', {
      port: 'Порт для других ПК сети (нужен перезапуск)', bind: 'Адрес для других ПК: tailscale или IP этого ПК',
      remote_weight: 'Вес задач участников при делении процессора (1 — как у ваших агентов)',
      max_hosted_tasks: 'Сколько задач участников может быть здесь одновременно',
      max_bundle_mb: 'Самый большой проект, который можно прислать сюда, МБ', max_results_mb: 'Сколько результатов задача может вернуть, МБ',
      max_disk_gb: 'Место под файлы участников на этом ПК, ГБ',
      protect_host: 'Защита записи: задачи участников могут читать, но не менять и не удалять файлы этого ПК',
      keep_results_h: 'Удалять незабранные результаты через, ч',
    }],
    update: ['Обновления', { auto: 'Проверять и ставить обновления автоматически (кроме рабочей копии разработчика)', check_every_h: 'Проверять раз в, ч' }],
    disk: ['Место на диске', {
      enabled: 'Замораживать задачи, когда на диске мало места',
      min_free_gb: 'Минимум свободного места, ГБ (системный диск — для всех задач, другой диск — для задач, работающих на нём)',
    }],
    power: ['Сон', {
      keep_awake_while_running: 'Не давать ПК уходить в сон по таймеру, пока выполняются задачи (экран гаснет как обычно)',
      keep_awake_on_battery: '…и при работе от батареи (иначе на батарее сон разрешён, чтобы не разрядить её)',
    }],
    scheduler: ['Очередь и честное деление', {
      tick_s: 'Шаг планировщика, сек', cpu_squeeze_pct: 'На сколько % можно ужать процессор работающих задач, чтобы запустить новую', settle_s: 'Пауза после вытеснения, сек',
      short_task_max_min: 'Короткая задача — до, мин', preempt_after_short_min: 'Короткая задача может вытеснить «захватчика» через, мин',
      preempt_after_min: 'Обычная задача может вытеснить через, мин', min_slice_min: 'Защита после запуска/возобновления, мин',
      reservation_after_min: 'Резервировать ресурсы для первой в очереди через, мин',
      starvation_after_min: 'Задача, ждущая дольше (мин), может вытеснять даже сверх доли своего агента',
      fair_preemption: 'Вытеснять агентов сверх доли',
      share_tolerance_pct: 'Допуск сверх честной доли, %', overtime_grace_pct: 'Запас к заявленному времени, %',
      usage_half_life_h: 'Период полураспада нагрузки агента, ч', overtime_usage_mult: 'Множитель нагрузки сверхурочных задач',
      age_full_h: 'За сколько часов ожидания очки за ожидание максимальны',
      w_priority: 'Очки: приоритет', w_fairshare: 'Очки: честная доля', w_age: 'Очки: ожидание', w_short: 'Очки: короткая задача', w_started: 'Очки: уже начатая задача',
    }],
    tasks: ['Задачи по умолчанию', {
      default_cpu: 'CPU, ядер', default_ram_gb: 'ОЗУ, ГБ', default_vram_gb: 'VRAM, ГБ', default_time: 'Ожидаемое время',
      default_priority: 'Приоритет', default_preempt: 'Вытеснение (suspend/requeue)', set_thread_env: 'Ставить OMP_NUM_THREADS и т.п. = заявленным ядрам',
      low_io_priority: 'Самый низкий приоритет диска для задач (ваши программы читают и пишут первыми)',
      log_max_mb: 'Размер лога, МБ', keep_finished_days: 'Хранить завершённые, дней', orphan_grace_s: 'Добивать оставшиеся процессы через, сек',
      stop_grace_s: 'Мягкая остановка: сколько секунд задача с --preempt requeue может сохранять чекпоинт (0 — останавливать сразу)',
      hung_after_min: 'Замораживать задачу как «зависшую» через, мин без признаков работы (0 — не замораживать)',
    }],
    agents: ['Новые агенты (регистрируются сами)', {
      default_weight: 'Вес', default_max_running: 'Макс. задач одновременно', default_max_queued: 'Макс. задач в очереди', default_max_priority: 'Макс. приоритет',
    }],
    server: ['Сервер (нужен перезапуск)', { host: 'Адрес', port: 'Порт' }],
  };
  const ENUMS = {
    'game_mode.cpu_cores': ['efficiency', 'all'], 'tasks.default_priority': ['low', 'normal', 'high'],
    'tasks.default_preempt': ['suspend', 'requeue'], 'agents.default_max_priority': ['low', 'normal', 'high'],
  };

  function renderSettingsForm(S) {
    const box = $('#settings-fields');
    const secs = [];
    for (const [sec, [title, labels]] of Object.entries(LABELS)) {
      const fields = [];
      for (const [key, label] of Object.entries(labels)) {
        const v = S[sec][key];
        const name = `${sec}.${key}`;
        let input;
        if (typeof v === 'boolean') {
          input = h('input', { type: 'checkbox', name });
          input.checked = v;
          fields.push(h('label', { class: 'chk wide' }, input, label));
          continue;
        }
        if (Array.isArray(v)) input = h('textarea', { name }, v.join('\n'));
        else if (ENUMS[name]) { input = h('select', { name }, ENUMS[name].map((o) => h('option', { value: o }, o))); input.value = v; }
        else if (typeof v === 'number') input = h('input', { type: 'number', name, step: 'any', min: '0', value: String(v) });
        else input = h('input', { name, value: v });
        fields.push(h('label', { class: `f${Array.isArray(v) ? ' wide' : ''}` }, label, input));
      }
      secs.push(h('div', { class: 'settings-sec' }, h('h3', null, title), h('div', { class: 'form-grid' }, fields)));
    }
    box.replaceChildren(...secs);
  }

  function renderRules(S) {
    const r = S.reserve; const sc = S.scheduler; const gm = S.game_mode; const g = S.gpu; const sm = S.smoothing;
    const li = (title, ...text) => h('li', null, h('b', null, title), ' ', ...text);
    $('#rules').replaceChildren(h('ol', null,
      li('Вы всегда первые.', 'Задачи агентов работают с самым низким приоритетом процессора (Idle)' + (S.tasks.low_io_priority ? ' и диска' : '') + ': любая ваша программа получает ресурсы мгновенно. Ваши программы менеджер не трогает вообще — он управляет только задачами, поставленными через очередь.'),
      li('Пул для задач', `= всё, что не заняли Windows и ваши программы, минус резерв: CPU — ${r.cpu_pct_of_free}% свободного, но не меньше ${r.cpu_min_cores} потока; ОЗУ — ${r.ram_pct_of_free}%, не меньше ${r.ram_min_gb} ГБ; VRAM — ${r.vram_pct_of_free}%, не меньше ${r.vram_min_gb} ГБ. Пример: 32 ГБ − 8 (Windows) − 10 (игра) = 14, резерв 1 → задачам 13 ГБ. Резерв CPU нужен не ради скорости ваших программ (их и так пропускает Idle), а чтобы ноутбук не грелся до упора и у системы был запас.`),
      li('Вам понадобилась память —', `свободной ОЗУ стало меньше резерва — задачи сразу замораживаются, их память уходит в файл подкачки (или задача перезапускается позже, если агент указал --preempt requeue). Сначала останавливаются задачи, превысившие свою заявку. Если игре нужно всё — останавливаются все. Память возвращается задачам, когда ваша нагрузка ${sm.ram_window_s} с остаётся ниже.`),
      li('Процессор делится, а не захватывается.', `Когда ПК простаивает, один агент может взять весь пул — всё, кроме вашей средней нагрузки за ${sm.cpu_window_s} с и резерва; заявка больше свободного означает «дай всё свободное». Новой задаче не нужно ждать, пока освободится процессор: работающие задачи ужимаются (в сумме не больше чем на ${S.scheduler.cpu_squeeze_pct}%), а другой агент вправе получить свою долю целиком. Процессор делится поровну между агентами (кому нужно меньше — получает своё, остальное другим), никого не замораживая. Высокий приоритет получает процессор первым, низкий — только остатки; незанятое никогда не простаивает.`),
      li('Игры.', `Игра определяется по папке (Steam, Epic, GOG, Xbox…) или полноэкранному режиму. Во время игры ${gm.pause_gpu_tasks ? 'задачи на GPU на паузе' : 'задачи на GPU продолжают'}, ${gm.cpu_cores === 'efficiency' ? 'задачи на CPU работают только на энергоэффективных E-ядрах (на процессорах без них — на половине потоков)' : 'задачи на CPU работают на свободных ядрах'}. ${gm.learn_game_memory ? `Менеджер запоминает, сколько памяти заняла каждая игра, и при следующем её запуске освобождает столько же заранее; для незнакомой игры — ${gm.extra_ram_reserve_gb} ГБ.` : ''} Если ваша собственная загрузка видеокарты выше ${g.user_busy_pct}%, GPU-задачи ждут и без игры.`),
      li('Видеопамять.', `Заморозка останавливает работу GPU-задачи, но видеопамять может остаться занятой. ${g.free_vram_for_user ? 'Если вашей программе её всё ещё не хватает, такая задача останавливается и будет перезапущена позже (агенты сохраняют чекпоинты).' : 'Замороженные задачи видеопамять не отдают (настройка выключена).'}`),
      li('Заявка.', 'Каждая задача заявляет ядра, ОЗУ, VRAM и ожидаемое время. Старт — только если заявка помещается в свободную часть пула. CPU жёстко ограничен её долей пула; задаче без заявки VRAM видеокарта не видна. Перерасход ОЗУ виден в дашборде: такие задачи при нехватке останавливаются первыми, а при перезапуске ждут свой реальный пик.'),
      li('Порядок очереди — по очкам:', `очки = ${sc.w_priority} × приоритет (низкий 0, обычный 0,5, высокий 1) + ${sc.w_fairshare} × честная доля агента (1 — не пользовался ПК, меньше — пользовался много; половина «веса» уходит за ~${sc.usage_half_life_h} ч) + ${sc.w_age} × ожидание (линейно до 1 за ${sc.age_full_h} ч) + ${sc.w_short} × короткость (1 для ≤5 мин, 0 для ≥24 ч, логарифмически) + ${sc.w_started} уже начатым − ${sc.w_priority / 2} сверхурочным. Агентам доступны приоритеты низкий и обычный; высокий — только вам.`),
      li('Честное деление.', `Каждому агенту с задачами положена доля пула по весу (поровну по умолчанию, допуск ${sc.share_tolerance_pct} процентных пунктов). Доля считается по самому занятому ресурсу. Пока другой агент ждёт, агент сверх своей доли не может занимать ресурсы, нужные ждущему.`),
      li('Никто не держит всё сутками.', `Если задача ждёт дольше ${sc.preempt_after_short_min} мин (короткая, до ${sc.short_task_max_min} мин) или ${sc.preempt_after_min} мин (остальные), а место занято агентом сверх его доли или задачей, работающей дольше заявленного, такая задача замораживается (не убивается) и продолжит позже. Первые ${sc.min_slice_min} мин после запуска/возобновления задачу не трогают. Высокий приоритет защищает только задачу, которая укладывается в заявленное время.`),
      li('Большие задачи не голодают.', `Если первая в очереди задача ждёт дольше ${sc.reservation_after_min} мин, вперёд неё пускаются только задачи, которые по заявке успеют закончиться раньше, чем для неё освободится место. Если задача ждёт дольше ${sc.starvation_after_min} мин, она может вытеснять «захватчиков», даже если её агент при этом займёт больше своей доли.`),
      li('Время', `считается только тогда, когда задача реально работает (пауза не в счёт). Задача, работающая дольше заявленного (+${sc.overtime_grace_pct}%), — «сверхурочная»: у неё меньше очков, её проще вытеснить, а занятые ресурсы считаются агенту ×${sc.overtime_usage_mult}. Жёсткий --time-limit убивает задачу. Замороженные задачи возобновляются в порядке очков; прогресс и время работы сохраняются.`),
      li('Температура.', S.thermal.enabled
        ? `Менеджер следит за процессором, платой (ACPI), видеокартой и SSD. ${S.thermal.regulate ? `Регулятор держит каждый компонент не выше цели (процессор ${S.thermal.cpu_target_c}°, плата ${S.thermal.board_target_c}°, видеокарта ${S.thermal.gpu_target_c}°, SSD ${S.thermal.ssd_target_c}°): пока компонент выше цели и не остывает, доля процессора для задач плавно снижается — не ниже ${Math.round(S.thermal.min_cpu_factor * 100)}%, а из-за видеокарты и SSD (их греет в основном своя работа) не ниже половины; когда он на 2° ниже цели, процессор так же плавно возвращается. ` : 'Плавный регулятор выключен. '}Перегрев (процессор ≥ ${S.thermal.cpu_hot_c}°, плата ≥ ${S.thermal.board_hot_c}°, видеокарта ≥ ${S.thermal.gpu_hot_c}°, SSD ≥ ${S.thermal.ssd_hot_c}°${S.thermal.freeze_on_throttling ? ' или прошивка тормозит процессор из-за нагрева' : ''} дольше ${S.thermal.hot_confirm_s} с) — все расчёты замораживаются, пока всё не остынет до нормы (процессор ≤ ${S.thermal.cpu_normal_c}°, плата ≤ ${S.thermal.board_normal_c}°, видеокарта ≤ ${S.thermal.gpu_normal_c}°, SSD ≤ ${S.thermal.ssd_normal_c}°) и не пробудет там ${S.thermal.cool_confirm_s} с; потом задачи продолжают на половине процессора и получают остальное постепенно. Температуру процессора читает LibreHardwareMonitor через драйвер PawnIO, локально и без интернета.`
        : 'Защита от перегрева выключена.'),
      li('Место на диске.', S.disk.enabled
        ? `Если на системном диске свободно меньше ${S.disk.min_free_gb} ГБ, все задачи замораживаются — Windows тоже нужно место. Если мало места на другом диске, замораживаются задачи, которые работают в папке на нём. Задачи продолжат сами, когда свободного станет на 2 ГБ больше.`
        : 'Проверка места на диске выключена.'),
      li('Вычислительная сеть.', `Несколько ПК с этим менеджером можно объединить в сеть через Tailscale (вкладка «Сеть»). Режимы: двусторонний — ваши агенты считают на других ПК, а участники на вашем; «только для себя» — ваши агенты по-прежнему могут считать на других ПК, а задачи участников здесь стоят; офлайн — без соединений. Задачи участников идут с низким приоритетом и весом ${S.network.remote_weight} при делении процессора, не больше ${S.network.max_hosted_tasks} одновременно, ${S.network.protect_host ? 'с защитой записи: читать файлы этого ПК могут, менять и удалять — нет' : 'без защиты записи'}. Им видны только переменные окружения Windows, ваши ключи и токены — нет.`),
      li('Мягкая остановка.', S.tasks.stop_grace_s > 0
        ? `Если задачу с --preempt requeue нужно остановить не срочно (честная очередь или нехватка памяти, ещё не критическая), менеджер создаёт для неё файл RMQ_STOP_FILE и даёт ${S.tasks.stop_grace_s} с, чтобы сохранить чекпоинт и завершиться; не успела — останавливается принудительно. Когда память или видеопамять нужны вам срочно, задача останавливается сразу.`
        : 'Выключена: задачи с --preempt requeue останавливаются сразу.'),
      li('Зависшие задачи.', S.tasks.hung_after_min > 0
        ? `Если задача ${S.tasks.hung_after_min} мин не грузит процессор и видеокарту, не пишет в лог и на диск, а её прогресс не меняется, она замораживается с пометкой «Зависла» и не занимает процессор. Сама она не продолжит: её можно продолжить (кнопка «Продолжить», агенту — rmq resume) или отменить.`
        : 'Заморозка зависших задач выключена.'),
      li('Сон.', `${S.power.keep_awake_while_running ? `Пока выполняется хотя бы одна задача, Windows не уходит в сон по таймеру; экран гаснет как обычно.${S.power.keep_awake_on_battery ? '' : ' При работе от батареи сон разрешён, чтобы расчёты не разрядили её.'}` : 'ПК засыпает по своим обычным настройкам (защита от сна выключена).'} Закрытие крышки, кнопка питания и «Пуск → Сон» усыпляют ПК как всегда — задачи просто встанут и продолжат после пробуждения; время сна им не засчитывается.`),
      li('Агенты', 'регистрируются сами при первой задаче (имя: инструмент@проект). Лимиты по умолчанию — на вкладке «Агенты», там же можно отключить агента.'),
      li('Вы управляете всем:', 'режимы «Авто» / «Игровой» / «Пауза всех», пауза, отмена и приоритет любой задачи, выключение менеджера.')));
  }

  async function loadSettings() {
    try {
      SETTINGS = await api('GET', '/api/settings');
      renderRules(SETTINGS);
      renderSettingsForm(SETTINGS);
    } catch (e) { setText($('#settings-msg'), e.message); }
  }

  $('#settings-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const patch = {};
    for (const el of $$('[name]', e.target)) {
      const [sec, key] = el.name.split('.');
      const cur = SETTINGS[sec][key];
      let v;
      if (el.type === 'checkbox') v = el.checked;
      else if (Array.isArray(cur)) v = el.value.split('\n').map((x) => x.trim()).filter(Boolean);
      else if (typeof cur === 'number') v = Number(el.value);
      else v = el.value;
      if (JSON.stringify(v) !== JSON.stringify(cur)) (patch[sec] = patch[sec] || {})[key] = v;
    }
    const msg = $('#settings-msg');
    if (!Object.keys(patch).length) { msg.className = 'msg'; setText(msg, 'Изменений нет'); return; }
    try {
      SETTINGS = await api('PATCH', '/api/settings', patch);
      renderRules(SETTINGS);
      renderSettingsForm(SETTINGS);
      msg.className = 'msg ok'; setText(msg, 'Сохранено');
    } catch (err) { msg.className = 'msg err'; setText(msg, err.message); }
  });
  $('#settings-reset').addEventListener('click', async () => {
    if (!confirm('Вернуть все настройки к значениям по умолчанию?')) return;
    try { SETTINGS = await api('POST', '/api/settings/reset'); renderRules(SETTINGS); renderSettingsForm(SETTINGS); } catch (e) { alert(e.message); }
  });

  // ================================================================ events
  function renderEvents(list, box) {
    const sig = list.length ? `${list[0].id}|${list.length}` : '';
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    box.replaceChildren(...(list.length ? list.map((e) => h('li', null, h('span', { class: 'ts' }, stamp(e.ts)),
      h('span', { class: 'tid' }, e.task_id ? `#${e.task_id}` : ''), h('span', null, e.message))) : [h('li', { class: 'muted' }, 'Событий пока нет')]));
  }

  // ================================================================ task drawer
  let drawer = null;
  function openTask(id) {
    closeTask();
    drawer = { id, offset: -65536, timer: null, sig: '' };
    $('#overlay').hidden = false;
    $('#drawer').hidden = false;
    $('#logbox').replaceChildren();
    setText($('#log-note'), '');
    refreshTask();
    $('#drawer-close').focus();
  }
  function closeTask() {
    if (drawer && drawer.timer) clearTimeout(drawer.timer);
    drawer = null;
    $('#overlay').hidden = true;
    $('#drawer').hidden = true;
  }
  $('#drawer-close').addEventListener('click', closeTask);
  $('#overlay').addEventListener('click', closeTask);
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!modal.hidden) closeModal();
    else if (drawer) closeTask();
  });

  async function refreshTask() {
    if (!drawer) return;
    const d = drawer;
    try {
      const t = await api('GET', `/api/tasks/${d.id}`);
      if (drawer !== d) return;
      setText($('#drawer-title'), `#${t.id} ${t.name}`);
      const sig = `${t.state}|${t.priority}`;
      if (d.sig !== sig) {
        d.sig = sig;
        const acts = actionButtons(t);
        if (['queued', 'running', 'suspended'].includes(t.state) && (STATE && STATE.me.admin)) {
          const sel = h('select', { 'aria-label': 'Приоритет', title: 'Приоритет' }, ['low', 'normal', 'high'].map((p) => h('option', { value: p }, PRIO[p])));
          sel.value = t.priority;
          sel.addEventListener('change', async () => { try { await api('POST', `/api/tasks/${t.id}/priority`, { priority: sel.value }); poll(true); } catch (e) { alert(e.message); } });
          acts.unshift(sel);
        }
        $('#drawer-actions').replaceChildren(...acts);
      }
      const u = t.used;
      const rowsKV = [
        ['Состояние', `${t.state_text}${t.reason_text ? ` — ${t.reason_text}` : ''}`],
        t.wait_text ? ['Почему ждёт', t.wait_text] : null,
        t.position ? ['Место в очереди', `${t.position} (очки ${Math.round(t.score)})`] : null,
        ['Агент', t.agent],
        t.where ? ['Где выполняется', `на ПК ${t.where} (вычислительная сеть); результаты вернутся в папку задачи`] : null,
        t.from ? ['Откуда', `с ПК ${t.from} (вычислительная сеть); работает с пониженным уровнем доступа`] : null,
        ['Команда', t.command],
        ['Папка', t.cwd],
        ['Заявка', `CPU ${num(t.cpu)} яд., ОЗУ ${num(t.ram)} ГБ, VRAM ${num(t.vram)} ГБ, время ~${dur(t.time_s)}${t.time_limit_s ? `, лимит ${dur(t.time_limit_s)}` : ''}, приоритет ${PRIO[t.priority]}, при нехватке — ${t.preempt === 'requeue' ? 'перезапуск' : 'заморозка'}`],
        u ? ['Сейчас', `CPU ${n1(u.cpu)} яд.${t.cap_cores != null ? ` (кап ${n1(t.cap_cores)})` : ''}, ОЗУ ${gb(u.ram)}, VRAM ${gb(u.vram)}, GPU ${pct(u.gpu)}, процессов ${u.procs}`] : null,
        ['Пик', `ОЗУ ${gb(t.peak_ram)}, VRAM ${gb(t.peak_vram)}`],
        t.progress ? ['Прогресс', `${progressText(t.progress)}${t.progress.note ? ` — ${t.progress.note}` : ''} (источник: ${t.progress.source}${t.progress.eta_s != null ? '; оставшееся время — по скорости работы' : ''})`] : null,
        t.stopping ? ['Остановка', `${t.stopping.reason}. Задаче дан файл RMQ_STOP_FILE: сохранить чекпоинт и завершиться; принудительно через ${dur(t.stopping.left_s)}`] : null,
        t.idle_s != null ? ['Активность', t.hung
          ? `${dur(t.idle_s)} не грузит процессор, не пишет в лог и на диск, прогресс не меняется — возможно, зависла`
          : t.idle_s < 30 ? 'работает' : `последний признак работы ${dur(t.idle_s)} назад`] : null,
        ['Время работы', `${dur(t.run_s)}${t.overtime ? ' — дольше заявленного' : ''}`],
        ['Поставлена', stamp(t.submitted_at)],
        t.started_at ? ['Запущена', stamp(t.started_at)] : null,
        t.finished_at ? ['Завершена', stamp(t.finished_at)] : null,
        t.restarts ? ['Перезапусков', String(t.restarts)] : null,
        t.exit_code != null ? ['Код выхода', String(t.exit_code)] : null,
      ].filter(Boolean);
      $('#drawer-kv').replaceChildren(...rowsKV.flatMap(([k, v]) => [h('dt', null, k), h('dd', { class: k === 'Команда' || k === 'Папка' ? 'mono' : null }, v)]));
      renderEvents(t.events || [], $('#drawer-events'));
      // log tail
      const box = $('#logbox');
      const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
      for (let guard = 0; guard < 8; guard += 1) {
        const chunk = await api('GET', `/api/tasks/${d.id}/log?offset=${d.offset}&limit=131072`);
        if (drawer !== d) return;
        if (chunk.rotated) box.append('\n[лог был ротирован]\n');
        if (d.offset < 0 && chunk.offset > chunk.data.length) setText($('#log-note'), '(показан конец)');
        if (chunk.data) box.append(chunk.data);
        d.offset = chunk.offset;
        if (chunk.data.length < 120000) break;
      }
      if (box.textContent.length > 600000) box.textContent = box.textContent.slice(-400000);
      if (atBottom) box.scrollTop = box.scrollHeight;
    } catch (e) {
      setText($('#log-note'), e.message);
    }
    if (drawer === d) d.timer = setTimeout(refreshTask, 1500);
  }

  // ================================================================ statistics (saved in data\stats)
  let REPORT = null;
  let PERIOD = '7d';
  let STAT_COMP = 'cpu';
  let statsLoadedAt = 0;
  const COMP_NAMES = { cpu: 'Процессор', gpu: 'Видеокарта', board: 'Плата (ACPI)', ssd: 'SSD' };
  const COMP_SHORT = { cpu: 'процессор', gpu: 'видеокарта', board: 'плата', ssd: 'SSD' };
  const FILE_NOTES = {
    'hourly.csv': 'по часам: нагрузка, температуры, заморозки, диск, задачи',
    'agents_hourly.csv': 'по агентам и часам',
    'README.txt': 'что означает каждая колонка',
  };
  const mins = (m) => {
    if (m == null) return '–';
    const v = Math.round(m);
    return v >= 60 ? `${Math.floor(v / 60)} ч ${String(v % 60).padStart(2, '0')} мин` : `${v} мин`;
  };
  const deg = (v) => (v == null ? '–' : `${Math.round(v)}°C`);
  const fsize = (b) => (b >= 1048576 ? `${n1(b / 1048576)} МБ` : `${Math.max(1, Math.round(b / 1024))} КБ`);
  const times = (n) => (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14) ? 'раза' : 'раз');
  const tipRow = (label, value, color) => h('div', { class: 'r' },
    color ? h('span', { class: 'lk', style: `border-top-color: var(${color})` }) : null, h('b', null, value), h('span', null, label));

  function bucketLabel(b, long) {
    const d = new Date(b.t * 1000);
    const two = (x) => String(x).padStart(2, '0');
    if (REPORT.bucket === 'hour') {
      const hh = `${two(d.getHours())}:00`;
      return long ? `${d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' })}, ${hh}–${two((d.getHours() + 1) % 24)}:00` : hh;
    }
    return long ? d.toLocaleDateString('ru-RU', { weekday: 'short', day: '2-digit', month: '2-digit', year: 'numeric' })
      : `${two(d.getDate())}.${two(d.getMonth() + 1)}`;
  }

  class BucketChart {
    // One value per hour or day of the report. o: { title, kind: 'bar' | 'line', series() -> [{ label, color, value(b) }],
    //   min, max(), step?, tick(v), refs()?, sub(), tip(b) -> nodes, controls? }
    constructor(o) {
      this.o = o;
      this.hover = null;
      this.sub = h('span', { class: 'sub' });
      this.legend = h('div', { class: 'legend' });
      this.canvas = h('canvas', { role: 'img', 'aria-label': `${o.title}: график; те же числа — в таблице ниже` });
      this.plot = h('div', { class: 'plot' }, this.canvas);
      this.root = h('section', { class: 'chart', 'aria-label': o.title },
        h('header', null, h('span', { class: 'title' }, o.title), this.sub, o.controls || null), this.legend, this.plot);
      this.canvas.addEventListener('pointermove', (e) => this.onMove(e));
      this.canvas.addEventListener('pointerleave', () => { this.hover = null; tooltip.hidden = true; this.draw(); });
      new ResizeObserver(() => this.draw()).observe(this.plot);
    }

    update() {
      if (!REPORT) return;
      setText(this.sub, this.o.sub());
      const ser = this.o.series();
      const refs = this.o.refs ? this.o.refs() : [];
      const items = [];
      if (ser.length > 1 || refs.length) {
        for (const x of ser) {
          const k = h('span', { class: `key${this.o.kind === 'bar' ? '' : ' line'}` });
          if (this.o.kind === 'bar') k.style.background = `var(${x.color})`; else k.style.borderTop = `2px solid var(${x.color})`;
          items.push(h('span', null, k, x.label));
        }
        for (const r of refs) items.push(h('span', null, h('span', { class: `key ${r.cls}` }), `${r.label} `, h('b', null, r.text)));
      }
      this.legend.replaceChildren(...items);
      this.legend.hidden = !items.length;
      this.draw();
    }

    geometry() {
      const w = this.plot.clientWidth;
      const ht = this.plot.clientHeight;
      const pad = { l: 46, r: 12, t: 8, b: 24 };
      const n = REPORT.buckets.length;
      const pw = Math.max(10, w - pad.l - pad.r);
      const ph = Math.max(10, ht - pad.t - pad.b);
      const lo = this.o.min || 0;
      let hi = Math.max(lo + 1e-6, this.o.max());
      const step = this.o.step || niceStep((hi - lo) / 4);
      if (!this.o.step) hi = lo + Math.ceil((hi - lo) / step - 1e-9) * step;   // auto scale: end on a gridline
      const slot = pw / Math.max(1, n);
      return {
        w, ht, pad, n, pw, ph, slot, lo, hi, step,
        X: (i) => pad.l + slot * (i + 0.5),
        Y: (v) => pad.t + ph - ((Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo)) * ph,
      };
    }

    draw() {
      if (!REPORT || TAB !== 'stats' || !this.plot.clientWidth) return;
      const dpr = window.devicePixelRatio || 1;
      const g = this.geometry();
      this.canvas.width = Math.round(g.w * dpr);
      this.canvas.height = Math.round(g.ht * dpr);
      const c = this.canvas.getContext('2d');
      c.setTransform(dpr, 0, 0, dpr, 0, 0);
      c.clearRect(0, 0, g.w, g.ht);
      const col = { grid: cssVar('--grid'), axis: cssVar('--axis'), muted: cssVar('--muted'), surface: cssVar('--surface'), hl: cssVar('--surface-2') };
      c.font = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
      const B = REPORT.buckets;
      if (this.hover != null) {
        c.fillStyle = col.hl;
        c.fillRect(g.pad.l + g.slot * this.hover, g.pad.t, g.slot, g.ph);
      }
      c.lineWidth = 1;
      c.textAlign = 'right';
      c.textBaseline = 'middle';
      for (let v = g.lo; v <= g.hi + 1e-9; v += g.step) {
        const y = Math.round(g.Y(v)) + 0.5;
        c.strokeStyle = v === g.lo ? col.axis : col.grid;
        c.beginPath(); c.moveTo(g.pad.l, y); c.lineTo(g.w - g.pad.r, y); c.stroke();
        c.fillStyle = col.muted;
        c.fillText(this.o.tick(+v.toFixed(6)), g.pad.l - 6, y);
      }
      c.textAlign = 'center';
      c.textBaseline = 'top';
      c.fillStyle = col.muted;
      const every = Math.max(1, Math.ceil(g.n / Math.max(2, Math.floor(g.pw / 52))));
      for (let i = 0; i < g.n; i += 1) {
        if ((g.n - 1 - i) % every === 0) c.fillText(bucketLabel(B[i]), g.X(i), g.pad.t + g.ph + 6);
      }
      for (const r of (this.o.refs ? this.o.refs() : [])) {
        const y = Math.round(g.Y(r.v)) + 0.5;
        c.strokeStyle = cssVar(r.color); c.lineWidth = 1.5; c.setLineDash(r.dash);
        c.beginPath(); c.moveTo(g.pad.l, y); c.lineTo(g.w - g.pad.r, y); c.stroke();
      }
      c.setLineDash([]);
      const ser = this.o.series();
      let any = false;
      if (this.o.kind === 'bar') {
        const x0 = ser[0];
        c.fillStyle = cssVar(x0.color);
        const bw = Math.max(2, Math.min(28, g.slot * 0.72, g.slot - 2));   // at least a 2px gap between bars
        const base = g.Y(g.lo);
        for (let i = 0; i < g.n; i += 1) {
          const v = x0.value(B[i]);
          if (v == null || v <= 0) continue;
          any = true;
          const top = Math.min(base - 1, g.Y(v));
          const l = g.X(i) - bw / 2; const r = l + bw;
          const rad = Math.min(4, bw / 2, base - top);
          c.beginPath();
          c.moveTo(l, base); c.lineTo(l, top + rad); c.arcTo(l, top, l + rad, top, rad);
          c.lineTo(r - rad, top); c.arcTo(r, top, r, top + rad, rad); c.lineTo(r, base); c.closePath();
          c.fill();
        }
      } else {
        for (const x of ser) {
          const color = cssVar(x.color);
          const vals = B.map((b) => x.value(b));
          c.strokeStyle = color; c.lineWidth = 2; c.lineJoin = 'round'; c.lineCap = 'round';
          c.beginPath();
          let pen = false;
          for (let i = 0; i < g.n; i += 1) {
            if (vals[i] == null) { pen = false; continue; }
            any = true;
            if (pen) c.lineTo(g.X(i), g.Y(vals[i])); else { c.moveTo(g.X(i), g.Y(vals[i])); pen = true; }
          }
          c.stroke();
          for (let i = 0; i < g.n; i += 1) {
            if (vals[i] == null) continue;
            const alone = (i === 0 || vals[i - 1] == null) && (i === g.n - 1 || vals[i + 1] == null);
            if (g.n > 31 && !alone && i !== this.hover) continue;
            c.beginPath(); c.arc(g.X(i), g.Y(vals[i]), i === this.hover ? 4.5 : 3.5, 0, Math.PI * 2);
            c.fillStyle = color; c.fill(); c.lineWidth = 2; c.strokeStyle = col.surface; c.stroke();
          }
        }
      }
      if (!any) {
        c.fillStyle = col.muted;
        c.textAlign = 'center';
        c.textBaseline = 'middle';
        c.fillText('За этот период данных нет', g.pad.l + g.pw / 2, g.pad.t + g.ph / 2);
      }
    }

    onMove(e) {
      if (!REPORT) return;
      const g = this.geometry();
      const rect = this.canvas.getBoundingClientRect();
      const i = Math.floor((e.clientX - rect.left - g.pad.l) / g.slot);
      if (i < 0 || i >= g.n) { this.hover = null; tooltip.hidden = true; this.draw(); return; }
      if (this.hover !== i) { this.hover = i; this.draw(); }
      const b = REPORT.buckets[i];
      tooltip.replaceChildren(h('div', { class: 't' }, bucketLabel(b, true)),
        ...(b.hours ? this.o.tip(b) : [h('div', { class: 'r' }, h('span', null, 'менеджер не работал'))]));
      tooltip.hidden = false;
      const tw = tooltip.offsetWidth; const th = tooltip.offsetHeight;
      let left = e.clientX + 14; let top = e.clientY + 14;
      if (left + tw > window.innerWidth - 8) left = e.clientX - tw - 14;
      if (top + th > window.innerHeight - 8) top = e.clientY - th - 14;
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${top}px`;
    }
  }

  const computeChart = new BucketChart({
    title: 'Сколько шли расчёты', kind: 'bar',
    series: () => [{ label: 'расчёты', color: '--s-tasks',
      value: (b) => (b.hours ? (REPORT.bucket === 'hour' ? b.compute_min : b.compute_min / 60) : null) }],
    max: () => (REPORT.bucket === 'hour' ? 60 : Math.max(1, ...REPORT.buckets.map((b) => b.compute_min / 60))),
    step: undefined,
    tick: (v) => (REPORT.bucket === 'hour' ? `${v} мин` : `${nf2.format(v)} ч`),
    sub: () => (REPORT.bucket === 'hour' ? 'минут за каждый час' : 'часов за каждый день'),
    tip: (b) => [
      tipRow('расчёты', mins(b.compute_min), '--s-tasks'),
      tipRow('процессор задачам', `${n1(b.tasks_cpu_core_h)} ядро-ч`),
      b.tasks_gpu_h >= 0.01 ? tipRow('видеокарта задачам', `${nf2.format(b.tasks_gpu_h)} ч`) : null,
      tipRow('задачи', `запущено ${b.tasks_started} · готово ${b.tasks_done} · ошибок ${b.tasks_failed}`),
    ],
  });
  const compSeg = h('div', { class: 'seg small', role: 'group', 'aria-label': 'Компонент' },
    Object.entries(COMP_NAMES).map(([k, v]) => h('button', { type: 'button', 'data-comp': k, 'aria-pressed': String(k === STAT_COMP) }, v)));
  $$('button', compSeg).forEach((b) => b.addEventListener('click', () => {
    STAT_COMP = b.dataset.comp;
    $$('button', compSeg).forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
    tempStatChart.update();
  }));
  const tempStatChart = new BucketChart({
    title: 'Температура', kind: 'line', min: 20, max: () => 110, step: 20, controls: compSeg,
    series: () => [
      { label: 'максимум', color: '--s-tasks', value: (b) => b[`${STAT_COMP}_max_c`] },
      { label: 'в среднем во время расчётов', color: '--s-user', value: (b) => b[`${STAT_COMP}_load_avg_c`] },
    ],
    refs: () => [
      { v: REPORT.hot[STAT_COMP], color: '--critical', dash: [5, 4], cls: 'thr-hot', label: 'перегрев', text: `${REPORT.hot[STAT_COMP]}°` },
      { v: REPORT.targets[STAT_COMP], color: '--warning', dash: [5, 4], cls: 'thr-warm', label: 'цель', text: `${REPORT.targets[STAT_COMP]}°` },
    ],
    tick: (v) => `${v}°`,
    sub: () => `${COMP_NAMES[STAT_COMP]}, ${REPORT.bucket === 'hour' ? 'по часам' : 'по дням'}`,
    tip: (b) => [
      tipRow('максимум', deg(b[`${STAT_COMP}_max_c`]), '--s-tasks'),
      tipRow('в среднем во время расчётов', deg(b[`${STAT_COMP}_load_avg_c`]), '--s-user'),
      tipRow('в среднем за всё время', deg(b[`${STAT_COMP}_avg_c`])),
      tipRow('выше цели', mins(b[`${STAT_COMP}_over_target_min`])),
    ],
  });
  const statCharts = [computeChart, tempStatChart];
  $('#stats-charts').append(...statCharts.map((c) => c.root));

  function renderStatsTiles(rep) {
    const t = rep.total;
    const obsH = t.observed_min / 60;
    const others = ['board', 'gpu', 'ssd'];
    $('#stats-tiles').replaceChildren(
      tile('Расчёты шли', n1(t.compute_min / 60), 'ч', null, [
        `${t.observed_min > 0 ? Math.round((t.compute_min / t.observed_min) * 100) : 0}% времени, пока работал менеджер (${n1(obsH)} ч)`,
        kv(null, 'Процессор задачам', `${n1(t.tasks_cpu_core_h)} ядро-ч`),
        kv(null, 'Видеокарта задачам', `${nf2.format(t.tasks_gpu_h)} ч`),
        kv(null, 'Задачи', `запущено ${t.tasks_started} · готово ${t.tasks_done} · ошибок ${t.tasks_failed}`),
      ]),
      tile('Процессор: максимум', t.cpu_max_c == null ? '–' : String(Math.round(t.cpu_max_c)), '°C', null, [
        kv(null, 'В среднем во время расчётов', deg(t.cpu_load_avg_c)),
        kv(null, `Выше цели ${rep.targets.cpu}°`, mins(t.cpu_over_target_min)),
        kv(null, 'Регулятор урезал процессор', `${mins(t.regulator_min)}${t.regulator_min >= 1 && t.cpu_share_pct != null ? `; задачам в среднем ${Math.round(t.cpu_share_pct)}%` : ''}`),
      ]),
      tile('Перегревы', String(t.heat_freezes), times(t.heat_freezes), null, [
        kv(null, 'Расчёты стояли из-за перегрева', mins(t.heat_frozen_min)),
        kv(null, 'Максимум', others.map((c) => `${COMP_SHORT[c]} ${deg(t[`${c}_max_c`])}`).join(' · ')),
        kv(null, 'Выше цели', others.map((c) => `${COMP_SHORT[c]} ${mins(t[`${c}_over_target_min`])}`).join(' · ')),
      ]),
      tile('Задачи записали', n1(t.disk_write_gb), 'ГБ', null, [
        kv(null, 'Прочитали', `${n1(t.disk_read_gb)} ГБ`),
        kv(null, 'Меньше всего свободно на системном диске', t.disk_free_min_gb == null ? '–' : gb(t.disk_free_min_gb)),
        kv(null, 'Стояли из-за места на диске', t.disk_freezes ? `${mins(t.disk_frozen_min)} (${t.disk_freezes} ${times(t.disk_freezes)})` : 'ни разу'),
      ]));
  }

  function renderStatsTable(rep) {
    setText($('#stats-table-title'), rep.bucket === 'hour' ? 'По часам' : 'По дням');
    const head = [rep.bucket === 'hour' ? 'Час' : 'День', 'Расчёты', 'CPU задачам, ядро-ч', 'GPU задачам, ч',
      'Макс. °C: CPU · плата · GPU · SSD', 'CPU при расчётах', 'CPU выше цели', 'Регулятор', 'Перегревы', 'Записано, ГБ',
      'Готово / ошибок'];
    $('#t-stats thead').replaceChildren(h('tr', null, head.map((x, i) => h('th', { class: i ? 'num' : null }, x))));
    const num = (x) => h('td', { class: 'num' }, x);
    $('#t-stats tbody').replaceChildren(...[...rep.buckets].reverse().map((b) => (b.hours
      ? h('tr', null, h('td', { class: 'nowrap' }, bucketLabel(b, true)), num(mins(b.compute_min)), num(n1(b.tasks_cpu_core_h)),
        num(nf2.format(b.tasks_gpu_h)),
        num(['cpu', 'board', 'gpu', 'ssd'].map((c) => (b[`${c}_max_c`] == null ? '–' : Math.round(b[`${c}_max_c`]))).join(' · ')),
        num(deg(b.cpu_load_avg_c)), num(mins(b.cpu_over_target_min)), num(mins(b.regulator_min)),
        num(b.heat_freezes ? `${b.heat_freezes} (${mins(b.heat_frozen_min)})` : '0'), num(n1(b.disk_write_gb)),
        num(`${b.tasks_done} / ${b.tasks_failed}`))
      : h('tr', { class: 'off' }, h('td', { class: 'nowrap' }, bucketLabel(b, true)), h('td', { colspan: head.length - 1 }, 'менеджер не работал')))));
  }

  function renderStatsAgents(rep) {
    const tbody = $('#t-stats-agents tbody');
    if (!rep.agents.length) {
      tbody.replaceChildren(h('tr', { class: 'empty' }, h('td', { colspan: 9 }, 'За этот период задач не было')));
      return;
    }
    const num = (x) => h('td', { class: 'num' }, x);
    tbody.replaceChildren(...rep.agents.map((a) => h('tr', null,
      h('td', null, a.agent === 'user' ? 'Вы (дашборд)' : a.agent), num(`${n1(a.task_h)} ч`), num(n1(a.cpu_core_h)),
      num(nf2.format(a.gpu_h)), num(n1(a.ram_gb_h)), num(n1(a.disk_write_gb)), num(String(a.started)),
      num(`${a.done} / ${a.failed}`), num(`${a.cancelled} / ${a.requeued}`))));
  }

  async function download(name) {
    try {
      const res = await fetch(`/api/stats/files/${encodeURIComponent(name)}`, { headers: { Authorization: `Bearer ${TOKEN}` } });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const url = URL.createObjectURL(await res.blob());
      const a = h('a', { href: url, download: name });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    } catch (e) { alert(`Не удалось скачать ${name}: ${e.message}`); }
  }

  function renderStatsFiles(rep) {
    const box = $('#stats-files');
    const sig = `${rep.folder}|${rep.files.map((f) => `${f.name}|${f.size}`).join(',')}`;
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    const note = (n) => FILE_NOTES[n] || (n.startsWith('minutes-') ? `поминутно за ${n.slice(8, 15)}` : '');
    fill(box,
      h('p', { class: 'small ink2' }, 'Вся статистика сохраняется в папке ', h('code', null, rep.folder),
        '. Это CSV (разделитель — запятая, дробная часть — через точку): открывается в Excel через «Данные → Из текстового/CSV-файла», в pandas и подобном. Поминутные файлы хранятся 12 месяцев, остальные — всегда.'),
      h('ul', { class: 'files' }, rep.files.map((f) => h('li', null, h('code', null, f.name),
        h('span', { class: 'muted small' }, `${fsize(f.size)} — ${note(f.name)}`),
        h('button', { class: 'btn', type: 'button', onclick: () => download(f.name) }, 'Скачать')))));
  }

  function renderStats() {
    if (!REPORT) return;
    renderStatsTiles(REPORT);
    statCharts.forEach((c) => c.update());
    renderStatsTable(REPORT);
    renderStatsAgents(REPORT);
    renderStatsFiles(REPORT);
    const since = REPORT.since ? new Date(REPORT.since.replace(' ', 'T')).toLocaleString('ru-RU', {
      day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : null;
    setText($('#stats-note'), since ? `Статистика копится с ${since}; обновляется раз в минуту.` : 'Статистика копится с этого часа.');
  }

  async function loadStats() {
    statsLoadedAt = Date.now();
    try {
      REPORT = await api('GET', `/api/stats?period=${PERIOD}`);
      if (TAB === 'stats') renderStats();
    } catch (e) { setText($('#stats-note'), e.message); }
  }
  $$('#stats-seg button').forEach((b) => b.addEventListener('click', () => {
    PERIOD = b.dataset.period;
    $$('#stats-seg button').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
    loadStats();
  }));

  // ================================================================ chronicle: the life of every task
  let CHRON = null;
  let CHRON_DAYS = 7;
  let chronLoadedAt = 0;
  const SEG_NAMES = { run: 'работала', wait: 'ждала в очереди', frozen: 'заморожена' };
  const SEG_COLORS = { run: '--s-tasks', wait: '--seg-wait', frozen: '--s-frozen' };
  const AGENT_COLORS = ['--c1', '--c2', '--c3'];   // categorical slots 1-3: they hold apart in any pair
  const DAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
  const lc1 = (x) => (x ? x.charAt(0).toLowerCase() + x.slice(1) : x);
  const when = (t) => new Date(t * 1000).toLocaleString('ru-RU', CHRON && CHRON.days > 1
    ? { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' } : { hour: '2-digit', minute: '2-digit' });

  function agentColor(id) {
    const i = CHRON ? CHRON.agents.findIndex((a) => a.id === id) : -1;
    return i >= 0 && i < AGENT_COLORS.length ? AGENT_COLORS[i] : '--c-other';
  }
  const agentLabel = (name) => (name === 'user' ? 'вы (дашборд)' : name);

  function outcomeText(t) {
    if (t.state === 'suspended' && t.reason === 'hung') return 'зависла — заморожена';
    return {
      done: 'готово', failed: `ошибка${t.exit_code != null ? ` (код ${t.exit_code})` : ''}`, cancelled: 'отменена',
      timeout: 'лимит времени', lost: 'потеряна', running: 'выполняется', queued: 'в очереди', suspended: 'на паузе',
    }[t.state] || t.state;
  }

  function fitText(c, text, maxW) {
    if (maxW <= 0) return '';
    if (c.measureText(text).width <= maxW) return text;
    let lo = 0; let hi = text.length;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (c.measureText(`${text.slice(0, mid)}…`).width <= maxW) lo = mid; else hi = mid - 1;
    }
    return lo ? `${text.slice(0, lo)}…` : '';
  }

  function pill(c, x, y, w, ht, rl, rr) {
    rl = Math.min(rl, w / 2, ht / 2);
    rr = Math.min(rr, w / 2, ht / 2);
    c.beginPath();
    c.moveTo(x + rl, y);
    c.lineTo(x + w - rr, y);
    c.arcTo(x + w, y, x + w, y + rr, rr);
    c.lineTo(x + w, y + ht - rr);
    c.arcTo(x + w, y + ht, x + w - rr, y + ht, rr);
    c.lineTo(x + rl, y + ht);
    c.arcTo(x, y + ht, x, y + ht - rl, rl);
    c.lineTo(x, y + rl);
    c.arcTo(x, y, x + rl, y, rl);
    c.closePath();
    c.fill();
  }

  function timeTicks(t0, t1, width) {
    const steps = [900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 432000, 604800];
    const step = steps.find((x) => (t1 - t0) / x <= Math.max(2, Math.floor(width / 84))) || 604800;
    const off = -new Date().getTimezoneOffset() * 60;
    const ticks = [];
    for (let t = Math.ceil((t0 + off) / step) * step - off; t <= t1; t += step) ticks.push(t);
    const fmt = step >= 86400 ? (t) => new Date(t * 1000).toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' }) : (t) => clock(t);
    return { ticks, fmt };
  }

  function placeTooltip(e) {
    tooltip.hidden = false;
    const tw = tooltip.offsetWidth; const th = tooltip.offsetHeight;
    let left = e.clientX + 14; let top = e.clientY + 14;
    if (left + tw > window.innerWidth - 8) left = e.clientX - tw - 14;
    if (top + th > window.innerHeight - 8) top = e.clientY - th - 14;
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  }

  function tableToggle(chart) {
    const b = h('button', { class: 'btn tbtn', type: 'button', 'aria-pressed': 'false' }, 'Таблица');
    b.addEventListener('click', () => {
      chart.showTable = !chart.showTable;
      b.setAttribute('aria-pressed', String(chart.showTable));
      setText(b, chart.showTable ? 'График' : 'Таблица');
      chart.update();
    });
    return b;
  }

  // ---------------------------------------------------------------- the task timeline
  class TimelineChart {
    constructor() {
      this.hover = null;
      this.showTable = false;
      this.sub = h('span', { class: 'sub' });
      this.legend = h('div', { class: 'legend' });
      this.axis = h('canvas', { 'aria-hidden': 'true' });
      this.canvas = h('canvas', { role: 'img', 'aria-label': 'Лента задач: когда каждая задача ждала, работала и была заморожена; те же данные — в таблице' });
      this.axisBox = h('div', { class: 'tl-axis' }, this.axis);
      this.scroll = h('div', { class: 'tl-scroll' }, this.canvas);
      this.tableEl = h('div', { class: 'table-view tl-table', hidden: true });
      this.empty = h('div', { class: 'na', hidden: true }, 'За этот период задач не было');
      this.root = h('section', { class: 'chart wide', 'aria-label': 'Лента задач' },
        h('header', null, h('span', { class: 'title' }, 'Лента задач'), this.sub, tableToggle(this)),
        this.legend, this.axisBox, this.scroll, this.tableEl, this.empty);
      const key = (k) => { const x = h('span', { class: 'key' }); x.style.background = `var(${SEG_COLORS[k]})`; return h('span', null, x, SEG_NAMES[k]); };
      this.legend.append(key('run'), key('wait'), key('frozen'),
        h('span', null, h('b', { class: 'ok-ink' }, '✓'), ' готово'), h('span', null, h('b', { class: 'bad-ink' }, '✕'), ' ошибка'),
        h('span', null, h('span', { class: 'key tick' }), 'перезапуск'), h('span', null, h('span', { class: 'key tick bad' }), 'зависла'));
      this.canvas.addEventListener('pointermove', (e) => this.onMove(e));
      this.canvas.addEventListener('pointerleave', () => { this.hover = null; tooltip.hidden = true; this.draw(); });
      this.canvas.addEventListener('click', () => { if (this.hover) openTask(this.hover.task.id); });
      new ResizeObserver(() => this.draw()).observe(this.scroll);
    }

    update() {
      if (!CHRON) return;
      const tasks = CHRON.tasks;
      const frozen = tasks.reduce((a, t) => a + t.totals.frozen, 0);
      const why = {};
      for (const t of tasks) for (const x of t.segments) if (x[2] === 'frozen' && x[3]) why[x[3]] = (why[x[3]] || 0) + (x[1] - x[0]);
      const top = Object.entries(why).sort((a, b) => b[1] - a[1])[0];
      const zoom = tasks.length && this.span()[0] > CHRON.from + 3600 ? `, с ${when(this.span()[0])}` : '';
      setText(this.sub, `строка — задача, слева направо — время до «сейчас»${zoom}${frozen >= 60 ? ` · заморожены суммарно ${dur(frozen)}${top ? `, чаще всего: ${lc1(top[0])}` : ''}` : ''}`);
      const none = !tasks.length;
      this.empty.hidden = !none;
      this.legend.hidden = none;
      this.axisBox.hidden = none || this.showTable;
      this.scroll.hidden = none || this.showTable;
      this.tableEl.hidden = none || !this.showTable;
      if (this.showTable) this.renderTable(); else this.draw();
    }

    renderTable() {
      const num = (x) => h('td', { class: 'num' }, x);
      this.tableEl.replaceChildren(h('table', null,
        h('thead', null, h('tr', null, ['#', 'Задача', 'Агент', 'Работала', 'Ждала', 'Заморожена', 'Перезапусков', 'Итог']
          .map((x, i) => h('th', { class: [0, 3, 4, 5, 6].includes(i) ? 'num' : null }, x)))),
        h('tbody', null, CHRON.tasks.map((t) => h('tr', { class: 'click', onclick: () => openTask(t.id) },
          num(`#${t.id}`), h('td', null, t.name), h('td', null, agentLabel(t.agent)), num(dur(t.totals.run)),
          num(dur(t.totals.wait)), num(dur(t.totals.frozen)), num(String(t.restarts)), h('td', null, outcomeText(t)))))));
    }

    span() {
      // the axis starts a little before the first task of the period, so short histories are not a sliver
      const t1 = CHRON.to;
      const first = Math.min(...CHRON.tasks.map((t) => t.segments[0][0]));
      const t0 = Math.max(CHRON.from, first - Math.max(120, (t1 - first) * 0.03));
      return [t0, t1];
    }

    geometry() {
      const w = this.scroll.clientWidth;
      const labelW = Math.min(300, Math.max(120, Math.round(w * 0.28)));
      const pad = { l: labelW, r: 26 };
      const [t0, t1] = this.span();
      const pw = Math.max(10, w - pad.l - pad.r);
      return { w, labelW, pad, t0, t1, pw, rowH: 24,
        X: (t) => pad.l + ((Math.max(t0, Math.min(t1, t)) - t0) / (t1 - t0)) * pw };
    }

    draw() {
      if (!CHRON || this.scroll.hidden || TAB !== 'chronicle' || !this.scroll.clientWidth) return;
      const g = this.geometry();
      const tasks = CHRON.tasks;
      const H = tasks.length * g.rowH + 4;
      const dpr = window.devicePixelRatio || 1;
      const font = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
      const tk = timeTicks(g.t0, g.t1, g.pw);
      // the time axis stays in view while the rows scroll
      this.axis.width = Math.round(g.w * dpr); this.axis.height = Math.round(22 * dpr);
      this.axis.style.width = `${g.w}px`; this.axis.style.height = '22px';
      const a = this.axis.getContext('2d');
      a.setTransform(dpr, 0, 0, dpr, 0, 0); a.clearRect(0, 0, g.w, 22);
      a.font = font; a.fillStyle = cssVar('--muted'); a.textAlign = 'center'; a.textBaseline = 'middle';
      for (const t of tk.ticks) {
        const x = g.X(t);
        if (x > g.pad.l + 16 && x < g.w - g.pad.r - 16) a.fillText(tk.fmt(t), x, 11);
      }
      this.canvas.width = Math.round(g.w * dpr); this.canvas.height = Math.round(H * dpr);
      this.canvas.style.width = `${g.w}px`; this.canvas.style.height = `${H}px`;
      const c = this.canvas.getContext('2d');
      c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, g.w, H);
      const col = { grid: cssVar('--grid'), ink: cssVar('--ink'), ink2: cssVar('--ink-2'), muted: cssVar('--muted'),
        hl: cssVar('--surface-2'), good: cssVar('--good-text'), bad: cssVar('--critical-text') };
      const fills = { run: cssVar('--s-tasks'), wait: cssVar('--seg-wait'), frozen: cssVar('--s-frozen') };
      c.strokeStyle = col.grid; c.lineWidth = 1;
      for (const t of tk.ticks) {
        const x = Math.round(g.X(t)) + 0.5;
        if (x <= g.pad.l || x >= g.w - g.pad.r) continue;
        c.beginPath(); c.moveTo(x, 0); c.lineTo(x, H); c.stroke();
      }
      c.textBaseline = 'middle';
      tasks.forEach((t, i) => {
        const y = i * g.rowH; const cy = y + g.rowH / 2;
        if (this.hover && this.hover.row === i) { c.fillStyle = col.hl; c.fillRect(0, y, g.w, g.rowH); }
        c.textAlign = 'left';
        c.font = '12px system-ui, -apple-system, "Segoe UI", sans-serif';
        const name = fitText(c, `#${t.id} ${t.name}`, g.labelW - 12);
        c.fillStyle = col.ink; c.fillText(name, 0, cy);
        const used = c.measureText(name).width;
        c.font = font;
        const who = fitText(c, ` · ${agentLabel(t.agent)}`, g.labelW - 12 - used);
        if (who.length > 4) { c.fillStyle = col.muted; c.fillText(who, used, cy); }
        const segs = t.segments.filter((x) => x[1] >= g.t0);
        segs.forEach((x, j) => {
          const x0 = g.X(x[0]);
          let x1 = g.X(x[1]);
          if (j < segs.length - 1) x1 -= 1;                 // a hairline of surface between spans
          c.fillStyle = fills[x[2]];
          pill(c, x0, cy - 6, Math.max(1.5, x1 - x0), 12, j === 0 ? 3 : 0, j === segs.length - 1 ? 3 : 0);
        });
        for (const m of t.marks) {
          if (m[1] !== 'restart' && m[1] !== 'hung') continue;
          const x = Math.round(g.X(m[0])) + 0.5;
          c.strokeStyle = m[1] === 'hung' ? col.bad : col.ink; c.lineWidth = 2;
          c.beginPath(); c.moveTo(x, cy - 9); c.lineTo(x, cy + 9); c.stroke();
        }
        const end = t.marks.find((m) => OUTCOME_GLYPH[m[1]]);
        if (end) {
          const [glyph, ok] = OUTCOME_GLYPH[end[1]];
          c.font = 'bold 12px system-ui, -apple-system, "Segoe UI", sans-serif';
          c.fillStyle = ok ? col.good : end[1] === 'cancelled' ? col.muted : col.bad;
          c.textAlign = 'left';
          c.fillText(glyph, Math.min(g.w - 12, g.X(end[0]) + 5), cy);
        }
      });
    }

    onMove(e) {
      if (!CHRON) return;
      const g = this.geometry();
      const rect = this.canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const row = Math.floor((e.clientY - rect.top) / g.rowH);
      const t = CHRON.tasks[row];
      if (!t) { this.hover = null; tooltip.hidden = true; this.draw(); return; }
      const tm = g.t0 + ((x - g.pad.l) / g.pw) * (g.t1 - g.t0);
      const seg = x >= g.pad.l ? t.segments.find((z) => tm >= z[0] && tm <= z[1]) : null;
      if (!this.hover || this.hover.row !== row) { this.hover = { row, task: t }; this.draw(); }
      const tot = t.totals;
      tooltip.replaceChildren(
        h('div', { class: 't' }, `#${t.id} ${t.name} · ${agentLabel(t.agent)}`),
        seg ? tipRow(`${SEG_NAMES[seg[2]]}, ${when(seg[0])}–${when(seg[1])}`, dur(seg[1] - seg[0]), SEG_COLORS[seg[2]]) : null,
        seg && seg[3] ? h('div', { class: 'r' }, h('span', null, seg[3])) : null,
        tipRow('работала', dur(tot.run)), tipRow('ждала', dur(tot.wait)), tot.frozen ? tipRow('заморожена', dur(tot.frozen)) : null,
        t.restarts ? tipRow('перезапусков', String(t.restarts)) : null,
        tipRow('итог', outcomeText(t)),
        h('div', { class: 'r' }, h('span', { class: 'muted' }, 'щелчок — карточка задачи')));
      placeTooltip(e);
    }
  }
  const OUTCOME_GLYPH = { done: ['✓', true], failed: ['✕', false], timeout: ['✕', false], lost: ['✕', false], cancelled: ['–', false] };

  // ---------------------------------------------------------------- request vs reality
  class RequestScatter {
    constructor() {
      this.mode = 'ram';
      this.hover = null;
      this.showTable = false;
      this.seg = h('div', { class: 'seg small', role: 'group', 'aria-label': 'Что сравнивать' },
        [['ram', 'Память'], ['cpu', 'Процессор']].map(([k, v]) => h('button', { type: 'button', 'data-k': k, 'aria-pressed': String(k === 'ram') }, v)));
      $$('button', this.seg).forEach((b) => b.addEventListener('click', () => {
        this.mode = b.dataset.k;
        $$('button', this.seg).forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
        this.update();
      }));
      this.sub = h('span', { class: 'sub' });
      this.legend = h('div', { class: 'legend' });
      this.canvas = h('canvas', { role: 'img', 'aria-label': 'Заявка и реальность: заявленные ресурсы против использованных; те же данные — в таблице' });
      this.plot = h('div', { class: 'plot chron-plot' }, this.canvas);
      this.tableEl = h('div', { class: 'table-view chron-table', hidden: true });
      this.root = h('section', { class: 'chart', 'aria-label': 'Заявка и реальность' },
        h('header', null, h('span', { class: 'title' }, 'Заявка и реальность'), this.sub,
          h('span', { class: 'tools' }, this.seg, tableToggle(this))),
        this.legend, this.plot, this.tableEl);
      this.canvas.addEventListener('pointermove', (e) => this.onMove(e));
      this.canvas.addEventListener('pointerleave', () => { this.hover = null; tooltip.hidden = true; this.draw(); });
      this.canvas.addEventListener('click', () => { if (this.hover) openTask(this.hover.t.id); });
      new ResizeObserver(() => this.draw()).observe(this.plot);
    }

    points() {
      const out = [];
      for (const t of (CHRON ? CHRON.tasks : [])) {
        if (t.run_s < 60) continue;
        if (this.mode === 'ram') { if (t.ram > 0 && t.peak_ram > 0.001) out.push({ t, x: t.ram, y: t.peak_ram }); }
        else if (t.cpu > 0 && t.cpu_s > 0) out.push({ t, x: t.cpu, y: Math.max(0.01, t.cpu_s / t.run_s) });
      }
      return out;
    }

    fmt(v) {
      if (this.mode === 'ram') return v < 1 ? `${Math.round(v * 1024)} МБ` : `${nf2.format(v)} ГБ`;
      return `${nf2.format(v)} яд.`;
    }

    update() {
      if (!CHRON) return;
      const pts = this.points();
      const ratios = pts.map((q) => q.y / q.x).sort((a, b) => a - b);
      const med = ratios.length ? ratios[Math.floor((ratios.length - 1) / 2)] : null;
      setText(this.sub, med == null ? 'задачи короче минуты не в счёт'
        : `обычно задачи используют ${Math.round(med * 100)}% заявленной ${this.mode === 'ram' ? 'памяти (пик)' : 'процессорной мощности (в среднем)'}`);
      const seen = new Map();
      for (const q of pts) seen.set(agentColor(q.t.agent_id) === '--c-other' ? -1 : q.t.agent_id, q.t.agent);
      this.legend.replaceChildren(...[...seen.entries()].map(([id, name]) => {
        const k = h('span', { class: 'key dot' });
        k.style.background = `var(${id === -1 ? '--c-other' : agentColor(id)})`;
        return h('span', null, k, id === -1 ? 'другие агенты' : agentLabel(name));
      }), h('span', null, h('span', { class: 'key line' }), 'ровно по заявке'));
      this.plot.hidden = this.showTable;
      this.tableEl.hidden = !this.showTable;
      if (this.showTable) {
        const num = (x) => h('td', { class: 'num' }, x);
        this.tableEl.replaceChildren(h('table', null,
          h('thead', null, h('tr', null, h('th', null, 'Задача'), h('th', null, 'Агент'), h('th', { class: 'num' }, 'Заявлено'),
            h('th', { class: 'num' }, this.mode === 'ram' ? 'Пик' : 'В среднем'), h('th', { class: 'num' }, 'Доля'))),
          h('tbody', null, pts.sort((a, b) => a.y / a.x - b.y / b.x).map((q) => h('tr', null,
            h('td', null, `#${q.t.id} ${q.t.name}`), h('td', null, agentLabel(q.t.agent)), num(this.fmt(q.x)), num(this.fmt(q.y)),
            num(`${Math.round((q.y / q.x) * 100)}%`))))));
      } else this.draw();
    }

    geometry(pts) {
      const w = this.plot.clientWidth; const ht = this.plot.clientHeight;
      const pad = { l: 58, r: 14, t: 20, b: 30 };
      const vals = pts.flatMap((q) => [q.x, q.y]);
      const floor = this.mode === 'ram' ? 0.005 : 0.01;
      let lo = vals.length ? Math.max(floor, Math.min(...vals) / 1.8) : 0.1;
      let hi = vals.length ? Math.max(...vals) * 1.8 : 10;
      if (hi / lo < 10) { const m = Math.sqrt(hi * lo); lo = m / Math.sqrt(10); hi = m * Math.sqrt(10); }
      const L0 = Math.log10(lo); const L1 = Math.log10(hi);
      const pw = Math.max(10, w - pad.l - pad.r); const ph = Math.max(10, ht - pad.t - pad.b);
      const f = (v) => (Math.log10(Math.max(lo, Math.min(hi, v))) - L0) / (L1 - L0);
      return { w, ht, pad, lo, hi, pw, ph, X: (v) => pad.l + f(v) * pw, Y: (v) => pad.t + ph - f(v) * ph };
    }

    draw() {
      if (!CHRON || this.plot.hidden || TAB !== 'chronicle' || !this.plot.clientWidth) return;
      const pts = this.points();
      const g = this.geometry(pts);
      const dpr = window.devicePixelRatio || 1;
      this.canvas.width = Math.round(g.w * dpr); this.canvas.height = Math.round(g.ht * dpr);
      const c = this.canvas.getContext('2d');
      c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, g.w, g.ht);
      const col = { grid: cssVar('--grid'), axis: cssVar('--axis'), muted: cssVar('--muted'), ink2: cssVar('--ink-2'), surface: cssVar('--surface') };
      c.font = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
      // 1-2-5 steps; memory below 1 GB in round megabytes
      const ticks = [];
      for (let k = -3; k <= 3; k += 1) {
        for (const m of [1, 2, 5]) {
          let v = m * 10 ** k;
          if (this.mode === 'ram' && v < 1) v = (m * 10 ** (k + 3)) / 1024;   // 10, 20, 50, 100 ... MB
          if (v >= g.lo && v <= g.hi) ticks.push([v, m === 1]);
        }
      }
      for (const [v, major] of ticks) {
        const x = Math.round(g.X(v)) + 0.5; const y = Math.round(g.Y(v)) + 0.5;
        c.strokeStyle = col.grid; c.lineWidth = 1;
        c.beginPath(); c.moveTo(x, g.pad.t); c.lineTo(x, g.pad.t + g.ph); c.stroke();
        c.beginPath(); c.moveTo(g.pad.l, y); c.lineTo(g.pad.l + g.pw, y); c.stroke();
        if (!major && ticks.length > 8) continue;
        c.fillStyle = col.muted;
        c.textAlign = 'right'; c.textBaseline = 'middle'; c.fillText(this.fmt(v), g.pad.l - 6, y);
        c.textAlign = 'center'; c.textBaseline = 'top'; c.fillText(this.fmt(v), x, g.pad.t + g.ph + 6);
      }
      c.strokeStyle = col.axis;
      c.beginPath(); c.moveTo(g.pad.l + 0.5, g.pad.t); c.lineTo(g.pad.l + 0.5, g.pad.t + g.ph); c.lineTo(g.pad.l + g.pw, g.pad.t + g.ph + 0.5); c.stroke();
      // as requested: the diagonal; above it a task used more than it asked for, far below it asked for too much
      c.strokeStyle = col.ink2; c.lineWidth = 1.5; c.setLineDash([5, 4]);
      c.beginPath(); c.moveTo(g.X(g.lo), g.Y(g.lo)); c.lineTo(g.X(g.hi), g.Y(g.hi)); c.stroke(); c.setLineDash([]);
      c.fillStyle = col.muted;
      c.textAlign = 'left'; c.textBaseline = 'bottom'; c.fillText(this.mode === 'ram' ? 'пик ↑' : 'в среднем ↑', 4, g.pad.t - 6);
      c.textBaseline = 'top'; c.fillText('больше заявки', g.pad.l + 8, g.pad.t + 4);
      c.textAlign = 'right'; c.textBaseline = 'bottom'; c.fillText('заявлено с запасом', g.pad.l + g.pw - 6, g.pad.t + g.ph - 4);
      c.textAlign = 'left'; c.textBaseline = 'bottom'; c.fillText('заявлено →', g.pad.l + 6, g.ht - 2);
      if (!pts.length) {
        c.textAlign = 'center'; c.textBaseline = 'middle';
        c.fillText('Нет задач дольше минуты за этот период', g.pad.l + g.pw / 2, g.pad.t + g.ph / 2);
        return;
      }
      for (const q of pts) {
        const on = this.hover && this.hover.t.id === q.t.id;
        c.beginPath(); c.arc(g.X(q.x), g.Y(q.y), on ? 6 : 4.5, 0, Math.PI * 2);
        c.fillStyle = cssVar(agentColor(q.t.agent_id)); c.fill();
        c.lineWidth = 2; c.strokeStyle = col.surface; c.stroke();
      }
    }

    onMove(e) {
      const pts = this.points();
      if (!pts.length) return;
      const g = this.geometry(pts);
      const rect = this.canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left; const my = e.clientY - rect.top;
      let best = null; let bd = 14 * 14;
      for (const q of pts) {
        const d = (g.X(q.x) - mx) ** 2 + (g.Y(q.y) - my) ** 2;
        if (d < bd) { bd = d; best = q; }
      }
      if ((best && best.t) !== (this.hover && this.hover.t)) { this.hover = best; this.draw(); }
      if (!best) { tooltip.hidden = true; return; }
      const t = best.t;
      tooltip.replaceChildren(h('div', { class: 't' }, `#${t.id} ${t.name}`),
        tipRow('агент', agentLabel(t.agent), agentColor(t.agent_id)),
        tipRow('заявлено', this.fmt(best.x)), tipRow(this.mode === 'ram' ? 'пик' : 'в среднем', this.fmt(best.y)),
        tipRow('от заявки', `${Math.round((best.y / best.x) * 100)}%`),
        h('div', { class: 'r' }, h('span', { class: 'muted' }, 'щелчок — карточка задачи')));
      placeTooltip(e);
    }
  }

  // ---------------------------------------------------------------- the weekly rhythm
  class RhythmHeatmap {
    constructor() {
      this.hover = null;
      this.showTable = false;
      this.sub = h('span', { class: 'sub' });
      this.ramp = h('span', { class: 'ramp' });
      this.maxLabel = h('b');
      this.legend = h('div', { class: 'legend' }, h('span', null, 'меньше', this.ramp, 'больше · максимум ', this.maxLabel));
      this.canvas = h('canvas', { role: 'img', 'aria-label': 'Когда ПК считает: минуты расчётов по дням недели и часам; те же данные — в таблице' });
      this.plot = h('div', { class: 'plot chron-plot' }, this.canvas);
      this.tableEl = h('div', { class: 'table-view chron-table', hidden: true });
      this.root = h('section', { class: 'chart', 'aria-label': 'Когда ПК считает' },
        h('header', null, h('span', { class: 'title' }, 'Когда ПК считает'), this.sub, tableToggle(this)),
        this.legend, this.plot, this.tableEl);
      this.canvas.addEventListener('pointermove', (e) => this.onMove(e));
      this.canvas.addEventListener('pointerleave', () => { this.hover = null; tooltip.hidden = true; this.draw(); });
      new ResizeObserver(() => this.draw()).observe(this.plot);
    }

    matrix() {
      // minutes when at least one task ran, by local weekday and hour (overlapping tasks count once)
      const spans = [];
      for (const t of CHRON.tasks) {
        for (const x of t.segments) if (x[2] === 'run') spans.push([Math.max(x[0], CHRON.from), Math.min(x[1], CHRON.to)]);
      }
      spans.sort((a, b) => a[0] - b[0]);
      const merged = [];
      for (const z of spans) {
        if (z[1] <= z[0]) continue;
        const last = merged[merged.length - 1];
        if (last && z[0] <= last[1]) last[1] = Math.max(last[1], z[1]); else merged.push([z[0], z[1]]);
      }
      const m = Array.from({ length: 7 }, () => new Array(24).fill(0));
      for (const [a0, b0] of merged) {
        let t = a0;
        while (t < b0) {
          const d = new Date(t * 1000);
          const next = new Date(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours() + 1).getTime() / 1000;
          const end = Math.min(b0, next);
          m[(d.getDay() + 6) % 7][d.getHours()] += (end - t) / 60;
          t = end;
        }
      }
      return m;
    }

    update() {
      if (!CHRON) return;
      const m = this.matrix();
      let peak = null;
      m.forEach((row, r) => row.forEach((v, hh) => { if (v > 0 && (!peak || v > peak[2])) peak = [r, hh, v]; }));
      this.max = peak ? peak[2] : 0;
      setText(this.sub, peak ? `чаще всего — ${DAYS[peak[0]].toLowerCase()}, ${String(peak[1]).padStart(2, '0')}:00–${String((peak[1] + 1) % 24).padStart(2, '0')}:00`
        : 'минуты, когда шла хотя бы одна задача');
      setText(this.maxLabel, peak ? mins(peak[2]) : '–');
      const base = cssVar('--s-tasks');
      this.ramp.style.background = `linear-gradient(to right, ${rgba(base, 0.14)}, ${base})`;
      this.plot.hidden = this.showTable;
      this.tableEl.hidden = !this.showTable;
      if (this.showTable) {
        this.tableEl.replaceChildren(h('table', null,
          h('thead', null, h('tr', null, h('th', null, 'Час'), DAYS.map((d) => h('th', { class: 'num' }, d)))),
          h('tbody', null, Array.from({ length: 24 }, (_, hh) => h('tr', null, h('td', null, `${String(hh).padStart(2, '0')}:00`),
            m.map((row) => h('td', { class: 'num' }, row[hh] ? mins(row[hh]) : '–')))))));
      } else this.draw();
    }

    geometry() {
      const w = this.plot.clientWidth; const ht = this.plot.clientHeight;
      const pad = { l: 30, r: 6, t: 4, b: 22 };
      const cw = Math.max(4, (w - pad.l - pad.r) / 24); const rh = Math.max(8, (ht - pad.t - pad.b) / 7);
      return { w, ht, pad, cw, rh };
    }

    draw() {
      if (!CHRON || this.plot.hidden || TAB !== 'chronicle' || !this.plot.clientWidth) return;
      const g = this.geometry();
      const m = this.matrix();
      const dpr = window.devicePixelRatio || 1;
      this.canvas.width = Math.round(g.w * dpr); this.canvas.height = Math.round(g.ht * dpr);
      const c = this.canvas.getContext('2d');
      c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, g.w, g.ht);
      const base = cssVar('--s-tasks'); const empty = cssVar('--surface-2'); const muted = cssVar('--muted'); const ink = cssVar('--ink');
      c.font = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
      c.textBaseline = 'middle';
      for (let r = 0; r < 7; r += 1) {
        c.fillStyle = muted; c.textAlign = 'left';
        c.fillText(DAYS[r], 0, g.pad.t + r * g.rh + g.rh / 2);
        for (let hh = 0; hh < 24; hh += 1) {
          const v = m[r][hh];
          c.fillStyle = v > 0 && this.max > 0 ? rgba(base, 0.14 + 0.86 * (v / this.max)) : empty;
          const x = g.pad.l + hh * g.cw; const y = g.pad.t + r * g.rh;
          pill(c, x + 1, y + 1, g.cw - 2, g.rh - 2, 3, 3);   // a 2px surface gap between cells
          if (this.hover && this.hover[0] === r && this.hover[1] === hh) {
            c.strokeStyle = ink; c.lineWidth = 2; c.strokeRect(x + 1, y + 1, g.cw - 2, g.rh - 2);
          }
        }
      }
      c.fillStyle = muted; c.textAlign = 'center'; c.textBaseline = 'top';
      for (let hh = 0; hh < 24; hh += 3) c.fillText(String(hh), g.pad.l + hh * g.cw + g.cw / 2, g.pad.t + 7 * g.rh + 6);
    }

    onMove(e) {
      if (!CHRON) return;
      const g = this.geometry();
      const rect = this.canvas.getBoundingClientRect();
      const hh = Math.floor((e.clientX - rect.left - g.pad.l) / g.cw);
      const r = Math.floor((e.clientY - rect.top - g.pad.t) / g.rh);
      if (hh < 0 || hh > 23 || r < 0 || r > 6) { this.hover = null; tooltip.hidden = true; this.draw(); return; }
      if (!this.hover || this.hover[0] !== r || this.hover[1] !== hh) { this.hover = [r, hh]; this.draw(); }
      const v = this.matrix()[r][hh];
      tooltip.replaceChildren(h('div', { class: 't' }, `${DAYS[r]}, ${String(hh).padStart(2, '0')}:00–${String((hh + 1) % 24).padStart(2, '0')}:00`),
        tipRow('расчёты шли (за период)', v ? mins(v) : 'нет', '--s-tasks'));
      placeTooltip(e);
    }
  }

  const timeline = new TimelineChart();
  const scatter = new RequestScatter();
  const rhythm = new RhythmHeatmap();
  const chronCharts = [timeline, scatter, rhythm];
  $('#chron-charts').append(...chronCharts.map((c) => c.root));

  function renderChronFacts(tasks) {
    const box = $('#chron-facts');
    if (!tasks.length) { box.replaceChildren(); return; }
    const by = (f) => tasks.reduce((a, t) => (f(t) > f(a) ? t : a), tasks[0]);
    const longest = by((t) => t.totals.run);
    const patient = by((t) => t.totals.wait);
    const n = (st) => tasks.filter((t) => st.includes(t.state)).length;
    const agents = {};
    for (const t of tasks) {
      const a = agents[t.agent] || (agents[t.agent] = { core: 0, n: 0, run: 0 });
      a.core += t.cpu_s / 3600; a.n += 1; a.run += t.totals.run;
    }
    const [hero, hs] = Object.entries(agents).sort((a, b) => b[1].core - a[1].core)[0];
    const restarts = by((t) => t.restarts);
    box.replaceChildren(
      tile('Задач за период', String(tasks.length), '', null, [
        `готово ${n(['done'])} · ошибок ${n(['failed', 'timeout', 'lost'])} · отменено ${n(['cancelled'])}`,
        n(['running', 'queued', 'suspended']) ? `сейчас в работе: ${n(['running', 'queued', 'suspended'])}` : null,
        restarts.restarts ? `больше всех перезапусков: #${restarts.id} (${restarts.restarts})` : null,
      ].filter(Boolean)),
      tile('Самая долгая', dur(longest.totals.run), '', null, [`#${longest.id} ${longest.name}`, agentLabel(longest.agent)]),
      tile('Дольше всех ждала', dur(patient.totals.wait), '', null, [`#${patient.id} ${patient.name}`, agentLabel(patient.agent)]),
      tile('Главный трудяга', n1(hs.core), 'ядро-ч', null, [agentLabel(hero), `${hs.n} ${hs.n % 10 === 1 && hs.n % 100 !== 11 ? 'задача' : hs.n % 10 >= 2 && hs.n % 10 <= 4 && (hs.n % 100 < 12 || hs.n % 100 > 14) ? 'задачи' : 'задач'}, работали ${dur(hs.run)}`]));
  }

  function renderChronicle() {
    if (!CHRON) return;
    renderChronFacts(CHRON.tasks);
    chronCharts.forEach((c) => c.update());
    setText($('#chron-note'), 'Собрано из журнала событий (он хранится 30 дней); обновляется раз в 30 секунд.');
  }

  async function loadChronicle() {
    chronLoadedAt = Date.now();
    try {
      CHRON = await api('GET', `/api/chronicle?days=${CHRON_DAYS}`);
      if (TAB === 'chronicle') renderChronicle();
    } catch (e) { setText($('#chron-note'), e.message); }
  }
  $$('#chron-seg button').forEach((b) => b.addEventListener('click', () => {
    CHRON_DAYS = Number(b.dataset.days);
    $$('#chron-seg button').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
    loadChronicle();
  }));

  // ================================================================ the compute network
  let NETVIEW = null;
  let netLoadedAt = 0;
  let inviteCode = '';
  let netHelpOpen = false;
  const MODE_INFO = {
    two_way: ['Двусторонний', 'Ваши агенты могут считать на других ПК, а участники — на вашем (с низким приоритетом).'],
    one_way: ['Только для себя', 'Ваши агенты по-прежнему могут считать на других ПК, а ваш ПК — только ваш: задачи участников здесь стоят.'],
    offline: ['Офлайн', 'Никаких соединений: менеджер работает сам по себе, как раньше.'],
  };
  const STATUS_TEXT = {
    listening: 'на связи', offline: 'сеть выключена', no_network: 'не в сети',
    no_tailscale: 'Tailscale не запущен', error: 'ошибка',
  };
  const ago = (t) => (t ? `${dur(Date.now() / 1000 - t)} назад` : 'давно');

  async function netCall(path, body) {
    try {
      NETVIEW = await api('POST', path, body || {});
      renderNetwork();
      poll(true);
      return true;
    } catch (e) { alert(e.message); return false; }
  }

  function renderNetHead(v) {
    const ts = v.tailscale;
    const status = v.network ? STATUS_TEXT[v.status] || v.status : 'этот ПК не в сети';
    fill($('#net-head'),
      h('div', { class: 'row' },
        h('span', { class: 'title' }, v.network ? `Сеть «${v.network.name}»` : 'Вычислительная сеть'),
        h('span', { class: `net-status ${v.status === 'listening' ? 'on' : ''}` }, h('i'), status),
        h('span', { class: 'small ink2' }, `Этот ПК: ${v.node_name}`),
        h('span', { class: 'small ink2' }, ts ? `Tailscale: ${ts}` : 'Tailscale не найден: установите его и войдите (как — в инструкции ниже)')),
      v.error ? h('div', { class: 'msg err' }, v.error) : null,
      h('div', { class: 'modes', role: 'group', 'aria-label': 'Режим сети' }, Object.entries(MODE_INFO).map(([k, [title, text]]) => {
        const b = h('button', { type: 'button', 'aria-pressed': String(v.mode === k) }, h('b', null, title), h('span', null, text));
        b.addEventListener('click', () => { if (v.mode !== k) netCall('/api/network/mode', { mode: k }); });
        return b;
      })));
  }

  function netHelp(inNetwork) {
    const repo = (STATE && STATE.update && STATE.update.repo) || '';
    const where = repo ? `github.com/${repo}/releases/latest` : 'страницы релизов менеджера на GitHub';
    const steps = [
      ['Tailscale на обоих ПК.', 'Установите его с tailscale.com/download и войдите (подойдёт аккаунт Google, Microsoft или GitHub). Значок Tailscale в трее должен показывать «Connected».'],
      ['ПК видят друг друга в Tailscale.', 'Проще всего пригласить друга в свою сеть Tailscale: login.tailscale.com/admin/users → Invite users. Если в вашей сети Tailscale есть и другие ваши устройства, безопаснее обменяться только ПК: Machines → «⋯» у ПК → Share. Делиться нужно в обе стороны, иначе задачи пойдут только в одну.'],
      ['Менеджер у друга.', `Архив rmq-….zip с ${where}: распаковать в постоянную папку (например, C:\\RMQ) и запустить install.cmd. Нужен Python 3.10+ с python.org.`],
      ['Код приглашения.', 'Один ПК создаёт сеть и нажимает «Получить код приглашения», второй вставляет код в «Присоединиться по коду». Код — как пароль от сети: пересылайте его только тому, кого приглашаете.'],
      ['Брандмауэр Windows.', 'Когда Windows спросит, разрешить ли Python доступ к сети, нажмите «Разрешить доступ» — на обоих ПК. Иначе другой ПК не сможет до вас достучаться.'],
      ['Понятное имя.', 'Переименуйте ПК в карточке «Этот ПК в сети» (например, «Иван»): по этому имени агенты отправляют туда задачи (rmq submit --where Иван …).'],
    ];
    const trouble = [
      'ПК «не в сети» — на нём выключен сам ПК, менеджер или Tailscale. В Tailscale (Machines) оба ПК должны быть видны и подключены.',
      'В таблице ниже написано, что ПК не может связаться с другим, — на том ПК брандмауэр не пускает Python или ПК не поделился в Tailscale.',
      'Если Python в брандмауэре случайно запретили: Панель управления → Брандмауэр Защитника Windows → Разрешение взаимодействия с приложением → «Изменить параметры» → у Python (pythonw.exe) отметить «Частная».',
      'Включённый VPN может мешать Tailscale. Если связь пропадает только с VPN, исключите Tailscale из VPN или выключите VPN.',
      '«Часы расходятся больше чем на 5 минут» — включите в Windows «Установить время автоматически» (Параметры → Время и язык).',
    ];
    const body = [
      h('p', { class: 'small ink2' }, 'Обоим ПК нужны этот менеджер и Tailscale — бесплатная зашифрованная частная сеть, которая работает через любой роутер.'),
      h('ol', null, steps.map(([b, t]) => h('li', null, h('b', null, b), ' ', t))),
      h('h4', null, 'Если связи нет'),
      h('ul', null, trouble.map((t) => h('li', null, t))),
    ];
    if (!inNetwork) return h('div', { class: 'card net-help' }, h('h3', null, 'Как подключить ПК друга'), body);
    return h('details', { class: 'card net-help', open: netHelpOpen, ontoggle: (e) => { netHelpOpen = e.currentTarget.open; } },
      h('summary', null, h('b', null, 'Как подключить ещё один ПК и что делать, если нет связи')), body);
  }

  function netNotes(m) {
    // only for a member that is alive (its hellos arrive): then a failure in one direction is a setting, not an outage
    if (m.self || m.left || !m.online) return [];
    const out = [];
    if (m.error && m.reaches === false) out.push(['Этот ПК не может связаться с ним. Скорее всего, там брандмауэр Windows не пускает Python или ПК не поделился с вами в Tailscale.', m.error]);
    if (m.they_reach === false) out.push(['Он не может связаться с этим ПК. Разрешите Python в брандмауэре Windows (частные сети) и проверьте, что этот ПК виден ему в Tailscale.', null]);
    return out.map(([t, why]) => h('div', { class: 'net-note', title: why }, t));
  }

  function renderNetActions(v) {
    const box = $('#net-actions');
    const cards = [];
    if (!v.network) {
      const name = h('input', { id: 'net-new-name', placeholder: 'Например: Друзья', 'aria-label': 'Название сети' });
      cards.push(h('div', { class: 'card' }, h('h3', null, 'Создать сеть'),
        h('p', { class: 'small ink2' }, 'Этот ПК станет первым в сети. Потом пригласите другие ПК кодом.'),
        name, h('div', null, h('button', { class: 'btn primary', type: 'button', onclick: () => netCall('/api/network/create', { name: name.value }) }, 'Создать'))));
      const code = h('textarea', { id: 'net-join-code', placeholder: 'rmq1-…', 'aria-label': 'Код приглашения' });
      cards.push(h('div', { class: 'card' }, h('h3', null, 'Присоединиться по коду'),
        h('p', { class: 'small ink2' }, 'Вставьте код приглашения, который прислал участник сети. Нужен запущенный Tailscale, в котором ПК видят друг друга (см. инструкцию).'),
        code, h('div', null, h('button', { class: 'btn primary', type: 'button', onclick: () => netCall('/api/network/join', { code: code.value }) }, 'Присоединиться'))));
      cards.push(netHelp(false));
    } else {
      const codeBox = h('div', { class: 'invite-code', hidden: !inviteCode }, inviteCode);
      const copy = h('button', { class: 'btn', type: 'button', hidden: !inviteCode }, 'Скопировать');
      copy.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(inviteCode); setText(copy, 'Скопировано'); } catch { /* the code is selectable */ }
      });
      cards.push(h('div', { class: 'card' }, h('h3', null, 'Пригласить ПК'),
        h('p', { class: 'small ink2' }, 'Код — как пароль от сети: отправьте его только тому, кому доверяете (в мессенджере). Второму ПК нужны этот менеджер и Tailscale, в котором он видит ваш ПК.'),
        h('div', { class: 'form-actions' }, h('button', { class: 'btn primary', type: 'button', onclick: async () => {
          try { inviteCode = (await api('POST', '/api/network/invite', {})).code; renderNetActions(NETVIEW); } catch (e) { alert(e.message); }
        } }, 'Получить код приглашения'), copy), codeBox));
      const rename = h('input', { id: 'net-rename', value: v.node_name, 'aria-label': 'Имя этого ПК в сети' });
      cards.push(h('div', { class: 'card' }, h('h3', null, 'Этот ПК в сети'),
        h('label', { class: 'f' }, 'Имя, которое видят участники', rename),
        h('div', { class: 'form-actions' },
          h('button', { class: 'btn', type: 'button', onclick: () => netCall('/api/network/rename', { name: rename.value }) }, 'Сохранить имя'),
          h('button', { class: 'btn danger', type: 'button', onclick: () => {
            if (confirm(`Выйти из сети «${v.network.name}»? Ваши задачи на других ПК и задачи участников здесь будут отменены. Вернуться можно по новому приглашению.`)) {
              inviteCode = '';
              netCall('/api/network/leave');
            }
          } }, 'Выйти из сети'))));
      cards.push(netHelp(true));
    }
    box.replaceChildren(...cards);
  }

  function renderNodes(v) {
    const tbody = $('#t-nodes tbody');
    const rowsN = v.members.filter((m) => !m.left || m.left_at > Date.now() / 1000 - 7 * 86400);
    if (!v.network || !rowsN.length) {
      tbody.replaceChildren(h('tr', { class: 'empty' }, h('td', { colspan: 9 }, 'Этот ПК не в сети: создайте сеть или присоединитесь по коду')));
      return;
    }
    const num = (x) => h('td', { class: 'num' }, x);
    tbody.replaceChildren(...rowsN.map((m) => {
      const info = m.info || {};
      const hw = info.hw || {};
      const free = info.free || {};
      const busy = info.busy || {};
      const tasks = info.tasks || {};
      const flags = [busy.game ? 'игра' : null, busy.paused ? 'пауза' : null, busy.heat === 'hot' ? 'перегрев' : null, busy.disk_low ? 'мало места' : null].filter(Boolean);
      let link;
      if (m.self) link = h('span', { class: 'net-status self' }, h('i'), 'этот ПК');
      else if (m.left) link = h('span', { class: 'net-status' }, h('i'), `вышел из сети ${ago(m.left_at)}`);
      else if (m.online) link = h('span', { class: 'net-status on' }, h('i'), `на связи${m.rtt_ms != null ? ` · ${m.rtt_ms} мс` : ''}`);
      else link = h('span', { class: 'net-status' }, h('i'), `не в сети${m.seen ? ` · был ${ago(m.seen)}` : ''}`);
      const known = m.online || m.self;
      return h('tr', { class: m.left ? 'off' : null },
        h('td', null, h('div', { class: 'name' }, m.name), h('div', { class: 'cmd' }, hw.os || '')),
        h('td', null, link, netNotes(m)),
        h('td', null, known ? `${(MODE_INFO[info.mode] || ['?'])[0]}${flags.length ? ` · ${flags.join(', ')}` : ''}` : '–'),
        h('td', null, hw.cpu ? `${hw.cpu}, ${hw.threads} потоков` : '–'),
        num(hw.ram_gb ? `${n1(hw.ram_gb)} ГБ` : '–'),
        h('td', null, hw.gpu ? `${hw.gpu}, ${n1(hw.vram_gb)} ГБ` : hw.cpu ? 'нет' : '–'),
        num(known && info.free ? `CPU ${n1(free.cpu)} · ОЗУ ${n1(free.ram)} ГБ · VRAM ${n1(free.vram)} ГБ` : '–'),
        num(known && info.tasks ? `▶ ${tasks.running || 0} ◷ ${tasks.queued || 0}${info.hosted ? ` · из сети ${info.hosted}` : ''}` : '–'),
        num(info.version || '–'));
    }));
  }

  function renderNetTasks(s) {
    const list = [...s.tasks, ...s.recent].filter((t) => t.where || t.from);
    rows('#t-net-tasks', list.slice(0, 40), netTaskCols, 'Задач через сеть пока не было');
  }
  const netTaskCols = [
    ['num', textCell((t) => String(t.id))],
    [null, nameCell],
    [null, textCell((t) => t.agent)],
    [null, textCell((t) => (t.where ? `→ на ПК ${t.where}` : `← с ПК ${t.from}`))],
    [null, stateCell],
    ['num', textCell((t) => `${dur(t.run_s)} / ${dur(t.time_s)}`)],
    [null, progressCell],
  ];

  function renderUpdate(s) {
    const u = s.update || {};
    const state = {
      dev: 'это рабочая копия разработчика: обновления выпускаются отсюда, сама она не обновляется',
      idle: u.checked_at ? `установлена последняя версия (проверено ${ago(u.checked_at)})` : 'проверка ещё не проводилась',
      disabled: 'автоматические обновления выключены в настройках',
      checking: 'проверяем…', available: `доступна версия ${u.latest}, устанавливается`,
      downloading: `загружается версия ${u.latest}`, installing: `устанавливается версия ${u.latest}: менеджер перезапустится`,
      error: `не удалось проверить: ${u.error}`,
    }[u.state] || u.state;
    const sig = `${u.state}|${u.latest}|${u.checked_at}|${u.error}`;
    const box = $('#update-card');
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    fill(box, h('div', { class: 'row' }, h('b', null, `Версия ${u.version || '?'}`), ' — ', state),
      h('p', { class: 'small ink2' }, `Обновления приходят из релизов github.com/${u.repo || '?'}: выпускает их только автор. Менеджер проверяет их раз в несколько часов, когда есть интернет, сверяет контрольную сумму и перезапускается; задачи продолжают с того же места.`),
      u.state === 'dev' ? null : h('div', { class: 'form-actions' }, h('button', { class: 'btn', type: 'button', onclick: async () => {
        try { await api('POST', '/api/update/check', {}); poll(true); } catch (e) { alert(e.message); }
      } }, 'Проверить сейчас')));
  }

  function renderNetwork() {
    if (!NETVIEW) return;
    renderNetHead(NETVIEW);
    if (document.activeElement && ['net-new-name', 'net-join-code', 'net-rename'].includes(document.activeElement.id)) {
      renderNodes(NETVIEW);
      return;  // do not redraw the inputs under the owner's fingers
    }
    renderNetActions(NETVIEW);
    renderNodes(NETVIEW);
  }

  async function loadNetwork() {
    netLoadedAt = Date.now();
    try {
      NETVIEW = await api('GET', '/api/network');
      if (TAB === 'network') renderNetwork();
    } catch (e) { fill($('#net-head'), h('div', { class: 'msg err' }, e.message)); }
  }

  // ================================================================ tabs
  function tab(name) {
    TAB = name;
    $$('.tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === name)));
    $$('main > section').forEach((s) => { s.hidden = s.id !== `tab-${name}`; });
    if (name === 'rules') loadSettings();
    if (name === 'agents') loadAgentRules();
    if (name === 'overview') charts.forEach((c) => c.update());
    if (name === 'thermal') tempCharts.forEach((c) => c.update());
    if (name === 'stats') { if (REPORT) renderStats(); loadStats(); }
    if (name === 'chronicle') { if (CHRON) renderChronicle(); loadChronicle(); }
    if (name === 'network') { if (NETVIEW) renderNetwork(); loadNetwork(); }
    if (STATE) render();
  }
  $$('.tabs button').forEach((b) => b.addEventListener('click', () => tab(b.dataset.tab)));

  function syncSmooth() {
    $$('.smooth-toggle').forEach((c) => { c.checked = SMOOTH; });
  }
  $$('.smooth-toggle').forEach((c) => c.addEventListener('change', () => {
    SMOOTH = c.checked;
    try { localStorage.setItem('rmq_smooth', SMOOTH ? '1' : '0'); } catch { /* ignore */ }
    syncSmooth();
    charts.forEach((x) => x.update());
    tempCharts.forEach((x) => x.update());
  }));
  syncSmooth();

  $$('.range-seg button').forEach((b) => b.addEventListener('click', async () => {
    RANGE = Number(b.dataset.range);
    $$('.range-seg button').forEach((x) => x.setAttribute('aria-pressed', String(x.dataset.range === b.dataset.range)));
    const all = [...charts, ...tempCharts];
    all.forEach((c) => c.plot.classList.add('loading'));
    await loadMetrics(true);
    all.forEach((c) => { c.plot.classList.remove('loading'); c.update(); });
  }));

  // ================================================================ polling
  async function loadMetrics(full) {
    const since = !full && METRICS && METRICS.t.length ? METRICS.t[METRICS.t.length - 1] : 0;
    const m = await api('GET', `/api/metrics?range=${RANGE}&since=${since}`);
    if (full || !METRICS || METRICS.step !== m.step) { METRICS = m; return; }
    for (const k of Object.keys(m)) if (Array.isArray(m[k])) METRICS[k].push(...m[k]);
    const cut = METRICS.t.length ? METRICS.t[METRICS.t.length - 1] - RANGE - 5 : 0;
    let drop = 0;
    while (drop < METRICS.t.length && METRICS.t[drop] < cut) drop += 1;
    if (drop) for (const k of Object.keys(METRICS)) if (Array.isArray(METRICS[k])) METRICS[k].splice(0, drop);
  }

  function render() {
    const s = STATE;
    renderHeader(s);
    renderBanners(s);
    if (TAB === 'overview') {
      renderTiles(s);
      renderDisks(s);
      charts.forEach((c) => c.update());
    }
    if (TAB === 'stats' && Date.now() - statsLoadedAt > 60000) loadStats();
    if (TAB === 'chronicle' && Date.now() - chronLoadedAt > 30000) loadChronicle();
    if (TAB === 'network') {
      if (Date.now() - netLoadedAt > 5000) loadNetwork();
      renderNetTasks(s);
      renderUpdate(s);
    }
    renderTasks(s);
    if (TAB === 'agents') renderAgents(s);
    if (TAB === 'log') renderEvents(s.events, $('#events'));
    if (TAB === 'thermal') renderThermal(s);
  }

  let pollTimer = null;
  let stopped = false;
  let inflight = false;
  let again = false;
  async function poll(now) {
    clearTimeout(pollTimer);
    if (stopped) return;
    if (inflight) { again = true; return; }
    if (document.hidden && !now) { pollTimer = setTimeout(poll, 3000); return; }
    inflight = true;
    try {
      STATE = await api('GET', '/api/state');
      await loadMetrics(false);
      offline = false;
      render();
    } catch (e) {
      if (e.message === 'unauthorized') { inflight = false; return; }
      offline = true;
      renderBanners(STATE);
    }
    inflight = false;
    if (again) { again = false; poll(true); return; }
    pollTimer = setTimeout(poll, 1000);
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(true); });

  function start() {
    $('#app').hidden = false;
    applyTheme(theme);
    syncSmooth();
    if (START_TAB !== 'overview' && $(`.tabs button[data-tab="${START_TAB}"]`)) tab(START_TAB);
    poll(true);
    if (START_TASK) openTask(START_TASK);
  }

  initToken();
  if (!TOKEN) showLogin('');
  else start();
})();
