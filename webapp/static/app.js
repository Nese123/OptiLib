/* ═══════════════════════════════════════════════════════════════
   Drug Library Optimization — Frontend Logic
   ═══════════════════════════════════════════════════════════════ */

// ─── State ───
let currentStep = 1;
let uploadedChemblIds = [];
let uploadedMatchedCount = 0;
let uploadedFilesData = [];
try {
    const stored = sessionStorage.getItem('uploadedFilesData');
    if (stored) {
        uploadedFilesData = JSON.parse(stored);
    }
} catch (e) {
    console.error('Failed to restore uploaded files', e);
}
let pipelinePollTimer = null;
let optPollTimer = null;

// ─── DOM Elements ───
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// Restore state on page load
window.addEventListener('DOMContentLoaded', async () => {
    // Restore UI if we have saved files
    if (uploadedFilesData.length > 0) {
        renderFiles();
    }

    try {
        const pipeRes = await fetch('/api/pipeline-status');
        const pipeState = await pipeRes.json();

        const optRes = await fetch('/api/status');
        const optState = await optRes.json();

        const datasetRes = await fetch('/api/dataset-info');
        const datasetState = await datasetRes.json();

        if (optState.status === 'complete') {
            goToStep(4);
            await loadDatasetInfo();
            await loadResults();
        } else if (optState.status === 'running') {
            goToStep(3);
            await loadDatasetInfo();
            runOptBtn.disabled = true;
            runOptBtn.style.display = 'none';
            $('#stopOptBtn').style.display = 'inline-flex';
            $('#stopOptBtn').disabled = false;
            optError.style.display = 'none';
            optProgress.style.display = 'block';
            startOptPolling(optState.max_gen);
        } else if (pipeState.status === 'complete' && datasetState.ready) {
            const savedStep = sessionStorage.getItem('currentStep');
            if (savedStep === '2') {
                goToStep(2);
                updatePipelineUI(pipeState);
            } else {
                goToStep(3);
                await loadDatasetInfo();
            }
        } else if (pipeState.status === 'running') {
            goToStep(2);
            startPipelinePolling();
        } else if (pipeState.status === 'error') {
            goToStep(2);
            updatePipelineUI(pipeState);
        }
    } catch (err) {
        console.error('Failed to restore backend state on load:', err);
    }
});

// ═══════════════════════════════════════════════════════════════
//  STEP NAVIGATION
// ═══════════════════════════════════════════════════════════════

function goToStep(step) {
    currentStep = step;
    sessionStorage.setItem('currentStep', step);

    // Update step sections
    $$('.step-section').forEach((s) => s.classList.remove('active'));
    $(`#step-${step}`).classList.add('active');

    // Update step indicator
    $$('.step-dot').forEach((dot) => {
        const dotStep = parseInt(dot.dataset.step);
        dot.classList.remove('active', 'completed');
        if (dotStep === step) dot.classList.add('active');
        else if (dotStep < step) dot.classList.add('completed');
    });

    // Update connecting lines
    for (let i = 1; i <= 3; i++) {
        const line = $(`#line-${i}-${i + 1}`);
        if (line) {
            line.classList.toggle('completed', i < step);
        }
    }

    // Scroll to top so the new step is visible from the beginning
    window.scrollTo(0, 0);
}

// ═══════════════════════════════════════════════════════════════
//  STEP 1: FILE UPLOAD
// ═══════════════════════════════════════════════════════════════

const dropZone = $('#dropZone');
const fileInput = $('#fileInput');
const fileInfo = $('#fileInfo');
const validationSummary = $('#validationSummary');
const buildMatrixBtn = $('#buildMatrixBtn');
const uploadError = $('#uploadError');
const thresholdControl = $('#thresholdControl');
const selectivityThreshold = $('#selectivityThreshold');
const thresholdValue = $('#thresholdValue');
const removeAllBtnContainer = $('#removeAllBtnContainer');
const removeAllBtn = $('#removeAllBtn');
const removeAllConfirm = $('#removeAllConfirm');
const removeAllYesBtn = $('#removeAllYesBtn');
const removeAllNoBtn = $('#removeAllNoBtn');

removeAllBtn.addEventListener('click', () => {
    removeAllBtn.style.display = 'none';
    removeAllConfirm.style.display = 'flex';
});

removeAllYesBtn.addEventListener('click', () => {
    uploadedFilesData = [];
    renderFiles();
});

removeAllNoBtn.addEventListener('click', () => {
    removeAllConfirm.style.display = 'none';
    removeAllBtn.style.display = 'inline-block';
});

// Drag & drop visual
dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('drag-over');
});

dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('drag-over');
});

dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    if (e.dataTransfer.files.length) {
        fileInput.files = e.dataTransfer.files;
        handleFileUpload(Array.from(e.dataTransfer.files));
    }
});

fileInput.addEventListener('change', () => {
    if (fileInput.files.length) {
        handleFileUpload(Array.from(fileInput.files));
    }
});

// Threshold slider
selectivityThreshold.addEventListener('input', () => {
    thresholdValue.value = parseFloat(selectivityThreshold.value).toFixed(1);
});

thresholdValue.addEventListener('input', () => {
    let val = parseFloat(thresholdValue.value);
    if (!isNaN(val)) {
        selectivityThreshold.value = val;
    }
});

thresholdValue.addEventListener('change', () => {
    let val = parseFloat(thresholdValue.value);
    if (!isNaN(val)) {
        val = Math.max(0, Math.min(2, val));
        thresholdValue.value = val.toFixed(1);
        selectivityThreshold.value = val.toFixed(1);
    }
});

async function handleFileUpload(files) {
    // Reset UI before uploading new files
    validationSummary.style.display = 'none';
    uploadError.style.display = 'none';
    buildMatrixBtn.disabled = true;
    thresholdControl.style.display = 'none';

    for (let f of files) {
        // Skip if a file with this name is already uploaded
        if (uploadedFilesData.some(d => d.name === f.name)) continue;

        const formData = new FormData();
        formData.append('files[]', f);

        try {
            const res = await fetch('/api/upload-targets', { method: 'POST', body: formData });
            const data = await res.json();

            if (res.ok) {
                uploadedFilesData.push({ name: f.name, data: data });
            } else {
                showError(uploadError, data.error || `Upload failed for ${f.name}`);
            }
        } catch (err) {
            showError(uploadError, `Network error: ${err.message}`);
        }
    }

    renderFiles();
}

function renderFiles() {
    fileInfo.innerHTML = '';

    // Recompute total cumulative targets
    let allMatched = [];
    let allUnmatched = [];

    uploadedFilesData.forEach(fileData => {
        const d = fileData.data;
        allMatched.push(...(d.matched || []));
        allUnmatched.push(...(d.unmatched || []));

        // Render box
        const box = document.createElement('div');
        box.className = 'file-selected';
        box.style.marginTop = '0';
        box.style.display = 'flex';
        box.style.justifyContent = 'space-between';
        box.style.alignItems = 'center';

        const leftSide = document.createElement('div');
        leftSide.style.display = 'flex';
        leftSide.style.alignItems = 'center';
        leftSide.style.gap = '0.75rem';

        const iconSpan = document.createElement('span');
        iconSpan.textContent = '📎';
        const textSpan = document.createElement('span');
        textSpan.textContent = fileData.name;

        leftSide.appendChild(iconSpan);
        leftSide.appendChild(textSpan);

        const rightSide = document.createElement('div');
        const deleteBtn = document.createElement('span');
        deleteBtn.textContent = '❌';
        deleteBtn.style.cursor = 'pointer';
        deleteBtn.style.color = '#ff4a4a';
        deleteBtn.onclick = () => {
            box.innerHTML = '';

            const msg = document.createElement('span');
            msg.textContent = `Are you sure you want to delete ${fileData.name}?`;
            msg.style.color = '#ff4a4a';
            msg.style.fontSize = '0.9rem';

            const btnContainer = document.createElement('div');
            btnContainer.style.display = 'flex';
            btnContainer.style.gap = '8px';

            const yesBtn = document.createElement('button');
            yesBtn.className = 'btn btn-primary';
            yesBtn.style.padding = '0.25rem 0.75rem';
            yesBtn.style.fontSize = '0.85rem';
            yesBtn.style.minWidth = '50px';
            yesBtn.textContent = 'Yes';
            yesBtn.onclick = () => {
                uploadedFilesData = uploadedFilesData.filter(d => d.name !== fileData.name);
                renderFiles();
            };

            const noBtn = document.createElement('button');
            noBtn.className = 'btn btn-secondary';
            noBtn.style.padding = '0.25rem 0.75rem';
            noBtn.style.fontSize = '0.85rem';
            noBtn.style.minWidth = '50px';
            noBtn.textContent = 'No';
            noBtn.onclick = () => renderFiles();

            btnContainer.appendChild(yesBtn);
            btnContainer.appendChild(noBtn);

            box.appendChild(msg);
            box.appendChild(btnContainer);
        };
        rightSide.appendChild(deleteBtn);

        box.appendChild(leftSide);
        box.appendChild(rightSide);
        fileInfo.appendChild(box);
    });

    if (uploadedFilesData.length > 0) {
        fileInfo.style.display = 'flex';
        removeAllBtnContainer.style.display = 'block';
        removeAllBtn.style.display = 'inline-block';
        removeAllConfirm.style.display = 'none';
    } else {
        fileInfo.style.display = 'none';
        removeAllBtnContainer.style.display = 'none';
        validationSummary.style.display = 'none';
        buildMatrixBtn.disabled = true;
        thresholdControl.style.display = 'none';
        uploadedChemblIds = [];
        sessionStorage.removeItem('uploadedFilesData');
        return; // Nothing more to do
    }

    // Save to session storage
    sessionStorage.setItem('uploadedFilesData', JSON.stringify(uploadedFilesData));

    const uniqueMatched = [...new Set(allMatched)];
    const uniqueUnmatched = [...new Set(allUnmatched)];

    let currentChemblIds = [];
    uniqueMatched.forEach(matchStr => {
        const match = matchStr.match(/->\s*([^\s(]+)/);
        if (match && match[1]) {
            currentChemblIds.push(match[1]);
        }
    });
    uploadedChemblIds = [...new Set(currentChemblIds)];

    uploadedMatchedCount = uniqueMatched.length;

    // Show validation summary
    validationSummary.style.display = 'block';

    // Matched
    $('#matchedCount').textContent = `${uniqueMatched.length} targets matched in ChEMBL`;

    const matchedListEl = $('#matchedList');
    matchedListEl.innerHTML = '';
    uniqueMatched.forEach((matchStr) => {
        const item = document.createElement('div');
        item.className = 'target-list-item';

        const textSpan = document.createElement('span');
        textSpan.textContent = matchStr;

        const delBtn = document.createElement('span');
        delBtn.textContent = '❌';
        delBtn.className = 'target-list-delete';
        delBtn.title = 'Remove target';
        delBtn.onclick = () => {
            item.innerHTML = '';

            const targetName = matchStr.split(' ->')[0];
            const msg = document.createElement('span');
            msg.textContent = `Are you sure you want to remove the target ${targetName}?`;
            msg.style.color = '#ff4a4a';

            const btnContainer = document.createElement('div');
            btnContainer.style.display = 'flex';
            btnContainer.style.gap = '8px';

            const yesBtn = document.createElement('button');
            yesBtn.className = 'btn btn-primary';
            yesBtn.style.padding = '0.15rem 0.5rem';
            yesBtn.style.fontSize = '0.75rem';
            yesBtn.style.minWidth = '40px';
            yesBtn.textContent = 'Yes';
            yesBtn.onclick = () => {
                uploadedFilesData.forEach(fileData => {
                    if (fileData.data.matched) {
                        fileData.data.matched = fileData.data.matched.filter(m => m !== matchStr);
                    }
                });
                renderFiles();
            };

            const noBtn = document.createElement('button');
            noBtn.className = 'btn btn-secondary';
            noBtn.style.padding = '0.15rem 0.5rem';
            noBtn.style.fontSize = '0.75rem';
            noBtn.style.minWidth = '40px';
            noBtn.textContent = 'No';
            noBtn.onclick = () => renderFiles();

            btnContainer.appendChild(yesBtn);
            btnContainer.appendChild(noBtn);

            item.appendChild(msg);
            item.appendChild(btnContainer);
        };

        item.appendChild(textSpan);
        item.appendChild(delBtn);
        matchedListEl.appendChild(item);
    });

    // Unmatched
    if (uniqueUnmatched.length > 0) {
        $('#unmatchedRow').style.display = 'flex';
        $('#unmatchedList').style.display = 'block';
        $('#unmatchedCount').textContent = `${uniqueUnmatched.length} targets not found`;
        const unmatchedListEl = $('#unmatchedList');
        unmatchedListEl.innerHTML = '';
        uniqueUnmatched.forEach(name => {
            const row = document.createElement('div');
            row.textContent = name;
            unmatchedListEl.appendChild(row);
        });
    } else {
        $('#unmatchedRow').style.display = 'none';
        $('#unmatchedList').style.display = 'none';
    }

    if (uniqueMatched.length > 0) {
        buildMatrixBtn.disabled = false;
        thresholdControl.style.display = 'block';
    } else {
        buildMatrixBtn.disabled = true;
        thresholdControl.style.display = 'none';
    }
}

// Build Matrix button
buildMatrixBtn.addEventListener('click', async () => {
    buildMatrixBtn.disabled = true;

    const removeTargets = document.getElementById('removeTargets').checked;

    const body = {
        chembl_ids: uploadedChemblIds,
        selectivity_threshold: parseFloat(selectivityThreshold.value),
        remove_targets: removeTargets,
        matched_count: uploadedMatchedCount
    };

    try {
        const res = await fetch('/api/build-matrix', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();

        if (!res.ok) {
            showError(uploadError, data.error || 'Failed to start pipeline');
            buildMatrixBtn.disabled = false;
            return;
        }

        // Move to step 2 and start polling
        goToStep(2);
        startPipelinePolling();

    } catch (err) {
        showError(uploadError, `Network error: ${err.message}`);
        buildMatrixBtn.disabled = false;
    }
});


// ═══════════════════════════════════════════════════════════════
//  STEP 2: PIPELINE PROGRESS
// ═══════════════════════════════════════════════════════════════

function startPipelinePolling() {
    // Reset pipeline UI
    resetPipelineUI();

    pipelinePollTimer = setInterval(async () => {
        try {
            const res = await fetch('/api/pipeline-status');
            const data = await res.json();
            updatePipelineUI(data);

            if (data.status === 'complete' || data.status === 'error') {
                clearInterval(pipelinePollTimer);
                pipelinePollTimer = null;
            }
        } catch (err) {
            // Silent retry
        }
    }, 2000);
}

function resetPipelineUI() {
    const steps = $$('.pipeline-step');
    steps.forEach((s) => {
        s.classList.remove('active', 'completed', 'error');
        s.querySelector('.step-icon').textContent = '⬜';
    });
    $('#pipelineError').style.display = 'none';
    $('#goToConfigBtn').disabled = true;

    // Clear detail texts
    $$('[data-detail]').forEach((d) => d.textContent = '');
}

function updatePipelineUI(data) {
    const currentPipeStep = data.current_step;
    const steps = $$('.pipeline-step');

    steps.forEach((s) => {
        const pStep = parseInt(s.dataset.pipeline);
        const icon = s.querySelector('.step-icon');
        const detailEl = s.querySelector('.step-detail');

        s.classList.remove('active', 'completed', 'error');

        if (pStep < currentPipeStep) {
            s.classList.add('completed');
            icon.textContent = '✅';
            if (data.step_summaries && data.step_summaries[pStep]) {
                detailEl.textContent = data.step_summaries[pStep];
            }

        } else if (pStep === currentPipeStep) {
            if (data.status === 'error') {
                s.classList.add('error');
                icon.textContent = '❌';
            } else if (data.status === 'complete') {
                s.classList.add('completed');
                icon.textContent = '✅';
            } else {
                s.classList.add('active');
                icon.innerHTML = '<span class="spinner"></span>';
            }
            if (data.detail) {
                detailEl.textContent = data.detail;
            }
        } else {
            icon.textContent = '⬜';
        }
    });

    if (data.status === 'complete') {
        // Mark all steps as completed
        steps.forEach((s) => {
            s.classList.remove('active');
            s.classList.add('completed');
            s.querySelector('.step-icon').textContent = '✅';
            const pStep = parseInt(s.dataset.pipeline);
            if (data.step_summaries && data.step_summaries[pStep]) {
                s.querySelector('.step-detail').textContent = data.step_summaries[pStep];
            }
        });

        // Update the last step detail with final info
        const details = $$('[data-detail]');
        const lastDetail = details[details.length - 1];
        if (lastDetail && data.detail) lastDetail.textContent = data.detail;

        $('#goToConfigBtn').disabled = false;
    }

    if (data.status === 'error') {
        showError($('#pipelineError'), data.error || 'Pipeline failed');
    }
}

// Go to Config button
$('#goToConfigBtn').addEventListener('click', async () => {
    goToStep(3);
    await loadDatasetInfo();
});

// Back to Uploads button
$('#backToStep1From2Btn').addEventListener('click', async () => {
    try {
        await fetch('/api/reset', { method: 'POST' });
    } catch (err) {
        console.error('Failed to reset backend state', err);
    }

    if (pipelinePollTimer) {
        clearInterval(pipelinePollTimer);
        pipelinePollTimer = null;
    }

    buildMatrixBtn.disabled = false;
    goToStep(1);
});

// ═══════════════════════════════════════════════════════════════
//  STEP 3: CONFIGURATION & RUN
// ═══════════════════════════════════════════════════════════════

const weightMean = $('#weightMean');
const weightMeanValue = $('#weightMeanValue');
const weightMin = $('#weightMin');
const weightMinValue = $('#weightMinValue');
const allowedMiss = $('#allowedMiss');
const allowedMissValue = $('#allowedMissValue');
const runOptBtn = $('#runOptBtn');
const optProgress = $('#optProgress');
const optError = $('#optError');

let lastRenderedGen = -1;

function initHistoryChart() {
    $('#historyCard').style.display = 'block';
    lastRenderedGen = -1;

    const layout = {
        margin: { t: 20, r: 80, l: 60, b: 80 },
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        font: { color: '#9898b8', family: 'Inter, sans-serif' },
        xaxis: { title: 'Generation', gridcolor: 'rgba(255,255,255,0.05)', automargin: true },
        yaxis: { title: 'Best Selectivity Score', gridcolor: 'rgba(255,255,255,0.05)', color: '#12f3b9', automargin: true },
        yaxis2: { title: 'Lowest Cost (USD)', overlaying: 'y', side: 'right', color: '#9d7cff', gridcolor: 'rgba(0,0,0,0)', automargin: true },
        showlegend: true,
        legend: { x: 0, y: 1.1, orientation: 'h' }
    };

    const traces = [
        { x: [], y: [], name: 'Selectivity', mode: 'lines+markers', line: { color: '#12f3b9' }, marker: { size: 4 } },
        { x: [], y: [], name: 'Cost', mode: 'lines+markers', line: { color: '#9d7cff' }, yaxis: 'y2', marker: { size: 4 } }
    ];

    Plotly.newPlot('historyChart', traces, layout, { responsive: true, displayModeBar: false });
}

function updateHistoryChart(history) {
    if (!history || history.length === 0) return;

    const x = [];
    const ySel = [];
    const yCost = [];

    history.forEach(h => {
        if (h.generation > lastRenderedGen) {
            x.push(h.generation);
            ySel.push(h.best_selectivity);
            yCost.push(h.best_cost);
        }
    });

    if (x.length > 0) {
        Plotly.extendTraces('historyChart', {
            x: [x, x],
            y: [ySel, yCost]
        }, [0, 1]);
        lastRenderedGen = x[x.length - 1];
    }
}

// Slider displays
weightMean.addEventListener('input', () => {
    let val = parseFloat(weightMean.value);
    weightMeanValue.value = val.toFixed(1);
    weightMin.value = (1 - val).toFixed(1);
    weightMinValue.value = (1 - val).toFixed(1);
});

weightMeanValue.addEventListener('input', () => {
    let val = parseFloat(weightMeanValue.value);
    if (!isNaN(val)) {
        weightMean.value = val.toFixed(1);
        weightMin.value = (1 - val).toFixed(1);
        weightMinValue.value = (1 - val).toFixed(1);
    }
});

weightMeanValue.addEventListener('change', () => {
    let val = parseFloat(weightMeanValue.value);
    if (!isNaN(val)) {
        val = Math.max(0, Math.min(1, val));
        weightMeanValue.value = val.toFixed(1);
        weightMean.value = val.toFixed(1);
        weightMin.value = (1 - val).toFixed(1);
        weightMinValue.value = (1 - val).toFixed(1);
    }
});

weightMin.addEventListener('input', () => {
    let val = parseFloat(weightMin.value);
    weightMinValue.value = val.toFixed(1);
    weightMean.value = (1 - val).toFixed(1);
    weightMeanValue.value = (1 - val).toFixed(1);
});

weightMinValue.addEventListener('input', () => {
    let val = parseFloat(weightMinValue.value);
    if (!isNaN(val)) {
        weightMin.value = val.toFixed(1);
        weightMean.value = (1 - val).toFixed(1);
        weightMeanValue.value = (1 - val).toFixed(1);
    }
});

weightMinValue.addEventListener('change', () => {
    let val = parseFloat(weightMinValue.value);
    if (!isNaN(val)) {
        val = Math.max(0, Math.min(1, val));
        weightMinValue.value = val.toFixed(1);
        weightMin.value = val.toFixed(1);
        weightMean.value = (1 - val).toFixed(1);
        weightMeanValue.value = (1 - val).toFixed(1);
    }
});

allowedMiss.addEventListener('input', () => {
    allowedMissValue.value = parseInt(allowedMiss.value);
});

allowedMissValue.addEventListener('input', () => {
    let val = Math.round(parseFloat(allowedMissValue.value));
    if (!isNaN(val)) {
        allowedMiss.value = val;
    }
});

allowedMissValue.addEventListener('change', () => {
    let val = Math.round(parseFloat(allowedMissValue.value));
    if (!isNaN(val)) {
        val = Math.max(0, Math.min(20, val));
        allowedMissValue.value = val;
        allowedMiss.value = val;
    }
});

async function loadDatasetInfo() {
    try {
        const res = await fetch('/api/dataset-info');
        const data = await res.json();

        $('#statDrugs').textContent = data.num_drugs.toLocaleString();
        $('#statTargets').textContent = data.num_targets.toLocaleString();
        $('#statCost').textContent = `$${data.total_cost.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
    } catch (err) {
        // Silent
    }
}

// Stop Optimization
$('#stopOptBtn').addEventListener('click', async () => {
    try {
        $('#stopOptBtn').disabled = true;
        await fetch('/api/stop-opt', { method: 'POST' });
    } catch (err) {
        console.error("Failed to request stop", err);
    }
});

// Run Optimization
runOptBtn.addEventListener('click', async () => {
    runOptBtn.disabled = true;
    runOptBtn.style.display = 'none';
    $('#stopOptBtn').style.display = 'inline-flex';
    $('#stopOptBtn').disabled = false;

    optError.style.display = 'none';
    optProgress.style.display = 'block';
    $('#optProgressFill').style.width = '0%';
    $('#optGenLabel').textContent = 'Optimizing...';

    initHistoryChart();

    const body = {
        weight_mean: parseFloat(weightMean.value),
        allowed_miss_pct: parseInt(allowedMiss.value) / 100.0,
        mutation_multiplier: parseFloat($('#mutationMultiplier').value),
        pop_size: parseInt($('#popSize').value),
        max_gen: parseInt($('#maxGen').value),
        ftol: parseFloat($('#ftol').value),
        term_period: parseInt($('#termPeriod').value),
    };

    try {
        const res = await fetch('/api/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();

        if (!res.ok) {
            showError(optError, data.error || 'Failed to start optimization');
            runOptBtn.disabled = false;
            runOptBtn.style.display = 'inline-flex';
            $('#stopOptBtn').style.display = 'none';
            optProgress.style.display = 'none';
            return;
        }

        startOptPolling(body.max_gen);

    } catch (err) {
        showError(optError, `Network error: ${err.message}`);
        runOptBtn.disabled = false;
        runOptBtn.style.display = 'inline-flex';
        $('#stopOptBtn').style.display = 'none';
        optProgress.style.display = 'none';
    }
});

function startOptPolling(maxGen) {
    const optStartTime = Date.now();
    optPollTimer = setInterval(async () => {
        try {
            const res = await fetch('/api/status');
            const data = await res.json();

            if (data.history && data.history.length > 0) {
                updateHistoryChart(data.history);
            }

            // Update progress bar
            if (data.status === 'running') {
                const currentGen = data.generation || 0;
                const pct = (currentGen / maxGen) * 100;
                $('#optProgressFill').style.width = `${pct}%`;

                if (currentGen > 0) {
                    const msPerGen = (Date.now() - optStartTime) / currentGen;
                    const timeLeftMs = msPerGen * (maxGen - currentGen);
                    const totalSecs = Math.round(timeLeftMs / 1000);
                    const mins = Math.floor(totalSecs / 60);
                    const secs = totalSecs % 60;

                    let timeStr = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
                    $('#optGenLabel').textContent = `Approximately ${timeStr} left... (Generation ${currentGen} / ${maxGen})`;
                } else {
                    $('#optGenLabel').textContent = `Optimizing... (Generation ${currentGen} / ${maxGen})`;
                }
            }

            if (data.status === 'complete') {
                clearInterval(optPollTimer);
                optPollTimer = null;
                $('#optProgressFill').style.width = '100%';
                $('#optGenLabel').textContent = 'Complete!';
                $('#stopOptBtn').style.display = 'none';
                runOptBtn.style.display = 'inline-flex';
                runOptBtn.disabled = false;

                // Short delay then go to results
                setTimeout(async () => {
                    goToStep(4);
                    await loadResults();
                }, 800);
            }

            if (data.status === 'error') {
                clearInterval(optPollTimer);
                optPollTimer = null;
                showError(optError, data.error || 'Optimization failed');
                optProgress.style.display = 'none';
                $('#stopOptBtn').style.display = 'none';
                runOptBtn.style.display = 'inline-flex';
                runOptBtn.disabled = false;
            }

        } catch (err) {
            // Silent retry
        }
    }, 500);
}

// Back button
$('#backToStep1Btn').addEventListener('click', async () => {
    try {
        await fetch('/api/reset', { method: 'POST' });
    } catch (err) {
        console.error('Failed to reset backend state', err);
    }

    // Clear frontend data
    fileInput.value = '';
    uploadError.style.display = 'none';
    uploadedFilesData = [];
    renderFiles();

    if (optPollTimer) {
        clearInterval(optPollTimer);
        optPollTimer = null;
    }

    optProgress.style.display = 'none';
    runOptBtn.style.display = 'inline-flex';
    runOptBtn.disabled = false;
    $('#stopOptBtn').style.display = 'none';

    goToStep(1);
});


// ═══════════════════════════════════════════════════════════════
//  STEP 4: RESULTS DASHBOARD
// ═══════════════════════════════════════════════════════════════

async function loadResults() {
    // Fetch heatmap data once — used by both heatmap and distribution chart
    let heatmapData = null;
    try {
        const res = await fetch('/api/heatmap-data');
        if (res.ok) heatmapData = await res.json();
    } catch (err) {
        console.error('Failed to fetch heatmap data:', err);
    }

    await Promise.all([
        loadComparison(),
        loadParetoChart(),
        loadHeatmap(heatmapData),
        loadDistributionChart(heatmapData),
    ]);
}

async function loadComparison() {
    try {
        const res = await fetch('/api/results');
        const data = await res.json();

        if (!res.ok) return;

        const { comparison } = data;
        const pool = comparison.pool;
        const lib = comparison.library;
        const pct = comparison.percentages;



        // Build comparison table
        const tbody = $('#comparisonBody');
        tbody.innerHTML = '';

        const metrics = [
            {
                name: 'Total Cost (USD)',
                pool: `$${pool.total_cost.toLocaleString(undefined, { maximumFractionDigits: 0 })}`,
                lib: `$${lib.total_cost.toLocaleString(undefined, { maximumFractionDigits: 0 })}`,
                pctVal: pct.cost,
                goodIfLow: true,
            },
            {
                name: 'Mean Selectivity',
                pool: pool.mean_selectivity.toFixed(2),
                lib: lib.mean_selectivity.toFixed(2),
                pctVal: pct.mean_selectivity,
                goodIfLow: false,
            },
            {
                name: 'Min Selectivity',
                pool: pool.min_selectivity.toFixed(2),
                lib: lib.min_selectivity.toFixed(2),
                pctVal: pct.min_selectivity,
                goodIfLow: false,
            },
            {
                name: 'Targets',
                pool: pool.num_targets,
                lib: lib.num_targets,
                pctVal: pct.targets,
                goodIfLow: false,
            },
            {
                name: 'Compounds',
                pool: pool.num_drugs,
                lib: lib.num_drugs,
                pctVal: pct.drugs,
                goodIfLow: true,
            },
        ];

        metrics.forEach((m) => {
            const row = document.createElement('tr');
            const badgeClass = m.goodIfLow
                ? (m.pctVal <= 50 ? 'good' : m.pctVal <= 80 ? 'neutral' : 'bad')
                : (m.pctVal >= 90 ? 'good' : m.pctVal >= 70 ? 'neutral' : 'bad');

            row.innerHTML = `
                <td class="metric-name">${m.name}</td>
                <td class="value">${m.pool}</td>
                <td class="value">${m.lib} <span class="pct-badge ${badgeClass}">${m.pctVal}%</span></td>
            `;
            tbody.appendChild(row);
        });

        // Render compounds list
        const compoundsBody = $('#compoundsListBody');
        if (compoundsBody && lib.compounds) {
            compoundsBody.innerHTML = '';
            lib.compounds.forEach((c) => {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td class="metric-name">${c.inchikey}</td>
                    <td class="metric-name" style="color: #a8a8b3; font-size: 0.9em;">${c.chembl_id}</td>
                    <td class="value">$${c.price.toFixed(2)}</td>
                `;
                compoundsBody.appendChild(tr);
            });
        }

    } catch (err) {
        console.error('Failed to load comparison:', err);
    }
}

async function loadParetoChart() {
    try {
        const res = await fetch('/api/pareto-data');
        const data = await res.json();

        if (!res.ok) return;

        const points = data.points;
        const bestIdx = data.best_idx;
        const selectedIdx = data.selected_idx !== undefined ? data.selected_idx : bestIdx;

        const x = points.map((p) => p[0]);  // Selectivity Score (weight_mean * mean selectivity + weight_min * min selectivity)
        const y = points.map((p) => p[1]);  // Cost

        // Store point indices for click handler
        const pointIndices = points.map((_, i) => i);

        // Get coordinates for the special points
        const selectedX = x[selectedIdx];
        const selectedY = y[selectedIdx];
        const bestX = x[bestIdx];
        const bestY = y[bestIdx];

        // Arrays for conditional styling on the selected point (matching by coordinates for duplicates)
        const hoverTemplates = points.map((_, i) => {
            if (x[i] === selectedX && y[i] === selectedY) return 'Selectivity: %{x:.2f}<br>Cost: $%{y:,.0f}<br><i>Selected</i><extra></extra>';
            if (x[i] === bestX && y[i] === bestY) return 'Selectivity: %{x:.2f}<br>Cost: $%{y:,.0f}<br><i>Best compromise solution, click to select</i><extra></extra>';
            return 'Selectivity: %{x:.2f}<br>Cost: $%{y:,.0f}<br><i>Click to select</i><extra></extra>';
        });
        const hoverBgColors = points.map((_, i) => {
            if (x[i] === selectedX && y[i] === selectedY) return '#ffd43b';
            if (x[i] === bestX && y[i] === bestY) return '#845ef7';
            return 'rgba(56, 217, 169, 0.9)';
        });

        // All points
        const traceAll = {
            x: x,
            y: y,
            mode: 'markers',
            type: 'scatter',
            name: 'Pareto Solutions',
            marker: {
                size: 8,
                color: 'rgba(56, 217, 169, 0.6)',
                line: { width: 1, color: 'rgba(56, 217, 169, 0.9)' },
            },
            hovertemplate: hoverTemplates,
            hoverlabel: { bgcolor: hoverBgColors },
            customdata: pointIndices,
        };

        // Best compromise (star)
        const traceBest = {
            x: [x[bestIdx]],
            y: [y[bestIdx]],
            mode: 'markers',
            type: 'scatter',
            name: 'Best Compromise',
            marker: {
                size: 16,
                color: '#845ef7',
                symbol: 'star',
                line: { width: 2, color: '#fff' },
            },
            hoverinfo: 'skip',
        };

        // Selected solution (ring)
        const traceSelected = {
            x: [x[selectedIdx]],
            y: [y[selectedIdx]],
            mode: 'markers',
            type: 'scatter',
            name: 'Selected',
            marker: {
                size: 18,
                color: 'rgba(0,0,0,0)',
                symbol: 'circle',
                line: { width: 3, color: '#ffd43b' },
            },
            hoverinfo: 'skip',
        };

        const layout = {
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0.15)',
            font: { family: 'Inter, sans-serif', color: '#9898b8' },
            xaxis: {
                title: { text: 'Selectivity Score', font: { size: 13, color: '#9898b8' } },
                gridcolor: 'rgba(120, 120, 255, 0.08)',
                zerolinecolor: 'rgba(120, 120, 255, 0.12)',
            },
            yaxis: {
                title: { text: 'Total Library Cost (USD)', font: { size: 13, color: '#9898b8' }, standoff: 20 },
                gridcolor: 'rgba(120, 120, 255, 0.08)',
                zerolinecolor: 'rgba(120, 120, 255, 0.12)',
                tickformat: '$,.0f',
            },
            legend: {
                font: { size: 11 },
                bgcolor: 'rgba(0,0,0,0.3)',
                bordercolor: 'rgba(120,120,255,0.1)',
                borderwidth: 1,
                x: 1.02,
                xanchor: 'left',
                y: 1,
                yanchor: 'top',
            },
            margin: { l: 90, r: 180, t: 20, b: 85 },
        };

        Plotly.newPlot('paretoChart', [traceAll, traceBest, traceSelected], layout, {
            responsive: true,
            displayModeBar: false,
        });

        // Click handler — only for trace 0 (the Pareto Solutions dots)
        const chartEl = document.getElementById('paretoChart');
        chartEl.on('plotly_click', async (eventData) => {
            const point = eventData.points[0];
            // Only respond to clicks on trace 0 (Pareto Solutions)
            if (point.curveNumber !== 0) return;

            const clickedIdx = point.customdata;

            // Get coordinates for the special points
            const clickedX = x[clickedIdx];
            const clickedY = y[clickedIdx];
            const bestX = x[bestIdx];
            const bestY = y[bestIdx];

            // Update hover styles based on new selectedIdx
            const newHoverTemplates = points.map((_, i) => {
                if (x[i] === clickedX && y[i] === clickedY) return 'Selectivity: %{x:.4f}<br>Cost: $%{y:,.0f}<br><i>Selected</i><extra></extra>';
                if (x[i] === bestX && y[i] === bestY) return 'Selectivity: %{x:.4f}<br>Cost: $%{y:,.0f}<br><i>Best compromise solution, click to select</i><extra></extra>';
                return 'Selectivity: %{x:.4f}<br>Cost: $%{y:,.0f}<br><i>Click to select</i><extra></extra>';
            });
            const newHoverBgColors = points.map((_, i) => {
                if (x[i] === clickedX && y[i] === clickedY) return '#ffd43b';
                if (x[i] === bestX && y[i] === bestY) return '#845ef7';
                return 'rgba(56, 217, 169, 0.9)';
            });

            // Move the selected ring to the clicked point
            Plotly.restyle('paretoChart', {
                x: [[x[clickedIdx]]],
                y: [[y[clickedIdx]]],
            }, [2]);  // Trace index 2 = traceSelected

            // Update the hover styling for all points
            Plotly.restyle('paretoChart', {
                hovertemplate: [newHoverTemplates],
                'hoverlabel.bgcolor': [newHoverBgColors]
            }, [0]);  // Trace index 0 = traceAll

            // Call backend to switch the active solution
            try {
                const selRes = await fetch('/api/select-solution', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ index: clickedIdx }),
                });
                const selData = await selRes.json();
                if (!selRes.ok) {
                    console.error('Failed to select solution:', selData.error);
                    return;
                }

                // Refresh comparison table, heatmap, and distribution
                await Promise.all([
                    loadComparison(),
                    loadHeatmap(),
                    loadDistributionChart(),
                ]);
            } catch (err) {
                console.error('Error selecting solution:', err);
            }
        });

    } catch (err) {
        console.error('Failed to load Pareto chart:', err);
    }
}

async function loadHeatmap(prefetchedData) {
    try {
        let data = prefetchedData;
        if (!data) {
            const res = await fetch('/api/heatmap-data');
            data = await res.json();
            if (!res.ok) return;
        }

        const hoverText = data.matrix.map(row =>
            row.map(val => val === null ? "No Data" : val.toFixed(2))
        );

        const truncCompounds = data.compounds.map((s) => s.length > 30 ? s.substring(0, 27) + '...' : s);
        const customData = data.matrix.map((row, i) =>
            row.map(() => truncCompounds[i])
        );

        const trace = {
            z: data.matrix,
            x: data.targets,
            y: data.compounds,
            text: hoverText,
            customdata: customData,
            type: 'heatmap',
            colorscale: 'Viridis',
            hovertemplate: 'Target: %{x}<br>Compound: %{customdata}<br>Selectivity: %{text}<extra></extra>',
            colorbar: {
                title: { text: 'Selectivity', font: { size: 12, color: '#9898b8' } },
                tickfont: { color: '#9898b8' },
            },
        };

        const layout = {
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(255,255,255,0.1)',
            font: { family: 'Inter, sans-serif', color: '#9898b8', size: 10 },
            xaxis: {
                showticklabels: false,
                showgrid: false,
                title: { text: 'Targets', font: { size: 12, color: '#9898b8' } },
                side: 'top',
            },
            yaxis: {
                showticklabels: false,
                showgrid: false,
                autorange: 'reversed',
                title: { text: 'Compounds', font: { size: 12, color: '#9898b8' } },
            },
            margin: { l: 40, r: 20, t: 40, b: 40 },
        };

        Plotly.newPlot('heatmapChart', [trace], layout, {
            responsive: true,
            displayModeBar: false,
        });

    } catch (err) {
        console.error('Failed to load heatmap:', err);
    }
}

async function loadDistributionChart(prefetchedData) {
    try {
        let data = prefetchedData;
        if (!data) {
            const res = await fetch('/api/heatmap-data');
            data = await res.json();
            if (!res.ok) return;
        }

        const numTargets = data.targets.length;
        const numCompounds = data.matrix.length;

        let stats = [];
        for (let j = 0; j < numTargets; j++) {
            let col = [];
            for (let i = 0; i < numCompounds; i++) {
                let val = data.matrix[i][j];
                if (val !== null && !isNaN(val)) {
                    col.push(val);
                }
            }

            if (col.length === 0) continue;

            col.sort((a, b) => a - b);

            let max = col[col.length - 1];
            let min = col[0];
            let median = 0;
            let mid = Math.floor(col.length / 2);
            if (col.length % 2 === 0) {
                median = (col[mid - 1] + col[mid]) / 2;
            } else {
                median = col[mid];
            }

            stats.push({
                target: data.targets[j],
                max: max,
                median: median,
                min: min
            });
        }

        stats.sort((a, b) => b.max - a.max);

        const x = stats.map((s, i) => i);

        const hoverTemplate = '<b>%{customdata[3]}</b><br>Max: %{customdata[0]:.2f}<br>Median: %{customdata[1]:.2f}<br>Min: %{customdata[2]:.2f}<extra></extra>';
        const customData = stats.map(s => [s.max, s.median, s.min, s.target]);

        const traceMax = {
            x: x,
            y: stats.map(s => s.max),
            name: 'Max',
            type: 'bar',
            marker: { color: 'rgba(132, 94, 247, 0.5)' },
            width: 0.85,
            hovertemplate: hoverTemplate,
            customdata: customData
        };

        const traceMedian = {
            x: x,
            y: stats.map(s => s.median),
            name: 'Median',
            type: 'bar',
            marker: { color: 'rgba(77, 171, 247, 0.75)' },
            width: 0.85,
            hovertemplate: hoverTemplate,
            customdata: customData
        };

        const traceMin = {
            x: x,
            y: stats.map(s => s.min),
            name: 'Min',
            type: 'bar',
            marker: { color: 'rgba(56, 217, 169, 0.95)' },
            width: 0.85,
            hovertemplate: hoverTemplate,
            customdata: customData
        };

        const layout = {
            barmode: 'overlay',
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0.15)',
            font: { family: 'Inter, sans-serif', color: '#9898b8' },
            xaxis: {
                showticklabels: false,
                title: { text: 'Targets', font: { size: 13, color: '#9898b8' } },
                zeroline: false,
                gridcolor: 'rgba(120, 120, 255, 0.08)',
                range: [-0.5, stats.length - 0.5]
            },
            yaxis: {
                title: { text: 'Selectivity Score', font: { size: 13, color: '#9898b8' }, standoff: 15 },
                gridcolor: 'rgba(120, 120, 255, 0.08)',
                zerolinecolor: 'rgba(255, 255, 255, 0.45)',
                zerolinewidth: 2,
                ticks: 'outside',
                ticklen: 5,
                tickcolor: 'rgba(0,0,0,0)'
            },
            legend: {
                font: { size: 11 },
                bgcolor: 'rgba(0,0,0,0.3)',
                bordercolor: 'rgba(120,120,255,0.1)',
                borderwidth: 1
            },
            margin: { l: 80, r: 30, t: 20, b: 60 },
            hovermode: 'closest'
        };

        Plotly.newPlot('distributionChart', [traceMax, traceMedian, traceMin], layout, {
            responsive: true,
            displayModeBar: false
        });

    } catch (err) {
        console.error('Failed to load distribution chart:', err);
    }
}


// Navigation buttons on step 4
$('#backToStep3Btn').addEventListener('click', async () => {
    try {
        await fetch('/api/reset-opt', { method: 'POST' });
    } catch (err) { }
    runOptBtn.disabled = false;
    optProgress.style.display = 'none';
    goToStep(3);
    await loadDatasetInfo();
});

// New run button
$('#newRunBtn').addEventListener('click', async () => {
    try {
        await fetch('/api/reset', { method: 'POST' });
    } catch (err) {
        console.error('Failed to reset backend state', err);
    }

    // Clear frontend data
    fileInput.value = '';
    uploadError.style.display = 'none';
    uploadedFilesData = [];
    renderFiles();

    if (optPollTimer) {
        clearInterval(optPollTimer);
        optPollTimer = null;
    }

    optProgress.style.display = 'none';
    runOptBtn.style.display = 'inline-flex';
    runOptBtn.disabled = false;
    $('#stopOptBtn').style.display = 'none';

    goToStep(1);
});


// ═══════════════════════════════════════════════════════════════
//  UTILITIES
// ═══════════════════════════════════════════════════════════════

function showError(el, message) {
    el.textContent = message;
    el.style.display = 'block';
}

// ═══════════════════════════════════════════════════════════════
//  INITIALIZATION
// ═══════════════════════════════════════════════════════════════

// End of app.js
