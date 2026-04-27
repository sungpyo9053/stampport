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

export function loadUser() {
  return readJson(USER_KEY, null);
}

export function saveUser(user) {
  writeJson(USER_KEY, user);
}

export function clearUser() {
  removeKey(USER_KEY);
}

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
