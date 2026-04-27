// Stampport localStorage adapter.
//
// User profile shape (Stampport Passport Identity):
//   {
//     id:               "local_<provider>_<random>",
//     provider:         "guest" | "kakao" | "naver",
//     provider_user_id: string | null,
//     nickname:         string,
//     avatar_style:     string,
//     passport_title:   string,
//     level:            number,
//     exp:              number,
//     created_at:       ISO string,
//     last_login_at:    ISO string,
//     // Legacy compatibility — older snapshots stored {nickname, email}
//     // and we keep email so nothing else in the app breaks.
//     email?:           string,
//     // Stable user_id used as the prefix for stamps/profile-meta keys.
//     // For migrated guests we keep the legacy user_id so an existing
//     // stamps:<old> bucket carries over without a copy.
//     user_id:          string,
//   }

const USER_KEY = 'stampport:user';

export function readJson(key, fallback) {
  if (typeof window === 'undefined') return fallback;
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw);
  } catch {
    return fallback;
  }
}

export function writeJson(key, value) {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // ignore quota errors in MVP
  }
}

export function removeKey(key) {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.removeItem(key);
  } catch {
    // ignore
  }
}

// Stable, URL-safe id derived from a (provider, seed) pair. Never
// hash — these stay readable in localStorage so a developer can
// inspect what's there.
function makeProfileId(provider, seed) {
  const safeSeed = String(seed || Date.now()).toLowerCase().replace(/[^a-z0-9]/g, '_');
  return `local_${provider}_${safeSeed}`;
}

const DEFAULT_AVATAR = 'stamp_collector';
const DEFAULT_TITLE = '동네 도장 수집가';

// Minimal "are required keys here?" check so we don't run migration
// on a fresh-format profile.
function isLegacyProfile(raw) {
  if (!raw || typeof raw !== 'object') return false;
  return !raw.provider || !raw.id;
}

// Convert a legacy {nickname, email, user_id} blob into the new
// passport-identity shape. We keep the legacy user_id so the stamps
// bucket key (`stampport:stamps:<user_id>`) still resolves to the
// pre-existing data — no data loss.
export function migrateLegacyProfile(legacy) {
  if (!legacy || typeof legacy !== 'object') return null;
  const seed = legacy.email || legacy.user_id || legacy.nickname || `legacy_${Date.now()}`;
  const id = makeProfileId('guest', seed);
  return {
    id,
    provider: 'guest',
    provider_user_id: null,
    nickname: String(legacy.nickname || '게스트').slice(0, 20),
    avatar_style: legacy.avatar_style || DEFAULT_AVATAR,
    passport_title: legacy.passport_title || DEFAULT_TITLE,
    level: typeof legacy.level === 'number' ? legacy.level : 1,
    exp: typeof legacy.exp === 'number' ? legacy.exp : 0,
    created_at: legacy.created_at || new Date().toISOString(),
    last_login_at: new Date().toISOString(),
    email: legacy.email || null,
    user_id: legacy.user_id || id,
  };
}

export function loadUser() {
  const raw = readJson(USER_KEY, null);
  if (!raw) return null;
  if (!isLegacyProfile(raw)) return raw;
  // Legacy profile detected — migrate in place so subsequent reads
  // are already on the new schema. We deliberately *don't* clear
  // legacy fields; leave them for any other code path that still
  // reads them.
  const migrated = migrateLegacyProfile(raw);
  if (migrated) writeJson(USER_KEY, migrated);
  return migrated;
}

export function saveUser(user) {
  if (!user) return;
  writeJson(USER_KEY, user);
}

export function clearUser() {
  removeKey(USER_KEY);
}

// Build a fresh profile for a given provider. Caller decides
// nickname / provider_user_id; everything else gets a sensible
// Stampport-tone default.
export function makeNewProfile({
  provider = 'guest',
  nickname,
  email = null,
  provider_user_id = null,
  avatar_style = DEFAULT_AVATAR,
  passport_title = DEFAULT_TITLE,
  seed,
} = {}) {
  const id = makeProfileId(provider, seed || provider_user_id || email || nickname);
  const now = new Date().toISOString();
  return {
    id,
    provider,
    provider_user_id,
    nickname: String(nickname || '여행자').slice(0, 20),
    avatar_style,
    passport_title,
    level: 1,
    exp: 0,
    created_at: now,
    last_login_at: now,
    email,
    // Use the same id as user_id so per-user storage buckets are
    // isolated by provider+seed. Legacy guests reuse their old
    // user_id via migrateLegacyProfile().
    user_id: id,
  };
}

export const PROFILE_DEFAULTS = {
  avatar_style: DEFAULT_AVATAR,
  passport_title: DEFAULT_TITLE,
};

export function stampsKey(userId) {
  return `stampport:stamps:${userId}`;
}

export function loadStamps(userId) {
  return readJson(stampsKey(userId), []);
}

export function saveStamps(userId, stamps) {
  writeJson(stampsKey(userId), stamps);
}

export function profileMetaKey(userId) {
  return `stampport:profile:${userId}`;
}

export function loadProfileMeta(userId) {
  return readJson(profileMetaKey(userId), { selected_title_id: null });
}

export function saveProfileMeta(userId, meta) {
  writeJson(profileMetaKey(userId), meta);
}
