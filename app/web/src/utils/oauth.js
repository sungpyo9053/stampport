// OAuth helpers for Stampport — Kakao + Naver authorize URL builders
// and a callback querystring parser. No token exchange happens here:
// the front-end never sees a client secret. When a real client_id is
// configured we redirect to the provider's authorize endpoint; when
// it isn't, we hand back a mock-mode marker so the Login screen can
// fall back to a synthetic profile (still saves to localStorage so
// the rest of the app works).
//
// Public env vars (Vite-exposed, fine to include in the bundle):
//   VITE_KAKAO_CLIENT_ID       — REST API key (no secret)
//   VITE_NAVER_CLIENT_ID       — App client id (no secret)
//   VITE_OAUTH_REDIRECT_BASE   — e.g. https://reviewdr.kr/stampport
//
// Server-side secrets (NEVER referenced here):
//   KAKAO_CLIENT_SECRET / NAVER_CLIENT_SECRET → backend only.

const KAKAO_AUTHORIZE = 'https://kauth.kakao.com/oauth/authorize';
const NAVER_AUTHORIZE = 'https://nid.naver.com/oauth2.0/authorize';

const PROVIDERS = ['kakao', 'naver'];

function readEnv(key) {
  // import.meta.env is the Vite-injected global. Fall back to an
  // empty string so calling code can rely on a string type.
  if (typeof import.meta === 'undefined') return '';
  return (import.meta.env?.[key] || '').toString().trim();
}

export function oauthConfigFor(provider) {
  if (!PROVIDERS.includes(provider)) {
    return { provider, clientId: '', redirectUri: '', isMock: true };
  }
  const clientId = provider === 'kakao'
    ? readEnv('VITE_KAKAO_CLIENT_ID')
    : readEnv('VITE_NAVER_CLIENT_ID');
  const base = readEnv('VITE_OAUTH_REDIRECT_BASE');
  // Default redirect points at this app's hash route. Production sets
  // VITE_OAUTH_REDIRECT_BASE to the full https origin; dev leaves it
  // empty and we fall back to window.location.origin at call time.
  let redirectUri = '';
  if (base) {
    redirectUri = `${base.replace(/\/$/, '')}/auth/callback/${provider}`;
  } else if (typeof window !== 'undefined') {
    redirectUri = `${window.location.origin}${window.location.pathname}#/auth/callback/${provider}`;
  }
  return {
    provider,
    clientId,
    redirectUri,
    isMock: !clientId,
  };
}

// Cryptographically OK enough for a CSRF-state token in this MVP.
// We persist the value in sessionStorage so the callback can verify
// the round-trip wasn't tampered with.
function makeState() {
  const buf = new Uint8Array(16);
  if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
    crypto.getRandomValues(buf);
  } else {
    for (let i = 0; i < buf.length; i += 1) buf[i] = Math.floor(Math.random() * 256);
  }
  return Array.from(buf).map((b) => b.toString(16).padStart(2, '0')).join('');
}

const STATE_KEY = 'stampport:oauth_state';

export function rememberOAuthState(provider) {
  const value = `${provider}.${makeState()}`;
  if (typeof window !== 'undefined' && window.sessionStorage) {
    try {
      window.sessionStorage.setItem(STATE_KEY, value);
    } catch {
      // Ignore — sessionStorage is best-effort here.
    }
  }
  return value;
}

export function consumeOAuthState() {
  if (typeof window === 'undefined' || !window.sessionStorage) return '';
  try {
    const v = window.sessionStorage.getItem(STATE_KEY) || '';
    window.sessionStorage.removeItem(STATE_KEY);
    return v;
  } catch {
    return '';
  }
}

// Build the provider's authorize URL. Returns { url, isMock,
// providerLabel } so the caller can decide whether to redirect or
// short-circuit to mock mode.
export function buildAuthorizeUrl(provider) {
  const cfg = oauthConfigFor(provider);
  if (cfg.isMock) {
    return {
      url: '',
      isMock: true,
      provider,
      reason: 'no_client_id',
    };
  }
  const state = rememberOAuthState(provider);
  const params = new URLSearchParams({
    client_id: cfg.clientId,
    redirect_uri: cfg.redirectUri,
    response_type: 'code',
    state,
  });
  const base = provider === 'kakao' ? KAKAO_AUTHORIZE : NAVER_AUTHORIZE;
  return {
    url: `${base}?${params.toString()}`,
    isMock: false,
    provider,
  };
}

// Parse callback querystring. Vite's hash router means we sometimes
// see params on `?`, sometimes after `#/auth/callback/kakao?…`. Try
// both. Returns { provider, code, state, error } with empty strings
// for missing keys.
export function parseCallbackParams(provider, locationLike = (typeof window !== 'undefined' ? window.location : null)) {
  if (!locationLike) {
    return { provider, code: '', state: '', error: '' };
  }
  const search = (locationLike.search || '').replace(/^\?/, '');
  const hash = locationLike.hash || '';
  const hashSearchIndex = hash.indexOf('?');
  const hashSearch = hashSearchIndex >= 0 ? hash.slice(hashSearchIndex + 1) : '';
  const params = new URLSearchParams(search || hashSearch);
  return {
    provider,
    code: params.get('code') || '',
    state: params.get('state') || '',
    error: params.get('error') || '',
    error_description: params.get('error_description') || '',
  };
}

// Mock fallback metadata — copy the Login screen surfaces so a
// developer-mode visitor still gets a believable provider feel.
export const MOCK_PROFILE = {
  kakao: { nickname: '카카오 여행자', avatar_style: 'kakao_explorer' },
  naver: { nickname: '네이버 탐험가', avatar_style: 'naver_explorer' },
};

export function isOAuthConfigured(provider) {
  return !oauthConfigFor(provider).isMock;
}

export const OAUTH_PROVIDERS = PROVIDERS;
