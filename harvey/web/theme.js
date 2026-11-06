// Appearance, applied before the page paints so a dark-mode viewer never sees
// a white flash: theme (auto follows the system), accent colour and density.
// Chosen in Settings → Appearance; stored in this browser only.
(function () {
  var root = document.documentElement;
  function read(key, allowed, fallback) {
    try {
      var v = localStorage.getItem(key);
      return allowed.indexOf(v) >= 0 ? v : fallback;
    } catch (e) {
      return fallback;
    }
  }
  var theme = read('harvey-theme', ['auto', 'light', 'dark'], 'auto');
  if (theme !== 'auto') root.setAttribute('data-theme', theme);
  var accent = read('harvey-accent', ['cobalt', 'indigo', 'fuchsia', 'graphite'], 'cobalt');
  if (accent !== 'cobalt') root.setAttribute('data-accent', accent);
  if (read('harvey-sidebar', ['open', 'collapsed'], 'open') === 'collapsed') {
    root.setAttribute('data-sidebar', 'collapsed');
  }
  if (read('harvey-density', ['comfortable', 'compact'], 'comfortable') === 'compact') {
    root.setAttribute('data-density', 'compact');
  }
})();
