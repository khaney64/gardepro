// GardePro Web UI

const S = {
  status: 'disconnected',
  step: '',
  mediaCount: 0,
  rtspUrl: null,
  hlsAvailable: false,
  hlsActive: false,
  signalDbm: null,
  signalLabel: null,
  error: null,
  lastSynced: null,

  // UI
  tab: 'gallery',
  page: 0,
  pageSize: parseInt(localStorage.getItem('pageSize') || '24', 10),
  sortDesc: localStorage.getItem('sortDesc') !== 'false', // default newest-first
  media: [],        // full list from server
  pageItems: [],    // items on current page (for lightbox nav)
  multiSelect: false,
  selected: new Set(),
  lightboxIdx: -1,
};

let evtSource = null;
let hlsInstance = null;

// ── SSE ───────────────────────────────────────────────────────────────────────

function startSSE() {
  if (evtSource) evtSource.close();
  evtSource = new EventSource('/api/events');
  evtSource.onmessage = (e) => {
    try { handleEvent(JSON.parse(e.data)); } catch (_) {}
  };
  evtSource.onerror = () => {
    setTimeout(startSSE, 3000);
  };
}

function handleEvent(data) {
  if (data.type === 'state') {
    const wasConnected = S.status === 'connected';
    const wasDisconnected = S.status === 'disconnected';
    S.status      = data.status;
    S.step        = data.step || '';
    S.mediaCount  = data.media_count || 0;
    S.rtspUrl     = data.rtsp_url || null;
    S.hlsAvailable = data.hls_available || false;
    S.error       = data.error || null;
    S.lastSynced  = data.last_synced || null;
    if (data.signal_dbm != null) { S.signalDbm = data.signal_dbm; S.signalLabel = data.signal_label; }

    if (!wasConnected && S.status === 'connected') {
      fetchMedia();
    } else if (wasDisconnected && S.status === 'disconnected' && S.mediaCount > 0 && S.media.length === 0) {
      // Server restarted with cached media — load gallery immediately
      fetchMedia();
    }
    updateUI();

  } else if (data.type === 'signal') {
    S.signalDbm   = data.dbm;
    S.signalLabel = data.label;
    updateSignalBadge();

  } else if (data.type === 'media_progress') {
    S.mediaCount = data.count;
    el('gallery-progress-text').textContent = `Scanning media… ${data.count} found`;
    el('media-count').textContent = `${data.count} items`;

  } else if (data.type === 'cache_progress') {
    if (data.cached < data.total) {
      el('cache-progress-text').textContent = `Caching thumbnails… ${data.cached} / ${data.total}`;
      show('cache-progress', true);
    } else {
      show('cache-progress', false);
    }

  } else if (data.type === 'media_deleted') {
    S.media = S.media.filter(m => !(m.id === data.id && m.kind === data.kind));
    renderGallery();
  }
}

// ── API calls ─────────────────────────────────────────────────────────────────

async function connectCamera() {
  if (S.status !== 'disconnected') return;
  el('connect-error').classList.add('hidden');
  el('connect-btn').disabled = true;
  el('connect-btn').textContent = 'Connecting…';
  showConnectLog(true);
  await fetch('/api/connect', { method: 'POST' });
}

async function disconnectCamera() {
  if (!confirm('Disconnect from camera?')) return;
  await fetch('/api/disconnect', { method: 'POST' });
}

async function fetchMedia() {
  showProgress(true);
  let all = [];
  let page = 0;
  const size = 200;
  while (true) {
    const r = await fetch(`/api/media?page=${page}&size=${size}`);
    const d = await r.json();
    all = all.concat(d.items);
    if (all.length >= d.total || d.items.length < size) break;
    page++;
  }
  S.media = S.sortDesc ? [...all].reverse() : all;
  S.page = 0;
  showProgress(false);
  updateSortBtn();
  renderGallery();
}

async function deleteFile(id, kind) {
  const r = await fetch(`/api/file/${id}/${kind}`, { method: 'DELETE' });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    alert(`Delete failed: ${d.detail || r.status}`);
    return false;
  }
  return true;
}

// ── UI update ─────────────────────────────────────────────────────────────────

function updateUI() {
  const connected = S.status === 'connected';
  const connecting = S.status === 'connecting' || S.status === 'disconnecting';
  const hasCached = S.mediaCount > 0;
  const showApp = connected || hasCached;

  // Show app (gallery, header, nav) when connected OR when cache has media
  show('connect-panel', !showApp);
  show('app-header', showApp);
  show('main-content', showApp);
  show('bottom-nav', showApp);
  // Disconnect button only makes sense when connected
  show('disconnect-btn', connected);
  updateOfflineBar();

  // Connect button
  const btn = el('connect-btn');
  if (btn) {
    const disconnecting = S.status === 'disconnecting';
    btn.disabled = connecting;
    btn.textContent = disconnecting   ? 'Disconnecting…' :
                      connecting      ? 'Connecting…'    : 'Connect Camera';
  }

  // Connect log — show steps while connecting, clear on all other states
  const log = el('connect-log');
  if (log) {
    if (S.status === 'connecting' && S.step) {
      log.classList.remove('hidden');
      const last = log.lastElementChild;
      if (!last || last.textContent !== S.step) {
        const p = document.createElement('p');
        p.textContent = S.step;
        log.appendChild(p);
        log.scrollTop = log.scrollHeight;
      }
    } else {
      // Clear for disconnecting, disconnected, or connected
      log.classList.add('hidden');
      log.innerHTML = '';
    }
  }

  // Error banner
  const errBanner = el('connect-error');
  if (S.error && !connected) {
    errBanner.textContent = S.error;
    errBanner.classList.remove('hidden');
    if (btn) { btn.disabled = false; btn.textContent = 'Try Again'; }
  }

  updateSignalBadge();
  updateLiveTab();
  updateSettingsTab();
}

function updateSignalBadge() {
  const badge = el('signal-badge');
  if (S.signalDbm != null && S.status === 'connected') {
    badge.textContent = `📶 ${S.signalLabel} (${S.signalDbm} dBm)`;
    badge.className = 'signal-badge signal-' + (S.signalLabel || 'unknown').toLowerCase();
    badge.classList.remove('hidden');
  } else {
    badge.classList.add('hidden');
  }
}

function showConnectLog(visible) {
  const log = el('connect-log');
  log.innerHTML = '';
  log.classList.toggle('hidden', !visible);
}

function showProgress(visible) {
  el('gallery-progress').classList.toggle('hidden', !visible);
  if (visible) el('gallery-progress-text').textContent = 'Scanning media…';
}

// ── Tabs ──────────────────────────────────────────────────────────────────────

function showTab(tab) {
  if (S.tab === 'live' && tab !== 'live' && S.hlsActive) {
    stopHls();
  }
  S.tab = tab;
  ['gallery', 'live', 'settings'].forEach(t => {
    el(`tab-${t}`).classList.toggle('hidden', t !== tab);
  });
  document.querySelectorAll('[data-tab]').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tab);
  });
  if (tab === 'live') updateLiveTab();
  if (tab === 'settings') updateSettingsTab();
}

// ── Gallery ───────────────────────────────────────────────────────────────────

function renderGallery() {
  const grid = el('gallery-grid');
  const empty = el('gallery-empty');
  const count = el('media-count');
  const pageInfo = el('page-info');

  const total = S.media.length;
  const totalPages = Math.max(1, Math.ceil(total / S.pageSize));
  S.page = Math.min(S.page, totalPages - 1);

  const start = S.page * S.pageSize;
  S.pageItems = S.media.slice(start, start + S.pageSize);

  count.textContent = total ? `${total} items` : '';
  pageInfo.textContent = total ? `${S.page + 1} / ${totalPages}` : '0 / 0';

  if (!total) {
    grid.innerHTML = '';
    empty.textContent = S.status === 'connected'
      ? 'No media found on camera.'
      : 'No cached media. Connect to camera to sync.';
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');

  grid.innerHTML = '';
  S.pageItems.forEach((item, idx) => {
    grid.appendChild(makeThumbCard(item, idx));
  });
}

function makeThumbCard(item, idx) {
  const card = document.createElement('div');
  const key = `${item.id}:${item.kind}`;
  card.className = 'thumb-card' + (S.selected.has(key) ? ' selected' : '');
  card.dataset.mediaKind = item.kind;

  const img = document.createElement('img');
  img.src = `/api/thumb/${item.id}/${item.kind}`;
  img.loading = 'lazy';
  img.alt = `${item.kind.toUpperCase()} ${item.id}`;
  img.draggable = false;
  card.appendChild(img);

  if (item.kind === 'mp4') {
    const badge = document.createElement('div');
    badge.className = 'video-badge';
    badge.textContent = '▶ MP4';
    card.appendChild(badge);
  }

  const check = document.createElement('div');
  check.className = 'thumb-check';
  check.textContent = '✓';
  card.appendChild(check);

  // Long-press → multi-select
  let timer = null;
  card.addEventListener('touchstart', () => {
    timer = setTimeout(() => { enterMultiSelect(); toggleSelect(item, card); }, 500);
  }, { passive: true });
  card.addEventListener('touchend',  () => clearTimeout(timer));
  card.addEventListener('touchmove', () => clearTimeout(timer), { passive: true });

  card.addEventListener('click', () => {
    if (S.multiSelect) toggleSelect(item, card);
    else openLightbox(idx);
  });

  return card;
}

// Pagination
function prevPage() {
  if (S.page > 0) { S.page--; renderGallery(); scrollToTop(); }
}
function nextPage() {
  const pages = Math.ceil(S.media.length / S.pageSize);
  if (S.page < pages - 1) { S.page++; renderGallery(); scrollToTop(); }
}
function onPageSizeChange() {
  S.pageSize = parseInt(el('page-size').value, 10);
  localStorage.setItem('pageSize', S.pageSize);
  S.page = 0;
  renderGallery();
}

function toggleSort() {
  S.sortDesc = !S.sortDesc;
  localStorage.setItem('sortDesc', S.sortDesc);
  S.media = [...S.media].reverse();
  S.page = 0;
  updateSortBtn();
  renderGallery();
}

function updateSortBtn() {
  const btn = el('sort-btn');
  if (btn) btn.textContent = S.sortDesc ? '↓ Newest' : '↑ Oldest';
}
function scrollToTop() {
  el('main-content').scrollTop = 0;
  window.scrollTo(0, 0);
}

// ── Multi-select ──────────────────────────────────────────────────────────────

function enterMultiSelect() {
  S.multiSelect = true;
  document.body.classList.add('multiselect-mode');
  show('select-all-btn', true);
  show('delete-sel-btn', true);
  show('multiselect-cancel-btn', true);
}

function exitMultiSelect() {
  S.multiSelect = false;
  S.selected.clear();
  document.body.classList.remove('multiselect-mode');
  show('select-all-btn', false);
  show('delete-sel-btn', false);
  show('multiselect-cancel-btn', false);
  el('sel-count').textContent = '0';
  document.querySelectorAll('.thumb-card.selected').forEach(c => c.classList.remove('selected'));
}

function toggleSelect(item, card) {
  const key = `${item.id}:${item.kind}`;
  if (S.selected.has(key)) {
    S.selected.delete(key);
    card.classList.remove('selected');
  } else {
    S.selected.add(key);
    card.classList.add('selected');
  }
  el('sel-count').textContent = S.selected.size;
}

function selectAll() {
  S.pageItems.forEach((item, idx) => {
    const key = `${item.id}:${item.kind}`;
    S.selected.add(key);
    const cards = el('gallery-grid').querySelectorAll('.thumb-card');
    if (cards[idx]) cards[idx].classList.add('selected');
  });
  el('sel-count').textContent = S.selected.size;
}

async function deleteSelected() {
  if (!S.selected.size) return;
  if (!confirm(`Delete ${S.selected.size} item(s)? This cannot be undone.`)) return;
  const toDelete = [...S.selected].map(k => {
    const [id, kind] = k.split(':');
    return { id: parseInt(id, 10), kind };
  });
  for (const item of toDelete) {
    await deleteFile(item.id, item.kind);
  }
  exitMultiSelect();
}

// ── Lightbox ──────────────────────────────────────────────────────────────────

function openLightbox(idx) {
  S.lightboxIdx = idx;
  const lb = el('lightbox');
  lb.classList.add('open');
  renderLightboxItem();

  // Swipe support
  let tx = 0;
  const content = el('lightbox-content');
  content.addEventListener('touchstart', e => { tx = e.touches[0].clientX; }, { passive: true });
  content.addEventListener('touchend', e => {
    const dx = e.changedTouches[0].clientX - tx;
    if (Math.abs(dx) > 50) dx > 0 ? lightboxPrev() : lightboxNext();
  });
}

function renderLightboxItem() {
  const item = S.pageItems[S.lightboxIdx];
  if (!item) return;

  const content = el('lightbox-content');
  // Stop any playing video
  const oldV = content.querySelector('video');
  if (oldV) oldV.pause();
  content.innerHTML = '';

  el('lb-counter').textContent =
    `${S.lightboxIdx + 1} / ${S.pageItems.length}`;

  const btn = el('lb-delete-btn');
  btn.dataset.id   = item.id;
  btn.dataset.kind = item.kind;
  btn.classList.toggle('hidden', S.status !== 'connected');

  if (item.kind === 'jpg') {
    const img = document.createElement('img');
    img.alt = `Photo ${item.id}`;
    img.onerror = () => {
      // Not cached + not connected: fall back to thumbnail
      img.onerror = null;
      img.src = `/api/thumb/${item.id}/${item.kind}`;
      const note = document.createElement('p');
      note.className = 'lb-offline-note';
      note.textContent = 'Cached thumbnail — connect for full resolution';
      content.appendChild(note);
    };
    img.src = `/api/file/${item.id}/${item.kind}`;
    content.appendChild(img);
  } else {
    const video = document.createElement('video');
    video.controls = true;
    video.setAttribute('playsinline', '');
    video.onerror = () => {
      content.innerHTML = '';
      const img = document.createElement('img');
      img.src = `/api/thumb/${item.id}/${item.kind}`;
      img.alt = `Video ${item.id}`;
      content.appendChild(img);
      const note = document.createElement('p');
      note.className = 'lb-offline-note';
      note.textContent = S.status !== 'connected'
        ? 'Connect to play video'
        : 'Could not load video. The file may still be transferring.';
      content.appendChild(note);
    };
    video.src = `/api/file/${item.id}/${item.kind}`;
    content.appendChild(video);
  }
}

function closeLightbox() {
  const content = el('lightbox-content');
  const v = content.querySelector('video');
  if (v) v.pause();
  content.innerHTML = '';
  el('lightbox').classList.remove('open');
  S.lightboxIdx = -1;
}

function lightboxBgClick(e) {
  if (e.target === el('lightbox')) closeLightbox();
}

function lightboxPrev() {
  if (S.lightboxIdx > 0) { S.lightboxIdx--; renderLightboxItem(); }
}

function lightboxNext() {
  if (S.lightboxIdx < S.pageItems.length - 1) { S.lightboxIdx++; renderLightboxItem(); }
}

async function lightboxDelete() {
  const btn = el('lb-delete-btn');
  const id   = parseInt(btn.dataset.id, 10);
  const kind = btn.dataset.kind;
  if (!confirm(`Delete this ${kind.toUpperCase()}?`)) return;
  const ok = await deleteFile(id, kind);
  if (ok) {
    S.media = S.media.filter(m => !(m.id === id && m.kind === kind));
    closeLightbox();
    renderGallery();
  }
}

// Keyboard navigation in lightbox
document.addEventListener('keydown', e => {
  if (!el('lightbox').classList.contains('open')) return;
  if (e.key === 'ArrowLeft')  lightboxPrev();
  if (e.key === 'ArrowRight') lightboxNext();
  if (e.key === 'Escape')     closeLightbox();
});

// ── Settings tab ──────────────────────────────────────────────────────────────

function updateSettingsTab() {
  const connected = S.status === 'connected';
  show('settings-disconnected', !connected);
  show('settings-content', connected);
}

// ── Live streaming ─────────────────────────────────────────────────────────────

function updateLiveTab() {
  if (S.status !== 'connected') {
    show('live-disconnected', true);
    show('live-info', false);
    return;
  }
  show('live-disconnected', false);
  show('live-info', true);

  const rtspInfo = el('rtsp-info');
  if (S.rtspUrl) {
    el('rtsp-url-text').textContent = S.rtspUrl;
    rtspInfo.classList.remove('hidden');
  } else {
    rtspInfo.classList.add('hidden');
  }

  const hint = el('hls-hint');
  if (!S.hlsAvailable) {
    hint.textContent = 'In-browser streaming requires ffmpeg. Run: sudo apt-get install ffmpeg';
  } else {
    hint.textContent = 'Watch live H.264 video directly in this browser.';
  }

  el('watch-btn').disabled = !S.hlsAvailable;
  el('watch-btn').textContent = S.hlsActive ? '⏹ Stop Stream' : '▶ Watch in Browser';
}

async function toggleHls() {
  if (S.hlsActive) {
    await stopHls();
  } else {
    await startHls();
  }
}

async function startHls() {
  el('watch-btn').disabled = true;
  el('watch-btn').textContent = 'Starting…';
  const r = await fetch('/api/stream/hls/start', { method: 'POST' });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    alert(`Stream failed: ${d.detail || r.status}`);
    el('watch-btn').disabled = false;
    el('watch-btn').textContent = '▶ Watch in Browser';
    return;
  }
  S.hlsActive = true;
  el('watch-btn').textContent = '⏹ Stop Stream';
  el('watch-btn').disabled = false;
  show('hls-player-wrap', true);

  const video = el('hls-video');
  const src = '/api/stream/hls/live.m3u8';
  if (typeof Hls !== 'undefined' && Hls.isSupported()) {
    if (hlsInstance) hlsInstance.destroy();
    hlsInstance = new Hls({
      lowLatencyMode: true,
      liveSyncDurationCount: 2,
      liveMaxLatencyDurationCount: 4,
      maxBufferLength: 6,
      backBufferLength: 3,
    });
    hlsInstance.loadSource(src);
    hlsInstance.attachMedia(video);
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    // Native HLS (Safari/iOS)
    video.src = src;
    video.load();
  }
}

async function stopHls() {
  if (hlsInstance) { hlsInstance.destroy(); hlsInstance = null; }
  const video = el('hls-video');
  video.pause();
  video.src = '';
  S.hlsActive = false;
  show('hls-player-wrap', false);
  el('watch-btn').textContent = '▶ Watch in Browser';
  await fetch('/api/stream/hls/stop', { method: 'POST' });
}

function copyRtspUrl() {
  const url = el('rtsp-url-text').textContent;
  navigator.clipboard.writeText(url).then(() => {
    const btn = event.currentTarget;
    btn.textContent = 'Copied!';
    setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
  });
}

// ── Settings ──────────────────────────────────────────────────────────────────

async function loadSettings() {
  const wrap = el('settings-table-wrap');
  const loading = el('settings-loading');
  loading.innerHTML = '<span class="muted">Loading…</span>';
  try {
    const r = await fetch('/api/settings');
    const d = await r.json();
    const settings = d.settings?.data || d.settings || {};
    const rows = Object.entries(settings)
      .filter(([k]) => !['code'].includes(k))
      .map(([k, v]) => `<tr><td>${k}</td><td>${JSON.stringify(v)}</td></tr>`)
      .join('');
    wrap.innerHTML = `<table class="settings-table"><tbody>${rows}</tbody></table>`;
    wrap.classList.remove('hidden');
    loading.classList.add('hidden');
  } catch (e) {
    loading.innerHTML = `<span class="error">Failed: ${e.message}</span>
      <button class="btn btn-sm" onclick="loadSettings()">Retry</button>`;
  }
}

async function formatSD() {
  if (!confirm('Format the SD card?\n\nThis will permanently delete ALL media.')) return;
  const confirmText = prompt('Type CONFIRM to proceed with formatting:');
  if (confirmText !== 'CONFIRM') { alert('Cancelled.'); return; }
  const r = await fetch('/api/settings/format', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm: 'CONFIRM' }),
  });
  const d = await r.json().catch(() => ({}));
  if (r.ok && d.code === 0) {
    alert('Format started. The camera will format the SD card.');
    S.media = [];
    renderGallery();
  } else {
    alert(`Format failed: ${d.desc || d.detail || r.status}`);
  }
}

// ── Offline bar ───────────────────────────────────────────────────────────────

function updateOfflineBar() {
  const offline = S.status !== 'connected' && S.mediaCount > 0;
  show('offline-bar', offline);
  if (!offline) return;
  if (S.lastSynced) {
    el('last-synced-label').textContent = 'Last synced: ' + formatRelativeTime(S.lastSynced);
  }
  const btn = el('offline-bar').querySelector('.btn');
  if (btn) {
    const busy = S.status === 'connecting' || S.status === 'disconnecting';
    btn.disabled = busy;
    btn.textContent = S.status === 'disconnecting' ? 'Disconnecting…'
                    : S.status === 'connecting'    ? 'Connecting…'
                    : 'Connect';
  }
}

function formatRelativeTime(isoStr) {
  const diff = (Date.now() - new Date(isoStr).getTime()) / 1000;
  if (diff < 60)    return 'just now';
  if (diff < 3600)  return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function el(id) { return document.getElementById(id); }
function show(id, visible) {
  const e = el(id);
  if (e) e.classList.toggle('hidden', !visible);
}

// ── Init ──────────────────────────────────────────────────────────────────────

// Set saved page size in select
const sizeSelect = el('page-size');
if (sizeSelect) sizeSelect.value = S.pageSize;

// Set initial sort button label from saved preference
updateSortBtn();

startSSE();
