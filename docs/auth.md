# Stampport Auth — Kakao / Naver / Guest

Stampport is a long-lived passport: stamps, badges, titles, and the
shared card all need a stable identity that persists across visits.
This doc describes the MVP auth model + how to wire real OAuth
later.

## Identity model

`localStorage` keeps a single canonical profile under the key
`stampport:user`. The shape is documented in
`app/web/src/utils/storage.js`:

```jsonc
{
  "id":               "local_kakao_3a7b4c8e1f0d",
  "provider":         "guest" | "kakao" | "naver",
  "provider_user_id": null,
  "nickname":         "성표",
  "avatar_style":     "stamp_collector",
  "passport_title":   "동네 도장 수집가",
  "level":            1,
  "exp":              0,
  "created_at":       "2026-04-28T01:23:45.000Z",
  "last_login_at":    "2026-04-28T01:23:45.000Z",
  "user_id":          "local_kakao_3a7b4c8e1f0d"
}
```

Stamps and profile metadata bucket on `user_id`:

- `stampport:stamps:<user_id>` — array of stamp records.
- `stampport:profile:<user_id>` — `{ selected_title_id }`.

### Migration

A pre-existing legacy profile (`{nickname, email, user_id}`) is
auto-migrated by `loadUser()` the first time the new bundle runs:

- legacy `user_id` is preserved → existing stamps stay reachable.
- `provider` set to `"guest"`.
- `level=1`, `exp=0` (real values are still computed from the
  stamps array on every render).

No user input is needed; the migration is silent and idempotent.

## Login flows

The Login screen offers three CTAs:

1. **카카오로 시작하기** — real OAuth when configured, mock profile
   otherwise.
2. **네이버로 시작하기** — same model.
3. **게스트로 둘러보기** — opens a nickname form. No social account.

Internally everything funnels into `loginAs({ provider, nickname,
... })` from `AppProvider`, which:

- reuses the existing profile when `provider + provider_user_id`
  match (or, for guests, when the legacy profile already exists),
- otherwise creates a fresh profile via `makeNewProfile()`,
- updates `last_login_at`, then saves.

## Real OAuth wiring

### Front-end env vars

Set these in `app/web/.env.local` (never commit):

```
VITE_KAKAO_CLIENT_ID=<REST API key>
VITE_NAVER_CLIENT_ID=<App client id>
VITE_OAUTH_REDIRECT_BASE=https://reviewdr.kr/stampport
```

The frontend only ever reads **public** client ids. Never put a
client secret into the front-end bundle.

### Provider redirect URIs

Register these in each provider's developer console:

- Kakao
  `https://reviewdr.kr/stampport/auth/callback/kakao`
- Naver
  `https://reviewdr.kr/stampport/auth/callback/naver`

The hash router translates these to `#/auth/callback/<provider>?code=…&state=…`.

### Authorize URL

`utils/oauth.js#buildAuthorizeUrl(provider)` builds the redirect to
each provider's authorize endpoint with `client_id`, `redirect_uri`,
`response_type=code`, and a CSRF `state` value persisted in
`sessionStorage`. The callback screen verifies the round-trip state
on return.

### Callback (today)

`screens/AuthCallback.jsx` reads the querystring (`code`, `state`,
`error`) and currently:

- redirects back to `/login` on `error`,
- otherwise synthesizes a mock profile via `loginAs(provider, …)`
  and sends the user to `/passport`.

This is intentional — the **real** code-for-token exchange must
happen on a backend that holds the client secret.

### Callback (next step)

When the FastAPI backend lands:

1. Provider redirects to
   `https://reviewdr.kr/stampport-control-api/auth/callback/<provider>?code=…`.
2. Backend exchanges the code for an access token using
   `KAKAO_CLIENT_SECRET` / `NAVER_CLIENT_SECRET` (server-only env).
3. Backend fetches the user-info endpoint, normalizes
   `{provider_user_id, nickname, avatar_url}`, signs a session
   token, and redirects to
   `https://reviewdr.kr/stampport/#/auth/callback/<provider>?token=…&nickname=…`.
4. AuthCallback reads `token` + `nickname`, calls
   `loginAs({ provider, nickname, provider_user_id })`, and stores
   the token (still in `localStorage` for now — a real cookie
   session lands later).

## Mock fallback rules

When `VITE_KAKAO_CLIENT_ID` / `VITE_NAVER_CLIENT_ID` are missing:

- `oauthConfigFor(provider).isMock` is true.
- `buildAuthorizeUrl()` returns `{ url: '', isMock: true }` —
  the Login screen short-circuits to the mock profile.
- The user gets a labelled "DEV" pill on the social buttons and a
  helper line under the CTAs:
  > "소셜 로그인은 환경변수가 설정되지 않아 데모 모드로 동작합니다."

Mock profiles use:

| Provider | Nickname       | avatar_style    |
| -------- | -------------- | --------------- |
| kakao    | 카카오 여행자  | kakao_explorer  |
| naver    | 네이버 탐험가  | naver_explorer  |

These are explicitly demo-only. They're acceptable in the deployed
build because no real Stampport account state is created until the
operator actually adds stamps.

## Secret hygiene

- ❌ Never commit `KAKAO_CLIENT_SECRET` / `NAVER_CLIENT_SECRET`.
- ❌ Never reference them in `app/web/**`.
- ✅ Front-end only sees `VITE_*_CLIENT_ID`.
- ✅ Backend (FastAPI) holds the secrets via env, ideally a
  `.env.local` file ignored by `.gitignore`.
- ✅ `app/web/src/utils/oauth.js` redacts nothing on its own —
  always check the bundle output if you change the env names.

## Open items

- [ ] Backend `auth/callback/<provider>` route (FastAPI).
- [ ] Real session token storage (cookie + revocation).
- [ ] Linking a guest profile to a social account
      ("이미 만든 여권을 카카오로 이어가기").
- [ ] Avatar renderer that honors `avatar_style` distinctly per
      provider.
