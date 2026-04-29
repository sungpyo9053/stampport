import { useMemo } from 'react';
import { useApp } from '../context/appContext.js';
import { categoryLabel } from '../data/options.js';
import { stampGradeFor } from '../utils/leveling.js';
import VisaBadge from '../components/VisaBadge.jsx';

function summarize(stamps, key) {
  const map = new Map();
  for (const s of stamps) {
    const k = s[key] || '기타';
    map.set(k, (map.get(k) || 0) + 1);
  }
  return [...map.entries()].sort((a, b) => b[1] - a[1]);
}

function CharacterAvatar({ user, level }) {
  const initial = (user?.nickname || '여행자').slice(0, 1);
  const provider = (user?.provider || 'guest').toUpperCase();
  return (
    <div className="character-avatar" aria-label={`${user?.nickname || '여행자'} 캐릭터`}>
      <svg viewBox="0 0 96 96" width="96" height="96" aria-hidden="true">
        <circle cx="48" cy="48" r="44" fill="#1f3d2b" />
        <circle cx="48" cy="48" r="44" fill="none" stroke="#c9a23a" strokeWidth="3" />
        <circle cx="48" cy="48" r="34" fill="#fbf6e9" />
        <circle
          cx="48"
          cy="48"
          r="38"
          fill="none"
          stroke="#c9a23a"
          strokeWidth="1"
          strokeDasharray="2 4"
          opacity="0.7"
        />
        <text
          x="48"
          y="56"
          textAnchor="middle"
          fontFamily="Iowan Old Style, Georgia, serif"
          fontSize="28"
          fontWeight="700"
          fill="#1f3d2b"
        >
          {initial}
        </text>
        <g transform="translate(64 64) rotate(-8)">
          <circle cx="0" cy="0" r="14" fill="#6e1f2a" />
          <text
            x="0"
            y="3"
            textAnchor="middle"
            fontFamily="Iowan Old Style, Georgia, serif"
            fontSize="9"
            fontWeight="800"
            fill="#f6efde"
          >
            SP
          </text>
        </g>
      </svg>
      <div className="character-level" title={`Level ${level}`}>
        <span className="lv">Lv</span>
        <strong>{level}</strong>
      </div>
      <div className="character-provider">{provider} 여권</div>
    </div>
  );
}

export default function MyPassport({ navigate }) {
  const {
    user,
    stamps,
    exp,
    level,
    levelInfo,
    earnedBadges,
    selectedTitle,
    quests,
    streakLast7Days,
    nextGoal,
  } = useApp();

  const areaSummary = useMemo(() => summarize(stamps, 'area'), [stamps]);
  const categorySummary = useMemo(
    () =>
      summarize(stamps, 'category').map(([id, n]) => [categoryLabel(id), n]),
    [stamps],
  );
  const recent = stamps.slice(0, 5);
  const activeQuest = quests.find((q) => !q.completed);
  const visaList = earnedBadges.filter((b) => b.id.endsWith('_visa') || b.id === 'gwanak_explorer');

  // This-week stamp count — drives the "이번 주 N개의 도장을 찍었어요"
  // line on the character header.
  const stampsThisWeek = useMemo(() => {
    const now = new Date();
    const day = now.getDay();
    const diff = (day + 6) % 7;
    const weekStart = new Date(now);
    weekStart.setHours(0, 0, 0, 0);
    weekStart.setDate(weekStart.getDate() - diff);
    return stamps.filter((s) => {
      if (!s.visited_at) return false;
      const d = new Date(s.visited_at);
      return d >= weekStart;
    }).length;
  }, [stamps]);

  const gradeDistribution = useMemo(() => {
    const counts = { S: 0, A: 0, B: 0, C: 0 };
    for (const s of stamps) {
      const g = (s.grade && s.grade.grade) || stampGradeFor(s).grade;
      if (counts[g] != null) counts[g] += 1;
    }
    return counts;
  }, [stamps]);

  return (
    <section className="form-stack" style={{ gap: 18 }}>
      <div className="passport-summary character-summary">
        <span className="ps-tag">My Passport</span>

        <div className="character-card">
          <CharacterAvatar user={user} level={level} />
          <div className="character-meta">
            <h2>{user?.nickname || '게스트'} 님의 여권</h2>
            <div className="character-title">
              <span className="ct-tag">현재 칭호</span>
              <strong>{selectedTitle}</strong>
            </div>
            <div className="character-stats">
              <span>도장 {stamps.length}</span>
              <span>·</span>
              <span>뱃지 {earnedBadges.length}</span>
              <span>·</span>
              <span>최근 7일 {streakLast7Days}일 방문</span>
            </div>
            <div className="character-week-line">
              이번 주 <strong>{stampsThisWeek}</strong>개의 도장을 찍었어요.
            </div>
            <div className="grade-strip">
              {['S', 'A', 'B', 'C'].map((g) => (
                <span key={g} className={`gp gp-${g}`}>
                  <strong>{g}</strong>
                  <em>{gradeDistribution[g]}</em>
                </span>
              ))}
            </div>
          </div>
        </div>

        <div className="ps-exp ps-exp-large">
          <div className="row">
            <span>
              Lv.{level} → Lv.{level + 1}
            </span>
            <span>
              {levelInfo.expIntoLevel} / {levelInfo.expForLevel} EXP
            </span>
          </div>
          <div className="ps-exp-bar">
            <div
              className="fill"
              style={{ width: `${Math.round(levelInfo.ratio * 100)}%` }}
            />
          </div>
          <div className="next-level-hint">
            다음 레벨까지 <strong>{levelInfo.expToNext}</strong> EXP — 도장 1~2개면 도착해요.
          </div>
        </div>
      </div>

      {visaList.length > 0 && (
        <div className="visa-strip">
          <span className="visa-strip-label">획득한 비자</span>
          <div className="visa-strip-row">
            {visaList.map((b) => <VisaBadge key={b.id} badge={b} />)}
          </div>
        </div>
      )}

      <button
        type="button"
        className="btn btn-primary btn-block"
        onClick={() => navigate('/stamp')}
      >
        스탬프 찍기
      </button>

      {nextGoal ? (
        <div className="next-goal-card">
          <div className="ngc-head">
            <span className="ngc-eyebrow">다음 목표</span>
            <span className="ngc-icon" aria-hidden="true">{nextGoal.icon}</span>
          </div>
          <div className="ngc-name">{nextGoal.name}</div>
          <div className="ngc-desc">{nextGoal.description}</div>
          <div className="ngc-bar">
            <div
              className="fill"
              style={{
                width: `${Math.round((nextGoal.progress / nextGoal.required) * 100)}%`,
              }}
            />
          </div>
          <div className="ngc-num">
            {nextGoal.progress > 0
              ? `${nextGoal.required - nextGoal.progress}곳만 더 다녀오면 ‘${nextGoal.titleLabel || nextGoal.name}’ 비자.`
              : `다음 도장이 ‘${nextGoal.titleLabel || nextGoal.name}’의 출발점이에요.`}
          </div>
        </div>
      ) : null}

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
            {activeQuest.next_hint ? (
              <div className="quest-hint">{activeQuest.next_hint}</div>
            ) : null}
          </div>
        </>
      ) : null}

      <div className="section-heading">
        <h2>최근 스탬프</h2>
      </div>
      {recent.length ? (
        <div className="stamp-list">
          {recent.map((s) => {
            const g = (s.grade && s.grade.grade) || stampGradeFor(s).grade;
            const gColor =
              (s.grade && s.grade.color) || stampGradeFor(s).color;
            return (
              <button
                key={s.id}
                type="button"
                className="stamp-row"
                onClick={() => navigate(`/result/${s.id}`)}
                style={{ appearance: 'none', textAlign: 'left', cursor: 'pointer' }}
              >
                <div
                  className="pin grade-pin"
                  style={{ backgroundColor: gColor }}
                  aria-label={`${g}등급`}
                >
                  {g}
                </div>
                <div className="info">
                  <div className="name">{s.place_name}</div>
                  <div className="meta">
                    {s.area} · {categoryLabel(s.category)} · {s.visited_at}
                  </div>
                  {s.experience_note ? (
                    <div className="note-snippet">“{s.experience_note}”</div>
                  ) : null}
                </div>
                <div className="arrow" aria-hidden="true">→</div>
              </button>
            );
          })}
        </div>
      ) : (
        <p className="empty">아직 스탬프가 없어요. 첫 도장을 찍어 볼까요?</p>
      )}
    </section>
  );
}
