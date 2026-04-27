# Stampport QA Checklist

## Basic Build

- app/web npm run build must pass.
- app/api app/main.py py_compile must pass.
- /health API must return ok true.

## Auth

- Logged-out user can see Landing.
- Logged-out user cannot access Stamp Form.
- Logged-out user is redirected to Login.
- Mock login creates user profile.
- Logout blocks protected pages again.

## Stamp Flow

- User can create a stamp.
- Stamp includes place name, area, category, tags, representative menu.
- Stamp result card appears after creation.
- Kick points count is exactly 3.
- Stamp is saved by user_id.

## Passport

- My Passport shows total stamp count.
- My Passport shows level and EXP.
- My Passport shows area summary.
- My Passport shows category summary.
- My Passport shows earned badges.

## Badge / Title

- Badge progress updates after stamp creation.
- Earned badge is visually different from in-progress badge.
- Title can be displayed from earned badge.
- Initial badges include cafe, bakery, restaurant, area, and tag-based badges.

## Quest

- Weekly quests are visible.
- Quest progress updates after stamp creation.
- Completed quest gives visible reward or completion state.

## Mobile

- 390px width must not break.
- Cards must not overflow horizontally.
- CTA must remain tappable.
- Text must remain readable.

## Prohibited Regression

- Do not turn Stampport into a generic review app.
- Do not add map-first UI in MVP.
- Do not require GPS or QR for MVP.
