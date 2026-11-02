// Browser storage is convenience storage, not a secret vault. Include legacy
// names so forgetting credentials also removes values from previous versions.
export const BROWSER_CREDENTIAL_KEYS = Object.freeze([
  'gemini_key', 'uploadPostKey_v3', 'uploadPostKey_v2', 'uploadPostKey_v1',
  'uploadPostKey', 'elevenLabsKey_v1', 'elevenLabsKey', 'falKey_v1', 'falKey',
  'uploadUserId',
]);

export function forgetBrowserCredentials(storage) {
  const failed = [];
  for (const name of BROWSER_CREDENTIAL_KEYS) {
    try {
      storage.removeItem(name);
    } catch (_) {
      failed.push(name);
    }
  }
  return { cleared: failed.length === 0, failed };
}

export function persistBrowserCredential(storage, name, value) {
  if (value) storage.setItem(name, value);
  else storage.removeItem(name);
}
