import { OFFICE_WIDTH, OFFICE_HEIGHT } from "../constants/agents.js";

// Office room background only. Desks/chairs/monitors live with each
// Workstation so we can layer them properly with the human characters.
// This layer renders behind everything.
export default function OfficeBackground() {
  return (
    <svg
      viewBox={`0 0 ${OFFICE_WIDTH} ${OFFICE_HEIGHT}`}
      className="absolute inset-0 h-full w-full"
      preserveAspectRatio="xMidYMid meet"
    >
      <defs>
        <pattern id="floor-grid" width="40" height="40" patternUnits="userSpaceOnUse">
          <path
            d="M 40 0 L 0 0 0 40"
            fill="none"
            stroke="rgba(148,163,184,0.18)"
            strokeWidth="1"
          />
        </pattern>
        <linearGradient id="floor-grad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#22324d" />
          <stop offset="100%" stopColor="#131c30" />
        </linearGradient>
        <linearGradient id="wall-grad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#2c3e62" />
          <stop offset="100%" stopColor="#1a263e" />
        </linearGradient>
        <linearGradient id="window-grad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#0ea5e9" stopOpacity="0.55" />
          <stop offset="100%" stopColor="#1e293b" stopOpacity="0.4" />
        </linearGradient>
      </defs>

      {/* wall */}
      <rect x="0" y="0" width={OFFICE_WIDTH} height="80" fill="url(#wall-grad)" />
      {/* floor */}
      <rect x="0" y="80" width={OFFICE_WIDTH} height={OFFICE_HEIGHT - 80} fill="url(#floor-grad)" />
      <rect x="0" y="80" width={OFFICE_WIDTH} height={OFFICE_HEIGHT - 80} fill="url(#floor-grid)" />

      {/* baseboard */}
      <rect x="0" y="78" width={OFFICE_WIDTH} height="3" fill="#0f172a" opacity="0.6" />

      {/* window panels in the wall */}
      <g>
        <rect x="40" y="14" width="160" height="50" rx="3" fill="url(#window-grad)" stroke="#475569" />
        <line x1="120" y1="14" x2="120" y2="64" stroke="#475569" />
        <rect x="560" y="14" width="160" height="50" rx="3" fill="url(#window-grad)" stroke="#475569" />
        <line x1="640" y1="14" x2="640" y2="64" stroke="#475569" />
      </g>

      {/* whiteboard at the top center */}
      <g transform="translate(250, 14)">
        <rect width="260" height="54" rx="6" fill="#0f1a2e" stroke="#475569" strokeWidth="1.5" />
        <rect x="6" y="6" width="248" height="42" rx="4" fill="#0b1626" stroke="#1e293b" />
        <text
          x="130"
          y="32"
          textAnchor="middle"
          fontSize="14"
          fontWeight="600"
          fill="#7dd3fc"
          letterSpacing="1"
        >
          스프린트 · Stampport MVP
        </text>
        <text
          x="130"
          y="44"
          textAnchor="middle"
          fontSize="9"
          fill="#94a3b8"
          letterSpacing="0.5"
        >
          PM → 기획자 → 디자이너 → FE → BE → AI → QA → 배포
        </text>
        {/* pen tray */}
        <rect x="105" y="50" width="50" height="3" rx="1.5" fill="#475569" />
      </g>

      {/* meeting / standup table tucked into the empty bottom-left area */}
      <g transform="translate(40, 430)">
        <ellipse cx="80" cy="64" rx="92" ry="44" fill="#1c2740" stroke="#475569" strokeWidth="1.5" />
        <ellipse cx="80" cy="58" rx="76" ry="32" fill="#26344f" />
        <text
          x="80"
          y="62"
          textAnchor="middle"
          fontSize="11"
          fill="#94a3b8"
          letterSpacing="0.5"
        >
          스탠드업
        </text>
        {/* chair dots */}
        <circle cx="22" cy="58" r="5" fill="#334155" />
        <circle cx="138" cy="58" r="5" fill="#334155" />
        <circle cx="80" cy="24" r="5" fill="#334155" />
        <circle cx="80" cy="92" r="5" fill="#334155" />
      </g>

      {/* coffee station at the bottom-left corner */}
      <g transform="translate(20, 562)">
        <rect width="92" height="44" rx="6" fill="#1c2740" stroke="#475569" strokeWidth="1.5" />
        <text x="46" y="29" textAnchor="middle" fontSize="20">☕</text>
      </g>

      {/* a plant near the entrance */}
      <g transform="translate(720, 575)">
        <ellipse cx="0" cy="14" rx="14" ry="3" fill="rgba(0,0,0,0.4)" />
        <rect x="-12" y="10" width="24" height="20" rx="3" fill="#1c2740" stroke="#475569" />
        <path d="M -8 10 Q -10 -8 0 -4 Q 10 -8 8 10 Z" fill="#15803d" />
        <path d="M -4 6 Q -6 -2 0 0 Q 6 -2 4 6 Z" fill="#16a34a" />
      </g>

      {/* small filing cabinet near right wall */}
      <g transform="translate(680, 110)">
        <rect width="50" height="80" rx="3" fill="#1c2740" stroke="#475569" strokeWidth="1.5" />
        <line x1="0" y1="28" x2="50" y2="28" stroke="#475569" />
        <line x1="0" y1="54" x2="50" y2="54" stroke="#475569" />
        <circle cx="25" cy="14" r="1.5" fill="#94a3b8" />
        <circle cx="25" cy="40" r="1.5" fill="#94a3b8" />
        <circle cx="25" cy="66" r="1.5" fill="#94a3b8" />
      </g>

      {/* a tiny clock on the wall */}
      <g transform="translate(380, 38)">
        <circle r="14" fill="#0b1626" stroke="#475569" />
        <line x1="0" y1="0" x2="0" y2="-8" stroke="#7dd3fc" strokeWidth="1.5" strokeLinecap="round" />
        <line x1="0" y1="0" x2="6" y2="2" stroke="#7dd3fc" strokeWidth="1" strokeLinecap="round" />
        <circle r="1" fill="#7dd3fc" />
      </g>
    </svg>
  );
}
