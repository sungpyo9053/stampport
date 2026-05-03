import { useState } from 'react';
import { useApp } from '../context/appContext.js';
import { BADGE_DEFS } from '../data/badges.js';

function getBadgeNavPath(b) {
  const text = (b.description || '') + ' ' + (b.name || '');
  let category = '';
  if (text.includes('카페')) category = 'cafe';
  else if (text.includes('빵')) category = 'bakery';
  else if (text.includes('맛집')) category = 'restaurant';
  else if (text.includes('디저트')) category = 'dessert';
  let area = '';
  for (const a of ['성수', '망원', '연남', '관악']) {
    if (text.includes(a)) { area = a; break; }
  }
  const params = new URLSearchParams();
  if (category) params.set('category', category);
  if (area) params.set('area', area);
  const qs = params.toString();
  return qs ? `/stamp?${qs}` : '/stamp';
}

export default function Badges({ navigate }) {
  const { badges, earnedBadges, profileMeta, setSelectedTitle, selectedTitle } = useApp();
  const [toast, setToast] = useState('');

  const selectedTitleId = profileMeta?.selected_title_id;
  const currentTitleLevel =
    badges.find((b) => b.id === selectedTitleId)?.level
    ?? Math.max(...earnedBadges.map((b) => b.level).filter(Boolean), 1);

  const showToast = (text) => {
    setToast(text);
    window.setTimeout(() => setToast(''), 1800);
  };

  return (
    <section className="form-stack" style={{ gap: 18 }}>
      <div className="page-head">
        <span className="page-eyebrow">Badges & Titles</span>
        <h1>뱃지와 칭호</h1>
        <p>조건을 채우면 뱃지가 빛나고, 칭호로 사용할 수 있어요.</p>
      </div>

      <div className="title-card">
        <div className="title-info">
          <div className="label">My Title</div>
          <div className="name">{selectedTitle}</div>
        </div>
        <div className="title-medal" aria-hidden="true">★</div>
      </div>

      <div className="section-heading">
        <h2>획득한 뱃지</h2>
      </div>
      {earnedBadges.length ? (
        <div className="badge-grid">
          {earnedBadges.map((b) => {
            const isSelected = profileMeta.selected_title_id === b.id;
            const def = BADGE_DEFS.find((d) => d.id === b.id);
            return (
              <button
                key={b.id}
                type="button"
                className={`badge-card earned`}
                onClick={() => {
                  setSelectedTitle(b.id);
                  showToast(`'${def?.titleLabel || b.name}' 칭호로 설정했어요`);
                }}
              >
                <div className="badge-medal" aria-hidden="true">{b.icon}</div>
                <div className="badge-name">{b.name}</div>
                <div className="badge-desc">{b.description}</div>
                <div className="badge-progress">
                  <div className="num">
                    {isSelected ? '★ 현재 칭호' : '탭하면 칭호로 설정'}
                  </div>
                </div>
              </button>
            );
          })}
        </div>
      ) : (
        <p className="empty">아직 획득한 뱃지가 없어요. 스탬프를 모아 보세요.</p>
      )}

      <div className="section-heading">
        <h2>도전 중인 뱃지</h2>
      </div>
      <div className="badge-grid">
        {badges
          .filter((b) => !b.earned)
          .map((b) => {
            const ratio = Math.min(b.progress / b.required, 1);
            const locked = (b.lockedUntilLevel ?? 1) > currentTitleLevel;
            return (
              <div
                key={b.id}
                className={`badge-card ${locked ? 'locked' : ''}`}
              >
                <div className="badge-medal" aria-hidden="true">{b.icon}</div>
                <div className="badge-name">{b.name}</div>
                <div className="badge-desc">{b.description}</div>
                <div className="badge-progress">
                  <div className="bar">
                    <div className="fill" style={{ width: `${Math.round(ratio * 100)}%` }} />
                  </div>
                  <div className="num">
                    {b.progress}/{b.required}
                  </div>
                </div>
                {!locked && (
                  <button
                    type="button"
                    className="btn btn-secondary"
                    style={{ marginTop: 8, fontSize: 12, padding: '4px 10px' }}
                    onClick={() => navigate(getBadgeNavPath(b))}
                  >
                    여기 다음에 가봐 →
                  </button>
                )}
              </div>
            );
          })}
      </div>

      <button
        type="button"
        className="btn btn-secondary btn-block"
        onClick={() => navigate('/passport')}
      >
        여권으로
      </button>

      {toast ? <div className="toast">{toast}</div> : null}
    </section>
  );
}
