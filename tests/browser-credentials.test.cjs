const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const modulePath = path.join(__dirname, '../dashboard/src/lib/browser-credentials.js');

async function functions() {
  return import(modulePath);
}

function store(entries) {
  const values = new Map(entries);
  return {
    values,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  };
}

test('forget removes current and legacy provider credentials but preserves unrelated work', async () => {
  const { BROWSER_CREDENTIAL_KEYS, forgetBrowserCredentials } = await functions();
  const storage = store(BROWSER_CREDENTIAL_KEYS.map(key => [key, 'synthetic-key']));
  storage.setItem('os_session', 'synthetic-project');
  storage.setItem('os_auth_token', 'synthetic-account-session');
  assert.deepEqual(forgetBrowserCredentials(storage), { cleared: true, failed: [] });
  assert.deepEqual([...storage.values.keys()], ['os_session', 'os_auth_token']);
});

test('one blocked storage entry does not stop deleting other keys', async () => {
  const { forgetBrowserCredentials } = await functions();
  const removed = [];
  const result = forgetBrowserCredentials({ removeItem: key => {
    if (key === 'gemini_key') throw Error('Storage blocked');
    removed.push(key);
  } });
  assert.equal(result.cleared, false);
  assert.deepEqual(result.failed, ['gemini_key']);
  assert(removed.includes('falKey_v1'));
  assert(removed.includes('uploadUserId'));
});

test('empty settings remove persisted credentials and cannot resurrect after reload', async () => {
  const { persistBrowserCredential } = await functions();
  const storage = store([['gemini_key', 'synthetic-key']]);
  persistBrowserCredential(storage, 'gemini_key', '');
  assert.equal(storage.values.has('gemini_key'), false);
  persistBrowserCredential(storage, 'gemini_key', 'new-synthetic-key');
  assert.equal(storage.values.get('gemini_key'), 'new-synthetic-key');
});

test('actual Settings handler clears every provider state and selected profile', async () => {
  const { forgetBrowserCredentials } = await functions();
  const source = fs.readFileSync(path.join(__dirname, '../dashboard/src/App.jsx'), 'utf8');
  const start = source.indexOf('  const handleForgetCredentials = () => {');
  const end = source.indexOf('\n  useEffect(', start);
  assert(start > 0 && end > start);
  const values = {};
  const context = { localStorage: store([]), forgetBrowserCredentials, credentialGeneration: { current: 0 } };
  for (const key of ['ApiKey', 'UploadPostKey', 'ElevenLabsKey', 'FalKey', 'UploadUserId', 'UserProfiles', 'ElevenLabsSaved', 'FalSaved', 'CredentialNotice']) {
    context['set' + key] = value => { values[key] = value; };
  }
  vm.createContext(context);
  vm.runInContext(source.slice(start, end) + '\nglobalThis.forget = handleForgetCredentials;', context);
  context.forget();
  for (const key of ['ApiKey', 'UploadPostKey', 'ElevenLabsKey', 'FalKey', 'UploadUserId']) assert.equal(values[key], '');
  assert.equal(values.UserProfiles.length, 0);
  assert.equal(values.ElevenLabsSaved, false);
  assert.match(values.CredentialNotice, /removed from this browser/);
});


test('late profile response cannot restore a profile after forgetting', async () => {
  const source = fs.readFileSync(path.join(__dirname, '../dashboard/src/App.jsx'), 'utf8');
  const start = source.indexOf('  const fetchUserProfiles = async');
  const end = source.indexOf('\n  };', start) + 5;
  let resolve;
  const pending = new Promise(done => { resolve = done; });
  const restored = [];
  const context = { uploadPostKey: 'synthetic-key', isManaged: false, uploadUserId: '',
    credentialGeneration: { current: 0 }, apiFetch: async () => pending,
    setUserProfiles: value => restored.push(value), setUploadUserId: value => restored.push(value), console,
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end) + '\nglobalThis.fetchProfiles = fetchUserProfiles;', context);
  const request = context.fetchProfiles();
  context.credentialGeneration.current += 1;
  resolve({ ok: true, json: async () => ({ profiles: [{ username: 'synthetic-profile' }] }) });
  await request;
  assert.deepEqual(restored, []);
});
