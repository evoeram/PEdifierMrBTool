/**
 * Edifier MR BLE Control — Client Application
 * v1.2 — fixed volume control, frequency editing
 */

// ═══════════════════════════════════════════════════
// WebSocket & State
// ═══════════════════════════════════════════════════

let ws = null;
let state = { connected: false, bands: [] };
let presets = [];
let requestId = 0;
const pendingRequests = new Map();
let editingBandIndex = null;

// ── Volume tracking ────────────────────────────────
// Предотвращает перезапись слайдера пока пользователь его двигает
let volumeDragging = false;
let volumeDragValue = null;
let volumeLastSentTime = 0;
let volumeDebounceTimer = null;
const VOLUME_DEBOUNCE_MS = 300;
const VOLUME_SEND_MIN_MS = 350;

function wsConnect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${location.host}/ws`);

    ws.onopen = () => {
        console.log('[WS] Connected');
        updateConnectionUI('ws-connected');
    };

    ws.onclose = () => {
        console.log('[WS] Disconnected');
        updateConnectionUI('ws-disconnected');
        setTimeout(wsConnect, 3000);
    };

    ws.onerror = (e) => console.error('[WS] Error', e);

    ws.onmessage = (event) => {
        try {
            handleMessage(JSON.parse(event.data));
        } catch (e) {
            console.error('[WS] Parse error', e);
        }
    };
}

function handleMessage(msg) {
    // Ответ на запрос
    if (msg.id !== undefined && pendingRequests.has(msg.id)) {
        const { resolve, reject, cmd } = pendingRequests.get(msg.id);
        pendingRequests.delete(msg.id);
        if (msg.ok) {
            resolve(msg.data);
        } else {
            reject(new Error(msg.error || 'Unknown error'));
        }
        return;
    }

    // Broadcast
    switch (msg.type) {
        case 'state':
            state = msg.data;
            renderAll();
            break;

        case 'volumeUpdate':
            // Лёгкое обновление громкости — не перерисовывать всё
            if (msg.data) {
                state.volume = msg.data.volume;
                state.maxVolume = msg.data.maxVolume;
                state.volumePercent = msg.data.volumePercent;
                // Обновляем UI только если пользователь НЕ двигает слайдер
                if (!volumeDragging) {
                    renderVolumeDisplay();
                }
            }
            break;

        case 'presets':
            presets = msg.data;
            renderPresets();
            break;

        case 'result':
            if (!msg.ok && msg.error) {
                toast(msg.error, 'error');
            }
            break;
    }
}

function send(cmd, params = {}) {
    return new Promise((resolve, reject) => {
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            reject(new Error('Not connected to server'));
            return;
        }
        const id = ++requestId;
        pendingRequests.set(id, { resolve, reject, cmd });

        ws.send(JSON.stringify({ cmd, params, id }));

        // Таймаут — увеличен для BLE
        const timeoutMs = cmd.startsWith('setVolume') ? 5000 : 10000;
        setTimeout(() => {
            if (pendingRequests.has(id)) {
                pendingRequests.delete(id);
                reject(new Error('Timeout'));
            }
        }, timeoutMs);
    });
}

// «Fire and forget» — не ждём ответ, не показываем ошибки
function sendNoWait(cmd, params = {}) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const id = ++requestId;
    // Регистрируем но не паникуем при таймауте
    pendingRequests.set(id, {
        resolve: () => {},
        reject: () => {},
        cmd
    });
    ws.send(JSON.stringify({ cmd, params, id }));
    setTimeout(() => pendingRequests.delete(id), 5000);
}


// ═══════════════════════════════════════════════════
// Frequency Helpers
// ═══════════════════════════════════════════════════

const COMMON_FREQUENCIES = [
    20, 25, 31, 40, 50, 62, 80, 100, 125, 160,
    200, 250, 315, 400, 500, 630, 800, 1000, 1250,
    1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000,
    10000, 12500, 16000, 20000
];

function getSuggestedFreqs(bandIndex, totalBands) {
    const ratio = bandIndex / (totalBands - 1);
    if (ratio < 0.15) return [20, 31, 40, 50, 62, 80, 100, 125];
    if (ratio < 0.35) return [80, 100, 125, 160, 200, 250, 315, 400];
    if (ratio < 0.55) return [250, 315, 400, 500, 630, 800, 1000, 1250];
    if (ratio < 0.75) return [800, 1000, 1250, 1600, 2000, 2500, 3150, 4000];
    return [2500, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000];
}

function formatFreq(f) {
    if (f >= 10000) return `${(f / 1000).toFixed(0)}k`;
    if (f >= 1000) return `${(f / 1000).toFixed(f % 1000 ? 1 : 0)}k`;
    return `${f}`;
}

function formatGain(g) {
    return (g >= 0 ? '+' : '') + g.toFixed(1);
}


// ═══════════════════════════════════════════════════
// Rendering
// ═══════════════════════════════════════════════════

function renderAll() {
    updateConnectionUI(state.connected ? 'ble-connected' : 'ble-disconnected');
    if (state.connected) {
        renderDeviceInfo();
        // Обновляем громкость только если не двигаем слайдер
        if (!volumeDragging) {
            renderVolumeDisplay();
        }
        renderEQSliders();
        drawEQCanvas();
        renderAdvanced();
    }
}

function updateConnectionUI(status) {
    const dot = document.getElementById('connDot');
    const label = document.getElementById('connLabel');
    const connectPanel = document.getElementById('connectPanel');
    const mainContent = document.getElementById('mainContent');
    const badge = document.getElementById('modelBadge');

    switch (status) {
        case 'ws-disconnected':
            dot.className = 'dot';
            label.textContent = 'Server offline';
            connectPanel.classList.remove('hidden');
            mainContent.classList.add('hidden');
            break;
        case 'ws-connected':
            dot.className = 'dot';
            label.textContent = 'Disconnected';
            connectPanel.classList.remove('hidden');
            mainContent.classList.add('hidden');
            break;
        case 'ble-connected':
            dot.className = 'dot connected';
            label.textContent = state.name || 'Connected';
            badge.textContent = state.model || '—';
            connectPanel.classList.add('hidden');
            mainContent.classList.remove('hidden');
            break;
        case 'ble-disconnected':
            dot.className = 'dot';
            label.textContent = 'Disconnected';
            badge.textContent = '—';
            connectPanel.classList.remove('hidden');
            mainContent.classList.add('hidden');
            break;
    }
}

function renderDeviceInfo() {
    const rows = [
        ['Model', state.model],
        ['Name', state.name],
        ['Address', state.address],
        ['Firmware', state.firmware],
        ['Volume', `${state.volume}/${state.maxVolume} (${state.volumePercent}%)`],
        ['Codec', state.codec],
        ['Speaker', state.activeSpeaker],
        ['Low Cut', `${state.lowCutFreq}Hz / ${state.lowCutSlope}`],
        ['Space', state.acousticSpace > 0 ? `-${state.acousticSpace}dB` : '0dB'],
        ['Desktop', state.desktopMode ? 'On' : 'Off'],
        ['Prompt', state.promptTone ? 'On' : 'Off'],
        ['Source', state.inputSource],
        ['EQ', `${state.eqBandCount}-band`],
    ];
    if (state.eqPresetName) rows.push(['Preset', state.eqPresetName]);

    const tbody = document.querySelector('#infoTable tbody');
    tbody.innerHTML = rows.map(([k, v]) =>
        `<tr><td>${k}</td><td>${v || '—'}</td></tr>`
    ).join('');
}


// ═══════════════════════════════════════════════════
// Volume — полностью переработан
// ═══════════════════════════════════════════════════

/**
 * Обновить отображение громкости.
 * Вызывается из renderAll() и volumeUpdate, но НЕ во время drag.
 */
function renderVolumeDisplay() {
    const slider = document.getElementById('volSlider');
    const valEl = document.getElementById('volValue');
    const maxEl = document.getElementById('volMax');
    const pctEl = document.getElementById('volPercent');

    const vol = volumeDragging ? volumeDragValue : (state.volume || 0);
    const mx = state.maxVolume || 30;

    slider.max = mx;

    // Не обновляем slider.value если пользователь его тянет
    if (!volumeDragging) {
        slider.value = vol;
    }

    const pct = mx > 0 ? Math.round(vol / mx * 100) : 0;
    slider.style.setProperty('--vol-pct', pct + '%');
    valEl.textContent = vol;
    maxEl.textContent = `/ ${mx}`;
    pctEl.textContent = `${pct}%`;
}

/**
 * Вызывается при КАЖДОМ движении слайдера (input event).
 * Только обновляет UI, отправку делает с debounce.
 */
function onVolumeSliderInput(e) {
    const v = parseInt(e.target.value);
    const mx = state.maxVolume || 30;
    const pct = mx > 0 ? Math.round(v / mx * 100) : 0;

    volumeDragValue = v;

    // Немедленно обновить числа
    e.target.style.setProperty('--vol-pct', pct + '%');
    document.getElementById('volValue').textContent = v;
    document.getElementById('volPercent').textContent = `${pct}%`;

    // Debounce отправки
    clearTimeout(volumeDebounceTimer);
    volumeDebounceTimer = setTimeout(() => {
        sendVolumeThrottled(v);
    }, VOLUME_DEBOUNCE_MS);
}

/**
 * Вызывается при отпускании слайдера (change event).
 * Гарантирует что финальное значение будет отправлено.
 */
function onVolumeSliderChange(e) {
    const v = parseInt(e.target.value);
    volumeDragging = false;
    volumeDragValue = null;

    // Отменить pending debounce, отправить сразу финальное
    clearTimeout(volumeDebounceTimer);
    sendVolumeThrottled(v);

    // Через небольшую задержку запросить актуальное состояние
    setTimeout(() => {
        if (!volumeDragging) {
            sendNoWait('getVolume');
        }
    }, 800);
}

function onVolumeSliderStart() {
    volumeDragging = true;
}

/**
 * Отправить громкость на сервер с минимальным интервалом.
 * Не ждём ответ — используем fire-and-forget.
 */
function sendVolumeThrottled(value) {
    const now = Date.now();
    const elapsed = now - volumeLastSentTime;

    if (elapsed >= VOLUME_SEND_MIN_MS) {
        volumeLastSentTime = now;
        state.volume = value;  // optimistic update
        sendNoWait('setVolume', { value });
    } else {
        // Запланировать отправку
        clearTimeout(volumeDebounceTimer);
        volumeDebounceTimer = setTimeout(() => {
            volumeLastSentTime = Date.now();
            state.volume = value;
            sendNoWait('setVolume', { value });
        }, VOLUME_SEND_MIN_MS - elapsed);
    }
}

/**
 * Для кнопок ±1, ±5, Mute — немедленная отправка.
 */
async function setVolumeImmediate(value) {
    value = Math.max(0, Math.min(state.maxVolume || 30, value));

    // Optimistic UI update
    state.volume = value;
    renderVolumeDisplay();

    try {
        await send('setVolumeImmediate', { value });
    } catch (err) {
        // Не показываем ошибку — UI уже обновлён,
        // при следующем state update подтянется
        console.warn('[Vol] Immediate failed:', err.message);
    }
}


// ═══════════════════════════════════════════════════
// EQ Sliders
// ═══════════════════════════════════════════════════

let eqDebounceTimers = {};
let lastBandCount = 0;

function renderEQSliders() {
    const container = document.getElementById('eqSliders');
    const bands = state.bands || [];

    const needsRebuild = container.children.length !== bands.length
                         || lastBandCount !== bands.length;

    if (needsRebuild) {
        lastBandCount = bands.length;
        editingBandIndex = null;
        container.innerHTML = bands.map((b, i) => buildBandHTML(b, i, false)).join('');
        bindBandEvents(container);
    } else {
        bands.forEach((b, i) => {
            if (editingBandIndex === i) return;

            const bandEl = container.querySelector(`.eq-band[data-index="${i}"]`);
            if (!bandEl) return;

            const slider = document.getElementById(`eqSlider${i}`);
            const label = document.getElementById(`gainLabel${i}`);
            const freqLabel = bandEl.querySelector('.freq-label');

            if (slider && !slider.matches(':active')) {
                slider.value = Math.round(b.gain * 2);
            }
            if (label) {
                label.textContent = formatGain(b.gain);
                label.className = 'gain-label' +
                    (b.gain > 0 ? ' positive' : b.gain < 0 ? ' negative' : '');
            }
            if (freqLabel) {
                freqLabel.textContent = formatFreq(b.frequency);
                freqLabel.title = `${b.frequency} Hz — click to edit`;
            }
        });
    }
}

function buildBandHTML(band, index, isEditing) {
    const freqStr = formatFreq(band.frequency);
    const editingClass = isEditing ? ' editing' : '';

    let freqArea;
    if (isEditing) {
        const suggested = getSuggestedFreqs(index, (state.bands || []).length);
        const presetBtns = suggested.map(f =>
            `<button class="freq-preset-btn" data-freq="${f}" title="${f} Hz">${formatFreq(f)}</button>`
        ).join('');

        freqArea = `
            <div class="freq-editor">
                <input type="number" id="freqInput${index}"
                       value="${band.frequency}" min="20" max="20000" step="1"
                       title="20 — 20000 Hz">
                <div class="freq-presets" data-index="${index}">${presetBtns}</div>
                <div class="freq-actions">
                    <button class="freq-ok" data-index="${index}" title="Apply">✓</button>
                    <button class="freq-cancel" data-index="${index}" title="Cancel">✕</button>
                </div>
                <span class="freq-hint">20 – 20k Hz</span>
            </div>
        `;
    } else {
        freqArea = `
            <span class="freq-label" data-index="${index}"
                  title="${band.frequency} Hz — click to edit">${freqStr}</span>
        `;
    }

    return `
        <div class="eq-band${editingClass}" data-index="${index}">
            <span class="band-index">#${index}</span>
            <span class="gain-label${band.gain > 0 ? ' positive' : band.gain < 0 ? ' negative' : ''}"
                  id="gainLabel${index}">${formatGain(band.gain)}</span>
            <input type="range" id="eqSlider${index}"
                   min="-6" max="6" step="1"
                   value="${Math.round(band.gain * 2)}" data-index="${index}">
            ${freqArea}
        </div>
    `;
}

function bindBandEvents(container) {
    container.querySelectorAll('input[type="range"]').forEach(slider => {
        slider.addEventListener('input', onEQSliderInput);
        slider.addEventListener('change', onEQSliderChange);
    });
    container.querySelectorAll('.freq-label').forEach(label => {
        label.addEventListener('click', onFreqLabelClick);
    });
    bindFreqEditorEvents(container);
}

function bindFreqEditorEvents(container) {
    container.querySelectorAll('.freq-ok').forEach(btn => btn.addEventListener('click', onFreqOk));
    container.querySelectorAll('.freq-cancel').forEach(btn => btn.addEventListener('click', onFreqCancel));
    container.querySelectorAll('.freq-preset-btn').forEach(btn => btn.addEventListener('click', onFreqPresetClick));
    container.querySelectorAll('.freq-editor input[type="number"]').forEach(input => {
        input.addEventListener('keydown', onFreqInputKeydown);
        setTimeout(() => input.focus(), 50);
    });
}

// ── Freq editing ─────────────────────────────────

function onFreqLabelClick(e) {
    startFreqEdit(parseInt(e.target.dataset.index));
}

function startFreqEdit(index) {
    editingBandIndex = index;
    rebuildSingleBand(index, true);
}

function stopFreqEdit() {
    const prev = editingBandIndex;
    editingBandIndex = null;
    if (prev !== null) rebuildSingleBand(prev, false);
}

function rebuildSingleBand(index, editing) {
    const container = document.getElementById('eqSliders');
    const bandEl = container.querySelector(`.eq-band[data-index="${index}"]`);
    if (!bandEl || !state.bands || index >= state.bands.length) return;

    const temp = document.createElement('div');
    temp.innerHTML = buildBandHTML(state.bands[index], index, editing);
    const newEl = temp.firstElementChild;
    bandEl.replaceWith(newEl);

    const slider = newEl.querySelector('input[type="range"]');
    if (slider) {
        slider.addEventListener('input', onEQSliderInput);
        slider.addEventListener('change', onEQSliderChange);
    }
    const freqLabel = newEl.querySelector('.freq-label');
    if (freqLabel) freqLabel.addEventListener('click', onFreqLabelClick);
    bindFreqEditorEvents(newEl);
}

function onFreqPresetClick(e) {
    const freq = parseInt(e.target.dataset.freq);
    const index = parseInt(e.target.closest('.freq-presets').dataset.index);
    const input = document.getElementById(`freqInput${index}`);
    if (input) { input.value = freq; input.focus(); }
}

async function onFreqOk(e) {
    const index = parseInt(e.target.dataset.index);
    const input = document.getElementById(`freqInput${index}`);
    if (!input) return;
    const freq = parseInt(input.value);
    if (isNaN(freq) || freq < 20 || freq > 20000) {
        toast('Frequency: 20–20000 Hz', 'error');
        input.focus();
        return;
    }
    const gain = (state.bands && state.bands[index]) ? state.bands[index].gain : 0;
    stopFreqEdit();
    try {
        await send('setBand', { index, frequency: freq, gain });
        toast(`Band ${index}: ${formatFreq(freq)}`, 'success');
    } catch (err) { toast(err.message, 'error'); }
}

function onFreqCancel() { stopFreqEdit(); }

function onFreqInputKeydown(e) {
    if (e.key === 'Enter') {
        e.preventDefault();
        const okBtn = e.target.closest('.freq-editor').querySelector('.freq-ok');
        if (okBtn) okBtn.click();
    } else if (e.key === 'Escape') {
        e.preventDefault();
        stopFreqEdit();
    }
}

// ── EQ Gain ──────────────────────────────────────

function onEQSliderInput(e) {
    const idx = parseInt(e.target.dataset.index);
    const gain = parseInt(e.target.value) / 2;
    const label = document.getElementById(`gainLabel${idx}`);
    if (label) {
        label.textContent = formatGain(gain);
        label.className = 'gain-label' + (gain > 0 ? ' positive' : gain < 0 ? ' negative' : '');
    }
    if (state.bands && state.bands[idx]) state.bands[idx].gain = gain;
    drawEQCanvas();
}

function onEQSliderChange(e) {
    const idx = parseInt(e.target.dataset.index);
    const gain = parseInt(e.target.value) / 2;
    clearTimeout(eqDebounceTimers[idx]);
    eqDebounceTimers[idx] = setTimeout(async () => {
        try {
            await send('setGain', { index: idx, gain });
        } catch (err) { toast(err.message, 'error'); }
    }, 100);
}


// ═══════════════════════════════════════════════════
// EQ Canvas
// ═══════════════════════════════════════════════════

function drawEQCanvas() {
    const canvas = document.getElementById('eqCanvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);

    const W = rect.width, H = rect.height;
    const bands = state.bands || [];
    if (!bands.length) return;

    const pad = { top: 20, bottom: 30, left: 44, right: 20 };
    const plotW = W - pad.left - pad.right;
    const plotH = H - pad.top - pad.bottom;
    const zeroY = pad.top + plotH / 2;

    ctx.fillStyle = '#222536';
    ctx.fillRect(0, 0, W, H);

    // Grid
    ctx.strokeStyle = '#333650';
    ctx.lineWidth = 0.5;
    for (let db = -3; db <= 3; db += 0.5) {
        const y = zeroY - (db / 3) * (plotH / 2);
        ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(W - pad.right, y); ctx.stroke();
    }
    ctx.strokeStyle = '#636e8a'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pad.left, zeroY); ctx.lineTo(W - pad.right, zeroY); ctx.stroke();

    // Labels
    ctx.fillStyle = '#8b8fa5'; ctx.font = '11px monospace'; ctx.textAlign = 'right';
    for (let db = -3; db <= 3; db += 1) {
        ctx.fillText(`${db >= 0 ? '+' : ''}${db}`, pad.left - 6, zeroY - (db / 3) * (plotH / 2) + 4);
    }

    const points = bands.map((b, i) => ({
        x: pad.left + (i / (bands.length - 1)) * plotW,
        y: zeroY - (b.gain / 3) * (plotH / 2),
        gain: b.gain, freq: b.frequency, index: i,
    }));

    function drawCurve(start) {
        for (let i = 0; i < points.length; i++) {
            if (i === 0) { start(points[0]); continue; }
            const p0 = points[i-1], p1 = points[i];
            const pp = points[Math.max(0, i-2)], pn = points[Math.min(points.length-1, i+1)];
            ctx.bezierCurveTo(
                p0.x + (p1.x - pp.x)/4, p0.y + (p1.y - pp.y)/4,
                p1.x - (pn.x - p0.x)/4, p1.y - (pn.y - p0.y)/4,
                p1.x, p1.y
            );
        }
    }

    // Fill
    const grad = ctx.createLinearGradient(0, pad.top, 0, H - pad.bottom);
    grad.addColorStop(0, 'rgba(108,92,231,0.3)');
    grad.addColorStop(0.5, 'rgba(108,92,231,0.05)');
    grad.addColorStop(1, 'rgba(225,112,85,0.3)');
    ctx.beginPath(); ctx.moveTo(points[0].x, zeroY);
    drawCurve(p => ctx.lineTo(p.x, p.y));
    ctx.lineTo(points[points.length-1].x, zeroY); ctx.closePath();
    ctx.fillStyle = grad; ctx.fill();

    // Line
    ctx.beginPath();
    drawCurve(p => ctx.moveTo(p.x, p.y));
    ctx.strokeStyle = '#6c5ce7'; ctx.lineWidth = 2.5; ctx.stroke();

    // Dots
    points.forEach(p => {
        const ed = editingBandIndex === p.index;
        ctx.beginPath(); ctx.arc(p.x, p.y, ed ? 7 : 5, 0, Math.PI * 2);
        ctx.fillStyle = ed ? '#fdcb6e' : p.gain > 0 ? '#6c5ce7' : p.gain < 0 ? '#e17055' : '#636e8a';
        ctx.fill(); ctx.strokeStyle = ed ? '#fdcb6e' : '#fff'; ctx.lineWidth = ed ? 3 : 2; ctx.stroke();
        ctx.fillStyle = ed ? '#fdcb6e' : '#8b8fa5';
        ctx.font = ed ? 'bold 11px sans-serif' : '10px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(formatFreq(p.freq), p.x, H - pad.bottom + 16);
        if (ed) ctx.fillText(formatGain(p.gain), p.x, p.y - 12);
    });
}


// ═══════════════════════════════════════════════════
// Presets & Advanced
// ═══════════════════════════════════════════════════

function renderPresets() {
    const grid = document.getElementById('presetsGrid');
    if (!presets || !presets.length) {
        grid.innerHTML = '<p style="color:var(--text-dim)">No presets</p>';
        return;
    }
    grid.innerHTML = presets.map(p => {
        const u = !p.builtin;
        return `
            <div class="preset-card" onclick="applyPreset('${p.key}')">
                <div class="preset-name">${esc(p.name)}</div>
                <div class="preset-desc">${esc(p.description||'')}</div>
                <span class="preset-badge ${u?'user':''}">${u?'User':'Built-in'}</span>
                ${u?`<button class="delete-btn show" onclick="event.stopPropagation();deletePreset('${esc(p.name)}')">✕</button>`:''}
            </div>`;
    }).join('');
}

function renderAdvanced() {
    document.getElementById('lowCutFreq').value = state.lowCutFreq || 60;
    const sm = {'6dB':'6','12dB':'12','18dB':'18','24dB':'24'};
    document.getElementById('lowCutSlope').value = sm[state.lowCutSlope]||'24';

    document.querySelectorAll('.space-btn').forEach(b =>
        b.classList.toggle('active', parseInt(b.dataset.space)===(state.acousticSpace||0)));

    document.getElementById('desktopToggle').checked = !!state.desktopMode;
    document.getElementById('desktopLabel').textContent = state.desktopMode ? 'On' : 'Off';
    document.getElementById('promptToggle').checked = state.promptTone !== false;
    document.getElementById('promptLabel').textContent = state.promptTone !== false ? 'On' : 'Off';

    const sg = document.getElementById('groupSpeaker');
    if (state.supportsActiveSpeaker) {
        sg.classList.remove('hidden');
        document.querySelectorAll('.speaker-btn').forEach(b =>
            b.classList.toggle('active', b.dataset.side.toLowerCase()===(state.activeSpeaker||'').toLowerCase()));
    } else sg.classList.add('hidden');

    const lg = document.getElementById('groupLdac');
    state.supportsLdac ? lg.classList.remove('hidden') : lg.classList.add('hidden');

    ['groupLowCut','groupSpace','groupDesktop'].forEach(id => {
        const g = document.getElementById(id);
        state.supportsAdvancedAudio ? g.classList.remove('hidden') : g.classList.add('hidden');
    });
}


// ═══════════════════════════════════════════════════
// Actions
// ═══════════════════════════════════════════════════

async function connectBLE(address) {
    const btn = document.getElementById('btnConnect');
    try {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> Connecting...';
        await send('connect', { address });
        toast('Connected!', 'success');
    } catch (err) { toast(err.message, 'error'); }
    finally { btn.disabled = false; btn.textContent = 'Connect'; }
}

async function scanBLE() {
    const btn = document.getElementById('btnScan');
    const div = document.getElementById('scanResults');
    try {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> Scanning...';
        div.classList.remove('hidden');
        div.innerHTML = '<p style="color:var(--text-dim)">Scanning...</p>';
        const devices = await send('scan');
        if (!devices||!devices.length) { div.innerHTML = '<p style="color:var(--text-dim)">No devices</p>'; return; }
        div.innerHTML = devices.map(d => `
            <div class="scan-item" onclick="selectDevice('${d.address}','${esc(d.name)}')">
                <div><span class="name">${esc(d.name)}</span> <span class="address">${d.address}</span></div>
                <span class="rssi">${d.rssi}dBm</span>
            </div>`).join('');
    } catch (err) { div.innerHTML = `<p style="color:var(--red)">${err.message}</p>`; }
    finally { btn.disabled = false; btn.textContent = 'Scan'; }
}

function selectDevice(addr, name) {
    document.getElementById('bleAddress').value = addr;
    toast(`Selected: ${name}`, 'info');
}

async function applyPreset(key) {
    try { await send('applyPreset',{name:key}); toast(`"${key}" applied`,'success'); }
    catch(e){ toast(e.message,'error'); }
}

async function savePreset() {
    const name = document.getElementById('presetName').value.trim();
    if (!name) { toast('Enter name','error'); return; }
    try {
        await send('savePreset',{name, description:'User preset'});
        document.getElementById('presetName').value = '';
        toast(`"${name}" saved`,'success');
    } catch(e){ toast(e.message,'error'); }
}

async function deletePreset(name) {
    if (!confirm(`Delete "${name}"?`)) return;
    try { await send('deletePreset',{name}); toast(`Deleted "${name}"`,'success'); }
    catch(e){ toast(e.message,'error'); }
}


// ═══════════════════════════════════════════════════
// Utilities
// ═══════════════════════════════════════════════════

function toast(msg, type='info') {
    const c = document.getElementById('toastContainer');
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = msg;
    c.appendChild(el);
    setTimeout(() => el.remove(), 3000);
}

function esc(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}
// Alias for templates that use escapeHtml
const escapeHtml = esc;


// ═══════════════════════════════════════════════════
// Click-outside for freq editor
// ═══════════════════════════════════════════════════

document.addEventListener('click', (e) => {
    if (editingBandIndex === null) return;
    const band = document.querySelector(`.eq-band[data-index="${editingBandIndex}"]`);
    if (band && band.contains(e.target)) return;
    if (e.target.classList.contains('freq-label')) return;
    stopFreqEdit();
});


// ═══════════════════════════════════════════════════
// Event Bindings
// ═══════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {

    // Tabs
    document.querySelectorAll('.tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            tab.classList.add('active');
            document.getElementById(`tab-${tab.dataset.tab}`).classList.add('active');
            if (tab.dataset.tab === 'eq') setTimeout(drawEQCanvas, 50);
            stopFreqEdit();
        });
    });

    // Connect
    document.getElementById('btnConnect').addEventListener('click', () => {
        const addr = document.getElementById('bleAddress').value.trim();
        addr ? connectBLE(addr) : toast('Enter address or scan', 'error');
    });
    document.getElementById('btnScan').addEventListener('click', scanBLE);
    document.getElementById('bleAddress').addEventListener('keydown', e => {
        if (e.key === 'Enter') { const a = e.target.value.trim(); if (a) connectBLE(a); }
    });

    // ── Volume — переработанные обработчики ──────────
    const volSlider = document.getElementById('volSlider');

    // mousedown / touchstart — начало drag
    volSlider.addEventListener('mousedown', onVolumeSliderStart);
    volSlider.addEventListener('touchstart', onVolumeSliderStart, { passive: true });

    // input — каждое движение
    volSlider.addEventListener('input', onVolumeSliderInput);

    // change — отпускание
    volSlider.addEventListener('change', onVolumeSliderChange);

    // mouseup / touchend на document — fallback для случаев
    // когда курсор ушёл за пределы слайдера
    document.addEventListener('mouseup', () => {
        if (volumeDragging) {
            volumeDragging = false;
            if (volumeDragValue !== null) {
                clearTimeout(volumeDebounceTimer);
                sendVolumeThrottled(volumeDragValue);
                volumeDragValue = null;
                setTimeout(() => { if (!volumeDragging) sendNoWait('getVolume'); }, 800);
            }
        }
    });

    // Кнопки ±
    document.getElementById('btnVolDown5').addEventListener('click', () =>
        setVolumeImmediate(Math.max(0, (state.volume||0) - 5)));
    document.getElementById('btnVolDown').addEventListener('click', () =>
        setVolumeImmediate(Math.max(0, (state.volume||0) - 1)));
    document.getElementById('btnMute').addEventListener('click', () =>
        setVolumeImmediate(0));
    document.getElementById('btnVolUp').addEventListener('click', () =>
        setVolumeImmediate(Math.min(state.maxVolume||30, (state.volume||0) + 1)));
    document.getElementById('btnVolUp5').addEventListener('click', () =>
        setVolumeImmediate(Math.min(state.maxVolume||30, (state.volume||0) + 5)));

    // EQ toolbar
    document.getElementById('btnFlat').addEventListener('click', async () => {
        stopFreqEdit();
        try { await send('flatEQ'); toast('EQ flat','success'); } catch(e){ toast(e.message,'error'); }
    });
    document.getElementById('btnResetEQ').addEventListener('click', async () => {
        stopFreqEdit();
        try { await send('resetEQ'); toast('EQ reset','success'); } catch(e){ toast(e.message,'error'); }
    });
    document.getElementById('btnRefreshEQ').addEventListener('click', async () => {
        stopFreqEdit();
        try { await send('queryEQ'); toast('EQ refreshed','info'); } catch(e){ toast(e.message,'error'); }
    });

    // Presets
    document.getElementById('btnSavePreset').addEventListener('click', savePreset);
    document.getElementById('presetName').addEventListener('keydown', e => { if(e.key==='Enter') savePreset(); });

    // Advanced
    document.getElementById('btnSetLowCut').addEventListener('click', async () => {
        const f=parseInt(document.getElementById('lowCutFreq').value);
        const s=parseInt(document.getElementById('lowCutSlope').value);
        try { await send('setLowCut',{frequency:f,slope:s}); toast(`Low cut: ${f}Hz ${s}dB/oct`,'success'); }
        catch(e){ toast(e.message,'error'); }
    });

    document.querySelectorAll('.space-btn').forEach(b => b.addEventListener('click', async () => {
        try { await send('setAcousticSpace',{value:parseInt(b.dataset.space)}); toast(`Space: -${b.dataset.space}dB`,'success'); }
        catch(e){ toast(e.message,'error'); }
    }));

    document.getElementById('desktopToggle').addEventListener('change', async e => {
        try { await send('setDesktopMode',{enabled:e.target.checked}); toast(`Desktop: ${e.target.checked?'On':'Off'}`,'success'); }
        catch(er){ e.target.checked=!e.target.checked; toast(er.message,'error'); }
    });

    document.querySelectorAll('.speaker-btn').forEach(b => b.addEventListener('click', async () => {
        try { await send('setActiveSpeaker',{side:b.dataset.side}); toast(`Speaker: ${b.dataset.side}`,'success'); }
        catch(e){ toast(e.message,'error'); }
    }));

    document.querySelectorAll('.ldac-btn').forEach(b => b.addEventListener('click', async () => {
        try { await send('setLdac',{mode:b.dataset.mode}); toast(`LDAC: ${b.dataset.mode}`,'success'); }
        catch(e){ toast(e.message,'error'); }
    }));

    document.getElementById('promptToggle').addEventListener('change', async e => {
        try { await send('setPromptTone',{enabled:e.target.checked}); toast(`Prompt: ${e.target.checked?'On':'Off'}`,'success'); }
        catch(er){ e.target.checked=!e.target.checked; toast(er.message,'error'); }
    });

    // System
    document.getElementById('btnShutdown').addEventListener('click', async () => {
        if (!confirm('Power off?')) return;
        try { await send('shutdown'); toast('Shutdown sent','success'); }
        catch(e){ toast(e.message,'error'); }
    });

    document.getElementById('btnDisconnect').addEventListener('click', async () => {
        try { await send('disconnect'); state={connected:false,bands:[]}; renderAll(); toast('Disconnected','info'); }
        catch(e){ toast(e.message,'error'); }
    });

    document.getElementById('btnRefreshInfo').addEventListener('click', async () => {
        try { await send('refreshState'); toast('Refreshed','info'); }
        catch(e){ toast(e.message,'error'); }
    });

    // Resize
    window.addEventListener('resize', () => {
        if (document.getElementById('tab-eq').classList.contains('active')) drawEQCanvas();
    });

    // GO
    wsConnect();
});