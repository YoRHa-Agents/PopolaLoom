/* PopolaLoom v0.8.3 — client-side day/night theme toggle (vanilla, no deps).
 *
 * State machine: two themes (light ↔ dark), persisted in localStorage.
 *
 * Resolve order (getCurrentTheme):
 *   1. localStorage['popola.theme']  ∈ {'light','dark'}  → use it
 *   2. window.matchMedia('(prefers-color-scheme: dark)') → 'dark'
 *   3. fallback                                          → 'light'
 *
 * apply(theme):
 *   - sets <html data-theme="light|dark">
 *   - updates [data-theme-toggle] glyph to the *target* (☾ when current is
 *     light → click goes to dark; ☀ when current is dark → click goes to
 *     light) + corresponding aria-label
 *
 * First-paint FOUC for OS-dark visitors is further mitigated by the
 * @media (prefers-color-scheme: dark) :root:not([data-theme="light"]) {…}
 * fallback in nier-popola.css, which lets dark CSS variables apply before
 * this script runs (and is overridden the moment we set data-theme="light"
 * explicitly).
 */
(function () {
  'use strict';

  const STORAGE_KEY = 'popola.theme';
  const DEFAULT_THEME = 'light';
  const SUPPORTED = ['light', 'dark'];

  // current theme → glyph showing the *target* (what a click would switch to)
  const GLYPH_FOR_NEXT = { light: '\u263E', dark: '\u2600' };  // ☾ / ☀
  const ARIA_FOR_NEXT = {
    light: 'Switch to dark theme',
    dark: 'Switch to light theme',
  };

  function readStored() {
    try {
      const v = localStorage.getItem(STORAGE_KEY);
      if (v && SUPPORTED.includes(v)) return v;
    } catch (err) {
      console.error('[popola.theme] localStorage read failed (private mode?):', err);
    }
    return null;
  }

  function osPrefersDark() {
    if (typeof window.matchMedia !== 'function') return false;
    try {
      return window.matchMedia('(prefers-color-scheme: dark)').matches;
    } catch (err) {
      console.error('[popola.theme] matchMedia evaluation failed:', err);
      return false;
    }
  }

  function getCurrentTheme() {
    const stored = readStored();
    if (stored) return stored;
    return osPrefersDark() ? 'dark' : DEFAULT_THEME;
  }

  function writeStored(theme) {
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch (err) {
      console.error('[popola.theme] localStorage write failed (quota / privacy?):', err);
    }
  }

  function apply(theme) {
    if (!SUPPORTED.includes(theme)) {
      console.error('[popola.theme] unsupported theme, ignoring:', theme);
      return;
    }
    document.documentElement.setAttribute('data-theme', theme);
    document.querySelectorAll('[data-theme-toggle]').forEach(btn => {
      btn.textContent = GLYPH_FOR_NEXT[theme];
      btn.setAttribute('aria-label', ARIA_FOR_NEXT[theme]);
    });
  }

  function initToggle() {
    document.querySelectorAll('[data-theme-toggle]').forEach(btn => {
      btn.addEventListener('click', () => {
        const cur = getCurrentTheme();
        const next = cur === 'light' ? 'dark' : 'light';
        writeStored(next);
        apply(next);
      });
    });
  }

  // Pre-apply theme as early as the script can run. With `defer`, the parser
  // has finished by the time we run, so the toggle button is already in the
  // DOM and its glyph/aria-label can be updated in this same tick.
  apply(getCurrentTheme());

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initToggle);
  } else {
    initToggle();
  }

  // Track OS preference changes — but only follow them when the user has
  // not yet made an explicit pick (localStorage empty). An explicit pick
  // is a user signal we must respect over OS-level shifts.
  if (typeof window.matchMedia === 'function') {
    try {
      const mq = window.matchMedia('(prefers-color-scheme: dark)');
      const handler = e => {
        if (readStored()) return;
        apply(e.matches ? 'dark' : 'light');
      };
      if (typeof mq.addEventListener === 'function') {
        mq.addEventListener('change', handler);
      } else if (typeof mq.addListener === 'function') {
        mq.addListener(handler);
      }
    } catch (err) {
      console.error('[popola.theme] matchMedia change-listener wiring failed:', err);
    }
  }
})();
