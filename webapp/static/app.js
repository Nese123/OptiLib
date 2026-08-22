/* ═══════════════════════════════════════════════════════════════
   Drug Library Optimization — Frontend Logic
   ═══════════════════════════════════════════════════════════════ */

// ─── State ───
let currentStep = 1;
let uploadMode = sessionStorage.getItem('uploadMode') || 'target'; // 'target' | 'affinity'
let uploadedChemblIds = [];
let uploadedMatchedCount = 0;
let uploadedFilesData = [];
let uploadedAffinityFilesData = [];
let uploadedAffinityData = null;
let uploadedPriceFilesData = [];

try {
    const stored = sessionStorage.getItem('uploadedFilesData');
    if (stored) {
        uploadedFilesData = JSON.parse(stored);
    }
} catch (e) {
    console.error('Failed to restore uploaded files', e);
}

try {
    const storedAffFiles = sessionStorage.getItem('uploadedAffinityFilesData');
    if (storedAffFiles) {
        uploadedAffinityFilesData = JSON.parse(storedAffFiles);
    } else {
        const storedAff = sessionStorage.getItem('uploadedAffinityData');
        if (storedAff) {
            const oldData = JSON.parse(storedAff);
            if (oldData && oldData.name) {
                uploadedAffinityFilesData = [{
                    name: oldData.name,
                    data: oldData,
                    allCompounds: oldData.compounds || [],
                    allTargets: oldData.targets || [],
                    totalCompounds: oldData.num_compounds || 0,
                    totalTargets: oldData.num_targets || 0,
                    totalDatapoints: oldData.num_datapoints || 0
                }];
            }
        }
    }
} catch (e) {
    console.error('Failed to restore affinity data', e);
}

try {
    const storedPriceFiles = sessionStorage.getItem('uploadedPriceFilesData');
    if (storedPriceFiles) {
        uploadedPriceFilesData = JSON.parse(storedPriceFiles);
    } else {
        const storedPrice = sessionStorage.getItem('uploadedPriceData');
        if (storedPrice) {
            const oldData = JSON.parse(storedPrice);
            if (oldData && oldData.filename) {
                uploadedPriceFilesData = [{
                    name: oldData.filename,
                    data: oldData,
                    allCompounds: oldData.compounds || [],
                    totalUnique: oldData.num_prices || (oldData.compounds || []).length
                }];
            }
        }
    }
} catch (e) {
    console.error('Failed to restore price data', e);
}

let pipelinePollTimer = null;
let optPollTimer = null;

// ─── DOM Elements ───
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// Restore state on page load
window.addEventListener('DOMContentLoaded', async () => {
    setupModeSwitcher();
    setupPriceUpload();
    setupOptSettings();

    // Restore UI if we have saved files
    if (uploadMode === 'target') {
        if (uploadedFilesData.length > 0) {
            renderFiles();
        }
    } else {
        if (uploadedAffinityFilesData && uploadedAffinityFilesData.length > 0) {
            renderAffinityFiles();
        }
    }

    if (uploadedPriceFilesData && uploadedPriceFilesData.length > 0) {
        renderPriceFiles();
    }

    try {
        const pipeRes = await fetch('/api/pipeline-status');
        const pipeState = await pipeRes.json();

        const optRes = await fetch('/api/status');
        const optState = await optRes.json();

        const datasetRes = await fetch('/api/dataset-info');
        const datasetState = await datasetRes.json();

        if (optState.status === 'complete') {
            const savedStep = sessionStorage.getItem('currentStep');
            if (savedStep === '4') {
                goToStep(4);
                await loadResults();
            } else {
                goToStep(3);
                await loadDatasetInfo();
                showOptCompleteBanner(optState.generation || '?');
                // Restore the optimization progress history chart
                if (optState.history && optState.history.length > 0) {
                    initHistoryChart();
                    updateHistoryChart(optState.history);
                }
            }
        } else if (optState.status === 'running') {
            goToStep(3);
            await loadDatasetInfo();
            runOptBtn.disabled = true;
            runOptBtn.style.display = 'none';
            $('#stopOptBtn').style.display = 'inline-flex';
            $('#stopOptBtn').disabled = false;
            optError.style.display = 'none';
            initHistoryChart();
            $('#optStatusIndicator').style.visibility = 'visible';
            $('#optStatusText').textContent = 'Optimizing...';
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

    if (step === 3) {
        restoreOptSettings();
    }

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

    // Trigger chart resize in case containers were previously hidden
    requestAnimationFrame(() => {
        ['historyChart', 'paretoChart', 'heatmapChart', 'distributionChart'].forEach((id) => {
            const el = document.getElementById(id);
            if (el && el.data) {
                Plotly.Plots.resize(el);
            }
        });
    });

    // Scroll to top so the new step is visible from the beginning
    window.scrollTo(0, 0);
}

// ═══════════════════════════════════════════════════════════════
//  STEP 1: FILE UPLOAD & MODE SWITCHING
// ═══════════════════════════════════════════════════════════════

const dropZone = $('#dropZone');
const fileInput = $('#fileInput');
const fileInfo = $('#fileInfo');
const validationSummary = $('#validationSummary');
const affinitySummary = $('#affinitySummary');
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

// Mode buttons
const modeTargetBtn = $('#modeTargetBtn');
const modeAffinityBtn = $('#modeAffinityBtn');
const step1Title = $('#step1Title');
const dropZoneText = $('#dropZoneText');
const dropZoneHint = $('#dropZoneHint');
const exampleDownloadBtn = $('#exampleDownloadBtn');
const exampleDownloadText = $('#exampleDownloadText');

function setupModeSwitcher() {
    if (!modeTargetBtn || !modeAffinityBtn) return;

    function applyMode(mode) {
        uploadMode = mode;
        sessionStorage.setItem('uploadMode', mode);

        if (mode === 'target') {
            modeTargetBtn.classList.add('active');
            modeAffinityBtn.classList.remove('active');
            if (step1Title) step1Title.innerHTML = '<span class="icon">📂</span> Upload Target List';
            if (dropZoneText) dropZoneText.textContent = 'Drag & drop your target files here, or click to browse';
            if (dropZoneHint) dropZoneHint.textContent = 'CSV or Excel (.xlsx) with a "Target" column · Accepts target names, ChEMBL IDs, Gene Symbols, or UniProt Accessions';
            if (exampleDownloadBtn) exampleDownloadBtn.href = '/static/example_targets.xlsx';
            if (exampleDownloadText) exampleDownloadText.textContent = 'Download Example Targets';
            if (affinitySummary) affinitySummary.style.display = 'none';
            renderFiles();
        } else {
            modeAffinityBtn.classList.add('active');
            modeTargetBtn.classList.remove('active');
            if (step1Title) step1Title.innerHTML = '<span class="icon">📂</span> Upload Predefined Affinity Data';
            if (dropZoneText) dropZoneText.textContent = 'Drag & drop your affinity data here, or click to browse';
            if (dropZoneHint) dropZoneHint.textContent = 'CSV or Excel (.xlsx) with columns "Compound", "Target", and "Affinity" (pKd, higher value means better affinity) · Accepts compound names, ChEMBL IDs, SMILES Strings, and InChIKeys for compound IDs & target names, ChEMBL IDs, Gene Symbols and UniProt Accessions for target IDs';
            if (exampleDownloadBtn) exampleDownloadBtn.href = '/static/example_affinity.xlsx';
            if (exampleDownloadText) exampleDownloadText.textContent = 'Download Example Affinity Data';
            if (validationSummary) validationSummary.style.display = 'none';
            if (uploadedAffinityFilesData && uploadedAffinityFilesData.length > 0) {
                renderAffinityFiles();
            } else {
                if (fileInfo) fileInfo.style.display = 'none';
                if (removeAllBtnContainer) removeAllBtnContainer.style.display = 'none';
                if (affinitySummary) affinitySummary.style.display = 'none';
                if (thresholdControl) thresholdControl.style.display = 'none';
                if (buildMatrixBtn) buildMatrixBtn.disabled = true;
            }
        }
    }

    modeTargetBtn.addEventListener('click', () => {
        if (uploadMode !== 'target') {
            resetOptSettingsToDefault();
        }
        applyMode('target');
    });
    modeAffinityBtn.addEventListener('click', () => {
        if (uploadMode !== 'affinity') {
            resetOptSettingsToDefault();
        }
        applyMode('affinity');
    });

    // Apply saved mode
    applyMode(uploadMode);
}

// Custom Price Upload Handling
function setupPriceUpload() {
    const priceDropZone = $('#priceDropZone');
    const priceFileInput = $('#priceFileInput');
    const priceRemoveAllBtn = $('#priceRemoveAllBtn');
    const priceRemoveAllConfirm = $('#priceRemoveAllConfirm');
    const priceRemoveAllYesBtn = $('#priceRemoveAllYesBtn');
    const priceRemoveAllNoBtn = $('#priceRemoveAllNoBtn');

    if (!priceDropZone || !priceFileInput) return;

    ['dragenter', 'dragover'].forEach(eventName => {
        priceDropZone.addEventListener(eventName, (e) => {
            e.preventDefault();
            e.stopPropagation();
            priceDropZone.classList.add('drag-over');
        });
        priceFileInput.addEventListener(eventName, (e) => {
            e.preventDefault();
            e.stopPropagation();
            priceDropZone.classList.add('drag-over');
        });
    });

    ['dragleave', 'dragend'].forEach(eventName => {
        priceDropZone.addEventListener(eventName, (e) => {
            e.preventDefault();
            e.stopPropagation();
            priceDropZone.classList.remove('drag-over');
        });
        priceFileInput.addEventListener(eventName, (e) => {
            e.preventDefault();
            e.stopPropagation();
            priceDropZone.classList.remove('drag-over');
        });
    });

    priceDropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        e.stopPropagation();
        priceDropZone.classList.remove('drag-over');
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
            handlePriceFileUpload(Array.from(e.dataTransfer.files));
        }
    });

    priceFileInput.addEventListener('drop', (e) => {
        e.preventDefault();
        e.stopPropagation();
        priceDropZone.classList.remove('drag-over');
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
            handlePriceFileUpload(Array.from(e.dataTransfer.files));
        }
    });

    priceFileInput.addEventListener('click', (e) => {
        e.target.value = '';
    });

    priceFileInput.addEventListener('change', () => {
        if (priceFileInput.files && priceFileInput.files.length) {
            handlePriceFileUpload(Array.from(priceFileInput.files));
        }
    });

    if (priceRemoveAllBtn && priceRemoveAllConfirm && priceRemoveAllYesBtn && priceRemoveAllNoBtn) {
        priceRemoveAllBtn.addEventListener('click', () => {
            priceRemoveAllBtn.style.display = 'none';
            priceRemoveAllConfirm.style.display = 'flex';
        });

        priceRemoveAllYesBtn.addEventListener('click', async () => {
            try {
                await fetch('/api/clear-prices', { method: 'POST' });
            } catch (err) { }
            uploadedPriceFilesData = [];
            sessionStorage.removeItem('uploadedPriceFilesData');
            resetOptSettingsToDefault();
            renderPriceFiles();
        });

        priceRemoveAllNoBtn.addEventListener('click', () => {
            priceRemoveAllConfirm.style.display = 'none';
            priceRemoveAllBtn.style.display = 'inline-block';
        });
    }
}

async function parseJsonResponse(res) {
    const contentType = res.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
        return await res.json();
    }
    const text = await res.text();
    if (!res.ok) {
        if (res.status === 404) {
            throw new Error('Endpoint not found (404). Please restart the Flask server (`python webapp/app.py`) so it registers newly added routes.');
        }
        throw new Error(`Server error (${res.status}): ${text.slice(0, 100)}`);
    }
    return {};
}

async function handlePriceFileUpload(files) {
    const fileList = Array.isArray(files) ? files : (files instanceof FileList ? Array.from(files) : [files]);
    if (!fileList.length) return;

    for (let f of fileList) {
        const formData = new FormData();
        formData.append('files[]', f);

        try {
            const res = await fetch('/api/upload-prices', { method: 'POST', body: formData });
            const data = await parseJsonResponse(res);

            if (res.ok) {
                if (Array.isArray(data.all_files)) {
                    uploadedPriceFilesData = data.all_files.map(af => ({
                        name: af.name,
                        data: af,
                        allCompounds: data.compounds || [],
                        totalUnique: data.num_prices
                    }));
                } else {
                    const existingIdx = uploadedPriceFilesData.findIndex(item => item.name === f.name);
                    const fileSummary = (data.uploaded_files && data.uploaded_files[0]) || {
                        name: f.name,
                        num_prices: data.num_prices,
                        compounds: data.compounds || []
                    };
                    const fileEntry = {
                        name: f.name,
                        data: fileSummary,
                        allCompounds: data.compounds || [],
                        totalUnique: data.num_prices
                    };
                    if (existingIdx >= 0) {
                        uploadedPriceFilesData[existingIdx] = fileEntry;
                    } else {
                        uploadedPriceFilesData.push(fileEntry);
                    }
                }
            } else {
                showError(uploadError, data.error || `Failed to process price file: ${f.name}`);
            }
        } catch (err) {
            showError(uploadError, `Price upload error: ${err.message}`);
        }
    }

    resetOptSettingsToDefault();
    renderPriceFiles();
}

function renderPriceFiles() {
    const priceFileInfo = $('#priceFileInfo');
    const priceRemoveAllBtnContainer = $('#priceRemoveAllBtnContainer');
    const priceRemoveAllBtn = $('#priceRemoveAllBtn');
    const priceRemoveAllConfirm = $('#priceRemoveAllConfirm');
    const priceValidationSummary = $('#priceValidationSummary');
    const priceMatchedCount = $('#priceMatchedCount');
    const priceCompoundList = $('#priceCompoundList');
    const priceFileInput = $('#priceFileInput');

    if (!priceFileInfo) return;

    priceFileInfo.innerHTML = '';

    if (!uploadedPriceFilesData || uploadedPriceFilesData.length === 0) {
        priceFileInfo.style.display = 'none';
        if (priceRemoveAllBtnContainer) priceRemoveAllBtnContainer.style.display = 'none';
        if (priceValidationSummary) priceValidationSummary.style.display = 'none';
        if (priceFileInput) priceFileInput.value = '';
        sessionStorage.removeItem('uploadedPriceFilesData');
        return;
    }

    sessionStorage.setItem('uploadedPriceFilesData', JSON.stringify(uploadedPriceFilesData));

    priceFileInfo.style.display = 'flex';
    if (priceRemoveAllBtnContainer) {
        priceRemoveAllBtnContainer.style.display = 'block';
        if (priceRemoveAllBtn) priceRemoveAllBtn.style.display = 'inline-block';
        if (priceRemoveAllConfirm) priceRemoveAllConfirm.style.display = 'none';
    }

    uploadedPriceFilesData.forEach(fileData => {
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
        deleteBtn.title = `Remove ${fileData.name}`;

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
            yesBtn.onclick = async () => {
                try {
                    const res = await fetch('/api/remove-price-file', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ filename: fileData.name })
                    });
                    const resData = await parseJsonResponse(res);
                    if (res.ok && Array.isArray(resData.all_files)) {
                        uploadedPriceFilesData = resData.all_files.map(af => ({
                            name: af.name,
                            data: af,
                            allCompounds: resData.compounds || [],
                            totalUnique: resData.num_prices
                        }));
                    } else {
                        uploadedPriceFilesData = uploadedPriceFilesData.filter(d => d.name !== fileData.name);
                    }
                } catch (e) {
                    uploadedPriceFilesData = uploadedPriceFilesData.filter(d => d.name !== fileData.name);
                }
                renderPriceFiles();
            };

            const noBtn = document.createElement('button');
            noBtn.className = 'btn btn-secondary';
            noBtn.style.padding = '0.25rem 0.75rem';
            noBtn.style.fontSize = '0.85rem';
            noBtn.style.minWidth = '50px';
            noBtn.textContent = 'No';
            noBtn.onclick = () => renderPriceFiles();

            btnContainer.appendChild(yesBtn);
            btnContainer.appendChild(noBtn);

            box.appendChild(msg);
            box.appendChild(btnContainer);
        };

        rightSide.appendChild(deleteBtn);
        box.appendChild(leftSide);
        box.appendChild(rightSide);
        priceFileInfo.appendChild(box);
    });

    // Compute unique compounds across all files
    let allCompounds = [];
    let totalUnique = 0;
    if (uploadedPriceFilesData.length > 0 && uploadedPriceFilesData[0].allCompounds && uploadedPriceFilesData[0].allCompounds.length > 0) {
        allCompounds = uploadedPriceFilesData[0].allCompounds;
        totalUnique = uploadedPriceFilesData[0].totalUnique || allCompounds.length;
    } else {
        uploadedPriceFilesData.forEach(f => {
            if (f.data && Array.isArray(f.data.compounds)) {
                allCompounds.push(...f.data.compounds);
            }
        });
        allCompounds = [...new Set(allCompounds)];
        totalUnique = allCompounds.length;
    }

    if (priceValidationSummary) {
        priceValidationSummary.style.display = 'block';
        if (priceMatchedCount) {
            priceMatchedCount.textContent = `${totalUnique} unique prices loaded`;
        }

        if (priceCompoundList) {
            priceCompoundList.innerHTML = '';
            allCompounds.forEach(c => {
                const item = document.createElement('div');
                item.className = 'target-list-item';

                const textSpan = document.createElement('span');
                textSpan.textContent = c;

                const delBtn = document.createElement('span');
                delBtn.textContent = '❌';
                delBtn.className = 'target-list-delete';
                delBtn.title = 'Remove compound';
                delBtn.onclick = () => {
                    item.innerHTML = '';

                    const compoundName = c.split(' ->')[0];
                    const msg = document.createElement('span');
                    msg.textContent = `Are you sure you want to remove the price for compound ${compoundName}?`;
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
                    yesBtn.onclick = async () => {
                        try {
                            const res = await fetch('/api/remove-price-compound', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ compound: c })
                            });
                            const resData = await parseJsonResponse(res);
                            if (res.ok) {
                                if (Array.isArray(resData.all_files)) {
                                    uploadedPriceFilesData = resData.all_files.map(af => ({
                                        name: af.name,
                                        data: af,
                                        allCompounds: resData.compounds || [],
                                        totalUnique: resData.num_prices
                                    }));
                                }
                                if (resData.num_prices === 0 || !resData.compounds || resData.compounds.length === 0) {
                                    uploadedPriceFilesData = [];
                                    sessionStorage.removeItem('uploadedPriceFilesData');
                                }
                            }
                        } catch (e) { }
                        renderPriceFiles();
                    };

                    const noBtn = document.createElement('button');
                    noBtn.className = 'btn btn-secondary';
                    noBtn.style.padding = '0.15rem 0.5rem';
                    noBtn.style.fontSize = '0.75rem';
                    noBtn.style.minWidth = '40px';
                    noBtn.textContent = 'No';
                    noBtn.onclick = () => renderPriceFiles();

                    btnContainer.appendChild(yesBtn);
                    btnContainer.appendChild(noBtn);

                    item.appendChild(msg);
                    item.appendChild(btnContainer);
                };

                item.appendChild(textSpan);
                item.appendChild(delBtn);
                priceCompoundList.appendChild(item);
            });
        }
    }
}

// Remove All buttons
removeAllBtn.addEventListener('click', () => {
    removeAllBtn.style.display = 'none';
    removeAllConfirm.style.display = 'flex';
});

removeAllYesBtn.addEventListener('click', async () => {
    if (uploadMode === 'target') {
        uploadedFilesData = [];
        resetOptSettingsToDefault();
        renderFiles();
    } else {
        try {
            await fetch('/api/clear-affinity', { method: 'POST' });
        } catch (e) { }
        uploadedAffinityFilesData = [];
        sessionStorage.removeItem('uploadedAffinityFilesData');
        sessionStorage.removeItem('uploadedAffinityData');
        resetOptSettingsToDefault();
        renderAffinityFiles();
    }
    removeAllConfirm.style.display = 'none';
    removeAllBtn.style.display = 'inline-block';
});

removeAllNoBtn.addEventListener('click', () => {
    removeAllConfirm.style.display = 'none';
    removeAllBtn.style.display = 'inline-block';
});

// Drag & drop visual for main drop zone
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

fileInput.addEventListener('click', function (e) {
    e.target.value = '';
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
    uploadError.style.display = 'none';

    if (uploadMode === 'affinity') {
        const fileList = Array.isArray(files) ? files : (files instanceof FileList ? Array.from(files) : [files]);
        if (!fileList.length) return;

        for (let f of fileList) {
            const formData = new FormData();
            formData.append('files[]', f);

            try {
                const res = await fetch('/api/upload-affinity', { method: 'POST', body: formData });
                const data = await parseJsonResponse(res);

                if (res.ok) {
                    if (Array.isArray(data.all_files)) {
                        uploadedAffinityFilesData = data.all_files.map(af => ({
                            name: af.name,
                            data: af,
                            allCompounds: data.compounds || [],
                            allTargets: data.targets || [],
                            totalCompounds: data.num_compounds,
                            totalTargets: data.num_targets,
                            totalDatapoints: data.num_datapoints
                        }));
                    } else {
                        const existingIdx = uploadedAffinityFilesData.findIndex(item => item.name === f.name);
                        const fileSummary = (data.uploaded_files && data.uploaded_files[0]) || {
                            name: f.name,
                            num_compounds: data.num_compounds,
                            num_targets: data.num_targets,
                            num_datapoints: data.num_datapoints,
                            compounds: data.compounds || [],
                            targets: data.targets || []
                        };
                        const fileEntry = {
                            name: f.name,
                            data: fileSummary,
                            allCompounds: data.compounds || [],
                            allTargets: data.targets || [],
                            totalCompounds: data.num_compounds,
                            totalTargets: data.num_targets,
                            totalDatapoints: data.num_datapoints
                        };
                        if (existingIdx >= 0) {
                            uploadedAffinityFilesData[existingIdx] = fileEntry;
                        } else {
                            uploadedAffinityFilesData.push(fileEntry);
                        }
                    }
                } else {
                    showError(uploadError, data.error || `Failed to process affinity file: ${f.name}`);
                }
            } catch (err) {
                showError(uploadError, `Affinity upload error: ${err.message}`);
            }
        }

        resetOptSettingsToDefault();
        renderAffinityFiles();
        return;
    }

    // Target mode upload
    validationSummary.style.display = 'none';
    buildMatrixBtn.disabled = true;
    thresholdControl.style.display = 'none';

    for (let f of files) {
        if (uploadedFilesData.some(d => d.name === f.name)) continue;

        const formData = new FormData();
        formData.append('files[]', f);

        try {
            const res = await fetch('/api/upload-targets', { method: 'POST', body: formData });
            const data = await parseJsonResponse(res);

            if (res.ok) {
                uploadedFilesData.push({ name: f.name, data: data });
            } else {
                showError(uploadError, data.error || `Upload failed for ${f.name}`);
            }
        } catch (err) {
            showError(uploadError, `Network error: ${err.message}`);
        }
    }

    resetOptSettingsToDefault();
    renderFiles();
}

function renderFiles() {
    fileInfo.innerHTML = '';

    // Recompute total cumulative targets
    let allMatched = [];
    let allUnmatched = [];

    uploadedFilesData.forEach((fileData) => {
        const d = fileData.data || {};
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
                uploadedFilesData = uploadedFilesData.filter((d) => d.name !== fileData.name);
                resetOptSettingsToDefault();
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
        uploadedMatchedCount = 0;
        sessionStorage.removeItem('uploadedFilesData');
        return; // Nothing more to do
    }

    // Save to session storage
    sessionStorage.setItem('uploadedFilesData', JSON.stringify(uploadedFilesData));

    const uniqueMatched = [...new Set(allMatched)];
    const uniqueUnmatched = [...new Set(allUnmatched)];

    let currentChemblIds = [];
    uploadedFilesData.forEach((fileData) => {
        const d = fileData.data || {};
        const map = d.chembl_map || {};
        (d.matched || []).forEach((matchStr) => {
            if (map[matchStr]) {
                currentChemblIds.push(map[matchStr]);
            } else {
                const match = matchStr.match(/->\s*([^\s(]+)/);
                if (match && match[1] && match[1].toUpperCase().startsWith('CHEMBL')) {
                    currentChemblIds.push(match[1]);
                }
            }
        });
        if (currentChemblIds.length === 0 && Array.isArray(d.chembl_ids)) {
            currentChemblIds.push(...d.chembl_ids);
        }
    });
    uploadedChemblIds = [...new Set(currentChemblIds)];
    uploadedMatchedCount = uniqueMatched.length;

    // Show validation summary
    validationSummary.style.display = 'block';

    // Matched
    const matchedCountEl = $('#matchedCount');
    if (matchedCountEl) matchedCountEl.textContent = `${uniqueMatched.length} targets matched in ChEMBL`;

    const matchedListEl = $('#matchedList');
    if (matchedListEl) {
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
                    uploadedFilesData.forEach((fileData) => {
                        if (fileData.data && fileData.data.matched) {
                            fileData.data.matched = fileData.data.matched.filter((m) => m !== matchStr);
                        }
                    });
                    resetOptSettingsToDefault();
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
    }

    // Unmatched
    const unmatchedRow = $('#unmatchedRow');
    const unmatchedList = $('#unmatchedList');
    const unmatchedCount = $('#unmatchedCount');
    if (uniqueUnmatched.length > 0) {
        if (unmatchedRow) unmatchedRow.style.display = 'flex';
        if (unmatchedList) {
            unmatchedList.style.display = 'block';
            unmatchedList.textContent = uniqueUnmatched.join('\n');
        }
        if (unmatchedCount) unmatchedCount.textContent = `${uniqueUnmatched.length} targets not found`;
    } else {
        if (unmatchedRow) unmatchedRow.style.display = 'none';
        if (unmatchedList) unmatchedList.style.display = 'none';
    }

    if (uniqueMatched.length > 0) {
        buildMatrixBtn.disabled = false;
        thresholdControl.style.display = 'block';
    } else {
        buildMatrixBtn.disabled = true;
        thresholdControl.style.display = 'none';
    }
}

function renderAffinityFiles() {
    if (!uploadedAffinityFilesData || uploadedAffinityFilesData.length === 0) {
        if (affinitySummary) affinitySummary.style.display = 'none';
        if (fileInfo) {
            fileInfo.style.display = 'none';
            fileInfo.innerHTML = '';
        }
        if (removeAllBtnContainer) removeAllBtnContainer.style.display = 'none';
        if (thresholdControl) thresholdControl.style.display = 'none';
        if (buildMatrixBtn) buildMatrixBtn.disabled = true;
        if (fileInput) fileInput.value = '';
        sessionStorage.removeItem('uploadedAffinityFilesData');
        return;
    }

    sessionStorage.setItem('uploadedAffinityFilesData', JSON.stringify(uploadedAffinityFilesData));

    fileInfo.innerHTML = '';
    fileInfo.style.display = 'flex';
    if (removeAllBtnContainer) {
        removeAllBtnContainer.style.display = 'block';
        if (removeAllBtn) removeAllBtn.style.display = 'inline-block';
        if (removeAllConfirm) removeAllConfirm.style.display = 'none';
    }

    // Render individual file cards
    uploadedAffinityFilesData.forEach(fileData => {
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
        deleteBtn.title = `Remove ${fileData.name}`;

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
            yesBtn.onclick = async () => {
                try {
                    const res = await fetch('/api/remove-affinity-file', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ filename: fileData.name })
                    });
                    const resData = await parseJsonResponse(res);
                    if (res.ok && Array.isArray(resData.all_files)) {
                        uploadedAffinityFilesData = resData.all_files.map(af => ({
                            name: af.name,
                            data: af,
                            allCompounds: resData.compounds || [],
                            allTargets: resData.targets || [],
                            totalCompounds: resData.num_compounds,
                            totalTargets: resData.num_targets,
                            totalDatapoints: resData.num_datapoints
                        }));
                    } else {
                        uploadedAffinityFilesData = uploadedAffinityFilesData.filter(d => d.name !== fileData.name);
                    }
                } catch (e) {
                    uploadedAffinityFilesData = uploadedAffinityFilesData.filter(d => d.name !== fileData.name);
                }
                resetOptSettingsToDefault();
                renderAffinityFiles();
            };

            const noBtn = document.createElement('button');
            noBtn.className = 'btn btn-secondary';
            noBtn.style.padding = '0.25rem 0.75rem';
            noBtn.style.fontSize = '0.85rem';
            noBtn.style.minWidth = '50px';
            noBtn.textContent = 'No';
            noBtn.onclick = () => renderAffinityFiles();

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

    // Reconcile cumulative lists and counts
    let allCompounds = [];
    let allTargets = [];
    let totalCompounds = 0;
    let totalTargets = 0;
    let totalDatapoints = 0;

    if (uploadedAffinityFilesData.length > 0 && uploadedAffinityFilesData[0].allCompounds) {
        allCompounds = uploadedAffinityFilesData[0].allCompounds || [];
        allTargets = uploadedAffinityFilesData[0].allTargets || [];
        totalCompounds = uploadedAffinityFilesData[0].totalCompounds !== undefined ? uploadedAffinityFilesData[0].totalCompounds : allCompounds.length;
        totalTargets = uploadedAffinityFilesData[0].totalTargets !== undefined ? uploadedAffinityFilesData[0].totalTargets : allTargets.length;
        totalDatapoints = uploadedAffinityFilesData[0].totalDatapoints || 0;
    } else {
        uploadedAffinityFilesData.forEach(f => {
            if (f.data) {
                if (Array.isArray(f.data.compounds)) allCompounds.push(...f.data.compounds);
                if (Array.isArray(f.data.targets)) allTargets.push(...f.data.targets);
                totalDatapoints += (f.data.num_datapoints || 0);
            }
        });
        allCompounds = [...new Set(allCompounds)];
        allTargets = [...new Set(allTargets)];
        totalCompounds = allCompounds.length;
        totalTargets = allTargets.length;
    }

    if (totalTargets === 0 && totalCompounds === 0) {
        if (affinitySummary) affinitySummary.style.display = 'none';
        if (thresholdControl) thresholdControl.style.display = 'none';
        if (buildMatrixBtn) buildMatrixBtn.disabled = true;
        return;
    }

    if (affinitySummary) {
        affinitySummary.style.display = 'block';

        const cmpdCountEl = $('#affinityCompoundCount');
        const tgtCountEl = $('#affinityTargetCount');
        const ptsCountEl = $('#affinityPointsCount');
        const cmpdListEl = $('#affinityCompoundList');
        const tgtListEl = $('#affinityTargetList');

        if (cmpdCountEl) cmpdCountEl.textContent = totalCompounds;
        if (tgtCountEl) tgtCountEl.textContent = totalTargets;
        if (ptsCountEl) ptsCountEl.textContent = totalDatapoints;

        // Compound List
        if (cmpdListEl) {
            cmpdListEl.innerHTML = '';
            allCompounds.forEach(c => {
                const item = document.createElement('div');
                item.className = 'target-list-item';

                const textSpan = document.createElement('span');
                textSpan.textContent = c;

                const delBtn = document.createElement('span');
                delBtn.textContent = '❌';
                delBtn.className = 'target-list-delete';
                delBtn.title = 'Remove compound';
                delBtn.onclick = () => {
                    item.innerHTML = '';

                    const compoundName = c.split(' ->')[0];
                    const msg = document.createElement('span');
                    msg.textContent = `Are you sure you want to remove the compound ${compoundName}?`;
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
                    yesBtn.onclick = async () => {
                        try {
                            const res = await fetch('/api/remove-affinity-compound', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ compound: c }),
                            });
                            const resData = await parseJsonResponse(res);
                            if (res.ok && Array.isArray(resData.all_files)) {
                                uploadedAffinityFilesData = resData.all_files.map(af => ({
                                    name: af.name,
                                    data: af,
                                    allCompounds: resData.compounds || [],
                                    allTargets: resData.targets || [],
                                    totalCompounds: resData.num_compounds,
                                    totalTargets: resData.num_targets,
                                    totalDatapoints: resData.num_datapoints
                                }));
                                if (resData.num_compounds === 0 || resData.num_targets === 0) {
                                    uploadedAffinityFilesData = [];
                                    sessionStorage.removeItem('uploadedAffinityFilesData');
                                }
                            }
                        } catch (e) { }
                        resetOptSettingsToDefault();
                        renderAffinityFiles();
                    };

                    const noBtn = document.createElement('button');
                    noBtn.className = 'btn btn-secondary';
                    noBtn.style.padding = '0.15rem 0.5rem';
                    noBtn.style.fontSize = '0.75rem';
                    noBtn.style.minWidth = '40px';
                    noBtn.textContent = 'No';
                    noBtn.onclick = () => renderAffinityFiles();

                    btnContainer.appendChild(yesBtn);
                    btnContainer.appendChild(noBtn);

                    item.appendChild(msg);
                    item.appendChild(btnContainer);
                };

                item.appendChild(textSpan);
                item.appendChild(delBtn);
                cmpdListEl.appendChild(item);
            });
        }

        // Target List
        if (tgtListEl) {
            tgtListEl.innerHTML = '';
            allTargets.forEach(t => {
                const item = document.createElement('div');
                item.className = 'target-list-item';

                const textSpan = document.createElement('span');
                textSpan.textContent = t;

                const delBtn = document.createElement('span');
                delBtn.textContent = '❌';
                delBtn.className = 'target-list-delete';
                delBtn.title = 'Remove target';
                delBtn.onclick = () => {
                    item.innerHTML = '';

                    const targetName = t.split(' ->')[0];
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
                    yesBtn.onclick = async () => {
                        try {
                            const res = await fetch('/api/remove-affinity-target', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ target: t }),
                            });
                            const resData = await parseJsonResponse(res);
                            if (res.ok && Array.isArray(resData.all_files)) {
                                uploadedAffinityFilesData = resData.all_files.map(af => ({
                                    name: af.name,
                                    data: af,
                                    allCompounds: resData.compounds || [],
                                    allTargets: resData.targets || [],
                                    totalCompounds: resData.num_compounds,
                                    totalTargets: resData.num_targets,
                                    totalDatapoints: resData.num_datapoints
                                }));
                                if (resData.num_compounds === 0 || resData.num_targets === 0) {
                                    uploadedAffinityFilesData = [];
                                    sessionStorage.removeItem('uploadedAffinityFilesData');
                                }
                            }
                        } catch (e) { }
                        resetOptSettingsToDefault();
                        renderAffinityFiles();
                    };

                    const noBtn = document.createElement('button');
                    noBtn.className = 'btn btn-secondary';
                    noBtn.style.padding = '0.15rem 0.5rem';
                    noBtn.style.fontSize = '0.75rem';
                    noBtn.style.minWidth = '40px';
                    noBtn.textContent = 'No';
                    noBtn.onclick = () => renderAffinityFiles();

                    btnContainer.appendChild(yesBtn);
                    btnContainer.appendChild(noBtn);

                    item.appendChild(msg);
                    item.appendChild(btnContainer);
                };

                item.appendChild(textSpan);
                item.appendChild(delBtn);
                tgtListEl.appendChild(item);
            });
        }
    }

    if (totalTargets >= 2 && totalCompounds > 0) {
        thresholdControl.style.display = 'block';
        buildMatrixBtn.disabled = false;
    } else {
        thresholdControl.style.display = 'none';
        buildMatrixBtn.disabled = true;
    }
}

// Build Matrix button
buildMatrixBtn.addEventListener('click', async () => {
    buildMatrixBtn.disabled = true;
    resetOptSettingsToDefault();
    const removeTargets = document.getElementById('removeTargets')?.checked ?? true;

    if (uploadMode === 'affinity') {
        const body = {
            selectivity_threshold: parseFloat(selectivityThreshold.value) || 0.0,
            remove_targets: removeTargets,
        };

        try {
            const res = await fetch('/api/build-matrix-from-affinity', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await parseJsonResponse(res);

            if (!res.ok) {
                showError(uploadError, data.error || 'Failed to start affinity pipeline');
                buildMatrixBtn.disabled = false;
                return;
            }

            goToStep(2);
            startPipelinePolling();
        } catch (err) {
            showError(uploadError, `Network error: ${err.message}`);
            buildMatrixBtn.disabled = false;
        }
        return;
    }

    const body = {
        chembl_ids: uploadedChemblIds,
        selectivity_threshold: parseFloat(selectivityThreshold.value) || 0.5,
        remove_targets: removeTargets,
        matched_count: uploadedMatchedCount
    };

    try {
        const res = await fetch('/api/build-matrix', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await parseJsonResponse(res);

        if (!res.ok) {
            showError(uploadError, data.error || 'Failed to start pipeline');
            buildMatrixBtn.disabled = false;
            return;
        }

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

    // Reset title
    $('#matrixTitle').innerHTML = '<span class="icon">⚙️</span> Building Selectivity Matrix';

    // Set step 1 label dynamically based on mode
    const step1NameEl = document.querySelector('.pipeline-step[data-pipeline="1"] .step-name');
    if (step1NameEl) {
        step1NameEl.textContent = uploadMode === 'affinity'
            ? 'Calculating selectivity matrix'
            : 'Searching for selective compounds';
    }

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

        // Update title to indicate completion
        $('#matrixTitle').innerHTML = '<span class="icon">✅</span> Selectivity Matrix Complete';
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

    $('#historyCard').style.display = 'none';
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
const optStatusIndicator = $('#optStatusIndicator');
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
        xaxis: { title: 'Generation', gridcolor: 'rgba(255,255,255,0.05)', automargin: true, fixedrange: true },
        yaxis: { title: 'Best Selectivity Score', gridcolor: 'rgba(255,255,255,0.05)', color: '#12f3b9', automargin: true, fixedrange: true },
        yaxis2: { title: 'Lowest Cost (USD)', overlaying: 'y', side: 'right', color: '#9d7cff', gridcolor: 'rgba(0,0,0,0)', automargin: true, fixedrange: true },
        showlegend: true,
        legend: { x: 0, y: 1.1, orientation: 'h' },
        dragmode: false
    };

    const traces = [
        { x: [], y: [], name: 'Selectivity', mode: 'lines+markers', line: { color: '#12f3b9' }, marker: { size: 4 } },
        { x: [], y: [], name: 'Cost', mode: 'lines+markers', line: { color: '#9d7cff' }, yaxis: 'y2', marker: { size: 4 } }
    ];

    Plotly.newPlot('historyChart', traces, layout, {
        responsive: true,
        displayModeBar: false,
        scrollZoom: false,
        doubleClick: false
    });
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

// ─── Optimization Settings Persistence & Reset ───
function resetOptSettingsToDefault() {
    try {
        localStorage.removeItem('optilib_opt_settings');
    } catch (e) {
        console.error('Failed to remove stored opt settings', e);
    }

    if (weightMean) weightMean.value = '0.5';
    if (weightMeanValue) weightMeanValue.value = '0.5';
    if (weightMin) weightMin.value = '0.5';
    if (weightMinValue) weightMinValue.value = '0.5';
    updateFormula();

    if (allowedMiss) allowedMiss.value = 10;
    if (allowedMissValue) allowedMissValue.value = 10;

    const ftolEl = $('#ftol');
    if (ftolEl) ftolEl.value = '0.0025';

    const maxPriceToggleEl = $('#maxPriceToggle');
    const maxPriceContainer = $('#maxPriceInputContainer');
    const maxPriceValEl = $('#maxPriceValue');
    if (maxPriceToggleEl) maxPriceToggleEl.checked = false;
    if (maxPriceContainer) maxPriceContainer.style.display = 'none';
    if (maxPriceValEl) maxPriceValEl.value = '1000';

    const advToggleEl = $('#advancedSettingsToggle');
    const advContainer = $('#advancedSettingsContainer');
    if (advToggleEl) advToggleEl.checked = false;
    if (advContainer) advContainer.style.display = 'none';

    const popSizeEl = $('#popSize');
    if (popSizeEl) popSizeEl.value = '100';

    const mutEl = $('#mutationMultiplier');
    if (mutEl) mutEl.value = '1.0';

    const maxGenEl = $('#maxGen');
    if (maxGenEl) maxGenEl.value = '1000';

    const termEl = $('#termPeriod');
    if (termEl) termEl.value = '30';
}

function saveOptSettings() {
    try {
        const ftolEl = $('#ftol');
        const maxPriceToggleEl = $('#maxPriceToggle');
        const maxPriceValEl = $('#maxPriceValue');
        const advToggleEl = $('#advancedSettingsToggle');
        const popSizeEl = $('#popSize');
        const mutEl = $('#mutationMultiplier');
        const maxGenEl = $('#maxGen');
        const termEl = $('#termPeriod');

        const settings = {
            weightMean: weightMean ? weightMean.value : '0.5',
            weightMin: weightMin ? weightMin.value : '0.5',
            allowedMiss: allowedMiss ? allowedMiss.value : '10',
            ftol: ftolEl ? ftolEl.value : '0.0025',
            maxPriceEnabled: maxPriceToggleEl ? maxPriceToggleEl.checked : false,
            maxPriceValue: maxPriceValEl ? maxPriceValEl.value : '1000',
            advancedSettingsEnabled: advToggleEl ? advToggleEl.checked : false,
            popSize: popSizeEl ? popSizeEl.value : '100',
            mutationMultiplier: mutEl ? mutEl.value : '1.0',
            maxGen: maxGenEl ? maxGenEl.value : '1000',
            termPeriod: termEl ? termEl.value : '30',
        };
        localStorage.setItem('optilib_opt_settings', JSON.stringify(settings));
    } catch (e) {
        console.error('Failed to save optimization settings', e);
    }
}

function restoreOptSettings() {
    try {
        const saved = localStorage.getItem('optilib_opt_settings');
        if (!saved) return;
        const settings = JSON.parse(saved);
        if (!settings || typeof settings !== 'object') return;

        if (settings.weightMean !== undefined && weightMean && weightMeanValue) {
            const val = parseFloat(settings.weightMean);
            if (!isNaN(val)) {
                const clamped = Math.max(0, Math.min(1, val));
                weightMean.value = clamped.toFixed(1);
                weightMeanValue.value = clamped.toFixed(1);
                if (weightMin && weightMinValue) {
                    weightMin.value = (1 - clamped).toFixed(1);
                    weightMinValue.value = (1 - clamped).toFixed(1);
                }
                updateFormula();
            }
        }

        if (settings.allowedMiss !== undefined && allowedMiss && allowedMissValue) {
            const val = parseInt(settings.allowedMiss, 10);
            if (!isNaN(val)) {
                const clamped = Math.max(0, Math.min(20, val));
                allowedMiss.value = clamped;
                allowedMissValue.value = clamped;
            }
        }

        const ftolEl = $('#ftol');
        if (settings.ftol !== undefined && ftolEl) {
            const val = parseFloat(settings.ftol);
            if (!isNaN(val)) {
                ftolEl.value = val;
            }
        }

        const maxPriceToggleEl = $('#maxPriceToggle');
        const maxPriceContainer = $('#maxPriceInputContainer');
        if (settings.maxPriceEnabled !== undefined && maxPriceToggleEl) {
            maxPriceToggleEl.checked = !!settings.maxPriceEnabled;
            if (maxPriceContainer) {
                maxPriceContainer.style.display = maxPriceToggleEl.checked ? 'block' : 'none';
            }
        }

        const maxPriceValEl = $('#maxPriceValue');
        if (settings.maxPriceValue !== undefined && maxPriceValEl) {
            const val = parseFloat(settings.maxPriceValue);
            if (!isNaN(val)) {
                maxPriceValEl.value = val;
            }
        }

        const advToggleEl = $('#advancedSettingsToggle');
        const advContainer = $('#advancedSettingsContainer');
        if (settings.advancedSettingsEnabled !== undefined && advToggleEl) {
            advToggleEl.checked = !!settings.advancedSettingsEnabled;
            if (advContainer) {
                advContainer.style.display = advToggleEl.checked ? 'block' : 'none';
            }
        }

        const popSizeEl = $('#popSize');
        if (settings.popSize !== undefined && popSizeEl) {
            const val = parseInt(settings.popSize, 10);
            if (!isNaN(val)) popSizeEl.value = val;
        }

        const mutEl = $('#mutationMultiplier');
        if (settings.mutationMultiplier !== undefined && mutEl) {
            const val = parseFloat(settings.mutationMultiplier);
            if (!isNaN(val)) mutEl.value = val;
        }

        const maxGenEl = $('#maxGen');
        if (settings.maxGen !== undefined && maxGenEl) {
            const val = parseInt(settings.maxGen, 10);
            if (!isNaN(val)) maxGenEl.value = val;
        }

        const termEl = $('#termPeriod');
        if (settings.termPeriod !== undefined && termEl) {
            const val = parseInt(settings.termPeriod, 10);
            if (!isNaN(val)) termEl.value = val;
        }
    } catch (e) {
        console.error('Failed to restore optimization settings', e);
    }
}

function setupOptSettings() {
    restoreOptSettings();

    // Weight Mean & Min listeners
    weightMean.addEventListener('input', () => {
        let val = parseFloat(weightMean.value);
        weightMeanValue.value = val.toFixed(1);
        weightMin.value = (1 - val).toFixed(1);
        weightMinValue.value = (1 - val).toFixed(1);
        updateFormula();
        saveOptSettings();
    });

    weightMeanValue.addEventListener('input', () => {
        let val = parseFloat(weightMeanValue.value);
        if (!isNaN(val)) {
            weightMean.value = val.toFixed(1);
            weightMin.value = (1 - val).toFixed(1);
            weightMinValue.value = (1 - val).toFixed(1);
            updateFormula();
            saveOptSettings();
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
            updateFormula();
            saveOptSettings();
        }
    });

    weightMin.addEventListener('input', () => {
        let val = parseFloat(weightMin.value);
        weightMinValue.value = val.toFixed(1);
        weightMean.value = (1 - val).toFixed(1);
        weightMeanValue.value = (1 - val).toFixed(1);
        updateFormula();
        saveOptSettings();
    });

    weightMinValue.addEventListener('input', () => {
        let val = parseFloat(weightMinValue.value);
        if (!isNaN(val)) {
            weightMin.value = val.toFixed(1);
            weightMean.value = (1 - val).toFixed(1);
            weightMeanValue.value = (1 - val).toFixed(1);
            updateFormula();
            saveOptSettings();
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
            updateFormula();
            saveOptSettings();
        }
    });

    // Allowed Miss listeners
    allowedMiss.addEventListener('input', () => {
        allowedMissValue.value = parseInt(allowedMiss.value);
        saveOptSettings();
    });

    allowedMissValue.addEventListener('input', () => {
        let val = Math.round(parseFloat(allowedMissValue.value));
        if (!isNaN(val)) {
            allowedMiss.value = val;
            saveOptSettings();
        }
    });

    allowedMissValue.addEventListener('change', () => {
        let val = Math.round(parseFloat(allowedMissValue.value));
        if (!isNaN(val)) {
            val = Math.max(0, Math.min(20, val));
            allowedMissValue.value = val;
            allowedMiss.value = val;
            saveOptSettings();
        }
    });

    // F-Tolerance
    const ftolEl = $('#ftol');
    if (ftolEl) {
        ftolEl.addEventListener('input', saveOptSettings);
        ftolEl.addEventListener('change', saveOptSettings);
    }

    // Maximum Price Limit
    const maxPriceToggleEl = $('#maxPriceToggle');
    const maxPriceContainer = $('#maxPriceInputContainer');
    if (maxPriceToggleEl) {
        maxPriceToggleEl.addEventListener('change', (e) => {
            if (maxPriceContainer) {
                maxPriceContainer.style.display = e.target.checked ? 'block' : 'none';
            }
            saveOptSettings();
        });
    }

    const maxPriceValEl = $('#maxPriceValue');
    if (maxPriceValEl) {
        maxPriceValEl.addEventListener('input', saveOptSettings);
        maxPriceValEl.addEventListener('change', saveOptSettings);
    }

    // Advanced Settings
    const advToggleEl = $('#advancedSettingsToggle');
    const advContainer = $('#advancedSettingsContainer');
    if (advToggleEl) {
        advToggleEl.addEventListener('change', (e) => {
            if (advContainer) {
                advContainer.style.display = e.target.checked ? 'block' : 'none';
            }
            saveOptSettings();
        });
    }

    ['#popSize', '#mutationMultiplier', '#maxGen', '#termPeriod'].forEach(sel => {
        const el = $(sel);
        if (el) {
            el.addEventListener('input', saveOptSettings);
            el.addEventListener('change', saveOptSettings);
        }
    });
}

// Formula display helper
function updateFormula() {
    const fMean = document.getElementById('formulaWeightMean');
    const fMin = document.getElementById('formulaWeightMin');
    if (fMean) fMean.textContent = parseFloat(weightMean.value).toFixed(1);
    if (fMin) fMin.textContent = parseFloat(weightMin.value).toFixed(1);
}

async function loadDatasetInfo() {
    try {
        const res = await fetch('/api/dataset-info');
        const data = await res.json();

        $('#statDrugs').textContent = data.num_drugs.toLocaleString();
        $('#statTargets').textContent = data.num_targets.toLocaleString();
        $('#statCost').textContent = `$${data.total_cost.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

        // Update pool cost reference in the Maximum Price Limit section
        const poolCostEl = $('#maxPricePoolCost');
        if (poolCostEl) {
            poolCostEl.textContent = `$${data.total_cost.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
        }
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
    saveOptSettings();
    runOptBtn.disabled = true;
    runOptBtn.style.display = 'none';
    $('#stopOptBtn').style.display = 'inline-flex';
    $('#stopOptBtn').disabled = false;

    optError.style.display = 'none';

    initHistoryChart();

    const maxPriceEnabled = $('#maxPriceToggle') && $('#maxPriceToggle').checked;
    const body = {
        weight_mean: parseFloat(weightMean.value),
        allowed_miss_pct: parseInt(allowedMiss.value) / 100.0,
        mutation_multiplier: parseFloat($('#mutationMultiplier').value),
        pop_size: parseInt($('#popSize').value),
        max_gen: parseInt($('#maxGen').value),
        ftol: parseFloat($('#ftol').value),
        term_period: parseInt($('#termPeriod').value),
        max_price: maxPriceEnabled ? parseFloat($('#maxPriceValue').value) : null,
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
            optStatusIndicator.style.visibility = 'hidden';
            return;
        }

        startOptPolling(body.max_gen);

    } catch (err) {
        showError(optError, `Network error: ${err.message}`);
        runOptBtn.disabled = false;
        runOptBtn.style.display = 'inline-flex';
        $('#stopOptBtn').style.display = 'none';
        optStatusIndicator.style.visibility = 'hidden';
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

            // Update status indicator
            if (data.status === 'running') {
                optStatusIndicator.style.visibility = 'visible';
                const currentGen = data.generation || 0;
                $('#optStatusText').textContent = `Optimizing... Generation ${currentGen}`;
            }

            if (data.status === 'complete') {
                clearInterval(optPollTimer);
                optPollTimer = null;
                optStatusIndicator.style.visibility = 'hidden';
                $('#stopOptBtn').style.display = 'none';
                runOptBtn.style.display = 'none';

                // Show the completion banner instead of auto-navigating
                const finalGen = data.generation || '?';
                showOptCompleteBanner(finalGen);
            }

            if (data.status === 'error') {
                clearInterval(optPollTimer);
                optPollTimer = null;
                showError(optError, data.error || 'Optimization failed');
                optStatusIndicator.style.visibility = 'hidden';
                $('#stopOptBtn').style.display = 'none';
                runOptBtn.style.display = 'inline-flex';
                runOptBtn.disabled = false;
            }

        } catch (err) {
            // Silent retry
        }
    }, 500);
}

function showOptCompleteBanner(generations) {
    const banner = $('#optCompleteBanner');
    const text = $('#optCompleteText');
    text.textContent = `Optimization Complete: The Algorithm Ran For ${generations} Generations`;
    banner.style.display = '';
    // Trigger entrance animation
    requestAnimationFrame(() => {
        banner.classList.add('visible');
    });
    // Hide the Run Optimization button since we're done, show Run Again & See Results
    runOptBtn.style.display = 'none';
    $('#runAgainBtn').style.display = 'inline-flex';
    $('#seeResultsBtn').style.display = 'inline-flex';
}

// See Results button
$('#seeResultsBtn').addEventListener('click', async () => {
    goToStep(4);
    await loadResults();
});

// Run Again button
$('#runAgainBtn').addEventListener('click', async () => {
    // Hide the completion banner and buttons
    $('#optCompleteBanner').style.display = 'none';
    $('#optCompleteBanner').classList.remove('visible');
    $('#runAgainBtn').style.display = 'none';
    $('#seeResultsBtn').style.display = 'none';

    // Reset backend optimization state
    try {
        await fetch('/api/reset-opt', { method: 'POST' });
    } catch (err) {
        console.error('Failed to reset opt state', err);
    }

    // Trigger the optimization as if the user clicked "Run Optimization"
    runOptBtn.style.display = 'inline-flex';
    runOptBtn.disabled = false;
    runOptBtn.click();
});

async function resetAllState() {
    try {
        await fetch('/api/reset', { method: 'POST' });
    } catch (err) {
        console.error('Failed to reset backend state', err);
    }

    if (optPollTimer) {
        clearInterval(optPollTimer);
        optPollTimer = null;
    }
    if (pipelinePollTimer) {
        clearInterval(pipelinePollTimer);
        pipelinePollTimer = null;
    }

    // Clear session storage
    sessionStorage.removeItem('uploadedFilesData');
    sessionStorage.removeItem('uploadedAffinityFilesData');
    sessionStorage.removeItem('uploadedAffinityData');
    sessionStorage.removeItem('uploadedPriceFilesData');
    sessionStorage.removeItem('uploadedPriceData');
    sessionStorage.removeItem('currentStep');

    // Clear state variables
    uploadedFilesData = [];
    uploadedAffinityFilesData = [];
    uploadedAffinityData = null;
    uploadedPriceFilesData = [];
    uploadedPriceData = null;
    uploadedChemblIds = [];
    uploadedMatchedCount = 0;

    // Reset file inputs and UI badges
    if (fileInput) fileInput.value = '';
    const priceFileInput = $('#priceFileInput');
    if (priceFileInput) priceFileInput.value = '';
    const priceFileInfo = $('#priceFileInfo');
    if (priceFileInfo) {
        priceFileInfo.style.display = 'none';
        priceFileInfo.innerHTML = '';
    }
    const priceRemoveAllBtnContainer = $('#priceRemoveAllBtnContainer');
    if (priceRemoveAllBtnContainer) priceRemoveAllBtnContainer.style.display = 'none';
    const priceValidationSummary = $('#priceValidationSummary');
    if (priceValidationSummary) priceValidationSummary.style.display = 'none';

    if (uploadError) uploadError.style.display = 'none';
    const pipelineError = $('#pipelineError');
    if (pipelineError) pipelineError.style.display = 'none';
    if (optError) optError.style.display = 'none';

    if (affinitySummary) affinitySummary.style.display = 'none';
    if (validationSummary) validationSummary.style.display = 'none';
    if (fileInfo) fileInfo.style.display = 'none';
    if (removeAllBtnContainer) removeAllBtnContainer.style.display = 'none';
    if (thresholdControl) thresholdControl.style.display = 'none';
    if (buildMatrixBtn) buildMatrixBtn.disabled = true;

    // Reset pipeline UI
    resetPipelineUI();

    // Reset Step 3 elements
    $('#historyCard').style.display = 'none';
    optStatusIndicator.style.visibility = 'hidden';
    runOptBtn.style.display = 'inline-flex';
    runOptBtn.disabled = false;
    $('#stopOptBtn').style.display = 'none';
    $('#runAgainBtn').style.display = 'none';
    $('#seeResultsBtn').style.display = 'none';
    $('#optCompleteBanner').style.display = 'none';
    $('#optCompleteBanner').classList.remove('visible');

    // Reset optimization settings to default
    resetOptSettingsToDefault();

    // Re-render based on current mode
    if (uploadMode === 'target') {
        renderFiles();
    }

    goToStep(1);
}

// Back button
$('#backToStep1Btn').addEventListener('click', async () => {
    await resetAllState();
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
        const thCompoundId = $('#thCompoundId');
        const hasCustomAffinity = !!(comparison && comparison.has_custom_affinity !== undefined
            ? comparison.has_custom_affinity
            : (uploadMode === 'affinity'));

        if (thCompoundId) {
            thCompoundId.style.display = hasCustomAffinity ? '' : 'none';
        }

        if (compoundsBody && lib.compounds) {
            compoundsBody.innerHTML = '';
            lib.compounds.forEach((c) => {
                const tr = document.createElement('tr');
                const compoundIdVal = (c.name && c.name !== 'Unknown') ? c.name : '—';
                const chemblVal = (c.chembl_id && c.chembl_id !== 'Unknown') ? c.chembl_id : '—';
                const inchikeyVal = (c.inchikey && c.inchikey !== 'Unknown') ? c.inchikey : '—';
                const priceVal = `$${Number(c.price || 0).toFixed(2)}`;

                if (hasCustomAffinity) {
                    tr.innerHTML = `
                        <td class="metric-name" style="font-weight: 600; color: #fff;">${compoundIdVal}</td>
                        <td class="metric-name" style="color: #a8a8b3; font-size: 0.9em;">${chemblVal}</td>
                        <td class="metric-name" style="color: #a8a8b3; font-size: 0.85em; font-family: 'JetBrains Mono', monospace;">${inchikeyVal}</td>
                        <td class="value">${priceVal}</td>
                    `;
                } else {
                    tr.innerHTML = `
                        <td class="metric-name" style="font-weight: 600; color: #fff;">${chemblVal}</td>
                        <td class="metric-name" style="color: #a8a8b3; font-size: 0.95em; font-family: 'JetBrains Mono', monospace;">${inchikeyVal}</td>
                        <td class="value">${priceVal}</td>
                    `;
                }
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

        const weightMeanVal = data.weight_mean !== undefined ? data.weight_mean : (parseFloat($('#weightMean')?.value) || 0.5);
        const weightMinVal = data.weight_min !== undefined ? data.weight_min : (parseFloat($('#weightMin')?.value) || 0.5);
        const wMean = Number(Number(weightMeanVal).toFixed(4));
        const wMin = Number(Number(weightMinVal).toFixed(4));
        const xAxisTitle = `Selectivity Score (${wMean} * Mean Selectivity + ${wMin} * Min Selectivity)`;

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
                title: { text: xAxisTitle, font: { size: 13, color: '#9898b8' } },
                gridcolor: 'rgba(120, 120, 255, 0.08)',
                zerolinecolor: 'rgba(120, 120, 255, 0.12)',
                automargin: true,
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

        const targetNames = (data.target_names || data.targets).map((name, j) => {
            const symbol = data.targets[j];
            return (name && name !== symbol) ? `${name} (${symbol})` : symbol;
        });

        const hoverText = data.matrix.map(row =>
            row.map(val => val === null ? "No Data" : val.toFixed(2))
        );

        const truncCompounds = data.compounds.map((s) => (s && s.length > 30) ? s.substring(0, 27) + '...' : (s || 'Unknown'));
        const customData = data.matrix.map((row, i) =>
            row.map((_, j) => [truncCompounds[i], targetNames[j]])
        );

        const xIndices = data.targets.map((_, j) => j);
        const yIndices = data.compounds.map((_, i) => i);

        const trace = {
            z: data.matrix,
            x: xIndices,
            y: yIndices,
            text: hoverText,
            customdata: customData,
            type: 'heatmap',
            colorscale: 'Viridis',
            showscale: true,
            hovertemplate: 'Target: %{customdata[1]}<br>Compound: %{customdata[0]}<br>Selectivity: %{text}<extra></extra>',
            colorbar: {
                tickfont: { color: '#9898b8', size: 9 },
                len: 0.85,
                thickness: 18,
                x: 1.04,
                xanchor: 'left',
                xpad: 0,
                outlinewidth: 0,
            },
        };

        const TARGET_WINDOW = 40;
        const COMPOUND_WINDOW = 20;
        const numTargets = data.targets.length;
        const numCompounds = data.compounds.length;

        const hasManyTargets = numTargets > TARGET_WINDOW;
        const hasManyCompounds = numCompounds > COMPOUND_WINDOW;

        const initialTargetCount = Math.min(TARGET_WINDOW, numTargets);
        const initialCompoundCount = Math.min(COMPOUND_WINDOW, numCompounds);

        // Setup sliders display before Plotly.newPlot so container width is accurately allocated
        const targetSliderWrapper = $('#heatmapTargetSliderWrapper');
        const targetRangeSlider = $('#heatmapTargetRangeSlider');
        const targetRangeText = $('#heatmapTargetSliderRangeText');
        const targetTotalText = $('#heatmapTargetSliderTotalText');
        const targetSubtext = $('#heatmapTargetSliderSubtext');
        const targetMinLabel = $('#heatmapTargetSliderMinLabel');
        const targetMaxLabel = $('#heatmapTargetSliderMaxLabel');

        if (targetSliderWrapper && targetRangeSlider) {
            if (hasManyTargets) {
                targetSliderWrapper.style.display = 'block';
                targetRangeSlider.min = 0;
                targetRangeSlider.max = numTargets - TARGET_WINDOW;
                targetRangeSlider.value = 0;
                if (targetTotalText) targetTotalText.textContent = `of ${numTargets}`;
                if (targetMinLabel) targetMinLabel.textContent = `1`;
                if (targetMaxLabel) targetMaxLabel.textContent = `${numTargets}`;
            } else {
                targetSliderWrapper.style.display = 'none';
            }
        }

        const compoundSliderWrapper = $('#heatmapCompoundSliderWrapper');
        const compoundRangeSlider = $('#heatmapCompoundRangeSlider');
        const compoundRangeText = $('#heatmapCompoundSliderRangeText');
        const compoundTotalText = $('#heatmapCompoundSliderTotalText');
        const compoundStartEl = $('#heatmapCompoundSliderStart');
        const compoundEndEl = $('#heatmapCompoundSliderEnd');
        const compoundSubtext = $('#heatmapCompoundSliderSubtext');
        const compoundMinLabel = $('#heatmapCompoundSliderMinLabel');
        const compoundMaxLabel = $('#heatmapCompoundSliderMaxLabel');

        if (compoundSliderWrapper && compoundRangeSlider) {
            if (hasManyCompounds) {
                compoundSliderWrapper.style.display = 'flex';
                compoundRangeSlider.min = 0;
                compoundRangeSlider.max = numCompounds - COMPOUND_WINDOW;
                compoundRangeSlider.value = 0;
                if (compoundTotalText) compoundTotalText.textContent = `of ${numCompounds}`;
                if (compoundMinLabel) compoundMinLabel.textContent = `1`;
                if (compoundMaxLabel) compoundMaxLabel.textContent = `${numCompounds}`;
            } else {
                compoundSliderWrapper.style.display = 'none';
            }
        }

        const layout = {
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(255,255,255,0.08)',
            font: { family: 'Inter, sans-serif', color: '#9898b8', size: 10 },
            xaxis: {
                title: false,
                showgrid: false,
                zeroline: false,
                showline: false,
                side: 'top',
                automargin: false,
                tickmode: 'array',
                tickvals: xIndices,
                ticktext: data.targets,
                tickfont: { color: '#9898b8', size: 10 },
                tickangle: -90,
                range: [-0.5, initialTargetCount - 0.5],
                fixedrange: true,
            },
            yaxis: {
                autorange: false,
                showgrid: false,
                zeroline: false,
                showline: false,
                tickmode: 'array',
                tickvals: yIndices,
                ticktext: truncCompounds,
                range: [initialCompoundCount - 0.5, -0.5],
                title: false,
                automargin: false,
                tickfont: { color: '#9898b8', size: 10 },
                fixedrange: true,
            },
            margin: { l: 95, r: 95, t: 65, b: 25 },
            dragmode: false,
            annotations: [
                {
                    text: 'Selectivity',
                    font: { family: 'Inter, sans-serif', size: 10, color: '#9898b8' },
                    xref: 'paper',
                    yref: 'paper',
                    x: 1.04,
                    y: 0.925,
                    yshift: -5,
                    xanchor: 'center',
                    xshift: 9,
                    yanchor: 'bottom',
                    showarrow: false,
                }
            ],
        };

        Plotly.newPlot('heatmapChart', [trace], layout, {
            responsive: true,
            displayModeBar: false,
            scrollZoom: false,
            doubleClick: false,
        });

        // Ensure proper chart dimensions inside flex container
        requestAnimationFrame(() => {
            Plotly.Plots.resize('heatmapChart');
        });

        // Attach resize observer to heatmap container if available
        const heatmapEl = document.getElementById('heatmapChart');
        if (heatmapEl && !heatmapEl._roAttached && window.ResizeObserver) {
            heatmapEl._roAttached = true;
            const ro = new ResizeObserver(() => {
                Plotly.Plots.resize('heatmapChart');
            });
            ro.observe(heatmapEl);
        }

        // Setup 40-target window slider interactions
        if (targetSliderWrapper && targetRangeSlider && hasManyTargets) {
            const updateTargetSliderView = (startIdx) => {
                const endIdx = Math.min(startIdx + TARGET_WINDOW, numTargets);
                if (targetRangeText) targetRangeText.textContent = `${startIdx + 1}–${endIdx}`;
                if (targetTotalText) targetTotalText.textContent = `of ${numTargets}`;
            };

            updateTargetSliderView(0);

            targetRangeSlider.oninput = (e) => {
                const startIdx = parseInt(e.target.value, 10);
                const endIdx = Math.min(startIdx + TARGET_WINDOW, numTargets);
                updateTargetSliderView(startIdx);
                Plotly.relayout('heatmapChart', {
                    'xaxis.range': [startIdx - 0.5, endIdx - 0.5]
                });
            };
        }

        // Setup 20-compound window slider interactions
        if (compoundSliderWrapper && compoundRangeSlider && hasManyCompounds) {
            const updateCompoundSliderView = (startIdx) => {
                const endIdx = Math.min(startIdx + COMPOUND_WINDOW, numCompounds);
                const startComp = truncCompounds[startIdx];
                const endComp = truncCompounds[endIdx - 1];
                if (compoundRangeText) compoundRangeText.textContent = `${startIdx + 1}–${endIdx}`;
                if (compoundTotalText) compoundTotalText.textContent = `of ${numCompounds}`;
                if (compoundStartEl) compoundStartEl.textContent = startComp;
                if (compoundEndEl) compoundEndEl.textContent = endComp;
                if (compoundSubtext) compoundSubtext.title = `${data.compounds[startIdx]} → ${data.compounds[endIdx - 1]}`;
            };

            updateCompoundSliderView(0);

            const vContainer = document.getElementById('heatmapVSliderContainer');
            if (vContainer && compoundRangeSlider) {
                const syncSliderHeight = () => {
                    const h = vContainer.clientHeight;
                    if (h > 40) {
                        compoundRangeSlider.style.width = `${h}px`;
                    }
                };
                syncSliderHeight();
                requestAnimationFrame(syncSliderHeight);
                setTimeout(syncSliderHeight, 50);
                setTimeout(syncSliderHeight, 150);
                setTimeout(syncSliderHeight, 400);
                if (!vContainer._roAttached && window.ResizeObserver) {
                    vContainer._roAttached = true;
                    const ro = new ResizeObserver(() => syncSliderHeight());
                    ro.observe(vContainer);
                }
            }

            compoundRangeSlider.oninput = (e) => {
                const startIdx = parseInt(e.target.value, 10);
                const endIdx = Math.min(startIdx + COMPOUND_WINDOW, numCompounds);
                updateCompoundSliderView(startIdx);
                Plotly.relayout('heatmapChart', {
                    'yaxis.range': [endIdx - 0.5, startIdx - 0.5]
                });
            };
        }

        // Handle double-click reset on heatmap
        if (heatmapEl && heatmapEl.on) {
            heatmapEl.on('plotly_doubleclick', () => false);
        }

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
        const targetNames = data.target_names || data.targets;

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

            const symbol = data.targets[j];
            const fullName = targetNames[j] || symbol;
            const displayName = (fullName && fullName !== symbol) ? `${fullName} (${symbol})` : symbol;

            stats.push({
                target: symbol,
                fullName: fullName,
                displayName: displayName,
                max: max,
                median: median,
                min: min
            });
        }

        stats.sort((a, b) => b.max - a.max);

        const x = stats.map((s, i) => i);

        const hoverTemplate = '<b>%{customdata[4]}</b><br>Max: %{customdata[0]:.2f}<br>Median: %{customdata[1]:.2f}<br>Min: %{customdata[2]:.2f}<extra></extra>';
        const customData = stats.map(s => [s.max, s.median, s.min, s.target, s.displayName]);

        const minBases = [];
        const minLens = [];
        const medBases = [];
        const medLens = [];
        const maxBases = [];
        const maxLens = [];

        stats.forEach(s => {
            const mn = s.min;
            const md = s.median;
            const mx = s.max;

            // Min Segment (Green)
            if (mn >= 0) {
                minBases.push(0);
                minLens.push(mn);
            } else {
                minBases.push(mn);
                const minTop = Math.min(md, 0);
                minLens.push(minTop - mn);
            }

            // Median Segment (Blue)
            if (md >= 0) {
                const medBase = Math.max(0, mn);
                medBases.push(medBase);
                medLens.push(md - medBase);
            } else {
                medBases.push(md);
                const medTop = Math.min(mx, 0);
                medLens.push(medTop - md);
            }

            // Max Segment (Purple)
            if (mx >= 0) {
                const maxBase = Math.max(0, md);
                maxBases.push(maxBase);
                maxLens.push(mx - maxBase);
            } else {
                maxBases.push(mx);
                maxLens.push(0 - mx);
            }
        });

        const traceMax = {
            x: x,
            y: maxLens,
            base: maxBases,
            name: 'Max',
            type: 'bar',
            marker: { color: 'rgba(132, 94, 247, 0.8)' },
            width: 0.85,
            hovertemplate: hoverTemplate,
            customdata: customData
        };

        const traceMedian = {
            x: x,
            y: medLens,
            base: medBases,
            name: 'Median',
            type: 'bar',
            marker: { color: 'rgba(77, 171, 247, 0.85)' },
            width: 0.85,
            hovertemplate: hoverTemplate,
            customdata: customData
        };

        const traceMin = {
            x: x,
            y: minLens,
            base: minBases,
            name: 'Min',
            type: 'bar',
            marker: { color: 'rgba(56, 217, 169, 0.95)' },
            width: 0.85,
            hovertemplate: hoverTemplate,
            customdata: customData
        };

        const WINDOW_SIZE = 40;
        const hasManyTargets = stats.length > WINDOW_SIZE;
        const initialVisibleCount = Math.min(WINDOW_SIZE, stats.length);

        const layout = {
            barmode: 'overlay',
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0.15)',
            font: { family: 'Inter, sans-serif', color: '#9898b8' },
            xaxis: {
                title: { text: 'Targets', font: { size: 13, color: '#9898b8' }, standoff: 15 },
                tickvals: stats.map((_, i) => i),
                ticktext: stats.map(s => s.target),
                tickfont: { color: '#9898b8', size: 10 },
                tickangle: -45,
                zeroline: false,
                gridcolor: 'rgba(120, 120, 255, 0.08)',
                range: [-0.5, initialVisibleCount - 0.5],
                automargin: true,
                fixedrange: true,
            },
            yaxis: {
                title: { text: 'Selectivity Score', font: { size: 13, color: '#9898b8' }, standoff: 5 },
                gridcolor: 'rgba(120, 120, 255, 0.08)',
                zeroline: true,
                zerolinecolor: 'rgba(255, 255, 255, 0.45)',
                zerolinewidth: 2,
                ticks: 'outside',
                ticklen: 5,
                tickcolor: 'rgba(0,0,0,0)',
                fixedrange: true,
            },
            legend: {
                font: { size: 11 },
                bgcolor: 'rgba(0,0,0,0.3)',
                bordercolor: 'rgba(120,120,255,0.1)',
                borderwidth: 1,
            },
            margin: { l: 55, r: 30, t: 20, b: 70 },
            hovermode: 'closest',
            dragmode: false
        };

        Plotly.newPlot('distributionChart', [traceMax, traceMedian, traceMin], layout, {
            responsive: true,
            displayModeBar: false,
            scrollZoom: false,
            doubleClick: false
        });

        // Setup 40-target window slider
        const sliderWrapper = $('#distributionSliderWrapper');
        const rangeSlider = $('#distributionRangeSlider');
        const rangeText = $('#distributionSliderRangeText');
        const totalText = $('#distributionSliderTotalText');
        const targetsSubtext = $('#distributionSliderTargetsSubtext');
        const minLabel = $('#distributionSliderMinLabel');
        const maxLabel = $('#distributionSliderMaxLabel');

        if (sliderWrapper && rangeSlider) {
            if (hasManyTargets) {
                sliderWrapper.style.display = 'block';
                rangeSlider.min = 0;
                rangeSlider.max = stats.length - WINDOW_SIZE;
                rangeSlider.value = 0;
                if (totalText) totalText.textContent = `of ${stats.length}`;
                if (minLabel) minLabel.textContent = `1`;
                if (maxLabel) maxLabel.textContent = `${stats.length}`;

                const updateSliderView = (startIdx) => {
                    const endIdx = Math.min(startIdx + WINDOW_SIZE, stats.length);
                    if (rangeText) rangeText.textContent = `${startIdx + 1}–${endIdx}`;
                    if (totalText) totalText.textContent = `of ${stats.length}`;
                };

                updateSliderView(0);

                rangeSlider.oninput = (e) => {
                    const startIdx = parseInt(e.target.value, 10);
                    const endIdx = Math.min(startIdx + WINDOW_SIZE, stats.length);
                    updateSliderView(startIdx);
                    Plotly.relayout('distributionChart', {
                        'xaxis.range': [startIdx - 0.5, endIdx - 0.5]
                    });
                };
            } else {
                sliderWrapper.style.display = 'none';
            }
        }

    } catch (err) {
        console.error('Failed to load distribution chart:', err);
    }
}


// Navigation buttons on step 4
$('#backToStep3Btn').addEventListener('click', async () => {
    try {
        await fetch('/api/reset-opt', { method: 'POST' });
    } catch (err) { }

    $('#historyCard').style.display = 'none';
    $('#optCompleteBanner').style.display = 'none';
    $('#optCompleteBanner').classList.remove('visible');
    runOptBtn.disabled = false;
    optStatusIndicator.style.visibility = 'hidden';
    goToStep(3);
    await loadDatasetInfo();
});

// New run button
$('#newRunBtn').addEventListener('click', async () => {
    await resetAllState();
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

// Global window resize listener to ensure all Plotly charts stay responsive and within bounds
window.addEventListener('resize', () => {
    ['historyChart', 'paretoChart', 'heatmapChart', 'distributionChart'].forEach((id) => {
        const el = document.getElementById(id);
        if (el && el.data) {
            Plotly.Plots.resize(el);
        }
    });
});

// End of app.js
