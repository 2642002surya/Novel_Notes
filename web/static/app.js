let currentPage = 1;
let debounceTimer = null;
let currentLayout = 'table';
let selectedNovelIds = new Set();
let cachedNovels = [];
let activeAbortController = null;

document.addEventListener("DOMContentLoaded", () => {
    setupNavigation();
    setupSidebarToggle();
    setupShortcuts();
    loadNovels();
});

function setupSidebarToggle() {
    const toggleBtn = document.getElementById("sidebarToggle");
    const sidebar = document.getElementById("sidebar");
    if (toggleBtn && sidebar) {
        toggleBtn.addEventListener("click", () => {
            sidebar.classList.toggle("open");
        });
    }
}

function setupNavigation() {
    document.querySelectorAll('.sidebar-nav .nav-item').forEach(link => {
        link.addEventListener('click', (e) => {
            const status = link.getAttribute('data-filter-status');
            const theme = link.getAttribute('data-filter-theme');
            const fav = link.getAttribute('data-filter-fav');
            const view = link.getAttribute('data-view');

            document.querySelectorAll('.sidebar-nav .nav-item').forEach(el => el.classList.remove('active'));
            link.classList.add('active');

            const sidebar = document.getElementById("sidebar");
            if (sidebar) sidebar.classList.remove("open");

            if (view === 'builder') {
                showPanel('builderPanel');
                document.getElementById('currentViewTitle').innerText = 'Provisioning Studio';
                return;
            }

            if (view === 'overview') {
                showPanel('overviewPanel');
                document.getElementById('currentViewTitle').innerText = 'Overview Analytics';
                return;
            }

            showPanel('inventoryPanel');
            document.getElementById('currentViewTitle').innerText = link.querySelector('.nav-label')?.innerText.trim() || 'Master Inventory';

            if (status) document.getElementById('filterReadingStatus').value = status;
            else document.getElementById('filterReadingStatus').value = 'ALL';

            if (theme) document.getElementById('filterTheme').value = theme;
            else document.getElementById('filterTheme').value = 'ALL';

            currentPage = 1;
            loadNovels();
        });
    });
}

function showPanel(panelId) {
    document.querySelectorAll('.view-panel').forEach(p => p.style.display = 'none');
    const target = document.getElementById(panelId);
    if (target) target.style.display = 'block';
}

function navigateToBuilder() {
    const builderLink = document.querySelector('[data-view="builder"]');
    if (builderLink) builderLink.click();
}

function filterByQuickScope(scope) {
    showPanel('inventoryPanel');
    const statusSelect = document.getElementById('filterReadingStatus');
    const themeSelect = document.getElementById('filterTheme');

    statusSelect.value = 'ALL';
    themeSelect.value = 'ALL';

    if (scope === 'classified') statusSelect.value = 'Currently Reading';
    if (scope === 'unclassified') statusSelect.value = 'Unassigned';
    if (scope === 'completed') statusSelect.value = 'Already Read';
    
    document.getElementById('currentViewTitle').innerText = 'Filtered Scope';
    currentPage = 1;
    loadNovels();
}

async function loadNovels() {
    if (activeAbortController) {
        activeAbortController.abort();
    }
    activeAbortController = new AbortController();

    const searchInput = document.getElementById("searchInput");
    const searchVal = searchInput ? searchInput.value.trim() : "";
    const readingStatus = document.getElementById("filterReadingStatus")?.value || "ALL";
    const theme = document.getElementById("filterTheme")?.value || "ALL";
    const domain = document.getElementById("filterDomain")?.value || "ALL";

    const clearBtn = document.getElementById("clearSearchBtn");
    if (clearBtn) clearBtn.style.display = searchVal ? "block" : "none";

    const container = document.getElementById("resultsContainer");
    if (container) {
        container.innerHTML = `
            <div class="loading-state" style="text-align: center; padding: 48px;">
                <div class="spinner" aria-hidden="true"></div>
                <p style="color: var(--text-secondary);">Querying Celestial Archives...</p>
            </div>
        `;
    }

    try {
        const queryParams = new URLSearchParams({
            search: searchVal,
            reading_status: readingStatus,
            theme: theme,
            domain: domain,
            page: currentPage
        });

        const res = await fetch(`/api/novels?${queryParams.toString()}`, {
            signal: activeAbortController.signal
        });

        if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);

        const data = await res.json();
        cachedNovels = Array.isArray(data.novels) ? data.novels : [];

        renderNovels(cachedNovels);

        const pageIndicator = document.getElementById("pageIndicator");
        if (pageIndicator) pageIndicator.innerText = `Page ${currentPage}`;

        const resultsCount = document.getElementById("resultsCount");
        if (resultsCount) resultsCount.innerText = `Showing ${cachedNovels.length} novel series`;

        const prevBtn = document.getElementById("prevBtn");
        if (prevBtn) prevBtn.disabled = currentPage <= 1;

        const nextBtn = document.getElementById("nextBtn");
        if (nextBtn) nextBtn.disabled = cachedNovels.length < 20;

    } catch (err) {
        if (err.name === 'AbortError') return;
        if (container) {
            container.innerHTML = `
                <div class="alert alert-danger" style="margin: 20px 0;">
                    <span>⚠️ Failed to fetch archive data: ${escapeHtml(err.message)}</span>
                </div>
            `;
        }
    }
}

function renderNovels(novels) {
    const container = document.getElementById("resultsContainer");
    if (!container) return;

    if (!novels || novels.length === 0) {
        container.innerHTML = `
            <div class="empty-state" style="text-align: center; padding: 60px 20px; background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 12px;">
                <div style="font-size: 3rem; margin-bottom: 12px;" aria-hidden="true">📜</div>
                <h3 style="font-family: var(--font-heading); color: var(--primary); margin-bottom: 6px;">Zero Celestial Scrolls Located</h3>
                <p style="color: var(--text-secondary); max-width: 420px; margin: 0 auto;">No matching novels were discovered for this category or filter criteria. Use <code>i novel scan-server</code> to index channels.</p>
            </div>
        `;
        return;
    }

    if (currentLayout === 'table') {
        container.innerHTML = `
            <div class="table-responsive">
                <table class="archive-table">
                    <thead>
                        <tr>
                            <th width="40"><input type="checkbox" onchange="toggleSelectAll(this.checked)" aria-label="Select all novels"></th>
                            <th>Novel Title &amp; Slug</th>
                            <th>Reading Realm</th>
                            <th>Theme</th>
                            <th>Sources</th>
                            <th width="90" style="text-align: right;">Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${novels.map(n => `
                            <tr>
                                <td>
                                    <input type="checkbox" onchange="toggleSelectOne(${n.id}, this.checked)" ${selectedNovelIds.has(n.id) ? 'checked' : ''} aria-label="Select novel">
                                </td>
                                <td>
                                    <div class="novel-title-cell" onclick="openEditDrawer(${n.id})">
                                        ${escapeHtml(n.title || 'Untitled Series')}${n.is_favorite ? '<span class="badge badge-fav">Fav</span>' : ''}
                                        ${n.is_completed ? '<span class="badge badge-comp">Ascended</span>' : ''}
                                    </div>
                                    <span style="font-size: 0.75rem; color: var(--text-muted);">${escapeHtml(n.canonical_slug || '')}</span>
                                </td>
                                <td><span class="badge badge-status">${escapeHtml(n.reading_status || 'Unassigned')}</span></td>
                                <td><span class="badge badge-theme">${escapeHtml(n.theme || n.category || 'Normal / Other')}</span></td>
                                <td><span style="font-size: 0.85rem; color: var(--text-secondary);">${n.link_count || 1} link(s)</span></td>
                                <td style="text-align: right;">
                                    <button class="btn btn-secondary btn-sm" onclick="openEditDrawer(${n.id})">Edit</button>
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        `;
    } else {
        container.innerHTML = `
            <div class="cards-grid">
                ${novels.map(n => `
                    <div class="novel-card-item">
                        <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                            <input type="checkbox" onchange="toggleSelectOne(${n.id}, this.checked)" ${selectedNovelIds.has(n.id) ? 'checked' : ''} aria-label="Select novel">
                            <div style="display: flex; gap: 4px;">
                                ${n.is_favorite ? '<span class="badge badge-fav">Fav</span>' : ''}
                                ${n.is_completed ? '<span class="badge badge-comp">Ascended</span>' : ''}
                            </div>
                        </div>
                        <h4 class="novel-title-cell" onclick="openEditDrawer(${n.id})">${escapeHtml(n.title || 'Untitled')}</h4>
                        <div style="display: flex; gap: 6px; flex-wrap: wrap;">
                            <span class="badge badge-status">${escapeHtml(n.reading_status || 'Unassigned')}</span>
                            <span class="badge badge-theme">${escapeHtml(n.theme || n.category || 'Normal / Other')}</span>
                        </div>
                        <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: auto;">
                            <span>${n.link_count || 1} linked source(s)</span>
                        </div>
                    </div>
                `).join('')}
            </div>
        `;
    }
}

function openEditDrawer(novelId) {
    const novel = cachedNovels.find(n => n.id === novelId);
    if (!novel) return;

    document.getElementById('editNovelId').value = novel.id;
    document.getElementById('editTitle').value = novel.title || '';
    document.getElementById('editReadingStatus').value = novel.reading_status || 'Unassigned';
    document.getElementById('editTheme').value = novel.theme || novel.category || 'Normal / Other';
    document.getElementById('editIsFavorite').checked = Boolean(novel.is_favorite);
    document.getElementById('editIsCompleted').checked = Boolean(novel.is_completed);

    const linksContainer = document.getElementById('drawerLinksList');
    if (novel.sources && novel.sources.length) {
        linksContainer.innerHTML = novel.sources.map(s => `
            <div style="margin-bottom: 10px; border-bottom: 1px solid var(--border-color); padding-bottom: 8px;">
                <span class="badge badge-theme" style="font-size: 0.65rem;">${escapeHtml((s.domain || 'LINK').toUpperCase())}</span>
                <a href="${escapeHtml(s.original_url)}" target="_blank" rel="noopener" style="color: var(--accent); text-decoration: none; word-break: break-all; display: block; margin: 4px 0;">
                    ${escapeHtml(s.original_url)}
                </a>
                <span style="font-size: 0.75rem; color: var(--text-muted);">From #${escapeHtml(s.channel_name || 'unknown')}</span>
            </div>
        `).join('');
    } else {
        linksContainer.innerHTML = `<span style="color: var(--text-muted); font-size: 0.85rem;">No direct message URLs attached.</span>`;
    }

    document.getElementById('drawerBackdrop').classList.add('active');
    document.getElementById('editDrawer').classList.add('active');
}

function closeEditDrawer() {
    document.getElementById('drawerBackdrop').classList.remove('active');
    document.getElementById('editDrawer').classList.remove('active');
}

async function saveDrawerEdit(e) {
    e.preventDefault();
    const id = document.getElementById('editNovelId').value;
    const title = document.getElementById('editTitle').value.trim();
    const readingStatus = document.getElementById('editReadingStatus').value;
    const theme = document.getElementById('editTheme').value;
    const isFavorite = document.getElementById('editIsFavorite').checked;
    const isCompleted = document.getElementById('editIsCompleted').checked;

    try {
        await fetch(`/api/novel/${id}/update`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                title: title,
                reading_status: readingStatus,
                theme: theme,
                is_favorite: isFavorite,
                is_completed: isCompleted
            })
        });

        closeEditDrawer();
        loadNovels();
    } catch (err) {
        alert("Failed to save novel modification: " + err.message);
    }
}

function toggleSelectOne(id, checked) {
    if (checked) selectedNovelIds.add(id);
    else selectedNovelIds.delete(id);
    updateBulkBar();
}

function toggleSelectAll(checked) {
    cachedNovels.forEach(n => {
        if (checked) selectedNovelIds.add(n.id);
        else selectedNovelIds.delete(n.id);
    });
    updateBulkBar();
    renderNovels(cachedNovels);
}

function updateBulkBar() {
    const bar = document.getElementById('bulkBar');
    const text = document.getElementById('selectedCountText');
    if (!bar || !text) return;

    text.innerText = `${selectedNovelIds.size} novel(s) selected`;
    bar.style.display = selectedNovelIds.size > 0 ? 'flex' : 'none';
}

function clearSelection() {
    selectedNovelIds.clear();
    updateBulkBar();
    renderNovels(cachedNovels);
}

async function submitBulkClassification() {
    const statusVal = document.getElementById('bulkReadingStatus').value;
    const themeVal = document.getElementById('bulkTheme').value;

    try {
        await fetch('/api/novels/bulk-classify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                novel_ids: Array.from(selectedNovelIds),
                reading_status: statusVal,
                theme: themeVal
            })
        });

        clearSelection();
        loadNovels();
    } catch (err) {
        alert("Bulk action failed: " + err.message);
    }
}

async function runPreflightCheck() {
    const targetGuildId = "1560935178879373396";
    const container = document.getElementById('preflightResults');
    if (!container) return;

    container.style.display = 'block';
    container.innerHTML = '<p style="color: var(--text-secondary);">Querying destination server capacity...</p>';

    try {
        const res = await fetch(`/api/builder/preflight/${targetGuildId}`);
        if (!res.ok) throw new Error("Destination server unreachable or bot lacking permissions.");

        const data = await res.json();
        container.innerHTML = `
            <div class="stat-card" style="margin-top: 10px; cursor: default;">
                <div>
                    <h4>Preflight Audit: ${data.capacity_exceeded ? '<span style="color: var(--danger)">CAPACITY OVERFLOW</span>' : '<span style="color: var(--success)">PASSED</span>'}</h4>
                    <p style="font-size: 0.85rem; color: var(--text-secondary); margin-top: 6px;">
                        • Target Server: <strong>${escapeHtml(data.guild_name || targetGuildId)}</strong><br>
                        • Existing Server Channels: <strong>${data.existing_channels} / 500</strong><br>
                        • Planned Channels to Provision: <strong>${data.planned_channels_to_create}</strong><br>
                        • Total Projected Channels: <strong>${data.projected_total_channels}</strong>
                    </p>
                </div>
            </div>
        `;
    } catch (err) {
        container.innerHTML = `<div class="alert alert-danger" style="margin-top: 10px;">Preflight audit failed: ${escapeHtml(err.message)}</div>`;
    }
}

function debounceSearch() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
        currentPage = 1;
        loadNovels();
    }, 280);
}

function clearSearch() {
    const input = document.getElementById('searchInput');
    if (input) input.value = '';
    currentPage = 1;
    loadNovels();
}

function applyFilters() {
    currentPage = 1;
    loadNovels();
}

function setLayout(layout) {
    currentLayout = layout;
    const tableBtn = document.getElementById('btnLayoutTable');
    const cardsBtn = document.getElementById('btnLayoutCards');
    if (tableBtn) tableBtn.classList.toggle('active', layout === 'table');
    if (cardsBtn) cardsBtn.classList.toggle('active', layout === 'cards');
    renderNovels(cachedNovels);
}

function prevPage() {
    if (currentPage > 1) {
        currentPage--;
        loadNovels();
    }
}

function nextPage() {
    currentPage++;
    loadNovels();
}

function toggleTheme() {
    const html = document.documentElement;
    const next = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', next);
    localStorage.setItem('sentinel_theme', next);
}

function setupShortcuts() {
    window.addEventListener('keydown', (e) => {
        if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'SELECT') {
            e.preventDefault();
            const input = document.getElementById('searchInput');
            if (input) input.focus();
        }
        if (e.key === 'Escape') {
            closeEditDrawer();
        }
    });
}

function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str).replace(/[&<>'"]/g, tag => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
    }[tag] || tag));
}