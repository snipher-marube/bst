/**
 * dashboard.js — AnalyticsMeta dashboard grid + widget rendering
 *
 * Responsibilities:
 *  - GridStack initialisation and drag-drop layout
 *  - Widget HTML scaffolding with skeleton loaders
 *  - Plotly chart rendering for all widget types
 *  - Error boundary so one broken widget never crashes the page
 *  - WebSocket event integration (widget-update, layout-update, etc.)
 *  - Add-widget modal with CSRF-safe POST
 *  - Paginated data-table inside table widgets
 */

'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let currentWorkspace = null;
let currentDashboard = null;
let grid = null;

// Map widgetId → { type, vizConfig, barOrder } so re-renders know how to draw
const widgetRegistry = new Map();

// CSRF token (injected by Django into the page meta tag)
function getCsrf() {
  return document.querySelector('meta[name="csrftoken"]')?.content ||
         document.cookie.split('; ')
           .find(r => r.startsWith('csrftoken='))
           ?.split('=')[1] || '';
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  initGrid();
  loadDashboardFromPage();
  setupEventListeners();
  setupWebSocketListeners();
});

function initGrid() {
  grid = GridStack.init({
    column: 12,
    cellHeight: 92,
    margin: 8,
    float: false,
    animate: true,
    resizable: { handles: 'se' },
    draggable: { handle: '.widget-drag-handle' },
  });

  grid.on('change', debounce(() => {
    autoSaveLayout();
  }, 1500));
}

// ---------------------------------------------------------------------------
// Load dashboard data from server-rendered JSON block
// ---------------------------------------------------------------------------
function loadDashboardFromPage() {
  const el = document.getElementById('dashboard-data-json');
  if (!el) return;

  try {
    const payload = JSON.parse(el.textContent);
    currentDashboard = payload;
    renderWidgets(payload.widgets || []);
  } catch (e) {
    console.error('[Dashboard] Failed to parse dashboard-data-json', e);
  }
}

// ---------------------------------------------------------------------------
// Widget grid rendering
// ---------------------------------------------------------------------------
function renderWidgets(widgets) {
  grid.removeAll();
  let barOrder = 0;
  widgets.forEach(widget => {
    const pos  = widget.position || { x: 0, y: 0, w: 6, h: 5 };
    const el   = createWidgetElement(widget);
    grid.addWidget(el, { x: pos.x, y: pos.y, w: pos.w, h: pos.h, id: widget.id });
    if (widget.widget_type === 'bar_chart') barOrder += 1;
    widgetRegistry.set(widget.id, {
      type:      widget.widget_type,
      vizConfig: widget.viz_config || {},
      barOrder,
    });
    // Render data that was already server-rendered into the payload
    if (widget.widget_data) {
      renderWidgetChart(widget.id, widget.widget_type, widget.widget_data, widget.viz_config || {});
    } else {
      fetchAndRenderWidget(widget.id);
    }
  });
}

function createWidgetElement(widget) {
  const div = document.createElement('div');
  div.className = 'grid-stack-item';
  div.setAttribute('gs-id', widget.id);
  div.innerHTML = `
    <div class="grid-stack-item-content rounded-xl shadow-sm border flex flex-col overflow-hidden" style="background:#ffffff;border-color:#e5e7eb;box-shadow:0 1px 3px rgba(0,0,0,.07);">
      <!-- Header -->
      <div class="widget-drag-handle flex items-center justify-between px-4 py-2 border-b cursor-move select-none" style="background:#f9fafb;border-color:#e5e7eb;">
        <div class="flex items-center gap-2 min-w-0">
          <i class="fas fa-grip-vertical text-xs" style="color:#d1d5db"></i>
          <span class="text-sm font-semibold truncate" style="color:#374151">${escHtml(widget.title)}</span>
        </div>
        <div class="flex items-center gap-3 flex-shrink-0">
          <div id="widget-time-filter-${widget.id}" class="hidden items-center gap-2" style="min-width:220px;">
            <span id="widget-time-filter-label-${widget.id}" class="text-[11px] whitespace-nowrap" style="color:#6b7280"></span>
            <div class="relative w-28 h-5">
              <input id="widget-time-filter-start-${widget.id}" type="range" min="0" max="0" value="0" class="absolute inset-0 w-full accent-blue-500 cursor-pointer" />
              <input id="widget-time-filter-end-${widget.id}" type="range" min="0" max="0" value="0" class="absolute inset-0 w-full accent-cyan-500 cursor-pointer" />
            </div>
          </div>
          <button class="widget-refresh-btn p-1 transition-colors rounded"
                  style="color:#9ca3af"
                  data-id="${widget.id}" title="Refresh">
            <i class="fas fa-sync-alt text-xs"></i>
          </button>
          <button class="widget-edit-btn p-1 transition-colors rounded"
                  style="color:#9ca3af"
                  data-id="${widget.id}" title="Edit">
            <i class="fas fa-edit text-xs"></i>
          </button>
          <button class="widget-delete-btn p-1 transition-colors rounded"
                  style="color:#9ca3af"
                  data-id="${widget.id}" title="Delete">
            <i class="fas fa-trash text-xs"></i>
          </button>
        </div>
      </div>
      <!-- Body -->
      <div class="widget-body flex-1 px-4 pb-4 pt-0 min-h-0" id="widget-body-${widget.id}">
        ${skeletonHtml(widget.widget_type)}
      </div>
    </div>
  `;
  return div;
}

function skeletonHtml(type) {
  if (type === 'metric') {
    return `<div class="skeleton-loader flex flex-col items-center justify-center h-full gap-2">
      <div class="skeleton-line w-16 h-8 rounded"></div>
      <div class="skeleton-line w-24 h-3 rounded"></div>
    </div>`;
  }
  return `<div class="skeleton-loader flex flex-col gap-2 p-2 h-full">
    <div class="skeleton-line w-3/4 h-3 rounded"></div>
    <div class="flex-1 skeleton-line rounded"></div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Fetch widget data and render
// ---------------------------------------------------------------------------
function fetchAndRenderWidget(widgetId) {
  const info = widgetRegistry.get(widgetId);
  fetch(`/api/v1/widgets/${widgetId}/data/`, {
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
    credentials: 'same-origin',
  })
    .then(r => r.ok ? r.json() : r.json().then(e => Promise.reject(e)))
    .then(data => {
      if (info) renderWidgetChart(widgetId, info.type, data, info.vizConfig);
    })
    .catch(err => renderWidgetError(widgetId, err?.error || err?.message || 'Failed to load data'));
}

// ---------------------------------------------------------------------------
// Chart rendering — error-boundary wrapper
// ---------------------------------------------------------------------------
function renderWidgetChart(widgetId, type, data, vizConfig) {
  const container = document.getElementById(`widget-body-${widgetId}`);
  if (!container) return;
  const info = widgetRegistry.get(widgetId) || {};

  // Clear any skeleton
  container.innerHTML = '';

  if (!data || data.error) {
    renderWidgetError(widgetId, data?.error || 'No data returned');
    return;
  }
  if (data.message) {
    renderWidgetEmpty(widgetId, data.message);
    return;
  }

  try {
    switch (type) {
      case 'metric':     renderMetric(container, data, vizConfig);    break;
      case 'line_chart': renderLineChart(container, data, vizConfig);  break;
      case 'area_chart': renderAreaChart(container, data, vizConfig);  break;
      case 'bar_chart':  renderBarChart(container, data, vizConfig, info);   break;
      case 'pie_chart':  renderPieChart(container, data, vizConfig);   break;
      case 'table':      renderDataTable(container, data, vizConfig);  break;
      default:
        renderWidgetEmpty(widgetId, `Unsupported widget type: ${type}`);
    }
  } catch (err) {
    console.error(`[Widget ${widgetId}] Render error:`, err);
    renderWidgetError(widgetId, 'Render failed — see console for details');
  }
}

function renderWidgetError(widgetId, message) {
  const container = document.getElementById(`widget-body-${widgetId}`);
  if (!container) return;
  container.innerHTML = `
    <div class="flex flex-col items-center justify-center h-full text-center gap-2 py-4">
      <i class="fas fa-exclamation-triangle text-2xl text-red-400"></i>
      <p class="text-sm text-red-500 font-medium">Error</p>
      <p class="text-xs text-gray-400 max-w-xs">${escHtml(String(message))}</p>
      <button onclick="fetchAndRenderWidget('${widgetId}')"
              class="mt-1 text-xs text-[#03466e] underline hover:no-underline">Retry</button>
    </div>`;
}

function renderWidgetEmpty(widgetId, message) {
  const container = document.getElementById(`widget-body-${widgetId}`);
  if (!container) return;
  container.innerHTML = `
    <div class="flex flex-col items-center justify-center h-full text-center gap-2 py-4">
      <i class="fas fa-inbox text-2xl text-gray-300"></i>
      <p class="text-sm text-gray-400">${escHtml(String(message))}</p>
    </div>`;
}

// ---------------------------------------------------------------------------
// Metric / KPI card
// ---------------------------------------------------------------------------
function renderMetric(container, data, viz) {
  const raw    = data.val ?? data.value ?? Object.values(data)[0] ?? 0;
  const prefix = viz.prefix || '';
  const suffix = viz.suffix || '';
  const icon   = viz.icon   || 'fa-chart-line';
  const color  = viz.color  || 'blue';

  const colorMap = {
    blue:   { bg: 'bg-blue-50',   text: 'text-blue-600',   icon: 'text-blue-400'   },
    green:  { bg: 'bg-green-50',  text: 'text-green-600',  icon: 'text-green-400'  },
    orange: { bg: 'bg-orange-50', text: 'text-orange-600', icon: 'text-orange-400' },
    purple: { bg: 'bg-purple-50', text: 'text-purple-600', icon: 'text-purple-400' },
    red:    { bg: 'bg-red-50',    text: 'text-red-600',    icon: 'text-red-400'    },
  };
  const c = colorMap[color] || colorMap.blue;

  const formatted = typeof raw === 'number'
    ? (Number.isInteger(raw) ? raw.toLocaleString() : raw.toLocaleString(undefined, { maximumFractionDigits: 2 }))
    : raw;

  container.innerHTML = `
    <div class="flex items-center justify-between h-full px-2">
      <div>
        <p class="text-3xl font-bold ${c.text} leading-none">
          ${escHtml(prefix)}${escHtml(String(formatted))}${escHtml(suffix)}
        </p>
      </div>
      <div class="w-12 h-12 rounded-xl ${c.bg} flex items-center justify-center flex-shrink-0">
        <i class="fas ${escHtml(icon)} text-xl ${c.icon}"></i>
      </div>
    </div>`;
}

// ---------------------------------------------------------------------------
// Line chart
// ---------------------------------------------------------------------------
function renderLineChart(container, data, viz) {
  const plotDiv = createPlotDiv(container);
  const valKey  = viz.y_axis || 'val';
  const grouped = extractGroupedData(data, valKey);

  if (!grouped.labels.length) { renderWidgetEmptyInContainer(container, 'No data to chart'); return; }
  const isDateAxis = labelsAreDates(grouped.labels);
  const xValues = isDateAxis ? grouped.labels.map(toDateOnly) : grouped.labels;

  Plotly.newPlot(plotDiv, [{
    x:    xValues,
    y:    grouped.values,
    type: 'scatter',
    mode: 'lines+markers',
    line: { color: '#4f8fdd', width: 1.9, shape: 'spline', smoothing: 0.8 },
    marker: { color: '#4f8fdd', size: 5, line: { width: 0 } },
    hovertemplate: isDateAxis
      ? '<b>%{x|%d %b %Y}</b><br>%{y:,.2f}<extra></extra>'
      : '<b>%{x}</b><br>%{y:,.2f}<extra></extra>',
    fill: 'tozeroy',
    fillcolor: 'rgba(79,143,221,0.12)',
  }], {
    ...plotlyLayout(container),
    xaxis: isDateAxis
      ? timeAxisLayout(grouped.labels, plotlyLayout(container).xaxis)
      : plotlyLayout(container).xaxis,
  }, plotlyConfig());
  setupHeaderTimeFilter(container, plotDiv, xValues, isDateAxis);
}

function renderAreaChart(container, data, viz) {
  const plotDiv = createPlotDiv(container);
  const valKey  = viz.y_axis || 'val';
  const grouped = extractGroupedData(data, valKey);

  if (!grouped.labels.length) { renderWidgetEmptyInContainer(container, 'No data to chart'); return; }
  const isDateAxis = labelsAreDates(grouped.labels);
  const xValues = isDateAxis ? grouped.labels.map(toDateOnly) : grouped.labels;

  Plotly.newPlot(plotDiv, [{
    x:    xValues,
    y:    grouped.values,
    type: 'scatter',
    mode: 'lines+markers',
    line: { color: '#19d3b5', width: 2.6, shape: 'spline', smoothing: 0.8 },
    marker: { color: '#f2a900', size: 5, line: { width: 0 } },
    hovertemplate: isDateAxis
      ? '<b>%{x|%d %b %Y}</b><br>%{y:,.2f}<extra></extra>'
      : '<b>%{x}</b><br>%{y:,.2f}<extra></extra>',
    fill: 'tozeroy',
    fillcolor: 'rgba(25,211,181,0.20)',
  }], {
    ...plotlyLayout(container),
    xaxis: isDateAxis
      ? timeAxisLayout(grouped.labels, plotlyLayout(container).xaxis)
      : plotlyLayout(container).xaxis,
  }, plotlyConfig());
  setupHeaderTimeFilter(container, plotDiv, xValues, isDateAxis);
}

// ---------------------------------------------------------------------------
// Bar chart
// ---------------------------------------------------------------------------
function renderBarChart(container, data, viz, meta = {}) {
  const plotDiv = createPlotDiv(container);
  const valKey  = viz.y_axis || 'val';
  const grouped = extractGroupedData(data, valKey);

  if (!grouped.labels.length) { renderWidgetEmptyInContainer(container, 'No data to chart'); return; }

  const horizontal = grouped.labels.length > 5;
  const pairs = grouped.labels.map((label, index) => ({
    label,
    value: Number(grouped.values[index] ?? 0),
  }));
  const sortAscending = Number(meta.barOrder || 1) % 2 === 0;
  const orderedPairs = [...pairs].sort((a, b) => sortAscending ? a.value - b.value : b.value - a.value);
  const labels = orderedPairs.map(({ label }) => label);
  const values = orderedPairs.map(({ value }) => value);

  const barWidth = horizontal ? 0.46 : 0.42;
  const barColors = colorsWithSmallestYellow(values, horizontal);

  Plotly.newPlot(plotDiv, [{
    x:    horizontal ? values : labels,
    y:    horizontal ? labels : values,
    type: 'bar',
    orientation: horizontal ? 'h' : 'v',
    width: barWidth,
    marker: {
      color: barColors,
      line: { color: 'rgba(255,255,255,0.36)', width: 0.8 },
    },
    hovertemplate: horizontal
      ? '<b>%{y}</b><br>%{x:,.2f}<extra></extra>'
      : '<b>%{x}</b><br>%{y:,.2f}<extra></extra>',
  }], {
    ...plotlyLayout(container),
    bargap: horizontal ? 0.62 : 0.54,
    margin: horizontal
      ? { t: 14, r: 18, b: 42, l: 118 }
      : plotlyLayout(container).margin,
    xaxis: horizontal
      ? plotlyLayout(container).xaxis
      : { ...plotlyLayout(container).xaxis, tickangle: -45, automargin: true },
    yaxis: horizontal
      ? { ...plotlyLayout(container).yaxis, autorange: 'reversed' }
      : plotlyLayout(container).yaxis,
  }, plotlyConfig());
}

// ---------------------------------------------------------------------------
// Pie chart
// ---------------------------------------------------------------------------
function renderPieChart(container, data, viz) {
  const plotDiv = createPlotDiv(container);
  const valKey  = viz.y_axis || 'val';
  const grouped = extractGroupedData(data, valKey);

  if (!grouped.labels.length) { renderWidgetEmptyInContainer(container, 'No data to chart'); return; }

  const slices = grouped.labels.map((label, index) => ({
    label,
    value: Number(grouped.values[index] ?? 0),
  })).filter(({ label, value }) => label !== null && label !== undefined && label !== '' && Number.isFinite(value) && value > 0);

  if (!slices.length) { renderWidgetEmptyInContainer(container, 'No chartable categories'); return; }

  const palette = ['#1f77b4','#f2a900','#10a37f','#3e8ec6','#f6b93b','#2fb58f','#165a8a','#0d7a60'];

  Plotly.newPlot(plotDiv, [{
    labels: slices.map(({ label }) => label),
    values: slices.map(({ value }) => value),
    type:   'pie',
    hole:   0.38,
    marker: { colors: palette, line: { color: '#ffffff', width: 2 } },
    textinfo: 'percent',
    hovertemplate: '<b>%{label}</b><br>%{value:,.2f} (%{percent})<extra></extra>',
  }], {
    ...plotlyLayout(container),
    showlegend: true,
    legend: { orientation: 'h', y: -0.15, font: { size: 10 } },
  }, plotlyConfig());
}

// ---------------------------------------------------------------------------
// Data table with client-side pagination
// ---------------------------------------------------------------------------
function renderDataTable(container, data, viz) {
  const pageSize = viz.page_size || 20;
  const rows     = Array.isArray(data) ? data : (data.rows || data.results || []);

  if (!rows.length) { renderWidgetEmptyInContainer(container, 'No records'); return; }

  const columns = Object.keys(rows[0]).filter(k => !k.startsWith('_'));

  let currentPage = 1;
  const totalPages = () => Math.ceil(rows.length / pageSize);
  const pageRows   = () => rows.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  function draw() {
    const isFirst = currentPage === 1;
    const isLast  = currentPage >= totalPages();
    container.innerHTML = `
      <div class="overflow-auto h-full flex flex-col">
        <table class="data-table w-full text-xs">
          <thead>
            <tr>
              ${columns.map(c => `<th class="data-table-th">${escHtml(c)}</th>`).join('')}
            </tr>
          </thead>
          <tbody>
            ${pageRows().map(row => `
              <tr class="data-table-row">
                ${columns.map(c => `<td class="data-table-td">${escHtml(String(row[c] ?? ''))}</td>`).join('')}
              </tr>`).join('')}
          </tbody>
        </table>
        ${totalPages() > 1 ? `
        <div class="flex items-center justify-between px-2 pt-2 mt-auto border-t border-gray-100 flex-shrink-0">
          <span class="text-xs text-gray-400">
            ${(currentPage - 1) * pageSize + 1}–${Math.min(currentPage * pageSize, rows.length)} of ${rows.length}
          </span>
          <div class="flex gap-1">
            <button class="tbl-prev px-2 py-1 text-xs rounded border ${isFirst ? 'text-gray-300 border-gray-200 cursor-default' : 'text-[#03466e] border-[#03466e] hover:bg-[#03466e] hover:text-white'}"
                    ${isFirst ? 'disabled' : ''}>‹ Prev</button>
            <button class="tbl-next px-2 py-1 text-xs rounded border ${isLast  ? 'text-gray-300 border-gray-200 cursor-default' : 'text-[#03466e] border-[#03466e] hover:bg-[#03466e] hover:text-white'}"
                    ${isLast ? 'disabled' : ''}>Next ›</button>
          </div>
        </div>` : ''}
      </div>`;

    container.querySelector('.tbl-prev')?.addEventListener('click', () => {
      if (currentPage > 1) { currentPage--; draw(); }
    });
    container.querySelector('.tbl-next')?.addEventListener('click', () => {
      if (currentPage < totalPages()) { currentPage++; draw(); }
    });
  }

  draw();
}

// ---------------------------------------------------------------------------
// Plotly helpers
// ---------------------------------------------------------------------------
function createPlotDiv(container) {
  const div = document.createElement('div');
  div.style.width  = '100%';
  div.style.height = '100%';
  container.appendChild(div);
  return div;
}

function plotlyLayout(container) {
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  return {
    margin:      { t: 14, r: 18, b: 52, l: 56 },
    paper_bgcolor: dark ? '#162131' : '#d1dceb',
    plot_bgcolor:  dark ? '#162131' : '#d1dceb',
    font:        { family: 'Inter, sans-serif', size: 11, color: dark ? '#c4d2e4' : '#6b7280' },
    xaxis:       { showgrid: false, zeroline: false, tickfont: { size: 10, color: dark ? '#c4d2e4' : '#6b7280' } },
    yaxis:       { gridcolor: dark ? 'rgba(255,255,255,0.10)' : 'rgba(255,255,255,0.88)', zeroline: false, tickfont: { size: 10, color: dark ? '#c4d2e4' : '#6b7280' } },
    hovermode:   'closest',
    hoverlabel:  { bgcolor: dark ? '#0b1220' : '#1f2937', font: { color: '#fff', size: 12 } },
    autosize:    true,
  };
}

function labelsAreDates(labels) {
  const iso = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?$/;
  return labels.length > 0 && labels.every(label => iso.test(String(label)));
}

function toDateOnly(label) {
  const s = String(label);
  return (s.length > 10 && (s[10] === 'T' || s[10] === ' ')) ? s.slice(0, 10) : s;
}

function hexToRgb(hex) {
  const cleaned = hex.replace('#', '');
  const expanded = cleaned.length === 3 ? cleaned.split('').map((ch) => ch + ch).join('') : cleaned;
  const value = parseInt(expanded, 16);
  return { r: (value >> 16) & 255, g: (value >> 8) & 255, b: value & 255 };
}

function rgbToHex({ r, g, b }) {
  return `#${[r, g, b].map((part) => Math.max(0, Math.min(255, Math.round(part))).toString(16).padStart(2, '0')).join('')}`;
}

function mixColor(from, to, t) {
  const a = hexToRgb(from);
  const b = hexToRgb(to);
  return rgbToHex({
    r: a.r + (b.r - a.r) * t,
    g: a.g + (b.g - a.g) * t,
    b: a.b + (b.b - a.b) * t,
  });
}

function colorsWithSmallestYellow(values, horizontal) {
  const nums = values.map((value) => Number(value) || 0);
  const min = Math.min(...nums);
  const colors = horizontal
    ? (() => {
        const splitIndex = Math.max(1, Math.ceil(values.length / 2));
        const topPalette = ['#19d3b5', '#12cbb2', '#0fc2ad', '#0db8a6'];
        const basePalette = ['#4f8fdd', '#4384d2', '#3779c7', '#2c6fbc'];
        return values.map((_, index) => (
          index < splitIndex
            ? topPalette[index % topPalette.length]
            : basePalette[(index - splitIndex) % basePalette.length]
        ));
      })()
    : values.map((_, i) => ['#1f77b4', '#f2a900', '#10a37f', '#3e8ec6', '#f6b93b', '#2fb58f'][i % 6]);
  return colors.map((color, index) => (nums[index] === min ? '#f4b740' : color));
}

function timeAxisLayout(labels, baseAxis = {}) {
  const count = labels.length;
  return {
    ...baseAxis,
    type: 'date',
    tickformat: '%d %b %Y',
    dtick: count <= 14 ? 86400000 : count <= 60 ? 7 * 86400000 : 'M1',
  };
}

function formatShortDate(label) {
  const date = new Date(toDateOnly(label));
  if (Number.isNaN(date.getTime())) return String(label);
  return date.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' });
}

function setupHeaderTimeFilter(container, plotDiv, labels, isDateAxis) {
  const bodyId = container.id || '';
  const widgetId = bodyId.replace('widget-body-', '');
  if (!widgetId) return;

  const filterWrap = document.getElementById(`widget-time-filter-${widgetId}`);
  const filterLabel = document.getElementById(`widget-time-filter-label-${widgetId}`);
  const filterStart = document.getElementById(`widget-time-filter-start-${widgetId}`);
  const filterEnd = document.getElementById(`widget-time-filter-end-${widgetId}`);
  if (!filterWrap || !filterLabel || !filterStart || !filterEnd) return;

  if (!isDateAxis || labels.length < 7) {
    filterWrap.classList.add('hidden');
    filterWrap.classList.remove('flex');
    return;
  }

  const maxIndex = labels.length - 1;
  if (maxIndex < 2) {
    filterWrap.classList.add('hidden');
    filterWrap.classList.remove('flex');
    return;
  }

  const syncRange = (changed) => {
    let start = Math.max(0, Math.min(Number(filterStart.value) || 0, maxIndex));
    let end = Math.max(0, Math.min(Number(filterEnd.value) || 0, maxIndex));
    if (start >= end) {
      if (changed === 'start') start = Math.max(0, end - 1);
      else end = Math.min(maxIndex, start + 1);
    }
    filterStart.value = String(start);
    filterEnd.value = String(end);
    Plotly.relayout(plotDiv, {
      'xaxis.range': [toDateOnly(labels[start]), toDateOnly(labels[end])],
    });
    filterLabel.textContent = `${formatShortDate(labels[start])} - ${formatShortDate(labels[end])}`;
  };

  filterWrap.classList.remove('hidden');
  filterWrap.classList.add('flex');
  [filterStart, filterEnd].forEach((input) => {
    input.min = '0';
    input.max = String(maxIndex);
    input.step = '1';
  });
  filterStart.value = '0';
  filterEnd.value = String(maxIndex);
  filterStart.oninput = () => syncRange('start');
  filterEnd.oninput = () => syncRange('end');
  syncRange();
}

function plotlyConfig() {
  return {
    responsive:  true,
    displaylogo: false,
    modeBarButtonsToRemove: ['lasso2d', 'select2d', 'autoScale2d'],
    toImageButtonOptions: { format: 'png', scale: 2 },
  };
}

function extractGroupedData(data, valKey) {
  // data may be { val: { label: value, ... } } or { field: { ... } }
  let mapping = null;
  if (data[valKey] && typeof data[valKey] === 'object') {
    mapping = data[valKey];
  } else {
    // fall back to first object-valued key
    for (const k of Object.keys(data)) {
      if (data[k] && typeof data[k] === 'object' && !Array.isArray(data[k])) {
        mapping = data[k]; break;
      }
    }
  }
  if (!mapping) return { labels: [], values: [] };

  const entries = Object.entries(mapping)
    .filter(([, v]) => v !== null && !isNaN(Number(v)))
    .sort((a, b) => String(a[0]).localeCompare(String(b[0])));

  return {
    labels: entries.map(([k]) => k),
    values: entries.map(([, v]) => Number(v)),
  };
}

function renderWidgetEmptyInContainer(container, msg) {
  container.innerHTML = `
    <div class="flex flex-col items-center justify-center h-full text-center gap-2">
      <i class="fas fa-inbox text-2xl text-gray-300"></i>
      <p class="text-xs text-gray-400">${escHtml(msg)}</p>
    </div>`;
}

// ---------------------------------------------------------------------------
// Event listeners
// ---------------------------------------------------------------------------
function setupEventListeners() {
  // Add widget button
  document.getElementById('add-widget-btn')?.addEventListener('click', () => {
    openWidgetModal();
  });

  // Modal close
  document.getElementById('widget-modal-close')?.addEventListener('click', closeWidgetModal);
  document.getElementById('widget-modal-overlay')?.addEventListener('click', closeWidgetModal);

  // Save widget from modal
  document.getElementById('widget-modal-save')?.addEventListener('click', submitNewWidget);

  // Refresh / edit / delete — delegated
  document.addEventListener('click', e => {
    const refreshBtn = e.target.closest('.widget-refresh-btn');
    if (refreshBtn) {
      const id = refreshBtn.dataset.id;
      refreshWidget(id);
    }
    const deleteBtn = e.target.closest('.widget-delete-btn');
    if (deleteBtn) {
      const id = deleteBtn.dataset.id;
      if (confirm('Delete this widget?')) deleteWidget(id);
    }
  });

  // Keyboard: Escape closes modal
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeWidgetModal();
  });
}

// ---------------------------------------------------------------------------
// WebSocket event integration
// ---------------------------------------------------------------------------
function setupWebSocketListeners() {
  document.addEventListener('widget-update', e => {
    const { widgetId, data } = e.detail;
    const info = widgetRegistry.get(widgetId);
    if (info) renderWidgetChart(widgetId, info.type, data, info.vizConfig);
  });

  document.addEventListener('widget-add', e => {
    const widget = e.detail.widget;
    if (!widget) return;
    const el = createWidgetElement(widget);
    const pos = widget.position || { x: 0, y: 0, w: 6, h: 4 };
    grid.addWidget(el, { x: pos.x, y: pos.y, w: pos.w, h: pos.h, id: widget.id });
    widgetRegistry.set(widget.id, { type: widget.widget_type, vizConfig: widget.viz_config || {} });
    if (widget.widget_data) {
      renderWidgetChart(widget.id, widget.widget_type, widget.widget_data, widget.viz_config || {});
    } else {
      fetchAndRenderWidget(widget.id);
    }
  });

  document.addEventListener('widget-remove', e => {
    const { widgetId } = e.detail;
    const el = document.querySelector(`[gs-id="${widgetId}"]`);
    if (el) grid.removeWidget(el);
    widgetRegistry.delete(widgetId);
  });

  document.addEventListener('layout-update', e => {
    const { widgets } = e.detail;
    if (!Array.isArray(widgets)) return;
    widgets.forEach(w => {
      const el = document.querySelector(`[gs-id="${w.id}"]`);
      if (el && w.position) {
        grid.update(el, { x: w.position.x, y: w.position.y, w: w.position.w, h: w.position.h });
      }
    });
  });
}

// ---------------------------------------------------------------------------
// Widget actions
// ---------------------------------------------------------------------------
function refreshWidget(widgetId) {
  const body = document.getElementById(`widget-body-${widgetId}`);
  if (body) body.innerHTML = skeletonHtml(widgetRegistry.get(widgetId)?.type || 'metric');

  // Prefer WebSocket refresh; fall back to HTTP
  if (window._rtDashboard?.isConnected()) {
    window._rtDashboard.refreshWidget(widgetId);
  } else {
    fetchAndRenderWidget(widgetId);
  }
}

function deleteWidget(widgetId) {
  if (window._rtDashboard?.isConnected()) {
    window._rtDashboard.deleteWidget(widgetId);
  } else {
    fetch(`/api/v1/widgets/${widgetId}/`, {
      method: 'DELETE',
      headers: { 'X-CSRFToken': getCsrf() },
      credentials: 'same-origin',
    }).then(r => {
      if (r.ok) {
        const el = document.querySelector(`[gs-id="${widgetId}"]`);
        if (el) grid.removeWidget(el);
        widgetRegistry.delete(widgetId);
      }
    });
  }
}

// ---------------------------------------------------------------------------
// Auto-save layout via WebSocket or HTTP
// ---------------------------------------------------------------------------
function autoSaveLayout() {
  const widgets = [];
  grid.engine.nodes.forEach(node => {
    if (!node.id) return;
    widgets.push({ id: node.id, position: { x: node.x, y: node.y, w: node.w, h: node.h } });
  });

  if (!widgets.length || !currentDashboard) return;

  if (window._rtDashboard?.isConnected()) {
    window._rtDashboard.updateLayout({}, widgets);
  } else {
    fetch(`/api/v1/dashboards/${currentDashboard.id}/`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrf() },
      credentials: 'same-origin',
      body: JSON.stringify({ widgets }),
    }).catch(console.error);
  }
}

// ---------------------------------------------------------------------------
// Add-widget modal
// ---------------------------------------------------------------------------
function openWidgetModal() {
  const modal = document.getElementById('widget-modal');
  if (!modal) return;
  modal.classList.remove('hidden');
  loadTablesIntoSelect();
}

function closeWidgetModal() {
  document.getElementById('widget-modal')?.classList.add('hidden');
}

function loadTablesIntoSelect() {
  if (!currentDashboard) return;
  const wsId = currentDashboard.workspace_id || currentDashboard.workspace;
  if (!wsId) return;

  fetch(`/api/v1/workspaces/${wsId}/tables/?limit=200`, { credentials: 'same-origin' })
    .then(r => r.json())
    .then(resp => {
      const tables = resp.results || resp;
      const select = document.getElementById('widget-modal-table');
      if (!select) return;
      select.innerHTML = '<option value="">— No table —</option>' +
        tables.map(t => `<option value="${t.id}">${escHtml(t.name)}</option>`).join('');
    })
    .catch(console.error);
}

function submitNewWidget() {
  const title  = document.getElementById('widget-modal-title')?.value?.trim();
  const type   = document.getElementById('widget-modal-type')?.value;
  const tableId = document.getElementById('widget-modal-table')?.value;

  if (!title || !type) {
    alert('Please fill in a title and widget type.');
    return;
  }
  if (!currentDashboard) return;

  const payload = {
    widget: {
      title,
      widget_type: type,
      table_id:    tableId || null,
      query_config: {},
      viz_config:   {},
      position:    { x: 0, y: 0, w: 6, h: 5 },
    }
  };

  if (window._rtDashboard?.isConnected()) {
    window._rtDashboard.addWidget(payload.widget);
    closeWidgetModal();
  } else {
    fetch(`/api/v1/dashboards/${currentDashboard.id}/widgets/`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrf() },
      credentials: 'same-origin',
      body: JSON.stringify(payload.widget),
    })
      .then(r => r.ok ? r.json() : r.json().then(e => Promise.reject(e)))
      .then(widget => {
        closeWidgetModal();
        const el  = createWidgetElement(widget);
        const pos = widget.position || { x: 0, y: 0, w: 6, h: 5 };
        grid.addWidget(el, { x: pos.x, y: pos.y, w: pos.w, h: pos.h, id: widget.id });
        widgetRegistry.set(widget.id, { type: widget.widget_type, vizConfig: widget.viz_config || {} });
        fetchAndRenderWidget(widget.id);
      })
      .catch(err => alert(`Failed to create widget: ${err.error || err}`));
  }
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function debounce(fn, wait) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}
