function startOfWeek(date) {
  const d = new Date(date);
  const day = d.getDay();
  const diff = (day + 6) % 7;
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() - diff);
  return d;
}

function inThisWeek(date, weekStart) {
  if (!date) return false;
  const d = new Date(date);
  return d >= weekStart && d < new Date(weekStart.getTime() + 7 * 24 * 60 * 60 * 1000);
}

function distinctVisitDays(stamps, weekStart) {
  const set = new Set();
  for (const s of stamps) {
    if (!inThisWeek(s.visited_at, weekStart)) continue;
    if (s.visited_at) set.add(String(s.visited_at).slice(0, 10));
  }
  return set.size;
}

function placeRevisitedThisWeek(stamps, weekStart) {
  const earlier = new Map();
  const thisWeek = new Set();
  for (const s of stamps) {
    const place = (s.place_name || '').trim().toLowerCase();
    if (!place) continue;
    if (inThisWeek(s.visited_at, weekStart)) {
      thisWeek.add(place);
    } else {
      earlier.set(place, true);
    }
  }
  let count = 0;
  for (const place of thisWeek) {
    if (earlier.has(place)) count += 1;
  }
  return count;
}

export const QUEST_DEFS = [
  {
    id: 'three_stamps_this_week',
    title: '이번 주 도장 3개 찍기',
    description: '한 주에 세 곳을 다녀오면 주간 퀘스트 완료.',
    reward_exp: 35,
    required: 3,
    progress: (stamps, weekStart) =>
      stamps.filter((s) => inThisWeek(s.visited_at, weekStart)).length,
    nextHint: (progress, required) =>
      progress >= required
        ? '완료! 보상 EXP를 자동으로 챙겼어요.'
        : `${required - progress}곳만 더 다녀오면 끝나요.`,
  },
  {
    id: 'new_area_this_week',
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
    nextHint: (progress, required) =>
      progress >= required
        ? '완료! 새 지역 비자가 진행 중이에요.'
        : '이번 주는 안 가본 동네로 한 번 가 볼까요?',
  },
  {
    id: 'verified_this_week',
    title: '위치 + 사진 인증 도장 1개',
    description: 'S등급(여권 비자) 도장은 EXP가 가장 커요.',
    reward_exp: 30,
    required: 1,
    progress: (stamps, weekStart) =>
      stamps.filter(
        (s) =>
          inThisWeek(s.visited_at, weekStart) &&
          !!s.photo_data_url &&
          !!s.location_label,
      ).length,
    nextHint: (progress, required) =>
      progress >= required
        ? '완료! 여권에 정식 비자가 늘었어요.'
        : '도장을 찍을 때 위치 확인 + 사진을 같이 추가해 보세요.',
  },
  {
    id: 'revisit_this_week',
    title: '단골 후보 만들기',
    description: '예전에 갔던 곳을 이번 주에 다시 한 번.',
    reward_exp: 25,
    required: 1,
    progress: (stamps, weekStart) => placeRevisitedThisWeek(stamps, weekStart),
    nextHint: (progress, required) =>
      progress >= required
        ? '완료! 단골 후보 뱃지가 한 걸음 더 가까워졌어요.'
        : '예전에 좋았던 곳을 한 번 더 다녀오면 단골 후보가 돼요.',
  },
  {
    id: 'streak_two_days',
    title: '연속 방문 streak 2일',
    description: '서로 다른 두 날짜에 도장 찍기.',
    reward_exp: 20,
    required: 2,
    progress: (stamps, weekStart) => distinctVisitDays(stamps, weekStart),
    nextHint: (progress, required) =>
      progress >= required
        ? '완료! 이번 주는 꾸준했어요.'
        : `${required - progress}일만 더 방문하면 streak 보상.`,
  },
];

export function computeQuests(stamps, now = new Date()) {
  const weekStart = startOfWeek(now);
  return QUEST_DEFS.map((def) => {
    const raw = def.progress(stamps, weekStart);
    const progress = Math.min(raw, def.required);
    const completed = progress >= def.required;
    return {
      id: def.id,
      title: def.title,
      description: def.description,
      reward_exp: def.reward_exp,
      required: def.required,
      progress,
      completed,
      next_hint: def.nextHint ? def.nextHint(progress, def.required) : '',
    };
  });
}

export function weekRangeLabel(now = new Date()) {
  const start = startOfWeek(now);
  const end = new Date(start.getTime() + 6 * 24 * 60 * 60 * 1000);
  const fmt = (d) => `${d.getMonth() + 1}월 ${d.getDate()}일`;
  return `${fmt(start)} – ${fmt(end)}`;
}
