// Execute the actual handler with fake API responses; no browser or account.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../dashboard/src/components/ResultCard.jsx'), 'utf8');
const start = source.indexOf('    const handlePost = async () => {');
const end = source.indexOf('    // Browser-rendered previews', start);
assert(start >= 0 && end > start);
const handler = source.slice(start, end).replace('const handlePost =', 'globalThis.handlePost =');
const guardStart = source.indexOf('    const hasUnpublishedEdits');
assert(guardStart >= 0 && guardStart < start);
const guardedHandler = source.slice(guardStart, end) + ';globalThis.handlePost = handlePost;';
for (const scheduling of [false, true]) {
  test(`accepted upload with pending tracking remains visible (scheduling=${scheduling})`, async () => {
    const calls = [], results = [], posting = [];
    const context = {
      hasUnpublishedEdits: false, editInProgress: false, canPost: true, noAccountsConnected: false, platforms: { youtube: true },
      isScheduling: scheduling, scheduleDate: '2099-01-01T12:00:00Z',
      jobId: 'synthetic-job', index: 0, uploadPostKey: 'synthetic-key', uploadUserId: 'synthetic-profile',
      postTitle: 'Synthetic', postDescription: 'Offline fixture',
      setPosting: v => posting.push(v), setPostResult: v => results.push(v),
      apiFetch: async (url, options) => { calls.push({url, options}); return { ok: true, json: async () => ({request_id: 'accepted-id', tracking_persistence: 'pending', tracking_warning: 'Upload already accepted; do not submit again.'}) }; },
      followPost: () => { throw Error('Do not hide tracking warning by polling'); },
      setShowModal: () => { throw Error('Do not auto-close tracking warning'); },
      setTimeout: () => { throw Error('Do not schedule closing the warning'); },
      track: () => {},
    };
    vm.createContext(context);
    vm.runInContext(handler, context);
    await context.handlePost();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, '/api/social/post');
    assert.equal(calls[0].options.method, 'POST');
    assert.match(results.at(-1).msg, /accepted-id/);
    assert.match(results.at(-1).msg, /do not submit again/);
    assert.equal(results.at(-1).success, true);
    assert.equal(posting.at(-1), false);
  });
}

const blockedCases = [
  ['browser render', { currentVideoUrl: 'blob:edited-version' }],
  ['restored hook', { activeLayers: { hook: { text: 'Synthetic hook' } } }],
  ['captions', { activeLayers: { subtitles: { words: [] } } }],
  ['effects', { activeLayers: { effects: { blur: true } } }],
  ['auto edit running', { isEditing: true }],
  ['captions running', { isSubtitling: true }],
  ['hook running', { isHooking: true }],
  ['translation running', { isTranslating: true }],
];

async function publishVersion(overrides) {
  const calls = [], results = [];
  const context = {
    activeLayers: {}, currentVideoUrl: '/videos/synthetic/server.mp4',
    isEditing: false, isSubtitling: false, isHooking: false, isTranslating: false,
    canPost: true, noAccountsConnected: false, platforms: { youtube: true },
    isScheduling: true, scheduleDate: '2099-01-01T12:00:00Z',
    jobId: 'synthetic-job', index: 0, uploadPostKey: 'synthetic-key',
    uploadUserId: 'synthetic-profile', postTitle: 'Synthetic', postDescription: '',
    setPosting: () => {}, setPostResult: value => results.push(value),
    setTimeout: () => {},
    apiFetch: async (url, options) => {
      calls.push({ url, options });
      return { ok: true, json: async () => ({ request_id: 'synthetic-accepted' }) };
    },
    ...overrides,
  };
  vm.createContext(context);
  vm.runInContext(guardedHandler, context);
  await context.handlePost();
  return { calls, results };
}

for (const [label, overrides] of blockedCases) {
  test(`unpublished version never reaches provider: ${label}`, async () => {
    const { calls, results } = await publishVersion(overrides);
    assert.equal(calls.length, 0);
    assert.equal(results.at(-1).success, false);
    assert.match(results.at(-1).msg, /Download this edited version|Wait for the current edit/);
  });
}

test('server-backed version reaches the simulated publisher once', async () => {
  const { calls, results } = await publishVersion({});
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/social/post');
  assert.equal(results.at(-1).success, true);
});
