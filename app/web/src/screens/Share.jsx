import { useState } from 'react';
import { useApp } from '../context/appContext.js';
import { categoryIcon, categoryLabel } from '../data/options.js';

export default function Share({ navigate, stampId }) {
  const { stampById, user, level, selectedTitle } = useApp();
  const stamp = stampById(stampId);
  const [toast, setToast] = useState('');

  if (!stamp) {
    return (
      <section className="form-stack">
        <p className="empty">스탬프 정보를 찾을 수 없어요.</p>
        <button
          type="button"
          className="btn btn-primary btn-block"
          onClick={() => navigate('/passport')}
        >
          여권으로 돌아가기
        </button>
      </section>
    );
  }

  const dateLabel = new Date(stamp.visited_at).toLocaleDateString('ko-KR', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });

  const copy = async () => {
    const text =
      `[Stampport]\n` +
      `${stamp.place_name} (${stamp.area} · ${categoryLabel(stamp.category)})\n` +
      (stamp.representative_menu ? `대표 메뉴: ${stamp.representative_menu}\n` : '') +
      (stamp.tags?.length ? `#${stamp.tags.join(' #')}\n` : '') +
      `${user?.nickname || ''}의 로컬 여권 · Lv.${level} · ${selectedTitle}`;
    try {
      await navigator.clipboard.writeText(text);
      setToast('공유 문구를 복사했어요');
    } catch {
      setToast('복사가 차단되었어요. 직접 캡처해 주세요.');
    }
    window.setTimeout(() => setToast(''), 1800);
  };

  return (
    <section className="share form-stack" style={{ gap: 18 }}>
      <button type="button" className="back-btn" onClick={() => navigate(`/result/${stamp.id}`)}>
        ← 뒤로
      </button>
      <div className="page-head">
        <span className="page-eyebrow">Shareable Card</span>
        <h1>SNS에 자랑하기</h1>
        <p>스크린샷이나 복사로 친구에게 보내 보세요.</p>
      </div>

      <div className="share-canvas">
        <span className="share-eyebrow">Stampport · 로컬 여권</span>
        <h2>{stamp.place_name}</h2>
        <p className="share-sub">
          {stamp.area} · {categoryLabel(stamp.category)} · {dateLabel}
        </p>
        {stamp.representative_menu ? (
          <p className="share-sub">대표 메뉴 · {stamp.representative_menu}</p>
        ) : null}
        {stamp.tags?.length ? (
          <div className="share-tags">
            {stamp.tags.map((t) => (
              <span key={t} className="tag-pill">#{t}</span>
            ))}
          </div>
        ) : null}

        <div className="share-foot">
          <div>
            <div>{user?.nickname || ''}</div>
            <div>Lv.{level} · {selectedTitle}</div>
          </div>
          <div className="share-stamp" aria-hidden="true">
            <span>STAMP</span>
            <strong>{categoryIcon(stamp.category)}</strong>
            <span>{stamp.area}</span>
          </div>
        </div>
      </div>

      <div className="form-stack">
        <button type="button" className="btn btn-gold btn-block" onClick={copy}>
          공유 문구 복사
        </button>
        <button
          type="button"
          className="btn btn-secondary btn-block"
          onClick={() => navigate('/passport')}
        >
          내 여권 보기
        </button>
      </div>

      {toast ? <div className="toast">{toast}</div> : null}
    </section>
  );
}
