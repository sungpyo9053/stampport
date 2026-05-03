import { categoryLabel } from '../data/options.js';

export default function KickNoticeCard({ kickPoint, onShare }) {
  if (typeof kickPoint === 'string') {
    return (
      <div className="kick-notice-card kick-notice-legacy">
        <span>{kickPoint}</span>
      </div>
    );
  }
  const ratio = 0;
  return (
    <div className="kick-notice-card">
      <div className="knc-header">
        <span className="knc-eyebrow">📍 다음 도장 예고</span>
        <span className="knc-location">{kickPoint.area} · {categoryLabel(kickPoint.category)}</span>
      </div>
      {kickPoint.badge_hint && (
        <>
          <p className="knc-badge-hint">{kickPoint.badge_hint}</p>
          <div className="knc-bar"><div className="fill" style={{ width: `${ratio * 100}%` }} /></div>
        </>
      )}
      <p className="knc-exp">다음 방문 예상 EXP +{kickPoint.exp_preview}</p>
      <p className="knc-action">{kickPoint.action_label}</p>
      {onShare && (
        <button type="button" className="btn btn-ghost btn-block knc-share-btn" onClick={onShare}>
          이 예고장 공유하기
        </button>
      )}
    </div>
  );
}
