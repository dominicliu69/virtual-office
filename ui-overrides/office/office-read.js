const KEY = 'office-read-seq';

function load() {
  try {
    const bag = JSON.parse(localStorage.getItem(KEY) || '');
    if (bag && typeof bag.read === 'object' && typeof bag.seen === 'object') return bag;
  } catch { /* a private window simply shows every current reply as unread */ }
  return { read: {}, seen: {} };
}

function save(bag) {
  try { localStorage.setItem(KEY, JSON.stringify(bag)); } catch { /* unread dots are optional */ }
}

export function noteAssistant(agent, seq) {
  const n = Number(seq) || 0;
  if (!agent || !n) return 0;
  const bag = load();
  bag.seen[agent] = Math.max(bag.seen[agent] || 0, n);
  save(bag);
  return bag.seen[agent];
}

export function rememberSeq(agent, seq) {
  const n = Number(seq) || 0;
  if (!agent || !n) return;
  const bag = load();
  bag.read[agent] = Math.max(bag.read[agent] || 0, n);
  bag.seen[agent] = Math.max(bag.seen[agent] || 0, n);
  save(bag);
}

export function markRead(agent) {
  if (!agent) return;
  const bag = load();
  const seen = bag.seen[agent] || 0;
  if (!seen) return;
  bag.read[agent] = Math.max(bag.read[agent] || 0, seen);
  save(bag);
}

export function hasUnread(agent) {
  if (!agent) return false;
  const bag = load();
  return (bag.seen[agent] || 0) > (bag.read[agent] || 0);
}

export function workingLine(text) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim();
  const parts = clean.split(/(?<=[。！？.!?])/).map((item) => item.trim()).filter(Boolean);
  return (parts[parts.length - 1] || clean).slice(0, 80);
}
