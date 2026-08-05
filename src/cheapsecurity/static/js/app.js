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

let currentSettings = { night_mode: false, night_mode_strength: 'normal', night_device_active: false, night_device_configured: false, notifications_enabled: false, telegram_enabled: false, auth_enabled: false };
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;

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

    updateToggles(data.night_mode, data.night_mode_strength, data.night_device_active, data.night_device_configured, data.notifications_enabled, data.telegram_enabled, data.auth_enabled);
  } catch (e) {
    document.getElementById('status-badge').textContent = 'Offline';
  }
}

let allRecordings = [];
let selectedDateStr = null;
let currentCalendarDate = new Date();

function formatDateKey(dateObj) {
  const y = dateObj.getFullYear();
  const m = String(dateObj.getMonth() + 1).padStart(2, '0');
  const d = String(dateObj.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

function getRecordingsByDateMap() {
  const map = new Map();
  allRecordings.forEach(r => {
    // Use the server-provided UTC date string to avoid timezone drift.
    const key = r.created_date || formatDateKey(new Date(r.created));
    if (!map.has(key)) {
      map.set(key, []);
    }
    map.get(key).push(r);
  });
  return map;
}

function renderCalendar() {
  const monthYearEl = document.getElementById('calendar-month-year');
  const daysEl = document.getElementById('calendar-days');
  if (!daysEl || !monthYearEl) return;

  const year = currentCalendarDate.getFullYear();
  const month = currentCalendarDate.getMonth();

  const monthNames = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'
  ];
  monthYearEl.textContent = `${monthNames[month]} ${year}`;

  const firstDay = new Date(year, month, 1).getDay();
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const daysInPrevMonth = new Date(year, month, 0).getDate();

  const recordingsByDate = getRecordingsByDateMap();
  const todayKey = formatDateKey(new Date());

  let html = '';

  // Previous month trailing days
  for (let i = firstDay - 1; i >= 0; i--) {
    const dayNum = daysInPrevMonth - i;
    html += `<div class="calendar-day other-month"><span class="day-num">${dayNum}</span></div>`;
  }

  // Current month days
  for (let day = 1; day <= daysInMonth; day++) {
    const dateObj = new Date(year, month, day);
    const dateKey = formatDateKey(dateObj);
    const dayRecordings = recordingsByDate.get(dateKey) || [];
    const count = dayRecordings.length;
    const hasRecs = count > 0;
    const isSelected = dateKey === selectedDateStr;
    const isToday = dateKey === todayKey;

    let classes = ['calendar-day'];
    if (hasRecs) classes.push('has-recordings');
    if (isSelected) classes.push('selected-day');
    if (isToday) classes.push('today');

    let redIndicator = '';
    if (hasRecs) {
      redIndicator = `<span class="red-circle-badge" title="${count} recording(s)">${count}</span>`;
    }

    const clickAttr = hasRecs ? `onclick="selectCalendarDate('${dateKey}')"` : '';

    html += `
      <div class="${classes.join(' ')}" ${clickAttr} data-date="${dateKey}">
        <span class="day-num">${day}</span>
        ${redIndicator}
      </div>
    `;
  }

  // Next month leading days to complete full weeks
  const totalCells = firstDay + daysInMonth;
  const remainingCells = (7 - (totalCells % 7)) % 7;
  for (let day = 1; day <= remainingCells; day++) {
    html += `<div class="calendar-day other-month"><span class="day-num">${day}</span></div>`;
  }

  daysEl.innerHTML = html;
}

function changeMonth(delta) {
  currentCalendarDate.setMonth(currentCalendarDate.getMonth() + delta);
  renderCalendar();
}

function selectCalendarDate(dateKey) {
  if (selectedDateStr === dateKey) {
    selectedDateStr = null;
  } else {
    selectedDateStr = dateKey;
  }
  renderCalendar();
  renderRecordingsList();
}

function clearDateFilter() {
  selectedDateStr = null;
  renderCalendar();
  renderRecordingsList();
}

function renderRecordingsList() {
  const list = document.getElementById('recordings');
  const titleEl = document.getElementById('recordings-title');
  const clearBtn = document.getElementById('clear-filter-btn');

  document.getElementById('select-all').checked = false;
  updateActionButtons();

  let filtered = allRecordings;
  if (selectedDateStr) {
    filtered = allRecordings.filter(r => (r.created_date || formatDateKey(new Date(r.created))) === selectedDateStr);
    const dateFormatted = new Date(selectedDateStr + 'T00:00:00').toLocaleDateString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric'
    });
    if (titleEl) titleEl.textContent = `Recordings for ${dateFormatted} (${filtered.length})`;
    if (clearBtn) clearBtn.style.display = 'inline-block';
  } else {
    if (titleEl) titleEl.textContent = `Recordings (${allRecordings.length})`;
    if (clearBtn) clearBtn.style.display = 'none';
  }

  if (filtered.length === 0) {
    list.innerHTML = selectedDateStr
      ? `<li class="empty">No recordings on ${selectedDateStr}.</li>`
      : '<li class="empty">No recordings yet.</li>';
    return;
  }

  list.innerHTML = filtered.map(r => `
    <li>
      <input type="checkbox" class="rec-checkbox" value="${encodeURIComponent(r.filename)}" onchange="updateActionButtons()">
      <div class="rec-info">
        <a href="/recordings/${encodeURIComponent(r.filename)}" target="_blank">${r.filename}</a>
        <div class="meta">${r.size_human} &bull; ${new Date(r.created).toLocaleString()}</div>
      </div>
    </li>
  `).join('');
}

async function loadRecordings() {
  const list = document.getElementById('recordings');
  list.innerHTML = '<li class="empty">Loading…</li>';
  document.getElementById('select-all').checked = false;
  updateActionButtons();
  try {
    const res = await fetch('/api/recordings');
    const data = await res.json();
    allRecordings = data.recordings || [];
    renderCalendar();
    renderRecordingsList();
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
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRF-Token': csrfToken },
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
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRF-Token': csrfToken },
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
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRF-Token': csrfToken },
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

let currentSettings = { night_mode: false, night_mode_strength: 'normal', night_device_active: false, night_device_configured: false, notifications_enabled: false, telegram_enabled: false, gdrive_enabled: false, onedrive_enabled: false, auth_enabled: false, encryption_passphrase: '', encrypt_telegram: false, encrypt_gdrive: false, encrypt_onedrive: false };

async function loadSettings() {
  try {
    const res = await fetch('/api/settings');
    const data = await res.json();
    updateToggles(data.night_mode, data.night_mode_strength, data.night_device_active, data.night_device_configured, data.notifications_enabled, data.telegram_enabled, data.gdrive_enabled, data.onedrive_enabled, data.auth_enabled, data.encryption_passphrase, data.encrypt_telegram, data.encrypt_gdrive, data.encrypt_onedrive);
  } catch (e) {
    console.error('Failed to load settings', e);
  }
}

function updateToggles(night, nightStrength, nightDeviceActive, nightDeviceConfigured, notifications, telegram, gdrive, onedrive, auth, passphrase, encTel, encGDrive, encOneDrive) {
  const next = { ...currentSettings };
  if (night !== undefined) next.night_mode = night;
  if (nightStrength !== undefined) next.night_mode_strength = nightStrength;
  if (nightDeviceActive !== undefined) next.night_device_active = nightDeviceActive;
  if (nightDeviceConfigured !== undefined) next.night_device_configured = nightDeviceConfigured;
  if (notifications !== undefined) next.notifications_enabled = notifications;
  if (telegram !== undefined) next.telegram_enabled = telegram;
  if (gdrive !== undefined) next.gdrive_enabled = gdrive;
  if (onedrive !== undefined) next.onedrive_enabled = onedrive;
  if (auth !== undefined) next.auth_enabled = auth;
  if (passphrase !== undefined) next.encryption_passphrase = passphrase || '';
  if (encTel !== undefined) next.encrypt_telegram = encTel;
  if (encGDrive !== undefined) next.encrypt_gdrive = encGDrive;
  if (encOneDrive !== undefined) next.encrypt_onedrive = encOneDrive;
  currentSettings = next;

  document.getElementById('night-toggle').checked = next.night_mode;
  document.getElementById('night-strength').value = next.night_mode_strength || 'normal';
  document.getElementById('notif-toggle').checked = next.notifications_enabled;
  document.getElementById('telegram-toggle').checked = next.telegram_enabled;
  if (document.getElementById('gdrive-toggle')) document.getElementById('gdrive-toggle').checked = Boolean(next.gdrive_enabled);
  if (document.getElementById('onedrive-toggle')) document.getElementById('onedrive-toggle').checked = Boolean(next.onedrive_enabled);
  document.getElementById('auth-toggle').checked = next.auth_enabled;
  if (document.getElementById('enc-passphrase')) document.getElementById('enc-passphrase').value = next.encryption_passphrase;
  if (document.getElementById('enc-telegram-toggle')) document.getElementById('enc-telegram-toggle').checked = Boolean(next.encrypt_telegram);
  if (document.getElementById('enc-gdrive-toggle')) document.getElementById('enc-gdrive-toggle').checked = Boolean(next.encrypt_gdrive);
  if (document.getElementById('enc-onedrive-toggle')) document.getElementById('enc-onedrive-toggle').checked = Boolean(next.encrypt_onedrive);
  updateNightCameraStatus();
}

function updateNightCameraStatus() {
  const statusEl = document.getElementById('night-camera-status');
  const labelEl = document.getElementById('night-camera-label');
  if (!statusEl || !labelEl) return;
  if (currentSettings.night_device_configured) {
    statusEl.style.display = 'block';
    labelEl.textContent = currentSettings.night_device_active ? 'IR/night camera' : 'Day camera';
  } else {
    statusEl.style.display = 'none';
  }
}

async function setNightMode(enabled) {
  try {
    const strength = document.getElementById('night-strength').value;
    const res = await fetch('/api/settings/night_mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ enabled, strength })
    });
    const data = await res.json();
    updateToggles(data.night_mode, data.night_mode_strength, data.night_device_active, data.night_device_configured, currentSettings.notifications_enabled, currentSettings.telegram_enabled, currentSettings.auth_enabled);
  } catch (e) {
    alert('Failed to update night mode');
    loadSettings();
  }
}

async function setNightModeStrength(strength) {
  try {
    const enabled = document.getElementById('night-toggle').checked;
    const res = await fetch('/api/settings/night_mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ enabled, strength })
    });
    const data = await res.json();
    updateToggles(data.night_mode, data.night_mode_strength, data.night_device_active, data.night_device_configured, currentSettings.notifications_enabled, currentSettings.telegram_enabled, currentSettings.auth_enabled);
  } catch (e) {
    alert('Failed to update night mode strength');
    loadSettings();
  }
}

async function setTelegram(enabled) {
  try {
    const res = await fetch('/api/settings/telegram', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ enabled })
    });
    const data = await res.json();
    updateToggles(currentSettings.night_mode, currentSettings.night_mode_strength, currentSettings.night_device_active, currentSettings.night_device_configured, currentSettings.notifications_enabled, data.telegram_enabled, currentSettings.auth_enabled);
  } catch (e) {
    alert('Failed to update Telegram setting');
    loadSettings();
  }
}

async function setNotifications(enabled) {
  try {
    const res = await fetch('/api/settings/notifications', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ enabled })
    });
    const data = await res.json();
    updateToggles(currentSettings.night_mode, currentSettings.night_mode_strength, currentSettings.night_device_active, currentSettings.night_device_configured, data.notifications_enabled, currentSettings.telegram_enabled, currentSettings.auth_enabled);
  } catch (e) {
    alert('Failed to update notifications setting');
    loadSettings();
  }
}

async function setGDrive(enabled) {
  try {
    const res = await fetch('/api/settings/gdrive', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ enabled })
    });
    const data = await res.json();
    updateToggles(currentSettings.night_mode, currentSettings.night_mode_strength, currentSettings.night_device_active, currentSettings.night_device_configured, currentSettings.notifications_enabled, currentSettings.telegram_enabled, data.gdrive_enabled, currentSettings.onedrive_enabled, currentSettings.auth_enabled);
  } catch (e) {
    alert('Failed to update Google Drive setting');
    loadSettings();
  }
}

async function setOneDrive(enabled) {
  try {
    const res = await fetch('/api/settings/onedrive', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ enabled })
    });
    const data = await res.json();
    updateToggles(currentSettings.night_mode, currentSettings.night_mode_strength, currentSettings.night_device_active, currentSettings.night_device_configured, currentSettings.notifications_enabled, currentSettings.telegram_enabled, currentSettings.gdrive_enabled, data.onedrive_enabled, currentSettings.auth_enabled);
  } catch (e) {
    alert('Failed to update OneDrive setting');
    loadSettings();
  }
}

async function setAuth(enabled) {
  try {
    const res = await fetch('/api/settings/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ enabled })
    });
    const data = await res.json();
    updateToggles(currentSettings.night_mode, currentSettings.night_mode_strength, currentSettings.night_device_active, currentSettings.night_device_configured, currentSettings.notifications_enabled, currentSettings.telegram_enabled, currentSettings.gdrive_enabled, currentSettings.onedrive_enabled, data.auth_enabled, currentSettings.encryption_passphrase, currentSettings.encrypt_telegram, currentSettings.encrypt_gdrive, currentSettings.encrypt_onedrive);
  } catch (e) {
    alert('Failed to update auth setting');
    loadSettings();
  }
}

async function saveEncryptionPassphrase() {
  const passphrase = document.getElementById('enc-passphrase').value;
  try {
    const res = await fetch('/api/settings/encryption', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ passphrase })
    });
    const data = await res.json();
    currentSettings.encryption_passphrase = data.encryption_passphrase;
    alert('Encryption passphrase saved successfully.');
  } catch (e) {
    alert('Failed to save encryption passphrase.');
    loadSettings();
  }
}

async function setEncryptionToggle(key, enabled) {
  const payload = {};
  payload[key] = enabled;
  try {
    const res = await fetch('/api/settings/encryption', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    updateToggles(currentSettings.night_mode, currentSettings.night_mode_strength, currentSettings.night_device_active, currentSettings.night_device_configured, currentSettings.notifications_enabled, currentSettings.telegram_enabled, currentSettings.gdrive_enabled, currentSettings.onedrive_enabled, currentSettings.auth_enabled, data.encryption_passphrase, data.encrypt_telegram, data.encrypt_gdrive, data.encrypt_onedrive);
  } catch (e) {
    alert('Failed to update encryption setting');
    loadSettings();
  }
}

loadStatus();
loadSettings();
loadRecordings();
setInterval(loadStatus, 2000);
setInterval(loadRecordings, 30000);
