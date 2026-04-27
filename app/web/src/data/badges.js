export const BADGE_DEFS = [
  {
    id: 'cafe_starter',
    name: '카페 입문자',
    description: '카페 스탬프 3개 모으기',
    icon: '☕',
    titleLabel: '카페 입문자',
    required: 3,
    progress: (stamps) => stamps.filter((s) => s.category === 'cafe').length,
  },
  {
    id: 'bakery_pilgrim',
    name: '빵지순례 시작',
    description: '빵집 스탬프 3개 모으기',
    icon: '🥐',
    titleLabel: '빵지 순례자',
    required: 3,
    progress: (stamps) => stamps.filter((s) => s.category === 'bakery').length,
  },
  {
    id: 'restaurant_explorer',
    name: '맛집 탐험가',
    description: '맛집 스탬프 5개 모으기',
    icon: '🍽',
    titleLabel: '맛집 탐험가',
    required: 5,
    progress: (stamps) => stamps.filter((s) => s.category === 'restaurant').length,
  },
  {
    id: 'gwanak_explorer',
    name: '관악구 탐험가',
    description: '관악에서 스탬프 3개 모으기',
    icon: '🗺',
    titleLabel: '관악 로컬',
    required: 3,
    progress: (stamps) => stamps.filter((s) => s.area === '관악').length,
  },
  {
    id: 'seongsu_explorer',
    name: '성수동 탐험가',
    description: '성수에서 스탬프 3개 모으기',
    icon: '🏙',
    titleLabel: '성수 로컬',
    required: 3,
    progress: (stamps) => stamps.filter((s) => s.area === '성수').length,
  },
  {
    id: 'solo_starter',
    name: '혼밥 입문자',
    description: '혼밥 가능 태그 3회 모으기',
    icon: '🥣',
    titleLabel: '혼밥 미식가',
    required: 3,
    progress: (stamps) => stamps.filter((s) => s.tags?.includes('혼밥 가능')).length,
  },
  {
    id: 'dessert_lover',
    name: '디저트 러버',
    description: '디저트 태그 5회 모으기',
    icon: '🍰',
    titleLabel: '디저트 러버',
    required: 5,
    progress: (stamps) => stamps.filter((s) => s.tags?.includes('디저트')).length,
  },
  {
    id: 'waiting_warrior',
    name: '웨이팅 전사',
    description: '웨이팅 태그 3회 모으기',
    icon: '⏳',
    titleLabel: '웨이팅 전사',
    required: 3,
    progress: (stamps) => stamps.filter((s) => s.tags?.includes('웨이팅')).length,
  },
  {
    id: 'weekend_explorer',
    name: '주말 탐험가',
    description: '주말 방문 스탬프 3개 모으기',
    icon: '🌤',
    titleLabel: '주말 탐험가',
    required: 3,
    progress: (stamps) =>
      stamps.filter((s) => {
        if (!s.visited_at) return false;
        const day = new Date(s.visited_at).getDay();
        return day === 0 || day === 6;
      }).length,
  },
  {
    id: 'salt_bread_collector',
    name: '소금빵 수집가',
    description: '소금빵 태그 3회 모으기',
    icon: '🧂',
    titleLabel: '소금빵 수집가',
    required: 3,
    progress: (stamps) => stamps.filter((s) => s.tags?.includes('소금빵')).length,
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
    };
  });
}
