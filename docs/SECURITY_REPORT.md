# HPMS Phase 11 Security Audit & Hardening Report

## Overview
This document summarizes the findings and fixes applied during the Phase 11 security hardening of the HPMS portal.

## 1. Authentication & Authorization (AuthN/AuthZ)
- **Finding:** Every endpoint uses `get_db_connection` (or `get_public_db_connection` for public routes) which validates the JWT synchronously via `_decode_token`.
- **Public Routes Justified:**
  - `auth.py`: `login`, `password_reset`, `mfa_enroll`, `mfa_verify` (Auth endpoints)
  - `colleges.py`: `list_colleges` (Signup prerequisite)
  - `properties.py`: `search_properties`, `compare_properties`, `get_property_detail` (Public listings for prospective students)
  - `reviews.py`: `get_property_reviews` (Public reviews for prospective students)
  - `rooms.py`: `list_rooms`, `list_beds` (Available inventory browsing)
  - `students.py`: `guardian_consent` (Public form using a secure one-time URL token)
  - `notifications.py`: `email_webhook` (Secured via header signature from the email provider)
- **RLS Audit:**
  - All application tables have RLS enabled (0 missing).
  - Found a permissive policy on `audit_logs`: `Allow insert audit` which allowed `public`.
  - **Fix Applied:** Modified policy in `20240113000000_phase11_security.sql` to `WITH CHECK (auth.role() = 'authenticated')`.
- **MFA Enforcement:**
  - **Finding:** `require_role` in `app/db/session.py` did not explicitly verify the JWT `aal` claim for Admins.
  - **Fix Applied:** Enforced server-side AAL2 check for admin roles in `require_role`. Any admin request with aal1 now returns a 403.

## 2. Input Validation & SQL Injection
- **Finding:** Widespread use of `BaseModel` handles input validation. Pydantic excludes unknown fields by default, so mass-assignment (e.g., overriding `role` or `status`) is prevented.
- **SQL Injection:**
  - Searched the codebase for `f"SELECT`, `f"UPDATE"`, and string formatting.
  - **Finding:** Dynamic queries (like `WHERE` clauses) are securely parameterized using psycopg `(..., params)` or `", ".join(f"{k} = %s" for k in updates)`. No manual string concatenation with user input exists.
- **XSS:**
  - React's default output encoding protects against XSS. Checked the frontend codebase and confirmed no instances of `dangerouslySetInnerHTML`.

## 3. Uploads
- **Finding:** KYC documents, room photos, and complaint attachments relied on the provided `content_type` HTTP header. Filenames were built using `uid/id/filename`, presenting a path traversal risk.
- **Fix Applied:** 
  - Overhauled `validate_file` in `app/services/storage.py` to sniff magic bytes (`\xFF\xD8\xFF` for JPEG, `\x89PNG` for PNG, `RIFF...WEBP`, and `%PDF`).
  - Added `secure_filename()` using `uuid4().hex` to prevent path traversal. Updated all upload usages in `owners.py`, `properties.py`, `rooms.py`, and `complaints.py`.

## 4. Rate Limiting
- **Finding:** `limiter.limit` was missing from some critical endpoints.
- **Fix Applied:** Added missing rate limits:
  - `exports.py`: `export_resource` (5/minute)
  - `search.py`: `global_search` (20/minute)
  - `bookings.py`: `create_booking` (5/minute)
  - `notifications.py`: `email_webhook` (100/minute)

## 5. Headers, Transport, and CORS
- **Finding:** The backend `app/main.py` had basic CORS enabled but lacked advanced security headers.
- **Fix Applied:** Implemented `SecurityHeadersMiddleware` adding:
  - `Strict-Transport-Security: max-age=31536000; includeSubDomains`
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: strict-origin-when-cross-origin`
  - `Content-Security-Policy: default-src 'self'`

## 6. Secrets & PII Scrubbing
- **Finding:** `app/services/audit.py` actively intercepts fields like `name`, `email`, `phone`, `id_proof_url`, etc., and replaces them with a salted SHA-256 hash.
- **Verified:** `.env` is properly ignored in `.gitignore`. No committed secrets were found.

## 7. Dependencies (pip-audit & npm audit)
- **Backend (`pip-audit`):** Found vulnerabilities in base environment packages (`pip`, `setuptools`, `ecdsa`), but all direct HPMS requirements (FastAPI, pydantic, psycopg, etc.) are clean.
- **Frontend (`npm audit`):** Found vulnerabilities in `react-router` (Moderate), `esbuild` (Moderate), and `braces` (High) via transitive dependencies.
- **Fixes Applied:** Added a GitHub Dependabot config (`.github/dependabot.yml`) for automated daily PRs to safely apply these version bumps and monitor for ongoing vulnerabilities without manually breaking the build right now.

## Remaining Risks
- The frontend UI package updates require extensive QA to ensure `react-router` v7 changes do not break routing navigation. Deferred to automated PR testing via Dependabot.
