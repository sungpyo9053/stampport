// EXP / level math + per-stamp grade.
//
// MVP level curve: 100 EXP per level. Each stamp contributes a base
// reward plus bonuses keyed on what the player actually wrote in the
// stamp form. The bonuses are the "RPG hook" that makes a real visit
// (with a memo, a photo, location, mood) feel more rewarding than a
// drive-by name-only entry.
//
// All bonus weights live in this file so a balance change ripples
// through the form preview, the result screen, and the passport
// totals consistently.

export const EXP_PER_STAMP = 30;
export const EXP_PER_TAG = 4;
export const EXP_PER_NEW_AREA = 15;
export const EXP_PER_NEW_CATEGORY = 10;

// New "real visit" bonuses — set when the user actually proves they
// were there (memo / photo / location / mood). Each one is opt-in but
// weighted enough to matter against the 100-EXP-per-level curve.
export const EXP_NOTE_MIN_CHARS = 10;
export const EXP_PER_NOTE = 12;
export const EXP_PER_PHOTO = 14;
export const EXP_PER_LOCATION = 10;
export const EXP_PER_MOOD = 6;

// Tag thresholds for the grade. We don't grade on a single tag because
// any drive-by entry might tap one. Grade jumps require richer input.
const GRADE_TAG_THRESHOLD = 2;

// Compact accessor: does this stamp's note count as a "real" memo?
function hasMeaningfulNote(stamp) {
  const text = (stamp?.experience_note || '').trim();
  return text.length >= EXP_NOTE_MIN_CHARS;
}

function hasPhoto(stamp) {
  return !!(stamp?.photo_data_url && stamp.photo_data_url.length > 0);
}

function hasLocation(stamp) {
  return !!(stamp?.location_label && stamp.location_label.length > 0);
}

function hasMood(stamp) {
  return !!(stamp?.visit_mood && stamp.visit_mood.length > 0);
}

// Per-stamp EXP. Returns a line-item breakdown so the form + result
// screens can show "기본 +30 / 메모 +12 / 사진 +14 …" without redoing
// the math themselves.
export function expGainBreakdown(stamp, previousStamps = []) {
  const items = [];
  items.push({ key: 'base', label: '도장 기본', exp: EXP_PER_STAMP });

  const tagCount = Math.min(stamp?.tags?.length || 0, 5);
  if (tagCount > 0) {
    items.push({
      key: 'tags',
      label: `태그 ×${tagCount}`,
      exp: tagCount * EXP_PER_TAG,
    });
  }

  if (hasMeaningfulNote(stamp)) {
    items.push({ key: 'note', label: '방문 후기 작성', exp: EXP_PER_NOTE });
  }
  if (hasPhoto(stamp)) {
    items.push({ key: 'photo', label: '사진 첨부', exp: EXP_PER_PHOTO });
  }
  if (hasLocation(stamp)) {
    items.push({ key: 'location', label: '위치 확인', exp: EXP_PER_LOCATION });
  }
  if (hasMood(stamp)) {
    items.push({ key: 'mood', label: '오늘의 기분', exp: EXP_PER_MOOD });
  }

  const knownAreas = new Set(previousStamps.map((s) => s.area));
  if (stamp?.area && !knownAreas.has(stamp.area)) {
    items.push({
      key: 'new_area',
      label: `새 지역 · ${stamp.area}`,
      exp: EXP_PER_NEW_AREA,
    });
  }
  const knownCategories = new Set(previousStamps.map((s) => s.category));
  if (stamp?.category && !knownCategories.has(stamp.category)) {
    items.push({
      key: 'new_category',
      label: '새 카테고리',
      exp: EXP_PER_NEW_CATEGORY,
    });
  }

  const total = items.reduce((acc, it) => acc + it.exp, 0);
  return { items, total };
}

export function expGainFor(stamp, previousStamps = []) {
  return expGainBreakdown(stamp, previousStamps).total;
}

// Total EXP for a passport. Re-walks all stamps so we never drift from
// expGainFor — if a stamp didn't store its computed exp_gained
// (legacy data), we recompute on the fly.
export function totalExp(stamps) {
  let exp = 0;
  const seenAreas = new Set();
  const seenCategories = new Set();
  for (const s of [...stamps].reverse()) {
    // Walk oldest → newest so "new area / new category" only fires
    // the first time we see each.
    const previous = {
      area: seenAreas.has(s.area),
      category: seenCategories.has(s.category),
    };
    exp += EXP_PER_STAMP;
    exp += Math.min(s.tags?.length || 0, 5) * EXP_PER_TAG;
    if (hasMeaningfulNote(s)) exp += EXP_PER_NOTE;
    if (hasPhoto(s)) exp += EXP_PER_PHOTO;
    if (hasLocation(s)) exp += EXP_PER_LOCATION;
    if (hasMood(s)) exp += EXP_PER_MOOD;
    if (s.area && !previous.area) {
      exp += EXP_PER_NEW_AREA;
      seenAreas.add(s.area);
    }
    if (s.category && !previous.category) {
      exp += EXP_PER_NEW_CATEGORY;
      seenCategories.add(s.category);
    }
  }
  return exp;
}

// Stamp grade — derived purely from input quality, not place identity.
// We deliberately reward the *experience input*, not the gourmet rating
// of the place, so the player has a reason to write things down.
//
// Criteria (each adds +1 to a 0-5 score):
//   1. Note ≥ 10자
//   2. Photo attached
//   3. Location verified
//   4. Mood selected
//   5. Tags ≥ 2개
//
// Grade map (cumulative quality):
//   0 → C  · 동네 도장  · just a place name
//   1 → C  · 동네 도장
//   2 → B  · 발견 도장
//   3 → A  · 맛집 비자
//   4 → A  · 맛집 비자
//   5 → S  · 여권 비자  · all signals lit
const GRADE_TABLE = [
  { grade: 'C', label: '동네 도장', color: '#877f6c' },
  { grade: 'C', label: '동네 도장', color: '#877f6c' },
  { grade: 'B', label: '발견 도장', color: '#6b4a2b' },
  { grade: 'A', label: '맛집 비자', color: '#6e1f2a' },
  { grade: 'A', label: '맛집 비자', color: '#6e1f2a' },
  { grade: 'S', label: '여권 비자', color: '#c9a23a' },
];

export function stampGradeFor(stamp) {
  const checks = [
    { key: 'note',     label: '방문 후기',  met: hasMeaningfulNote(stamp) },
    { key: 'photo',    label: '사진',        met: hasPhoto(stamp) },
    { key: 'location', label: '위치 확인',  met: hasLocation(stamp) },
    { key: 'mood',     label: '기분 태그',  met: hasMood(stamp) },
    { key: 'tags',     label: `태그 ${GRADE_TAG_THRESHOLD}+`, met: (stamp?.tags?.length || 0) >= GRADE_TAG_THRESHOLD },
  ];
  const score = checks.filter((c) => c.met).length;
  const tier = GRADE_TABLE[Math.max(0, Math.min(score, GRADE_TABLE.length - 1))];
  return {
    grade: tier.grade,
    label: tier.label,
    color: tier.color,
    score,
    maxScore: checks.length,
    checks,
  };
}

// Level curve. Linear for the MVP; the level value is intentionally
// 1-indexed so "Lv. 1" reads naturally even at zero EXP.
const LEVEL_STEP = 100;

export function levelFor(exp) {
  return Math.floor(exp / LEVEL_STEP) + 1;
}

export function levelProgress(exp) {
  const level = levelFor(exp);
  const within = exp - (level - 1) * LEVEL_STEP;
  return {
    level,
    expIntoLevel: within,
    expForLevel: LEVEL_STEP,
    expToNext: Math.max(0, LEVEL_STEP - within),
    ratio: Math.min(within / LEVEL_STEP, 1),
  };
}
