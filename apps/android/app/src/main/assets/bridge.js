// Injected into the Paper2Run page on every load. The web UI was written for
// desktop browsers; three things it does need help inside an Android WebView:
//  - downloads made from blob:/data: URLs (task export zip, workbench files),
//    which DownloadManager cannot fetch: the bytes are handed to the app here;
//  - window.print() (report "export PDF"), which a WebView ignores: the app
//    opens the system print dialog ("Save as PDF"; the WebView fires before/afterprint);
//  - URL.revokeObjectURL right after a.click(), which would revoke the blob
//    before it is read: revocation is deferred for a minute.
(() => {
  if (window.__p2rAndroid || !window.P2RAndroid) return;
  window.__p2rAndroid = true;
  const app = window.P2RAndroid;
  const CHUNK = 768 * 1024; // bytes per bridge call (1 MiB of base64)

  const revoke = URL.revokeObjectURL.bind(URL);
  URL.revokeObjectURL = (url) => { setTimeout(() => revoke(url), 60000); };

  const base64 = (bytes) => {
    let s = '';
    for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(s);
  };

  async function save(href, name) {
    let id = null;
    try {
      const blob = await (await fetch(href)).blob();
      id = app.begin(name || 'download', blob.type || 'application/octet-stream');
      for (let at = 0; at < blob.size; at += CHUNK) {
        app.chunk(id, base64(new Uint8Array(await blob.slice(at, at + CHUNK).arrayBuffer())));
      }
      app.end(id);
    } catch (error) {
      app.fail(id || '', String(error && error.message || error));
    }
  }

  const sameOrigin = (a) => { try { return new URL(a.href).origin === location.origin; } catch { return false; } };
  const intercept = (a) => {
    if (!a.hasAttribute('download')) return false;
    if (!/^(blob|data):/.test(a.href) && !sameOrigin(a)) return false;
    save(a.href, a.getAttribute('download') || a.href.split('/').pop().split('?')[0]);
    return true;
  };

  const click = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function () { if (!intercept(this)) click.call(this); };
  document.addEventListener('click', (event) => {
    const a = event.target && event.target.closest && event.target.closest('a[download]');
    if (a && intercept(a)) event.preventDefault();
  }, true);

  window.print = () => app.print(document.title || 'Paper2Run');
})();
