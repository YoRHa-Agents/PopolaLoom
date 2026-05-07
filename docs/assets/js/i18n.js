/* PopolaLoom v0.8.3 — client-side i18n switcher (vanilla, no deps).
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

  // EN-only notice (v0.8.2+) — surfaced when a user switches to zh on a page
  // whose entire i18n surface is just the chrome (header + footer). Doc
  // pages (QUICKSTART / USER_GUIDE / DEMO) carry header(4 nav hooks) +
  // footer(1 tagline) = 5 [data-i18n] elements; the landing page carries
  // 30+. Threshold is the max chrome-only count we treat as "no per-page
  // Chinese yet"; comparison is `<=` so 5-hook docs pages trigger.
  const EN_ONLY_THRESHOLD = 5;
  const NOTICE_DISMISSED_KEY = 'popola.notice.dismissed.en_only';

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

  const PAGE_LANG = document.documentElement.dataset.pageLang;
  const TRANSLATION_URL = document.documentElement.dataset.translationUrl;

  const _dicts = Object.create(null);
  let _enDict = null;

  function getCurrentLang() {
    if (PAGE_LANG && SUPPORTED_LANGS.includes(PAGE_LANG)) return PAGE_LANG;
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
    if (Object.prototype.hasOwnProperty.call(dict, key)) {
      return typeof dict[key] === 'string' ? dict[key] : undefined;
    }
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
    if (titleEl && !PAGE_LANG) {
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

  function isNoticeDismissed() {
    try {
      return sessionStorage.getItem(NOTICE_DISMISSED_KEY) === 'true';
    } catch (err) {
      console.error('[popola.i18n] sessionStorage read failed (private mode?):', err);
      return false;
    }
  }

  function dismissNotice() {
    try {
      sessionStorage.setItem(NOTICE_DISMISSED_KEY, 'true');
    } catch (err) {
      console.error('[popola.i18n] sessionStorage write failed (quota / privacy?):', err);
    }
  }

  function spawnEnOnlyNotice(text) {
    if (document.querySelector('[data-lang-notice]')) return;

    const notice = document.createElement('div');
    notice.className = 'lang-notice';
    notice.setAttribute('role', 'status');
    notice.setAttribute('aria-live', 'polite');
    notice.setAttribute('aria-atomic', 'true');
    notice.setAttribute('data-lang-notice', '');

    // <p data-i18n="notice.en_only"> so future applyTranslations() calls
    // re-translate the body if the user toggles language again.
    const p = document.createElement('p');
    p.setAttribute('data-i18n', 'notice.en_only');
    p.textContent = text;
    notice.appendChild(p);

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'lang-notice-close';
    close.setAttribute('aria-label', 'Dismiss notice');
    close.textContent = '\u2715';  // ✕
    close.addEventListener('click', () => {
      dismissNotice();
      notice.remove();
    });
    notice.appendChild(close);

    document.body.appendChild(notice);
  }

  function maybeShowEnOnlyNotice(targetLang) {
    if (targetLang !== 'zh') return;
    if (isNoticeDismissed()) return;
    const hookCount = document.querySelectorAll('[data-i18n]').length;
    if (hookCount > EN_ONLY_THRESHOLD) return;
    // setLangAndRender('zh') ran first, so _dicts.zh is populated.
    const dict = _dicts.zh || {};
    const fb = _enDict || _dicts.en || {};
    const text = resolve('notice.en_only', dict, fb);
    spawnEnOnlyNotice(text);
  }

  function initToggle() {
    document.querySelectorAll('[data-lang-toggle]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const cur = getCurrentLang();
        const next = cur === 'en' ? 'zh' : 'en';
        if (TRANSLATION_URL) {
          setCurrentLang(next);
          window.location.href = TRANSLATION_URL;
          return;
        }
        await setLangAndRender(next);
        maybeShowEnOnlyNotice(next);
      });
    });
  }

  document.addEventListener('DOMContentLoaded', async () => {
    const lang = getCurrentLang();
    await setLangAndRender(lang);
    initToggle();
  });
})();
