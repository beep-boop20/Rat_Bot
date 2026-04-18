// Utility functions for TheRatBot

function formatRelativeTime(timestamp) {
    const normalized = typeof timestamp === 'string' && !timestamp.endsWith('Z')
        ? timestamp + 'Z' : timestamp;
    const diffMs = Date.now() - new Date(normalized).getTime();
    const sec = Math.floor(diffMs / 1000);
    const min = Math.floor(sec / 60);
    const hr = Math.floor(min / 60);
    const day = Math.floor(hr / 24);

    if (sec < 60) return 'just now';
    if (min < 60) return `${min} min ago`;
    if (hr < 24) return `${hr}h ago`;
    if (day < 7) return `${day}d ago`;
    return new Date(normalized).toLocaleDateString();
}

function updateRelativeTimestamps() {
    document.querySelectorAll('[data-timestamp]').forEach(el => {
        const ts = el.getAttribute('data-timestamp');
        if (ts) el.textContent = formatRelativeTime(ts);
    });
}

// Update timestamps every 30s
setInterval(updateRelativeTimestamps, 30000);
document.addEventListener('DOMContentLoaded', updateRelativeTimestamps);
