/* PopolaLoom v1.0.0-pre.1 — UX extras (vanilla, no deps).
 *
 * Two progressive enhancements injected at DOMContentLoaded:
 *
 *   1. Code-block copy buttons — every <pre> gets a hover-revealed button
 *      in its top-right corner. Click → navigator.clipboard.writeText →
 *      ✓ flash for FEEDBACK_MS, then back to ⎘. Failure → ✗ + console.error.
 *
 *   2. Heading anchor links — h2[id] / h3[id] get a hover-revealed "¶"
 *      link to "#<id>". Skips hero / feature-card headings (visual chrome
 *      with no anchor semantics). h1 is intentionally ignored to keep
 *      hero titles uncluttered.
 *
 * Progressive enhancement: when JS is disabled, code blocks remain
 * select-and-copy-able and heading IDs remain href-targetable manually.
 */
(function () {
  'use strict';

  const COPY_GLYPH    = '\u2398';   // ⎘  (next page / copy)
  const COPY_OK_GLYPH = '\u2713';   // ✓
  const COPY_ERR_GLYPH = '\u2717';  // ✗
  const FEEDBACK_MS = 500;
  const ANCHOR_GLYPH = '\u00b6';    // ¶

  function readCodeText(pre, btn) {
    // Preferred path: kramdown-rouge wraps code in <pre><code>...</code></pre>,
    // so <code>.textContent gives us clean text without our injected button.
    const codeEl = pre.querySelector('code');
    if (codeEl) return codeEl.textContent;
    // Fallback for rare <pre> without <code>: temporarily swap the button
    // for a temporary marker, read pre.textContent, then put the button
    // back. Avoids reading the ⎘ glyph as part of the copied payload.
    const marker = document.createComment('copy-btn-marker');
    pre.replaceChild(marker, btn);
    const text = pre.textContent;
    pre.replaceChild(btn, marker);
    return text;
  }

  function flashFeedback(btn, glyph) {
    btn.textContent = glyph;
    setTimeout(() => { btn.textContent = COPY_GLYPH; }, FEEDBACK_MS);
  }

  function onCopyClick(pre, btn) {
    const text = readCodeText(pre, btn);
    if (!navigator.clipboard || typeof navigator.clipboard.writeText !== 'function') {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      try {
        const ok = document.execCommand('copy');
        flashFeedback(btn, ok ? COPY_OK_GLYPH : COPY_ERR_GLYPH);
      } catch (err) {
        console.error('[popola.extras] copy fallback failed:', err);
        flashFeedback(btn, COPY_ERR_GLYPH);
      } finally {
        ta.remove();
      }
      return;
    }
    navigator.clipboard.writeText(text).then(
      () => flashFeedback(btn, COPY_OK_GLYPH),
      err => {
        console.error('[popola.extras] clipboard.writeText failed:', err);
        flashFeedback(btn, COPY_ERR_GLYPH);
      }
    );
  }

  function initCopyButtons() {
    document.querySelectorAll('pre, .terminal-block').forEach(pre => {
      if (pre.querySelector('[data-copy-btn]')) return;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'copy-btn';
      btn.setAttribute('aria-label', 'Copy code');
      btn.setAttribute('data-copy-btn', '');
      btn.textContent = COPY_GLYPH;
      btn.addEventListener('click', () => onCopyClick(pre, btn));
      // Insert as first child so it's positioned absolutely outside the
      // text flow. CSS gives <pre> position:relative + the button
      // position:absolute top/right.
      pre.insertBefore(btn, pre.firstChild);
    });
  }

  function initAnchorLinks() {
    document.querySelectorAll('h2[id], h3[id]').forEach(h => {
      // Hero + feature-card headings are visual chrome; permalinking them
      // makes no semantic sense and the ::after gold underline collides
      // with the ¶ glyph on hover.
      if (h.closest('.hero, .feature-card')) return;
      if (h.querySelector('.anchor-link')) return;
      const a = document.createElement('a');
      a.className = 'anchor-link';
      a.href = '#' + h.id;
      a.setAttribute('aria-label', 'Permalink to this section');
      a.textContent = ANCHOR_GLYPH;
      h.appendChild(a);
    });
  }

  function init() {
    try {
      initCopyButtons();
    } catch (err) {
      console.error('[popola.extras] copy-button init failed:', err);
    }
    try {
      initAnchorLinks();
    } catch (err) {
      console.error('[popola.extras] anchor-link init failed:', err);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
