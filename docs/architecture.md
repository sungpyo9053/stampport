# Stampport Architecture

## Initial Architecture

Frontend:
- React
- Vite
- localStorage for MVP data
- mock login

Backend:
- FastAPI
- /health endpoint first
- API expansion later

Data persistence:
- MVP: localStorage
- Later: Supabase or PostgreSQL

## Main Data Models

### UserProfile

- user_id
- nickname
- email
- selected_title
- level
- exp
- created_at

### Stamp

- id
- user_id
- place_name
- area
- category
- tags
- representative_menu
- visited_at
- verification_level
- verification_status
- trust_score
- kick_points

### Badge

- id
- name
- description
- condition_type
- required
- progress
- earned

### Quest

- id
- title
- description
- reward_exp
- condition
- progress
- completed

## Verification Levels

MVP:
- manual
- photo

Future:
- photo_location
- qr
- qr_location
- dynamic_qr_location
- receipt
- pos

## Frontend Storage Rule

Even when using localStorage, data must be separated by user_id.

Example keys:
- stampport:user
- stampport:stamps:{user_id}
- stampport:passport:{user_id}

## API Direction

Initial API:
- GET /health

Later API:
- POST /auth/mock-login
- GET /me
- POST /stamps
- GET /stamps
- GET /passport
- GET /badges
- GET /quests

## Development Priority

1. Make the web MVP work locally.
2. Keep API minimal.
3. Keep data model ready for backend migration.
4. Do not implement real auth before MVP validation.
