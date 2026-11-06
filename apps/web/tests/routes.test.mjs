import test from 'node:test';
import assert from 'node:assert/strict';
import { parseRoute, draftHref } from '../src/shell/routes.ts';

test('plain routes parse without redirect', () => {
  assert.deepEqual(parseRoute(''), { route: { view: 'overview' }, redirect: null });
  assert.deepEqual(parseRoute('#/'), { route: { view: 'overview' }, redirect: null });
  assert.deepEqual(parseRoute('#/settings'), { route: { view: 'settings' }, redirect: null });
  assert.deepEqual(parseRoute('#/new'), { route: { view: 'new' }, redirect: null });
});

test('legacy #/autonomous links redirect', () => {
  assert.equal(parseRoute('#/autonomous?task=abc').redirect, '#/task/abc');
  assert.equal(parseRoute('#/autonomous').redirect, '#/new');
  assert.equal(parseRoute('#/autonomous?task=a%2Fb').redirect, '#/task/a%2Fb');
});

test('#/new?project= redirects to the draft route', () => {
  assert.equal(parseRoute('#/new?project=p1').redirect, '#/draft/p1');
  assert.equal(parseRoute('#/new?project=p%2F1').redirect, '#/draft/p%2F1');
});

test('draft and task ids are decoded', () => {
  assert.deepEqual(parseRoute('#/draft/p1').route, { view: 'draft', projectId: 'p1' });
  assert.deepEqual(parseRoute('#/draft/a%2Fb').route, { view: 'draft', projectId: 'a/b' });
  assert.deepEqual(parseRoute('#/task/t%2F1?x=1').route, { view: 'task', taskId: 't/1' });
  assert.equal(parseRoute('#/task/t1').redirect, null);
});

test('draftHref encodes the project id', () => {
  assert.equal(draftHref('a/b'), '#/draft/a%2Fb');
  assert.equal(draftHref('p1'), '#/draft/p1');
});
