/**
 * TANSAT SSE Stream Client
 * Connects to /api/stream, dispatches events to registered page handlers.
 * Auto-reconnects on disconnect.
 */

const TansatStream = (() => {
    let _es = null;
    let _reconnectTimer = null;
    const _handlers = {}; // eventType -> [callback]
    let _connected = false;

    function on(eventType, callback) {
        if (!_handlers[eventType]) _handlers[eventType] = [];
        _handlers[eventType].push(callback);
    }

    function off(eventType, callback) {
        if (!_handlers[eventType]) return;
        _handlers[eventType] = _handlers[eventType].filter(h => h !== callback);
    }

    function _dispatch(eventType, data) {
        (_handlers[eventType] || []).forEach(h => {
            try { h(data); } catch(e) { console.error('TansatStream handler error:', e); }
        });
    }

    function _updateConnectionUI(connected) {
        _connected = connected;
        const dot = document.querySelector('.status-dot');
        const label = document.querySelector('.status-label');
        if (dot) {
            dot.className = 'status-dot ' + (connected ? 'connected' : 'disconnected');
        }
        if (label) label.textContent = connected ? 'Connected' : 'Disconnected';
        // Show/hide live indicator
        const live = document.querySelector('.live-indicator');
        if (live) live.style.display = connected ? 'block' : 'none';
    }

    function connect() {
        if (_es) { _es.close(); _es = null; }
        _es = new EventSource('/api/stream');

        // Handle named events
        ['status', 'packet', 'raw', 'uplink', 'image', 'control', 'error'].forEach(type => {
            _es.addEventListener(type, e => {
                let data;
                try { data = JSON.parse(e.data); } catch { data = e.data; }
                if (type === 'status') _updateConnectionUI(data.acquisition_running || false);
                _dispatch(type, data);
            });
        });

        _es.onopen = () => {
            _updateConnectionUI(false); // will be updated by first status event
            if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
            console.log('[TansatStream] SSE connected');
        };

        _es.onerror = () => {
            _updateConnectionUI(false);
            _es.close();
            _es = null;
            // Auto-reconnect after 3s
            if (!_reconnectTimer) {
                _reconnectTimer = setTimeout(() => {
                    _reconnectTimer = null;
                    connect();
                }, 3000);
            }
        };
    }

    function disconnect() {
        if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
        if (_es) { _es.close(); _es = null; }
        _updateConnectionUI(false);
    }

    // Format helpers used by all pages
    const fmt = {
        /** Format a telemetry number or return em dash if value is null/undefined */
        num(v, decimals=2) {
            if (v === null || v === undefined) return '\u2014';
            return Number(v).toFixed(decimals);
        },
        deg(v) { return fmt.num(v, 1) + '°'; },
        volt(v) { return fmt.num(v, 2) + ' V'; },
        pct(v) { return fmt.num(v, 1) + '%'; },
        km(v) { return v == null ? '\u2014' : fmt.num(v/1000, 2) + ' km'; },
        /** GPS lock guard: if !gpsLock, return em dash regardless of value */
        geo(v, gpsLock, decimals=4) {
            if (!gpsLock || v === null || v === undefined) return '\u2014';
            return Number(v).toFixed(decimals) + '°';
        },
    };

    // Auto-connect on page load
    document.addEventListener('DOMContentLoaded', connect);
    window.addEventListener('beforeunload', disconnect);

    // Expose on window so templates can guard with `if (window.TansatStream)`
    window.TansatStream = { on, off, connect, disconnect, fmt };

    return { on, off, connect, disconnect, fmt };
})();
