# Deployment

Four services, all on free or low-cost tiers during development:

| Layer | Service | Notes |
|---|---|---|
| Source | GitHub (`secure-quiz`) | |
| Frontend | Vercel | static build from `frontend/` |
| Backend | Render | Docker web service from `backend/` |
| Database, Auth, Storage | Supabase | the only stateful component |

The backend container is disposable. It must never rely on its own filesystem for
anything that has to survive a redeploy — exports go to Supabase Storage or
straight down the wire to the browser.

## 1. Supabase

1. Create the project; record the project ref and database password.
2. **Project Settings → API**: copy `Project URL`, `anon` key, `service_role` key.
3. **Project Settings → API → JWT Settings**: copy the JWT secret.
4. **Project Settings → Database → Connection string → URI**: take both the
   pooled URI (port 6543, for the app) and the direct URI (port 5432, for
   Alembic). Rewrite the scheme to `postgresql+psycopg://`.
5. **Storage**: create two **private** buckets, `evidence` and `exports`. Public
   buckets would expose student data.
6. **Authentication → Providers**: enable Email. Add the Vercel domain and
   `http://localhost:5173` to the redirect allowlist.
7. Run the migrations from a machine that can reach the direct connection:

```bash
cd backend
ALEMBIC_DATABASE_URL="postgresql+psycopg://...:5432/postgres" alembic upgrade head
```

Row Level Security: the backend connects with a privileged role and performs its
own authorization, so RLS is not the enforcement layer here. Leave RLS enabled on
the public schema and do not expose these tables through PostgREST — the REST
surface is the FastAPI app, not Supabase's auto API.

## 2. Backend on Render

New → Web Service → connect the repo.

| Setting | Value |
|---|---|
| Runtime | Docker |
| Dockerfile path | `backend/Dockerfile` |
| Docker build context | `backend` |
| Health check path | `/api/health` |
| Instance type | Free to start; paid tier removes cold starts |

Environment variables (Render dashboard → Environment; mark all as secret):

```
ENVIRONMENT=production
DATABASE_URL=postgresql+psycopg://postgres.REF:PASSWORD@aws-0-REGION.pooler.supabase.com:6543/postgres
SUPABASE_URL=https://REF.supabase.co
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...
SUPABASE_JWT_SECRET=...
GEMINI_API_KEY=...
RESEND_API_KEY=...
EMAIL_FROM=Secure Quiz <results@your-domain>
FRONTEND_URL=https://your-app.vercel.app
CORS_ORIGINS=https://your-app.vercel.app
```

`PORT` is injected by Render; the image already honours it. Do not set it by hand.

Render's free tier sleeps after inactivity — the first request after a sleep is
slow. That is acceptable for a teacher dashboard and **not** acceptable
mid-exam, so use a paid instance for a real exam window, or keep the service warm
during it.

Migrations on deploy: run them as a one-off job or a pre-deploy command rather
than at container start, so that a rolling deploy cannot run two migrations
concurrently.

## 3. Frontend on Vercel

New Project → import the repo.

| Setting | Value |
|---|---|
| Root directory | `frontend` |
| Framework preset | Vite |
| Build command | `npm run build` |
| Output directory | `dist` |

Environment variables (all three are public by construction):

```
VITE_API_URL=https://secure-quiz-api.onrender.com
VITE_SUPABASE_URL=https://REF.supabase.co
VITE_SUPABASE_ANON_KEY=...
```

Never add a service-role key, `GEMINI_API_KEY` or `RESEND_API_KEY` here. Anything
`VITE_`-prefixed is compiled into a file the world can download.

SPA routing is handled by `frontend/vercel.json`, which rewrites unknown paths to
`index.html` so `/q/8F7K2P` and `/attempt/:id` survive a hard refresh.

## 4. Order of operations

1. Supabase project, buckets, auth settings, migrations.
2. Render backend with the Supabase values; confirm `/api/health/ready` returns
   `{"database": "ok"}`.
3. Vercel frontend with `VITE_API_URL` pointing at Render.
4. Set `CORS_ORIGINS` and `FRONTEND_URL` on Render to the Vercel domain and
   redeploy.
5. Add the Vercel domain to Supabase Auth redirect URLs.

Step 4 is the one that is always forgotten; the symptom is a CORS error in the
browser console on every teacher request.

## 5. Resend

Verify a sending domain, create an API key, set `RESEND_API_KEY` and
`EMAIL_FROM`. Until a domain is verified, Resend only delivers to the account
owner's address — fine for development. Email failure is logged as `EMAIL_FAILED`
and never loses a result; the teacher can resend from the results page.

## 6. Local development

See the README for the short version. In brief: `docker compose up` runs the API
against a local PostgreSQL with the same migrations, and `npm run dev` runs the
frontend against it. Local development does not require Supabase for the database,
but it does require a Supabase project for Auth unless the stub auth mode is used.

## 7. Operational checks after any deploy

- `GET /api/health/ready` returns `database: ok`
- Teacher sign-in returns a profile from `GET /api/auth/me`
- `GET /api/public/quizzes/{code}` returns metadata and **no** question text
- A test attempt receives a paper, and a second start with the same email is
  refused with 409
- `POST /submit` twice returns the same score and sends one email
