import test from 'node:test';
import assert from 'node:assert/strict';
import { loadConfigFromFile } from 'vite';

test('API and health proxies share the default or explicitly configured backend', async () => {
  const previous = process.env.P2R_API_TARGET;
  try {
    delete process.env.P2R_API_TARGET;
    const defaults = await loadConfigFromFile({ command: 'serve', mode: 'development' });
    for (const path of ['/api/v1', '/healthz']) {
      assert.equal(defaults.config.server.proxy[path].target, 'http://127.0.0.1:8000');
    }
    process.env.P2R_API_TARGET = 'http://127.0.0.1:8010';
    const override = await loadConfigFromFile({ command: 'serve', mode: 'development' });
    for (const path of ['/api/v1', '/healthz']) {
      assert.equal(override.config.server.proxy[path].target, 'http://127.0.0.1:8010');
      assert.equal(override.config.server.proxy[path].changeOrigin, false);
    }
    assert.equal(override.config.server.host, '127.0.0.1');
  } finally {
    if (previous === undefined) delete process.env.P2R_API_TARGET;
    else process.env.P2R_API_TARGET = previous;
  }
});
