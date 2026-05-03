// Count how many distinct places match a predicate. Repeat visits to
// the same place collapse to one — we want "exploration breadth"
// signals, not "visit count" signals.
function distinctPlaces(stamps, pred) {
  const seen = new Set();
  for (const s of stamps) {
    if (!pred(s)) continue;
    const key = (s.place_name || '').trim().toLowerCase();
    if (key) seen.add(key);
  }
  return seen.size;
}

// Count how many *places* the user has visited 2+ times — used by the
// 단골 후보 badge.
function regularPlaceCount(stamps) {
  const counts = new Map();
  for (const s of stamps) {
    const key = (s.place_name || '').trim().toLowerCase();
    if (!key) continue;
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  let regulars = 0;
  for (const n of counts.values()) {
    if (n >= 2) regulars += 1;
  }
  return regulars;
}

export const BADGE_DEFS = [
  {
    id: 'cafe_starter',
    name: '카페 입문자',
    description: '카페 스탬프 3곳 모으기',
    icon: '☕',
    titleLabel: '카페 입문자',
    required: 3,
    level: 1,
    tier: 'starter',
    lockedUntilLevel: 1,
    progress: (stamps) => distinctPlaces(stamps, (s) => s.category === 'cafe'),
  },
  {
    id: 'bakery_pilgrim',
    name: '빵지순례 시작',
    description: '빵집 스탬프 3곳 모으기',
    icon: '🥐',
    titleLabel: '빵지 순례자',
    required: 3,
    level: 1,
    tier: 'starter',
    lockedUntilLevel: 1,
    progress: (stamps) => distinctPlaces(stamps, (s) => s.category === 'bakery'),
  },
  {
    id: 'restaurant_explorer',
    name: '맛집 탐험가',
    description: '맛집 스탬프 5곳 모으기',
    icon: '🍽',
    titleLabel: '맛집 탐험가',
    required: 5,
    level: 2,
    tier: 'lover',
    lockedUntilLevel: 1,
    progress: (stamps) => distinctPlaces(stamps, (s) => s.category === 'restaurant'),
  },
  {
    id: 'dessert_explorer',
    name: '디저트 탐험가',
    description: '디저트 스탬프 5곳 모으기',
    icon: '🍰',
    titleLabel: '디저트 탐험가',
    required: 5,
    level: 2,
    tier: 'lover',
    lockedUntilLevel: 1,
    progress: (stamps) => distinctPlaces(stamps, (s) => s.category === 'dessert'),
  },
  {
    id: 'seongsu_cafe_visa',
    name: '성수 카페 비자',
    description: '성수에서 카페 3곳 모으기',
    icon: '🏙',
    titleLabel: '성수 카페 비자',
    required: 3,
    level: 2,
    tier: 'lover',
    lockedUntilLevel: 2,
    progress: (stamps) =>
      distinctPlaces(stamps, (s) => s.area === '성수' && s.category === 'cafe'),
  },
  {
    id: 'mangwon_dessert_visa',
    name: '망원 디저트 비자',
    description: '망원에서 디저트 2곳 모으기',
    icon: '🌿',
    titleLabel: '망원 디저트 비자',
    required: 2,
    level: 1,
    tier: 'starter',
    lockedUntilLevel: 1,
    progress: (stamps) =>
      distinctPlaces(stamps, (s) => s.area === '망원' && s.category === 'dessert'),
  },
  {
    id: 'yeonnam_visa',
    name: '연남 동네 비자',
    description: '연남에서 어떤 카테고리든 3곳',
    icon: '🌸',
    titleLabel: '연남 단골',
    required: 3,
    level: 2,
    tier: 'lover',
    lockedUntilLevel: 2,
    progress: (stamps) => distinctPlaces(stamps, (s) => s.area === '연남'),
  },
  {
    id: 'gwanak_explorer',
    name: '관악구 탐험가',
    description: '관악에서 스탬프 3곳 모으기',
    icon: '🗺',
    titleLabel: '관악 로컬',
    required: 3,
    level: 2,
    tier: 'lover',
    lockedUntilLevel: 2,
    progress: (stamps) => distinctPlaces(stamps, (s) => s.area === '관악'),
  },
  {
    id: 'salt_bread_collector',
    name: '소금빵 수집가',
    description: '소금빵 태그 3곳 모으기',
    icon: '🧂',
    titleLabel: '소금빵 수집가',
    required: 3,
    level: 2,
    tier: 'lover',
    lockedUntilLevel: 1,
    progress: (stamps) =>
      distinctPlaces(stamps, (s) => s.tags?.includes('소금빵')),
  },
  {
    id: 'solo_starter',
    name: '혼밥 입문자',
    description: '혼밥 가능 태그 3곳 모으기',
    icon: '🥣',
    titleLabel: '혼밥 미식가',
    required: 3,
    level: 1,
    tier: 'starter',
    lockedUntilLevel: 1,
    progress: (stamps) =>
      distinctPlaces(stamps, (s) => s.tags?.includes('혼밥 가능')),
  },
  {
    id: 'weekend_explorer',
    name: '주말 탐험가',
    description: '주말 방문 스탬프 3개',
    icon: '🌤',
    titleLabel: '주말 탐험가',
    required: 3,
    level: 1,
    tier: 'starter',
    lockedUntilLevel: 1,
    progress: (stamps) =>
      stamps.filter((s) => {
        if (!s.visited_at) return false;
        const day = new Date(s.visited_at).getDay();
        return day === 0 || day === 6;
      }).length,
  },
  {
    id: 'verified_collector',
    name: '여권 비자 수집가',
    description: 'S등급(위치+사진) 도장 3개',
    icon: '🏷',
    titleLabel: '여권 비자 수집가',
    required: 3,
    level: 3,
    tier: 'master',
    lockedUntilLevel: 3,
    progress: (stamps) =>
      stamps.filter((s) => {
        const g = s.grade?.grade || (s.verification_level === 'verified' ? 'S' : null);
        if (g === 'S') return true;
        return !!s.photo_data_url && !!s.location_label;
      }).length,
  },
  {
    id: 'regular_candidate',
    name: '단골 후보',
    description: '같은 장소 2회 이상 방문한 곳 1개',
    icon: '🪪',
    titleLabel: '단골 후보',
    required: 1,
    level: 1,
    tier: 'starter',
    lockedUntilLevel: 1,
    progress: (stamps) => regularPlaceCount(stamps),
  },
];

export function computeBadges(stamps) {
  return BADGE_DEFS.map((def) => {
    const progress = Math.min(def.progress(stamps), def.required);
    const earned = progress >= def.required;
    return {
      id: def.id,
      name: def.name,
      description: def.description,
      icon: def.icon,
      titleLabel: def.titleLabel,
      required: def.required,
      progress,
      earned,
      level: def.level,
      tier: def.tier,
      lockedUntilLevel: def.lockedUntilLevel,
    };
  });
}

// Compute the diff between two badge snapshots — used after addStamp
// to surface "new this stamp" badges on the result screen.
export function newlyEarnedBadges(prev, next) {
  const prevSet = new Set(prev.filter((b) => b.earned).map((b) => b.id));
  return next.filter((b) => b.earned && !prevSet.has(b.id));
}
