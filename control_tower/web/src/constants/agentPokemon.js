// Stampport Control Tower — agent ↔ Pokemon mapping for the central
// PixelOffice. The office renders each agent role as a Pokemon
// character via `PokemonAvatar`, which loads the asset path below and
// falls back to the emoji glyph if the file is missing.
//
// Why this file exists separately from `constants/agents.js`:
//   - The Pokemon mapping is a *theme* layer on top of the agent
//     roster. Keeping it isolated means we can swap to a different
//     character set (or remove the theme entirely) without touching
//     agent ids, layout, ping-pong wiring, or supervisor evaluators.
//   - Asset paths live here so the office never imports anything
//     under `/public/assets/...` directly — the operator can drop
//     sprites in without code edits.
//
// Each entry shape:
//   {
//     pokemon:   English Pokemon name (PR-friendly logging)
//     korean:    Korean character name shown next to the role label
//     asset:     /assets/... path; PokemonAvatar uses BASE_URL prefix
//     fallback:  emoji shown when the asset 404s or hasn't been added
//     accent:    short hex used as the desk glow tint, distinct from
//                the role color so the office stays readable
//     reason:    why this Pokemon got the role; surfaced in tooltips
//     props:     declarative desk-side props (label, icon emoji)
//                rendered as a tiny chip next to the avatar
//   }
//
// `getAgentPokemon(agentId)` resolves the canonical id (the existing
// agents.js uses `ai_architect` while the spec writes `ai`).

const AGENT_POKEMON_BASE = {
  pm: {
    pokemon: "Pikachu",
    korean: "피카츄",
    asset: "/assets/agents/pokemon/pikachu.png",
    fallback: "⚡",
    accent: "#facc15",
    reason: "팀 리더와 빠른 조율",
    props: { label: "리더 보드", emoji: "⚡" },
  },
  planner: {
    pokemon: "Bulbasaur",
    korean: "이상해씨",
    asset: "/assets/agents/pokemon/bulbasaur.png",
    fallback: "🌱",
    accent: "#34d399",
    reason: "아이디어를 심고 키우는 기획자",
    props: { label: "아이디어 보드", emoji: "🌱" },
  },
  designer: {
    pokemon: "Jigglypuff",
    korean: "푸린",
    asset: "/assets/agents/pokemon/jigglypuff.png",
    fallback: "🎤",
    accent: "#f9a8d4",
    reason: "감성적이고 시각적 매력을 만드는 디자이너",
    props: { label: "팔레트", emoji: "🎨" },
  },
  frontend: {
    pokemon: "Charmander",
    korean: "파이리",
    asset: "/assets/agents/pokemon/charmander.png",
    fallback: "🔥",
    accent: "#fb923c",
    reason: "빠르게 UI를 구현하는 프론트엔드",
    props: { label: "UI 카드", emoji: "🔥" },
  },
  backend: {
    pokemon: "Squirtle",
    korean: "꼬부기",
    asset: "/assets/agents/pokemon/squirtle.png",
    fallback: "💧",
    accent: "#38bdf8",
    reason: "안정적인 API/데이터 기반 담당",
    props: { label: "API 콘솔", emoji: "💧" },
  },
  ai_architect: {
    pokemon: "Eevee",
    korean: "이브이",
    asset: "/assets/agents/pokemon/eevee.png",
    fallback: "✨",
    accent: "#c084fc",
    reason: "다양하게 진화 가능한 AI 설계자",
    props: { label: "룰 그래프", emoji: "✨" },
  },
  qa: {
    pokemon: "Snorlax",
    korean: "잠만보",
    asset: "/assets/agents/pokemon/snorlax.png",
    fallback: "🛌",
    accent: "#a3a3a3",
    reason: "차분하고 꼼꼼한 검수자",
    props: { label: "체크리스트", emoji: "🔎" },
  },
  deploy: {
    pokemon: "Meowth",
    korean: "나옹",
    asset: "/assets/agents/pokemon/meowth.png",
    fallback: "📦",
    accent: "#fbbf24",
    reason: "배포와 전달을 담당하는 운영자",
    props: { label: "배포 박스", emoji: "📦" },
  },
};

// Aliases — the spec writes `ai`, the existing roster uses `ai_architect`.
// Keep both keys pointing at the same record so callers can use whichever
// id they have in context.
export const AGENT_POKEMON = {
  ...AGENT_POKEMON_BASE,
  ai: AGENT_POKEMON_BASE.ai_architect,
};

const FALLBACK_RECORD = {
  pokemon: "Unknown",
  korean: "—",
  asset: "",
  fallback: "🎯",
  accent: "#94a3b8",
  reason: "역할 미지정",
  props: { label: "준비 중", emoji: "·" },
};

export function getAgentPokemon(agentId) {
  if (!agentId) return FALLBACK_RECORD;
  return AGENT_POKEMON[agentId] || FALLBACK_RECORD;
}

// Resolve an asset URL through Vite's BASE_URL so the deployed bundle
// can sit under a sub-path (e.g. /stampport-control/) without breaking
// asset references.
export function resolveAgentAsset(assetPath) {
  if (!assetPath) return "";
  const base = (import.meta.env?.BASE_URL || "/").replace(/\/$/, "");
  return assetPath.startsWith("/") ? `${base}${assetPath}` : `${base}/${assetPath}`;
}
