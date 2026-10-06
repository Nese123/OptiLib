/* Public-service adapters: asynchronous jobs, bounded pages and heatmap tiles. */
(() => {
    const apiFetch = window.fetch;
    let pendingSelections = 0;
    let sessionId = document.querySelector('meta[name="optilib-session"]')?.content;
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const shouldWait = url => /\/api\/(upload-|remove-|clear-|select-solution)/.test(String(url));
    async function waitForJob(data, signal) {
        while (true) {
            if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
            const response = await apiFetch(data.status_url, {signal});
            const job = await response.json();
            if (!response.ok) throw new Error(job.error || 'Could not check job status.');
            if (job.status === 'complete') return job.result;
            if (['error', 'cancelled'].includes(job.status)) throw new Error(job.error || 'The job was cancelled.');
            await sleep(1000);
        }
    }
    window.fetch = async (url, options = {}) => {
        const isSelection = String(url) === '/api/select-solution';
        if (isSelection) pendingSelections++;
        try {
        if (String(url).startsWith('/api/download/')) {
            options = {...options, headers: new Headers(options.headers || {})};
            options.headers.set('X-CSRFToken', document.querySelector('meta[name="csrf-token"]')?.content || '');
        }
        const response = await apiFetch(url, options);
        const serverSession = response.headers.get('X-Optilib-Session');
        if (serverSession && sessionId && serverSession !== sessionId && String(url) !== '/api/reset') {
            sessionStorage.clear();
            window.location.reload();
            throw new Error('Your temporary session ended.');
        }
        if (serverSession) {
            sessionId = serverSession;
            const previous = sessionStorage.getItem('optilibSession');
            if (previous && previous !== sessionId && String(url) !== '/api/reset') {
                sessionStorage.clear();
                sessionStorage.setItem('optilibSession', sessionId);
                window.location.reload();
                throw new Error('Your temporary session ended.');
            }
            sessionStorage.setItem('optilibSession', sessionId);
        }
        if (response.status === 202 && shouldWait(url)) {
            try {
                const result = await waitForJob(await response.json(), options.signal);
                return new Response(JSON.stringify(result), {status: 200, headers: {'Content-Type': 'application/json'}});
            } catch (error) {
                if (error.name === 'AbortError') throw error;
                return new Response(JSON.stringify({error: error.message}), {status: 400, headers: {'Content-Type': 'application/json'}});
            }
        }
        return response;
        } finally {
            if (isSelection) pendingSelections--;
        }
    };

    // Async downloads remain links for accessibility, with work shown on click.
    document.addEventListener('click', async event => {
        const link = event.target.closest('a[href^="/api/download/"]');
        if (!link) return;
        event.preventDefault();
        if (link.getAttribute('aria-busy') === 'true') return;
        const original = Array.from(link.childNodes);
        link.setAttribute('aria-busy', 'true'); link.textContent = 'Preparing download…';
        try {
            while (pendingSelections) await sleep(100);
            const downloadUrl = link.getAttribute('href');
            const prepareUrl = new URL(downloadUrl, window.location.href);
            prepareUrl.searchParams.set('prepare', '1');
            const response = await window.fetch(`${prepareUrl.pathname}${prepareUrl.search}`);
            if (response.status === 202) await waitForJob(await response.json());
            else if (!response.ok) throw new Error((await response.json()).error || 'Download failed.');
            // Navigate to the completed artifact; never buffer a large blob in JS.
            window.location.assign(downloadUrl);
        } catch (error) {
            let notice = document.getElementById('publicJobNotice');
            if (!notice) {
                notice = document.createElement('p'); notice.id = 'publicJobNotice';
                notice.className = 'session-notice'; notice.setAttribute('role', 'alert');
                document.querySelector('.workspace-header').append(notice);
            }
            notice.textContent = error.message;
        } finally {
            link.replaceChildren(...original); link.removeAttribute('aria-busy');
        }
    });

    const originalComparison = loadComparison;
    let compoundOffset = 0;
    loadComparison = async function() {
        compoundOffset = 0;
        await originalComparison();
        const table = document.getElementById('compoundsListBody')?.closest('table');
        if (!table) return;
        document.getElementById('compoundPaging')?.remove();
        const controls = document.createElement('div'); controls.id = 'compoundPaging'; controls.className = 'public-pagination';
        const previous = document.createElement('button'), next = document.createElement('button'), label = document.createElement('span');
        previous.className = next.className = 'btn btn-secondary';
        previous.textContent = 'Previous compounds'; next.textContent = 'Next compounds';
        controls.append(previous, label, next); table.after(controls);
        async function page(offset) {
            const response = await window.fetch(`/api/results?offset=${offset}&limit=100`);
            if (!response.ok) return;
            const data = await response.json();
            compoundOffset = offset;
            const body = document.getElementById('compoundsListBody'); body.replaceChildren();
            const custom = data.comparison.has_custom_affinity;
            for (const item of data.comparison.library.compounds) {
                const row = document.createElement('tr');
                const values = custom ? [item.name,item.chembl_id,item.inchikey,`$${item.price.toFixed(2)}`] : [item.chembl_id,item.inchikey,`$${item.price.toFixed(2)}`];
                values.forEach(value => { const cell = document.createElement('td'); cell.textContent = value || '—'; row.append(cell); });
                body.append(row);
            }
            label.textContent = `${offset + 1}–${Math.min(offset + 100, data.total)} of ${data.total}`;
            previous.disabled = offset === 0; next.disabled = offset + 100 >= data.total;
        }
        previous.onclick = () => page(Math.max(0, compoundOffset - 100)).catch(console.error);
        next.onclick = () => page(compoundOffset + 100).catch(console.error);
        await page(0);
    };

})();
