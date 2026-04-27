import { useApp } from '../context/appContext.js';
import { categoryIcon, categoryLabel } from '../data/options.js';
import { levelProgress } from '../utils/leveling.js';

export default function StampResult({ navigate, stampId }) {
  const { stampById, badges, exp } = useApp();
  const stamp = stampById(stampId);

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

  const info = levelProgress(exp);
  const earnedAfter = badges.filter((b) => b.earned).length;
  const inProgressBadges = badges
    .filter((b) => !b.earned && b.progress > 0)
    .slice(0, 3);

  return (
    <section className="result">
      <div className="result-header">
        <span className="eyebrow">Stamp Acquired</span>
        <h1>스탬프를 획득했어요!</h1>
      </div>

      <div className="stamp-card">
        <div className="stamp-meta">
          <span>{stamp.area} · {categoryLabel(stamp.category)}</span>
          <span>{dateLabel}</span>
        </div>
        <div className="place-name">{stamp.place_name}</div>
        {stamp.representative_menu ? (
          <div className="place-sub">대표 메뉴 · {stamp.representative_menu}</div>
        ) : null}
        {stamp.tags?.length ? (
          <div className="tag-row">
            {stamp.tags.map((t) => (
              <span key={t} className="tag-pill">#{t}</span>
            ))}
          </div>
        ) : null}
        <div className="stamp-mark" aria-hidden="true">
          <span>STAMPPORT</span>
          <strong>{categoryIcon(stamp.category)}</strong>
          <span>{stamp.area}</span>
        </div>
      </div>

      <div className="card">
        <div className="card-title">레벨 진행도</div>
        <div className="exp-bar-row">
          <div className="exp-bar">
            <div className="fill" style={{ width: `${Math.round(info.ratio * 100)}%` }} />
          </div>
          <span className="exp-amount">+{stamp.exp_gained} EXP</span>
        </div>
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

      <div className="card">
        <div className="card-title">다음 방문을 위한 킥 포인트 3가지</div>
        <p className="card-sub">취향과 카테고리에 맞춰 자동 추천했어요.</p>
        <div className="kick-list">
          {stamp.kick_points.map((kp, i) => (
            <div key={kp} className="kick-item">
              <span className="num">{i + 1}</span>
              <span>{kp}</span>
            </div>
          ))}
        </div>
      </div>

      {inProgressBadges.length ? (
        <div className="card">
          <div className="card-title">진행 중인 뱃지</div>
          <div className="kick-list">
            {inProgressBadges.map((b) => (
              <div key={b.id} className="kick-item">
                <span className="num" style={{ background: 'var(--color-burgundy)' }}>
                  {b.icon}
                </span>
                <span>
                  <strong>{b.name}</strong> · {b.progress}/{b.required}
                  <br />
                  <span style={{ color: 'var(--color-ink-muted)', fontSize: 12 }}>
                    {b.description}
                  </span>
                </span>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      <div className="form-stack">
        <button
          type="button"
          className="btn btn-gold btn-block"
          onClick={() => navigate(`/share/${stamp.id}`)}
        >
          공유 카드 만들기
        </button>
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
