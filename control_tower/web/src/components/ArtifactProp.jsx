// Small SVG props that visually represent each role's deliverable.
// Used both on the desk in `done` state and in the courier's hands during
// a handoff.

const ARTIFACT_LABEL = {
  brief: "제품 브리프",
  schedule: "로드맵",
  diagram: "AI 실행 설계",
  wireframe: "와이어프레임",
  apispec: "API 명세",
  uimock: "프론트엔드 골격",
  checklist: "테스트 케이스",
  copy: "런칭 카피",
};

function Brief() {
  return (
    <g>
      <rect x="8" y="6" width="32" height="38" rx="2" fill="#f8fafc" stroke="#0f172a" strokeWidth="1" />
      <rect x="11" y="9" width="26" height="5" fill="#1d4ed8" />
      <line x1="11" y1="20" x2="37" y2="20" stroke="#475569" strokeWidth="1" />
      <line x1="11" y1="25" x2="34" y2="25" stroke="#475569" strokeWidth="1" />
      <line x1="11" y1="30" x2="36" y2="30" stroke="#475569" strokeWidth="1" />
      <line x1="11" y1="35" x2="30" y2="35" stroke="#475569" strokeWidth="1" />
    </g>
  );
}

function Schedule() {
  return (
    <g>
      <rect x="6" y="8" width="36" height="34" rx="2" fill="#fef3c7" stroke="#a16207" strokeWidth="1" />
      <rect x="6" y="8" width="36" height="6" fill="#a16207" />
      {[0, 1, 2, 3].map((i) => (
        <line key={`v${i}`} x1={6 + (i + 1) * 7.2} y1={14} x2={6 + (i + 1) * 7.2} y2={42} stroke="#a16207" strokeWidth="0.6" />
      ))}
      {[0, 1, 2].map((i) => (
        <line key={`h${i}`} x1={6} y1={20 + i * 7} x2={42} y2={20 + i * 7} stroke="#a16207" strokeWidth="0.6" />
      ))}
      <rect x="9" y="22" width="13" height="5" fill="#7c3aed" opacity="0.85" />
      <rect x="24" y="29" width="14" height="5" fill="#0ea5e9" opacity="0.85" />
    </g>
  );
}

function Diagram() {
  return (
    <g>
      <rect x="6" y="6" width="36" height="38" rx="3" fill="#f1f5f9" stroke="#0f172a" strokeWidth="1" />
      <circle cx="14" cy="16" r="4" fill="#be185d" />
      <circle cx="34" cy="16" r="4" fill="#0ea5e9" />
      <circle cx="24" cy="34" r="4" fill="#10b981" />
      <line x1="14" y1="16" x2="24" y2="34" stroke="#475569" strokeWidth="1" />
      <line x1="34" y1="16" x2="24" y2="34" stroke="#475569" strokeWidth="1" />
      <line x1="14" y1="16" x2="34" y2="16" stroke="#475569" strokeWidth="1" />
    </g>
  );
}

function Wireframe() {
  return (
    <g>
      <rect x="6" y="8" width="36" height="34" rx="2" fill="#fff" stroke="#0f172a" strokeWidth="1" />
      <rect x="9" y="11" width="30" height="4" fill="#cbd5e1" />
      <rect x="9" y="18" width="14" height="14" fill="#e2e8f0" stroke="#94a3b8" strokeWidth="0.5" />
      <rect x="25" y="18" width="14" height="6" fill="#e2e8f0" stroke="#94a3b8" strokeWidth="0.5" />
      <rect x="25" y="26" width="14" height="6" fill="#e2e8f0" stroke="#94a3b8" strokeWidth="0.5" />
      <rect x="9" y="34" width="30" height="6" fill="#fb7185" opacity="0.7" />
    </g>
  );
}

function ApiSpec() {
  return (
    <g>
      <rect x="6" y="6" width="36" height="38" rx="3" fill="#0f172a" stroke="#475569" strokeWidth="1" />
      <text x="10" y="16" fill="#34d399" fontSize="6" fontFamily="ui-monospace, Menlo, monospace">GET /api</text>
      <text x="10" y="24" fill="#facc15" fontSize="6" fontFamily="ui-monospace, Menlo, monospace">POST /run</text>
      <text x="10" y="32" fill="#38bdf8" fontSize="6" fontFamily="ui-monospace, Menlo, monospace">PUT /task</text>
      <rect x="10" y="36" width="28" height="4" fill="#334155" />
    </g>
  );
}

function UiMock() {
  return (
    <g>
      <rect x="6" y="6" width="36" height="38" rx="3" fill="#0f172a" stroke="#facc15" strokeWidth="1" />
      <rect x="9" y="9" width="30" height="6" fill="#facc15" />
      <rect x="9" y="17" width="13" height="13" rx="2" fill="#1e293b" stroke="#facc15" strokeWidth="0.6" />
      <rect x="24" y="17" width="15" height="6" fill="#1e293b" stroke="#facc15" strokeWidth="0.6" />
      <rect x="24" y="25" width="15" height="5" fill="#1e293b" stroke="#facc15" strokeWidth="0.6" />
      <rect x="9" y="33" width="30" height="7" rx="2" fill="#facc15" />
    </g>
  );
}

function Checklist() {
  return (
    <g>
      <rect x="6" y="6" width="36" height="38" rx="3" fill="#fff7ed" stroke="#9a3412" strokeWidth="1" />
      {[0, 1, 2, 3].map((i) => (
        <g key={i}>
          <rect x="10" y={13 + i * 7} width="5" height="5" rx="1" fill="#fff" stroke="#9a3412" />
          {i < 2 && (
            <path
              d={`M 11 ${15.5 + i * 7} L 13 ${17 + i * 7} L 15 ${14.5 + i * 7}`}
              stroke="#16a34a"
              strokeWidth="1.2"
              fill="none"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          )}
          <line x1="18" y1={16 + i * 7} x2={18 + 16 - i * 2} y2={16 + i * 7} stroke="#9a3412" strokeWidth="1" />
        </g>
      ))}
    </g>
  );
}

function Copy() {
  return (
    <g>
      <rect x="6" y="6" width="36" height="38" rx="3" fill="#ecfeff" stroke="#0e7490" strokeWidth="1" />
      <text
        x="24"
        y="22"
        textAnchor="middle"
        fill="#0e7490"
        fontSize="9"
        fontWeight="700"
        fontFamily="ui-monospace, Menlo, monospace"
      >
        LAUNCH
      </text>
      <line x1="10" y1="27" x2="38" y2="27" stroke="#0e7490" strokeWidth="0.6" />
      <line x1="10" y1="32" x2="34" y2="32" stroke="#0e7490" strokeWidth="0.6" />
      <line x1="10" y1="37" x2="36" y2="37" stroke="#0e7490" strokeWidth="0.6" />
    </g>
  );
}

const PROP_BY_TYPE = {
  brief: Brief,
  schedule: Schedule,
  diagram: Diagram,
  wireframe: Wireframe,
  apispec: ApiSpec,
  uimock: UiMock,
  checklist: Checklist,
  copy: Copy,
};

export default function ArtifactProp({ type = "brief", size = 48, withLabel = false }) {
  const Comp = PROP_BY_TYPE[type] || Brief;
  return (
    <div
      style={{
        width: size,
        height: withLabel ? size + 12 : size,
        position: "relative",
        pointerEvents: "none",
      }}
    >
      <svg
        viewBox="0 0 48 50"
        width={size}
        height={size}
        style={{ overflow: "visible" }}
      >
        <Comp />
      </svg>
      {withLabel && (
        <div
          style={{
            position: "absolute",
            left: "50%",
            bottom: -2,
            transform: "translateX(-50%)",
            fontSize: 9,
            color: "#e2e8f0",
            background: "rgba(15,23,42,0.85)",
            padding: "1px 6px",
            borderRadius: 4,
            whiteSpace: "nowrap",
            fontFamily: "ui-monospace, Menlo, monospace",
            letterSpacing: "0.5px",
          }}
        >
          {ARTIFACT_LABEL[type] || type}
        </div>
      )}
    </div>
  );
}

export { ARTIFACT_LABEL };
