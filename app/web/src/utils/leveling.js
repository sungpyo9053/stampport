// EXP / level math + per-stamp grade.
//
// Stampport's RPG hook is the verification ladder. The base reward for
// a stamp is decided by *how the player proved they were there*, not
// by the place's identity:
//
//   verification level → base EXP → grade
//   manual              5            C  (이름·후기만)
//   location           15            B  (위치 확인)
//   photo              20            A  (사진 첨부)
//   verified           30            S  (위치 + 사진)
//
// On top of that, content bonuses still apply — they reward filling
// out the passport more thoroughly:
//
//   note ≥ 10자          +5
//   mood selected         +3
//   tags ×N (max 5)       +2 each
//   new area              +10
//   new category          +6
//
// All weights live here so balance changes ripple through the form
// preview, the result screen and the passport totals consistently.

export const EXP_NOTE_MIN_CHARS = 10;
export const EXP_PER_NOTE = 5;
export const EXP_PER_MOOD = 3;
export const EXP_PER_TAG = 2;
export const EXP_PER_NEW_AREA = 10;
export const EXP_PER_NEW_CATEGORY = 6;

// Verification ladder — base EXP and grade per level.
export const VERIFICATION_LEVELS = ['manual', 'location', 'photo', 'verified'];

const VERIFICATION_DEFS = {
  manual: {
    label: '직접 입력 도장',
    short: 'Manual Stamp',
    grade: 'C',
    color: '#877f6c',
    base_exp: 5,
    description: '이름과 후기만으로 받은 동네 도장',
  },
  location: {
    label: '위치 확인 도장',
    short: 'Location Stamp',
    grade: 'B',
    color: '#6b4a2b',
    base_exp: 15,
    description: '현재 위치를 확인한 발견 도장',
  },
  photo: {
    label: '사진 첨부 도장',
    short: 'Photo Stamp',
    grade: 'A',
    color: '#6e1f2a',
    base_exp: 20,
    description: '그날의 분위기까지 남긴 비자',
  },
  verified: {
    label: '여권 비자',
    short: 'Verified Stamp',
    grade: 'S',
    color: '#c9a23a',
    base_exp: 30,
    description: '위치 + 사진까지 인증된 정식 비자',
  },
};

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

// Derive verification level from inputs. Photo+location promotes to
// verified; either-alone keeps the lower tier; neither falls to
// manual. We take stamp.verification_level as a hint but recompute
// from the actual fields so we never drift.
export function verificationLevelFor(stamp) {
  const photo = hasPhoto(stamp);
  const loc = hasLocation(stamp);
  if (photo && loc) return 'verified';
  if (photo) return 'photo';
  if (loc) return 'location';
  return 'manual';
}

export function verificationDef(level) {
  return VERIFICATION_DEFS[level] || VERIFICATION_DEFS.manual;
}

// Per-stamp EXP. Returns a line-item breakdown so the form + result
// screens can show "기본 / 후기 / 기분 …" without redoing the math.
export function expGainBreakdown(stamp, previousStamps = []) {
  const items = [];
  const level = verificationLevelFor(stamp);
  const def = verificationDef(level);
  items.push({ key: 'base', label: `${def.short} (${def.grade}등급)`, exp: def.base_exp });

  if (hasMeaningfulNote(stamp)) {
    items.push({ key: 'note', label: '방문 후기', exp: EXP_PER_NOTE });
  }
  if (hasMood(stamp)) {
    items.push({ key: 'mood', label: '오늘의 기분', exp: EXP_PER_MOOD });
  }
  const tagCount = Math.min(stamp?.tags?.length || 0, 5);
  if (tagCount > 0) {
    items.push({
      key: 'tags',
      label: `태그 ×${tagCount}`,
      exp: tagCount * EXP_PER_TAG,
    });
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
    const def = verificationDef(verificationLevelFor(s));
    exp += def.base_exp;
    if (hasMeaningfulNote(s)) exp += EXP_PER_NOTE;
    if (hasMood(s)) exp += EXP_PER_MOOD;
    exp += Math.min(s.tags?.length || 0, 5) * EXP_PER_TAG;
    if (s.area && !seenAreas.has(s.area)) {
      exp += EXP_PER_NEW_AREA;
      seenAreas.add(s.area);
    }
    if (s.category && !seenCategories.has(s.category)) {
      exp += EXP_PER_NEW_CATEGORY;
      seenCategories.add(s.category);
    }
  }
  return exp;
}

// Stamp grade — derived from the verification ladder. We expose the
// list of "checks" so the form preview can show what's missing for the
// next tier.
export function stampGradeFor(stamp) {
  const level = verificationLevelFor(stamp);
  const def = verificationDef(level);
  const checks = [
    { key: 'note',     label: '방문 후기',  met: hasMeaningfulNote(stamp) },
    { key: 'tags',     label: '태그 1+',     met: (stamp?.tags?.length || 0) >= 1 },
    { key: 'mood',     label: '기분 태그',  met: hasMood(stamp) },
    { key: 'location', label: '위치 확인',  met: hasLocation(stamp) },
    { key: 'photo',    label: '사진',        met: hasPhoto(stamp) },
  ];
  return {
    grade: def.grade,
    label: def.label,
    color: def.color,
    level,
    base_exp: def.base_exp,
    description: def.description,
    score: checks.filter((c) => c.met).length,
    maxScore: checks.length,
    checks,
  };
}

// Hint for the form: what's the next verification tier and what does
// the player need to add to get there?
export function nextVerificationHint(stamp) {
  const level = verificationLevelFor(stamp);
  const photo = hasPhoto(stamp);
  const loc = hasLocation(stamp);
  if (level === 'verified') return null;
  if (level === 'photo') {
    return { next: 'verified', need: '위치 확인', exp_delta: 30 - 20 };
  }
  if (level === 'location') {
    return { next: 'verified', need: '사진 첨부', exp_delta: 30 - 15 };
  }
  // manual — suggest the cheaper next step first
  if (!loc && !photo) {
    return { next: 'location', need: '위치 확인을 추가하면 Location Stamp', exp_delta: 15 - 5 };
  }
  return { next: 'photo', need: '사진을 추가하면 Photo Stamp', exp_delta: 20 - 5 };
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
