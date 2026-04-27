function startOfWeek(date) {
  const d = new Date(date);
  const day = d.getDay();
  const diff = (day + 6) % 7;
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() - diff);
  return d;
}

function inThisWeek(date, weekStart) {
  const d = new Date(date);
  return d >= weekStart && d < new Date(weekStart.getTime() + 7 * 24 * 60 * 60 * 1000);
}

export const QUEST_DEFS = [
  {
    id: 'first_stamp',
    title: '이번 주 첫 스탬프 찍기',
    description: '한 주의 시작은 도장 한 번에서.',
    reward_exp: 30,
    required: 1,
    progress: (stamps, weekStart) =>
      stamps.filter((s) => inThisWeek(s.visited_at, weekStart)).length,
  },
  {
    id: 'cafe_or_bakery',
    title: '카페 또는 빵집 스탬프 1개 찍기',
    description: '단골 카테고리부터 채워봐요.',
    reward_exp: 20,
    required: 1,
    progress: (stamps, weekStart) =>
      stamps.filter(
        (s) =>
          inThisWeek(s.visited_at, weekStart) &&
          (s.category === 'cafe' || s.category === 'bakery'),
      ).length,
  },
  {
    id: 'new_area',
    title: '새로운 지역 스탬프 찍기',
    description: '아직 안 가본 동네에 도장을.',
    reward_exp: 40,
    required: 1,
    progress: (stamps, weekStart) => {
      const previousAreas = new Set(
        stamps
          .filter((s) => !inThisWeek(s.visited_at, weekStart))
          .map((s) => s.area),
      );
      return stamps.filter(
        (s) => inThisWeek(s.visited_at, weekStart) && !previousAreas.has(s.area),
      ).length;
    },
  },
  {
    id: 'two_tags',
    title: '태그 2개 이상 포함해서 스탬프 찍기',
    description: '취향이 더 또렷해져요.',
    reward_exp: 25,
    required: 1,
    progress: (stamps, weekStart) =>
      stamps.filter(
        (s) => inThisWeek(s.visited_at, weekStart) && (s.tags?.length || 0) >= 2,
      ).length,
  },
];

export function computeQuests(stamps, now = new Date()) {
  const weekStart = startOfWeek(now);
  return QUEST_DEFS.map((def) => {
    const progress = Math.min(def.progress(stamps, weekStart), def.required);
    return {
      id: def.id,
      title: def.title,
      description: def.description,
      reward_exp: def.reward_exp,
      required: def.required,
      progress,
      completed: progress >= def.required,
    };
  });
}

export function weekRangeLabel(now = new Date()) {
  const start = startOfWeek(now);
  const end = new Date(start.getTime() + 6 * 24 * 60 * 60 * 1000);
  const fmt = (d) => `${d.getMonth() + 1}월 ${d.getDate()}일`;
  return `${fmt(start)} – ${fmt(end)}`;
}
