// "이번 배포 안내" — Release Preview panel.
//
// Shown directly above the DeployInfoStrip so the operator sees a
// human-readable summary of WHAT this push is going to change before
// they hit 배포하기. Reads runner heartbeat metadata
// (local_factory.publish / qa_gate / publish_blocker) and turns the
// changed-files list into per-area summaries via a small fallback
// table — no LLM, no external deps. The panel is read-only and never
// mutates deploy state, so it can't break the existing button.

const MAX_FILES_VISIBLE = 10;

// Each rule maps a path predicate onto the user-facing copy block
// for that area. A single deploy can match multiple rules — e.g.
// touching Login.jsx + control_tower/web both light up. We dedupe
// across rules so the operator doesn't see the same scenario twice.
const FALLBACK_RULES = [
  {
    id: "auth",
    match: (p) =>
      p === "app/web/src/screens/Login.jsx" ||
      p === "app/web/src/screens/AuthCallback.jsx" ||
      /\/oauth\.[jt]sx?$/.test(p) ||
      /app\/web\/src\/(api|stores)\/auth/.test(p),
    summary: "로그인/계정 흐름이 변경됩니다.",
    screens: ["로그인 화면", "OAuth callback", "내 여권 프로필"],
    scenarios: [
      "/stampport/ 접속",
      "로그인 화면 확인",
      "카카오/네이버 시작 버튼 확인",
      "게스트 모드 확인",
      "로그인 후 내 여권 화면에서 캐릭터/레벨/여권 정보 확인",
    ],
    expected: [
      "사용자가 이어서 플레이할 수 있는 계정 구조가 준비됨",
      "social login mock fallback이 동작함",
    ],
    risks: [
      "실제 Kakao/Naver OAuth는 Client ID/Redirect URI 설정 전까지 mock fallback임",
    ],
  },
  {
    id: "stamp_form",
    match: (p) => p === "app/web/src/screens/StampForm.jsx",
    summary: "스탬프 생성 화면이 변경됩니다.",
    screens: ["스탬프 생성"],
    scenarios: [
      "스탬프 탭 이동",
      "가게 이름만 입력했을 때 버튼 상태 확인",
      "지역/카테고리/대표 메뉴/메모/태그 입력 흐름 확인",
    ],
    expected: ["도장 찍기 조건이 명확히 표시됨"],
    risks: [],
  },
  {
    id: "stamp_result",
    match: (p) => p === "app/web/src/screens/StampResult.jsx",
    summary: "스탬프 결과 화면이 변경됩니다.",
    screens: ["스탬프 결과"],
    scenarios: [
      "스탬프 생성",
      "결과 화면 이동",
      "EXP, 배지, 킥포인트, 공유 카드 확인",
    ],
    expected: [],
    risks: [],
  },
  {
    id: "passport",
    match: (p) => p === "app/web/src/screens/MyPassport.jsx",
    summary: "내 여권 화면이 변경됩니다.",
    screens: ["내 여권"],
    scenarios: [
      "여권 탭 이동",
      "캐릭터/레벨/EXP/배지/최근 스탬프 확인",
    ],
    expected: [],
    risks: [],
  },
  {
    id: "control_tower_web",
    match: (p) => p.startsWith("control_tower/web/"),
    summary: "관제실 UI가 변경됩니다.",
    screens: ["Control Tower"],
    scenarios: [
      "/stampport-control/ 접속",
      "배포 버튼 상태 확인",
      "System Log 확인",
      "Claude 작업 지시 패널 확인",
      "이번 배포 안내 패널 확인",
    ],
    expected: [],
    risks: [],
  },
  {
    id: "local_runner",
    match: (p) => p.startsWith("control_tower/local_runner/"),
    summary: "로컬 러너 동작이 변경됩니다.",
    screens: ["관제실 러너 상태/명령 처리"],
    scenarios: [
      "맥북 runner 재시작",
      "runner online 확인",
      "테스트/빌드/배포 명령 수신 확인",
    ],
    expected: [],
    risks: [],
  },
  {
    id: "control_tower_api",
    match: (p) => p.startsWith("control_tower/api/"),
    summary: "Control Tower API가 변경됩니다.",
    screens: [],
    scenarios: [
      "/stampport-control-api/health 확인",
      "/runners/ 응답 확인",
      "관제실 API 오류 없는지 확인",
    ],
    expected: [],
    risks: [],
  },
  {
    id: "agent_rules",
    match: (p) =>
      p.startsWith("docs/") || p.startsWith("config/domain_profiles/"),
    summary: "에이전트/제품 규칙이 변경됩니다.",
    screens: [],
    scenarios: [
      "다음 factory cycle에서 기획자/디자이너 산출물 확인",
      "Ping-Pong Board와 Desire Score 확인",
    ],
    expected: [],
    risks: [],
  },
];

function classifyChanges(files) {
  const matched = [];
  const seen = new Set();
  for (const rule of FALLBACK_RULES) {
    if (files.some((p) => rule.match(p))) {
      matched.push(rule);
      seen.add(rule.id);
    }
  }
  return matched;
}

function dedupePush(target, items) {
  for (const item of items || []) {
    if (item && !target.includes(item)) target.push(item);
  }
}

function buildPreview(files) {
  const matched = classifyChanges(files);
  const summaries = [];
  const screens = [];
  const scenarios = [];
  const expected = [];
  const risks = [];
  for (const rule of matched) {
    if (rule.summary) summaries.push(rule.summary);
    dedupePush(screens, rule.screens);
    dedupePush(scenarios, rule.scenarios);
    dedupePush(expected, rule.expected);
    dedupePush(risks, rule.risks);
  }
  // Fall back to a generic "기타 변경" when nothing matched but there
  // ARE changed files — better than rendering an empty preview.
  if (matched.length === 0 && files.length > 0) {
    summaries.push("이번 배포에 영향 받는 영역을 자동으로 분류하지 못했습니다.");
    scenarios.push("변경 파일 목록을 보고 직접 영향 범위를 점검하세요.");
  }
  return { summaries, screens, scenarios, expected, risks };
}

// ---------------------------------------------------------------------------
// Status banner — picks the single most important thing to surface at
// the top so a glance tells the operator "are we go / no-go".
// ---------------------------------------------------------------------------

function pickStatusBanner({ deployState }) {
  const { publish = {}, qa = {}, blocker = {}, dryRun, changedCount } =
    deployState || {};
  const lastPushStatus = publish.last_push_status;

  if (changedCount === 0) {
    return {
      tone: "muted",
      title: "배포할 변경사항이 없습니다.",
      detail: lastPushStatus
        ? `마지막 push 상태 · ${lastPushStatus}`
        : "factory cycle이 새 변경을 만들면 여기에 안내가 나타납니다.",
    };
  }
  if (blocker.blocked) {
    const reasons = (blocker.warning_reasons || []).slice(0, 2).join(" · ");
    return {
      tone: "error",
      title: "Release Safety Gate 차단",
      detail:
        reasons ||
        blocker.publish_blocker_message ||
        "차단 사유는 publish_blocker 패널을 확인하세요.",
    };
  }
  if (qa.status === "failed") {
    return {
      tone: "error",
      title: "QA 실패로 배포 전 확인이 필요합니다.",
      detail: qa.failed_reason || "qa_feedback.md를 확인 후 다시 시도하세요.",
    };
  }
  if (dryRun) {
    return {
      tone: "info",
      title: "배포 예행연습 모드입니다.",
      detail:
        "실제 push/deploy는 수행되지 않습니다. " +
        "LOCAL_RUNNER_PUBLISH_DRY_RUN=false + LOCAL_RUNNER_ALLOW_PUBLISH=true 후 다시 시도하세요.",
    };
  }
  return {
    tone: "ready",
    title: "배포 준비 완료 — 변경 요약과 수동 QA 시나리오를 확인하세요.",
    detail:
      qa.status === "passed"
        ? "QA Gate 통과 · Release Safety Gate clean"
        : "QA Gate는 배포 직전 on-demand 실행됩니다.",
  };
}

const TONE_STYLE = {
  ready:  { dot: "#34d399", border: "#34d39955", bg: "rgba(52,211,153,0.08)" },
  info:   { dot: "#7dd3fc", border: "#38bdf855", bg: "rgba(56,189,248,0.08)" },
  muted:  { dot: "#94a3b8", border: "#1e293b",   bg: "rgba(15,23,42,0.4)" },
  error:  { dot: "#f87171", border: "#f8717155", bg: "rgba(248,113,113,0.08)" },
};

// ---------------------------------------------------------------------------
// Tiny render helpers. No external libs — plain ul/li with the same
// monospace vibe the rest of ControlDock uses.
// ---------------------------------------------------------------------------

function SectionHeader({ children }) {
  return (
    <h4 className="text-[10px] font-bold uppercase tracking-[0.3em] text-[#d4a843]">
      {children}
    </h4>
  );
}

function BulletList({ items, emptyHint }) {
  if (!items || items.length === 0) {
    if (!emptyHint) return null;
    return (
      <p className="text-[11px] text-slate-500">{emptyHint}</p>
    );
  }
  return (
    <ul className="space-y-1 text-[11.5px] leading-relaxed text-slate-200">
      {items.map((line, i) => (
        <li key={`${i}-${line}`} className="flex gap-2">
          <span className="select-none text-[#d4a843]">·</span>
          <span>{line}</span>
        </li>
      ))}
    </ul>
  );
}

function NumberedList({ items }) {
  if (!items || items.length === 0) return null;
  return (
    <ol className="space-y-1 text-[11.5px] leading-relaxed text-slate-200">
      {items.map((line, i) => (
        <li key={`${i}-${line}`} className="flex gap-2">
          <span
            className="select-none font-bold text-[#d4a843]"
            style={{ minWidth: "1.5em" }}
          >
            {i + 1}.
          </span>
          <span>{line}</span>
        </li>
      ))}
    </ol>
  );
}

function FileChips({ files }) {
  if (!files || files.length === 0) return null;
  const visible = files.slice(0, MAX_FILES_VISIBLE);
  const overflow = files.length - visible.length;
  return (
    <div className="flex flex-wrap gap-1">
      {visible.map((p) => (
        <code
          key={p}
          className="rounded px-1.5 py-0.5 text-[10px] text-slate-200"
          style={{
            backgroundColor: "#0a1228",
            border: "1px solid #1e293b",
          }}
          title={p}
        >
          {p}
        </code>
      ))}
      {overflow > 0 && (
        <span className="text-[10px] text-slate-500">외 {overflow}개</span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------

export default function ReleasePreviewPanel({ deployState }) {
  const publish = deployState?.publish || {};
  const changedFiles = Array.isArray(publish.changed_files)
    ? publish.changed_files
    : [];
  const changedCount = deployState?.changedCount ?? changedFiles.length ?? 0;
  const actionsUrl = publish.actions_url;
  const lastMessage = publish.last_publish_message;
  const banner = pickStatusBanner({ deployState });
  const tone = TONE_STYLE[banner.tone] || TONE_STYLE.muted;
  const preview = buildPreview(changedFiles);

  return (
    <section
      className="grid gap-3 rounded p-3"
      data-testid="release-preview-panel"
      style={{
        backgroundColor: "#0e1a35",
        border: "1.5px solid #1e293b",
        borderRadius: 6,
        fontFamily: "ui-monospace, monospace",
      }}
    >
      <header className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <div>
          <h3 className="text-[12px] font-bold uppercase tracking-[0.3em] text-[#d4a843]">
            이번 배포 안내
          </h3>
          <p className="mt-0.5 text-[11px] text-slate-400">
            배포 전에 변경사항과 직접 확인할 시나리오를 확인하세요.
          </p>
        </div>
        <span
          className="rounded px-2 py-0.5 text-[10px] tracking-widest text-slate-300"
          style={{ backgroundColor: "#0a1228", border: "1px solid #1e293b" }}
        >
          변경 {changedCount}개
        </span>
      </header>

      {/* Status banner */}
      <div
        className="rounded px-3 py-2"
        style={{
          backgroundColor: tone.bg,
          border: `1px solid ${tone.border}`,
        }}
      >
        <div className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-2 rounded-full"
            style={{ backgroundColor: tone.dot }}
          />
          <span className="text-[11.5px] font-bold text-slate-100">
            {banner.title}
          </span>
        </div>
        {banner.detail && (
          <p className="mt-1 text-[10.5px] leading-snug text-slate-400">
            {banner.detail}
          </p>
        )}
      </div>

      {changedCount === 0 ? (
        <p className="text-[11px] text-slate-500">
          factory cycle이 새 변경을 만들거나, 운영자가 직접 작업한 변경이
          working tree에 들어오면 이번 배포 안내가 채워집니다.
        </p>
      ) : (
        <>
          <div className="grid gap-1">
            <SectionHeader>1. 변경 요약</SectionHeader>
            <BulletList
              items={preview.summaries}
              emptyHint="자동 분류된 요약이 없습니다."
            />
          </div>

          <div className="grid gap-1">
            <SectionHeader>2. 바뀐 화면</SectionHeader>
            <BulletList
              items={preview.screens}
              emptyHint="화면 변경 없음 (백엔드/설정 변경)"
            />
          </div>

          <div className="grid gap-1">
            <SectionHeader>3. 직접 확인할 시나리오</SectionHeader>
            <NumberedList items={preview.scenarios} />
            {(!preview.scenarios || preview.scenarios.length === 0) && (
              <p className="text-[11px] text-slate-500">
                자동 시나리오가 없습니다. 변경 파일을 보고 직접 점검 항목을
                정해 주세요.
              </p>
            )}
          </div>

          <div className="grid gap-1">
            <SectionHeader>4. 기대 결과</SectionHeader>
            <BulletList
              items={preview.expected}
              emptyHint="이번 배포의 기대 결과가 별도로 정의돼 있지 않습니다."
            />
          </div>

          <div className="grid gap-1">
            <SectionHeader>5. 주의 / 리스크</SectionHeader>
            <BulletList
              items={preview.risks}
              emptyHint="자동 식별된 리스크가 없습니다 — 그래도 직접 한 번 더 점검해 주세요."
            />
          </div>

          <div className="grid gap-1">
            <SectionHeader>6. 변경 파일 ({changedFiles.length}개)</SectionHeader>
            <FileChips files={changedFiles} />
          </div>
        </>
      )}

      {/* GitHub Actions handoff link — always shown when we have a URL,
          even with 0 changes, since the operator may want to inspect
          a previous run. */}
      <div className="grid gap-1">
        <SectionHeader>7. GitHub Actions</SectionHeader>
        {actionsUrl ? (
          <a
            href={actionsUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[11px] text-sky-300 hover:text-sky-200"
          >
            ▶ Deploy Stampport workflow 열기
          </a>
        ) : (
          <p className="text-[11px] text-slate-500">
            actions_url 미설정 — runner heartbeat가 들어오면 채워집니다.
          </p>
        )}
        {lastMessage && (
          <p className="line-clamp-3 text-[10.5px] text-slate-500">
            마지막 메시지 · {lastMessage}
          </p>
        )}
      </div>
    </section>
  );
}
