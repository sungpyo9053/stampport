import { useMemo, useState } from 'react';
import { useApp } from '../context/appContext.js';
import { categoryIcon, categoryLabel } from '../data/options.js';
import { stampGradeFor, verificationDef } from '../utils/leveling.js';

export default function Share({ navigate, stampId }) {
  const { stampById, user, level, selectedTitle, badges } = useApp();
  const stamp = stampById(stampId);
  const [toast, setToast] = useState('');

  // We pick the badge with the highest progress ratio that involves
  // *this stamp's category or area* — gives the share card a "이 도장
  // 덕분에 X까지 N곳 남음" line that matters.
  const relatedBadge = useMemo(() => {
    if (!stamp) return null;
    const candidates = badges
      .filter((b) => !b.earned)
      .filter((b) => {
        const desc = (b.description || '') + ' ' + (b.name || '');
        return (
          desc.includes(categoryLabel(stamp.category)) ||
          (stamp.area && desc.includes(stamp.area))
        );
      })
      .sort((a, b) => b.progress / b.required - a.progress / a.required);
    return candidates[0] || null;
  }, [badges, stamp]);

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

  const grade = stamp.grade || stampGradeFor(stamp);
  const verification = verificationDef(stamp.verification_level || grade.level || 'manual');

  const copy = async () => {
    const text =
      `[Stampport · 로컬 여권]\n` +
      `${stamp.place_name} (${stamp.area} · ${categoryLabel(stamp.category)})\n` +
      `등급 ${grade.grade} · ${verification.label} · +${stamp.exp_gained} EXP\n` +
      (stamp.experience_note ? `메모: ${stamp.experience_note}\n` : '') +
      (stamp.representative_menu ? `대표 메뉴: ${stamp.representative_menu}\n` : '') +
      (stamp.tags?.length ? `#${stamp.tags.join(' #')}\n` : '') +
      `${user?.nickname || ''} · Lv.${level} · ${selectedTitle}\n` +
      `${dateLabel}`;
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

      <div className="share-canvas" data-grade={grade.grade}>
        <div className="share-top">
          <span className="share-eyebrow">STAMPPORT · 로컬 여권</span>
          <span className="share-grade" style={{ backgroundColor: grade.color }}>
            {grade.grade} · {verification.short}
          </span>
        </div>
        <h2>{stamp.place_name}</h2>
        <p className="share-sub">
          {stamp.area} · {categoryLabel(stamp.category)} · {dateLabel}
        </p>
        {stamp.representative_menu ? (
          <p className="share-sub">대표 메뉴 · {stamp.representative_menu}</p>
        ) : null}
        {stamp.experience_note ? (
          <blockquote className="share-note">
            “{stamp.experience_note}”
          </blockquote>
        ) : null}
        {stamp.tags?.length ? (
          <div className="share-tags">
            {stamp.tags.map((t) => (
              <span key={t} className="tag-pill">#{t}</span>
            ))}
          </div>
        ) : null}

        {relatedBadge ? (
          <div className="share-badge-progress">
            <div className="sbp-line">
              <span aria-hidden="true">{relatedBadge.icon}</span>
              <span>
                <strong>{relatedBadge.name}</strong> · {relatedBadge.progress}/
                {relatedBadge.required}
              </span>
            </div>
            <div className="sbp-bar">
              <div
                className="fill"
                style={{
                  width: `${Math.round((relatedBadge.progress / relatedBadge.required) * 100)}%`,
                }}
              />
            </div>
          </div>
        ) : null}

        <div className="share-foot">
          <div className="share-id">
            <strong>{user?.nickname || '여행자'}</strong>
            <span>Lv.{level} · {selectedTitle}</span>
            <span className="share-exp-line">+{stamp.exp_gained} EXP</span>
          </div>
          <div className="share-stamp" aria-hidden="true">
            <span>STAMP</span>
            <strong>{categoryIcon(stamp.category)}</strong>
            <span>{stamp.area}</span>
          </div>
        </div>
      </div>

      <p className="share-hint">
        이 카드는 미리보기예요. 화면을 길게 눌러 이미지로 저장하거나, 아래 버튼으로
        공유 문구를 복사해 SNS에 붙여 넣어 보세요.
      </p>

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
