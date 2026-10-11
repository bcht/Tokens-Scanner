/* Tokens Scanner —— 前端

   几条约束决定了这里的结构:
   1. 自动轮询反复刷新,DOM 只建一次、之后只改数值,
      否则每次刷新都重建节点,hover 和滚动都会被打断。
   2. 热力图固定画最近 365 天,不受时间范围影响(只受「数据来源」影响)。
   3. 右侧数字默认跟随时间范围;只有合计那个大数字滚动,明细直接改值。
   4. 切换分组时整块淡出淡入,并且要能扛住"动画没跑完就又来了新动作"。 */

const DAY_MS = 86400000;
const MONTH_LABELS = ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'];
const WEEKDAY_LABELS = ['一','二','三','四','五','六','日'];
const WEEKDAY_GUTTER = 27;
const CELL_GAP = 4;
const RAMP_STEPS = 5;
const ROWS_SLIDE_MS = 165;

const QUICK_RANGES = [
  { key: '7d', label: '近 7 天', days: 7 },
  { key: '30d', label: '近 30 天', days: 30 },
  { key: '1y', label: '近一年', days: 365 },
  { key: 'all', label: '全部', days: null },
];

const GROUP_OPTIONS = [
  { key: 'parts', label: '构成' },
  { key: 'sources', label: '按来源' },
  { key: 'models', label: '按模型' },
  { key: 'scale', label: '规模' },
];

const POLL_OPTIONS = [
  { label: '30 秒', ms: 30000 },
  { label: '1 分', ms: 60000 },
  { label: '5 分', ms: 300000 },
  { label: '关', ms: 0 },
];

const LOGOS = {
  all: 'logos/all.svg',
  claude_code: 'logos/claude-code.svg',
  claude_desktop: 'logos/claude-desktop.svg',
  dsh: 'logos/dsh.svg',
  codex: 'logos/codex.svg',
};

const state = {
  data: null,
  source: 'all',
  group: 'parts',
  range: { from: null, to: null },
  quick: '1y',
  showTable: false,
  pollMs: 60000,
  thresholds: [0, 0, 0, 0],
  fatal: null,
  builtKey: null,
  sourceIds: null,
  rowSignature: null,
  initialised: false,
};

const $ = (id) => document.getElementById(id);
const numberFormat = new Intl.NumberFormat('zh-CN');
let pollTimer = 0;
let scanInFlight = false;

/* ------------------------------------------------------------- 日期工具 */

const pad = (n) => String(n).padStart(2, '0');
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

function parseYmd(text) {
  const [y, m, d] = text.split('-').map(Number);
  return new Date(y, m - 1, d);
}

function addDays(date, count) {
  const copy = new Date(date);
  copy.setDate(copy.getDate() + count);
  return copy;
}

function startOfWeek(date) {
  const copy = new Date(date);
  copy.setHours(0, 0, 0, 0);
  return addDays(copy, -((copy.getDay() + 6) % 7));
}

function todayKey() {
  const now = new Date();
  now.setHours(0, 0, 0, 0);
  return ymd(now);
}

/* --------------------------------------------------------- 数字格式化 */

const fmt = (n) => numberFormat.format(Math.round(n));

function compact(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

const sumOf = (values) => values[0] + values[1] + values[2] + values[3];

/* --------------------------------------- 滚动数字(只给合计大数字用) */

/* 轨道放两轮 0-9:9 进位到 0 时要往后滚一格,没有重复的 0 就只能倒着滚回去,
   看起来像在倒带。滚完再悄悄归位到第一轮。 */
const TRACK_SLOTS = 20;
const SETTLE_MS = 720;

function makeDigitCell(char) {
  const node = document.createElement('span');
  node.className = 'roll';
  const track = document.createElement('span');
  track.className = 'track';
  for (let slotIndex = 0; slotIndex < TRACK_SLOTS; slotIndex += 1) {
    const slot = document.createElement('b');
    slot.textContent = String(slotIndex % 10);
    track.appendChild(slot);
  }
  const position = Number(char);
  track.style.setProperty('--d', position);
  node.appendChild(track);
  return { node, track, char, position, isDigit: true, timer: 0 };
}

function makeStaticCell(char) {
  const node = document.createElement('span');
  node.className = 'sep';
  node.textContent = char;
  return { node, char, isDigit: false };
}

/** 滚到目标数字。9 -> 0 走下面那个重复的 0,才是真正的进位方向。 */
function rollTo(cell, char) {
  if (cell.position > 9) settle(cell); // 上一次进位还没归位,先归位
  const from = cell.position;
  const target = Number(char);
  const to = target >= from ? target : target + 10;
  cell.track.style.setProperty('--d', to);
  cell.position = to;
  clearTimeout(cell.timer);
  cell.timer = setTimeout(() => settle(cell), SETTLE_MS);
}

/** 滚完把轨道悄悄拨回 0-9,免得越滚越远。 */
function settle(cell) {
  const home = cell.position % 10;
  if (home === cell.position) return;
  cell.track.style.transition = 'none';
  cell.track.style.setProperty('--d', home);
  cell.position = home;
  void cell.track.offsetHeight; // 强制重排,让"无过渡"这一步立刻生效
  cell.track.style.transition = '';
}

/**
 * 更新滚动数字。
 * @param glideTarget 位数变化时一起做平移的元素。传了就把补偿位移施加在它身上 ——
 *                    这样 "tokens" 单位会跟着数字一起滑,而不是被布局甩过去。
 */
function setNumber(el, text, glideTarget) {
  if (el.dataset.text === text) return;
  el.dataset.text = text;

  const cells = el._cells || (el._cells = []);

  // 位数一变,整串会横向挪一截。先记下一个"确定会活下来"的格子的位置,
  // 改完再反向补偿、过渡回 0 —— 这样看到的是整串平移,而不是瞬移。
  const diff = text.length - cells.length;
  const anchor = cells[diff < 0 ? -diff : 0];
  const anchorLeft = anchor ? anchor.node.getBoundingClientRect().left : null;

  if (diff > 0) {
    const fresh = [];
    for (let index = 0; index < diff; index += 1) {
      const char = text[index];
      const cell = /\d/.test(char) ? makeDigitCell('0') : makeStaticCell(char);
      if (cell.isDigit) {
        cell.char = char; // 标记成已是最新值,下面那轮就跳过它
        requestAnimationFrame(() => rollTo(cell, char)); // 让新位从 0 滚上来
      }
      fresh.push(cell);
    }
    el.prepend(...fresh.map((cell) => cell.node));
    cells.unshift(...fresh);
  } else if (diff < 0) {
    for (let index = 0; index < -diff; index += 1) {
      el.removeChild(cells[0].node);
      cells.shift();
    }
  }

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    const cell = cells[index];
    if (cell.char === char) continue;

    if (cell.isDigit !== /\d/.test(char)) {
      const next = /\d/.test(char) ? makeDigitCell(char) : makeStaticCell(char);
      el.replaceChild(next.node, cell.node);
      cells[index] = next;
      continue;
    }

    cell.char = char;
    if (cell.isDigit) rollTo(cell, char);
    else cell.node.textContent = char;
  }

  if (anchor && anchorLeft !== null) {
    const delta = anchorLeft - anchor.node.getBoundingClientRect().left;
    if (Math.abs(delta) > 0.5) {
      const target = glideTarget || el;
      target.style.transition = 'none';
      target.style.transform = `translateX(${delta}px)`;
      void target.offsetWidth; // 强制重排,让补偿这一步先落下再过渡
      target.style.transition = 'transform .44s cubic-bezier(.22, .9, .25, 1)';
      target.style.transform = 'translateX(0)';
    }
  }
}

/* ------------------------------------------------------------- 数据载入 */

let loadAttempted = false;

function bridge() {
  return window.pywebview && window.pywebview.api ? window.pywebview.api : null;
}

/** 打包后走桥接;浏览器里直接打开时退回预生成的 data.json。 */
function boot() {
  if (loadAttempted) return;
  loadAttempted = true;
  const api = bridge();
  const run = api
    ? api.load()
    : fetch('data.json').then((response) => response.json());
  run.then(acceptData).catch(() => acceptData(null));
}

function acceptData(data) {
  state.fatal = data ? null : '没读到本机用量数据。确认 Python 环境正常后,点「重新扫描」再试。';
  state.data = data || {
    sources: [], daily: {}, modelDaily: {},
    totals: {}, warnings: [], firstDay: null, lastDay: null,
  };

  if (!state.initialised) {
    state.initialised = true;
    state.range = quickRange('1y');
  }

  // 来源集合一变就重建选择器
  const ids = state.data.sources.map((s) => s.id).join('|');
  if (ids !== state.sourceIds) {
    state.sourceIds = ids;
    if (state.source !== 'all' && !state.data.sources.some((s) => s.id === state.source)) {
      state.source = 'all';
    }
    buildSourcePicker();
    state.rowSignature = null;
  }

  if (state.quick === 'all') state.range = quickRange('all');
  syncDateInputs();
  paint();
}

/* --------------------------------------------------------------- 时间范围 */

function quickRange(key) {
  const option = QUICK_RANGES.find((q) => q.key === key);
  const end = todayKey();
  if (!option || option.days === null) {
    return { from: earliestDay(), to: end };
  }
  return { from: ymd(addDays(parseYmd(end), -(option.days - 1))), to: end };
}

function earliestDay() {
  let min = null;
  for (const id of selectedSourceIds()) {
    const source = (state.data.sources || []).find((s) => s.id === id);
    if (source && source.first && (!min || source.first < min)) min = source.first;
  }
  return min || (state.data && state.data.firstDay) || todayKey();
}

function inRange(day) {
  return day >= state.range.from && day <= state.range.to;
}

/* --------------------------------------------------------- 选取与聚合 */

function selectedSourceIds() {
  if (state.source !== 'all') return [state.source];
  return (state.data.sources || []).filter((s) => s.calls > 0).map((s) => s.id);
}

/** 按当前来源 + 当前时间范围汇总右侧所有数字。 */
function scoped() {
  const ids = selectedSourceIds();
  const parts = [0, 0, 0, 0];
  let calls = 0;
  const days = new Set();
  const bySource = new Map();
  const byModel = new Map();

  for (const id of ids) {
    const sourceTotal = [0, 0, 0, 0, 0];
    for (const day of Object.keys(state.data.daily[id] || {})) {
      if (!inRange(day)) continue;
      const record = state.data.daily[id][day];
      for (let i = 0; i < 4; i += 1) {
        parts[i] += record[i];
        sourceTotal[i] += record[i];
      }
      calls += record[4];
      sourceTotal[4] += record[4];
      days.add(day);
    }
    bySource.set(id, sourceTotal);

    for (const row of state.data.modelDaily[id] || []) {
      if (!inRange(row[1])) continue;
      const acc = byModel.get(row[0]) || [0, 0, 0, 0, 0];
      for (let i = 0; i < 5; i += 1) acc[i] += row[i + 2];
      byModel.set(row[0], acc);
    }
  }

  // 连续活跃天数:从区间的**结束日**(也就是最新那天)往前倒推,
  // 连着数到第一个没用量的日子为止。结束日当天没用量就是 0。
  let streak = 0;
  if (state.range.to) {
    const first = parseYmd(state.range.from);
    let cursor = parseYmd(state.range.to);
    while (cursor >= first && days.has(ymd(cursor))) {
      streak += 1;
      cursor = addDays(cursor, -1);
    }
  }

  return {
    ids,
    parts,
    calls,
    byModel,
    bySource,
    activeDays: days.size,
    streak,
    grand: parts[0] + parts[1] + parts[2] + parts[3],
  };
}

/** 当前来源下出现过的模型名,按总量降序。行集合固定成它,区间内的值可能为 0。 */
function modelOrder() {
  const totals = new Map();
  for (const id of selectedSourceIds()) {
    for (const row of state.data.modelDaily[id] || []) {
      const value = row[2] + row[3] + row[4] + row[5];
      totals.set(row[0], (totals.get(row[0]) || 0) + value);
    }
  }
  return [...totals.entries()].sort((a, b) => b[1] - a[1]).map((entry) => entry[0]);
}

/* ------------------------------------------------- 选择器(单选) */

function buildPicker(container, options, activeKey, onPick) {
  container.textContent = '';
  for (const option of options) {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.key = option.key;
    button.setAttribute('aria-pressed', String(option.key === activeKey));
    if (option.disabled) {
      button.dataset.disabled = '1';
      button.setAttribute('aria-disabled', 'true');
    }

    const label = document.createElement('span');
    label.className = 'picker-label';
    label.textContent = option.label;
    button.appendChild(label);

    if (option.meta) {
      const meta = document.createElement('span');
      meta.className = 'picker-meta';
      meta.textContent = option.meta;
      button.appendChild(meta);
    }

    button.addEventListener('click', () => onPick(option));
    container.appendChild(button);
  }
}

function syncPressed(container, key) {
  for (const button of container.children) {
    button.setAttribute('aria-pressed', String(button.dataset.key === key));
  }
}

function buildSourcePicker() {
  // LOGO 只出现在最顶上的品牌区,选择器里不放
  const options = [{ key: 'all', label: '全部来源' }];
  for (const source of state.data.sources) {
    options.push({
      key: source.id,
      label: source.label,
      meta: source.calls === 0 ? '未探测到' : compact(sumOf(state.data.totals[source.id] || [0, 0, 0, 0])),
      disabled: source.calls === 0,
    });
  }
  buildPicker($('sourcePicker'), options, state.source, (option) => {
    if (option.disabled) return;
    state.source = option.key;
    syncPressed($('sourcePicker'), state.source);
    if (state.quick === 'all') state.range = quickRange('all');
    updateBrand();
    paint();
  });
}

function buildGroupPicker() {
  buildPicker($('groupPicker'), GROUP_OPTIONS, state.group, (option) => {
    state.group = option.key;
    syncPressed($('groupPicker'), state.group);
    paint();
  });
}

function buildQuick() {
  buildPicker($('quick'), QUICK_RANGES.map((q) => ({ key: q.key, label: q.label })), state.quick,
    (option) => {
      state.quick = option.key;
      state.range = quickRange(option.key);
      syncPressed($('quick'), state.quick);
      syncDateInputs();
      paint();
    });
  syncDateInputs();
}

function buildPoll() {
  buildPicker($('poll'), POLL_OPTIONS.map((o) => ({ key: String(o.ms), label: o.label })),
    String(state.pollMs), (option) => {
      state.pollMs = Number(option.key);
      syncPressed($('poll'), String(state.pollMs));
      schedulePoll();
    });
}

function syncDateInputs() {
  if (!$('dateFrom')) return;
  $('dateFrom').value = state.range.from || '';
  $('dateTo').value = state.range.to || '';
}

/* ------------------------------------------------------- 热力图:建 + 画 */

/** 固定画最近 365 天。它只受「数据来源」影响,不受时间范围影响。 */
function buildHeatmap() {
  const end = parseYmd(todayKey());
  const start = addDays(end, -364);
  const gridStart = startOfWeek(start);
  const totalDays = Math.round((end - gridStart) / DAY_MS) + 1;
  const weekCount = Math.ceil(totalDays / 7);

  $('rangeSub').textContent = `${ymd(start)} 至 ${ymd(end)}`;

  fitCells(weekCount);
  const cellSize = cellPitch();
  const gridWidth = weekCount * cellSize - CELL_GAP;

  const months = $('months');
  const weeks = $('weeks');
  const weekdays = $('weekdays');
  months.textContent = '';
  weeks.textContent = '';
  weekdays.textContent = '';
  months.style.width = `${gridWidth}px`;
  weeks.style.width = `${gridWidth}px`;

  const monthSeen = new Set();
  state.cells = [];

  for (let index = 0; index < totalDays; index += 1) {
    const date = addDays(gridStart, index);
    const inside = date >= start && date <= end;

    const cell = document.createElement('div');
    cell.className = 'cell';
    if (inside) {
      cell.dataset.day = ymd(date);
      attachTooltip(cell);
      state.cells.push(cell);
    } else {
      cell.dataset.out = '1';
    }
    weeks.appendChild(cell);

    const week = Math.floor(index / 7);
    if (date.getDate() === 1 && !monthSeen.has(week)) {
      monthSeen.add(week);
      const label = document.createElement('span');
      label.className = 'month-label';
      label.textContent = MONTH_LABELS[date.getMonth()];
      label.style.left = `${week * cellSize}px`;
      months.appendChild(label);
    }
  }

  for (const name of WEEKDAY_LABELS) {
    const span = document.createElement('span');
    span.className = 'weekday';
    span.textContent = name === '一' || name === '三' || name === '五' ? name : '';
    weekdays.appendChild(span);
  }

  state.builtKey = 'fixed';
}

function dayStats(dateKey) {
  const parts = [0, 0, 0, 0];
  let calls = 0;
  for (const id of selectedSourceIds()) {
    const record = (state.data.daily[id] || {})[dateKey];
    if (!record) continue;
    for (let i = 0; i < 4; i += 1) parts[i] += record[i];
    calls += record[4];
  }
  return { parts, calls, total: parts[0] + parts[1] + parts[2] + parts[3] };
}

/* 分档用对数刻度:用量常常跨几十倍,按分位数分档等于按**排名**上色 ——
   10-03(52.9M)和 10-11(146.8M)差 2.8 倍却同色,而 48.3M 和 51.3M 只差 6%
   却差两档。改成在 log 空间里等分,颜色才真的对应用量。 */
function computeThresholds() {
  const values = [];
  for (const cell of state.cells || []) {
    const total = dayStats(cell.dataset.day).total;
    if (total > 0) values.push(total);
  }
  if (!values.length) return [0, 0, 0, 0];

  const lo = Math.log(Math.min(...values));
  const hi = Math.log(Math.max(...values));
  if (!(hi - lo > 1e-9)) {
    // 每天都一样多:只有一个档有意义,全部落在最浅
    const only = Math.exp(lo);
    return [only, only, only, only];
  }
  return [0.2, 0.4, 0.6, 0.8].map((q) => Math.exp(lo + (hi - lo) * q));
}

function levelOf(value) {
  if (value <= 0) return 0;
  let level = 1;
  for (const threshold of state.thresholds) if (value > threshold) level += 1;
  return Math.min(level, RAMP_STEPS);
}

function paintHeatmap() {
  state.thresholds = computeThresholds();
  for (const cell of state.cells || []) {
    cell.dataset.level = String(levelOf(dayStats(cell.dataset.day).total));
  }
}

function fitCells(columns) {
  const width = $('chart').clientWidth - WEEKDAY_GUTTER;
  const pitch = (width - (columns - 1) * CELL_GAP) / columns;
  const cell = Math.max(8, Math.min(15, Math.floor(pitch)));
  document.documentElement.style.setProperty('--cell', `${cell}px`);
}

function cellPitch() {
  const styles = getComputedStyle(document.documentElement);
  return (parseFloat(styles.getPropertyValue('--cell')) || 14)
    + (parseFloat(styles.getPropertyValue('--gap')) || 4);
}

/* 图例:平时只显示六个色块,鼠标悬停到某一档才在右侧显示它对应的用量区间 */
let legendBands = [];
let hoveredBand = null;

function legendBandText(index) {
  const th = state.thresholds;
  if (!th[3]) return index === 0 ? '没有用量' : '';
  return [
    '没有用量',
    `0 – ${compact(th[0])}`,
    `${compact(th[0])} – ${compact(th[1])}`,
    `${compact(th[1])} – ${compact(th[2])}`,
    `${compact(th[2])} – ${compact(th[3])}`,
    `${compact(th[3])} 以上`,
  ][index] || '';
}

function paintLegend() {
  const legend = $('legend');
  if (!legend.dataset.built) {
    legend.dataset.built = '1';
    const less = document.createElement('span');
    less.textContent = '少';
    legend.appendChild(less);

    const colors = ['var(--empty)', 'var(--l1)', 'var(--l2)', 'var(--l3)', 'var(--l4)', 'var(--l5)'];
    colors.forEach((color, index) => {
      const swatch = document.createElement('span');
      swatch.className = 'legend-swatch';
      swatch.style.background = color;
      swatch.addEventListener('mouseenter', () => { hoveredBand = index; showBandHint(); });
      swatch.addEventListener('mouseleave', () => {
        if (hoveredBand === index) { hoveredBand = null; showBandHint(); }
      });
      legend.appendChild(swatch);
    });

    const more = document.createElement('span');
    more.textContent = '多';
    legend.appendChild(more);

    const hint = document.createElement('span');
    hint.id = 'legendHint';
    // 紧跟在色块后面,不要用 auto 推到最右边 —— 那样离悬停点太远
    hint.style.marginLeft = '14px';
    legend.appendChild(hint);
  }

  legendBands = Array.from({ length: 6 }, (_, index) => legendBandText(index));
  showBandHint();
}

function showBandHint() {
  const hint = $('legendHint');
  if (!hint) return;
  hint.textContent = hoveredBand === null ? '' : (legendBands[hoveredBand] || '');
}

/* ------------------------------------------------------------- 悬浮提示 */

function attachTooltip(cell) {
  const show = () => {
    const stats = dayStats(cell.dataset.day);
    const tip = $('tip');
    const date = parseYmd(cell.dataset.day);
    const rows = [['输入', stats.parts[0]], ['输出', stats.parts[1]], ['缓存读', stats.parts[2]], ['缓存写', stats.parts[3]]]
      .filter(([, value]) => value > 0)
      .map(([label, value]) => `<div class="tip-row"><span>${label}</span><b>${fmt(value)}</b></div>`)
      .join('');

    tip.innerHTML = `<div class="tip-head">${date.getFullYear()} 年 ${date.getMonth() + 1} 月 ${date.getDate()} 日`
      + `　${fmt(stats.total)}</div>${rows}`
      + `<div class="tip-row"><span>调用</span><b>${fmt(stats.calls)} 次</b></div>`;
    tip.hidden = false;

    const box = tip.getBoundingClientRect();
    const anchor = cell.getBoundingClientRect();
    let left = anchor.left + anchor.width / 2 - box.width / 2;
    let top = anchor.top - box.height - 9;
    left = Math.max(10, Math.min(left, window.innerWidth - box.width - 10));
    if (top < 10) top = anchor.bottom + 9;
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
  };
  const hide = () => { $('tip').hidden = true; };

  cell.addEventListener('mouseenter', show);
  cell.addEventListener('focus', show);
  cell.addEventListener('mouseleave', hide);
  cell.addEventListener('blur', hide);
}

/* -------------------------------------------------------------- 右侧栏 */

function updateBrand() {
  $('brandLogo').src = LOGOS[state.source] || LOGOS.all;
  const label = state.source === 'all'
    ? '全部来源'
    : ((state.data.sources.find((s) => s.id === state.source) || {}).label || '');
  $('brandName').textContent = label;
}

function paint() {
  if (!state.data) return;
  if (state.builtKey !== 'fixed') buildHeatmap();

  const data = scoped();

  paintHeatmap();
  paintLegend();
  paintSourcePicker();
  updateBrand();
  paintHero(data);
  paintRows(data);
  paintNotes(data);
  renderTable();

  // 缓存明细放到 title 里 —— 页脚横向空间紧张,写出来会换行
  const stamp = $('stamp');
  stamp.textContent = stampText();
  const scan = state.data.scan;
  stamp.title = scan
    ? [
        `${scan.files} 个会话文件在磁盘上`,
        `${scan.reused} 个用存档`,
        `新解析 ${scan.parsed} 个`,
        scan.archived ? `${scan.archived} 个已从磁盘消失但仍计入` : null,
        `耗时 ${scan.seconds}s`,
      ].filter(Boolean).join(',')
    : '';
}

function stampText() {
  const raw = String(state.data.generated_at || '').replace('T', ' ');
  return raw ? `扫描于 ${raw.slice(11, 16)}` : '';
}

function paintSourcePicker() {
  for (const button of $('sourcePicker').children) {
    const key = button.dataset.key;
    if (key === 'all') continue;
    const meta = button.querySelector('.picker-meta');
    if (!meta) continue;
    const source = state.data.sources.find((s) => s.id === key);
    if (source) meta.textContent = source.calls === 0 ? '未探测到' : compact(sumOf(state.data.totals[key] || [0, 0, 0, 0]));
  }
  syncPressed($('sourcePicker'), state.source);
}

function paintHero(data) {
  const hero = $('heroValue');
  const wrapper = hero.parentElement;
  if (!data.grand) {
    wrapper.hidden = true;
    $('groupPicker').hidden = true;
    document.querySelector('.rows-viewport').hidden = true;
    return;
  }
  wrapper.hidden = false;
  $('groupPicker').hidden = false;
  document.querySelector('.rows-viewport').hidden = false;

  // 补偿位移施加在 .hero 上(数字和 "tokens" 的共同祖先),
  // 这样两个一起滑;只动其中一个的话它们会各走各的。
  setNumber(hero, fmt(data.grand), wrapper);
  hero.style.fontSize = fmt(data.grand).length > 12 ? '30px' : '';
  $('brandSub').textContent = `${fmt(data.calls)} 次调用`;
}

/** 行集合只在「分组 / 来源 / 模型种类」变化时才重建。 */
function rowsSignature(data) {
  if (state.group === 'models') {
    return `models|${state.source}|${modelOrder().join(',')}`;
  }
  return `${state.group}|${state.source}|${data.ids.join(',')}`;
}

/** 明细里的数字是纯文本,不做滚动动画 —— 只有合计那个大数字滚。 */
function makeValueCell() {
  const wrap = document.createElement('span');
  wrap.className = 'row-value';
  const value = document.createElement('span');
  value.className = 'value-text';
  const share = document.createElement('span');
  share.className = 'row-share';
  wrap.append(value, share);
  return { wrap, value, share };
}

function buildRows(data) {
  const container = $('rows');
  container.textContent = '';
  const entries = [];

  const addRow = (label) => {
    const row = document.createElement('div');
    row.className = 'row';
    const name = document.createElement('span');
    name.className = 'row-label';
    name.textContent = label;
    name.title = label;
    const cell = makeValueCell();
    row.append(name, cell.wrap);
    container.appendChild(row);
    entries.push({ row, label: name, ...cell });
    return entries[entries.length - 1];
  };

  if (state.group === 'sources') {
    for (const id of data.ids) {
      const source = state.data.sources.find((s) => s.id === id);
      addRow(source ? source.label : id);
    }
  } else if (state.group === 'models') {
    for (const model of modelOrder()) addRow(model);
  } else if (state.group === 'parts') {
    for (const label of ['输入', '输出', '缓存读', '缓存写']) addRow(label);
  } else {
    addRow('连续活跃天数');
    addRow('调用次数');
    addRow('活跃天数');
    addRow('数据区间');
  }

  container._entries = entries;
}

function fillRows(data) {
  const entries = $('rows')._entries || [];
  const grand = data.grand || 1;
  const put = (entry, value) => {
    if (!entry) return;
    entry.value.textContent = fmt(value);
    entry.share.textContent = `${((value / grand) * 100).toFixed(1)}%`;
  };

  if (state.group === 'sources') {
    entries.forEach((entry, index) => {
      put(entry, sumOf(data.bySource.get(data.ids[index]) || [0, 0, 0, 0, 0]));
    });
  } else if (state.group === 'models') {
    const order = modelOrder();
    entries.forEach((entry, index) => {
      const value = sumOf(data.byModel.get(order[index]) || [0, 0, 0, 0, 0]);
      entry.value.textContent = value ? fmt(value) : '0';
      entry.share.textContent = value ? `${((value / grand) * 100).toFixed(1)}%` : '';
      entry.row.classList.toggle('is-dim', value === 0);
    });
  } else if (state.group === 'parts') {
    entries.forEach((entry, index) => put(entry, data.parts[index]));
  } else {
    const values = [data.streak, data.calls, data.activeDays];
    entries.slice(0, 3).forEach((entry, index) => {
      if (!entry) return;
      entry.value.textContent = fmt(values[index]);
      entry.share.textContent = '';
    });
    if (entries[3]) {
      entries[3].value.textContent = `${state.range.from} → ${state.range.to}`;
      entries[3].share.textContent = '';
    }
  }
}

/* 分组切换:整块横向推走(外层 .rows-viewport 负责裁切)。
   方向跟着选项的左右位置走 —— 点到左边的选项就往右滑,点到右边的就往左滑,
   这样手感才和选择器对得上,而不是永远朝一个方向。

   用一个令牌作废上一轮动画 —— 否则数据来源变化和切换动画撞在一起时,
   旧动画的回调会把新内容覆盖掉,或者让整块停在场外。
   动画期间来的新数据先存起来,等真正重建时用最新的那份。 */
let rowsToken = 0;
let rowsPending = null;
let lastGroupKey = null;
let lastSlideDir = 1; // +1 表示内容向左走(点了右边的选项)

/** @returns +1 内容向左走; -1 内容向右走 */
function slideDirection(groupKey) {
  if (groupKey === lastGroupKey) return lastSlideDir; // 不是切分组引起的,沿用上次方向
  const order = GROUP_OPTIONS.map((option) => option.key);
  const from = order.indexOf(lastGroupKey);
  const to = order.indexOf(groupKey);
  if (from < 0 || to < 0 || to === from) return lastSlideDir;
  return to > from ? 1 : -1; // 往右边的选项 -> 内容向左走
}

function paintRows(data) {
  const signature = rowsSignature(data);
  const firstBuild = state.rowSignature === null;
  const groupChanged = !firstBuild && state.group !== lastGroupKey;

  // 首次渲染直接建,不要滑 —— 一打开就看到内容滑一下很怪
  if (firstBuild) {
    state.rowSignature = signature;
    lastGroupKey = state.group;
    buildRows(data);
    fillRows(data);
    return;
  }

  // 只有切分组才滑动;换数据来源、模型种类变了都直接重建。
  // 上一轮滑动没结束就再切分组的话,重新起一轮,令牌会让旧的作废。
  if (groupChanged) {
    state.rowSignature = signature;
    lastSlideDir = slideDirection(state.group);
    lastGroupKey = state.group;
    rowsPending = data;
    slideRows(lastSlideDir);
    return;
  }

  if (rowsPending) {
    // 滑动进行中(不是切分组引起的):把最新数据交给它,等它统一重建
    rowsPending = data;
    state.rowSignature = signature;
    return;
  }

  if (signature !== state.rowSignature) {
    state.rowSignature = signature;
    buildRows(data);
    fillRows(data);
    return;
  }

  fillRows(data);
}

function slideRows(dir) {
  const token = (rowsToken += 1);
  const container = $('rows');
  const exitX = dir * -100; // dir=+1 时是 -100%(往左滑出)
  const enterX = dir * 100;

  // 第一段:按方向滑出去
  container.style.transition = `transform ${ROWS_SLIDE_MS}ms cubic-bezier(.45, 0, .75, .6)`;
  container.style.transform = `translateX(${exitX}%)`;

  setTimeout(() => {
    if (token !== rowsToken) return; // 已被更新的动作接管,交给它

    const data = rowsPending;
    rowsPending = null;
    if (data) {
      buildRows(data);
      fillRows(data);
    }

    // 第二段:先无过渡地挪到对侧场外,强制重排后再过渡回来
    container.style.transition = 'none';
    container.style.transform = `translateX(${enterX}%)`;
    void container.offsetWidth;
    requestAnimationFrame(() => {
      if (token !== rowsToken) return;
      container.style.transition = `transform ${ROWS_SLIDE_MS}ms cubic-bezier(.2, .85, .3, 1)`;
      container.style.transform = 'translateX(0)';
    });
  }, ROWS_SLIDE_MS);
}

/** 每一条提示独占一行;所有没探测到的来源都要逐个声明。 */
function paintNotes(data) {
  const container = $('railNotes');
  const notes = [];
  if (state.fatal) notes.push(state.fatal);

  for (const source of state.data.sources) {
    if (source.calls === 0) notes.push(`${source.label} 在本机没有探测到数据。`);
  }

  if (state.data.scan && state.data.scan.db_error) {
    notes.push(`数据库不可用,每次都全量扫描:${state.data.scan.db_error}`);
  }
  if (state.data.scan && state.data.scan.db_backup) {
    const name = state.data.scan.db_backup.split(/[\\/]/).pop();
    const moved = state.data.scan.db_migrated || 0;
    notes.push(`存档格式已升级,旧库另存为 ${name}` + (moved ? `,其中 ${moved} 条记录已搬入新库` : ''));
  }
  for (const warning of state.data.warnings || []) notes.push(warning);

  const signature = notes.join('\u0000');
  if (container.dataset.signature === signature) return;
  container.dataset.signature = signature;
  container.textContent = '';
  for (const note of notes) {
    const line = document.createElement('p');
    line.textContent = note;
    container.appendChild(line);
  }
}

/* -------------------------------------------------------------- 表格视图 */

function renderTable() {
  const container = $('tableView');
  container.hidden = !state.showTable;
  if (!state.showTable) return;

  const ordered = [];
  for (let cursor = parseYmd(state.range.to); cursor >= parseYmd(state.range.from); cursor = addDays(cursor, -1)) {
    const key = ymd(cursor);
    if (dayStats(key).total > 0) ordered.push(key);
  }

  if (!ordered.length) {
    container.innerHTML = '<p class="empty-note">这个范围里没有用量记录。</p>';
    return;
  }

  const body = ordered.map((day) => {
    const stats = dayStats(day);
    return `<tr>
      <td>${day}</td>
      <td>${fmt(stats.parts[0])}</td>
      <td>${fmt(stats.parts[1])}</td>
      <td>${fmt(stats.parts[2])}</td>
      <td>${fmt(stats.parts[3])}</td>
      <td>${fmt(stats.total)}</td>
      <td>${fmt(stats.calls)}</td>
    </tr>`;
  }).join('');

  container.innerHTML = `<table>
    <thead><tr>
      <th>日期</th><th>输入</th><th>输出</th><th>缓存读</th><th>缓存写</th><th>合计</th><th>调用</th>
    </tr></thead>
    <tbody>${body}</tbody>
  </table>`;
}

/* ------------------------------------------------------------------ 轮询 */

function schedulePoll() {
  clearInterval(pollTimer);
  pollTimer = 0;
  if (state.pollMs > 0) pollTimer = setInterval(tickPoll, state.pollMs);
}

function tickPoll() {
  if (document.hidden || scanInFlight) return;
  scan();
}

function scan(manual) {
  if (scanInFlight) return;
  scanInFlight = true;
  if (manual) $('stamp').textContent = '正在扫描…';

  const run = bridge()
    ? bridge().rescan()
    : fetch('data.json').then((response) => response.json());

  return run
    .then(acceptData)
    .catch(() => { if (manual) $('stamp').textContent = '扫描失败'; })
    .finally(() => { scanInFlight = false; });
}

/* ------------------------------------------------------------------ 总控 */

function wireChrome() {
  $('tableToggle').addEventListener('click', () => {
    state.showTable = !state.showTable;
    $('tableToggle').setAttribute('aria-pressed', String(state.showTable));
    renderTable();
  });

  $('rescan').addEventListener('click', () => scan(true));

  const onDate = () => {
    const from = $('dateFrom').value;
    const to = $('dateTo').value;
    if (!from || !to || from > to) return;
    state.range = { from, to };
    state.quick = null; // 手动改日期就不再属于任何快捷区间
    syncPressed($('quick'), null);
    paint();
  };
  $('dateFrom').addEventListener('change', onDate);
  $('dateTo').addEventListener('change', onDate);

  let resizeTimer = 0;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { buildHeatmap(); paint(); }, 90);
  });

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) tickPoll();
  });
}

function start() {
  buildQuick();
  buildGroupPicker();
  buildPoll();
  wireChrome();
  schedulePoll();

  window.addEventListener('pywebviewready', boot);
  if (bridge()) boot();
  // 浏览器调试兜底:等足够久确认 pywebview 不会来,再退回 data.json,
  // 否则打包后的程序会去 fetch 一个不存在的文件,把界面带进空状态。
  else setTimeout(() => { if (!bridge()) boot(); }, 900);
}

document.addEventListener('DOMContentLoaded', start);
