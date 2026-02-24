// Dashboard state
let currentWorkspace = null;
let currentDashboard = null;
let grid = null;

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    loadWorkspace();
    initGrid();
    setupEventListeners();
});

function loadWorkspace() {
    // Get workspace from session
    fetch('/api/workspaces/current/')
        .then(response => response.json())
        .then(data => {
            currentWorkspace = data;
            document.getElementById('workspace-name').textContent = data.name;
            loadDashboard();
        });
}

function initGrid() {
    grid = GridStack.init({
        column: 12,
        cellHeight: 100,
        margin: 10,
        float: true,
        acceptWidgets: true
    });
}

function loadDashboard() {
    // Load dashboard data
    const dashboardId = window.location.pathname.split('/').pop();
    fetch(`/api/dashboards/${dashboardId}/`)
        .then(response => response.json())
        .then(data => {
            currentDashboard = data;
            document.getElementById('dashboard-title').textContent = data.name;
            renderWidgets(data.widgets);
        });
}

function renderWidgets(widgets) {
    grid.removeAll();
    widgets.forEach(widget => {
        const widgetHtml = createWidgetElement(widget);
        grid.addWidget(widgetHtml, widget.position);
        loadWidgetData(widget);
    });
}

function createWidgetElement(widget) {
    const div = document.createElement('div');
    div.className = 'grid-stack-item';
    div.innerHTML = `
        <div class="grid-stack-item-content bg-white rounded-lg shadow-sm border border-gray-200 p-4">
            <div class="flex justify-between items-center mb-3">
                <h3 class="font-semibold text-[#03466e]">${widget.title}</h3>
                <div class="flex space-x-2">
                    <button class="text-gray-400 hover:text-[#f68712] edit-widget" data-id="${widget.id}">
                        <i class="fas fa-edit"></i>
                    </button>
                    <button class="text-gray-400 hover:text-red-500 delete-widget" data-id="${widget.id}">
                        <i class="fas fa-trash"></i>
                    </button>
                </div>
            </div>
            <div class="widget-content" id="widget-${widget.id}">
                <div class="flex justify-center items-center h-48">
                    <i class="fas fa-spinner fa-spin text-2xl text-gray-400"></i>
                </div>
            </div>
        </div>
    `;
    return div;
}

function loadWidgetData(widget) {
    fetch(`/api/widgets/${widget.id}/data/`)
        .then(response => response.json())
        .then(data => {
            renderWidgetChart(widget, data);
        });
}

function renderWidgetChart(widget, data) {
    const container = document.getElementById(`widget-${widget.id}`);
    if (!container) return;
    
    container.innerHTML = '';
    
    switch(widget.widget_type) {
        case 'line_chart':
            renderLineChart(container, data, widget.viz_config);
            break;
        case 'bar_chart':
            renderBarChart(container, data, widget.viz_config);
            break;
        case 'pie_chart':
            renderPieChart(container, data, widget.viz_config);
            break;
        case 'metric':
            renderMetric(container, data, widget.viz_config);
            break;
        default:
            container.innerHTML = '<p class="text-gray-500">Preview not available</p>';
    }
}

function setupEventListeners() {
    document.getElementById('add-widget').addEventListener('click', () => {
        document.getElementById('widget-modal').classList.remove('hidden');
        loadTables();
    });
    
    document.getElementById('close-modal').addEventListener('click', () => {
        document.getElementById('widget-modal').classList.add('hidden');
    });
    
    document.getElementById('save-widget').addEventListener('click', createWidget);
    
    document.getElementById('save-dashboard').addEventListener('click', saveDashboardLayout);
}

function loadTables() {
    fetch(`/api/workspaces/${currentWorkspace.id}/tables/`)
        .then(response => response.json())
        .then(data => {
            const select = document.getElementById('widget-table');
            select.innerHTML = data.map(table => 
                `<option value="${table.id}">${table.name}</option>`
            ).join('');
        });
}

function createWidget() {
    const widgetData = {
        widget_type: document.getElementById('widget-type').value,
        title: document.getElementById('widget-title').value,
        table_id: document.getElementById('widget-table').value,
        position: {x: 0, y: 0, w: 6, h: 4}
    };
    
    fetch(`/api/dashboards/${currentDashboard.id}/widgets/`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(widgetData)
    })
    .then(response => response.json())
    .then(widget => {
        document.getElementById('widget-modal').classList.add('hidden');
        location.reload(); // Simple reload for now
    });
}

function saveDashboardLayout() {
    const widgets = [];
    grid.engine.nodes.forEach(node => {
        widgets.push({
            id: node.id,
            position: {x: node.x, y: node.y, w: node.w, h: node.h}
        });
    });
    
    fetch(`/api/dashboards/${currentDashboard.id}/update_layout/`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({widgets: widgets})
    })
    .then(response => response.json())
    .then(() => {
        alert('Dashboard saved!');
    });
}