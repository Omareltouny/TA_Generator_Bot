# Spec: TA Course-Material Bot (Telegram)

> **Superseded in part:** sections 6.5 (feedback buttons), 6.6 (feedback handling, "latest wins" conflict resolution) and the related data model are replaced by `TA_BOT_REWRITE_SPEC.md`. Rules are no longer auto-deactivated, have course/type/item scope, and Telegram delivery is one live status message plus per-item documents.

Target implementer: Claude Code. This document is the single source of truth. All decisions in section 3 are confirmed by the owner.

---

## 1. Purpose

A Telegram bot used by TAs (and professors) to turn a **course outline** into a full term of **labs, tutorials and assignments**, each with a **student version** and an **answer key**, delivered as **.docx**. TAs review, approve, and give free-text feedback. Feedback is stored per course and applied to every future generation/regeneration in that course.

The output is a head start, not verified output. TAs verify content themselves. The bot does not execute code or verify math.

## 2. Locked decisions

| Area | Decision |
|---|---|
| Platform | Telegram bot, free-tier cloud host |
| Database | Postgres (e.g. Supabase free tier). No local-disk state; the host disk is ephemeral |
| LLMs | Free models via Groq, Gemini, OpenRouter, behind a provider router with fallback |
| Subjects | Any course (CS in any language, stats, calculus, linear algebra, discrete math, optimization, algorithms, computer architecture, etc.). No fixed subject list |
| Input | Course outline (required, PDF). Optional: lecture slides, optional zip/files of past labs/tutorials as style/content reference |
| Output | Per item: **two .docx files** (student version + answer key). Native Word equations, monospace code blocks. Optional extra: `.tex` export |
| Scope v1 | Labs, tutorials, assignments. **No** quizzes, exams, rubrics, variants |
| Plan step | Bot extracts a term plan from the outline and the user confirms/edits it before bulk generation |
| Selective generation | Generate one item, several items, all of a type (labs / tutorials / assignments), or everything |
| Feedback input | Free-text Telegram messages |
| Feedback scope | Course-level |
| Conflicts | Latest feedback wins |
| Revision | Any item, including already-approved ones, can be revised later by giving feedback |
| Access | Any TA with the invite token can use the bot and sees/uses **all** courses. No per-course sharing or roles |
| Document hygiene | No draft banners, watermarks or review notes inside documents |
| Branding | User can upload a university logo for document headers and pick a different output template than the default |

## 3. Confirmed decisions

1. **Token flow (confirmed).** The owner creates the bot and distributes an **invite token** (a secret string) to TAs. A TA opens the bot, presses Start, pastes the token, and their Telegram user ID is registered. After that they can upload material and create courses without further approval. Token is stored as an env var (`INVITE_TOKEN`) and can be rotated; rotating does not remove already-registered users. The owner (`OWNER_TELEGRAM_ID` env var) can `/revoke <user>`.
2. **Item-pinned corrections (confirmed).** Feedback is course-level, but a purely item-specific correction (e.g. "Q3 answer should be 42") must not be lost when that item is regenerated. So rules have a nullable `item_id`. `NULL` = applies to the whole course; non-null = pinned to that item and only injected when generating that item. The LLM classifies each feedback message into one or the other, and the bot tells the user which it chose so they can flip it.
3. **Access (confirmed).** All registered TAs see and can act on all courses. No owners, share codes, join flow or roles. Latest-wins means roles don't affect conflict resolution.

## 4. Architecture

- **Language/runtime:** Python 3.11+, fully async.
- **Telegram:** `python-telegram-bot` v21+ (async). Use webhooks if the host supports it, otherwise long polling. Make it a config switch.
- **DB access:** SQLAlchemy 2.x async + asyncpg, Alembic migrations.
- **Background jobs:** DB-backed `jobs` table plus an in-process async worker loop (no Redis). Jobs survive restarts (on boot, requeue jobs stuck in `running`).
- **Document pipeline:** LLM produces Markdown with LaTeX math (`$...$`, `$$...$$`) and fenced code blocks, then **pandoc** converts to docx with native OMML equations using a `reference.docx` for styles, then `python-docx` post-processes (logo in header, page numbers, monospace style check). Ship pandoc in the Docker image. Optional `.tex` export is also via pandoc.
- **PDF/text extraction:** `pymupdf` first, fall back to `pdfplumber` for tables. If a page has no text layer, say so to the user rather than guessing.
- **Packaging:** Dockerfile, `.env.example`, `README` with deploy steps for a free-tier host.

### Module layout

```
bot/
  main.py                # app bootstrap, handlers registration
  config.py              # env parsing
  handlers/
    auth.py              # /start, token gate, /revoke
    courses.py           # create/list/select course
    upload.py            # outline/slides/reference/logo uploads
    plan.py              # plan review + edit
    generate.py          # selection menus, job creation
    review.py            # send files, approve, feedback entry
    feedback.py          # free-text feedback handling
  services/
    outline_parser.py    # outline -> structured course JSON
    planner.py           # course JSON -> plan items
    generator.py         # item -> student_md + key_md
    feedback_store.py    # rule extraction, conflict resolution, retrieval
    renderer.py          # md -> docx (pandoc + python-docx), tex export
    llm_router.py        # provider routing, retries, rate limits
    jobs.py              # queue + worker
  db/
    models.py
    migrations/
  templates/
    reference.docx
tests/
```

Keep modules small and documented. Every service has a pure-function core that can be unit-tested without Telegram or a live LLM.

## 5. Data model (Postgres)

```
users(id, telegram_id UNIQUE, name, registered_at, revoked bool)
courses(id, created_by_user_id, code, name, term, language_hint, outline_json JSONB,
        logo_blob BYTEA NULL, template_id, created_at)
materials(id, course_id, kind, filename, extracted_text, uploaded_by, created_at)
           -- kind: outline | slides | reference | logo
plan_items(id, course_id, type, week, seq, title, topics JSONB, due_date NULL,
           weight NULL, source TEXT, status, current_version, created_at)
           -- type: lab | tutorial | assignment
           -- status: planned | generating | draft | approved | failed
item_versions(id, item_id, version, student_md, key_md,
              student_docx BYTEA, key_docx BYTEA, tex BYTEA NULL,
              llm_provider, llm_model, feedback_rule_ids JSONB, created_at)
feedback_messages(id, course_id, item_id NULL, user_id, raw_text, created_at)
feedback_rules(id, course_id, item_id NULL, rule_text, source_message_id,
               active bool, superseded_by NULL, created_at)
jobs(id, course_id, kind, payload JSONB, status, progress, error,
     created_by, created_at, started_at, finished_at)
```

Store docx as BYTEA (or object storage if added later). Never rely on local disk. Keep every version.

## 6. User flows

### 6.1 Onboarding
1. `/start`: if unregistered, ask for the invite token.
2. Valid token: register the user, show main menu. Invalid: reject, rate-limit attempts per Telegram ID.
3. Registered users see: **Courses** (all courses) and **New course**.

### 6.2 New course
1. Ask for the outline PDF (required). Optional prompts: lecture slides, past material (individual files or one zip), logo image.
2. Parse the outline into `outline_json` (see 7.1). Show a short summary: course name/code, number of weeks, assessments found. Ask the user to correct anything wrong via free text. Corrections update `outline_json`.
3. Extract text from slides/reference files and store in `materials`. Cap per-file and total size, tell the user what was skipped.

### 6.3 Plan confirmation
1. `planner` builds plan items from `outline_json`:
   - **Assignments:** one per row in the assessment table whose name matches assignment/homework/project-like entries, with due week/date and weight attached.
   - **Labs/tutorials:** one per schedule week where the outline lists a lab/tutorial column or weekly worksheet. If the outline gives no lab/tutorial info, propose one lab per teaching week and flag it as inferred.
   - Skip weeks marked midterm/quiz/exam/study/project-delivery/review.
2. Send the plan as a numbered message, grouped by type (week, title, topics, source: `outline` or `inferred`).
3. Buttons: **Confirm**, **Edit**. Edit accepts free text ("drop lab 5, add a tutorial for week 6 on AVL trees, move assignment 2 to week 7"). The LLM converts it to plan operations, the bot applies them and re-shows the plan. Repeat until confirmed.
4. No generation starts before the plan is confirmed. Plan can be edited again later (add/remove items).

### 6.4 Generation menu
Buttons: **Everything**, **All labs**, **All tutorials**, **All assignments**, **Pick items**.
- **Pick items:** multi-select list of plan items (paginated inline keyboard), plus a free-text shortcut ("labs 2-5 and assignment 1").
- Creates one job. Items are generated sequentially; the bot sends a progress message (edit-in-place) and delivers each item as soon as it finishes (two docx files per item, student version first).
- Failures are per-item (status `failed`, reason shown) and do not abort the batch. A **Retry failed** button appears at the end.
- Only `planned` and `failed` items are generated by default. Re-running an already-generated item goes through regeneration (6.6).

### 6.5 Review, approve, feedback
- Each delivered item has inline buttons: **Approve**, **Give feedback**, **Regenerate**, **Export .tex**.
- **Approve** sets status `approved`. It does not lock the item.
- **Give feedback** (or replying to the item's message with text) puts the bot in feedback mode for that item. The user sends free text, possibly several messages. A **Done** button (or a short idle timeout) closes input.
- Approved items stay reachable from **Courses > Course > Items**, with filters by type and status. Selecting any item, approved or not, offers **Give feedback**. This is how errors found later get fixed.

### 6.6 Feedback handling and regeneration
On feedback for item X in course C:
1. Save the raw text in `feedback_messages`.
2. `feedback_store` extracts atomic rules and classifies each as `course` (item_id NULL) or `item` (item_id = X). The bot replies with the extracted list, e.g. "Stored as course rule: ... / Pinned to this item: ...", with a button to flip any rule's scope or delete it.
3. **Conflict resolution, latest wins:** for each new rule, compare against active rules in the same scope (and course rules when pinning). If one contradicts or supersedes an older rule, mark the older one `active=false, superseded_by=new`. Tell the user which rule was replaced.
4. Immediately create a new version of item X: regenerate with all active course rules plus rules pinned to X. Deliver the new files. The previous version stays in history.
5. Status after feedback-driven revision: `draft` (the user re-approves). If the item was previously approved, say so in the message.
6. Later regeneration of **any** item in the course (single, multiple, all) injects all active course rules plus that item's pinned rules. Other items are **not** auto-regenerated when a course rule is added.
7. Commands: `/rules` lists active course rules, with delete and edit. `/history <item>` lists versions and can re-send or roll back to an earlier version.

### 6.7 Other commands
- `/courses`, `/switch`, `/rules`, `/logo` (upload/replace), `/template` (choose output template), `/cancel` (cancel the current job), `/help`.

## 7. Generation details

### 7.1 Outline parsing
- Input: extracted outline text (+ tables). Output JSON validated against a schema:
  ```
  { course_code, course_name, term, language_hint, description,
    textbooks[], software[], learning_objectives[],
    schedule[{week, topics[], lab_or_tutorial_text, notes}],
    assessments[{name, type, weight, due, week}] }
  ```
- Outlines in the wild are inconsistent (see acceptance tests). Parsing must tolerate missing sections, merged tables and split pages. Any field it cannot find is `null`; it never invents values. Show uncertain parses to the user for correction.

### 7.2 Item generation
- Prompt inputs: course JSON, the item's plan entry (type, week, topics), neighbouring items (to avoid duplication and to keep a progression), relevant slides/reference excerpts if available (chunk and retrieve; do not stuff everything), and active feedback rules (course + pinned).
- Output contract: strict JSON `{title, student_md, key_md}`, validated and repaired/retried on schema failure. Markdown uses `$...$` for math and fenced code blocks with a language tag.
- Answer key must correspond one-to-one with student questions (same numbering, step-by-step solutions, code solutions where applicable). A validation pass checks question counts match between the two versions; mismatch triggers one retry.
- Type conventions:
  - **Lab:** hands-on, scaffolded tasks, starter code or setup where the subject is programming, progressive difficulty, expected output.
  - **Tutorial:** worked problem-solving practice, conceptual and computational questions.
  - **Assignment:** graded work aligned with the assessment entry (weight/due week), a clear marks breakdown inside the student version, and stated submission expectations when the outline provides them.
- Language/stack comes from the outline (software list, textbook, course name), overridable by feedback rules. The bot must not assume Java/Python.
- Reference material is optional. When present it informs style, difficulty and formatting. The bot must not copy it verbatim.
- Generated documents contain **no** draft banners, no "needs review" notes, no AI disclaimers.

### 7.3 Rendering
- Student and key docs share a template: logo (if uploaded) in header, course code/name, item title, week, type. Key doc is titled "... Answer Key" (this is the only difference in header content).
- Equations: native Word equations (OMML) via pandoc. Verify that equations are editable in Word, not images.
- Code: monospace style from `reference.docx`, preserved line breaks and indentation.
- Optional `.tex` export of each version via pandoc.
- Templates: `reference.docx` is the default. `/template` lets the user upload their own `.docx` to use as the pandoc reference document (styles for headings, body, code). Validate that required styles exist; warn if missing.

### 7.4 LLM router
- Providers: Groq, Gemini, OpenRouter, each with a configurable model list.
- Per-provider rate limit tracking, exponential backoff, automatic fallback to the next provider on 429/5xx/timeouts. Log provider/model per version (`item_versions`).
- Long inputs: truncate/chunk deliberately (outline text is small; slides/reference go through chunk-and-retrieve).
- JSON mode where the provider supports it, otherwise schema-validate and retry.
- All keys via env vars. No keys in the repo.

## 8. Telegram constraints

- Bots can download files up to 20 MB via the standard Bot API and upload up to 50 MB. Reject larger uploads with a clear message and suggest splitting/zipping.
- Zip uploads: extract safely (guard against zip bombs and path traversal), accept pdf/docx/pptx/txt/md/code files, ignore the rest and report what was skipped.
- Handle message length limits (4096 chars) by splitting plan/rule lists.
- Use inline keyboards for menus. Free text is always accepted where a flow expects it.

## 9. Security and access

- Every handler checks that the Telegram user is registered and not revoked.
- Invite-token attempts are rate-limited. Token comparison is constant-time.
- Course data is only visible to registered users.
- Uploaded material and generated documents are treated as private. Only send them to registered users.
- No secrets in logs. Redact tokens/keys.

## 10. Reliability

- All state in Postgres. A restart mid-job resumes or requeues; the user is notified.
- Idempotent job steps: regenerating an item creates a new version; a retry never duplicates a version.
- Structured logging with job id, course id, item id.
- Graceful degradation: if all LLM providers are rate-limited, tell the user, keep the job queued, and retry later with backoff instead of failing the batch.

## 11. Non-goals (v1)

Quizzes/exams/rubrics, multiple variants per item, running or auto-testing generated code, verifying math automatically, professor-specific roles, web UI, payments.

## 12. Build order

1. Project skeleton, config, Dockerfile, Alembic, DB models.
2. Auth: invite token gate, owner revoke.
3. Course creation + outline upload + outline parser with schema validation.
4. LLM router with provider fallback and tests using stubbed providers.
5. Planner + plan confirmation/edit flow.
6. Generator (student + key JSON), validation, rendering to docx (pandoc + post-processing), delivery of two files.
7. Job queue, progress updates, selective generation menu.
8. Review: approve, feedback capture, rule extraction/classification, conflict resolution, regeneration with rules, versions/history, `/rules`.
9. Materials: slides/reference/zip ingestion, chunk + retrieve.
10. Logo upload, template choice, `.tex` export.
11. Hardening: rate limits, restart recovery, size limits, tests, README.

## 13. Acceptance tests

Use the seven provided outlines as fixtures (the bot must work on all without code changes):

| Outline | What it exercises |
|---|---|
| Math 1920 (Calculus II) | Weekly worksheets in a lab column, 3 assignments in the assessment table with due dates, math-heavy equations |
| CS-4650 Video Game Architecture | Optional labs, project, Unreal Engine content, non-text-only subject |
| CS-2820 Programming Practices | Unix/shell/C course, schedule with sub-bullets, quizzes and two midterms in the schedule |
| CS-3130 Android Development | "Participation" tasks per week, Kotlin, project milestones |
| CS-2920 Data Structures and Algorithms | Dated schedule (summer), Java, assignments with due dates and best-of quiz rule |
| MCS-3320 Theory of Computing | No lab column, only lecture topics: planner must propose inferred items and flag them; proof/automata notation |
| CS-4350 Computer Graphics | OpenGL/C++, quizzes and assignments interleaved by week |

Checks:
- Outline parse: each fixture yields valid `outline_json`; missing fields are `null`, not invented.
- Plan: assignments match the assessment tables (count, week/due date); midterm/quiz/review weeks produce no labs; MCS-3320 plan items are flagged as inferred.
- Generation: each item yields two docx files; question numbering matches between student and key; equations are native OMML (open the docx, check `m:oMath` elements); code in monospace style; no draft/review text anywhere.
- Selective generation: single item, multi-select, all labs, all tutorials, all assignments, everything.
- Feedback loop:
  1. Give feedback on an item. A new version is created and rules are stored.
  2. Regenerate a different item in the same course. The stored course rule is reflected.
  3. Give contradicting feedback later. The older rule is deactivated and the bot reports the replacement.
  4. Approve an item, then give feedback on it. A new version is produced, the status goes back to `draft`, and the history keeps both versions.
  5. Item-pinned correction survives regeneration of that item and does not leak into other items.
- Access: unregistered user is blocked; wrong token rejected; after registering with the right token, a TA sees and can act on courses created by other TAs; a revoked user is blocked again.
- Restart: kill the process mid-job; on boot the job resumes/requeues and no duplicate versions appear.
