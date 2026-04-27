export const EXP_PER_STAMP = 30;
export const EXP_PER_TAG = 4;
export const EXP_PER_NEW_AREA = 15;
export const EXP_PER_NEW_CATEGORY = 10;

export function expGainFor(stamp, previousStamps) {
  let gained = EXP_PER_STAMP;
  gained += Math.min(stamp.tags?.length || 0, 5) * EXP_PER_TAG;
  const knownAreas = new Set(previousStamps.map((s) => s.area));
  if (stamp.area && !knownAreas.has(stamp.area)) gained += EXP_PER_NEW_AREA;
  const knownCategories = new Set(previousStamps.map((s) => s.category));
  if (stamp.category && !knownCategories.has(stamp.category)) gained += EXP_PER_NEW_CATEGORY;
  return gained;
}

export function totalExp(stamps) {
  let exp = 0;
  const seenAreas = new Set();
  const seenCategories = new Set();
  for (const s of stamps) {
    exp += EXP_PER_STAMP;
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
    ratio: Math.min(within / LEVEL_STEP, 1),
  };
}
