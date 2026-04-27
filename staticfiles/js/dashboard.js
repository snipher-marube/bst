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

// Map widgetId → { type, vizConfig } so re-renders know how to draw
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
    cellHeight: 80,
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
  widgets.forEach(widget => {
    const pos  = widget.position || { x: 0, y: 0, w: 6, h: 4 };
    const el   = createWidgetElement(widget);
    grid.addWidget(el, { x: pos.x, y: pos.y, w: pos.w, h: pos.h, id: widget.id });
    widgetRegistry.set(widget.id, {
      type:      widget.widget_type,
      vizConfig: widget.viz_config || {},
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
    <div class="grid-stack-item-content bg-white rounded-xl shadow-sm border border-gray-100 flex flex-col overflow-hidden">
      <!-- Header -->
      <div class="widget-drag-handle flex items-center justify-between px-4 py-2 border-b border-gray-100 cursor-move select-none bg-gray-50">
        <div class="flex items-center gap-2 min-w-0">
          <i class="fas fa-grip-vertical text-gray-300 text-xs"></i>
          <span class="text-sm font-semibold text-gray-700 truncate">${escHtml(widget.title)}</span>
        </div>
        <div class="flex items-center gap-1 flex-shrink-0">
          <button class="widget-refresh-btn p-1 text-gray-400 hover:text-[#03466e] transition-colors rounded"
                  data-id="${widget.id}" title="Refresh">
            <i class="fas fa-sync-alt text-xs"></i>
          </button>
          <button class="widget-edit-btn p-1 text-gray-400 hover:text-[#f68712] transition-colors rounded"
                  data-id="${widget.id}" title="Edit">
            <i class="fas fa-edit text-xs"></i>
          </button>
          <button class="widget-delete-btn p-1 text-gray-400 hover:text-red-500 transition-colors rounded"
                  data-id="${widget.id}" title="Delete">
            <i class="fas fa-trash text-xs"></i>
          </button>
        </div>
      </div>
      <!-- Body -->
      <div class="widget-body flex-1 p-3 min-h-0" id="widget-body-${widget.id}">
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
      case 'bar_chart':  renderBarChart(container, data, vizConfig);   break;
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

  Plotly.newPlot(plotDiv, [{
    x:    grouped.labels,
    y:    grouped.values,
    type: 'scatter',
    mode: 'lines+markers',
    line: { color: '#03466e', width: 2.5, shape: 'spline', smoothing: 0.8 },
    marker: { color: '#f68712', size: 5 },
    hovertemplate: '<b>%{x}</b><br>%{y:,.2f}<extra></extra>',
    fill: 'tozeroy',
    fillcolor: 'rgba(3,70,110,0.06)',
  }], plotlyLayout(container), plotlyConfig());
}

// ---------------------------------------------------------------------------
// Bar chart
// ---------------------------------------------------------------------------
function renderBarChart(container, data, viz) {
  const plotDiv = createPlotDiv(container);
  const valKey  = viz.y_axis || 'val';
  const grouped = extractGroupedData(data, valKey);

  if (!grouped.labels.length) { renderWidgetEmptyInContainer(container, 'No data to chart'); return; }

  Plotly.newPlot(plotDiv, [{
    x:    grouped.labels,
    y:    grouped.values,
    type: 'bar',
    marker: {
      color: grouped.values.map((_, i) =>
        i % 2 === 0 ? '#03466e' : '#f68712'),
      opacity: 0.85,
    },
    hovertemplate: '<b>%{x}</b><br>%{y:,.2f}<extra></extra>',
  }], plotlyLayout(container), plotlyConfig());
}

// ---------------------------------------------------------------------------
// Pie chart
// ---------------------------------------------------------------------------
function renderPieChart(container, data, viz) {
  const plotDiv = createPlotDiv(container);
  const valKey  = viz.y_axis || 'val';
  const grouped = extractGroupedData(data, valKey);

  if (!grouped.labels.length) { renderWidgetEmptyInContainer(container, 'No data to chart'); return; }

  const palette = ['#03466e','#f68712','#0ea5e9','#10b981','#8b5cf6','#ef4444','#f59e0b','#14b8a6'];

  Plotly.newPlot(plotDiv, [{
    labels: grouped.labels,
    values: grouped.values,
    type:   'pie',
    hole:   0.38,
    marker: { colors: palette },
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
  return {
    margin:      { t: 8, r: 8, b: 36, l: 44 },
    paper_bgcolor: 'transparent',
    plot_bgcolor:  'transparent',
    font:        { family: 'Inter, sans-serif', size: 11, color: '#6b7280' },
    xaxis:       { showgrid: false, zeroline: false, tickfont: { size: 10 } },
    yaxis:       { gridcolor: '#f3f4f6', zeroline: false, tickfont: { size: 10 } },
    hovermode:   'closest',
    hoverlabel:  { bgcolor: '#1f2937', font: { color: '#fff', size: 12 } },
    autosize:    true,
  };
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
      position:    { x: 0, y: 0, w: 6, h: 4 },
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
        const pos = widget.position || { x: 0, y: 0, w: 6, h: 4 };
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
