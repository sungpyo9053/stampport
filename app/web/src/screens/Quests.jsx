import { useApp } from '../context/appContext.js';
import { weekRangeLabel } from '../data/quests.js';

export default function Quests({ navigate }) {
  const { quests } = useApp();
  const completed = quests.filter((q) => q.completed).length;

  return (
    <section className="form-stack" style={{ gap: 18 }}>
      <div className="page-head">
        <span className="page-eyebrow">Weekly Quests</span>
        <h1>이번 주 퀘스트</h1>
        <p>
          {weekRangeLabel()} · 완료 {completed}/{quests.length}
        </p>
      </div>

      <div className="form-stack" style={{ gap: 12 }}>
        {quests.map((q) => {
          const ratio = Math.min(q.progress / q.required, 1);
          return (
            <div key={q.id} className={`quest-card ${q.completed ? 'completed' : ''}`}>
              <div className="quest-head">
                <h3>
                  {q.completed ? '✓ ' : ''}
                  {q.title}
                </h3>
                <span className="reward">+{q.reward_exp} EXP</span>
              </div>
              <p className="quest-desc">{q.description}</p>
              <div className="progress-bar">
                <div className="fill" style={{ width: `${Math.round(ratio * 100)}%` }} />
              </div>
              <div className="progress-line">
                <span>{q.completed ? '완료' : '진행 중'}</span>
                <span>
                  {q.progress}/{q.required}
                </span>
              </div>
            </div>
          );
        })}
      </div>

      <button
        type="button"
        className="btn btn-primary btn-block"
        onClick={() => navigate('/stamp')}
      >
        스탬프 찍으러 가기
      </button>
    </section>
  );
}
