import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { initialThemePref, parseThemePref, resolveTheme, THEME_STORAGE_KEY } from '../src/theme.ts';

test('resolveTheme follows the system only for the system preference', () => {
  assert.equal(resolveTheme('system', true), 'dark');
  assert.equal(resolveTheme('system', false), 'light');
  assert.equal(resolveTheme('light', true), 'light');
  assert.equal(resolveTheme('dark', false), 'dark');
});

test('parseThemePref rejects anything but the three known values', () => {
  for (const value of ['system', 'light', 'dark']) assert.equal(parseThemePref(value), value);
  for (const value of ['', 'blue', null, undefined, 1]) assert.equal(parseThemePref(value), null);
});

test('initial preference: URL override, then stored value, then system', () => {
  assert.equal(initialThemePref('', null), 'system');
  assert.equal(initialThemePref('', 'garbage'), 'system');
  assert.equal(initialThemePref('', 'dark'), 'dark');
  assert.equal(initialThemePref('?theme=light', 'dark'), 'light');
  assert.equal(initialThemePref('?theme=nope', 'dark'), 'dark');
});

test('the pre-paint boot script and the theme module agree on the storage key', async () => {
  const boot = await readFile(new URL('../public/theme-boot.js', import.meta.url), 'utf8');
  assert.equal(THEME_STORAGE_KEY, 'p2r.theme');
  assert.match(boot, /getItem\('p2r\.theme'\)/);
  const html = await readFile(new URL('../index.html', import.meta.url), 'utf8');
  assert.ok(html.indexOf('/theme-boot.js') < html.indexOf('/src/main.tsx'), 'theme applies before the app loads');
});

test('storage stays confined to the theme module: api keys and shell code never persist', async () => {
  for (const file of ['../src/shell/hooks.ts', '../src/shell/Sidebar.tsx', '../src/shell/AppShell.tsx', '../src/features/settings/Settings.tsx', '../src/features/overview/OverviewPage.tsx', '../src/features/autonomous/TaskPage.tsx', '../src/features/flow/DraftPage.tsx', '../src/features/flow/steps/PaperStep.tsx', '../src/features/flow/steps/ObjectiveStep.tsx', '../src/features/flow/steps/ModelStep.tsx', '../src/features/flow/steps/LaunchStep.tsx','../src/api-client/papers.ts', '../src/api-client/autonomous.ts', '../src/desktop.ts']) {
    const source = await readFile(new URL(file, import.meta.url), 'utf8');
    assert.doesNotMatch(source, /localStorage\.|sessionStorage\./, file);
  }
});
