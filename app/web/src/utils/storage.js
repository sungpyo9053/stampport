// Stampport localStorage adapter.
//
// User profile shape (Stampport Passport Identity):
//   {
//     id, provider, provider_user_id, nickname, avatar_style,
//     passport_title, level, exp, created_at, last_login_at,
//     email, user_id,
//   }
//
// Stamp shape (per-visit record kept under stampport:stamps:<user_id>):
//   {
//     id, user_id, place_name, area, area_source, category, tags[],
//     representative_menu, visit_purpose, visited_at, created_at,
//     experience_note, photo_data_url, location_label,
//     latitude, longitude, visit_mood,
//     verification_level,        // manual | location | photo | verified
//     grade, exp_breakdown, exp_gained,
//   }

const USER_KEY = 'stampport:user';

// localStorage budget for an embedded photo. Photos are downscaled
// in the form so we never write more than ~250KB into a single stamp.
export const PHOTO_MAX_DATA_URL_BYTES = 260_000;

// Abuse-prevention thresholds — enforced at write time.
export const DAILY_STAMP_LIMIT = 10;
export const RECENT_AREAS_LIMIT = 6;

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

function makeProfileId(provider, seed) {
  const safeSeed = String(seed || Date.now()).toLowerCase().replace(/[^a-z0-9]/g, '_');
  return `local_${provider}_${safeSeed}`;
}

const DEFAULT_AVATAR = 'stamp_collector';
const DEFAULT_TITLE = '동네 도장 수집가';

function isLegacyProfile(raw) {
  if (!raw || typeof raw !== 'object') return false;
  return !raw.provider || !raw.id;
}

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

// Fill in default values on legacy stamps so consumers can rely on the
// shape — we deliberately don't recompute exp_gained here (that lives
// in leveling.js' totalExp), only the structural fields.
function migrateStamp(s) {
  if (!s || typeof s !== 'object') return s;
  const out = {
    place_name: '',
    area: '기타',
    area_source: 'manual',
    category: 'cafe',
    tags: [],
    representative_menu: '',
    visit_purpose: '',
    experience_note: '',
    photo_data_url: '',
    location_label: '',
    latitude: null,
    longitude: null,
    visit_mood: '',
    verification_level: 'manual',
    ...s,
  };
  if (!Array.isArray(out.tags)) out.tags = [];
  return out;
}

export function loadStamps(userId) {
  const raw = readJson(stampsKey(userId), []);
  if (!Array.isArray(raw)) return [];
  return raw.map(migrateStamp);
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

// Recent-areas list (LRU, capped). Used by the StampForm area picker
// so a returning visitor sees their actual neighborhoods, not just the
// suggested chips.
export function recentAreasKey(userId) {
  return `stampport:recent_areas:${userId}`;
}

export function loadRecentAreas(userId) {
  const raw = readJson(recentAreasKey(userId), []);
  return Array.isArray(raw) ? raw.filter((x) => typeof x === 'string') : [];
}

export function pushRecentArea(userId, area) {
  if (!userId || !area) return;
  const trimmed = String(area).trim();
  if (!trimmed) return;
  const current = loadRecentAreas(userId).filter((a) => a !== trimmed);
  current.unshift(trimmed);
  writeJson(recentAreasKey(userId), current.slice(0, RECENT_AREAS_LIMIT));
}

// Abuse-prevention helpers. Calendar-day boundaries are computed in
// the user's local TZ so the message ("내일 다시 찍어 주세요") matches
// what the user sees.
function dayKey(date = new Date()) {
  const d = new Date(date);
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

function normalizePlace(name) {
  return (name || '').trim().toLowerCase().replace(/\s+/g, ' ');
}

function visitedDayKey(stamp) {
  if (!stamp) return '';
  if (stamp.visited_at) return String(stamp.visited_at).slice(0, 10);
  if (stamp.created_at) return String(stamp.created_at).slice(0, 10);
  return '';
}

// Returns: { ok, reason, message } — caller surfaces message verbatim.
export function checkStampLimits(stamps, candidate, today = dayKey()) {
  const list = Array.isArray(stamps) ? stamps : [];
  const todayCount = list.filter((s) => visitedDayKey(s) === today).length;
  if (todayCount >= DAILY_STAMP_LIMIT) {
    return {
      ok: false,
      reason: 'daily_limit',
      message: '오늘의 여권 페이지가 가득 찼어요. 내일 다시 찍어 주세요.',
    };
  }
  const place = normalizePlace(candidate?.place_name);
  if (place) {
    const dup = list.find(
      (s) =>
        visitedDayKey(s) === today &&
        normalizePlace(s.place_name) === place,
    );
    if (dup) {
      return {
        ok: false,
        reason: 'duplicate_place',
        message:
          '오늘은 이 장소에 이미 도장을 찍었어요. 내일 다시 남겨 주세요.',
      };
    }
  }
  return { ok: true };
}
