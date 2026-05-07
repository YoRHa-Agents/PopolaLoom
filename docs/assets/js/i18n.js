/* PopolaLoom v0.8.1 — client-side i18n switcher (vanilla, no deps).
 *
 * Strategy: localStorage-persisted lang state + per-page fetch of JSON dict
 * + DOM textContent rewrite over [data-i18n="key"] attributes. No router
 * push, no URL mutation, no page reload — pure DOM update.
 *
 * Fallback chain: current-lang dict → EN dict → key literal (so missing
 * keys are visible in dev, never silently empty).
 */
(function () {
  'use strict';

  const STORAGE_KEY = 'popola.lang';
  const DEFAULT_LANG = 'en';
  const SUPPORTED_LANGS = ['en', 'zh'];
  const HTML_LANG_MAP = { en: 'en', zh: 'zh-CN' };

  // Derive baseurl from this script's own src so the same code works under
  // /PopolaLoom (GitHub Pages) and at site root (jekyll serve --baseurl '').
  // <html data-baseurl="..."> wins if set (escape hatch for Stage A).
  function deriveBaseUrl() {
    const fromDataset = document.documentElement.dataset.baseurl;
    if (fromDataset != null) return fromDataset;
    const me = document.currentScript;
    if (me && me.src) {
      try {
        const u = new URL(me.src);
        const idx = u.pathname.indexOf('/assets/js/i18n.js');
        if (idx >= 0) return u.pathname.slice(0, idx);
      } catch (err) {
        console.error('[popola.i18n] failed to derive baseurl from script src:', err);
      }
    }
    return '';
  }
  const I18N_BASE = deriveBaseUrl() + '/assets/i18n/';

  const _dicts = Object.create(null);
  let _enDict = null;

  function getCurrentLang() {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored && SUPPORTED_LANGS.includes(stored)) return stored;
    } catch (err) {
      console.error('[popola.i18n] localStorage read failed (private mode?):', err);
    }
    return DEFAULT_LANG;
  }

  function setCurrentLang(lang) {
    if (!SUPPORTED_LANGS.includes(lang)) {
      console.error('[popola.i18n] unsupported lang:', lang);
      return;
    }
    try {
      localStorage.setItem(STORAGE_KEY, lang);
    } catch (err) {
      console.error('[popola.i18n] localStorage write failed (quota / privacy?):', err);
    }
  }

  async function loadDict(lang) {
    if (_dicts[lang]) return _dicts[lang];
    try {
      const res = await fetch(I18N_BASE + lang + '.json', { cache: 'force-cache' });
      if (!res.ok) throw new Error('HTTP ' + res.status + ' ' + res.statusText);
      const dict = await res.json();
      _dicts[lang] = dict;
      return dict;
    } catch (err) {
      console.error('[popola.i18n] failed to load dictionary', lang, err);
      return null;
    }
  }

  function lookup(key, dict) {
    if (!dict) return undefined;
    const parts = key.split('.');
    let cur = dict;
    for (const p of parts) {
      if (cur && typeof cur === 'object' && p in cur) {
        cur = cur[p];
      } else {
        return undefined;
      }
    }
    return typeof cur === 'string' ? cur : undefined;
  }

  function resolve(key, dict, fallback) {
    const v = lookup(key, dict);
    if (v !== undefined) return v;
    const f = lookup(key, fallback);
    if (f !== undefined) {
      console.error('[popola.i18n] missing key in current dict, used EN fallback:', key);
      return f;
    }
    console.error('[popola.i18n] missing key in every dict:', key);
    return key;
  }

  function applyTranslations(dict, fallback) {
    document.querySelectorAll('[data-i18n]').forEach(el => {
      const key = el.getAttribute('data-i18n');
      el.textContent = resolve(key, dict, fallback);
    });

    const titleEl = document.querySelector('title');
    if (titleEl) {
      const t = lookup('page.title', dict) ?? lookup('page.title', fallback);
      if (t) titleEl.textContent = t;
    }

    document.querySelectorAll('[data-lang-toggle]').forEach(btn => {
      btn.textContent = resolve('lang.toggle_target', dict, fallback);
      const aria = lookup('lang.toggle_label', dict) ?? lookup('lang.toggle_label', fallback);
      if (aria) btn.setAttribute('aria-label', aria);
    });

    document.querySelectorAll('[data-theme-toggle]').forEach(btn => {
      const aria = lookup('theme.toggle_label', dict) ?? lookup('theme.toggle_label', fallback);
      if (aria) btn.setAttribute('aria-label', aria);
    });
  }

  async function setLangAndRender(lang) {
    setCurrentLang(lang);
    const dict = await loadDict(lang);
    if (!dict) return;
    if (!_enDict && lang !== 'en') _enDict = await loadDict('en');
    const fallback = lang === 'en' ? dict : (_enDict || {});
    document.documentElement.setAttribute('lang', HTML_LANG_MAP[lang] || lang);
    applyTranslations(dict, fallback);
  }

  function initToggle() {
    document.querySelectorAll('[data-lang-toggle]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const cur = getCurrentLang();
        const next = cur === 'en' ? 'zh' : 'en';
        await setLangAndRender(next);
      });
    });
  }

  document.addEventListener('DOMContentLoaded', async () => {
    const lang = getCurrentLang();
    await setLangAndRender(lang);
    initToggle();
  });
})();
