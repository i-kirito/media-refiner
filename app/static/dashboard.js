// ==========================================
// Media Refiner - Dashboard Shared JavaScript
// Apple-style utilities for the UI
// ==========================================

// ========== Chart.js 通用渲染 ==========

/**
 * 渲染水平柱状分布图 (用 CSS bars 而非 Chart.js)
 * @param {string} containerId - DOM 容器 ID
 * @param {Object} data - {key: count} 对象
 * @param {string[]} keys - 有序的 key 列表
 * @param {string[]} colors - 对应的颜色数组
 */
function renderBarChart(containerId, data, keys, colors) {
    const container = document.getElementById(containerId);
    if (!container || !data) return;
    const maxVal = Math.max(...keys.map(k => data[k] || 0), 1);
    container.innerHTML = keys.map((k, i) => `
        <div class="mb-2.5 last:mb-0">
            <div class="flex justify-between text-xs mb-1.5">
                <span class="font-bold text-gray-600 dark:text-gray-400">${k}</span>
                <span class="font-mono font-bold text-gray-500">${data[k] || 0}</span>
            </div>
            <div class="w-full bg-gray-100 dark:bg-gray-700 rounded-full h-2.5 overflow-hidden shadow-inner">
                <div class="h-full rounded-full energy-bar" style="width: ${((data[k] || 0) / maxVal * 100).toFixed(1)}%; background: ${colors[i]}"></div>
            </div>
        </div>
    `).join('');
    // Trigger transition by forcing reflow
    setTimeout(() => {
        container.querySelectorAll('.energy-bar').forEach(el => {
            const w = el.style.width;
            el.style.width = '0%';
            void el.offsetWidth;
            el.style.width = w;
        });
    }, 50);
}

/**
 * 使用 Chart.js 渲染饼图/环形图
 * @param {string} canvasId - Canvas 元素 ID
 * @param {Object} data - {label: value} 对象
 * @param {string[]} colors - 颜色数组
 * @param {string} type - 'doughnut' | 'pie'
 * @returns {Chart|null}
 */
function renderPieChart(canvasId, data, colors, type = 'doughnut') {
    const canvas = document.getElementById(canvasId);
    if (!canvas || !data) return null;
    const ctx = canvas.getContext('2d');
    const labels = Object.keys(data);
    const values = Object.values(data);

    // 销毁已有图表
    if (canvas._chart) canvas._chart.destroy();

    canvas._chart = new Chart(ctx, {
        type: type,
        data: {
            labels: labels,
            datasets: [{
                data: values,
                backgroundColor: colors,
                borderWidth: 0,
                hoverOffset: 4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            cutout: type === 'doughnut' ? '65%' : '0%',
            plugins: {
                legend: {
                    display: false
                },
                tooltip: {
                    backgroundColor: 'rgba(0,0,0,0.8)',
                    titleFont: { size: 12, weight: 'bold' },
                    bodyFont: { size: 11 },
                    padding: 10,
                    cornerRadius: 8,
                    displayColors: true
                }
            }
        }
    });
    return canvas._chart;
}

// ========== 扫描管理器 ==========

const ScanManager = {
    pollInterval: null,
    isScanning: false,
    startTime: null,
    _scanButton: null,
    _scanButtonText: null,

    /**
     * 初始化：绑定按钮，恢复扫描状态（页面加载时检查是否已有扫描在跑）
     */
    init(buttonId = 'btnStartScan') {
        this._scanButton = document.getElementById(buttonId);
        if (this._scanButton) {
            this._scanButtonText = this._scanButton.innerHTML;
        }
        // 页面加载时检查扫描状态
        this._checkStatusOnLoad();
    },

    async _checkStatusOnLoad() {
        try {
            const resp = await fetch('/api/scan/status');
            const json = await resp.json();
            if (json.status === 'success' && json.data.is_scanning) {
                this.isScanning = true;
                this.startTime = Date.now() - (json.data.elapsed_seconds || 0) * 1000;
                this._setButtonLoading(true);
                this._showProgress(json.data);
                this._startPolling();
            }
            // 显示已有扫描结果
            if (!json.data.is_scanning && json.data.total_count > 0) {
                this._showCompleted(json.data);
            }
        } catch (e) {
            // 静默失败，页面还没加载完成
        }
    },

    _setButtonLoading(loading) {
        if (!this._scanButton) return;
        if (loading) {
            this._scanButton.disabled = true;
            const inner = this._scanButton.innerHTML;
            if (!inner.includes('扫描中')) {
                this._scanButton._origHTML = inner;
            }
            this._scanButton.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin mr-1"></i> 扫描中...';
            this._scanButton.classList.add('opacity-70', 'cursor-not-allowed');
        } else {
            this._scanButton.disabled = false;
            this._scanButton.innerHTML = this._scanButton._origHTML || this._scanButtonText || '<i class="fa-solid fa-magnifying-glass mr-1"></i> 开始质量扫描';
            this._scanButton.classList.remove('opacity-70', 'cursor-not-allowed');
        }
    },

    async start(excludedLibraries = []) {
        if (this.isScanning) {
            showToast('warning', '已有扫描任务进行中');
            return;
        }
        this.startTime = Date.now();
        this._setButtonLoading(true);

        // 先展示初始进度卡片，再发请求（_setButtonLoading 可能跳过无按钮页面）
        this._showProgress({
            is_scanning: true,
            progress: 0,
            scanned_count: 0,
            total_count: 0,
            current_item: '正在连接到服务器...'
        });
        const el = document.getElementById('scan-progress');
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

        try {
            const resp = await fetch('/api/scan/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ excluded_libraries: excludedLibraries })
            });
            const json = await resp.json();
            if (json.status === 'success') {
                // 后台扫描，立即返回 → 直接开始轮询进度
                this.isScanning = true;
                this._startPolling();
            } else {
                this._setButtonLoading(false);
                const errMsg = json.detail || json.message || '启动扫描失败';
                if (resp.status === 400 && errMsg.includes('进行中')) {
                    showToast('warning', errMsg);
                } else {
                    showToast('error', errMsg);
                }
            }
        } catch (e) {
            this._setButtonLoading(false);
            showToast('error', '网络请求失败，请检查连接');
        }
    },

    _startPolling() {
        if (this.pollInterval) clearInterval(this.pollInterval);
        this.pollInterval = setInterval(async () => {
            try {
                const resp = await fetch('/api/scan/status');
                const json = await resp.json();
                if (json.status === 'success') {
                    const data = json.data;
                    this._showProgress(data);
                    if (!data.is_scanning) {
                        this._stopPolling();
                        this._showCompleted(data);
                        showToast('success', '质量扫描已完成');
                        // 触发页面刷新
                        window.dispatchEvent(new CustomEvent('scan-complete', { detail: data }));
                    }
                }
            } catch (e) {
                console.error('Scan poll error:', e);
            }
        }, 1500);
    },

    _stopPolling() {
        if (this.pollInterval) {
            clearInterval(this.pollInterval);
            this.pollInterval = null;
        }
        this.isScanning = false;
        this.startTime = null;
        this._setButtonLoading(false);
    },

    /** 格式化耗时 */
    _formatElapsed(seconds) {
        if (!seconds || seconds < 0) return '';
        const m = Math.floor(seconds / 60);
        const s = seconds % 60;
        if (m < 1) return `${s}秒`;
        return `${m}分${s}秒`;
    },

    /** 估算剩余时间（匀速推断） */
    _estimateRemaining(data) {
        if (!data.total_count || !data.scanned_count || data.scanned_count < 10) return '';
        const elapsed = (Date.now() - this.startTime) / 1000;
        if (elapsed < 5) return '';
        const rate = data.scanned_count / elapsed; // 条/秒
        const remaining = (data.total_count - data.scanned_count) / rate;
        if (remaining < 5 || remaining > 3600) return '';
        return this._formatElapsed(Math.round(remaining));
    },

    _showProgress(data) {
        const container = document.getElementById('scan-progress');
        if (!container) return;

        container.classList.remove('hidden');
        const pct = Math.min(data.progress || 0, 100);
        const scanned = data.scanned_count || 0;
        const total = data.total_count || 0;
        const elapsed = this.startTime ? Math.round((Date.now() - this.startTime) / 1000) : 0;
        const eta = this._estimateRemaining(data);

        // 初始状态（total=0 还没拿到总数）
        const isInitial = data.is_scanning && total === 0;

        container.innerHTML = `
            <div class="apple-card p-4 md:p-5">
                <!-- 顶部状态 -->
                <div class="flex items-center justify-between mb-4">
                    <div class="flex items-center gap-2.5">
                        <div class="w-8 h-8 rounded-full flex items-center justify-center ${data.is_scanning ? 'bg-brand-50 dark:bg-brand-500/20 scanning-pulse' : 'bg-green-50 dark:bg-green-500/20'}">
                            <i class="fa-solid ${data.is_scanning ? 'fa-circle-notch fa-spin text-brand-500' : 'fa-check text-green-500'} text-sm"></i>
                        </div>
                        <div>
                            <div class="text-sm font-bold text-gray-800 dark:text-gray-200">
                                ${isInitial ? '正在准备扫描...'
                                    : data.is_scanning ? '正在扫描媒体质量'
                                    : '扫描完成'}
                            </div>
                            <div class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                                ${isInitial ? '获取媒体库信息中'
                                    : data.is_scanning
                                        ? `已扫描 <span class="font-mono font-bold">${scanned}</span> / <span class="font-mono">${total}</span> 项`
                                        : `共扫描 <span class="font-mono font-bold">${total}</span> 项`
                                }
                            </div>
                        </div>
                    </div>
                    <div class="text-right">
                        <div class="text-lg font-black font-mono ${data.is_scanning ? 'text-brand-500' : 'text-green-500'}">${isInitial ? '--' : pct + '%'}</div>
                        ${elapsed > 0 ? `<div class="text-[10px] text-gray-400">耗时 ${this._formatElapsed(elapsed)}</div>` : ''}
                    </div>
                </div>

                <!-- 进度条 -->
                <div class="relative w-full bg-gray-100 dark:bg-gray-700 rounded-full h-3 overflow-hidden shadow-inner mb-3">
                    <div class="h-full rounded-full progress-bar ${data.is_scanning ? 'scan-bar-active' : 'scan-bar-done'}"
                         style="width: ${isInitial ? 2 : pct}%"></div>
                    ${data.is_scanning ? `
                    <div class="absolute inset-0 overflow-hidden rounded-full">
                        <div class="scan-bar-shimmer"></div>
                    </div>` : ''}
                </div>

                <!-- 底部详情 -->
                <div class="flex items-center justify-between text-xs">
                    <div class="flex-1 min-w-0 flex items-center gap-2">
                        ${isInitial
                            ? '<span class="text-gray-400"><i class="fa-solid fa-spinner fa-spin mr-1"></i>正在连接 Emby... </span>'
                            : data.current_item
                                ? `<i class="fa-solid fa-film text-gray-300 dark:text-gray-600 shrink-0"></i>
                                   <span class="text-gray-500 dark:text-gray-400 truncate scan-item-name">${data.current_item}</span>`
                                : '<span class="text-gray-400">准备中...</span>'
                        }
                    </div>
                    ${eta ? `<span class="text-gray-400 shrink-0 ml-2 font-medium">剩余约 ${eta}</span>` : ''}
                </div>
            </div>
        `;
    },

    _showCompleted(data) {
        const container = document.getElementById('scan-progress');
        if (!container) return;

        container.classList.remove('hidden');
        const total = data.total_count || 0;
        const elapsed = this.startTime ? Math.round((Date.now() - this.startTime) / 1000) : 0;

        container.innerHTML = `
            <div class="apple-card p-4 md:p-5 scan-complete-card">
                <div class="flex items-center gap-4">
                    <div class="w-14 h-14 rounded-full bg-green-50 dark:bg-green-500/20 flex items-center justify-center shrink-0 scan-check-container">
                        <i class="fa-solid fa-check text-2xl text-green-500 scan-check-icon"></i>
                    </div>
                    <div class="flex-1 min-w-0">
                        <div class="text-base font-bold text-gray-800 dark:text-gray-200">质量扫描已完成</div>
                        <div class="text-sm text-gray-500 dark:text-gray-400 mt-0.5">
                            共扫描 <span class="font-mono font-bold text-green-500">${total}</span> 个媒体文件
                            ${elapsed > 0 ? `，耗时 ${this._formatElapsed(elapsed)}` : ''}
                        </div>
                    </div>
                    <button onclick="ScanManager._dismissCompleted()"
                            class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 transition-colors p-1">
                        <i class="fa-solid fa-xmark text-lg"></i>
                    </button>
                </div>
            </div>
        `;
    },

    _dismissCompleted() {
        const container = document.getElementById('scan-progress');
        if (container) {
            container.classList.add('hidden');
        }
        window.dispatchEvent(new CustomEvent('scan-dismiss'));
    }
};

// ========== 格式化工具 ==========

function formatFileSize(bytes) {
    if (!bytes) return '未知大小';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    let size = bytes;
    while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
    return size.toFixed(i > 0 ? 1 : 0) + ' ' + units[i];
}

function formatDate(dateStr) {
    if (!dateStr) return '';
    const d = new Date(dateStr);
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function safeJson(text) {
    try { return JSON.parse(text); } catch(e) { return {}; }
}

// ========== 通用 API 请求 ==========

async function apiGet(url) {
    const resp = await fetch(url);
    const json = await resp.json();
    if (json.status === 'success') return json.data;
    throw new Error(json.message || json.detail || '请求失败');
}

async function apiPost(url, body = {}) {
    const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    });
    const json = await resp.json();
    if (json.status === 'success') return json.data || json;
    throw new Error(json.message || json.detail || '请求失败');
}
