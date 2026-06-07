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
  lastEvent: null,

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
  lightboxSource: 'gallery', // 'gallery' | 'local'
  localItems: [],
  modalOpen: false,

  // Analysis: Map of "id:kind" → {subjects, description}
  analysis: {},
  chatEnabled: false,
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
  if (data.type === 'log') {
    appendLog(data);
    appendConnectModalLog(data.msg);
  } else if (data.type === 'alert_error') {
    appendLog(data, true);
    appendConnectModalLog(data.msg);

  } else if (data.type === 'state') {
    const wasConnected = S.status === 'connected';
    const wasDisconnected = S.status === 'disconnected';
    S.status      = data.status;
    S.step        = data.step || '';
    S.mediaCount  = data.media_count || 0;
    S.rtspUrl     = data.rtsp_url || null;
    S.hlsAvailable = data.hls_available || false;
    S.error       = data.error || null;
    S.lastSynced  = data.last_synced || null;
    S.lastEvent   = data.last_event  || null;
    if (data.signal_dbm != null) { S.signalDbm = data.signal_dbm; S.signalLabel = data.signal_label; }

    if (!wasConnected && S.status === 'connected') {
      fetchMedia();
      fetchAnalysis();
    } else if (wasDisconnected && S.status === 'disconnected' && S.mediaCount > 0 && S.media.length === 0) {
      // Server restarted with cached media — load gallery immediately
      fetchMedia();
      fetchAnalysis();
    }
    if (S.multiSelect && S.status === 'connecting') exitMultiSelect();
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

  } else if (data.type === 'analysis_update') {
    const key = `${data.id}:${data.kind}`;
    S.analysis[key] = { subjects: data.subjects || [], description: data.description || '' };
    const cards = document.querySelectorAll(`#gallery-grid .thumb-card[data-media-id="${data.id}"][data-media-kind="${data.kind}"]`);
    cards.forEach(card => applyAnalysisToCard(card, data.id, data.kind));

  } else if (data.type === 'saved_analysis_update') {
    const result = { subjects: data.subjects || [], description: data.description || '' };
    const item = S.localItems.find(i => i.saved_id === data.saved_id);
    if (item) item.analysis = result;
    const cards = document.querySelectorAll(`#local-grid .thumb-card[data-saved-id="${data.saved_id}"]`);
    cards.forEach(card => applyAnalysisToLocalCard(card, result));
  }
}

// ── API calls ─────────────────────────────────────────────────────────────────

async function connectCamera() {
  if (S.status !== 'disconnected') return;
  el('connect-error').classList.add('hidden');
  openConnectModal();
  await fetch('/api/connect', { method: 'POST' });
}

function openConnectModal() {
  S.modalOpen = true;
  el('connect-modal-log').innerHTML = '';
  el('connect-modal-error').classList.add('hidden');
  el('connect-modal-title').textContent = 'Connecting to Camera…';
  show('connect-modal-cancel', true);
  show('connect-modal-close', false);
  el('connect-modal-x').style.display = 'none';
  el('connect-modal').classList.remove('hidden');
}

function closeConnectModal() {
  S.modalOpen = false;
  el('connect-modal').classList.add('hidden');
}

async function cancelConnectModal() {
  show('connect-modal-cancel', false);
  await fetch('/api/disconnect', { method: 'POST' });
}

function appendConnectModalLog(msg) {
  if (!S.modalOpen) return;
  const log = el('connect-modal-log');
  const p = document.createElement('p');
  p.className = 'cml-step';
  p.textContent = msg;
  log.appendChild(p);
  log.scrollTop = log.scrollHeight;
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

async function fetchAnalysis() {
  try {
    const r = await fetch('/api/analysis');
    if (r.ok) S.analysis = await r.json();
  } catch (_) {}
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
  const showApp = connected || hasCached || connecting;

  // Show app (gallery, header, nav) when connected OR when cache has media
  show('connect-panel', !showApp);
  show('app-header', showApp);
  show('main-content', showApp);
  show('bottom-nav', showApp);
  // Disconnect button only makes sense when connected
  show('disconnect-btn', connected);
  updateOfflineBar();
  updateLastEvent();

  // Sync Now button
  const syncBtn = el('sync-btn');
  if (syncBtn) {
    syncBtn.disabled = connecting;
    const lbl = syncBtn.querySelector('.sync-label');
    if (lbl) lbl.textContent = connecting ? ' Syncing…' : ' Sync Now';
  }

  // Connect button (on the initial connect-panel)
  const btn = el('connect-btn');
  if (btn) {
    btn.disabled = connecting;
    btn.textContent = connecting ? 'Connecting…' : 'Connect Camera';
  }

  // Connection modal state machine
  if (S.modalOpen) {
    if (connected) {
      // Successfully connected — close modal
      closeConnectModal();
    } else if (S.error && !connecting) {
      // Connection failed — show error, switch to close mode
      el('connect-modal-title').textContent = 'Connection Failed';
      const errEl = el('connect-modal-error');
      errEl.textContent = S.error;
      errEl.classList.remove('hidden');
      show('connect-modal-cancel', false);
      show('connect-modal-close', true);
      el('connect-modal-x').style.display = '';
    }
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
  ['gallery', 'live', 'local', 'settings', 'logs'].forEach(t => {
    el(`tab-${t}`).classList.toggle('hidden', t !== tab);
  });
  document.querySelectorAll('[data-tab]').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tab);
  });
  if (tab === 'live')     updateLiveTab();
  if (tab === 'settings') { updateSettingsTab(); loadAnalysisConfig(); }
  if (tab === 'logs')     fetchLogs();
  if (tab === 'local')    loadLocalMedia();
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

const _SUBJECT_PRIORITY = ['raccoon', 'bear', 'coyote', 'person', 'human', 'legs', 'fox', 'deer', 'cat', 'dog'];
const _SUBJECT_LABELS = {
  raccoon: '🦝 raccoon', bear: '🐻 bear', coyote: '🐺 coyote',
  person: '🚶 person', human: '🚶 person', legs: '🚶 person',
  fox: '🦊 fox', deer: '🦌 deer', cat: '🐱 cat', dog: '🐶 dog',
  squirrel: '🐿 squirrel', rabbit: '🐇 rabbit', bird: '🐦 bird',
  skunk: '🦨 skunk', turkey: '🦃 turkey', possum: '🐾 possum',
};

function topSubject(subjects) {
  if (!subjects || !subjects.length) return null;
  return _SUBJECT_PRIORITY.find(s => subjects.includes(s)) || subjects[0];
}

function subjectAlertClass(subject) {
  if (!subject) return null;
  if (['raccoon', 'bear', 'coyote', 'skunk'].includes(subject)) return 'alert-wild';
  if (['person', 'human', 'legs'].includes(subject)) return 'alert-person';
  if (['cat', 'dog'].includes(subject)) return 'alert-pet';
  return 'alert-animal';
}

function applyAnalysisToLocalCard(card, analysis) {
  card.classList.remove('alert-wild', 'alert-person', 'alert-pet', 'alert-animal');
  const oldBadge = card.querySelector('.analysis-badge');
  if (oldBadge) oldBadge.remove();
  if (!analysis || !analysis.subjects || !analysis.subjects.length) return;
  const top = topSubject(analysis.subjects);
  const cls = subjectAlertClass(top);
  if (cls) card.classList.add(cls);
  const badge = document.createElement('div');
  badge.className = 'analysis-badge';
  badge.textContent = analysis.subjects.map(s => _SUBJECT_LABELS[s] || s).join(' · ');
  card.appendChild(badge);
}

function applyAnalysisToCard(card, id, kind) {
  const key = `${id}:${kind}`;
  const analysis = S.analysis[key];
  // Remove old analysis classes and badge
  card.classList.remove('alert-wild', 'alert-person', 'alert-pet', 'alert-animal');
  const oldBadge = card.querySelector('.analysis-badge');
  if (oldBadge) oldBadge.remove();
  if (!analysis || !analysis.subjects || !analysis.subjects.length) return;
  const top = topSubject(analysis.subjects);
  const cls = subjectAlertClass(top);
  if (cls) card.classList.add(cls);
  const badge = document.createElement('div');
  badge.className = 'analysis-badge';
  badge.textContent = analysis.subjects.map(s => _SUBJECT_LABELS[s] || s).join(' · ');
  card.appendChild(badge);
}

function makeThumbCard(item, idx) {
  const card = document.createElement('div');
  const key = `${item.id}:${item.kind}`;
  card.className = 'thumb-card' + (S.selected.has(key) ? ' selected' : '');
  card.dataset.mediaKind = item.kind;
  card.dataset.mediaId   = item.id;

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

  applyAnalysisToCard(card, item.id, item.kind);

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
  S.lightboxSource = 'gallery';
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
  const isLocal = S.lightboxSource === 'local';
  const items   = isLocal ? S.localItems : S.pageItems;
  const item    = items[S.lightboxIdx];
  if (!item) return;

  const content = el('lightbox-content');
  const oldV = content.querySelector('video');
  if (oldV) oldV.pause();
  content.innerHTML = '';

  el('lb-counter').textContent = `${S.lightboxIdx + 1} / ${items.length}`;

  const fileUrl  = isLocal ? `/api/saved/file/${item.saved_id}`  : `/api/file/${item.id}/${item.kind}`;
  const thumbUrl = isLocal ? `/api/saved/thumb/${item.saved_id}` : `/api/thumb/${item.id}/${item.kind}`;

  // Delete: always visible for local, connected-only for gallery
  const delBtn = el('lb-delete-btn');
  delBtn.dataset.savedId = item.saved_id || '';
  delBtn.dataset.id      = isLocal ? item.cam_id : item.id;
  delBtn.dataset.kind    = item.kind;
  delBtn.classList.toggle('hidden', false);

  // Save: only for gallery items
  const saveBtn = el('lb-save-btn');
  if (saveBtn) saveBtn.classList.toggle('hidden', isLocal);

  // Re-analyze: available for both gallery and local items
  const reBtn = el('lb-reanalyze-btn');
  if (reBtn) {
    reBtn.classList.remove('hidden');
    reBtn.dataset.savedId = isLocal ? (item.saved_id || '') : '';
    reBtn.dataset.id      = isLocal ? (item.cam_id || '') : item.id;
    reBtn.dataset.kind    = item.kind;
    reBtn.textContent     = '🔬';
    reBtn.disabled        = false;
  }

  // Chat: shown only when chat is enabled in settings
  const chatBtn = el('lb-chat-btn');
  if (chatBtn) {
    chatBtn.classList.toggle('hidden', !S.chatEnabled);
    chatBtn.dataset.savedId = isLocal ? (item.saved_id || '') : '';
    chatBtn.dataset.id      = isLocal ? (item.cam_id || '') : item.id;
    chatBtn.dataset.kind    = item.kind;
  }

  if (item.kind === 'jpg') {
    const img = document.createElement('img');
    img.alt = isLocal ? `Saved photo` : `Photo ${item.id}`;
    img.onerror = () => {
      img.onerror = null;
      img.src = thumbUrl;
      const note = document.createElement('p');
      note.className = 'lb-offline-note';
      note.textContent = isLocal
        ? 'Could not load saved file'
        : 'Cached thumbnail — connect for full resolution';
      content.appendChild(note);
    };
    img.src = fileUrl;
    content.appendChild(img);
  } else {
    const video = document.createElement('video');
    video.controls = true;
    video.setAttribute('playsinline', '');
    video.onerror = () => {
      content.innerHTML = '';
      const img = document.createElement('img');
      img.src = thumbUrl;
      img.alt = isLocal ? `Saved video` : `Video ${item.id}`;
      content.appendChild(img);
      const note = document.createElement('p');
      note.className = 'lb-offline-note';
      note.textContent = isLocal
        ? 'Could not load saved video'
        : (S.status !== 'connected'
            ? 'Connect to play video'
            : 'Could not load video. The file may still be transferring.');
      content.appendChild(note);
      appendAnalysisText(content);
    };
    video.src = fileUrl;
    content.appendChild(video);
  }

  // Analysis description — rendered below image/video for both gallery and local items
  function appendAnalysisText(container) {
    let analysis = null;
    if (isLocal) {
      analysis = item.analysis || null;
    } else {
      const aKey = `${item.id}:${item.kind}`;
      analysis = S.analysis[aKey] || null;
    }
    const desc = document.createElement('p');
    desc.id = 'lb-analysis-text';
    desc.className = 'lb-analysis';
    desc.textContent = (analysis && analysis.description) ? analysis.description : '';
    container.appendChild(desc);
  }
  appendAnalysisText(content);
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
  const items = S.lightboxSource === 'local' ? S.localItems : S.pageItems;
  if (S.lightboxIdx < items.length - 1) { S.lightboxIdx++; renderLightboxItem(); }
}

async function lightboxDelete() {
  if (S.lightboxSource === 'local') {
    const btn     = el('lb-delete-btn');
    const savedId = parseInt(btn.dataset.savedId, 10);
    const kind    = btn.dataset.kind;
    if (!confirm(`Delete this saved ${kind.toUpperCase()}?`)) return;
    const r = await fetch(`/api/saved/${savedId}`, { method: 'DELETE' });
    if (r.ok) {
      S.localItems = S.localItems.filter(i => i.saved_id !== savedId);
      closeLightbox();
      renderLocalGallery();
    } else {
      const d = await r.json().catch(() => ({}));
      alert(`Delete failed: ${d.detail || r.status}`);
    }
    return;
  }
  const btn  = el('lb-delete-btn');
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

async function lightboxReanalyze() {
  const btn     = el('lb-reanalyze-btn');
  const isLocal = S.lightboxSource === 'local';
  const savedId = parseInt(btn.dataset.savedId, 10);
  const id      = parseInt(btn.dataset.id, 10);
  const kind    = btn.dataset.kind;
  btn.textContent = '⏳';
  btn.disabled = true;
  try {
    const url = isLocal
      ? `/api/analysis/run/saved/${savedId}`
      : `/api/analysis/run/${id}/${kind}`;
    const r = await fetch(url, { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      alert(`Re-analyze failed: ${d.detail || r.status}`);
      return;
    }
    const result = { subjects: d.subjects || [], description: d.description || '' };
    // Update the analysis text in lightbox in-place
    const descEl = document.getElementById('lb-analysis-text');
    if (descEl) descEl.textContent = result.description;
    if (isLocal) {
      // Update the in-memory local item so lightbox re-opens correctly
      const item = S.localItems.find(i => i.saved_id === savedId);
      if (item) item.analysis = result;
      // Refresh local grid card
      const cards = document.querySelectorAll(`#local-grid .thumb-card[data-saved-id="${savedId}"]`);
      cards.forEach(card => applyAnalysisToLocalCard(card, result));
    } else {
      const key = `${id}:${kind}`;
      S.analysis[key] = result;
      const cards = document.querySelectorAll(`#gallery-grid .thumb-card[data-media-id="${id}"][data-media-kind="${kind}"]`);
      cards.forEach(card => applyAnalysisToCard(card, id, kind));
    }
  } catch (e) {
    alert(`Re-analyze failed: ${e.message}`);
  } finally {
    btn.textContent = '🔬';
    btn.disabled = false;
  }
}

function lightboxChat() {
  el('chat-prompt-input').value = el('analysis-prompt')?.value || '';
  el('chat-response').classList.add('hidden');
  el('chat-response').textContent = '';
  el('chat-status').textContent = '';
  el('chat-dialog').classList.remove('hidden');
}

function closeChatDialog() {
  el('chat-dialog').classList.add('hidden');
}

async function submitChat() {
  const prompt = el('chat-prompt-input').value.trim();
  if (!prompt) return;
  const btn    = el('lb-chat-btn');
  const submit = el('chat-submit-btn');
  const isLocal = S.lightboxSource === 'local';
  const savedId = parseInt(btn.dataset.savedId, 10);
  const id      = parseInt(btn.dataset.id, 10);
  const kind    = btn.dataset.kind;
  submit.disabled = true;
  el('chat-status').textContent = 'Thinking…';
  el('chat-response').classList.add('hidden');
  try {
    const url = isLocal
      ? `/api/analysis/chat/saved/${savedId}`
      : `/api/analysis/chat/${id}/${kind}`;
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt }),
    });
    const d = await r.json().catch(() => ({}));
    el('chat-status').textContent = '';
    el('chat-response').textContent = (!r.ok || d.error)
      ? `Error: ${d.error || d.detail || r.status}`
      : (d.response || '(no response)');
    el('chat-response').classList.remove('hidden');
  } catch (e) {
    el('chat-status').textContent = '';
    el('chat-response').textContent = `Error: ${e.message}`;
    el('chat-response').classList.remove('hidden');
  } finally {
    submit.disabled = false;
  }
}

async function lightboxSave() {
  const item = S.pageItems[S.lightboxIdx];
  if (!item) return;
  const btn = el('lb-save-btn');
  btn.disabled = true;
  btn.textContent = '…';
  try {
    const r = await fetch(`/api/save/${item.id}/${item.kind}`, { method: 'POST' });
    if (r.ok) {
      btn.textContent = '✓';
      setTimeout(() => { btn.textContent = '💾'; btn.disabled = false; }, 1500);
      return;
    }
    const d = await r.json().catch(() => ({}));
    alert(`Save failed: ${d.detail || r.status}`);
  } catch (e) {
    alert(`Save failed: ${e.message}`);
  }
  btn.textContent = '💾';
  btn.disabled = false;
}

// Keyboard navigation in lightbox
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (!el('chat-dialog').classList.contains('hidden')) { closeChatDialog(); return; }
    if (el('lightbox').classList.contains('open')) closeLightbox();
    return;
  }
  if (!el('lightbox').classList.contains('open')) return;
  if (e.key === 'ArrowLeft')  lightboxPrev();
  if (e.key === 'ArrowRight') lightboxNext();
});

// ── Settings tab ──────────────────────────────────────────────────────────────

function updateSettingsTab() {
  const connected = S.status === 'connected';
  show('settings-disconnected', !connected);
  show('settings-content', connected);
}

async function loadAnalysisConfig() {
  try {
    const r = await fetch('/api/analysis/config');
    if (!r.ok) return;
    const d = await r.json();
    el('analysis-enabled').checked   = !!d.analyze_enabled;
    el('alerts-enabled').checked     = !!d.alerts_enabled;
    el('analysis-backend').value     = d.backend || 'llm';
    el('analysis-llm-url').value     = d.llm_url || '';
    el('analysis-llm-model').value   = d.llm_model || '';
    el('analysis-anthropic-model').value = d.anthropic_model || '';
    el('analysis-key-status').textContent = d.anthropic_key_set ? '✓ API key set' : '✗ API key not set in /etc/gardepro.env';
    el('analysis-prompt').value      = d.prompt || '';
    el('analysis-max-tokens').value       = d.max_tokens || 800;
    el('analysis-thinking-budget').value  = d.thinking_budget ?? 2048;
    const temp = parseFloat(d.temperature ?? 0.1);
    el('analysis-temperature').value = temp;
    el('analysis-temp-val').textContent = temp.toFixed(2);
    S.chatEnabled = !!d.chat_enabled;
    el('chat-enabled').checked = S.chatEnabled;
    toggleAnalysisBackend();

    // Email status
    const emailEl = el('alert-email-status');
    const testBtn = el('test-email-btn');
    if (d.alert_email) {
      emailEl.textContent = `✓ ${d.alert_email}`;
      emailEl.style.color = 'var(--green, #22c55e)';
      testBtn.classList.remove('hidden');
    } else {
      emailEl.textContent = 'Not configured — set GARDEPRO_ALERT_EMAIL in /etc/gardepro.env';
      emailEl.style.color = '';
      testBtn.classList.add('hidden');
    }

    // Cooldown
    el('alert-cooldown-minutes').value = d.alert_cooldown_minutes ?? 30;

    // Per-rule toggles
    const rulesEnabled = d.alert_rules_enabled || {};
    const rules = d.alert_rules || [];
    const container = el('alert-rule-toggles');
    if (rules.length) {
      container.innerHTML = rules.map(name => {
        const checked = (name in rulesEnabled) ? rulesEnabled[name] : true;
        const label = name.charAt(0).toUpperCase() + name.slice(1);
        return `<div class="form-row">
          <label>${label} alerts</label>
          <label class="toggle"><input type="checkbox" id="alert-rule-${name}"${checked ? ' checked' : ''}><span class="toggle-slider"></span></label>
        </div>`;
      }).join('');
    } else {
      container.innerHTML = '<div class="form-row"><span class="muted hint">No rules — configure in ~/.gardepro/alerts.yaml</span></div>';
    }
  } catch (_) {}
}

function toggleAnalysisBackend() {
  const isLlm = el('analysis-backend').value === 'llm';
  show('analysis-llm-fields', isLlm);
  show('analysis-anthropic-fields', !isLlm);
}

function _setConfigStatus(msg) {
  ['analysis-save-status', 'alerts-save-status'].forEach(id => {
    const el_ = document.getElementById(id);
    if (el_) el_.textContent = msg;
  });
}

async function saveAnalysisConfig() {
  _setConfigStatus('Saving…');
  try {
    const alertRulesEnabled = {};
    document.querySelectorAll('[id^="alert-rule-"]').forEach(cb => {
      alertRulesEnabled[cb.id.replace('alert-rule-', '')] = cb.checked;
    });
    const body = {
      analyze_enabled:        el('analysis-enabled').checked,
      alerts_enabled:         el('alerts-enabled').checked,
      backend:                el('analysis-backend').value,
      llm_url:                el('analysis-llm-url').value.trim(),
      llm_model:              el('analysis-llm-model').value.trim(),
      anthropic_model:        el('analysis-anthropic-model').value.trim(),
      prompt:                 el('analysis-prompt').value,
      max_tokens:             parseInt(el('analysis-max-tokens').value, 10),
      thinking_budget:        parseInt(el('analysis-thinking-budget').value, 10) || 0,
      temperature:            parseFloat(el('analysis-temperature').value),
      alert_cooldown_minutes: parseInt(el('alert-cooldown-minutes').value, 10) || 0,
      alert_rules_enabled:    alertRulesEnabled,
      chat_enabled:           el('chat-enabled').checked,
    };
    const r = await fetch('/api/analysis/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      _setConfigStatus('Error: ' + (d.detail || r.status));
      return;
    }
    S.chatEnabled = el('chat-enabled').checked;
    _setConfigStatus('✓ Saved');
    setTimeout(() => { _setConfigStatus(''); }, 2500);
  } catch (e) {
    _setConfigStatus('Error: ' + e.message);
  }
}

async function testAlertEmail() {
  const btn = el('test-email-btn');
  const status = el('test-email-status');
  btn.disabled = true;
  status.textContent = 'Sending…';
  status.style.color = '';
  try {
    const r = await fetch('/api/alert/test-email', { method: 'POST' });
    if (r.ok) {
      status.textContent = '✓ Sent — check your inbox';
      status.style.color = 'var(--green, #22c55e)';
    } else {
      const d = await r.json().catch(() => ({}));
      status.textContent = 'Failed: ' + (d.detail || r.status);
      status.style.color = 'var(--red, #ef4444)';
    }
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
    status.style.color = 'var(--red, #ef4444)';
  }
  btn.disabled = false;
  setTimeout(() => { status.textContent = ''; }, 6000);
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
  const label = el('last-synced-label');
  if (S.lastSynced) {
    const diffMin = (Date.now() - new Date(S.lastSynced).getTime()) / 60000;
    const stale = diffMin > 12;
    label.textContent = 'Last synced: ' + formatRelativeTime(S.lastSynced)
      + (stale ? ' ⚠' : '');
    label.classList.toggle('stale', stale);
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

function updateLastEvent() {
  const lbl = el('last-event-label');
  if (lbl) lbl.textContent = 'Last event: ' + formatEventAge(S.lastEvent);
}

async function syncNow() {
  await fetch('/api/sync', { method: 'POST' });
}

function formatRelativeTime(isoStr) {
  const diff = (Date.now() - new Date(isoStr).getTime()) / 1000;
  if (diff < 60)    return 'just now';
  if (diff < 3600)  return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

function formatEventAge(isoStr) {
  if (!isoStr) return 'never';
  const diff = Math.abs((Date.now() - new Date(isoStr).getTime()) / 1000);
  if (diff < 60)       { const s  = Math.floor(diff);           return s  + (s  === 1 ? ' second' : ' seconds') + ' ago'; }
  if (diff < 3600)     { const m  = Math.floor(diff / 60);      return m  + (m  === 1 ? ' minute' : ' minutes') + ' ago'; }
  if (diff < 86400)    { const h  = Math.floor(diff / 3600);    return h  + (h  === 1 ? ' hour'   : ' hours')   + ' ago'; }
  if (diff < 604800)   { const d  = Math.floor(diff / 86400);   return d  + (d  === 1 ? ' day'    : ' days')    + ' ago'; }
  if (diff < 2592000)  { const w  = Math.floor(diff / 604800);  return w  + (w  === 1 ? ' week'   : ' weeks')   + ' ago'; }
  if (diff < 31536000) { const mo = Math.floor(diff / 2592000); return mo + (mo === 1 ? ' month'  : ' months')  + ' ago'; }
  const y = Math.floor(diff / 31536000);
  return y + (y === 1 ? ' year' : ' years') + ' ago';
}

// ── Local tab ─────────────────────────────────────────────────────────────────

async function loadLocalMedia() {
  try {
    const r = await fetch('/api/saved');
    const d = await r.json();
    S.localItems = d.items || [];
  } catch (_) {
    S.localItems = [];
  }
  renderLocalGallery();
}

function renderLocalGallery() {
  const grid  = el('local-grid');
  const empty = el('local-empty');
  const count = el('local-count');
  count.textContent = S.localItems.length ? `${S.localItems.length} saved` : '';
  if (!S.localItems.length) {
    grid.innerHTML = '';
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');
  grid.innerHTML = '';
  S.localItems.forEach((item, idx) => grid.appendChild(makeLocalThumbCard(item, idx)));
}

function makeLocalThumbCard(item, idx) {
  const card = document.createElement('div');
  card.className = 'thumb-card';
  card.dataset.mediaKind = item.kind;
  card.dataset.savedId   = item.saved_id;

  const img = document.createElement('img');
  img.src = `/api/saved/thumb/${item.saved_id}`;
  img.loading = 'lazy';
  img.alt = `Saved ${item.kind.toUpperCase()}`;
  img.draggable = false;
  card.appendChild(img);

  if (item.kind === 'mp4') {
    const badge = document.createElement('div');
    badge.className = 'video-badge';
    badge.textContent = '▶ MP4';
    card.appendChild(badge);
  }

  if (item.analysis) applyAnalysisToLocalCard(card, item.analysis);

  const dateLabel = document.createElement('div');
  dateLabel.className = 'saved-date-label';
  dateLabel.textContent = formatSavedAt(item.saved_at);
  card.appendChild(dateLabel);

  card.addEventListener('click', () => {
    S.lightboxSource = 'local';
    S.lightboxIdx = idx;
    el('lightbox').classList.add('open');
    renderLightboxItem();
  });
  return card;
}

function formatSavedAt(savedAt) {
  const d = new Date(savedAt);
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    + ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

// ── Logs tab ──────────────────────────────────────────────────────────────────

function appendLog(entry, isError) {
  const output = el('logs-output');
  if (!output) return;
  const line = document.createElement('div');
  line.className = isError || entry.level === 'error' ? 'log-line log-line--error' : 'log-line';
  const ts = document.createElement('span');
  ts.className = 'log-ts';
  ts.textContent = entry.ts;
  const msg = document.createElement('span');
  msg.className = 'log-msg';
  msg.textContent = entry.msg;
  line.appendChild(ts);
  line.appendChild(msg);
  output.appendChild(line);
  // Trim to 300 lines
  while (output.children.length > 300) output.removeChild(output.firstChild);
  const autoScroll = el('logs-autoscroll');
  if (autoScroll && autoScroll.checked) output.scrollTop = output.scrollHeight;
}

async function fetchLogs() {
  try {
    const r = await fetch('/api/logs');
    const d = await r.json();
    const output = el('logs-output');
    if (!output) return;
    output.innerHTML = '';
    (d.entries || []).forEach(appendLog);
    output.scrollTop = output.scrollHeight;
  } catch (_) {}
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

// Refresh relative timestamps every 60 s so "last synced/event" doesn't go stale
setInterval(() => { updateOfflineBar(); updateLastEvent(); }, 60000);

// Load cached analysis data on startup so borders show even before connecting
fetchAnalysis();

startSSE();
