# HPMS — Hostel & PG Management System

A full-stack portal to find verified hostels and PGs, book them, and manage stays, rent and properties.
It has three roles, each with its own dashboard:

| Role | What they can do |
|------|------------------|
| **Student** | Search and map-browse properties, wishlist, book, pay rent, raise complaints, review stays |
| **Owner** | Manage properties, rooms and beds, handle bookings and tenancies, post notices, reply to reviews |
| **Admin** | Verify owners and properties, manage users, resolve duplicates, moderate reviews, view audit logs and reports |

## Tech Stack

- **Frontend:** React 18, Vite, Tailwind CSS, Framer Motion, Recharts, React Router, Google Maps (`@vis.gl/react-google-maps`)
- **Backend:** FastAPI, Pydantic Settings, SlowAPI (rate limiting), Celery and Redis (background jobs)
- **Database and auth:** Supabase (PostgreSQL via `psycopg`, JWT auth)

## Project Structure

```
HPMS_portal/
├── backend/            FastAPI app (app/routers, app/services, app/db, app/core)
├── frontend/           React + Vite app (src/pages, src/components, src/layouts)
├── supabase/           Database migrations
├── docs/               Additional documentation
├── render.yaml         Render deployment blueprint (backend)
└── HPMS_properties_rooms_beds_seed.sql   Demo properties/rooms/beds seed
```

## Prerequisites

- Python 3.11+
- Node.js 18+
- A Supabase project (database + auth)
- Google Maps API keys (a browser key and a server key)
- Redis (optional, only for background tasks)

## Local Setup

### 1. Backend

```bash
cd backend
python -m venv venv
venv\Scripts\activate          # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env         # macOS/Linux: cp .env.example .env
# fill in .env, then:
uvicorn app.main:app --reload
```

API runs at http://localhost:8000. Interactive docs: http://localhost:8000/docs. Health check: `/health`.

### 2. Frontend

```bash
cd frontend
npm install
copy .env.example .env.local   # macOS/Linux: cp .env.example .env.local
npm run dev
```

App runs at http://localhost:5173.

### 3. Background worker (optional)

PDF receipts, notice fan-out and notification tasks use Celery:

```bash
cd backend
celery -A app.core.celery.celery_app worker --loglevel=info
```

Requires `REDIS_URL` (defaults to `redis://localhost:6379/0`).

## Environment Variables

### Backend (`backend/.env`)

| Variable | Description |
|----------|-------------|
| `ENV` | `development` or `production` |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_JWT_SECRET` | Supabase credentials |
| `DATABASE_URL` | PostgreSQL connection string |
| `TURNSTILE_SECRET` | Cloudflare Turnstile secret |
| `GOOGLE_MAPS_SERVER_KEY` | Google Maps server key |
| `CORS_ORIGINS` | Comma-separated frontend URLs allowed in production |
| `RAZORPAY_*`, `HASH_PEPPER`, `WEBHOOK_SECRET`, `EMAIL_PROVIDER` | Optional, see [config.py](backend/app/config.py) |

### Frontend (`frontend/.env.local`)

| Variable | Description |
|----------|-------------|
| `VITE_API_URL` | Backend base URL, e.g. `http://localhost:8000` |
| `VITE_GOOGLE_MAPS_API_KEY` | Google Maps browser key |

> Never commit `.env` files or secrets.

## Demo Data

With `ENV=development`, helper endpoints are available for seeding demo data
(`/seed/demo`, `/seed/100`). They are **disabled in production**.
Demo users must exist in Supabase Auth before inserting matching `public.users` records.
All names, emails and phone numbers in the demo data are fictional.

## Deployment

| Part | Platform | Notes |
|------|----------|-------|
| Backend | [Render](https://render.com) | Use the `render.yaml` blueprint. Set `ENV=production` and `CORS_ORIGINS` |
| Frontend | [Vercel](https://vercel.com) | Root directory `frontend`, build `npm run build`, output `dist`. Set `VITE_API_URL` and `VITE_GOOGLE_MAPS_API_KEY` |

Steps:

1. Deploy the backend and fill in the secret environment variables.
2. Deploy the frontend with `VITE_API_URL` pointing at the backend.
3. Set `CORS_ORIGINS` on the backend to the frontend URL, then redeploy.
4. Restrict the Google Maps browser key to your frontend domain.

## Testing

```bash
cd backend
pytest
```
