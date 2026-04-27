export const CATEGORIES = [
  { id: 'cafe', label: '카페', icon: '☕' },
  { id: 'bakery', label: '빵집', icon: '🥐' },
  { id: 'restaurant', label: '맛집', icon: '🍽' },
  { id: 'dessert', label: '디저트', icon: '🍰' },
];

export const SUGGESTED_AREAS = ['성수', '관악', '망원', '연남', '한남'];

export const TAGS = [
  '소금빵',
  '조용함',
  '웨이팅',
  '혼밥 가능',
  '데이트',
  '감성공간',
  '디저트',
  '창가자리',
  '사진맛집',
  '재방문 의사',
];

export const VERIFICATION_LEVELS = ['manual', 'location', 'photo', 'verified'];

// Visit purpose chips — used when 대표 메뉴 is awkward (e.g., "산책 중
// 들름"). Either menu OR purpose must be filled before the stamp button
// activates, so the player always commits to *why* they were there.
export const VISIT_PURPOSES = [
  { id: 'explore',  label: '새 발견', emoji: '🧭' },
  { id: 'date',     label: '데이트',  emoji: '💞' },
  { id: 'work',     label: '작업/공부', emoji: '💻' },
  { id: 'meeting',  label: '약속/모임', emoji: '🤝' },
  { id: 'solo',     label: '혼자 시간', emoji: '🌿' },
  { id: 'dessert',  label: '단 거 충전', emoji: '🍰' },
  { id: 'regular',  label: '단골 인사', emoji: '🪪' },
];

export function categoryLabel(id) {
  return CATEGORIES.find((c) => c.id === id)?.label || id;
}

export function categoryIcon(id) {
  return CATEGORIES.find((c) => c.id === id)?.icon || '📍';
}

export function visitPurposeLabel(id) {
  return VISIT_PURPOSES.find((p) => p.id === id)?.label || '';
}
