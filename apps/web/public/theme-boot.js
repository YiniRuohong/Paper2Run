// Runs synchronously before first paint. Mirrors src/theme.ts: the preference
// lives in localStorage "p2r.theme" (system | light | dark); "?theme=" in the
// URL overrides it for this page load only. Also marks the desktop shell.
(function () {
  var root = document.documentElement;
  var pref = null;
  try {
    var q = new URLSearchParams(window.location.search).get('theme');
    pref = q === 'system' || q === 'light' || q === 'dark' ? q : window.localStorage.getItem('p2r.theme');
  } catch (e) { /* storage unavailable: fall back to the system theme */ }
  var dark = false;
  try { dark = window.matchMedia('(prefers-color-scheme: dark)').matches; } catch (e) { /* ignore */ }
  root.setAttribute('data-theme', pref === 'light' || pref === 'dark' ? pref : (dark ? 'dark' : 'light'));
  var desktop = window.p2rDesktop;
  if (desktop && typeof desktop.platform === 'string') root.setAttribute('data-shell', desktop.platform);
})();
