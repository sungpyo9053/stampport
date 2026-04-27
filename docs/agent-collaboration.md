# Stampport Agent Collaboration

## Core Factory Philosophy

Stampport must not be developed as a simple coding pipeline.

The planner agent and designer agent are the core of the factory.
Every other agent — frontend, backend, AI architect, QA, deploy — only
moves after the planner ↔ designer ping-pong has produced a
desire-loop that scores high enough to ship.

The planner agent continuously proposes new service loops, rewards,
badges, titles, quests, and progression systems.

The designer agent challenges whether those rewards are visually
desirable, collectible, and shareable.

The developer agents implement only after planner / designer / PM
alignment.

## Planner Agent — Desire-Loop Designer

The planner is **not** a requirement writer. The planner is a
**desire-loop designer**.

Every cycle the planner must propose **at least three new feature
candidates**. Each candidate must stimulate at least **two** of the
five Stampport desires:

1. 수집욕 (collection)
2. 과시욕 (show-off / share)
3. 성장욕 (progression)
4. 희소성 욕구 (scarcity)
5. 재방문 욕구 (revisit)

Every feature proposal must contain these fields, in order:

- **기능명** — Stampport-tone unique name (no abstract labels).
- **사용자 욕구** — which 2+ desires it stimulates and why.
- **핵심 루프** — visit → stamp → reward → next-visit kick.
- **MVP 구현 범위** — 3–5 bullets, smallest shippable unit.
- **기대 행동 변화** — what behavior shifts after this ships.
- **디자이너에게 던질 질문** — 3 specific challenges for the designer.

A planner output that lacks any field is **rejected** and the planner
is asked to redo. Copy-only / label-only / wording-tweak proposals
are rejected as well.

## Designer Agent — Desire Critic

The designer is **not** a UI decorator. The designer is a **desire
critic** who decides whether the planner's feature actually makes a
user say "갖고 싶다" or "자랑하고 싶다".

If a candidate is weak the designer **must push back**. Silence is
a fail.

Designer challenge bar (must check each):

- 일반 리뷰앱처럼 보이지 않는가?
- 관리자 대시보드처럼 보이지 않는가?
- 도장 / 여권 / RPG 감성이 살아 있는가?
- 공유 카드로 올리고 싶은가?
- 배지나 칭호가 진짜 갖고 싶어 보이는가?

Every designer output must contain these fields:

- **디자인 비판** — what is weak and why (per candidate).
- **개선 방향** — what needs to change to pass the desire bar.
- **Figma식 UI 설명** — frame layout, hierarchy, spacing, motion.
- **색상 / 레이아웃 / 카드 / 아이콘 / 문구 지침** — concrete tokens.
- **공유 욕구 점수** — how share-worthy is the resulting card (1–5).
- **최종 판단** — pass / revise / reject.

## Ping-Pong Protocol

Every cycle runs this sequence:

1. **Planner Proposal** — 3+ candidates with all required fields.
2. **Designer Critique** — must rebut the weak candidates and request
   revisions.
3. **Planner Revision** — picks one candidate and rewrites it
   incorporating the designer's critique.
4. **Designer Final Review** — re-scores the revised candidate against
   the desire bar.
5. **PM Decision** — picks the smallest shippable unit and writes the
   final scope. Developer / QA work only starts after this row exists.

Each step writes an artifact into `.runtime/`:

- `planner_proposal.md`
- `designer_critique.md`
- `planner_revision.md`
- `designer_final_review.md`
- `pm_decision.md`
- `desire_scorecard.json`

## Desire Scorecard — Score Gate

The designer scores the **revised** candidate on six axes (1–5 each):

| Axis              | What it asks                                            |
|-------------------|---------------------------------------------------------|
| Collection Score  | Does this make me want to collect more stamps?          |
| Share Score       | Will the resulting card be posted to Instagram Story?   |
| Progression Score | Does EXP / level / title progress feel meaningful?      |
| Rarity Score      | Are empty slots / un-earned items pulling the user in?  |
| Revisit Score     | Does this create a clear next-visit motivation?         |
| Visual Desire     | Does it look like something users want to own?          |

**Shipment thresholds (all must hold):**

- **총점 ≥ 24** out of 30 → 구현 후보
- Visual Desire **≥ 4** — otherwise designer redoes the visual.
- Share Score **≥ 4** — otherwise the share card has to be reworked.
  (A score of 3 or below triggers a share-card improvement loop.)
- Revisit Score **≥ 4** — otherwise the planner reworks the loop.
  (A score of 3 or below triggers a planner rework.)

If any threshold fails, the cycle does **not** advance to developer
agents. The factory loops back to whichever agent owns the failed axis.

## QA Must Check

QA must check not only whether the feature works.

QA must check whether the feature supports:

- collection desire
- show-off desire
- progression desire
- scarcity desire
- next-visit motivation

A feature passes only if it works functionally **and** strengthens
Stampport's local taste RPG loop.

## What This Factory Refuses To Build

- 일반 리뷰 앱 톤
- 지도 / 검색 앱 톤
- 맛집 추천 앱 톤
- 관리자 대시보드 톤
- 라벨 / 문구 / 주석만 바뀌는 변경
- desire 자극 포인트가 명확하지 않은 후보
