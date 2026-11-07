import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildShelf, loadAnnotations, loadColorKey, loadPosition, loadShelfPrefs,
  saveAnnotations, saveColorKey, savePosition, saveShelfPrefs,
} from '../src/features/reader/storage.ts';

function memoryStore() {
  const data = new Map();
  return { data, getItem: k => (data.has(k) ? data.get(k) : null), setItem: (k, v) => { data.set(k, String(v)); } };
}

const paper = (id, extra = {}) => ({
  paper_id: id, project_id: 'p1', title: `Title ${id}`, status: 'ready', sha256: '', page_count: null,
  content_available: false, current_extraction_id: null, active_operation_id: null, error: null,
  created_at: '2026-11-05T10:00:00+00:00', source_kind: 'pdf', media_type: 'application/pdf', ...extra,
});
const projects = [{ id: 'p1', name: 'Project One' }];

test('shelf lists only PDF papers and applies alias, pin and hide', () => {
  const rows = [
    paper('a'),
    paper('b', { source_kind: 'text', media_type: 'text/plain' }),
    paper('c'),
    paper('d', { source_kind: undefined, media_type: 'application/pdf' }),
  ];
  const shelf = buildShelf(rows, projects, { alias: { a: 'My name' }, pinned: ['c'], hidden: ['d'] });
  assert.deepEqual(shelf.map(d => d.id), ['a', 'c']);
  assert.equal(shelf[0].name, 'My name');
  assert.equal(shelf[0].projectName, 'Project One');
  assert.equal(shelf[1].pinned, true);
  assert.equal(shelf[0].createdAt, Date.parse('2026-11-05T10:00:00+00:00'));
});

test('shelf falls back to the paper id when title is empty and to 0 for a bad date', () => {
  const [doc] = buildShelf([paper('x', { title: '  ', created_at: '' })], [], { alias: {}, pinned: [], hidden: [] });
  assert.equal(doc.name, 'x');
  assert.equal(doc.projectName, 'p1');
  assert.equal(doc.createdAt, 0);
});

test('shelf prefs round-trip and ignore malformed stored values', () => {
  const store = memoryStore();
  assert.deepEqual(loadShelfPrefs(store), { alias: {}, pinned: [], hidden: [] });
  assert.equal(saveShelfPrefs({ alias: { a: 'A' }, pinned: ['a'], hidden: ['b'] }, store), true);
  assert.deepEqual(loadShelfPrefs(store), { alias: { a: 'A' }, pinned: ['a'], hidden: ['b'] });

  store.setItem('p2r.reader.shelf.v1', JSON.stringify({ alias: { a: 3, b: ' ', c: 'C' }, pinned: 'nope', hidden: [1, 'h'] }));
  assert.deepEqual(loadShelfPrefs(store), { alias: { c: 'C' }, pinned: [], hidden: ['h'] });
  store.setItem('p2r.reader.shelf.v1', '{not json');
  assert.deepEqual(loadShelfPrefs(store), { alias: {}, pinned: [], hidden: [] });
});

test('annotations are stored per paper and drop non-object entries', () => {
  const store = memoryStore();
  saveAnnotations('a', { highlights: [{ id: 'h1' }], strokes: [{ id: 's1' }] }, store);
  saveAnnotations('b', { highlights: [], strokes: [{ id: 's2' }] }, store);
  assert.deepEqual(loadAnnotations('a', store), { highlights: [{ id: 'h1' }], strokes: [{ id: 's1' }] });
  assert.deepEqual(loadAnnotations('b', store).strokes, [{ id: 's2' }]);
  assert.deepEqual(loadAnnotations('missing', store), { highlights: [], strokes: [] });

  store.setItem('p2r.reader.notes.v1.a', JSON.stringify({ highlights: [null, 'x', { id: 'ok' }], strokes: 5 }));
  assert.deepEqual(loadAnnotations('a', store), { highlights: [{ id: 'ok' }], strokes: [] });
});

test('reading position is kept per paper and only positive numbers are restored', () => {
  const store = memoryStore();
  savePosition('a', 120.6, store);
  savePosition('b', -5, store);
  assert.equal(loadPosition('a', store), 121);
  assert.equal(loadPosition('b', store), 0);
  assert.equal(loadPosition('c', store), 0);
});

test('color key falls back when the stored value is not allowed', () => {
  const store = memoryStore();
  assert.equal(loadColorKey(['indigo', 'amber'], 'indigo', store), 'indigo');
  saveColorKey('amber', store);
  assert.equal(loadColorKey(['indigo', 'amber'], 'indigo', store), 'amber');
  saveColorKey('purple', store);
  assert.equal(loadColorKey(['indigo', 'amber'], 'indigo', store), 'indigo');
});

test('writes report failure instead of throwing when storage is full', () => {
  const store = { getItem: () => null, setItem: () => { throw new Error('QuotaExceededError'); } };
  assert.equal(saveAnnotations('a', { highlights: [], strokes: [] }, store), false);
  assert.equal(saveShelfPrefs({ alias: {}, pinned: [], hidden: [] }, store), false);
  assert.equal(savePosition('a', 10, store), false);
});
