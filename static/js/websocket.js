// static/js/websocket.js

class RealtimeDashboard {
    constructor(dashboardId, options = {}) {
        this.dashboardId = dashboardId;
        this.ws = null;
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 10;
        this.reconnectDelay = 3000;
        this.pingInterval = null;
        this.messageHandlers = new Map();
        this.debug = options.debug || false;
        
        this.init();
    }
    
    init() {
        this.connect();
        this.setupMessageHandlers();
        this.setupPingInterval();
    }
    
    connect() {
        const wsUrl = this.getWebSocketUrl();
        this.log(`Connecting to ${wsUrl}`);
        
        this.ws = new WebSocket(wsUrl);
        
        this.ws.onopen = () => {
            this.log('WebSocket connected');
            this.reconnectAttempts = 0;
            this.updateConnectionStatus(true);
            this.trigger('connected');
        };
        
        this.ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                this.handleMessage(data);
            } catch (e) {
                this.log('Error parsing message:', e);
            }
        };
        
        this.ws.onclose = (event) => {
            this.log('WebSocket disconnected', event.code, event.reason);
            this.updateConnectionStatus(false);
            this.trigger('disconnected', { code: event.code });
            this.reconnect();
        };
        
        this.ws.onerror = (error) => {
            this.log('WebSocket error:', error);
            this.trigger('error', error);
        };
    }
    
    getWebSocketUrl() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const host = window.location.host;
        return `${protocol}//${host}/ws/dashboard/${this.dashboardId}/`;
    }
    
    reconnect() {
        if (this.reconnectAttempts >= this.maxReconnectAttempts) {
            this.log('Max reconnection attempts reached');
            this.trigger('reconnect_failed');
            return;
        }
        
        this.reconnectAttempts++;
        const delay = this.reconnectDelay * this.reconnectAttempts;
        
        this.log(`Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts})`);
        
        setTimeout(() => {
            this.connect();
        }, delay);
    }
    
    setupPingInterval() {
        // Send ping every 30 seconds to keep connection alive
        this.pingInterval = setInterval(() => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.send({
                    type: 'ping',
                    timestamp: Date.now()
                });
            }
        }, 30000);
    }
    
    setupMessageHandlers() {
        // Default message handlers
        this.on('widget_update', (data) => {
            this.log('Widget updated:', data.widget_id);
            this.updateWidget(data.widget_id, data.data);
        });
        
        this.on('layout_update', (data) => {
            this.log('Layout updated');
            this.updateLayout(data.layout, data.widgets);
        });
        
        this.on('widget_added', (data) => {
            this.log('Widget added');
            this.addWidget(data.widget);
        });
        
        this.on('widget_deleted', (data) => {
            this.log('Widget deleted:', data.widget_id);
            this.removeWidget(data.widget_id);
        });
        
        this.on('user_joined', (data) => {
            this.log('User joined:', data.user);
            this.showNotification(`${data.user} joined the dashboard`);
        });
        
        this.on('user_left', (data) => {
            this.log('User left:', data.user);
            this.showNotification(`${data.user} left the dashboard`);
        });
        
        this.on('error', (data) => {
            this.log('Server error:', data.message);
            this.showNotification(`Error: ${data.message}`, 'error');
        });
    }
    
    handleMessage(data) {
        this.log('Received:', data);
        
        const handlers = this.messageHandlers.get(data.type) || [];
        handlers.forEach(handler => {
            try {
                handler(data);
            } catch (e) {
                this.log('Error in handler:', e);
            }
        });
    }
    
    on(type, handler) {
        if (!this.messageHandlers.has(type)) {
            this.messageHandlers.set(type, []);
        }
        this.messageHandlers.get(type).push(handler);
    }
    
    off(type, handler) {
        const handlers = this.messageHandlers.get(type);
        if (handlers) {
            const index = handlers.indexOf(handler);
            if (index !== -1) {
                handlers.splice(index, 1);
            }
        }
    }
    
    send(data) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(data));
            this.log('Sent:', data);
        } else {
            this.log('Cannot send, WebSocket not connected');
        }
    }
    
    refreshWidget(widgetId) {
        this.send({
            type: 'refresh_widget',
            widget_id: widgetId
        });
    }
    
    updateLayout(layout, widgets) {
        this.send({
            type: 'update_layout',
            layout: layout,
            widgets: widgets
        });
    }
    
    addWidget(widgetData) {
        this.send({
            type: 'add_widget',
            widget: widgetData
        });
    }
    
    deleteWidget(widgetId) {
        this.send({
            type: 'delete_widget',
            widget_id: widgetId
        });
    }
    
    requestInsights() {
        this.send({
            type: 'request_insights'
        });
    }
    
    updateConnectionStatus(connected) {
        const indicator = document.getElementById('realtimeIndicator');
        if (indicator) {
            if (connected) {
                indicator.className = 'realtime-indicator bg-green-500';
                indicator.innerHTML = '<i class="fas fa-circle text-white text-[8px] mr-1"></i>Live';
            } else {
                indicator.className = 'realtime-indicator bg-gray-500';
                indicator.innerHTML = '<i class="fas fa-circle text-white text-[8px] mr-1"></i>Reconnecting...';
            }
        }
    }
    
    updateWidget(widgetId, data) {
        // Find the widget element
        const widgetEl = document.querySelector(`[data-id="${widgetId}"]`);
        if (!widgetEl) return;
        
        // Update widget content (implement based on your renderer)
        const event = new CustomEvent('widget-update', {
            detail: { widgetId, data }
        });
        document.dispatchEvent(event);
    }
    
    updateLayout(layout, widgets) {
        const event = new CustomEvent('layout-update', {
            detail: { layout, widgets }
        });
        document.dispatchEvent(event);
    }
    
    addWidget(widget) {
        const event = new CustomEvent('widget-add', {
            detail: { widget }
        });
        document.dispatchEvent(event);
    }
    
    removeWidget(widgetId) {
        const event = new CustomEvent('widget-remove', {
            detail: { widgetId }
        });
        document.dispatchEvent(event);
    }
    
    showNotification(message, type = 'info') {
        // Create notification element
        const notification = document.createElement('div');
        notification.className = `fixed bottom-4 right-4 px-4 py-2 rounded-lg shadow-lg z-50 animate-slide-up ${
            type === 'error' ? 'bg-red-500' : 'bg-green-500'
        } text-white`;
        notification.textContent = message;
        
        document.body.appendChild(notification);
        
        // Remove after 3 seconds
        setTimeout(() => {
            notification.remove();
        }, 3000);
    }
    
    trigger(event, data) {
        const customEvent = new CustomEvent(`dashboard:${event}`, {
            detail: data
        });
        document.dispatchEvent(customEvent);
    }
    
    log(...args) {
        if (this.debug) {
            console.log('[RealtimeDashboard]', ...args);
        }
    }
    
    disconnect() {
        if (this.pingInterval) {
            clearInterval(this.pingInterval);
        }
        
        if (this.ws) {
            this.ws.close();
        }
    }
}

// Export for use
window.RealtimeDashboard = RealtimeDashboard;