# TA Course-Material Bot (Telegram)

Upload a course outline (PDF) -> get a term plan -> generate **labs, tutorials and assignments**, each as a student
.docx and an answer-key .docx (native Word equations, monospace code). Review, approve, and give free-text
feedback; feedback becomes per-course rules applied to every later generation. Spec: `ta_course_bot_spec.md`.

## Quick start (what you need)
1. A bot token from **@BotFather**.
2. A free **Postgres** URL (e.g. Supabase -> Project settings -> Database -> connection string).
3. At least one free LLM key: `GEMINI_API_KEY` (Google AI Studio), `GROQ_API_KEY`, `OPENROUTER_API_KEY`.
4. Set env vars (see `.env.example`): `TELEGRAM_BOT_TOKEN`, `INVITE_TOKEN` (any secret string you hand to TAs),
   `OWNER_TELEGRAM_ID` (your numeric Telegram id; lets you `/revoke`), `DATABASE_URL`, and the key(s).

### Run locally
    pip install -r requirements.txt        # pandoc must be installed (apt/brew install pandoc)
    export $(grep -v '^#' .env | xargs)
    alembic upgrade head && python -m bot.main

### Deploy on a free host (Render / Koyeb / Fly)
Deploy the `Dockerfile` (it installs pandoc, runs migrations, starts the bot). Set the env vars above.
* Polling (default) works anywhere. If the host requires an HTTP port, it is served automatically from `PORT`
  (health endpoint) - nothing to configure.
* Hosts that sleep idle services: set `UPDATE_MODE=webhook` and `WEBHOOK_URL=https://<your-app>/` instead.
* Disk is ephemeral; everything (documents, versions, rules, jobs) lives in Postgres.
* Supabase pooler URLs (port 6543) and `?sslmode=require` are handled automatically.

## First test
1. Message the bot, `/start`, paste the invite token.
2. `/newcourse` -> send an outline PDF -> check the summary (correct anything in plain text) -> **Continue to plan**.
3. Edit the plan in plain text if needed -> **Confirm plan** -> **Generate** (everything / all labs / all tutorials /
   all assignments / pick items, e.g. "labs 2-5 and assignment 1").
4. Each item arrives as two .docx files. **Approve**, **Give feedback**, **Regenerate**, **Export .tex**.
   You can also just reply to an item's message with feedback text.

Before trusting a deployment, run the real outlines through your real keys:

    python scripts/check_outlines.py            # all PDFs in tests/fixtures; prints parse + plan + PASS/FAIL

## Commands
`/newcourse /courses (/switch) /plan /generate /items /rules /history <item> /logo /template /cancel /help`
(owner only: `/revoke <telegram_id|name>`).

## Behaviour worth knowing
* **Plan rules:** one lab or tutorial per teaching week (weeks that are only midterm/quiz/review/project-delivery are
  skipped; several sessions in a week merge into one item). Assignments are taken from the grading table; if none are
  listed, 3 are proposed and marked `[inferred]`. A bare "Project"/"Final Project" is not auto-planned as an
  assignment (add it with a plan edit if you want a handout for it). With no lab/tutorial column, items are proposed
  and marked `[inferred]`: labs if the outline names software/a language, tutorials otherwise.
* **Two LLM calls per item:** the student version first, then the answer key written from it (keeps numbering
  identical and fits free-tier output limits). Output uses delimiters instead of JSON so LaTeX backslashes survive.
* **Feedback:** each message is split into atomic rules, classified `course` or `item`-pinned (the bot tells you and
  gives Flip/Delete buttons). Latest wins: a contradicting new rule deactivates the older one in the same scope.
  Item-pinned rules never leak to other items and never switch off a course rule (they override it for their item).
  Other items are not auto-regenerated when a course rule is added; later regenerations pick it up.
* **Rate limits:** the router falls back across providers/models; if all are limited the job stays queued and
  resumes automatically (you are told once). Jobs survive restarts without duplicating versions.
* Not verified: generated math/code is **not** executed or checked. TAs review everything.

## Tests
    pip install -r requirements-dev.txt && pytest
Covers the services, the five feedback-loop acceptance scenarios, restart recovery, access control, native-equation
rendering, and a full Telegram journey through the real handlers using a fake Bot API and a scripted LLM.
