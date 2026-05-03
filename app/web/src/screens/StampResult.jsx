import { useEffect, useMemo, useState } from 'react';
import { useApp } from '../context/appContext.js';
import { categoryIcon, categoryLabel, visitPurposeLabel } from '../data/options.js';
import { levelProgress, stampGradeFor, verificationDef } from '../utils/leveling.js';
import KickNoticeCard from '../components/KickNoticeCard.jsx';

// Animate the EXP bar from 0% → ratio% on first paint so the player
// *sees* the gain settle in. Pure visual — totals are already final.
function useExpRise(ratio) {
  const [shown, setShown] = useState(0);
  useEffect(() => {
    let raf;
    const start = performance.now();
    const duration = 900;
    const tick = (now) => {
      const elapsed = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - elapsed, 3);
      setShown(eased * ratio);
      if (elapsed < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [ratio]);
  return shown;
}

function loadNewBadgesFor(stampId) {
  if (typeof window === 'undefined') return [];
  try {
    const raw = window.sessionStorage.getItem(`stampport:newBadges:${stampId}`);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export default function StampResult({ navigate, stampId }) {
  const { stampById, badges, exp, level, selectedTitle, user } = useApp();
  const stamp = stampById(stampId);

  // Read newly-earned-badges *once*, regardless of whether the stamp
  // exists yet — must be a top-level hook call.
  const newBadges = useMemo(() => loadNewBadgesFor(stampId), [stampId]);

  const info = useMemo(() => levelProgress(exp), [exp]);
  const animatedRatio = useExpRise(info.ratio);

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

  const earnedAfter = badges.filter((b) => b.earned).length;
  const inProgressBadges = badges
    .filter((b) => !b.earned && b.progress > 0)
    .sort((a, b) => b.progress / b.required - a.progress / a.required)
    .slice(0, 3);

  const grade = stamp.grade || stampGradeFor(stamp);
  const verification = verificationDef(stamp.verification_level || grade.level || 'manual');
  const breakdown = stamp.exp_breakdown || [];

  const purposeLabel = visitPurposeLabel(stamp.visit_purpose);

  return (
    <section className="result">
      <div className="result-header">
        <span className="eyebrow">Stamp Acquired · {grade.grade}등급</span>
        <h1>{verification.label} 획득!</h1>
        <p className="result-tagline">오늘의 도장이 여권에 기록되었습니다.</p>
      </div>

      <div
        className={`stamp-card stamp-card-press grade-${grade.grade}`}
        data-grade={grade.grade}
      >
        <div className="grade-ribbon" style={{ backgroundColor: grade.color }}>
          <span>{grade.grade}</span>
          <strong>{verification.short}</strong>
        </div>
        <div className="stamp-meta">
          <span>{stamp.area} · {categoryLabel(stamp.category)}</span>
          <span>{dateLabel}</span>
        </div>
        <div className="place-name">{stamp.place_name}</div>
        {stamp.representative_menu ? (
          <div className="place-sub">대표 메뉴 · {stamp.representative_menu}</div>
        ) : purposeLabel ? (
          <div className="place-sub">방문 목적 · {purposeLabel}</div>
        ) : null}

        {stamp.photo_data_url ? (
          <img
            className="stamp-photo"
            src={stamp.photo_data_url}
            alt={`${stamp.place_name} 방문 사진`}
          />
        ) : null}

        {stamp.experience_note ? (
          <blockquote className="stamp-note">
            “{stamp.experience_note}”
          </blockquote>
        ) : null}

        <div className="stamp-proofs">
          {stamp.location_label ? (
            <span className="proof-pill">📍 {stamp.location_label}</span>
          ) : null}
          {stamp.visit_mood ? (
            <span className="proof-pill">✨ {moodLabel(stamp.visit_mood)}</span>
          ) : null}
          <span className="proof-pill">🏷 +{stamp.exp_gained} EXP</span>
        </div>

        {stamp.tags?.length ? (
          <div className="tag-row">
            {stamp.tags.map((t) => (
              <span key={t} className="tag-pill">#{t}</span>
            ))}
          </div>
        ) : null}
        <div className="stamp-mark stamp-mark-pressed" aria-hidden="true">
          <span>STAMPPORT</span>
          <strong>{categoryIcon(stamp.category)}</strong>
          <span>{stamp.area}</span>
        </div>
      </div>

      <div className="card">
        <div className="card-title">레벨 진행도</div>
        <div className="exp-bar-row">
          <div className="exp-bar">
            <div className="fill" style={{ width: `${Math.round(animatedRatio * 100)}%` }} />
          </div>
          <span className="exp-amount exp-amount-rise">+{stamp.exp_gained} EXP</span>
        </div>
        <div className="next-level-line">
          다음 레벨까지{' '}
          <strong>{info.expToNext}</strong> EXP — Lv.{info.level + 1}{' '}
          {info.expToNext < 30 ? '코앞이에요.' : '가까워지고 있어요.'}
        </div>

        {breakdown.length ? (
          <ul className="exp-breakdown">
            {breakdown.map((it) => (
              <li key={it.key}>
                <span>{it.label}</span>
                <strong>+{it.exp}</strong>
              </li>
            ))}
          </ul>
        ) : null}

        <div className="gain-row" style={{ marginTop: 14 }}>
          <div className="gain">
            <div className="label">레벨</div>
            <div className="value">Lv. {info.level}</div>
          </div>
          <div className="gain">
            <div className="label">누적 EXP</div>
            <div className="value">{exp}</div>
          </div>
          <div className="gain">
            <div className="label">획득 뱃지</div>
            <div className="value">{earnedAfter}</div>
          </div>
        </div>
      </div>

      {newBadges.length ? (
        <div className="card card-celebrate">
          <div className="card-title">🎉 방금 새로 받은 뱃지</div>
          <p className="card-sub">여권에 새 비자가 추가됐어요.</p>
          <div className="badge-grid badge-grid-new">
            {newBadges.map((b) => (
              <div key={b.id} className="badge-card earned">
                <div className="badge-medal" aria-hidden="true">{b.icon}</div>
                <div className="badge-name">{b.name}</div>
                <div className="badge-desc">{b.description}</div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {inProgressBadges.length ? (
        <div className="card">
          <div className="card-title">진행 중인 뱃지</div>
          <p className="card-sub">조금만 더 다녀오면 새 비자를 발급받아요.</p>
          <div className="kick-list">
            {inProgressBadges.map((b) => {
              const ratio = Math.min(b.progress / b.required, 1);
              return (
                <div key={b.id} className="badge-progress-row">
                  <div className="bpr-medal" aria-hidden="true">{b.icon}</div>
                  <div className="bpr-text">
                    <div className="bpr-name">{b.name}</div>
                    <div className="bpr-desc">{b.description}</div>
                    <div className="bpr-bar">
                      <div className="fill" style={{ width: `${Math.round(ratio * 100)}%` }} />
                    </div>
                    <div className="bpr-num">
                      진행 {b.progress}/{b.required} — {b.required - b.progress}곳 남음
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

      <div className="card">
        <div className="card-title">공유 카드 미리보기</div>
        <p className="card-sub">이 도장을 공유 카드로 남겨보세요.</p>
        <SharePreview
          stamp={stamp}
          grade={grade}
          verification={verification}
          user={user}
          level={level}
          title={selectedTitle}
        />
        <button
          type="button"
          className="btn btn-gold btn-block"
          onClick={() => navigate(`/share/${stamp.id}`)}
          style={{ marginTop: 12 }}
        >
          공유 카드 만들기
        </button>
      </div>

      <div className="card">
        <div className="card-title">다음 도장 예고장</div>
        <p className="card-sub">취향과 뱃지 진행 현황을 반영했어요.</p>
        {stamp.kick_points.slice(0, 1).map((kp, i) => (
          <KickNoticeCard
            key={i}
            kickPoint={kp}
            onShare={() => navigate(`/share/${stamp.id}?preview=notice`)}
          />
        ))}
      </div>

      <div className="form-stack">
        <button
          type="button"
          className="btn btn-secondary btn-block"
          onClick={() => navigate('/passport')}
        >
          내 여권 보기
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-block"
          onClick={() => navigate('/stamp')}
        >
          한 곳 더 찍기
        </button>
      </div>
    </section>
  );
}

function SharePreview({ stamp, grade, verification, user, level, title }) {
  return (
    <div className="share-mini" data-grade={grade.grade}>
      <div className="sm-top">
        <span className="sm-eyebrow">STAMPPORT · 로컬 여권</span>
        <span className="sm-grade" style={{ backgroundColor: grade.color }}>
          {grade.grade} · {verification.short}
        </span>
      </div>
      <h3>{stamp.place_name}</h3>
      <div className="sm-meta">
        {stamp.area} · {categoryLabel(stamp.category)}
      </div>
      {stamp.experience_note ? (
        <p className="sm-note">“{stamp.experience_note}”</p>
      ) : null}
      <div className="sm-bottom">
        <div className="sm-id">
          <strong>{user?.nickname || '여행자'}</strong>
          <span>Lv.{level} · {title}</span>
        </div>
        <div className="sm-exp">+{stamp.exp_gained} EXP</div>
      </div>
    </div>
  );
}

const MOOD_LABEL = {
  cozy: '아늑함',
  memorable: '기억에 남음',
  with_someone: '누구랑 또',
  alone: '혼자 좋음',
  value: '가성비 좋음',
  dessert: '디저트 천국',
  discovery: '새 발견',
};

function moodLabel(id) {
  return MOOD_LABEL[id] || id;
}
