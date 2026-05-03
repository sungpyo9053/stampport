import { categoryLabel } from '../data/options.js';

const CATEGORY_POOL = {
  cafe: [
    '다음 카페에서는 시그니처 메뉴를 도전해 보세요.',
    '같은 동네 다른 카페 한 곳을 더 찾아 비교해 보세요.',
    '아침 시간대 카페 한 곳을 새로 방문해 보세요.',
    '디저트가 강한 카페로 한 번 더 가 보세요.',
  ],
  bakery: [
    '다음 빵집에서는 새로운 빵 한 종류에 도전해 보세요.',
    '소금빵으로 유명한 다른 빵집과 비교해 보세요.',
    '오픈 시간에 갓 나온 빵을 노려 보세요.',
    '같은 동네 빵집 한 곳을 더 추가해 보세요.',
  ],
  restaurant: [
    '같이 가고 싶은 사람과 다시 한 번 방문해 보세요.',
    '대표 메뉴 외 다른 메뉴도 시도해 보세요.',
    '같은 카테고리의 새로운 맛집을 한 곳 더 시도해 보세요.',
    '다른 동네의 비슷한 맛집을 비교해 보세요.',
  ],
  dessert: [
    '다른 디저트 가게의 같은 메뉴와 비교해 보세요.',
    '계절 한정 메뉴가 있는 디저트 가게를 찾아 보세요.',
    '디저트와 어울리는 음료 페어링을 시도해 보세요.',
    '근처 카페에서 디저트 더블 코스에 도전해 보세요.',
  ],
};

const TAG_POOL = {
  소금빵: '다른 가게의 소금빵과 맛을 비교해 보세요.',
  조용함: '비슷한 분위기의 조용한 공간을 한 곳 더 찾아 보세요.',
  웨이팅: '웨이팅을 견딘 만큼, 다음에는 평일 낮 시간대도 노려 보세요.',
  '혼밥 가능': '혼밥하기 좋은 다른 동네를 한 곳 더 탐험해 보세요.',
  데이트: '같은 사람과 다른 컨셉의 장소를 한 곳 더 가 보세요.',
  감성공간: '같은 분위기의 감성 공간을 사진으로 남겨 모아 보세요.',
  디저트: '디저트 러버 뱃지에 한 걸음 더 가까워지는 방문을 해 보세요.',
  창가자리: '창가 자리가 좋은 다른 카페를 한 곳 더 찾아 보세요.',
  사진맛집: '같은 컨셉의 사진맛집을 SNS 카드로 모아 보세요.',
  '재방문 의사': '재방문할 때 다른 메뉴/시간대로 새 스탬프를 찍어 보세요.',
};

const FALLBACKS = [
  '같은 지역의 새로운 가게를 한 곳 더 탐험해 보세요.',
  '다음에는 다른 카테고리의 가게에 도전해 보세요.',
  '비슷한 취향을 가진 친구에게 이번 방문을 추천해 보세요.',
  '주말에 새로운 동네에서 스탬프를 찍어 보세요.',
];

export function generateKickPoints(stamp, badges = []) {
  const relatedBadge = badges
    .filter((b) => !b.earned && b.progress > 0)
    .find((b) => {
      const text = (b.name + b.description).toLowerCase();
      return (stamp.area && text.includes(stamp.area))
        || text.includes(categoryLabel(stamp.category));
    }) || null;

  const badge_hint = relatedBadge
    ? `${relatedBadge.name}까지 ${relatedBadge.required - relatedBadge.progress}곳 남음`
    : null;

  const visitCount = badges.reduce((n, b) => n + b.progress, 0);
  const pool = CATEGORY_POOL[stamp.category] || FALLBACKS;
  const action_label = pool[visitCount % pool.length];

  return [{
    area: stamp.area || '',
    category: stamp.category || '',
    badge_hint,
    exp_preview: 20,
    action_label,
  }];
}
