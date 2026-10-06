# TA Course-Material Bot (Telegram)

Upload a course outline (PDF) -> get a term plan -> generate **labs, tutorials and assignments**, each as a student
.docx and an answer-key .docx (native Word equations, monospace code). Review, approve, and give free-text
feedback; feedback becomes rules (course / type / item scope) applied to every later generation. Specs:
`ta_course_bot_spec.md` (original) and `TA_BOT_REWRITE_SPEC.md` (rules, worksheets, OCR, Telegram noise; supersedes
sections 6.5/6.6 of the original).

**Upgrading from an older version:** the schema changed without migrations. Reset the database once:

    python scripts/reset_db.py && alembic upgrade head      # type RESET to confirm; ALL data is deleted

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

## Flow
outline -> plan -> **rules gate** -> generate -> review.
1. `/newcourse` -> outline PDF (scanned PDFs are OCR'd) -> check the summary -> **Continue to plan** -> **Confirm plan**.
2. After the plan the bot offers: **Add a rule**, **Build format from worksheets**, **Review rules**, or **Continue**.
   Generation is blocked while any rule is pending (an undecided conflict or an unreviewed worksheet format).
3. **Generate** (everything / all labs / ... / pick items such as "labs 2-5 and assignment 1"). One live status
   message is edited as the job runs; each item arrives as two documents (answer key, then the student sheet with the
   buttons). No zips. Jobs with several items end with one summary message.
4. **Approve**, **Give feedback**, **Regenerate**, **Export .tex**. Reply to an item's student document with
   feedback text, or use the button.

Before trusting a deployment, run the real outlines through your real keys:

    python scripts/check_outlines.py            # all PDFs in tests/fixtures; prints parse + plan + PASS/FAIL

## Rules
* Scopes: **course** (everything), **type** (all labs / tutorials / assignments), **item** (one item). Precedence in
  the prompt: item over type over course (item rules come last).
* Rules are **never** deactivated, deleted or rewritten automatically. Only your tap changes a rule. When a new rule
  truly conflicts with an existing one the bot shows both, gives its reason, and asks: Keep old / Keep new / Both are
  fine / Edit. Rules that merely differ in scope (e.g. an item rule vs a course rule) are not conflicts: the narrower
  scope wins and you are told in a note.
* Feedback is a one-off fix for that item unless you generalize it ("always", "from now on", a type or the whole
  course). Each rule card has buttons to widen it to the type or the course, or Undo (disables it; restore in
  /rules > Disabled).
* Regeneration is always from scratch under ALL applicable rules.
* `/rules` is the hub: groups, edit, change scope, disable/restore, add, pending decisions, style examples.

## Worksheets and style examples
Upload past worksheets (PDF) per type (**Upload reference** -> pick lab / tutorial / assignment). **Build format from
worksheets** extracts their format (numbering word, structure, question style, marks...) as an editable list of
type-scope rules and shows it for review; nothing applies until you approve. Approving can replace or keep an earlier
worksheet-derived spec. The first `EXAMPLES_PER_TYPE` uploads per type are also attached to prompts as style
examples (change which ones under /rules > Style examples).

## OCR
Scanned PDFs (outlines or worksheets) are OCR'd with Tesseract (`pytesseract`, `tesseract-ocr` is installed by the
Dockerfile; English by default - build with `--build-arg OCR_LANG_PACKAGES="tesseract-ocr-eng tesseract-ocr-fra"`
and set `OCR_LANGS=eng+fra` for more). Settings: `OCR_ENABLED`, `OCR_LANGS`, `OCR_DPI`, `OCR_MAX_PAGES`. If the
engine or language data is missing, or a scan yields no text, the bot says so instead of guessing.

## Other settings
`EXAMPLES_PER_TYPE` (2), `EXAMPLE_MAX_CHARS` (3500), `STATUS_EDIT_MIN_INTERVAL_S` (3; throttle of status-message
edits). All in `.env.example`.

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
* **Feedback:** each message is split into atomic rules (see Rules above); nothing is silently overwritten.
* **Rate limits:** the router falls back across providers/models; if all are limited the job stays queued and
  resumes automatically (the status message says so). Jobs survive restarts without duplicating versions.
* Not verified: generated math/code is **not** executed or checked. TAs review everything.

## Tests
    pip install -r requirements-dev.txt && pytest
Covers the rules engine, prompt layering, worksheet specs, OCR, the message budget, restart recovery, restart recovery, access control, native-equation
rendering, and a full Telegram journey through the real handlers using a fake Bot API and a scripted LLM.
