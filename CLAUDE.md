# Stampport Project Memory

## Project Identity

This project is Stampport, Korean name 스탬포트.

Stampport is not a restaurant review app.
Stampport is not a map search app.
Stampport is not a generic food recommendation app.

Stampport is a local taste RPG where users collect stamps by visiting cafes, bakeries, dessert shops, and restaurants.

## Core Concept

Users visit places and collect passport-like stamps.

Each stamp:
- increases EXP
- updates badge progress
- updates title progress
- unlocks kick points
- updates the user's local taste passport
- creates a shareable stamp card

## Core Loop

Visit place
→ Create stamp
→ Gain EXP
→ Update badge/title progress
→ Unlock kick points
→ Update passport
→ Share aesthetic stamp card
→ Receive next quest

## MVP Scope

The first MVP must include:

1. Mock login
2. Stamp creation
3. Stamp acquired result card
4. My Passport screen
5. EXP / level system
6. Badges and titles
7. Weekly quests
8. Kick points
9. Mobile-first UI

## MVP Exclusions

Do not implement these in the first MVP:

- Real map
- GPS verification
- QR verification
- Receipt OCR
- Payment
- Real social community
- Real recommendation algorithm
- Supabase Auth
- Push notifications
- Native app conversion

Keep extension points, but do not implement them yet.

## Design Direction

The UI must feel like:

- passport
- travel stamp
- local exploration
- taste collection
- RPG progression
- badge achievement
- aesthetic SNS card

The UI must not look like:

- Naver Map
- generic review app
- admin dashboard
- plain todo app

Preferred colors:

- deep green
- cream
- burgundy
- navy
- gold accent

## Main Copy

Korean:
오늘 다녀온 곳, 스탬포트에 도장 찍기.

Sub copy:
먹고 머문 곳들이, 나만의 로컬 여권이 됩니다.

## Required Screens

- Landing
- Login
- Stamp Form
- Stamp Result
- My Passport
- Badges / Titles
- Weekly Quests
- Share Card

## Development Rules

- Before coding, read CLAUDE.md and docs/*.md.
- Keep changes small.
- Do not auto commit or push.
- Always run build after frontend changes.
- Do not remove existing working features.
- Use mock data only when it can later be replaced by API or DB.
- Mobile width 390px must not break.
