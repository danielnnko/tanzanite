/**
 * TANZANITESAT Chart helpers.
 * Exact series colors per design system §4.
 */
const SERIES_COLORS = {
    battery:    '#00e6a0',
    temperature:'#ff4d6a',
    altitude:   '#4f7cff',
    range:      '#a855f7',
    range_rate: '#ffb020',
    doppler:    '#00d4ff',
    azimuth:    '#ec4899',
    elevation:  '#ffd23f',
};

const CHART_DEFAULTS = {
    backgroundColor: '#0f1015',
    gridColor: '#2a2d3d',
    textColor: '#8b8fa8',
    font: 'JetBrains Mono, Cascadia Mono, monospace',
    maxPoints: 300,  // rolling window
};

/**
 * Create a rolling time-series line chart.
 * @param {string|HTMLCanvasElement} canvasOrId
 * @param {string} metricKey - key in SERIES_COLORS
 * @param {string} label
 * @param {string} unit
 * @param {number} maxPoints
 */
function createTimeSeriesChart(canvasOrId, metricKey, label, unit = '', maxPoints = CHART_DEFAULTS.maxPoints) {
    const canvas = typeof canvasOrId === 'string' ? document.getElementById(canvasOrId) : canvasOrId;
    if (!canvas) return null;
    const color = SERIES_COLORS[metricKey] || '#4f7cff';

    return new Chart(canvas, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: label + (unit ? ` (${unit})` : ''),
                data: [],
                borderColor: color,
                backgroundColor: color + '22',
                borderWidth: 1.5,
                pointRadius: 0,
                fill: true,
                tension: 0.2,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 0 },
            interaction: { intersect: false, mode: 'index' },
            plugins: {
                legend: { labels: { color: CHART_DEFAULTS.textColor, font: { family: CHART_DEFAULTS.font, size: 11 } } },
                tooltip: { bodyFont: { family: CHART_DEFAULTS.font } }
            },
            scales: {
                x: {
                    ticks: { color: CHART_DEFAULTS.textColor, font: { family: CHART_DEFAULTS.font, size: 10 }, maxTicksLimit: 8 },
                    grid: { color: CHART_DEFAULTS.gridColor },
                },
                y: {
                    ticks: { color: CHART_DEFAULTS.textColor, font: { family: CHART_DEFAULTS.font, size: 10 } },
                    grid: { color: CHART_DEFAULTS.gridColor },
                    title: { display: !!unit, text: unit, color: CHART_DEFAULTS.textColor }
                }
            }
        },
        _maxPoints: maxPoints,
        _push(timestamp, value) {
            this.data.labels.push(timestamp);
            this.data.datasets[0].data.push(value);
            if (this.data.labels.length > this._maxPoints) {
                this.data.labels.shift();
                this.data.datasets[0].data.shift();
            }
            this.update('none');
        }
    });
}

/**
 * Create a dual time-series chart (e.g., azimuth + elevation).
 */
function createDualTimeSeriesChart(canvasOrId, metric1, metric2, label1, label2, unit='', maxPoints=CHART_DEFAULTS.maxPoints) {
    const canvas = typeof canvasOrId === 'string' ? document.getElementById(canvasOrId) : canvasOrId;
    if (!canvas) return null;

    return new Chart(canvas, {
        type: 'line',
        data: {
            labels: [],
            datasets: [
                {
                    label: label1,
                    data: [],
                    borderColor: SERIES_COLORS[metric1] || '#4f7cff',
                    backgroundColor: 'transparent',
                    borderWidth: 1.5,
                    pointRadius: 0,
                },
                {
                    label: label2,
                    data: [],
                    borderColor: SERIES_COLORS[metric2] || '#ffd23f',
                    backgroundColor: 'transparent',
                    borderWidth: 1.5,
                    pointRadius: 0,
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 0 },
            plugins: {
                legend: { labels: { color: CHART_DEFAULTS.textColor, font: { family: CHART_DEFAULTS.font, size: 11 } } }
            },
            scales: {
                x: { ticks: { color: CHART_DEFAULTS.textColor, font: { family: CHART_DEFAULTS.font, size: 10 }, maxTicksLimit: 8 }, grid: { color: CHART_DEFAULTS.gridColor } },
                y: { ticks: { color: CHART_DEFAULTS.textColor, font: { family: CHART_DEFAULTS.font, size: 10 } }, grid: { color: CHART_DEFAULTS.gridColor } }
            }
        },
        _maxPoints: maxPoints,
        _push(timestamp, v1, v2) {
            this.data.labels.push(timestamp);
            this.data.datasets[0].data.push(v1);
            this.data.datasets[1].data.push(v2);
            if (this.data.labels.length > this._maxPoints) {
                this.data.labels.shift();
                this.data.datasets[0].data.shift();
                this.data.datasets[1].data.shift();
            }
            this.update('none');
        }
    });
}
