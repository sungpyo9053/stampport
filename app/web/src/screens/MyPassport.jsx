import { useMemo } from 'react';
import { useApp } from '../context/appContext.js';
import { categoryLabel } from '../data/options.js';

function summarize(stamps, key) {
  const map = new Map();
  for (const s of stamps) {
    const k = s[key] || '기타';
    map.set(k, (map.get(k) || 0) + 1);
  }
  return [...map.entries()].sort((a, b) => b[1] - a[1]);
}

export default function MyPassport({ navigate }) {
  const { user, stamps, exp, level, levelInfo, earnedBadges, selectedTitle, quests } = useApp();

  const areaSummary = useMemo(() => summarize(stamps, 'area'), [stamps]);
  const categorySummary = useMemo(
    () =>
      summarize(stamps, 'category').map(([id, n]) => [categoryLabel(id), n]),
    [stamps],
  );
  const recent = stamps.slice(0, 5);
  const activeQuest = quests.find((q) => !q.completed);

  return (
    <section className="form-stack" style={{ gap: 18 }}>
      <div className="passport-summary">
        <span className="ps-tag">My Passport</span>
        <h2>{user?.nickname}님의 로컬 여권</h2>
        <div className="ps-title">현재 칭호 · {selectedTitle}</div>

        <div className="passport-card-identity">
          <div className="pci-avatar">{(user?.nickname || '?').slice(0, 1)}</div>
          <div className="pci-text">
            <div className="pci-title">
              Lv.{level} · {user?.passport_title || '동네 도장 수집가'}
            </div>
            <div className="pci-meta">
              {user?.nickname || '게스트'}님 · 도장 {stamps.length}개 · 다음 레벨까지 {Math.max(0, levelInfo.expForLevel - levelInfo.expIntoLevel)} EXP
            </div>
            <div className="pci-provider">
              {(user?.provider || 'guest').toUpperCase()} 여권
            </div>
          </div>
        </div>

        <div className="ps-stats">
          <div className="ps-stat">
            <div className="label">Stamps</div>
            <div className="value">{stamps.length}</div>
          </div>
          <div className="ps-stat">
            <div className="label">Level</div>
            <div className="value">{level}</div>
          </div>
          <div className="ps-stat">
            <div className="label">Badges</div>
            <div className="value">{earnedBadges.length}</div>
          </div>
        </div>

        <div className="ps-exp">
          <div className="row">
            <span>EXP {exp}</span>
            <span>
              {levelInfo.expIntoLevel} / {levelInfo.expForLevel}
            </span>
          </div>
          <div className="ps-exp-bar">
            <div className="fill" style={{ width: `${Math.round(levelInfo.ratio * 100)}%` }} />
          </div>
        </div>
      </div>

      <button
        type="button"
        className="btn btn-primary btn-block"
        onClick={() => navigate('/stamp')}
      >
        스탬프 찍기
      </button>

      <div className="summary-grid">
        <div className="summary-block">
          <h3>지역별</h3>
          {areaSummary.length ? (
            <ul>
              {areaSummary.map(([area, count]) => (
                <li key={area}>
                  <span>{area}</span>
                  <strong>{count}</strong>
                </li>
              ))}
            </ul>
          ) : (
            <p className="form-helper" style={{ marginTop: 6 }}>
              아직 지역 데이터가 없어요.
            </p>
          )}
        </div>
        <div className="summary-block">
          <h3>카테고리별</h3>
          {categorySummary.length ? (
            <ul>
              {categorySummary.map(([label, count]) => (
                <li key={label}>
                  <span>{label}</span>
                  <strong>{count}</strong>
                </li>
              ))}
            </ul>
          ) : (
            <p className="form-helper" style={{ marginTop: 6 }}>
              스탬프를 한 번 찍어 볼까요?
            </p>
          )}
        </div>
      </div>

      <div className="section-heading">
        <h2>보유 뱃지</h2>
        <button type="button" className="more" onClick={() => navigate('/badges')}>
          전체 보기 →
        </button>
      </div>
      {earnedBadges.length ? (
        <div className="badge-grid">
          {earnedBadges.slice(0, 4).map((b) => (
            <div key={b.id} className="badge-card earned">
              <div className="badge-medal" aria-hidden="true">{b.icon}</div>
              <div className="badge-name">{b.name}</div>
              <div className="badge-desc">{b.description}</div>
            </div>
          ))}
        </div>
      ) : (
        <p className="empty">아직 보유 뱃지가 없어요. 스탬프를 모아 보세요.</p>
      )}

      {activeQuest ? (
        <>
          <div className="section-heading">
            <h2>이번 주 퀘스트</h2>
            <button type="button" className="more" onClick={() => navigate('/quests')}>
              전체 보기 →
            </button>
          </div>
          <div className="quest-card">
            <div className="quest-head">
              <h3>{activeQuest.title}</h3>
              <span className="reward">+{activeQuest.reward_exp} EXP</span>
            </div>
            <p className="quest-desc">{activeQuest.description}</p>
            <div className="progress-bar">
              <div
                className="fill"
                style={{ width: `${Math.round((activeQuest.progress / activeQuest.required) * 100)}%` }}
              />
            </div>
            <div className="progress-line">
              <span>진행</span>
              <span>
                {activeQuest.progress}/{activeQuest.required}
              </span>
            </div>
          </div>
        </>
      ) : null}

      <div className="section-heading">
        <h2>최근 스탬프</h2>
      </div>
      {recent.length ? (
        <div className="stamp-list">
          {recent.map((s) => (
            <button
              key={s.id}
              type="button"
              className="stamp-row"
              onClick={() => navigate(`/result/${s.id}`)}
              style={{ appearance: 'none', textAlign: 'left', cursor: 'pointer' }}
            >
              <div className="pin">{categoryLabel(s.category).slice(0, 1)}</div>
              <div className="info">
                <div className="name">{s.place_name}</div>
                <div className="meta">
                  {s.area} · {categoryLabel(s.category)} · {s.visited_at}
                </div>
              </div>
              <div className="arrow" aria-hidden="true">→</div>
            </button>
          ))}
        </div>
      ) : (
        <p className="empty">아직 스탬프가 없어요. 첫 도장을 찍어 볼까요?</p>
      )}
    </section>
  );
}
