// Picture controls for the office. Defaults match the picture before the panel existed.
// The browser remembers the last choice; the scene applies it on the next frame.

const KEY = 'bothq.render';
const COARSE = typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;

function urlTilt() {
  try { return new URLSearchParams(location.search).get('tiltshift') !== '0'; }
  catch { return true; }
}

export const RENDER_DEFAULTS = {
  scale: 1,
  shadow: COARSE ? 1024 : 2048,
  tilt: urlTilt(),
  aura: true,
  lamps: true,
  aa: 0,
};

function oneOf(value, allowed, fallback) {
  return allowed.includes(value) ? value : fallback;
}

export function normalizeRenderSettings(raw) {
  const src = raw && typeof raw === 'object' ? raw : {};
  return {
    scale: oneOf(src.scale, [0.5, 0.75, 1], RENDER_DEFAULTS.scale),
    shadow: oneOf(src.shadow, [0, 1024, 2048], RENDER_DEFAULTS.shadow),
    tilt: src.tilt == null ? RENDER_DEFAULTS.tilt : !!src.tilt,
    aura: src.aura == null ? RENDER_DEFAULTS.aura : !!src.aura,
    lamps: src.lamps == null ? RENDER_DEFAULTS.lamps : !!src.lamps,
    aa: oneOf(src.aa, [0, 2, 4], RENDER_DEFAULTS.aa),
  };
}

function load() {
  try { return normalizeRenderSettings(JSON.parse(localStorage.getItem(KEY) || '')); }
  catch { return normalizeRenderSettings(null); }
}

function same(a, b) {
  return a.scale === b.scale && a.shadow === b.shadow && a.tilt === b.tilt
    && a.aura === b.aura && a.lamps === b.lamps && a.aa === b.aa;
}

let state = load();
const listeners = new Set();

export function renderSettings() {
  return { ...state };
}

export function setRenderSettings(patch) {
  const next = normalizeRenderSettings({ ...state, ...patch });
  if (same(next, state)) return;
  state = next;
  try { localStorage.setItem(KEY, JSON.stringify(state)); } catch { /* private mode: this visit only */ }
  for (const fn of listeners) fn(renderSettings());
}

export function onRenderSettings(fn) {
  listeners.add(fn);
  fn(renderSettings());
  return () => listeners.delete(fn);
}

window.bothq = Object.assign(window.bothq || {}, { renderSettings, setRenderSettings });
