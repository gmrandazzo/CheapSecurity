/*
 * CheapSecurity - lightweight CCTV system for the Odroid XU4
 * Copyright (C) 2026  Marco
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published
 * by the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

let currentSettings = { night_mode: false, notifications_enabled: false, telegram_enabled: false, auth_enabled: false };

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

    updateToggles(data.night_mode, data.notifications_enabled, data.telegram_enabled, data.auth_enabled);
  } catch (e) {
    document.getElementById('status-badge').textContent = 'Offline';
  }
}

async function loadRecordings() {
  const list = document.getElementById('recordings');
  list.innerHTML = '<li class="empty">Loading…</li>';
  document.getElementById('select-all').checked = false;
  updateActionButtons();
  try {
    const res = await fetch('/api/recordings');
    const data = await res.json();
    if (!data.recordings || data.recordings.length === 0) {
      list.innerHTML = '<li class="empty">No recordings yet.</li>';
      return;
    }
    list.innerHTML = data.recordings.map(r => `
      <li>
        <input type="checkbox" class="rec-checkbox" value="${encodeURIComponent(r.filename)}" onchange="updateActionButtons()">
        <div class="rec-info">
          <a href="/recordings/${encodeURIComponent(r.filename)}" target="_blank">${r.filename}</a>
          <div class="meta">${r.size_human} &bull; ${new Date(r.created).toLocaleString()}</div>
        </div>
      </li>
    `).join('');
  } catch (e) {
    list.innerHTML = '<li class="empty">Failed to load recordings.</li>';
  }
}

function getSelectedFilenames() {
  return Array.from(document.querySelectorAll('.rec-checkbox:checked')).map(cb => decodeURIComponent(cb.value));
}

function updateActionButtons() {
  const selected = getSelectedFilenames().length;
  document.getElementById('telegram-btn').disabled = selected === 0;
  document.getElementById('download-btn').disabled = selected === 0;
  document.getElementById('delete-btn').disabled = selected === 0;
}

function toggleSelectAll(checked) {
  document.querySelectorAll('.rec-checkbox').forEach(cb => cb.checked = checked);
  updateActionButtons();
}

async function deleteSelected() {
  const filenames = getSelectedFilenames();
  if (filenames.length === 0) return;
  if (!confirm(`Delete ${filenames.length} selected recording(s)? This cannot be undone.`)) return;
  try {
    const res = await fetch('/api/recordings/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      body: JSON.stringify({ filenames })
    });
    const data = await res.json();
    const failed = data.results.filter(r => !r.deleted);
    if (failed.length > 0) {
      alert('Some files could not be deleted:\n' + failed.map(r => `${r.filename}: ${r.error}`).join('\n'));
    }
    loadRecordings();
  } catch (e) {
    alert('Failed to delete recordings');
  }
}

async function sendSelectedToTelegram() {
  const filenames = getSelectedFilenames();
  if (filenames.length === 0) return;
  if (!confirm(`Send ${filenames.length} selected video(s) to Telegram?`)) return;
  try {
    const res = await fetch('/api/recordings/telegram', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      body: JSON.stringify({ filenames })
    });
    const data = await res.json();
    const failed = data.results.filter(r => !r.sent);
    if (failed.length > 0) {
      alert('Some videos could not be sent:\n' + failed.map(r => `${r.filename}: ${r.error}`).join('\n'));
    } else {
      alert('Videos sent to Telegram.');
    }
  } catch (e) {
    alert('Failed to send videos to Telegram');
  }
}

async function downloadSelected() {
  const filenames = getSelectedFilenames();
  if (filenames.length === 0) return;
  try {
    const res = await fetch('/api/recordings/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      body: JSON.stringify({ filenames })
    });
    if (!res.ok) throw new Error('Download failed');
    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'cheapsecurity_recordings.zip';
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);
  } catch (e) {
    alert('Failed to download recordings');
  }
}

async function loadSettings() {
  try {
    const res = await fetch('/api/settings');
    const data = await res.json();
    updateToggles(data.night_mode, data.notifications_enabled, data.telegram_enabled, data.auth_enabled);
  } catch (e) {
    console.error('Failed to load settings', e);
  }
}

function updateToggles(night, notifications, telegram, auth) {
  currentSettings = { night_mode: night, notifications_enabled: notifications, telegram_enabled: telegram, auth_enabled: auth };
  document.getElementById('night-toggle').checked = night;
  document.getElementById('notif-toggle').checked = notifications;
  document.getElementById('telegram-toggle').checked = telegram;
  document.getElementById('auth-toggle').checked = auth;
}

async function setNightMode(enabled) {
  try {
    const res = await fetch('/api/settings/night_mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      body: JSON.stringify({ enabled })
    });
    const data = await res.json();
    updateToggles(data.night_mode, currentSettings.notifications_enabled, currentSettings.telegram_enabled, currentSettings.auth_enabled);
  } catch (e) {
    alert('Failed to update night mode');
    loadSettings();
  }
}

async function setTelegram(enabled) {
  try {
    const res = await fetch('/api/settings/telegram', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      body: JSON.stringify({ enabled })
    });
    const data = await res.json();
    updateToggles(currentSettings.night_mode, currentSettings.notifications_enabled, data.telegram_enabled, currentSettings.auth_enabled);
  } catch (e) {
    alert('Failed to update Telegram setting');
    loadSettings();
  }
}

async function setNotifications(enabled) {
  try {
    const res = await fetch('/api/settings/notifications', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      body: JSON.stringify({ enabled })
    });
    const data = await res.json();
    updateToggles(currentSettings.night_mode, data.notifications_enabled, currentSettings.telegram_enabled, currentSettings.auth_enabled);
  } catch (e) {
    alert('Failed to update notifications setting');
    loadSettings();
  }
}

async function setAuth(enabled) {
  try {
    const res = await fetch('/api/settings/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      body: JSON.stringify({ enabled })
    });
    const data = await res.json();
    updateToggles(currentSettings.night_mode, currentSettings.notifications_enabled, currentSettings.telegram_enabled, data.auth_enabled);
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
