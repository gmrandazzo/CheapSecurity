let currentSettings = { notifications_enabled: false, auth_enabled: false };

async function loadStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();

    document.getElementById('status-res').textContent = data.resolution || '—';
    document.getElementById('status-fps').textContent = data.fps || '—';
    document.getElementById('status-rec').textContent = data.is_recording ? 'Yes' : 'No';
    document.getElementById('status-motion').textContent = data.motion_active ? 'Detected' : 'None';
    document.getElementById('status-file').textContent = data.recording_file || '—';

    const badge = document.getElementById('status-badge');
    badge.className = 'badge';
    if (data.is_recording) {
      badge.textContent = '🔴 Recording';
      badge.classList.add('recording');
    } else if (data.motion_active) {
      badge.textContent = '⚠ Motion';
      badge.classList.add('motion');
    } else {
      badge.textContent = 'Live';
      badge.classList.add('ok');
    }

    updateToggles(data.notifications_enabled, data.auth_enabled);
  } catch (e) {
    document.getElementById('status-badge').textContent = 'Offline';
  }
}

async function loadRecordings() {
  const list = document.getElementById('recordings');
  list.innerHTML = '<li class="empty">Loading…</li>';
  try {
    const res = await fetch('/api/recordings');
    const data = await res.json();
    if (!data.recordings || data.recordings.length === 0) {
      list.innerHTML = '<li class="empty">No recordings yet.</li>';
      return;
    }
    list.innerHTML = data.recordings.map(r => `
      <li>
        <div>
          <a href="/recordings/${encodeURIComponent(r.filename)}" target="_blank">${r.filename}</a>
          <div class="meta">${r.size_human} &bull; ${new Date(r.created).toLocaleString()}</div>
        </div>
      </li>
    `).join('');
  } catch (e) {
    list.innerHTML = '<li class="empty">Failed to load recordings.</li>';
  }
}

async function loadSettings() {
  try {
    const res = await fetch('/api/settings');
    const data = await res.json();
    updateToggles(data.notifications_enabled, data.auth_enabled);
  } catch (e) {
    console.error('Failed to load settings', e);
  }
}

function updateToggles(notifications, auth) {
  currentSettings = { notifications_enabled: notifications, auth_enabled: auth };
  document.getElementById('notif-toggle').checked = notifications;
  document.getElementById('auth-toggle').checked = auth;
}

async function setNotifications(enabled) {
  try {
    const res = await fetch('/api/settings/notifications', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled })
    });
    const data = await res.json();
    updateToggles(data.notifications_enabled, currentSettings.auth_enabled);
  } catch (e) {
    alert('Failed to update notifications setting');
    loadSettings();
  }
}

async function setAuth(enabled) {
  try {
    const res = await fetch('/api/settings/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled })
    });
    const data = await res.json();
    updateToggles(currentSettings.notifications_enabled, data.auth_enabled);
  } catch (e) {
    alert('Failed to update auth setting');
    loadSettings();
  }
}

loadStatus();
loadSettings();
loadRecordings();
setInterval(loadStatus, 2000);
setInterval(loadRecordings, 30000);
