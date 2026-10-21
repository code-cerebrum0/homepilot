# HomePilot Frontend

Next.js 14 (App Router) · React 18 · TypeScript · Recharts. No UI framework; plain CSS in `src/app/globals.css`.

## Run
```bash
npm install
cp .env.example .env.local   # set NEXT_PUBLIC_API_BASE_URL
npm run dev                  # http://localhost:3000
npm run lint                 # type-check
```
The backend must follow `docs/API.md`: cookie auth, a readable `csrf_token` cookie, CORS with credentials for `http://localhost:3000`. For cookies to be sent in local dev, the backend and frontend should both be on `localhost` (different ports are fine); use `Secure=false` cookies over http locally.

## Pages
| Route | Purpose |
|---|---|
| `/login`, `/register` | Authentication |
| `/dashboard` | Live power, today's kWh, estimated cost, device breakdown, daily chart, comparison, saving tips, data freshness |
| `/devices` | Register, rename, enable/disable, rotate key, remove, recent readings (owner/admin manage; members read) |
| `/assistant` | Chat with the AI agent; shows which tools ran and whether they succeeded |
| `/settings` | Profile, flat-rate tariff, log out / log out everywhere |

## Structure
```
src/lib/api.ts      fetch wrapper: cookies, CSRF header, one-shot refresh on 401
src/lib/auth.tsx    session context (me, household, login/logout)
src/lib/types.ts    TypeScript types matching docs/API.md
src/app/(app)/      authenticated pages sharing a nav layout and auth guard
```

## Notes
- Nothing is mocked: every value shown comes from the API. Empty states are shown when there is no data.
- Simulated devices/readings are labelled; costs are labelled as estimates.
- Active household is the user's first membership; a household switcher is not built yet.
- Not yet verified by a build: dependencies could not be installed in the environment where this was written, so run `npm install && npm run lint && npm run build` first and fix any type errors.
