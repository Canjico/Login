import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const loginSource = await readFile(new URL('../index.html', import.meta.url), 'utf8');

test('login persiste handoff e redireciona sem depender de /cliente/me no caminho feliz', () => {
  assert.match(loginSource, /const handoffPersisted = persistAuthHandoffEnvelope\(json\);/);
  assert.match(
    loginSource,
    /const canRedirectImmediately = Boolean\(\s*authToken\s*\|\|\s*hasBootstrapUser\s*\|\|\s*hasBootstrapHandoff\s*\|\|\s*hasBackendRedirect\s*\);/s
  );
  assert.equal(/await\s+probeSessionAfterLogin\(/.test(loginSource), false);
  assert.equal(/void\s+probeSessionAfterLogin\(/.test(loginSource), false);
});

test('GET /cliente/me?minimal=1 usa cookie-first com fallback apenas em 401/403', () => {
  assert.match(loginSource, /async function fetchAuthenticatedGetCookieFirst\(/);
  assert.match(loginSource, /const cookieFirstResponse = await fetch\(/);
  assert.match(loginSource, /firstStatus === 401 \|\| firstStatus === 403/);
  const cookieFirstMeCalls = loginSource.match(/fetchAuthenticatedGetCookieFirst\('\/cliente\/me\?minimal=1'/g) || [];
  assert.equal(cookieFirstMeCalls.length >= 1, true);
});

test('fluxo feliz não mantém probe pós-redirect', () => {
  assert.equal(loginSource.includes('login_post_redirect_probe_error'), false);
  assert.equal(loginSource.includes('login_blocked_by_session_probe'), false);
});

test('password_change_required continua direcionando para troca obrigatória de senha', () => {
  assert.match(loginSource, /if \(shouldForcePasswordFlow\) \{/);
  assert.match(loginSource, /goToPasswordChangeFlow\(json, \{ email: data\.email, authToken \}\);/);
});
