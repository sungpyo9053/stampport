export const CATEGORIES = [
  { id: 'cafe', label: '카페', icon: '☕' },
  { id: 'bakery', label: '빵집', icon: '🥐' },
  { id: 'restaurant', label: '맛집', icon: '🍽' },
  { id: 'dessert', label: '디저트', icon: '🍰' },
];

export const AREAS = ['성수', '관악', '망원', '연남', '한남', '기타'];

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

export const VERIFICATION_LEVELS = ['manual', 'photo'];

export function categoryLabel(id) {
  return CATEGORIES.find((c) => c.id === id)?.label || id;
}

export function categoryIcon(id) {
  return CATEGORIES.find((c) => c.id === id)?.icon || '📍';
}
