// Presentation-only integration: never forwards credentials, API calls, or task commands.
(() => {
  'use strict';
  const params = new URLSearchParams(window.location.search);
  // The frame name survives local login/logout redirects that drop query parameters.
  const namedFrame = window.name === 'ricardo-gpt-register' && window.parent !== window;
  if (params.get('embedded') !== '1' && !namedFrame) return;
  const root = document.documentElement;
  root.classList.add('embedded');
  root.dataset.theme = params.get('theme') === 'dark' ? 'dark' : 'light';
  document.title = 'GPT 注册机 · Ricardo AI 工具';

  function loopbackOrigin(value) {
    try {
      const url = new URL(value);
      if (!['http:', 'https:'].includes(url.protocol)
        || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
        || url.username || url.password || url.pathname !== '/' || url.search || url.hash) return null;
      return url.origin;
    } catch { return null; }
  }
  const specifiedOrigin = params.get('parentOrigin');
  let parentOrigin = specifiedOrigin ? loopbackOrigin(specifiedOrigin) : null;
  if (specifiedOrigin && !parentOrigin) return;
  if (window.parent === window) return;

  let ready = false;
  let pending = false;
  let lastHeight = 0;
  function reportLayout() {
    if (!ready || pending || !parentOrigin) return;
    pending = true;
    window.requestAnimationFrame(() => {
      pending = false;
      // Embedded CSS removes viewport min-height; body includes stats, tabs and memory card.
      const height = Math.ceil(document.body.getBoundingClientRect().height);
      if (!Number.isFinite(height) || height < 200 || height > 20000 || height === lastHeight) return;
      lastHeight = height;
      window.parent.postMessage({ type: 'turb-register-layout', height }, parentOrigin);
    });
  }

  window.addEventListener('message', (event) => {
    if (event.source !== window.parent || !loopbackOrigin(event.origin)) return;
    if (parentOrigin && event.origin !== parentOrigin) return;
    if (event.data?.type !== 'ricardo-ui-theme' || !['dark', 'light'].includes(event.data.theme)) return;
    // Re-establish only the presentation origin after a same-service login redirect.
    parentOrigin = event.origin;
    root.dataset.theme = event.data.theme;
    lastHeight = 0;
    reportLayout();
  });

  document.addEventListener('DOMContentLoaded', () => {
    ready = true;
    if (typeof ResizeObserver !== 'undefined') new ResizeObserver(reportLayout).observe(document.body);
    window.addEventListener('resize', reportLayout);
    window.addEventListener('load', reportLayout, { once: true });
    reportLayout();
  }, { once: true });
})();
