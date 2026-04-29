export default function VisaBadge({ badge }) {
  return (
    <div className="visa-badge" aria-label={`${badge.name} 획득`}>
      <svg viewBox="0 0 80 56" width="80" height="56" aria-hidden="true">
        <rect x="2" y="2" width="76" height="52" rx="6"
          fill="none" stroke="#c9a23a" strokeWidth="1.5" strokeDasharray="4 2" />
        <rect x="6" y="6" width="68" height="44" rx="4" fill="#1f3d2b" opacity="0.08" />
        <text x="40" y="26" textAnchor="middle" fontSize="18">{badge.icon}</text>
        <text x="40" y="42" textAnchor="middle"
          fontFamily="Iowan Old Style, Georgia, serif"
          fontSize="7" fontWeight="700" fill="#1f3d2b" letterSpacing="0.5">
          {badge.titleLabel || badge.name}
        </text>
        <text x="40" y="14" textAnchor="middle"
          fontFamily="Iowan Old Style, Georgia, serif"
          fontSize="6" fill="#c9a23a" letterSpacing="2">
          VISA
        </text>
      </svg>
    </div>
  );
}
