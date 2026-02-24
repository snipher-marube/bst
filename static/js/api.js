// API Configuration
const API = {
    baseURL: '/api',
    headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': getCookie('csrftoken')
    },
    
    // Workspace endpoints
    async getCurrentWorkspace() {
        return this.request('/workspaces/current/');
    },
    
    async getWorkspaceStats() {
        return this.request('/workspaces/stats/');
    },
    
    async switchWorkspace(workspaceId) {
        return this.request(`/workspaces/${workspaceId}/switch/`, {
            method: 'POST'
        });
    },
    
    // Table endpoints
    async getTables(params = {}) {
        const queryString = new URLSearchParams(params).toString();
        return this.request(`/tables/?${queryString}`);
    },
    
    async getTable(tableId) {
        return this.request(`/tables/${tableId}/`);
    },
    
    async createTable(data) {
        return this.request('/tables/', {
            method: 'POST',
            body: JSON.stringify(data)
        });
    },
    
    async updateTable(tableId, data) {
        return this.request(`/tables/${tableId}/`, {
            method: 'PUT',
            body: JSON.stringify(data)
        });
    },
    
    async deleteTable(tableId) {
        return this.request(`/tables/${tableId}/`, {
            method: 'DELETE'
        });
    },
    
    // Record endpoints
    async getRecords(tableId, params = {}) {
        const queryString = new URLSearchParams(params).toString();
        return this.request(`/tables/${tableId}/records/?${queryString}`);
    },
    
    async createRecord(tableId, data) {
        return this.request(`/tables/${tableId}/records/`, {
            method: 'POST',
            body: JSON.stringify({ data })
        });
    },
    
    async updateRecord(tableId, recordId, data) {
        return this.request(`/tables/${tableId}/records/${recordId}/`, {
            method: 'PUT',
            body: JSON.stringify({ data })
        });
    },
    
    async deleteRecord(tableId, recordId) {
        return this.request(`/tables/${tableId}/records/${recordId}/`, {
            method: 'DELETE'
        });
    },
    
    async importCSV(tableId, file, mapping = {}) {
        const formData = new FormData();
        formData.append('file', file);
        formData.append('mapping', JSON.stringify(mapping));
        
        return fetch(`${this.baseURL}/tables/${tableId}/import_csv/`, {
            method: 'POST',
            headers: {
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: formData
        }).then(response => response.json());
    },
    
    // Dashboard endpoints
    async getDashboards(params = {}) {
        const queryString = new URLSearchParams(params).toString();
        return this.request(`/dashboards/?${queryString}`);
    },
    
    async getDashboard(dashboardId) {
        return this.request(`/dashboards/${dashboardId}/`);
    },
    
    async createDashboard(data) {
        return this.request('/dashboards/', {
            method: 'POST',
            body: JSON.stringify(data)
        });
    },
    
    async updateDashboard(dashboardId, data) {
        return this.request(`/dashboards/${dashboardId}/`, {
            method: 'PUT',
            body: JSON.stringify(data)
        });
    },
    
    // Widget endpoints
    async createWidget(dashboardId, data) {
        return this.request(`/dashboards/${dashboardId}/widgets/`, {
            method: 'POST',
            body: JSON.stringify(data)
        });
    },
    
    async updateWidget(widgetId, data) {
        return this.request(`/widgets/${widgetId}/`, {
            method: 'PUT',
            body: JSON.stringify(data)
        });
    },
    
    async deleteWidget(widgetId) {
        return this.request(`/widgets/${widgetId}/`, {
            method: 'DELETE'
        });
    },
    
    // Generic request method
    async request(endpoint, options = {}) {
        const url = this.baseURL + endpoint;
        const response = await fetch(url, {
            ...options,
            headers: {
                ...this.headers,
                ...options.headers
            }
        });
        
        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.detail || `HTTP error! status: ${response.status}`);
        }
        
        // Check if response is JSON
        const contentType = response.headers.get('content-type');
        if (contentType && contentType.includes('application/json')) {
            return response.json();
        }
        
        return response;
    }
};

// Helper to get CSRF token
function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== '') {
        const cookies = document.cookie.split(';');
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

// Set workspace ID header
function setWorkspaceHeader(workspaceId) {
    API.headers['X-Workspace-ID'] = workspaceId;
}

// Export for use in other scripts
window.API = API;
window.setWorkspaceHeader = setWorkspaceHeader;